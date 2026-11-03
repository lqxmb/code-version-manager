#!/usr/bin/python3
import configparser
import html
import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote, unquote, urlencode, urlsplit

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402


def load_application_version() -> str:
    source = Path(__file__).resolve()
    for version_file in (source.with_name("VERSION"), source.parents[1] / "VERSION"):
        try:
            version = version_file.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if version:
            return version
    return "unknown"


APP_ID = "cn.local.CodeVersionManager"
APP_NAME = "工作空间版本管理"
APP_VERSION = load_application_version()
DEFAULT_REPOSITORY = Path.home() / "catkin_ws"
VERSION_PATTERN = re.compile(r"^v(\d+(?:\.\d+){2,})$")
PRERELEASE_PATTERN = re.compile(
    r"^(v\d+(?:\.\d+){2,})-beta\.(0\.0\.[1-9]\d*|[1-9]\d*)$"
)
FORMAL_VERSION_FILE_PATTERN = re.compile(r"^\d+(?:\.\d+){2,}$")
PRERELEASE_VERSION_FILE_PATTERN = re.compile(
    r"^\d+(?:\.\d+){2,}-beta\.(?:0\.0\.[1-9]\d*|[1-9]\d*)$"
)
METADATA_DIRECTORY = "code-version-manager"
MANIFEST_DIRECTORY = "versions"
MANIFEST_SUFFIX = ".manifest"
MANAGER_CHANGELOG = "CHANGELOG.rst"
WORKSPACE_CHANGELOG = "CHANGELOG.rst"
WORKSPACE_VERSION = "VERSION"
VERSION_SYNC_CONFIG = "codeversionmanager.syncVersionFile"
RECENT_WORKSPACE_LIMIT = 10
DELETE_TAG_RESPONSE = 1
DELETE_COMMIT_RESPONSE = 2
REMOTE_TIMEOUT = 120
PUSH_TIMEOUT = 900
MAX_PUSH_TIMEOUT = 3600
DEFAULT_EXCLUDED_DIRECTORIES = {
    ".git",
    ".cache",
    "__pycache__",
    "build",
    "devel",
    "install",
    "log",
    "logs",
}
DEFAULT_WORKSPACE_GITIGNORE = """# Catkin-generated output
/build/
/devel/
/install/
/build_isolated/
/devel_isolated/
/install_isolated/
/.catkin_workspace

# Local editor settings
/.vscode/

# Logs and caches
/log/
/logs/
/.cache/
__pycache__/
*.py[cod]
.pytest_cache/

# Editor temporary files
*.swp
*~

# ZIP exports
*.zip

# Local release notes, read by the publish dialog
/CHANGELOG.rst
"""
# Ubuntu's bcompare launcher re-quotes arguments, so this must not end in a backslash.
# The order is the same as Beyond Compare's Name Filters view: include files,
# include folders, then the excluded file/folder masks.
BCOMPARE_FILTER = "*;*;-.git\\;-*.zip;-__pycache__/"
COMPARE_TOOLS = {
    "bcompare": ("Beyond Compare", "bcompare"),
    "meld": ("Meld", "meld"),
    "kdiff3": ("KDiff3", "kdiff3"),
}
DEFAULT_COMPARE_TOOL = "bcompare"

class GitError(RuntimeError):
    pass


def parse_version_tag(name: str) -> Optional[Tuple[Optional[str], str]]:
    if name.startswith("refs/tags/"):
        name = name[len("refs/tags/"):]
    if VERSION_PATTERN.fullmatch(name):
        return None, name
    parts = name.split("/")
    if len(parts) == 3 and parts[0] == "branches" and PRERELEASE_PATTERN.fullmatch(parts[2]):
        owner = unquote(parts[1])
        if owner and quote(owner, safe="") == parts[1]:
            return owner, parts[2]
    return None


def version_tag_name(owner: str, version: str) -> str:
    return f"branches/{quote(owner, safe='')}/{version}"


def prerelease_sequence(version: str) -> int:
    parsed = PRERELEASE_PATTERN.fullmatch(version)
    return int(parsed.group(2).split(".")[-1]) if parsed else 0


def is_new_prerelease(version: str) -> bool:
    parsed = PRERELEASE_PATTERN.fullmatch(version)
    return bool(parsed and parsed.group(2).startswith("0.0."))


def display_version(name: str) -> str:
    parsed = parse_version_tag(name)
    return f"{parsed[0]}/{parsed[1]}" if parsed and parsed[0] else name


def canonical_version(value: str, branch: Optional[str] = None) -> str:
    if value.startswith("refs/tags/"):
        value = value[len("refs/tags/"):]
    if parse_version_tag(value):
        return value
    if branch and PRERELEASE_PATTERN.fullmatch(value):
        return version_tag_name(branch, value)
    parts = value.rsplit("/", 1)
    if len(parts) == 2 and parts[0] and PRERELEASE_PATTERN.fullmatch(parts[1]):
        return version_tag_name(*parts)
    return value


def version_sort_key(name: str):
    parsed = parse_version_tag(canonical_version(name))
    if not parsed:
        return ((-1,), 0, 0, name)
    owner, version = parsed
    base = version.split("-beta.", 1)[0]
    return (version_key(base), int(owner is not None), prerelease_sequence(version) if owner else 0, owner or "")


@dataclass(frozen=True)
class BranchInfo:
    name: str
    source: str
    oid: str
    current: bool = False


@dataclass(frozen=True)
class BranchChoice:
    """One user-facing branch row, optionally backed by local and origin refs."""

    name: str
    local: Optional[BranchInfo] = None
    remote: Optional[BranchInfo] = None

    @property
    def current(self) -> bool:
        return bool(self.local and self.local.current)

    @property
    def source(self) -> str:
        return "local" if self.local else "remote"

    @property
    def oid(self) -> str:
        branch = self.local or self.remote
        return branch.oid if branch else ""

    @property
    def label(self) -> str:
        details = []
        if self.current:
            details.append("当前")
        if self.local and self.remote:
            details.append("本地+origin")
        elif self.local:
            details.append("本地")
        else:
            details.append("origin")
        return f"{self.name}（{'，'.join(details)}）"

    @property
    def foreground(self) -> Optional[str]:
        return "#32804f" if self.current else None


def combine_branch_choices(
    local_branches: Sequence[BranchInfo],
    remote_branches: Sequence[BranchInfo],
) -> List[BranchChoice]:
    local_by_name = {branch.name: branch for branch in local_branches}
    remote_by_name = {branch.name: branch for branch in remote_branches}
    return [
        BranchChoice(name, local_by_name.get(name), remote_by_name.get(name))
        for name in sorted(set(local_by_name) | set(remote_by_name))
    ]


@dataclass(frozen=True)
class VersionTagInfo:
    ref: str
    owner: Optional[str]
    kind: str
    version: str
    commit: str
    oid: str
    read_only: bool = False

    @property
    def name(self) -> str:
        return self.ref[len("refs/tags/"):]

    @property
    def label(self) -> str:
        return display_version(self.name)


@dataclass(frozen=True)
class MergeState:
    active: bool
    source: str = ""
    conflicts: Tuple[str, ...] = ()
    notice: str = ""

    @property
    def ready(self) -> bool:
        return self.active and not self.conflicts


@dataclass(frozen=True)
class BranchDeletionPlan:
    name: str
    cleanup_tags: bool
    delete_remote: bool
    local_refs: Tuple[Tuple[str, str], ...]
    remote_url: str = ""
    remote_refs: Tuple[Tuple[str, str], ...] = ()


@dataclass(frozen=True)
class RemoteSyncSnapshot:
    remote_url: str
    branch_ref: str
    refs: Tuple[Tuple[str, str], ...]
    all_refs: Tuple[Tuple[str, str], ...] = ()


@dataclass(frozen=True)
class RefDifference:
    ref: str
    local_oid: Optional[str]
    remote_oid: Optional[str]


@dataclass(frozen=True)
class SyncCommit:
    oid: str
    date: str
    subject: str
    versions: Tuple[str, ...] = ()
    version_note: str = ""


@dataclass(frozen=True)
class RefComparison:
    difference: RefDifference
    local: Optional[SyncCommit]
    remote: Optional[SyncCommit]
    relationship: str
    local_count: int = 0
    remote_count: int = 0

    @property
    def name(self) -> str:
        ref = self.difference.ref
        if ref.startswith("refs/heads/"):
            return "分支 " + ref[len("refs/heads/"):]
        return "版本 " + display_version(ref[len("refs/tags/"):])

    @property
    def summary(self) -> str:
        return {
            "same": "两边指向同一提交",
            "tag_changed": "代码提交相同，标签信息不同",
            "local_ahead": f"本地较新，领先远程 {self.local_count} 个提交",
            "remote_ahead": f"远程较新，领先本地 {self.remote_count} 个提交",
            "diverged": (
                f"双方已分叉：本地独有 {self.local_count} 个提交，"
                f"远程独有 {self.remote_count} 个提交，无法简单判断新旧"
            ),
            "unrelated": "双方没有共同提交历史，无法判断新旧",
            "local_only": "仅本地存在（不能据此判断新旧）",
            "remote_only": "仅远程存在（不能据此判断新旧）",
            "unknown": "提交历史不完整或无法读取，暂时无法判断新旧",
        }[self.relationship]

    def effect(self, direction: str) -> str:
        target = "本地" if direction == "local" else "远程"
        source_oid = self.difference.remote_oid if direction == "local" else self.difference.local_oid
        target_oid = self.difference.local_oid if direction == "local" else self.difference.remote_oid
        if source_oid == target_oid:
            return "保持一致，无需修改"
        if not source_oid:
            if self.difference.ref.startswith("refs/heads/"):
                return f"来源不存在此分支，无法覆盖{target}"
            return f"将删除{target}的此版本标签"
        if not target_oid:
            kind = "分支" if self.difference.ref.startswith("refs/heads/") else "版本标签"
            return f"将在{target}新增此{kind}"
        if self.relationship == "tag_changed":
            return f"将替换{target}的标签信息，指向的代码提交相同"
        if self.relationship in ("local_ahead", "remote_ahead"):
            source_ahead = (
                self.relationship == "remote_ahead" if direction == "local"
                else self.relationship == "local_ahead"
            )
            count = self.local_count or self.remote_count
            if self.difference.ref.startswith("refs/tags/"):
                return f"将把{target}的此标签改为指向{'较新' if source_ahead else '较旧'}的提交"
            if source_ahead:
                return f"将更新{target}，补入 {count} 个提交"
            return f"将回退{target}，移出当前分支的 {count} 个提交"
        if self.relationship in ("diverged", "unrelated"):
            return f"将覆盖{target}的历史，请先核对双方修改"
        return f"将覆盖{target}，当前无法确认是否会回退"


@dataclass(frozen=True)
class RemoteSyncStatus:
    state: str
    message: str
    can_sync_local: bool = False
    can_sync_remote: bool = False
    local_changes: Tuple[RefDifference, ...] = ()
    remote_changes: Tuple[RefDifference, ...] = ()
    remote_refs: Tuple[Tuple[str, str], ...] = ()
    local_message: str = ""
    remote_message: str = ""
    comparisons: Tuple[RefComparison, ...] = ()
    comparison_error: str = ""
    all_remote_refs: Tuple[Tuple[str, str], ...] = ()


def settings_file_path() -> Path:
    return Path(GLib.get_user_config_dir()) / "code-version-manager" / "settings.ini"


def _read_settings(config_file: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    try:
        parser.read(config_file, encoding="utf-8")
    except (OSError, configparser.Error):
        pass
    return parser


def recent_workspaces(config_file: Optional[Path] = None) -> List[Path]:
    config_file = config_file or settings_file_path()
    parser = _read_settings(config_file)
    raw_paths = parser.get("recent_workspaces", "paths", fallback="[]")
    try:
        values = json.loads(raw_paths)
    except (TypeError, ValueError):
        return []
    if not isinstance(values, list):
        return []

    paths = []
    seen = set()
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        path = Path(value).expanduser().resolve()
        key = str(path)
        if key in seen or not path.is_dir():
            continue
        seen.add(key)
        paths.append(path)
        if len(paths) == RECENT_WORKSPACE_LIMIT:
            break
    return paths


def save_workspace_history(
    path: Path, config_file: Optional[Path] = None
) -> List[Path]:
    config_file = config_file or settings_file_path()
    resolved = path.expanduser().resolve()
    paths = [resolved]
    paths.extend(candidate for candidate in recent_workspaces(config_file) if candidate != resolved)
    paths = paths[:RECENT_WORKSPACE_LIMIT]

    parser = _read_settings(config_file)
    parser["repository"] = {"path": str(resolved)}
    parser["recent_workspaces"] = {
        "paths": json.dumps([str(candidate) for candidate in paths], ensure_ascii=False)
    }
    config_file.parent.mkdir(parents=True, exist_ok=True)
    with config_file.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    return paths


def remove_recent_workspace(path: Path, config_file: Optional[Path] = None) -> None:
    config_file = config_file or settings_file_path()
    resolved = path.expanduser().resolve()
    paths = [candidate for candidate in recent_workspaces(config_file) if candidate != resolved]
    parser = _read_settings(config_file)
    parser["recent_workspaces"] = {
        "paths": json.dumps([str(candidate) for candidate in paths], ensure_ascii=False)
    }
    config_file.parent.mkdir(parents=True, exist_ok=True)
    with config_file.open("w", encoding="utf-8") as handle:
        parser.write(handle)


def clear_recent_workspaces(config_file: Optional[Path] = None) -> None:
    config_file = config_file or settings_file_path()
    parser = _read_settings(config_file)
    parser["recent_workspaces"] = {"paths": "[]"}
    config_file.parent.mkdir(parents=True, exist_ok=True)
    with config_file.open("w", encoding="utf-8") as handle:
        parser.write(handle)


@dataclass(frozen=True)
class Change:
    status: str
    path: str


@dataclass(frozen=True)
class HistoryEntry:
    commit: str
    short_commit: str
    date: str
    subject: str
    tags: Sequence[str]
    read_only: bool = False

    @property
    def reference(self) -> str:
        return self.tags[0] if self.tags else self.commit


def run_remote_process(command: Sequence[str], timeout: int, **options):
    with subprocess.Popen(command, start_new_session=True, **options) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr) from None
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def redact_git_output(message: str) -> str:
    return re.sub(r"(https?://)[^/\s@]+@", r"\1[已隐藏账号]@", message)


def github_repository_coordinates(remote_url: str) -> Optional[Tuple[str, str]]:
    try:
        remote_url = validate_remote_url(remote_url, allow_https=True)
    except (GitError, ValueError):
        return None
    if remote_url.startswith("git@github.com:"):
        repository_path = remote_url.split(":", 1)[1]
    else:
        try:
            parsed = urlsplit(remote_url)
        except ValueError:
            return None
        if parsed.hostname != "github.com" or parsed.scheme not in ("https", "ssh"):
            return None
        repository_path = parsed.path
    repository_path = repository_path.strip("/")
    if repository_path.endswith(".git"):
        repository_path = repository_path[:-4]
    parts = repository_path.split("/")
    if len(parts) != 2:
        return None
    owner, repository_name = parts
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", owner):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repository_name) or repository_name in (".", ".."):
        return None
    return owner, repository_name


def workspace_repository_name(workspace_name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", workspace_name)[:100]
    return name if name.strip(".-") else "workspace"


def suggested_ssh_remote(remote_url: str) -> str:
    try:
        return validate_remote_url(remote_url)
    except (GitError, ValueError):
        coordinates = github_repository_coordinates(remote_url)
        return "git@github.com:{}/{}.git".format(*coordinates) if coordinates else ""


def repository_web_url(remote_url: str) -> str:
    remote_url = remote_url.strip()
    if not remote_url:
        raise GitError("请先填写 SSH 仓库地址。")
    try:
        validate_remote_url(remote_url, allow_https=True)
        if "://" in remote_url:
            parsed = urlsplit(remote_url)
            if parsed.scheme not in ("ssh", "https"):
                raise GitError("本地仓库地址没有对应的仓库网页。")
            host = parsed.hostname
            path = parsed.path
        else:
            match = re.fullmatch(r"[^@/:\s]+@([^:/\s]+):(.+)", remote_url)
            if not match:
                raise GitError("无法从该地址识别仓库网页，请检查 SSH 地址。")
            host, path = match.groups()
    except ValueError:
        raise GitError("SSH 仓库地址格式无效，无法打开网页。") from None

    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if (
        not host
        or not re.fullmatch(r"[A-Za-z0-9.-]+", host)
        or not path
        or any(ord(char) < 32 for char in path)
    ):
        raise GitError("无法从该地址识别仓库网页，请检查 SSH 地址。")
    return "https://{}/{}".format(host, quote(path, safe="/-._~"))


def github_repository_creation_url(remote_url: str, workspace_name: str) -> str:
    parameters = {"visibility": "private"}
    if remote_url.strip():
        coordinates = github_repository_coordinates(remote_url)
        if not coordinates:
            raise GitError("此创建入口仅适用于 GitHub。请填写 GitHub 仓库地址，或清空地址后创建。")
        owner, repository_name = coordinates
        parameters["owner"] = owner
    else:
        repository_name = workspace_repository_name(workspace_name)
    parameters["name"] = repository_name
    return "https://github.com/new?" + urlencode(parameters)


def ssh_public_keys(directory: Optional[Path] = None) -> Dict[str, str]:
    directory = directory if directory is not None else Path.home() / ".ssh"
    keys = {}
    for path in sorted(directory.glob("*.pub")):
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
                continue
            public_key = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        if re.fullmatch(
            r"(?:ssh-(?:ed25519|rsa)|ecdsa-sha2-nistp(?:256|384|521)|"
            r"sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com) "
            r"[A-Za-z0-9+/=]+(?: [^\r\n]*)?",
            public_key,
        ):
            keys[str(path)] = public_key
    return keys


def generate_ssh_key(email: str, passphrase: str = "", directory: Optional[Path] = None) -> Path:
    email = email.strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+", email) or any(ord(char) < 32 for char in email):
        raise GitError("请输入有效的密钥邮箱，例如 name@example.com。")
    if "\0" in passphrase:
        raise GitError("密钥口令不能包含空字符。")
    directory = directory if directory is not None else Path.home() / ".ssh"
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".cvm-key-", dir=str(directory)) as temporary:
            private = Path(temporary) / "id_ed25519"
            public = private.with_suffix(".pub")
            result = run_remote_process(
                ["ssh-keygen", "-q", "-t", "ed25519", "-C", email,
                 "-N", passphrase, "-f", str(private)],
                timeout=30, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            )
            if result.returncode != 0:
                raise GitError(result.stderr.strip() or "SSH 密钥生成失败。")
            if str(public) not in ssh_public_keys(Path(temporary)):
                raise GitError("ssh-keygen 未生成有效的 .pub 公钥，请检查 OpenSSH 安装。")
            # Publish both files without replacing any existing file, including symlinks.
            for index in range(1000):
                name = "id_ed25519" if index == 0 else f"id_ed25519_cvm_{index}"
                destination = directory / name
                public_destination = directory / (name + ".pub")
                if os.path.lexists(public_destination):
                    continue
                try:
                    os.link(private, destination)
                except FileExistsError:
                    continue
                try:
                    os.link(public, public_destination)
                except FileExistsError:
                    destination.unlink()
                    continue
                except OSError:
                    destination.unlink()
                    raise
                return destination
            raise GitError("没有可用的密钥文件名，请整理 ~/.ssh 中的密钥后重试。")
    except subprocess.TimeoutExpired:
        raise GitError("生成 SSH 密钥超过 30 秒，请稍后重试。") from None
    except FileNotFoundError:
        raise GitError("无法运行 ssh-keygen，请先安装 openssh-client。") from None
    except OSError as error:
        raise GitError(f"无法保存 SSH 密钥：{error}") from error


def validate_remote_url(value: str, *, allow_https: bool = False) -> str:
    value = value.strip()
    if not value or value.startswith("-") or any(ord(char) < 32 for char in value):
        raise GitError("请填写有效的远程仓库地址。")
    if "://" in value:
        try:
            parsed = urlsplit(value)
        except ValueError:
            raise GitError("远程仓库地址格式无效。") from None
        allowed_schemes = ("ssh", "file", "https") if allow_https else ("ssh", "file")
        if parsed.scheme not in allowed_schemes:
            raise GitError("远程同步仅支持 SSH，请填写 git@host:owner/repo.git、SSH URL 或本地仓库路径。")
        if parsed.scheme != "file" and not parsed.hostname:
            raise GitError("远程仓库地址缺少主机名。")
        if parsed.password is not None or (parsed.scheme == "https" and parsed.username):
            raise GitError("仓库地址不能包含密码等登录凭据。")
        if parsed.query or parsed.fragment:
            raise GitError("远程仓库地址不能包含查询参数或片段。")
    elif not (Path(value).is_absolute() or re.match(r"^[^\s@:]+@[^\s:]+:.+", value)):
        raise GitError("请填写 SSH 地址或本地仓库的绝对路径。")
    return value


def validate_git_identity(name: str, email: str) -> None:
    if not name.strip() or not email.strip():
        raise GitError("请填写 Git 提交姓名和邮箱。")
    if any(ord(char) < 32 for char in name + email):
        raise GitError("提交信息不能包含换行或控制字符。")


def validate_compare_tool(value: str) -> str:
    if value not in COMPARE_TOOLS:
        raise GitError("对比工具配置无效，请在“设置”中重新选择。")
    return value


class GitRepository:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        if not self.path.is_dir():
            raise GitError("所选目录不是 Git 工作区。")
        top_output = self.run(["rev-parse", "--show-toplevel"], check=False).strip()
        if not top_output:
            raise GitError("所选目录不是 Git 工作区。")
        top = Path(top_output).resolve()
        if top != self.path:
            raise GitError("所选目录没有独立的 Git 工作区。")
        git_directory = self.run(["rev-parse", "--absolute-git-dir"]).strip()
        if not git_directory:
            raise GitError("无法确定 Git 元数据目录。")
        self.metadata_path = Path(git_directory).resolve() / METADATA_DIRECTORY
        self.last_local_sync_stash_ref: Optional[str] = None

    def run(
        self,
        args: Sequence[str],
        check: bool = True,
        *,
        input: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.path), *args],
            input=input,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if check and result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "Git 操作失败。"
            raise GitError(message)
        return result.stdout

    def run_bytes(self, args: Sequence[str]) -> bytes:
        result = subprocess.run(
            ["git", "-C", str(self.path), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise GitError(message or "Git 操作失败。")
        return result.stdout

    def remote_url(self) -> str:
        return self.run(["config", "--get", "remote.origin.url"], check=False).strip()

    def push_timeout_seconds(self) -> int:
        configured = self.run(
            ["config", "--get", "codeversionmanager.pushTimeout"], check=False
        ).strip()
        if not configured:
            return PUSH_TIMEOUT
        try:
            seconds = int(configured)
        except ValueError:
            raise GitError("推送超时配置无效，请在“Git 账号”中设置 1–60 分钟。") from None
        if not 60 <= seconds <= MAX_PUSH_TIMEOUT:
            raise GitError("推送超时必须为 1–60 分钟，请在“Git 账号”中重新设置。")
        return seconds

    def compare_tool(self) -> str:
        configured = self.run(
            ["config", "--get", "codeversionmanager.compareTool"], check=False
        ).strip()
        if not configured:
            return DEFAULT_COMPARE_TOOL
        return validate_compare_tool(configured)

    def configure_compare_tool(self, compare_tool: str) -> None:
        self.run([
            "config", "--local", "codeversionmanager.compareTool",
            validate_compare_tool(compare_tool),
        ])

    def sync_version_file_enabled(self) -> bool:
        configured = self.run(["config", "--local", "--get", VERSION_SYNC_CONFIG], check=False)
        if not configured:
            return (self.path / WORKSPACE_VERSION).is_file()
        return self.run(["config", "--local", "--bool", "--get", VERSION_SYNC_CONFIG]).strip() == "true"

    def configure_version_file(self, enabled: bool) -> None:
        self.run(["config", "--local", VERSION_SYNC_CONFIG, "true" if enabled else "false"])

    def configure_remote(
        self,
        remote_url: str,
        name: str,
        email: str,
        push_timeout: Optional[int] = None,
        compare_tool: Optional[str] = None,
    ) -> None:
        remote_url = validate_remote_url(remote_url)
        validate_git_identity(name, email)
        if compare_tool is not None:
            compare_tool = validate_compare_tool(compare_tool)
        if push_timeout is not None and (
            not isinstance(push_timeout, int) or not 60 <= push_timeout <= MAX_PUSH_TIMEOUT
        ):
            raise GitError("推送超时必须为 1–60 分钟。")
        if "origin" not in self.run(["remote"]).splitlines():
            self.run(["remote", "add", "origin", remote_url])
        else:
            self.run(["config", "--local", "--replace-all", "remote.origin.url", remote_url])
            self.run(["config", "--local", "--unset-all", "remote.origin.pushurl"], check=False)
        self.run(["config", "--local", "user.name", name.strip()])
        self.run(["config", "--local", "user.email", email.strip()])
        self.run(["config", "--local", "--unset-all", "codeversionmanager.username"], check=False)
        self.run(["config", "--local", "--unset-all", "codeversionmanager.autoSync"], check=False)
        if push_timeout is not None:
            self.run(["config", "--local", "codeversionmanager.pushTimeout", str(push_timeout)])
        if compare_tool is not None:
            self.run([
                "config", "--local", "codeversionmanager.compareTool",
                compare_tool,
            ])

    def _run_remote(self, args: Sequence[str], remote_url: str) -> str:
        remote_url = validate_remote_url(remote_url)
        checking_push = bool(args and args[0] == "push" and "--dry-run" in args)
        pushing = bool(args and args[0] == "push" and not checking_push)
        fetching = bool(args and args[0] == "fetch")
        timeout = self.push_timeout_seconds() if pushing or fetching else REMOTE_TIMEOUT
        environment = os.environ.copy()
        environment.update({
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "SSH_ASKPASS": "/bin/false",
            "SSH_ASKPASS_REQUIRE": "never",
            "LC_ALL": "C",
        })
        environment.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes -oConnectTimeout=15")
        try:
            result = run_remote_process(
                ["git", "-C", str(self.path), *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            output = error.stderr or error.stdout or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            output = redact_git_output(output)
            last_output = "\n".join(output.replace("\r", "\n").splitlines()[-8:])[-2000:]
            phase = "推送分支与版本标签" if pushing else "获取远程分支和版本标签" if fetching else "连接或读取远程仓库"
            if checking_push:
                phase = "预检查推送权限（不上传版本）"
            message = (
                f"远程操作超时：{phase}超过 {timeout} 秒。\n"
                "本地提交和版本标签均已保留，不要重新发布同一版本。\n"
                "超时不能确认远程是否已经接收更新；恢复网络后点击“刷新”重新比较两端，再选择同步方向。\n"
                "请检查网络、代理或 VPN；读取成功不代表上传链路正常。"
            )
            if pushing:
                message += "\n传输缓慢时可在“Git 账号”中调整推送超时，默认 15 分钟。"
            if last_output:
                message += "\n\nGit 最后输出：\n" + last_output
            raise GitError(message) from None
        except OSError as error:
            raise GitError(f"无法执行远程 Git 操作：{error}") from error
        if result.returncode != 0:
            message = redact_git_output(
                result.stderr.strip() or result.stdout.strip() or "远程 Git 操作失败。"
            )
            if github_repository_coordinates(remote_url):
                if re.search(r"Permission denied \(publickey(?:,[^)]*)?\)", message, re.IGNORECASE):
                    raise GitError(
                        "GitHub 尚未接受本机 SSH 密钥。\n"
                        "请在“Git 账号”中点击“SSH 公钥设置”，将本机 .pub 公钥添加到 GitHub，"
                        "并在终端运行 ssh -T git@github.com 验证。加密私钥需先用 ssh-add 解锁。\n"
                        "不要上传或发送没有 .pub 后缀的私钥。本地提交和版本标签均已保留。\n\n"
                        "Git 原始错误：\n" + message
                    )
                if "Host key verification failed" in message:
                    raise GitError(
                        "尚未建立可信的 GitHub SSH 主机连接。请在终端运行 ssh -T git@github.com，"
                        "核对 GitHub 官方公布的主机指纹后确认；不要关闭主机密钥检查。\n"
                        "本地提交和版本标签均已保留。\n\nGit 原始错误：\n" + message
                    )
                if re.search(r"permission to .+ denied to|write access to repository not granted", message, re.IGNORECASE):
                    raise GitError(
                        "GitHub 拒绝当前 SSH 账号写入仓库。请检查密钥所属账号的仓库写权限，"
                        "以及分支保护规则和组织授权要求。\n"
                        "本地提交和版本标签均已保留，无需再次发布。\n\nGit 原始错误：\n" + message
                    )
            raise GitError(message)
        return result.stdout

    def check_remote_connection(self) -> str:
        if not self.current_branch():
            remote_url = self._sync_target()
            self._run_remote(["ls-remote", "--", remote_url], remote_url)
            return "远程仓库可读取；当前正在查看历史版本，未检查推送权限。请返回工作分支再验证。"
        snapshot = self.sync_snapshot()
        status = self.remote_sync_status(snapshot)
        if status.state == "unavailable":
            raise GitError(status.message)
        if status.can_sync_remote:
            return "远程可读取，推送预检查通过，当前分支和版本标签覆盖预检查通过，未上传版本。"
        if snapshot.branch_ref not in dict(snapshot.refs):
            return "远程可读取；尚无本地提交，未检查推送权限。"
        if not status.remote_changes:
            return "远程可读取；本地分支和版本标签已一致，当前无需上传。"
        if status.remote_changes:
            raise GitError(status.remote_message)
        return "远程可读取。"

    def _sync_target(self) -> str:
        urls = self.run(
            ["remote", "get-url", "--push", "--all", "origin"], check=False
        ).splitlines()
        if len(urls) != 1:
            raise GitError("请先通过“Git 账号”配置唯一的 origin 远程仓库地址。")
        return validate_remote_url(urls[0])

    @staticmethod
    def _managed_ref(ref: str) -> bool:
        return ref.startswith("refs/heads/") or (
            ref.startswith("refs/tags/") and parse_version_tag(ref) is not None
        )

    @staticmethod
    def _owned_ref(ref: str, branch: str) -> bool:
        parsed = parse_version_tag(ref)
        return parsed is not None and (
            (branch == "main" and parsed[0] is None) or (branch != "main" and parsed[0] == branch)
        )

    def _local_refs(self) -> Dict[str, str]:
        refs = {}
        for line in self.run([
            "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/", "refs/tags/",
        ]).splitlines():
            ref, oid = line.split()
            if self._managed_ref(ref):
                refs[ref] = oid
        return refs

    def _remote_refs(self, remote_url: str) -> Dict[str, str]:
        refs = {}
        for line in self._run_remote(["ls-remote", "--refs", "--", remote_url], remote_url).splitlines():
            fields = line.split()
            if len(fields) == 2 and self._managed_ref(fields[1]):
                refs[fields[1]] = fields[0]
        return refs

    def _contains_commit(self, commit: str, head: str) -> bool:
        result = subprocess.run(
            ["git", "-C", str(self.path), "merge-base", "--is-ancestor", commit, head],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        if result.returncode not in (0, 1):
            raise GitError(result.stderr.strip() or "无法判断提交的合并关系。")
        return result.returncode == 0

    def _ensure_objects(self, remote_url: str, refs: Dict[str, str]) -> None:
        self._ensure_full_history()
        if not refs:
            return
        output = self.run(
            ["cat-file", "--batch-check=%(objectname) %(objecttype)"],
            input="\n".join(sorted(set(refs.values()))) + "\n",
        )
        missing = [line.split()[0] for line in output.splitlines() if line.endswith(" missing")]
        if missing:
            self._run_remote([
                "fetch", "--no-tags", "--refmap=", "--no-auto-gc", "--no-recurse-submodules",
                remote_url, *missing,
            ], remote_url)

    def _ensure_full_history(self) -> None:
        if self.run(["rev-parse", "--is-shallow-repository"]).strip() == "true":
            raise GitError("当前为浅克隆，历史不完整；请先获取完整历史再执行版本同步或合并。")

    def _shared_release_branches(
        self, oid: str, local: Dict[str, str], remote: Dict[str, str],
    ) -> List[str]:
        commit = self.run(["rev-parse", "--verify", f"{oid}^{{commit}}"]).strip()
        return [
            prefix + ref[len("refs/heads/"):]
            for prefix, refs in (("本地 ", local), ("origin/", remote))
            for ref, head in refs.items()
            if ref.startswith("refs/heads/") and ref != "refs/heads/main"
            and self._contains_commit(commit, head)
        ]

    def _validate_sync_confirmation(self, snapshot: RemoteSyncSnapshot, status: RemoteSyncStatus) -> None:
        if self.sync_snapshot() != snapshot:
            raise GitError("本地分支或版本标签已变化，请刷新后重新确认。")
        if tuple(sorted(self._remote_refs(snapshot.remote_url).items())) != status.all_remote_refs:
            raise GitError("远程分支或版本标签已变化，请刷新后重新确认。")

    def sync_snapshot(self, require_versions: bool = False) -> RemoteSyncSnapshot:
        self._assert_no_operation()
        branch = self.current_branch()
        if not branch:
            raise GitError("查看历史版本时不能同步远程，请先返回工作分支。")
        remote_url = self._sync_target()
        branch_ref = f"refs/heads/{branch}"
        all_refs = self._local_refs()
        commit = all_refs.get(branch_ref)
        refs = [(branch_ref, commit)] if commit else []
        refs.extend(sorted((ref, oid) for ref, oid in all_refs.items() if self._owned_ref(ref, branch)))
        if require_versions and (not commit or not any(ref.startswith("refs/tags/") for ref, _oid in refs)):
            raise GitError("当前分支尚无可同步的版本，请先发布版本。")
        return RemoteSyncSnapshot(remote_url, branch_ref, tuple(refs), tuple(sorted(all_refs.items())))

    @staticmethod
    def _push_arguments(snapshot: RemoteSyncSnapshot, dry_run: bool = False) -> List[str]:
        options = ["--dry-run", "--no-verify"] if dry_run else ["--progress", "--set-upstream"]
        return [
            "push", "--atomic", "--porcelain", "--no-follow-tags", *options,
            "origin", *[f"{ref}:{ref}" for ref, _object_id in snapshot.refs],
        ]

    def remote_sync_status(self, snapshot: RemoteSyncSnapshot) -> RemoteSyncStatus:
        try:
            all_remote = self._remote_refs(snapshot.remote_url)
            self._ensure_objects(snapshot.remote_url, all_remote)
        except GitError as error:
            return RemoteSyncStatus(
                "unavailable", f"无法检查远程状态：{error}\n修复后点击“刷新”重新检查。"
            )
        branch = snapshot.branch_ref[len("refs/heads/"):]
        remote_refs = {
            ref: oid for ref, oid in all_remote.items()
            if ref == snapshot.branch_ref or self._owned_ref(ref, branch)
        }
        local_refs = dict(snapshot.refs)
        remote_changes = self._ref_differences(local_refs, remote_refs)
        local_changes = self._ref_differences(local_refs, remote_refs)
        if snapshot.branch_ref not in remote_refs:
            local_available = False
            local_message = "远程不存在当前分支；为保护本地工作分支，不能从远程同步本地。"
        else:
            local_available = bool(local_changes)
            local_message = "远程分支和版本标签可用于同步本地。"
        if not local_changes:
            local_message = "当前分支和版本标签已与远程一致。"
        remote_available = bool(remote_changes) and any(
            ref == snapshot.branch_ref for ref, _oid in snapshot.refs
        )
        remote_message = "尚无本地提交，未检查推送权限。"
        if remote_available:
            try:
                self.push_timeout_seconds()
                self._run_remote(
                    self._push_ref_differences(snapshot, remote_refs, dry_run=True),
                    snapshot.remote_url,
                )
                remote_message = "推送预检查通过；确认后将以本地分支和版本标签覆盖远程差异。"
            except GitError as error:
                remote_available = False
                remote_message = f"远程覆盖预检查失败：{error}"
        elif not remote_changes:
            remote_message = "当前分支和版本标签已与远程一致。"
        try:
            local_blocks, remote_blocks = [], []
            for change in local_changes:
                parsed = parse_version_tag(change.ref)
                if parsed and parsed[0] is None:
                    owners = sorted(set(
                        branch
                        for oid in (change.local_oid, change.remote_oid) if oid
                        for branch in self._shared_release_branches(oid, dict(snapshot.all_refs), all_remote)
                    ))
                    if owners:
                        notice = f"{change.ref[len('refs/tags/'):]}：其他活动分支也包含此正式标签（{'、'.join(owners)}），不能改写或删除。"
                        if change.local_oid:
                            local_blocks.append(notice)
                        if change.remote_oid:
                            remote_blocks.append(notice)
            if local_blocks:
                local_available = False
                local_message = "\n".join(local_blocks)
            if remote_blocks:
                remote_available = False
                remote_message = "\n".join(remote_blocks)
        except GitError as error:
            return RemoteSyncStatus("unavailable", f"无法确认正式标签的共享范围，已停止同步：{error}")
        differences = tuple(sorted(set(local_changes + remote_changes), key=lambda item: item.ref))
        comparisons, comparison_error = (
            self._sync_comparisons(snapshot, remote_refs) if differences else ((), "")
        )
        message = "当前分支和版本标签已与远程一致。" if not differences else (
            f"{comparisons[0].name}：{comparisons[0].summary}。"
            f"共 {len(differences)} 项分支或版本标签差异，点击同步查看详情。"
        )
        return RemoteSyncStatus(
            "synced" if not differences else "pending",
            message,
            local_available,
            remote_available,
            local_changes,
            remote_changes,
            tuple(sorted(remote_refs.items())),
            local_message,
            remote_message,
            comparisons,
            comparison_error,
            tuple(sorted(all_remote.items())),
        )

    def _sync_comparisons(
        self, snapshot: RemoteSyncSnapshot, remote_refs: Dict[str, str],
    ) -> Tuple[Tuple[RefComparison, ...], str]:
        local_refs = dict(snapshot.refs)
        comparison_error = ""
        if remote_refs:
            object_ids = sorted(set(remote_refs.values()))
            objects = self.run(
                ["cat-file", "--batch-check=%(objectname) %(objecttype)"],
                input="\n".join(object_ids) + "\n",
            )
            missing = [line.split()[0] for line in objects.splitlines() if line.endswith(" missing")]
            if missing:
                try:
                    # Fetch missing objects without changing branches, tags or the worktree.
                    self._run_remote([
                        "fetch", "--no-tags", "--no-auto-gc", "--refmap=",
                        "--no-recurse-submodules",
                        snapshot.remote_url, *missing,
                    ], snapshot.remote_url)
                except GitError as error:
                    comparison_error = f"获取远程提交详情失败：{error}"

        commits = {}
        for object_id in sorted(set(local_refs.values()) | set(remote_refs.values())):
            try:
                commit_id = self.run(["rev-parse", "--verify", f"{object_id}^{{commit}}"]).strip()
                details = self.run([
                    "show", "--no-patch", "--format=%H%x00%ci%x00%s", commit_id,
                ]).strip().split("\0", 2)
                version_note = ""
                if object_id != commit_id:
                    tag = self.run(["cat-file", "-p", object_id])
                    version_note = tag.split("\n\n", 1)[-1].strip()
                commits[object_id] = SyncCommit(*details, version_note=version_note) if len(details) == 3 else None
            except GitError:
                commits[object_id] = None

        def with_versions(object_id: Optional[str], refs: Dict[str, str]) -> Optional[SyncCommit]:
            commit = commits.get(object_id)
            if not commit:
                return None
            versions = tuple(sorted((
                display_version(ref[len("refs/tags/"):]) for ref, oid in refs.items()
                if ref.startswith("refs/tags/") and commits.get(oid)
                and commits[oid].oid == commit.oid
            ), key=version_sort_key, reverse=True))
            return SyncCommit(commit.oid, commit.date, commit.subject, versions, commit.version_note)

        shallow = self.run(["rev-parse", "--is-shallow-repository"]).strip() == "true"
        refs = [snapshot.branch_ref] + sorted(
            (set(local_refs) | set(remote_refs)) - {snapshot.branch_ref},
            key=lambda ref: version_sort_key(ref[len("refs/tags/"):]), reverse=True,
        )
        comparisons = []
        for ref in refs:
            local_oid, remote_oid = local_refs.get(ref), remote_refs.get(ref)
            if ref != snapshot.branch_ref and local_oid == remote_oid:
                continue
            local = with_versions(local_oid, local_refs)
            remote = with_versions(remote_oid, remote_refs)
            local_count = remote_count = 0
            if local_oid == remote_oid:
                relationship = "same"
            elif not local_oid:
                relationship = "remote_only"
            elif not remote_oid:
                relationship = "local_only"
            elif not local or not remote:
                relationship = "unknown"
            elif local.oid == remote.oid:
                relationship = "tag_changed"
            elif shallow:
                relationship = "unknown"
            else:
                try:
                    counts = self.run([
                        "rev-list", "--left-right", "--count", f"{local.oid}...{remote.oid}",
                    ]).split()
                    local_count, remote_count = map(int, counts)
                    if not remote_count:
                        relationship = "local_ahead"
                    elif not local_count:
                        relationship = "remote_ahead"
                    elif self.run(["merge-base", local.oid, remote.oid], check=False).strip():
                        relationship = "diverged"
                    else:
                        relationship = "unrelated"
                except (GitError, ValueError):
                    relationship = "unknown"
            comparisons.append(RefComparison(
                RefDifference(ref, local_oid, remote_oid), local, remote,
                relationship, local_count, remote_count,
            ))
        return tuple(comparisons), comparison_error

    @staticmethod
    def _ref_differences(
        source: Dict[str, str], target: Dict[str, str],
    ) -> Tuple[RefDifference, ...]:
        return tuple(
            RefDifference(ref, source.get(ref), target.get(ref))
            for ref in sorted(set(source) | set(target))
            if source.get(ref) != target.get(ref)
        )

    @staticmethod
    def _push_ref_differences(
        snapshot: RemoteSyncSnapshot, remote_refs: Dict[str, str], dry_run: bool = False,
    ) -> List[str]:
        local_refs = dict(snapshot.refs)
        args = ["push", "--atomic", "--porcelain", "--no-follow-tags"]
        args.extend(["--dry-run", "--no-verify"] if dry_run else ["--progress"])
        refspecs = []
        for ref in sorted(set(local_refs) | set(remote_refs)):
            local_oid, remote_oid = local_refs.get(ref), remote_refs.get(ref)
            if local_oid == remote_oid:
                continue
            args.append(f"--force-with-lease={ref}:{remote_oid or ''}")
            if local_oid is None:
                refspecs.append(f":{ref}")
            else:
                prefix = "+" if remote_oid else ""
                refspecs.append(f"{prefix}{local_oid}:{ref}")
        if not refspecs:
            raise GitError("本地和远程没有待同步的引用差异。")
        return [*args, "origin", *refspecs]

    def sync_remote(self) -> None:
        snapshot = self.sync_snapshot()
        if not snapshot.refs:
            raise GitError("当前分支尚无可同步的版本，请先发布版本。")
        self.push_timeout_seconds()
        self._run_remote(self._push_arguments(snapshot), snapshot.remote_url)

    def _recovery_namespace(self, direction: str) -> str:
        return f"refs/code-version-manager/recovery/{int(time.time())}-{uuid.uuid4().hex[:8]}/{direction}"

    def _save_recovery_refs(
        self, namespace: str, refs: Dict[str, str], prefix: str,
    ) -> List[str]:
        saved = []
        for ref, object_id in sorted(refs.items()):
            if ref.startswith("refs/heads/"):
                suffix = "heads/" + ref[len("refs/heads/"):]
            elif ref.startswith("refs/tags/"):
                suffix = "tags/" + ref[len("refs/tags/"):]
            else:
                continue
            recovery_ref = f"{namespace}/{prefix}/{suffix}"
            self.run(["update-ref", recovery_ref, object_id])
            saved.append(recovery_ref)
        return saved

    def _save_metadata_recovery(self, namespace: str) -> Path:
        recovery_name = namespace.split("/")[-2]
        recovery_path = self.metadata_path / "recovery" / recovery_name
        recovery_path.mkdir(parents=True, exist_ok=True)
        manifests = self.metadata_path / MANIFEST_DIRECTORY
        if manifests.is_dir():
            shutil.copytree(manifests, recovery_path / MANIFEST_DIRECTORY, dirs_exist_ok=True)
        changelog = self.metadata_path / MANAGER_CHANGELOG
        if changelog.is_file():
            shutil.copy2(changelog, recovery_path / MANAGER_CHANGELOG)
        return recovery_path

    def _ignored_path_conflicts(self, target_ref: str) -> List[str]:
        ignored_paths = self.run([
            "ls-files", "--others", "--ignored", "--exclude-standard", "-z",
        ]).split("\0")
        target_paths = self.run([
            "ls-tree", "-r", "--name-only", "-z", target_ref,
        ]).split("\0")
        conflicts = set()
        for ignored_path in filter(None, ignored_paths):
            ignored_path = ignored_path.rstrip("/")
            for target_path in filter(None, target_paths):
                if (
                    target_path == ignored_path
                    or target_path.startswith(ignored_path + "/")
                    or ignored_path.startswith(target_path + "/")
                ):
                    conflicts.add(ignored_path)
        return sorted(conflicts)

    def _move_ignored_conflicts(self, paths: Sequence[str], recovery_path: Path) -> List[str]:
        moved = []
        destination_root = recovery_path / "ignored-worktree"
        for relative in paths:
            source = self.path / relative
            if not source.exists() and not source.is_symlink():
                continue
            destination = destination_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            moved.append(str(destination))
        return moved

    def _fetch_remote_refs(
        self, snapshot: RemoteSyncSnapshot, remote_refs: Dict[str, str], namespace: str,
    ) -> Dict[str, str]:
        refspecs = []
        destinations = {}
        if snapshot.branch_ref in remote_refs:
            branch_name = snapshot.branch_ref[len("refs/heads/"):]
            destination = f"{namespace}/heads/{branch_name}"
            destinations[snapshot.branch_ref] = destination
            refspecs.append(f"+{snapshot.branch_ref}:{destination}")
        for ref in sorted(remote_refs):
            if not ref.startswith("refs/tags/"):
                continue
            tag_name = ref[len("refs/tags/"):]
            destination = f"{namespace}/tags/{tag_name}"
            destinations[ref] = destination
            refspecs.append(f"+{ref}:{destination}")
        if refspecs:
            self._run_remote(["fetch", "--no-tags", "--refmap=", "--no-recurse-submodules", snapshot.remote_url, *refspecs], snapshot.remote_url)
        fetched = {}
        for source, destination in destinations.items():
            object_id = self.run(["rev-parse", "--verify", destination], check=False).strip()
            if object_id:
                fetched[source] = object_id
        if fetched != remote_refs:
            raise GitError("远程引用在检查期间发生变化，操作已取消；请刷新后重试。")
        return fetched

    def _delete_fetched_refs(
        self, snapshot: RemoteSyncSnapshot, remote_refs: Dict[str, str], namespace: str,
    ) -> None:
        refs = []
        if snapshot.branch_ref in remote_refs:
            branch_name = snapshot.branch_ref[len("refs/heads/"):]
            refs.append(f"{namespace}/heads/{branch_name}")
        refs.extend(
            f"{namespace}/tags/{ref[len('refs/tags/'):]}"
            for ref in remote_refs if ref.startswith("refs/tags/")
        )
        for ref in refs:
            self.run(["update-ref", "-d", ref], check=False)

    def sync_local_from_remote(
        self, snapshot: RemoteSyncSnapshot, status: RemoteSyncStatus,
    ) -> str:
        if not status.can_sync_local or not status.remote_refs:
            raise GitError("远程没有可用的当前分支，请刷新远程状态后重试。")
        self._validate_sync_confirmation(snapshot, status)
        remote_refs = dict(status.remote_refs)
        if snapshot.branch_ref not in remote_refs:
            raise GitError("远程不存在当前分支，不能同步本地。")
        namespace = self._recovery_namespace("pull")
        fetched = self._fetch_remote_refs(snapshot, remote_refs, namespace + "/incoming")
        self._validate_sync_confirmation(snapshot, status)
        local_refs = dict(snapshot.refs)
        metadata_backup = self._save_metadata_recovery(namespace)
        backup_refs = self._save_recovery_refs(namespace, local_refs, "before")
        branch_name = snapshot.branch_ref[len("refs/heads/"):]
        target_branch = f"{namespace}/incoming/heads/{branch_name}"
        ignored_conflicts = self._ignored_path_conflicts(target_branch)
        ignored_backup = []
        if ignored_conflicts:
            ignored_backup = self._move_ignored_conflicts(ignored_conflicts, metadata_backup)
        dirty = self.run([
            "status", "--porcelain=v1", "-z", "--untracked-files=all",
        ])
        stash_ref = None
        if dirty:
            recovery_name = namespace.split("/")[-2]
            self.run([
                "stash", "push", "--include-untracked", "--message",
                f"Code Version Manager recovery {recovery_name}",
            ])
            stash_commit = self.run(
                ["rev-parse", "--verify", "refs/stash"], check=False
            ).strip()
            if stash_commit:
                stash_ref = f"{namespace}/before/worktree"
                self.run(["update-ref", stash_ref, stash_commit])
        self.last_local_sync_stash_ref = stash_ref
        try:
            self.run(["reset", "--hard", target_branch])
        except GitError as error:
            backup = backup_refs[0] if backup_refs else namespace
            stash_notice = f"\n工作区修改恢复点：{stash_ref}" if stash_ref else ""
            raise GitError(
                "同步本地未完成：Git 无法将当前分支更新到远程状态。\n"
                f"本地引用恢复点：{backup}\n版本元数据备份：{metadata_backup}"
                f"{stash_notice}\n\n{error}"
            ) from error
        incoming_tags = {
            ref: fetched[ref] for ref in fetched if ref.startswith("refs/tags/")
        }
        for ref in sorted(set(local_refs) | set(incoming_tags)):
            if not ref.startswith("refs/tags/"):
                continue
            desired = incoming_tags.get(ref)
            previous = local_refs.get(ref)
            if desired and previous != desired:
                self.run(["update-ref", ref, desired, previous or "0" * len(desired)])
                manifest = self.metadata_path / MANIFEST_DIRECTORY / (
                    ref[len("refs/tags/"):] + MANIFEST_SUFFIX
                )
                manifest.unlink(missing_ok=True)
            elif desired is None and previous:
                self.run(["update-ref", "-d", ref, previous])
                manifest = self.metadata_path / MANIFEST_DIRECTORY / (
                    ref[len("refs/tags/"):] + MANIFEST_SUFFIX
                )
                manifest.unlink(missing_ok=True)
        self._write_changelog(None)
        self._delete_fetched_refs(snapshot, remote_refs, namespace + "/incoming")
        backup = backup_refs[0] if backup_refs else namespace
        recovery = f"操作前的本地引用保存在 {backup}；版本清单和 CHANGELOG 备份保存在 {metadata_backup}。"
        if stash_ref:
            recovery += f"\n未提交修改和未跟踪文件保存在 {stash_ref}。"
        if ignored_backup:
            recovery += "\n被忽略文件已移到 " + str(metadata_backup / "ignored-worktree") + "。"
        return f"本地分支和版本标签已与远程一致。{recovery}"

    def restore_local_sync_changes(self) -> str:
        stash_ref = self.last_local_sync_stash_ref
        if not stash_ref or not self.run(["rev-parse", "--verify", stash_ref], check=False).strip():
            raise GitError("没有可恢复的同步前工作区修改。")
        self.run(["stash", "apply", stash_ref])
        self.last_local_sync_stash_ref = None
        return "同步前的未提交修改和未跟踪文件已恢复到当前工作区；恢复点仍保留。"

    def sync_remote_from_local(
        self, snapshot: RemoteSyncSnapshot, status: RemoteSyncStatus,
    ) -> str:
        if not status.can_sync_remote:
            raise GitError("远程覆盖预检查未通过，请刷新状态并检查权限或仓库规则。")
        self._validate_sync_confirmation(snapshot, status)
        remote_refs = dict(status.remote_refs)
        local_refs = dict(snapshot.refs)
        namespace = self._recovery_namespace("push")
        fetched = self._fetch_remote_refs(snapshot, remote_refs, namespace + "/incoming")
        backup_refs = self._save_recovery_refs(namespace, fetched, "before")
        self._validate_sync_confirmation(snapshot, status)
        self._run_remote(
            self._push_ref_differences(snapshot, remote_refs), snapshot.remote_url
        )
        self._set_upstream(snapshot.branch_ref[len("refs/heads/"):], local_refs[snapshot.branch_ref])
        self._delete_fetched_refs(snapshot, remote_refs, namespace + "/incoming")
        backup = backup_refs[0] if backup_refs else namespace
        return f"远程分支和版本标签已与本地一致。同步前远程引用已保存在本地恢复点 {backup}。"

    def current_branch(self) -> Optional[str]:
        branch = self.run(["symbolic-ref", "--quiet", "--short", "HEAD"], check=False).strip()
        return branch or None

    def _git_path(self, name: str) -> Path:
        return self.path / self.run(["rev-parse", "--git-path", name]).strip()

    def _assert_no_operation(self) -> None:
        if any(self._git_path(name).exists() for name in (
            "MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD", "REVERT_HEAD", "sequencer",
        )):
            raise GitError("Git 有未完成的合并、变基或提交操作，请先完成或取消。")

    def _assert_clean(self, target: Optional[str] = None) -> None:
        self._assert_no_operation()
        if self.run(["status", "--porcelain=v1", "-z", "--untracked-files=all"]):
            raise GitError("工作区有尚未发布的修改（包括暂存内容和未跟踪文件），请先处理。")
        if target:
            conflicts = self._ignored_path_conflicts(target)
            if conflicts:
                raise GitError("目标提交会覆盖被忽略的文件，请先移走或备份：\n" + "\n".join(conflicts))

    def _validate_branch_name(self, name: str) -> None:
        if not name or name == "HEAD" or name.startswith("-"):
            raise GitError("分支名称无效。")
        self.run(["check-ref-format", "refs/heads/" + name])

    def local_branches(self) -> List[BranchInfo]:
        current = self.current_branch()
        branches = [
            BranchInfo(ref[len("refs/heads/"):], "local", oid, ref == f"refs/heads/{current}")
            for ref, oid in self._local_refs().items() if ref.startswith("refs/heads/")
        ]
        if current and not any(branch.current for branch in branches):
            branches.append(BranchInfo(current, "local", "", True))
        return sorted(branches, key=lambda branch: branch.name)

    def remote_branches(self) -> List[BranchInfo]:
        remote_url = self._sync_target()
        return sorted([
            BranchInfo(ref[len("refs/heads/"):], "remote", oid)
            for ref, oid in self._remote_refs(remote_url).items() if ref.startswith("refs/heads/")
        ], key=lambda branch: branch.name)

    def remember_working_branch(self) -> None:
        branch = self.current_branch()
        if branch and self.last_working_branch() != branch:
            self.run(["config", "--local", "codeversionmanager.lastWorkingBranch", branch])

    def last_working_branch(self) -> Optional[str]:
        return self.run(["config", "--local", "--get", "codeversionmanager.lastWorkingBranch"], check=False).strip() or None

    def create_branch(self, name: str) -> str:
        self._validate_branch_name(name)
        if any(branch.name == name for branch in self.local_branches()):
            raise GitError(f"本地分支 {name} 已存在。")
        head = self.run(["rev-parse", "--verify", "HEAD^{commit}"], check=False).strip()
        if not head:
            raise GitError("请先发布首个版本，再创建分支。")
        self._assert_clean(head)
        self.run(["switch", "--no-overwrite-ignore", "--no-track", "-c", name, head])
        self.remember_working_branch()
        return f"已创建并切换到 {name}；开发版本将使用独立的预发布标签。"

    def switch_branch(self, name: str, source: str = "local", expected_oid: Optional[str] = None) -> str:
        self._validate_branch_name(name)
        existing = next((branch for branch in self.local_branches() if branch.name == name), None)
        if existing and existing.current:
            return f"当前已在 {name}。"
        if existing:
            self._assert_clean(existing.oid)
            self.run(["switch", "--no-guess", "--no-overwrite-ignore", "--", name])
        elif source == "remote":
            self._assert_clean()
            remote_url = self._sync_target()
            incoming = f"refs/code-version-manager/branch-import/{uuid.uuid4().hex}"
            try:
                self._run_remote([
                    "fetch", "--no-tags", "--refmap=", "--no-recurse-submodules", remote_url,
                    f"refs/heads/{name}:{incoming}",
                ], remote_url)
                oid = self.run(["rev-parse", "--verify", incoming]).strip()
                if expected_oid and oid != expected_oid:
                    raise GitError("远程分支已变化，请刷新后重试。")
                remote_refs = self._remote_refs(remote_url)
                if remote_refs.get(f"refs/heads/{name}") != oid:
                    raise GitError("远程分支在读取期间发生变化，请刷新后重试。")
                baselines = {
                    ref: tag_oid for ref, tag_oid in remote_refs.items()
                    if ref.startswith("refs/tags/") and (parsed := parse_version_tag(ref)) and parsed[0] is None
                }
                self._ensure_objects(remote_url, baselines)
                baselines = {
                    ref: tag_oid for ref, tag_oid in baselines.items()
                    if self._contains_commit(
                        self.run(["rev-parse", "--verify", f"{tag_oid}^{{commit}}"]).strip(), oid,
                    )
                }
                local_refs = self._local_refs()
                for ref, tag_oid in baselines.items():
                    if ref in local_refs and local_refs[ref] != tag_oid:
                        label = ref[len("refs/tags/"):]
                        raise GitError(f"正式基线 {label} 与远程不同，请先处理标签冲突。")
                if self._sync_target() != remote_url or self._remote_refs(remote_url) != remote_refs:
                    raise GitError("远程分支或正式标签在读取期间发生变化，请刷新后重试。")
                self._assert_clean(oid)
                for ref, tag_oid in baselines.items():
                    if ref not in local_refs:
                        self.run(["update-ref", ref, tag_oid, "0" * len(tag_oid)])
                self.run(["switch", "--no-overwrite-ignore", "--no-track", "-c", name, oid])
                self._set_upstream(name, oid)
            finally:
                self.run(["update-ref", "-d", incoming], check=False)
        else:
            raise GitError(f"本地分支 {name} 不存在。")
        self.remember_working_branch()
        return f"已切换到 {name}。"

    def _set_upstream(self, branch: str, oid: str) -> None:
        self.run(["update-ref", f"refs/remotes/origin/{branch}", oid])
        self.run(["config", "--local", f"branch.{branch}.remote", "origin"])
        self.run(["config", "--local", f"branch.{branch}.merge", f"refs/heads/{branch}"])

    def merge_state(self) -> MergeState:
        active = self._git_path("MERGE_HEAD").exists()
        source = ""
        manual_resolution: List[str] = []
        notice = ""
        try:
            merge = json.loads((self.metadata_path / "merge.json").read_text())
            source = merge.get("source", "")
            manual_resolution = merge.get("manual_resolution", [])
            notice = merge.get("notice", "")
        except (OSError, ValueError, AttributeError):
            pass
        conflicts = set()
        if active:
            conflicts.update(filter(None, self.run([
                "diff", "--name-only", "--diff-filter=U", "-z",
            ]).split("\0")))
            conflicts.update(path for path in manual_resolution if isinstance(path, str))
        return MergeState(active, source if active else "", tuple(sorted(conflicts)), notice if active else "")

    def _file_entry_at(self, reference: str, path: str) -> Optional[Tuple[str, str]]:
        entries = self.run([
            "--literal-pathspecs", "ls-tree", "-z", reference, "--", path,
        ]).split("\0")
        for entry in filter(None, entries):
            metadata, name = entry.split("\t", 1)
            if name == path:
                mode, _kind, oid = metadata.split()
                return mode, oid
        return None

    def _version_file_at(self, reference: str) -> Optional[Tuple[str, bytes]]:
        entry = self._file_entry_at(reference, WORKSPACE_VERSION)
        if entry is None:
            return None
        mode, oid = entry
        data = self.run_bytes(["cat-file", "blob", oid]) if mode in ("100644", "100755") else b""
        return mode, data

    @staticmethod
    def _version_merge_policy(
        main_file: Optional[Tuple[str, bytes]], source_file: Optional[Tuple[str, bytes]],
    ) -> Tuple[str, str]:
        if any(entry and entry[0] not in ("100644", "100755") for entry in (main_file, source_file)):
            return "manual", "VERSION 涉及符号链接或特殊文件，请手动检查并标记解决。"
        if main_file == source_file:
            return "same", ""
        main_data = main_file[1] if main_file else None
        source_data = source_file[1] if source_file else None
        main_text = main_data.decode("utf-8", errors="replace").strip() if main_data is not None else None
        source_text = source_data.decode("utf-8", errors="replace").strip() if source_data is not None else None
        main_formal = bool(main_text and FORMAL_VERSION_FILE_PATTERN.fullmatch(main_text))
        source_prerelease = bool(
            source_text and PRERELEASE_VERSION_FILE_PATTERN.fullmatch(source_text)
        )
        source_formal = bool(source_text and FORMAL_VERSION_FILE_PATTERN.fullmatch(source_text))
        if main_formal and (source_data is None or source_prerelease or source_formal):
            return (
                "keep_main",
                f"VERSION 已保留 main 正式版本 {main_text}；开发分支预发布号不会进入正式分支。",
            )
        if main_data is None and source_prerelease:
            return (
                "remove_prerelease",
                f"VERSION 中的开发预发布号 {source_text} 已从 main 移除；请在 main 发布正式版本时更新。",
            )
        return (
            "manual",
            "VERSION 两侧内容无法按正式版与预发布版规则安全判断，请手动检查并标记解决。",
        )

    def _apply_version_merge_policy(self, policy: str, notice: str) -> None:
        merge_path = self.metadata_path / "merge.json"
        try:
            merge = json.loads(merge_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            merge = {}
        if policy == "keep_main":
            target = self.path / WORKSPACE_VERSION
            if target.is_symlink() or (target.exists() and not target.is_file()):
                policy = "manual"
                notice = "VERSION 不是普通文件，未自动覆盖；请手动检查并标记解决。"
            else:
                self.run(["--literal-pathspecs", "checkout", "HEAD", "--", WORKSPACE_VERSION])
        elif policy == "remove_prerelease":
            self.run([
                "--literal-pathspecs", "rm", "-f", "--ignore-unmatch", "--", WORKSPACE_VERSION,
            ])
        if policy == "manual":
            pending = set(merge.get("manual_resolution", []))
            pending.add(WORKSPACE_VERSION)
            merge["manual_resolution"] = sorted(pending)
        if notice:
            merge["notice"] = notice
        merge_path.write_text(json.dumps(merge, ensure_ascii=False), encoding="utf-8")

    def _assert_branch_not_in_other_worktree(self, name: str) -> None:
        worktree = ""
        for line in self.run(["worktree", "list", "--porcelain"]).splitlines():
            if line.startswith("worktree "):
                worktree = line[len("worktree "):]
            elif line == f"branch refs/heads/{name}" and Path(worktree).resolve() != self.path:
                raise GitError(f"分支 {name} 正在另一个工作区使用，请先在那里处理。")

    def _prepare_merge_branches(self, source: str) -> None:
        local = self._local_refs()
        names = ("main", source)
        if any(f"refs/heads/{name}" not in local for name in names):
            raise GitError("main 和来源分支都必须先存在于本地。")
        for name in names:
            self._assert_branch_not_in_other_worktree(name)
        if not self.remote_url():
            return
        remote_url = self._sync_target()
        remote = self._remote_refs(remote_url)
        wanted = {f"refs/heads/{name}": remote[f"refs/heads/{name}"] for name in names if f"refs/heads/{name}" in remote}
        releases = {ref: oid for ref, oid in remote.items() if self._owned_ref(ref, "main")}
        source_tags = {ref: oid for ref, oid in remote.items() if self._owned_ref(ref, source)}
        self._ensure_objects(remote_url, {**wanted, **releases, **source_tags})
        for ref, oid in {**releases, **source_tags}.items():
            if ref in local and local[ref] != oid:
                raise GitError(f"版本标签 {display_version(ref[len('refs/tags/'):])} 两端不同，请先处理标签冲突。")
        if f"refs/heads/{source}" in wanted:
            for ref, oid in source_tags.items():
                commit = self.run(["rev-parse", "--verify", f"{oid}^{{commit}}"]).strip()
                if not self._contains_commit(commit, wanted[f"refs/heads/{source}"]):
                    raise GitError(f"origin 上的预发布标签 {display_version(ref[len('refs/tags/'):])} 不属于 {source} 历史。")
        updates = {}
        for ref, remote_oid in wanted.items():
            local_oid = local[ref]
            if local_oid != remote_oid:
                if self._contains_commit(local_oid, remote_oid):
                    updates[ref] = remote_oid
                elif not self._contains_commit(remote_oid, local_oid):
                    raise GitError(f"{ref[len('refs/heads/'):]} 与 origin 已分叉；请先处理，软件不会覆盖任何一端。")
        # Validate both branches before applying either fast-forward.
        if self._sync_target() != remote_url or self._local_refs() != local or self._remote_refs(remote_url) != remote:
            raise GitError("分支在检查期间发生变化，请刷新后重试。")
        for ref, oid in updates.items():
            self._assert_clean(oid)
            name = ref[len("refs/heads/"):]
            if self.current_branch() == name:
                self.run(["merge", "--ff-only", "--no-overwrite-ignore", oid])
            else:
                self.run(["update-ref", ref, oid, local[ref]])
        for ref, oid in wanted.items():
            self._set_upstream(ref[len("refs/heads/"):], oid)
        for ref, oid in {**releases, **source_tags}.items():
            if ref not in local:
                self.run(["update-ref", ref, oid, "0" * len(oid)])

    def merge_to_main(self, source: str) -> str:
        self._validate_branch_name(source)
        self._ensure_full_history()
        if source == "main":
            raise GitError("请选择需要合并的开发分支。")
        self._assert_clean()
        self._prepare_merge_branches(source)
        self.switch_branch("main")
        source_oid = self.run(["rev-parse", "--verify", f"refs/heads/{source}"]).strip()
        self._assert_clean(source_oid)
        head = self.run(["rev-parse", "HEAD"]).strip()
        if self._contains_commit(source_oid, head):
            return f"{source} 已合入 main，无需重复合并。"
        version_policy, version_notice = self._version_merge_policy(
            self._version_file_at(head), self._version_file_at(source_oid),
        )
        ensure_git_identity(self)
        self.metadata_path.mkdir(parents=True, exist_ok=True)
        self._save_recovery_refs(self._recovery_namespace("merge"), {
            "refs/heads/main": head, f"refs/heads/{source}": source_oid,
        }, "before")
        (self.metadata_path / "merge.json").write_text(
            json.dumps({"source": source}, ensure_ascii=False), encoding="utf-8",
        )
        result = subprocess.run([
            "git", "-C", str(self.path), "merge", "--no-ff", "--no-commit", "--no-overwrite-ignore", source_oid,
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if not self._git_path("MERGE_HEAD").exists():
            (self.metadata_path / "merge.json").unlink(missing_ok=True)
            raise GitError(result.stderr.strip() or result.stdout.strip() or "Git 未保留待完成的合并状态。")
        if result.returncode != 0 and not self.merge_state().conflicts:
            raise GitError(result.stderr.strip() or result.stdout.strip() or "Git 合并未成功，请取消后重试。")
        self._apply_version_merge_policy(version_policy, version_notice)
        state = self.merge_state()
        if state.active and state.conflicts:
            details = f"正在将 {source} 合并到 main：有 {len(state.conflicts)} 个冲突或待人工确认文件，请逐个处理后完成合并，或取消合并。"
            return details + ("\n" + state.notice if state.notice else "")
        return self.complete_merge()

    def complete_merge(self) -> str:
        state = self.merge_state()
        if not state.active or self.current_branch() != "main":
            raise GitError("没有正在 main 上进行的合并。")
        if state.conflicts:
            raise GitError("仍有未解决的合并冲突，请逐个解决并标记已解决。")
        ensure_git_identity(self)
        self.run(["commit", "-m", f"Merge {state.source or 'development branch'} into main"])
        (self.metadata_path / "merge.json").unlink(missing_ok=True)
        self.remember_working_branch()
        message = "合并已完成，main 保留双方提交历史；请检查结果，再发布新的正式版本并同步远程。"
        return message + ("\n" + state.notice if state.notice else "")

    def abort_merge(self) -> str:
        if not self.merge_state().active:
            raise GitError("没有正在进行的合并。")
        self.run(["merge", "--abort"])
        (self.metadata_path / "merge.json").unlink(missing_ok=True)
        return "合并已取消，已恢复到开始合并时的 main。合并前安全快进取得的远程提交仍保留。"

    def resolve_merge_file(self, path: str, delete: bool = False) -> str:
        state = self.merge_state()
        if path not in state.conflicts:
            raise GitError("所选文件已不在冲突列表，请刷新。")
        if not delete:
            target = self.path / path
            if not target.is_file() or target.is_symlink():
                raise GitError("请先保存合并结果，或明确选择删除此文件。")
            if re.search(
                rb"^(?:<{7}|={7}|>{7})(?:\s|$)", target.read_bytes(), re.MULTILINE,
            ):
                raise GitError("文件仍有冲突标记，请处理后再标记已解决。")
            self.run(["--literal-pathspecs", "add", "--", path])
        else:
            self.run(["--literal-pathspecs", "rm", "-f", "--ignore-unmatch", "--", path])
        merge_path = self.metadata_path / "merge.json"
        try:
            merge = json.loads(merge_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            merge = {}
        merge["manual_resolution"] = [
            item for item in merge.get("manual_resolution", []) if item != path
        ]
        merge_path.write_text(json.dumps(merge, ensure_ascii=False), encoding="utf-8")
        return f"已标记解决：{path}"

    def launch_merge_tool(self, path: str) -> str:
        if path not in self.merge_state().conflicts:
            raise GitError("所选文件已不在冲突列表。")
        target = self.path / path
        if self.path not in target.resolve().parents or target.is_symlink():
            raise GitError("符号链接或工作区外路径请手动解决。")
        tool = self.compare_tool()
        label, command = COMPARE_TOOLS[tool]
        if not shutil.which(command):
            raise GitError(f"没有找到 {label}，请安装后重试，或在设置中更换工具。")
        entries = self.run(["--literal-pathspecs", "ls-files", "--unmerged", "-z", "--", path]).split("\0")
        stages = {int(line.split("\t", 1)[0].split()[2]): line.split("\t", 1)[0].split() for line in entries if line}
        if not stages and path == WORKSPACE_VERSION:
            # Git may have merged unknown VERSION contents cleanly; retain a manual review step.
            base = self.run(["merge-base", "HEAD", "MERGE_HEAD"]).strip()
            for stage, reference in ((1, base), (2, "HEAD"), (3, "MERGE_HEAD")):
                entry = self._file_entry_at(reference, path)
                if entry:
                    mode, oid = entry
                    stages[stage] = (mode, oid)
        if not stages or any(value[0] not in ("100644", "100755") for value in stages.values()):
            raise GitError("此冲突涉及特殊文件，请手动编辑后标记解决，或明确选择删除。")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="cvm-merge-") as temporary:
            files = []
            for stage, name in ((1, "base"), (2, "main"), (3, "incoming")):
                file = Path(temporary) / name
                if stage in stages:
                    file.write_bytes(self.run_bytes(["cat-file", "blob", stages[stage][1]]))
                else:
                    file.write_bytes(b"")
                files.append(str(file))
            base, local, incoming = files
            if tool == "bcompare":
                args = [command, "-solo", local, incoming, base, f"-mergeoutput={target}"]
            elif tool == "meld":
                args = [command, f"--output={target}", local, base, incoming]
            else:
                args = [command, base, local, incoming, "-o", str(target)]
            result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if result.returncode != 0:
                raise GitError(result.stderr.strip() or "合并工具已取消或执行失败，冲突状态保留。")
        return "合并工具已关闭，请检查保存结果并点击“标记已解决”；软件不会自动将工具退出视为解决。"

    def plan_branch_deletion(self, name: str, cleanup_tags: bool = False, delete_remote: bool = False) -> BranchDeletionPlan:
        self._validate_branch_name(name)
        self._ensure_full_history()
        self._assert_clean()
        refs = self._local_refs()
        if name == "main" or self.current_branch() != "main":
            raise GitError("请在 main 上删除已合并的开发分支。")
        if f"refs/heads/{name}" not in refs or not self._contains_commit(refs[f"refs/heads/{name}"], refs["refs/heads/main"]):
            raise GitError("来源分支尚未完全合入 main，不能删除。")
        remote_url = self._sync_target() if delete_remote else ""
        remote = self._remote_refs(remote_url) if delete_remote else {}
        if delete_remote:
            remote_main = remote.get("refs/heads/main")
            remote_source = remote.get(f"refs/heads/{name}")
            if not remote_main:
                raise GitError("origin/main 不存在，无法确认远程删除安全性。")
            targets = {f"refs/heads/{name}": remote_source} if remote_source else {}
            if cleanup_tags:
                targets.update({ref: oid for ref, oid in remote.items() if self._owned_ref(ref, name)})
            self._ensure_objects(remote_url, {"refs/heads/main": remote_main, **targets})
            if any(not self._contains_commit(
                self.run(["rev-parse", "--verify", f"{oid}^{{commit}}"]).strip(), remote_main,
            ) for oid in targets.values()):
                raise GitError("origin/main 尚未包含待删除的远程分支或标签，请先同步合并后的 main。")
        return BranchDeletionPlan(name, cleanup_tags, delete_remote, tuple(sorted(refs.items())), remote_url, tuple(sorted(remote.items())))

    def delete_merged_branch(
        self, name: str, cleanup_tags: bool = False, delete_remote: bool = False,
        expected: Optional[BranchDeletionPlan] = None,
    ) -> str:
        self._validate_branch_name(name)
        self._ensure_full_history()
        self._assert_clean()
        if name == "main" or self.current_branch() != "main":
            raise GitError("请在 main 上删除已合并的开发分支，不能删除 main。")
        self._assert_branch_not_in_other_worktree(name)
        refs = self._local_refs()
        if expected and (expected.name != name or expected.cleanup_tags != cleanup_tags
                         or expected.delete_remote != delete_remote or expected.local_refs != tuple(sorted(refs.items()))):
            raise GitError("删除确认后本地引用发生变化，请重新查看删除清单。")
        branch_ref = f"refs/heads/{name}"
        if branch_ref not in refs or not self._contains_commit(refs[branch_ref], refs["refs/heads/main"]):
            raise GitError("来源分支尚未完全合入 main，不能删除。")
        deletions = {branch_ref: refs[branch_ref]}
        if cleanup_tags:
            deletions.update({ref: oid for ref, oid in refs.items() if self._owned_ref(ref, name)})
        namespace = self._recovery_namespace("delete-branch")
        self._save_recovery_refs(namespace, deletions, "before")
        self._save_metadata_recovery(namespace)
        if delete_remote:
            remote_url = self._sync_target()
            remote = self._remote_refs(remote_url)
            if expected and (remote_url != expected.remote_url or tuple(sorted(remote.items())) != expected.remote_refs):
                raise GitError("删除确认后远程引用发生变化，请重新查看删除清单。")
            target = {ref: oid for ref, oid in remote.items() if ref == branch_ref or (cleanup_tags and self._owned_ref(ref, name))}
            self._ensure_objects(remote_url, target)
            main = remote.get("refs/heads/main")
            if main:
                self._ensure_objects(remote_url, {"refs/heads/main": main})
            if any(not main or not self._contains_commit(
                self.run(["rev-parse", f"{oid}^{{commit}}"]).strip(), main,
            ) for oid in target.values()):
                raise GitError("origin/main 尚未包含待删除的远程分支或标签，请先上传合并后的 main。")
            if target:
                # Reuse the recovery fetcher by selecting the source branch explicitly.
                selected = RemoteSyncSnapshot(remote_url, branch_ref, ())
                fetched = self._fetch_remote_refs(selected, target, namespace + "/incoming")
                self._save_recovery_refs(namespace, fetched, "remote-before")
                if self._local_refs() != refs or self._remote_refs(remote_url) != remote:
                    raise GitError("删除检查后引用发生变化，请刷新并重新确认。")
                args = ["push", "--atomic", "--porcelain", "--no-follow-tags"]
                args.extend(f"--force-with-lease={ref}:{oid}" for ref, oid in target.items())
                if main:
                    args.append(f"--force-with-lease=refs/heads/main:{main}")
                refspecs = [":" + ref for ref in target]
                if main:
                    refspecs.append(f"{main}:refs/heads/main")
                self._run_remote([*args, "origin", *refspecs], remote_url)
        if self._local_refs() != refs:
            raise GitError("本地引用已变化，已停止本地删除；请刷新检查，恢复点仍保留。")
        self._assert_branch_not_in_other_worktree(name)
        for ref, oid in deletions.items():
            self.run(["update-ref", "-d", ref, oid])
        self.run(["config", "--local", "--remove-section", f"branch.{name}"], check=False)
        if delete_remote:
            self.run(["update-ref", "-d", f"refs/remotes/origin/{name}"], check=False)
        for ref in deletions:
            if ref.startswith("refs/tags/"):
                self._remove_version_metadata(ref[len("refs/tags/"):])
        self._write_changelog(None)
        return f"已删除已合并分支 {name}；{'已清理该分支的预发布标签' if cleanup_tags else '预发布标签保留归档'}。恢复引用保存在 {namespace}。"

    def head_label(self) -> str:
        exact = self.run(["describe", "--tags", "--exact-match", "HEAD"], check=False).strip()
        return exact or self.run(["rev-parse", "--short", "HEAD"]).strip()

    def status(self) -> List[Change]:
        raw = self.run(["status", "--porcelain=v1", "-z", "--untracked-files=normal"])
        fields = raw.split("\0")
        changes: List[Change] = []
        index = 0
        while index < len(fields):
            field = fields[index]
            index += 1
            if not field:
                continue
            status = field[:2]
            path = field[3:]
            if "R" in status or "C" in status:
                if index < len(fields) and fields[index]:
                    path = f"{fields[index]} -> {path}"
                    index += 1
            changes.append(Change(status=status.strip() or "M", path=path))
        return changes

    def changed_files(self) -> List[str]:
        return [
            path
            for path in self.changed_paths()
            if (self.path / path).is_file() or (self.path / path).is_symlink()
        ]

    def deleted_files(self) -> List[str]:
        raw = self.run(["ls-files", "--deleted", "-z"])
        raw += self.run([
            "diff", "--cached", "--name-only", "--diff-filter=D", "--no-renames", "-z",
        ])
        return sorted({
            path for path in raw.split("\0")
            if path and not os.path.lexists(self.path / path)
        })

    def changed_paths(self) -> List[str]:
        raw = self.run(["status", "--porcelain=v1", "-z", "--untracked-files=all"])
        fields = raw.split("\0")
        paths: List[str] = []
        index = 0
        while index < len(fields):
            field = fields[index]
            index += 1
            if not field:
                continue
            status = field[:2]
            path = field[3:]
            if "R" in status or "C" in status:
                if index < len(fields) and fields[index]:
                    index += 1
            paths.append(path)
        return sorted(set(paths))

    def storage_size(self) -> int:
        git_directory = self.run(["rev-parse", "--absolute-git-dir"]).strip()
        return allocated_disk_usage(Path(git_directory)) if git_directory else 0

    def candidate_files(self) -> List[str]:
        raw = self.run(["ls-files", "-co", "--exclude-standard", "-z"])
        paths = []
        for path in raw.split("\0"):
            if not path:
                continue
            absolute = self.path / path
            if absolute.is_file() or absolute.is_symlink():
                paths.append(path)
        return self.filter_ignored(sorted(set(paths)))

    def tags(self) -> List[str]:
        output = self.run(["tag", "--sort=version:refname"])
        return [line for line in output.splitlines() if line]

    def version_tags(self, all_versions: bool = False) -> List[VersionTagInfo]:
        branch = self.current_branch()
        head = self.run(["rev-parse", "--verify", "HEAD^{commit}"], check=False).strip()
        if not all_versions and branch and branch != "main" and self.run(["rev-parse", "--is-shallow-repository"]).strip() == "true":
            raise GitError("当前为浅克隆，无法判断正式标签是否属于当前分支；请先获取完整历史，或改看“全部分支版本”。")
        tags = []
        for ref, oid in self._local_refs().items():
            if not ref.startswith("refs/tags/"):
                continue
            owner, version = parse_version_tag(ref)
            commit = self.run(["rev-parse", "--verify", f"{oid}^{{commit}}"]).strip()
            baseline = owner is None and branch != "main"
            visible = (branch == "main" and owner is None) or (branch is not None and owner == branch)
            if not visible and baseline and head:
                visible = self._contains_commit(commit, head)
            if branch is None:
                visible = bool(head) and self._contains_commit(commit, head)
            if all_versions or visible:
                tags.append(VersionTagInfo(
                    ref, owner, "release" if owner is None else "prerelease", version, commit, oid,
                    owner is None and branch != "main" or owner is not None and owner != branch,
                ))
        return sorted(tags, key=lambda tag: version_sort_key(tag.name), reverse=True)

    def _assert_version_editable(self, version: str) -> str:
        self._assert_no_operation()
        version = canonical_version(version)
        parsed = parse_version_tag(version)
        if not parsed or version not in self.tags():
            raise GitError(f"版本 {version} 不存在。")
        branch = self.current_branch()
        owner = parsed[0]
        if not branch or (owner is None and branch != "main") or (owner is not None and owner != branch):
            raise GitError("此版本属于其他分支或是继承的正式版本（只读），请切换到所属分支操作。")
        if owner is None:
            local = self._local_refs()
            remote = {}
            remote_url = self._sync_target() if self.remote_url() else ""
            self._ensure_full_history()
            if remote_url:
                remote = self._remote_refs(remote_url)
                self._ensure_objects(remote_url, remote)
            shared = self._shared_release_branches(local[f"refs/tags/{version}"], local, remote)
            if shared:
                raise GitError("其他活动分支也包含此正式标签，不能改写或删除：" + "、".join(shared))
        return version

    def version_message(self, version: str) -> str:
        version = canonical_version(version)
        if not parse_version_tag(version) or version not in self.tags():
            raise GitError(f"版本 {version} 不存在。")
        return self._version_message_unchecked(version)

    def _version_message_unchecked(self, version: str) -> str:
        message = self.run(
            ["for-each-ref", "--format=%(contents)", f"refs/tags/{version}"]
        ).strip()
        return "\n".join(normalize_changes(message))

    def _tags_by_commit(self) -> Dict[str, List[str]]:
        mapping: Dict[str, List[str]] = {}
        for tag in self.tags():
            commit = self.run(["rev-list", "-n", "1", tag]).strip()
            mapping.setdefault(commit, []).append(tag)
        return mapping

    def history(self, limit: int = 500, all_versions: bool = False) -> List[HistoryEntry]:
        entries: List[HistoryEntry] = []
        for tag in self.version_tags(all_versions=all_versions)[:limit]:
            commit, short_commit, day, subject = self.run([
                "show", "--no-patch", "--date=short", "--format=%H%x1f%h%x1f%ad%x1f%s", tag.commit,
            ]).strip().split("\x1f", 3)
            changes = normalize_changes(self._version_message_unchecked(tag.name))
            if changes:
                subject = changes[0]
            if tag.read_only:
                subject = ("[继承的正式版本 · 只读] " if tag.owner is None else "[其他分支 · 只读] ") + subject
            entries.append(HistoryEntry(commit, short_commit, day, subject, (tag.name,), tag.read_only))
        return entries

    def next_version(self) -> str:
        tags = self.tags()
        release_tags = [tag for tag in tags if VERSION_PATTERN.fullmatch(tag)]
        base = max(release_tags, key=version_key) if release_tags else None
        branch = self.current_branch()
        if not branch or branch == "main":
            latest = version_key(base) if base else (0, 0, 0)
            return "v" + ".".join(str(value) for value in (*latest[:-1], latest[-1] + 1))
        if not base:
            raise GitError("尚无正式版本，请先在 main 发布首个正式版本，再创建开发预发布版本。")
        numbers = [
            prerelease_sequence(parsed[1])
            for tag in tags for parsed in [parse_version_tag(tag)]
            if parsed and parsed[0] == branch and parsed[1].startswith(base + "-beta.")
        ]
        return version_tag_name(branch, f"{base}-beta.0.0.{max(numbers, default=0) + 1}")

    def manifest(self, reference: str) -> Optional[List[str]]:
        tag = self._tag_for_reference(reference)
        if not tag:
            return None
        manifest_path = (
            self.metadata_path
            / MANIFEST_DIRECTORY
            / f"{tag}{MANIFEST_SUFFIX}"
        )
        try:
            content = manifest_path.read_text(encoding="utf-8")
        except OSError:
            return None
        return [line for line in content.splitlines() if line]

    def _tag_for_reference(self, reference: str) -> Optional[str]:
        reference = canonical_version(reference)
        if reference in self.tags():
            return reference
        tags = self.run(["tag", "--points-at", reference], check=False).splitlines()
        return sorted(tags, key=version_sort_key)[-1] if tags else None

    def default_package_files(self) -> List[str]:
        previous = self.manifest("HEAD")
        candidates = set(self.candidate_files())
        if previous is None:
            return sorted(candidates)
        return sorted((set(previous) | set(self.changed_files())) & candidates)

    def _write_manifest(self, version: str, selected_files: Sequence[str]) -> Path:
        manifest_path = (
            self.metadata_path
            / MANIFEST_DIRECTORY
            / f"{version}{MANIFEST_SUFFIX}"
        )
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text("".join(f"{path}\n" for path in sorted(selected_files)), encoding="utf-8")
        return manifest_path

    def changelog_text(self, pending=None) -> str:
        sections = []
        if pending:
            version, message, _file_count = pending
            sections.append((version, date.today().isoformat(), normalize_changes(message)))
        for tag in sorted((tag for tag in self.tags() if parse_version_tag(tag)), key=version_sort_key, reverse=True):
            commit = self.run(["rev-list", "-n", "1", tag]).strip()
            version_date = self.run(
                ["show", "-s", "--date=short", "--format=%ad", commit]
            ).strip()
            message = self._version_message_unchecked(tag)
            sections.append((tag, version_date, normalize_changes(message)))

        workspace_name = self.path.name
        title = f"Changelog for {workspace_name}"
        marker = "^" * max(39, len(title))
        lines = [marker, title, marker, ""]
        for version, version_date, changes in sections:
            label = version[1:] if version.startswith("v") else display_version(version)
            heading = f"{label} ({version_date})"
            lines.extend(
                [
                    heading,
                    "-" * len(heading),
                    *[f"* {change}" for change in changes],
                    "",
                ]
            )
        if not sections:
            lines.extend(["尚无版本", "--------", ""])
        return "\n".join(lines)

    def _write_changelog(self, pending) -> Path:
        changelog_path = self.metadata_path / MANAGER_CHANGELOG
        changelog_path.parent.mkdir(parents=True, exist_ok=True)
        changelog_path.write_text(self.changelog_text(pending), encoding="utf-8")
        return changelog_path

    def download_changelog(self, destination: Path) -> None:
        destination.write_text(self.changelog_text(), encoding="utf-8")

    def _add_paths(
        self, paths: Sequence[str], *, env: Optional[Dict[str, str]] = None,
    ) -> None:
        for start in range(0, len(paths), 200):
            self.run(
                ["--literal-pathspecs", "add", "--", *paths[start : start + 200]],
                env=env,
            )

    def filter_ignored(self, paths: Sequence[str]) -> List[str]:
        if not paths:
            return []
        payload = "\0".join(paths) + "\0"
        result = subprocess.run(
            ["git", "-C", str(self.path), "check-ignore", "--no-index", "--stdin", "-z"],
            input=payload.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode not in (0, 1):
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise GitError(message or "无法读取工作空间的 Git 忽略规则。")
        ignored = {
            value.decode("utf-8", errors="surrogateescape")
            for value in result.stdout.split(b"\0")
            if value
        }
        return [path for path in paths if path not in ignored and path != WORKSPACE_CHANGELOG]

    def ignored_tracked_files(self) -> List[str]:
        # Include HEAD as well as the index so staged removals are not missed.
        raw = self.run(["ls-files", "--cached", "-z"])
        if self.run(["rev-parse", "--verify", "HEAD"], check=False).strip():
            raw += self.run(["ls-tree", "-r", "--name-only", "-z", "HEAD"])
        tracked = sorted({path for path in raw.split("\0") if path})
        return sorted(set(tracked) - set(self.filter_ignored(tracked)))

    def save_version(
        self,
        version: str,
        message: str,
        selected_files: Sequence[str],
        selected_scopes: Optional[Sequence[str]] = None,
        *,
        sync_version_file: Optional[bool] = None,
    ) -> None:
        self._assert_no_operation()
        if self.current_branch() is None:
            raise GitError("当前正在查看历史版本。请先返回工作分支，再发布新版本。")
        branch = self.current_branch()
        version = canonical_version(version, branch)
        parsed = parse_version_tag(version)
        if not parsed or (branch == "main" and parsed[0] is not None) or (branch != "main" and parsed[0] != branch):
            raise GitError("main 只能发布正式版本；开发分支只能发布自己的预发布版本，如 test1/v0.3.0-beta.0.0.1。")
        if branch != "main" and not is_new_prerelease(parsed[1]):
            raise GitError("新预发布版本必须使用 beta.0.0.序号 格式，例如 v0.3.0-beta.0.0.1；旧 beta 标签仍可查看。")
        if branch != "main" and not any(VERSION_PATTERN.fullmatch(tag) for tag in self.tags()):
            raise GitError("尚无正式版本，请先在 main 发布首个正式版本，再创建开发预发布版本。")
        if version in self.tags():
            raise GitError(f"版本 {version} 已存在。")
        ensure_git_identity(self)
        changes = normalize_changes(message)
        if not changes:
            raise GitError("至少填写一条改动内容。")
        commit_message = changes[0]
        if len(changes) > 1:
            commit_message += "\n\n" + "\n".join(changes[1:])
        if not self.run(["rev-parse", "--verify", "HEAD"], check=False).strip():
            added = prepare_workspace_release_files(self.path)
            selected_files = sorted(set(selected_files) | set(added))
        selected = validate_selected_files(self.path, selected_files)
        selected = self.filter_ignored(selected)
        sync_version = (
            self.sync_version_file_enabled() if sync_version_file is None else sync_version_file
        )
        version_path = self.path / WORKSPACE_VERSION
        if sync_version:
            if version_path.is_symlink() or (version_path.exists() and not version_path.is_file()):
                raise GitError("同步 VERSION 需要根目录下的普通文件，请先检查 VERSION 的路径类型。")
            if not self.filter_ignored([WORKSPACE_VERSION]):
                raise GitError("VERSION 被 .gitignore 排除，请先移除对应忽略规则，或关闭 VERSION 同步。")
            selected = sorted(set(selected) | {WORKSPACE_VERSION})
        scopes = validate_top_level_scopes(selected_scopes or [])
        deleted = self.filter_ignored([
            path for path in self.deleted_files()
            if any(path == scope or path.startswith(scope + "/") for scope in scopes)
        ])
        ignored_removals = self.ignored_tracked_files()
        if not selected and not deleted and not ignored_removals:
            raise GitError("至少选择一个未被 .gitignore 排除的文件或已删除项。")
        if self.run(["ls-files", "--unmerged", "-z"]):
            raise GitError("Git 工作区存在尚未解决的合并冲突。请先解决冲突再发布版本。")
        if any(
            self.run(["rev-parse", "--verify", "--quiet", state], check=False).strip()
            for state in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD")
        ):
            raise GitError("Git 工作区有尚未完成的合并、拣选或还原操作。请先完成该操作再发布版本。")
        self.metadata_path.mkdir(parents=True, exist_ok=True)
        added_paths = sorted(set(selected) | set(deleted))
        paths = sorted(set(added_paths) | set(ignored_removals))
        with tempfile.TemporaryDirectory(prefix="publish-", dir=self.metadata_path) as temporary:
            # Start from HEAD so unrelated staged content stays out of this version.
            # A failed add or commit leaves the user's index unchanged.
            environment = os.environ.copy()
            environment["GIT_INDEX_FILE"] = str(Path(temporary) / "index")
            has_head = self.run(["rev-parse", "--verify", "HEAD"], check=False).strip()
            self.run(
                ["read-tree", "HEAD"] if has_head else ["read-tree", "--empty"],
                env=environment,
            )
            previous_version = version_path.read_bytes() if sync_version and version_path.exists() else None
            try:
                if sync_version:
                    version_path.write_text(parsed[1][1:] + "\n", encoding="utf-8")
                self._add_paths(added_paths, env=environment)
                if ignored_removals:
                    # Remove ignored historical files from the version, never from disk.
                    self.run(
                        ["update-index", "--force-remove", "-z", "--stdin"],
                        input="\0".join(ignored_removals) + "\0",
                        env=environment,
                    )
                self.run(["commit", "--allow-empty", "-m", commit_message], env=environment)
            except Exception:
                if sync_version:
                    if previous_version is None:
                        version_path.unlink(missing_ok=True)
                    else:
                        version_path.write_bytes(previous_version)
                raise
            # Match the real index to the new commit only for published paths.
            # NUL-delimited stdin handles large selections and literal filenames.
            self.run(
                ["--literal-pathspecs", "reset", "--quiet", "HEAD",
                 "--pathspec-from-file=-", "--pathspec-file-nul"],
                input="\0".join(paths) + "\0",
            )
        self.run(["tag", "-a", version, "-m", commit_message])
        self._write_manifest(version, selected)
        self._write_changelog(None)
        self.configure_version_file(sync_version)

    def update_version_message(self, version: str, message: str) -> None:
        version = self._assert_version_editable(version)
        changes = normalize_changes(message)
        if not changes:
            raise GitError("至少填写一条改动内容。")
        commit_message = changes[0]
        if len(changes) > 1:
            commit_message += "\n\n" + "\n".join(changes[1:])
        commit = self.run(["rev-list", "-n", "1", version]).strip()
        ensure_git_identity(self)
        self.run(["tag", "-f", "-a", version, "-m", commit_message, commit])
        self._write_changelog(None)

    def switch_main(self) -> None:
        self.switch_branch("main")

    def delete_version(self, version: str) -> None:
        version = self._assert_version_editable(version)
        self.run(["tag", "--delete", version])
        self._remove_version_metadata(version)

    def delete_latest_version_commit(self, version: str) -> None:
        version = self._assert_version_editable(version)
        if not parse_version_tag(version) or version not in self.tags():
            raise GitError(f"版本 {version} 不存在或已经删除。")
        if self.current_branch() != "main":
            raise GitError("只能在 main 分支上删除最新版本的 Git 提交。")
        commit = self.run(["rev-list", "-n", "1", version]).strip()
        head = self.run(["rev-parse", "HEAD"]).strip()
        if commit != head:
            raise GitError("所选版本不是 main 的最新提交。为避免改写后续历史，不能删除该提交。")
        tags_at_head = self.run(["tag", "--points-at", commit]).splitlines()
        if tags_at_head != [version]:
            raise GitError("该提交还有其他标签，不能直接删除。")
        parent = self.run(["rev-parse", f"{commit}^"], check=False).strip()
        if not parent:
            raise GitError("首个 Git 提交不能用此方式删除。")
        # A soft reset preserves existing staged content, including partial edits.
        # Without staged changes, keep the removed commit's changes unstaged.
        staged = self.run(["diff", "--cached", "--name-only", "-z"])
        self.run(["reset", "--soft" if staged else "--mixed", parent])
        self.run(["tag", "--delete", version])
        self._remove_version_metadata(version)

    def _remove_version_metadata(self, version: str) -> None:
        manifest_path = (
            self.metadata_path
            / MANIFEST_DIRECTORY
            / f"{version}{MANIFEST_SUFFIX}"
        )
        try:
            manifest_path.unlink()
        except FileNotFoundError:
            pass
        self._write_changelog(None)

    def _archive_bytes(self, reference: str) -> bytes:
        paths = self.manifest(reference)
        if paths == []:
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w:"):
                pass
            return archive.getvalue()
        args = ["--literal-pathspecs", "archive", "--format=tar", reference]
        if paths:
            args.extend(["--", *paths])
        return self.run_bytes(args)

    def materialize(self, reference: str, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        archive = self._archive_bytes(reference)
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
            for member in bundle.getmembers():
                target = (destination / member.name).resolve()
                if destination.resolve() not in target.parents and target != destination.resolve():
                    raise GitError("版本包包含不安全的文件路径。")
            bundle.extractall(destination)

    def compare(self, left: str, right: Optional[str] = None) -> None:
        compare_tool = self.compare_tool()
        tool_label, command_name = COMPARE_TOOLS[compare_tool]
        if not shutil.which(command_name):
            raise GitError(f"没有找到 {tool_label}，请先安装或在“设置”中选择其他对比工具。")
        with tempfile.TemporaryDirectory(prefix="code-version-manager-compare-") as temporary:
            root = Path(temporary)
            left_directory = root / reference_folder_name(left, "left")
            self.materialize(left, left_directory)
            if right:
                right_directory = root / reference_folder_name(right, "right")
                self.materialize(right, right_directory)
                read_only_option = "-readonly"
            else:
                right_directory = self.path
                read_only_option = "-leftreadonly"
            environment = os.environ.copy()
            command = [command_name]
            if compare_tool == "bcompare":
                config_home = prepare_bcompare_config(root)
                environment["XDG_CONFIG_HOME"] = str(config_home)
                command.extend(["-solo", read_only_option, f"-filters={BCOMPARE_FILTER}"])
            command.extend([str(left_directory), str(right_directory)])
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            if result.returncode not in (0, 1):
                raise GitError(result.stderr.strip() or f"{tool_label} 启动失败。")

    def export(self, reference: str, destination: Path) -> None:
        paths = self.manifest(reference)
        if paths == []:
            with zipfile.ZipFile(destination, "w"):
                pass
            return
        args = ["--literal-pathspecs", "archive", "--format=zip", f"--output={destination}", reference]
        if paths:
            args.extend(["--", *paths])
        self.run(args)

    def commit_details(self, reference: str) -> str:
        return self.run(["show", "--stat", "--format=fuller", reference])


def version_key(value: str):
    match = VERSION_PATTERN.match(value)
    return tuple(int(item) for item in match.group(1).split(".")) if match else (-1,)


def allocated_disk_usage(path: Path) -> int:
    total = 0
    seen = set()
    for current, directories, filenames in os.walk(path, followlinks=False):
        for candidate in [Path(current), *[Path(current) / name for name in directories + filenames]]:
            try:
                file_stat = candidate.lstat()
            except OSError:
                continue
            identity = (file_stat.st_dev, file_stat.st_ino)
            if identity in seen:
                continue
            seen.add(identity)
            blocks = getattr(file_stat, "st_blocks", None)
            total += blocks * 512 if blocks is not None else file_stat.st_size
    return total


def format_storage_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KB", "MB", "GB"):
        value /= 1024
        if value < 1024:
            return f"{value:.1f} {unit}"
    return f"{value / 1024:.1f} TB"


def normalize_changes(message: str) -> List[str]:
    changes = []
    for line in message.splitlines():
        value = line.strip()
        if value.startswith(("-", "*")):
            value = value[1:].strip()
        if value:
            changes.append(value)
    return changes


def workspace_version_message(root: Path, version: str) -> Optional[str]:
    """Read only the requested RST release section, without changing the file."""
    version = canonical_version(version)
    parsed = parse_version_tag(version)
    if not parsed:
        return None
    try:
        lines = (root / WORKSPACE_CHANGELOG).read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        return None

    def is_heading(index: int) -> bool:
        title = lines[index].strip()
        underline = lines[index + 1].strip() if index + 1 < len(lines) else ""
        return bool(
            title and not lines[index][0].isspace()
            and len(underline) >= max(3, len(title))
            and re.fullmatch(r"([=\-~^\"'+#*`:])\1+", underline)
        )

    for index, line in enumerate(lines):
        heading = re.fullmatch(r"(.+?)\s+\(\d{4}-\d{2}-\d{2}\)", line.strip())
        expected = {display_version(version), version}
        if parsed[0] is None:
            expected.add(parsed[1][1:])
        if not heading or heading.group(1) not in expected or not is_heading(index):
            continue
        changes: List[str] = []
        for body_index in range(index + 2, len(lines)):
            if is_heading(body_index):
                break
            body = lines[body_index]
            bullet = re.match(r"^[*+-]\s+(.+)$", body)
            if bullet:
                changes.append(bullet.group(1).strip())
            elif body[:1].isspace() and body.strip() and changes:
                changes[-1] += " " + body.strip()
        return "\n".join(changes)
    return None


def prepare_workspace_release_files(root: Path) -> List[str]:
    """Create the local notes template and return changed files to publish."""
    changelog = root / WORKSPACE_CHANGELOG
    if changelog.is_symlink() or (changelog.exists() and not changelog.is_file()):
        raise GitError("无法创建 CHANGELOG.rst：根目录同名路径必须是普通文件。")
    gitignore = root / ".gitignore"
    if gitignore.is_symlink() or (gitignore.exists() and not gitignore.is_file()):
        raise GitError("无法更新 .gitignore：根目录 .gitignore 必须是普通文件。")
    if not changelog.exists() and not changelog.is_symlink():
        title = f"Changelog for {root.name}"
        marker = "^" * max(39, len(title))
        heading = f"0.0.1 ({date.today().isoformat()})"
        changelog.write_text(
            f"{marker}\n{title}\n{marker}\n\n"
            ".. 在下方添加版本节，例如（去掉示例前的注释标记）：\n..\n"
            f"..    {heading}\n..    {'-' * len(heading)}\n..    * 改动内容\n",
            encoding="utf-8",
        )
    if not gitignore.exists():
        gitignore.write_text(DEFAULT_WORKSPACE_GITIGNORE, encoding="utf-8")
        return [".gitignore"]
    content = gitignore.read_bytes()
    if b"/CHANGELOG.rst" not in content.splitlines():
        newline = b"\r\n" if b"\r\n" in content else b"\n"
        separator = b"" if not content or content.endswith(b"\n") else newline
        gitignore.write_bytes(content + separator + b"/CHANGELOG.rst" + newline)
        return [".gitignore"]
    return []


def validate_selected_files(root: Path, selected_files: Sequence[str]) -> List[str]:
    selected = []
    root = root.resolve()
    for relative in sorted(set(selected_files)):
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            continue
        path = root / relative_path
        if path.is_symlink():
            selected.append(relative_path.as_posix())
            continue
        resolved = path.resolve()
        if root not in resolved.parents or not resolved.is_file():
            continue
        selected.append(relative_path.as_posix())
    return selected


def scan_workspace_files(path: Path) -> List[str]:
    root = path.resolve()
    files: List[str] = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if name not in DEFAULT_EXCLUDED_DIRECTORIES]
        current_path = Path(current)
        for filename in filenames:
            absolute = current_path / filename
            relative = absolute.relative_to(root).as_posix()
            files.append(relative)
    return sorted(files)


def top_level_package_groups(
    path: Path, deleted_files: Sequence[str] = ()
) -> Dict[str, List[str]]:
    root = path.resolve()
    groups: Dict[str, List[str]] = {}
    for entry in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if entry.name in DEFAULT_EXCLUDED_DIRECTORIES:
            continue
        if entry.is_dir() and not entry.is_symlink():
            nested = scan_workspace_files(entry)
            if nested:
                groups[f"{entry.name}/"] = [f"{entry.name}/{item}" for item in nested]
        elif entry.is_file() or entry.is_symlink():
            groups[entry.name] = [entry.name]
    # Deleted top-level entries cannot be discovered by walking the workspace.
    # Keep them selectable so git add -u can record the removals too.
    for relative in deleted_files:
        parts = Path(relative).parts
        if parts:
            entry = parts[0]
            if entry in groups or entry + "/" in groups:
                continue
            groups[entry + "/" if len(parts) > 1 else entry] = []
    return dict(sorted(groups.items(), key=lambda item: item[0].lower()))


def expand_package_groups(
    groups: Dict[str, List[str]], selected_entries: Sequence[str]
) -> List[str]:
    files = set()
    for entry in selected_entries:
        files.update(groups.get(entry, []))
    return sorted(files)


def top_level_groups_for_paths(
    groups: Dict[str, List[str]], paths: Sequence[str]
) -> List[str]:
    selected_paths = set(paths)
    entries = []
    for entry, files in groups.items():
        prefix = entry if entry.endswith("/") else None
        if any(path in selected_paths for path in files):
            entries.append(entry)
        elif prefix and any(path.startswith(prefix) for path in selected_paths):
            entries.append(entry)
        elif not prefix and entry in selected_paths:
            entries.append(entry)
    return entries


def validate_top_level_scopes(scopes: Sequence[str]) -> List[str]:
    validated = []
    for scope in scopes:
        value = scope.rstrip("/")
        path = Path(value)
        if value and not path.is_absolute() and len(path.parts) == 1 and ".." not in path.parts:
            validated.append(value)
    return sorted(set(validated))


def reference_folder_name(reference: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", reference).strip("._")
    return cleaned or fallback


def bundled_bcompare_sessions_path() -> Path:
    """Locate the BCSessions.xml shipped with the source tree or package."""
    source = Path(__file__).resolve()
    candidates = (
        source.parent / "bcompare" / "BCSessions.xml",
        source.parents[1] / "data" / "bcompare" / "BCSessions.xml",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise GitError("软件缺少 Beyond Compare 默认配置文件。")


def prepare_bcompare_config(temporary_root: Path) -> Path:
    # BCSessions.xml is shipped with this application so the comparison rules
    # do not depend on whichever workspace or user session was opened first.
    template = bundled_bcompare_sessions_path()
    config_home = temporary_root / "config"
    target = config_home / "bcompare"
    source_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    source = source_home / "bcompare"
    if source.is_dir():
        shutil.copytree(
            source,
            target,
            # Sessions contain user-specific paths and rules.  They are
            # replaced below so every workspace gets the same compare setup.
            ignore=shutil.ignore_patterns(
                "BCLOCK_*",
                "BcCrashReport-*.log",
                "BCSessions.xml",
                "BCSessions.xml.bak",
            ),
        )
    else:
        target.mkdir(parents=True, exist_ok=True)

    sessions_path = target / "BCSessions.xml"
    shutil.copyfile(template, sessions_path)
    return config_home


def ensure_git_identity(repository: GitRepository) -> None:
    if not repository.run(["config", "user.name"], check=False).strip():
        repository.run(["config", "user.name", os.environ.get("USER", "Local User")])
    if not repository.run(["config", "user.email"], check=False).strip():
        user = os.environ.get("USER", "user")
        repository.run(["config", "user.email", f"{user}@localhost"])


def create_repository(path: Path) -> GitRepository:
    root = path.expanduser().resolve()
    result = subprocess.run(
        ["git", "-C", str(root), "init", "-q"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise GitError(result.stderr.strip() or "无法启用版本管理。")
    repository = GitRepository(root)
    repository.run(["symbolic-ref", "HEAD", "refs/heads/main"])
    return repository


def initialize_repository(
    path: Path, version: str, message: str, selected_files: Sequence[str],
    *, sync_version_file: Optional[bool] = None,
) -> GitRepository:
    selected = validate_selected_files(path.expanduser().resolve(), selected_files)
    if not selected:
        raise GitError("至少选择一个需要封装的文件。")
    repository = create_repository(path)
    ensure_git_identity(repository)
    repository.save_version(
        version, message, selected,
        sync_version_file=sync_version_file,
    )
    return repository


class FileSelectionDialog(Gtk.Dialog):
    def __init__(
        self,
        parent: Gtk.Window,
        files: Sequence[str],
        selected_files: Sequence[str],
        changed_files: Sequence[str],
        title: str = "选择版本封装文件",
        deleted_entries: Sequence[str] = (),
        ignored_removals: Sequence[str] = (),
    ):
        super().__init__(title=title, transient_for=parent, modal=True)
        self.set_default_size(820, 620)
        self.add_buttons("取消", Gtk.ResponseType.CANCEL, "继续", Gtk.ResponseType.OK)
        selected = set(selected_files)
        changed = set(changed_files)
        deleted = set(deleted_entries)

        content = self.get_content_area()
        content.set_spacing(8)
        content.set_border_width(12)

        if ignored_removals:
            hint = Gtk.Label(
                label=f"新版本将停止跟踪 {len(ignored_removals)} 个已被忽略的文件，本地文件保留。",
                xalign=0,
            )
            hint.set_line_wrap(True)
            hint.set_tooltip_text("\n".join(ignored_removals))
            content.pack_start(hint, False, False, 0)

        if deleted:
            hint = Gtk.Label(
                label="勾选“已删除”项会将删除记录写入版本。", xalign=0
            )
            content.pack_start(hint, False, False, 0)

        search = Gtk.SearchEntry()
        search.set_placeholder_text("搜索文件路径")
        content.pack_start(search, False, False, 0)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        all_button = Gtk.Button(label="全选")
        none_button = Gtk.Button(label="取消全选")
        changed_button = Gtk.Button(label="只选修改")
        controls.pack_start(all_button, False, False, 0)
        controls.pack_start(none_button, False, False, 0)
        controls.pack_start(changed_button, False, False, 0)
        self.count_label = Gtk.Label(xalign=1)
        controls.pack_end(self.count_label, False, False, 0)
        content.pack_start(controls, False, False, 0)

        self.store = Gtk.ListStore(bool, str, str, bool)
        for path in files:
            is_changed = path in changed
            item_type = "目录" if path.endswith("/") else "文件"
            status = "已删除" if path in deleted else ("已修改" if is_changed else item_type)
            self.store.append(
                [path in selected, status, path, is_changed]
            )
        self.filter = self.store.filter_new()
        self.search_text = ""
        self.filter.set_visible_func(self._filter_visible)

        tree = Gtk.TreeView(model=self.filter)
        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self._toggle_file)
        tree.append_column(Gtk.TreeViewColumn("封装", toggle, active=0))
        tree.append_column(Gtk.TreeViewColumn("状态", Gtk.CellRendererText(), text=1))
        path_column = Gtk.TreeViewColumn("文件", Gtk.CellRendererText(), text=2)
        path_column.set_expand(True)
        tree.append_column(path_column)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(tree)
        content.pack_start(scroll, True, True, 0)

        search.connect("search-changed", self._search_changed)
        all_button.connect("clicked", lambda _button: self._select_mode("all"))
        none_button.connect("clicked", lambda _button: self._select_mode("none"))
        changed_button.connect("clicked", lambda _button: self._select_mode("changed"))
        self._update_count()

    def _filter_visible(self, model, tree_iter, _data=None) -> bool:
        if not self.search_text:
            return True
        return self.search_text in model.get_value(tree_iter, 2).lower()

    def _search_changed(self, entry: Gtk.SearchEntry) -> None:
        self.search_text = entry.get_text().strip().lower()
        self.filter.refilter()

    def _toggle_file(self, _renderer, filtered_path: str) -> None:
        filtered_iter = self.filter.get_iter(filtered_path)
        child_iter = self.filter.convert_iter_to_child_iter(filtered_iter)
        self.store.set_value(child_iter, 0, not self.store.get_value(child_iter, 0))
        self._update_count()

    def _select_mode(self, mode: str) -> None:
        tree_iter = self.store.get_iter_first()
        while tree_iter:
            if mode == "all":
                value = True
            elif mode == "changed":
                value = self.store.get_value(tree_iter, 3)
            else:
                value = False
            self.store.set_value(tree_iter, 0, value)
            tree_iter = self.store.iter_next(tree_iter)
        self._update_count()

    def _update_count(self) -> None:
        selected = sum(1 for row in self.store if row[0])
        self.count_label.set_text(f"已选择 {selected} / {len(self.store)}")

    def selected_files(self) -> List[str]:
        return [row[2] for row in self.store if row[0]]


class VersionManagerWindow(Gtk.ApplicationWindow):
    def __init__(self, application: Gtk.Application, initial_path: Path):
        super().__init__(application=application, title=APP_NAME)
        self.get_style_context().add_class("version-manager-app")
        self.set_default_size(1120, 720)
        self.set_icon_name("code-version-manager")
        self.repository: Optional[GitRepository] = None
        self.workspace_path = initial_path.expanduser().resolve()
        self.history_entries: List[HistoryEntry] = []
        self.info_mode = ""
        self.busy = False
        self.background_timer: Optional[int] = None
        self.remote_status_generation = 0
        self.remote_status_snapshot: Optional[RemoteSyncSnapshot] = None
        self.remote_status = RemoteSyncStatus("unavailable", "尚未检查远程状态。")
        self.remote_sync_error: Optional[str] = None
        self.branch_generation = 0
        self.remote_branch_url = ""
        self.remote_branch_choices: List[BranchInfo] = []
        self.branch_choices: Dict[str, BranchChoice] = {}
        self.updating_branch_choices = False
        self.updating_history_scope = False
        self.connect("destroy", lambda _window: self._invalidate_requests())

        self._build_header()
        self._build_content()
        self._apply_style()
        self.load_workspace(initial_path)

    def _build_header(self) -> None:
        header = Gtk.HeaderBar()
        header.get_style_context().add_class("app-header")
        self.header = header
        header.set_show_close_button(True)
        header.set_decoration_layout(":minimize,maximize,close")
        header.props.title = APP_NAME
        self.set_titlebar(header)

        open_button = self._command_button(
            "打开工作空间", "folder-open-symbolic", self.on_open_workspace
        )
        header.pack_start(open_button)

        self.recent_button = Gtk.MenuButton(label="最近工作空间")
        self.recent_button.set_image(
            Gtk.Image.new_from_icon_name("document-open-recent-symbolic", Gtk.IconSize.BUTTON)
        )
        self.recent_button.set_always_show_image(True)
        self.recent_button.set_tooltip_text("打开最近工作空间")
        self.recent_popover = Gtk.Popover.new(self.recent_button)
        self.recent_popover.connect("show", self.on_recent_popover_show)
        self.recent_button.set_popover(self.recent_popover)
        header.pack_start(self.recent_button)

        settings_button = self._command_button(
            "设置", "preferences-system-symbolic", self.on_settings
        )
        header.pack_start(settings_button)

        self.git_account_button = self._command_button(
            "Git 账号", "avatar-default-symbolic", self.on_git_account
        )
        header.pack_end(self.git_account_button)
        sync_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.sync_local_button = Gtk.Button(label="↓  同步本地")
        self.sync_local_button.set_tooltip_text("以远程分支和版本标签更新本地")
        self.sync_local_button.connect("clicked", self.on_sync_local)
        self.sync_local_button.get_style_context().add_class("sync-action")
        self.sync_remote_button = Gtk.Button(label="↑  同步远程")
        self.sync_remote_button.set_tooltip_text("以本地分支和版本标签覆盖远程")
        self.sync_remote_button.connect("clicked", self.on_sync_remote)
        self.sync_remote_button.get_style_context().add_class("sync-action")
        self.sync_local_button.set_sensitive(False)
        self.sync_remote_button.set_sensitive(False)
        sync_actions.pack_start(self.sync_local_button, False, False, 0)
        sync_actions.pack_start(self.sync_remote_button, False, False, 0)
        header.pack_end(sync_actions)

        refresh_button = self._icon_button("view-refresh-symbolic", "刷新", self.on_refresh)
        header.pack_end(refresh_button)
        about_button = self._icon_button("help-about-symbolic", "关于", self.on_about)
        header.pack_end(about_button)

    def _icon_button(self, icon: str, tooltip: str, callback) -> Gtk.Button:
        button = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
        button.set_tooltip_text(tooltip)
        button.connect("clicked", callback)
        return button

    def _command_button(self, label: str, icon: str, callback) -> Gtk.Button:
        button = Gtk.Button(label=label)
        button.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))
        button.set_always_show_image(True)
        button.connect("clicked", callback)
        return button

    @staticmethod
    def _add_maximize_button(dialog: Gtk.Dialog) -> Gtk.Button:
        dialog.set_type_hint(Gdk.WindowTypeHint.NORMAL)
        header = Gtk.HeaderBar()
        header.get_style_context().add_class("app-header")
        header.set_title(dialog.get_title())
        header.set_show_close_button(True)
        header.set_decoration_layout(":close")
        button = Gtk.Button.new_from_icon_name(
            "window-maximize-symbolic", Gtk.IconSize.BUTTON
        )
        button.set_tooltip_text("最大化窗口")
        button.connect(
            "clicked", VersionManagerWindow._on_toggle_dialog_maximized, dialog
        )
        dialog.connect(
            "window-state-event", VersionManagerWindow._on_dialog_window_state, button
        )
        header.pack_end(button)
        dialog.set_titlebar(header)
        return button

    @staticmethod
    def _on_toggle_dialog_maximized(_button, dialog: Gtk.Dialog) -> None:
        if dialog.is_maximized():
            dialog.unmaximize()
        else:
            dialog.maximize()

    @staticmethod
    def _on_dialog_window_state(_dialog, event, button: Gtk.Button) -> bool:
        maximized = bool(event.new_window_state & Gdk.WindowState.MAXIMIZED)
        icon = "window-restore-symbolic" if maximized else "window-maximize-symbolic"
        tooltip = "还原窗口" if maximized else "最大化窗口"
        button.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))
        button.set_tooltip_text(tooltip)
        return False

    def _build_content(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.get_style_context().add_class("app-shell")
        self.add(root)

        self.info_bar = Gtk.InfoBar()
        self.info_bar.set_message_type(Gtk.MessageType.WARNING)
        self.info_label = Gtk.Label(xalign=0)
        self.info_label.set_use_markup(True)
        self.info_bar.get_content_area().add(self.info_label)
        self.info_action_button = self.info_bar.add_button("选择工作分支", Gtk.ResponseType.OK)
        self.info_bar.connect("response", self.on_return_main)
        root.pack_start(self.info_bar, False, False, 0)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.toolbar = toolbar
        toolbar.get_style_context().add_class("command-bar")
        self.save_button = self._command_button("发布版本", "document-save-symbolic", self.on_save_version)
        self.save_button.get_style_context().add_class("primary-action")
        self.save_button.set_tooltip_text("仅在本地创建版本，不上传到远程仓库")
        self.compare_button = self._command_button("比较所选版本", "edit-copy-symbolic", self.on_compare_versions)
        self.current_button = self._command_button(
            "所选版本与当前", "document-open-recent-symbolic", self.on_compare_current
        )
        self.export_button = self._command_button("下载版本", "document-save-symbolic", self.on_download_version)
        self.edit_message_button = self._command_button(
            "修改说明", "document-edit-symbolic", self.on_edit_version_message
        )
        self.delete_button = self._icon_button(
            "edit-delete-symbolic", "删除所选版本", self.on_delete_version
        )
        self.delete_button.get_style_context().add_class("destructive-action")
        self.changelog_button = self._command_button(
            "查看 CHANGELOG", "document-properties-symbolic", self.on_view_changelog
        )
        self.download_changelog_button = self._command_button(
            "下载 CHANGELOG", "document-save-symbolic", self.on_download_changelog
        )
        buttons = (
            self.save_button,
            self.compare_button,
            self.current_button,
            self.export_button,
            self.edit_message_button,
            self.delete_button,
            self.changelog_button,
            self.download_changelog_button,
        )
        for index, button in enumerate(buttons):
            if index in (1, 3, 5, 7):
                separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
                separator.get_style_context().add_class("command-separator")
                toolbar.pack_start(separator, False, False, 4)
            toolbar.pack_start(button, False, False, 0)
        root.pack_start(toolbar, False, False, 0)

        self.branch_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                                  margin_start=16, margin_end=16, margin_top=8)
        self.branch_bar.pack_start(Gtk.Label(label="分支"), False, False, 0)
        self.branch_model = Gtk.ListStore(str, str, str, bool)
        self.branch_combo = Gtk.ComboBox.new_with_model(self.branch_model)
        self.branch_combo.set_id_column(0)
        branch_renderer = Gtk.CellRendererText()
        branch_renderer.set_property("ellipsize", 3)
        branch_renderer.set_property("max-width-chars", 24)
        self.branch_combo.pack_start(branch_renderer, True)
        self.branch_combo.add_attribute(branch_renderer, "text", 1)
        self.branch_combo.add_attribute(branch_renderer, "foreground", 2)
        self.branch_combo.add_attribute(branch_renderer, "foreground-set", 3)
        self.branch_combo.connect("changed", self.on_branch_selected)
        self.branch_bar.pack_start(self.branch_combo, False, False, 0)
        self.new_branch_button = self._command_button("新建分支", "list-add-symbolic", self.on_create_branch)
        self.refresh_branches_button = self._command_button("刷新远程分支", "view-refresh-symbolic", self.on_refresh_remote_branches)
        self.merge_button = self._command_button("合并到 main", "emblem-shared-symbolic", self.on_merge_to_main)
        self.delete_branch_button = self._command_button("删除已合并分支", "edit-delete-symbolic", self.on_delete_merged_branch)
        for button in (self.new_branch_button, self.refresh_branches_button, self.merge_button, self.delete_branch_button):
            self.branch_bar.pack_start(button, False, False, 0)
        self.branch_status_label = Gtk.Label(xalign=0, ellipsize=3)
        self.branch_status_label.set_hexpand(True)
        self.branch_bar.pack_start(self.branch_status_label, True, True, 0)
        root.pack_start(self.branch_bar, False, False, 0)

        self.merge_bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                                 margin_start=16, margin_end=16, margin_top=8)
        self.merge_status_label = Gtk.Label(xalign=0)
        self.merge_status_label.set_line_wrap(True)
        self.merge_bar.pack_start(self.merge_status_label, False, False, 0)
        merge_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.conflict_combo = Gtk.ComboBoxText()
        self.conflict_combo.set_hexpand(True)
        merge_actions.pack_start(self.conflict_combo, True, True, 0)
        self.merge_tool_button = Gtk.Button(label="打开三方合并工具")
        self.merge_tool_button.connect("clicked", self.on_merge_tool)
        self.resolve_button = Gtk.Button(label="标记已解决")
        self.resolve_button.connect("clicked", self.on_resolve_conflict)
        self.resolve_delete_button = Gtk.Button(label="以删除解决")
        self.resolve_delete_button.connect("clicked", self.on_resolve_conflict, True)
        self.complete_merge_button = Gtk.Button(label="完成合并")
        self.complete_merge_button.connect("clicked", self.on_complete_merge)
        self.abort_merge_button = Gtk.Button(label="取消合并")
        self.abort_merge_button.connect("clicked", self.on_abort_merge)
        for button in (self.merge_tool_button, self.resolve_button, self.resolve_delete_button,
                       self.complete_merge_button, self.abort_merge_button):
            merge_actions.pack_start(button, False, False, 0)
        self.merge_bar.pack_start(merge_actions, False, False, 0)
        root.pack_start(self.merge_bar, False, False, 0)

        self.remote_summary_label = Gtk.Label(xalign=0, margin_start=16, margin_end=16, margin_top=8)
        self.remote_summary_label.set_line_wrap(True)
        self.remote_summary_label.set_max_width_chars(100)
        self.remote_summary_label.get_style_context().add_class("remote-summary")
        root.pack_start(self.remote_summary_label, False, False, 0)

        self.notebook = Gtk.Notebook()
        history_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        filters = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                          margin_start=16, margin_end=16, margin_top=8)
        self.history_scope_combo = Gtk.ComboBoxText()
        self.history_scope_combo.append("current", "本分支相关版本")
        self.history_scope_combo.append("all", "全部分支版本")
        self.history_scope_combo.set_active_id("current")
        self.history_scope_combo.connect("changed", self.on_history_scope_changed)
        filters.pack_start(self.history_scope_combo, False, False, 0)
        hint = Gtk.Label(
            label="本分支相关版本包含当前分支版本和继承的正式版本；全部分支版本包含仓库内所有分支版本。",
            xalign=0,
        )
        filters.pack_start(hint, False, False, 0)
        history_panel.pack_start(filters, False, False, 0)
        history_panel.pack_start(self._build_history_view(), True, True, 0)
        self.notebook.append_page(history_panel, Gtk.Label(label="版本历史"))
        self.notebook.append_page(self._build_changes_view(), Gtk.Label(label="当前修改"))
        root.pack_start(self.notebook, True, True, 0)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        footer.get_style_context().add_class("footer")
        self.repo_label = Gtk.Label(xalign=0, ellipsize=3)
        self.repo_label.get_style_context().add_class("workspace-status")
        self.storage_label = Gtk.Label(xalign=1)
        self.storage_label.get_style_context().add_class("storage-status")
        self.storage_label.set_tooltip_text(
            "当前工作空间 Git 增量版本历史实际占用的磁盘空间，不包含工作空间源文件"
        )
        self.status_label = Gtk.Label(xalign=1)
        self.status_label.get_style_context().add_class("operation-status")
        footer.pack_start(self.repo_label, True, True, 0)
        footer.pack_end(self.status_label, False, False, 0)
        footer.pack_end(self.storage_label, False, False, 0)
        root.pack_end(footer, False, False, 0)

    def _build_history_view(self) -> Gtk.Widget:
        self.history_store = Gtk.ListStore(str, str, str, str, str)
        self.history_tree = Gtk.TreeView(model=self.history_store)
        self.history_tree.set_headers_clickable(True)
        self.history_tree.get_style_context().add_class("data-table")
        self.history_tree.set_rules_hint(True)
        self.history_tree.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.history_tree.connect("row-activated", self.on_history_activated)
        columns = (
            ("版本", 0, 150),
            ("日期", 1, 110),
            ("修改说明", 2, 520),
            ("提交", 3, 100),
        )
        for title, index, width in columns:
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", 3)
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_min_width(width)
            column.set_resizable(True)
            self.history_tree.append_column(column)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.history_tree)
        return scroll

    def _build_changes_view(self) -> Gtk.Widget:
        self.change_store = Gtk.ListStore(str, str)
        tree = Gtk.TreeView(model=self.change_store)
        tree.get_style_context().add_class("data-table")
        tree.set_rules_hint(True)
        status_renderer = Gtk.CellRendererText()
        path_renderer = Gtk.CellRendererText()
        tree.append_column(Gtk.TreeViewColumn("状态", status_renderer, text=0))
        path_column = Gtk.TreeViewColumn("文件", path_renderer, text=1)
        path_column.set_expand(True)
        tree.append_column(path_column)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(tree)
        return scroll

    def _apply_style(self) -> None:
        css = b"""
        window.version-manager-app { background-color: #f3f6f8; color: #24313b; }
        headerbar.app-header, headerbar.app-header:backdrop {
            min-height: 48px; padding: 4px 10px; background-image: none;
            background-color: #ffffff; border-bottom: 1px solid #d9e0e5;
            color: #24313b; box-shadow: none; text-shadow: none;
        }
        headerbar.app-header .title, headerbar.app-header .subtitle {
            color: #24313b; text-shadow: none;
        }
        headerbar.app-header button {
            min-height: 28px; min-width: 24px; padding: 4px 8px;
            background-image: none; background-color: #ffffff; color: #26343e;
            border: 1px solid #d4dce2; border-radius: 4px;
            box-shadow: none; text-shadow: none;
        }
        headerbar.app-header button label, headerbar.app-header button image {
            color: inherit; -gtk-icon-shadow: none;
        }
        headerbar.app-header button:hover {
            background-color: #edf3f5; border-color: #9eafb9;
        }
        headerbar.app-header button:disabled {
            color: #89959d; background-color: #f3f5f6;
        }
        headerbar.app-header button.sync-action {
            min-width: 112px; padding: 4px 10px; font-weight: bold;
        }
        headerbar.app-header button.titlebutton.close:hover {
            background-color: #bb3030; border-color: #982727; color: white;
        }
        .command-bar {
            padding: 9px 12px; background-color: #f8fafb;
            border-bottom: 1px solid #d9e0e5;
        }
        .command-bar button {
            min-height: 32px; padding: 3px 9px; border-radius: 4px;
            border: 1px solid #d4dce2; background-image: none;
            background-color: #ffffff; color: #26343e;
        }
        .command-bar button:hover { background-color: #edf3f5; border-color: #9eafb9; }
        .command-bar button:disabled { color: #89959d; background-color: #f3f5f6; }
        .command-bar button.primary-action,
        .command-bar button.primary-action:disabled {
            color: #ffffff; background-color: #197d87; border-color: #126a73;
            text-shadow: none; opacity: 1;
        }
        .command-bar button.primary-action:hover { background-color: #126d76; }
        .command-bar button.destructive-action { color: #a73232; }
        .command-bar button.destructive-action:hover {
            color: #8f2222; background-color: #fff0ef; border-color: #d99a96;
        }
        .command-separator { margin: 3px 3px; background-color: #d5dde2; }
        notebook { padding: 8px 12px 10px; }
        notebook > header { background-color: transparent; border-bottom: 1px solid #d5dde2; }
        notebook > header > tabs > tab { padding: 8px 14px; color: #5d6a73; }
        notebook > header > tabs > tab:checked {
            color: #126d76; border-bottom: 2px solid #197d87;
        }
        treeview.data-table.view {
            padding: 2px; background-color: #ffffff; color: #26343e;
            border: 1px solid #d9e0e5;
        }
        treeview.data-table.view:selected,
        treeview.data-table.view:selected:focus {
            background-color: #d9eff0; color: #17383b;
        }
        treeview.data-table header button {
            min-height: 32px; padding: 4px 8px; color: #43525c;
            background-image: none; background-color: #edf2f4;
            border: 0; border-right: 1px solid #d9e0e5;
            border-bottom: 1px solid #d9e0e5; border-radius: 0;
            font-weight: bold;
        }
        .footer {
            min-height: 34px; padding: 6px 12px; background-color: #ffffff;
            border-top: 1px solid #d9e0e5;
        }
        .workspace-status { color: #43525c; }
        .storage-status { color: #687780; }
        .operation-status { color: #126d76; font-weight: bold; }
        .remote-summary { color: #43525c; }
        .sync-summary {
            padding: 12px; border: 1px solid #cbdde0; border-radius: 6px;
            background-color: #eaf4f5; color: #234a50;
        }
        .sync-comparison {
            padding: 14px; border-bottom: 1px solid #d9e0e5;
            background-color: #ffffff;
        }
        .sync-heading { font-weight: bold; }
        .sync-effect { color: #a73232; font-weight: bold; }
        .sync-note { color: #687780; }
        .dialog-title { color: #24343e; font-size: 14pt; font-weight: bold; }
        dialog { background-color: #f3f6f8; color: #26343e; }
        dialog entry, dialog spinbutton, dialog combobox button {
            min-height: 30px; padding: 3px 7px; border-radius: 4px;
            background-color: #ffffff; color: #26343e;
            border: 1px solid #cbd5db;
        }
        dialog entry:focus, dialog spinbutton:focus, dialog combobox button:focus {
            border-color: #197d87;
        }
        dialog button { min-height: 30px; padding: 3px 10px; border-radius: 4px; }
        dialog scrolledwindow { border: 1px solid #d9e0e5; }
        infobar.warning { background-color: #fff3dc; color: #654d1b; }
        headerbar.app-header button.sync-pending,
        headerbar.app-header button.sync-pending:disabled {
            background-image: none; background-color: #bb3030; color: white;
            border-color: #982727; text-shadow: none; box-shadow: none; opacity: 1;
        }
        headerbar.app-header button.sync-pending:hover { background-color: #a32929; }
        headerbar.app-header button.sync-synced,
        headerbar.app-header button.sync-synced:disabled {
            background-image: none; background-color: #32804f; color: white;
            border-color: #286b41; text-shadow: none; box-shadow: none; opacity: 1;
        }
        headerbar.app-header button.sync-unavailable,
        headerbar.app-header button.sync-unavailable:disabled {
            background-image: none; background-color: #f1f4f5; color: #89959d;
            border-color: #d5dde2; text-shadow: none; box-shadow: none; opacity: 1;
        }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(
            self.get_screen(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def load_workspace(self, path: Path) -> None:
        resolved = path.expanduser().resolve()
        if not resolved.is_dir():
            self.show_error("所选工作空间不存在。")
            return
        self.workspace_path = resolved
        self.remote_sync_error = None
        self.branch_generation += 1
        self.remote_branch_choices = []
        self.remote_branch_url = ""
        self._invalidate_remote_status()
        try:
            self.repository = GitRepository(resolved)
            self.workspace_path = self.repository.path
        except GitError:
            self.repository = None
        self.updating_history_scope = True
        self.history_scope_combo.set_active_id("current")
        self.updating_history_scope = False
        self._save_repository_path(self.workspace_path)
        self.refresh()
        if self.repository and self.repository.remote_url():
            self.on_refresh_remote_branches(None)

    def _invalidate_requests(self) -> None:
        self.branch_generation += 1
        self._invalidate_remote_status()

    def _refresh_branch_choices(self) -> None:
        self.updating_branch_choices = True
        try:
            self.branch_choices = {}
            self.branch_model.clear()
            if not self.repository:
                self.branch_model.append(["none", "未启用版本管理", None, False])
                self.branch_combo.set_active_id("none")
                self.branch_bar.set_sensitive(False)
                self.branch_status_label.set_text("请先发布首个版本")
                self.merge_bar.hide()
                self.merge_bar.set_no_show_all(True)
                return
            repo = self.repository
            try:
                remote_url = repo._sync_target()
            except GitError:
                remote_url = ""
            if self.remote_branch_url != remote_url:
                self.remote_branch_choices = []
                self.remote_branch_url = remote_url
                self.branch_generation += 1
            branches = repo.local_branches()
            active = "detached"
            if not repo.current_branch():
                self.branch_model.append([active, "历史提交（请选择分支）", None, False])
            for choice in combine_branch_choices(branches, self.remote_branch_choices):
                key = "branch:" + choice.name
                self.branch_choices[key] = choice
                self.branch_model.append([
                    key, choice.label, choice.foreground, choice.foreground is not None,
                ])
                if choice.current:
                    active = key
            self.branch_combo.set_active_id(active)
            self.branch_combo.set_tooltip_text("当前分支：" + (repo.current_branch() or "历史提交"))
            state = repo.merge_state()
            self.branch_bar.set_sensitive(not self.busy and not state.active)
            has_head = bool(repo.run(["rev-parse", "--verify", "HEAD"], check=False).strip())
            self.new_branch_button.set_sensitive(has_head)
            self.refresh_branches_button.set_sensitive(bool(remote_url))
            self.merge_button.set_sensitive(any(branch.name == "main" for branch in branches) and len(branches) > 1)
            self.delete_branch_button.set_sensitive(repo.current_branch() == "main" and len(branches) > 1)
            self.branch_status_label.set_text("main 发正式版；开发分支发自己的预发布版")
            self._refresh_merge_controls(state)
        finally:
            self.updating_branch_choices = False

    def _refresh_merge_controls(self, state: MergeState) -> None:
        self.merge_bar.set_no_show_all(not state.active)
        if not state.active:
            self.merge_bar.hide()
            return
        self.merge_status_label.set_text(
            f"正在合并 {state.source or '开发分支'} → main：{len(state.conflicts)} 个未解决冲突。"
            "解决后请标记并完成合并；也可取消。完成前禁止切换、发布和同步。"
            + ("\n" + state.notice if state.notice else "")
        )
        self.conflict_combo.remove_all()
        for path in state.conflicts:
            self.conflict_combo.append(path, path)
        self.conflict_combo.set_active(0)
        self.complete_merge_button.set_sensitive(state.ready)
        for button in (self.merge_tool_button, self.resolve_button, self.resolve_delete_button):
            button.set_sensitive(bool(state.conflicts))
        self.merge_bar.show_all()

    def on_refresh_remote_branches(self, _button) -> None:
        repo = self.repository
        if not repo or self.busy:
            return
        try:
            remote_url = repo._sync_target()
        except GitError as error:
            self.branch_status_label.set_text(str(error))
            return
        self.branch_generation += 1
        generation = self.branch_generation
        self.remote_branch_url = remote_url
        self.refresh_branches_button.set_sensitive(False)
        self.branch_status_label.set_text("正在读取远程分支…")

        def worker():
            try:
                branches, error = repo.remote_branches(), ""
            except Exception as exception:
                branches, error = [], str(exception)
            GLib.idle_add(self._remote_branches_complete, repo, generation, remote_url, branches, error)

        threading.Thread(target=worker, daemon=True).start()

    def _remote_branches_complete(self, repo, generation, remote_url, branches, error) -> bool:
        if repo is not self.repository or generation != self.branch_generation or self.busy:
            return False
        try:
            if repo._sync_target() != remote_url:
                return False
        except GitError:
            return False
        self.remote_branch_choices = branches
        self._refresh_branch_choices()
        message = "远程分支读取失败：" + error if error else f"origin 共 {len(branches)} 个分支；网站标签仍是仓库级列表。"
        self.branch_status_label.set_text(message.splitlines()[0])
        self.branch_status_label.set_tooltip_text(message)
        return False

    def on_branch_selected(self, combo) -> None:
        if self.updating_branch_choices or self.busy or not self.repository:
            return
        branch = self.branch_choices.get(combo.get_active_id())
        if not branch or branch.current:
            return
        repo = self.repository
        self._refresh_branch_choices()
        self.run_background(
            f"正在切换到 {branch.name}…",
            lambda: repo.switch_branch(branch.name, branch.source, branch.oid), "分支已切换。",
        )

    def on_create_branch(self, _button) -> None:
        if not self.repository or self.busy:
            return
        repo = self.repository
        dialog = Gtk.Dialog(title="新建并切换分支", transient_for=self, modal=True)
        dialog.add_buttons("取消", Gtk.ResponseType.CANCEL, "创建并切换", Gtk.ResponseType.OK)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=16)
        content.pack_start(Gtk.Label(label="从当前提交创建；不会自动上传。分支名示例：test1、feature/camera。"), False, False, 0)
        entry = Gtk.Entry(placeholder_text="新分支名称")
        content.pack_start(entry, False, False, 0)
        dialog.get_content_area().add(content)
        dialog.show_all()
        response = dialog.run()
        name = entry.get_text().strip()
        dialog.destroy()
        if response == Gtk.ResponseType.OK:
            self.run_background("正在创建分支…", lambda: repo.create_branch(name), "分支已创建。")

    def _choose_development_branch(self, title: str) -> Optional[str]:
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.add_buttons("取消", Gtk.ResponseType.CANCEL, "继续", Gtk.ResponseType.OK)
        combo = Gtk.ComboBoxText(margin=16)
        for branch in self.repository.local_branches():
            if branch.name != "main":
                combo.append(branch.name, branch.name)
        combo.set_active(0)
        if self.repository.current_branch() != "main":
            combo.set_active_id(self.repository.current_branch())
        dialog.get_content_area().add(combo)
        dialog.show_all()
        response = dialog.run()
        branch = combo.get_active_id()
        dialog.destroy()
        return branch if response == Gtk.ResponseType.OK else None

    def on_merge_to_main(self, _button) -> None:
        if not self.repository or self.busy:
            return
        source = self._choose_development_branch("选择合并到 main 的开发分支")
        if source:
            repo = self.repository
            self.run_background("正在安全更新并合并到 main…", lambda: repo.merge_to_main(source), "合并已完成。")

    def on_merge_tool(self, _button) -> None:
        path = self.conflict_combo.get_active_id()
        if path and self.repository:
            repo = self.repository
            self.run_background("三方合并工具已打开…", lambda: repo.launch_merge_tool(path), "请检查结果并标记已解决。")

    def on_resolve_conflict(self, _button, delete: bool = False) -> None:
        path = self.conflict_combo.get_active_id()
        if not path or not self.repository:
            return
        if delete:
            dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
                                       buttons=Gtk.ButtonsType.OK_CANCEL, text=f"确定以删除解决 {path} 的冲突？")
            response = dialog.run()
            dialog.destroy()
            if response != Gtk.ResponseType.OK:
                return
        repo = self.repository
        self.run_background("正在记录冲突解决结果…", lambda: repo.resolve_merge_file(path, delete), "冲突已处理。")

    def on_complete_merge(self, _button) -> None:
        if self.repository:
            self.run_background("正在完成合并…", self.repository.complete_merge, "合并已完成。")

    def on_abort_merge(self, _button) -> None:
        if not self.repository:
            return
        dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
                                   buttons=Gtk.ButtonsType.OK_CANCEL, text="取消合并并丢弃本次冲突解决内容？")
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.OK:
            self.run_background("正在取消合并…", self.repository.abort_merge, "合并已取消。")

    def on_delete_merged_branch(self, _button) -> None:
        if not self.repository or self.busy:
            return
        name = self._choose_development_branch("删除已合入 main 的开发分支")
        if not name:
            return
        repo = self.repository
        dialog = Gtk.Dialog(title=f"删除已合并分支 {name}", transient_for=self, modal=True)
        dialog.add_buttons("取消", Gtk.ResponseType.CANCEL, "查看删除清单", Gtk.ResponseType.OK)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=16)
        cleanup = Gtk.CheckButton(label="同时清理此分支前缀的预发布标签（默认保留归档）")
        remote = Gtk.CheckButton(label="同时删除 origin 分支；勾选清理时也删除远程对应标签")
        remote.set_sensitive(bool(repo.remote_url()))
        content.pack_start(cleanup, False, False, 0)
        content.pack_start(remote, False, False, 0)
        content.pack_start(Gtk.Label(label="不会删除正式基线或 main。远程删除前必须先上传合并后的 main。"), False, False, 0)
        dialog.get_content_area().add(content)
        dialog.show_all()
        response = dialog.run()
        cleanup_tags, delete_remote = cleanup.get_active(), remote.get_active()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        # Build the exact deletion list in the worker; approval happens after it is available.
        self.run_background(
            "正在检查删除清单…", lambda: repo.plan_branch_deletion(name, cleanup_tags, delete_remote),
            "删除清单已就绪。", deletion_preview=True,
        )

    def _confirm_branch_deletion(self, plan: BranchDeletionPlan) -> None:
        if not self.repository:
            return
        rows = []
        for side, refs in (("本地", plan.local_refs), ("origin", plan.remote_refs)):
            for ref, _oid in refs:
                if ref == f"refs/heads/{plan.name}" or (plan.cleanup_tags and self.repository._owned_ref(ref, plan.name)):
                    rows.append(side + " · " + (ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else display_version(ref[len("refs/tags/"):])) )
        dialog = Gtk.Dialog(title="确认删除以下分支和标签", transient_for=self, modal=True)
        dialog.add_buttons("取消", Gtk.ResponseType.CANCEL, "确认删除", Gtk.ResponseType.OK)
        dialog.set_default_size(650, 400)
        text = Gtk.TextView(editable=False, monospace=True)
        text.get_buffer().set_text("\n".join(rows) + ("\n\n预发布标签保留归档。" if not plan.cleanup_tags else ""))
        scroll = Gtk.ScrolledWindow()
        scroll.add(text)
        dialog.get_content_area().pack_start(scroll, True, True, 12)
        dialog.show_all()
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.OK:
            repo = self.repository
            self.run_background("正在删除已合并分支…", lambda: repo.delete_merged_branch(
                plan.name, plan.cleanup_tags, plan.delete_remote, expected=plan,
            ), "分支已删除。")

    def on_history_scope_changed(self, _combo) -> None:
        if not self.updating_history_scope and not self.busy:
            self.refresh(update_remote=False)

    def refresh(self, check_remote: bool = True, update_remote: bool = True) -> None:
        if not self.repository:
            self._refresh_branch_choices()
            self.history_entries = []
            self.history_store.clear()
            self.change_store.clear()
            self.info_mode = "unversioned"
            self.info_label.set_markup(
                "<b>该工作空间还没有版本</b>  启用后可选择文件并保存第一个版本。"
            )
            self.info_action_button.set_label("启用版本管理")
            self.info_bar.show_all()
            self.save_button.set_sensitive(False)
            self._invalidate_remote_status()
            self._set_remote_status(RemoteSyncStatus("unavailable", "请先启用版本管理并发布本地版本。"))
            self.compare_button.set_sensitive(False)
            self.current_button.set_sensitive(False)
            self.export_button.set_sensitive(False)
            self.edit_message_button.set_sensitive(False)
            self.delete_button.set_sensitive(False)
            self.changelog_button.set_sensitive(False)
            self.download_changelog_button.set_sensitive(False)
            self.repo_label.set_text(f"{self.workspace_path}  ·  未启用版本管理")
            self.storage_label.set_text("版本存储 0 B")
            self.status_label.set_text("尚无版本")
            return
        try:
            self._refresh_branch_choices()
            changes = self.repository.status()
            self.history_entries = self.repository.history(all_versions=self.history_scope_combo.get_active_id() == "all")
            self.history_store.clear()
            for entry in self.history_entries:
                version = ", ".join(display_version(tag) for tag in entry.tags)
                self.history_store.append(
                    [version, entry.date, entry.subject, entry.short_commit, entry.commit]
                )
            self.change_store.clear()
            for change in changes:
                self.change_store.append([change.status, change.path])

            branch = self.repository.current_branch()
            if branch:
                self.repository.remember_working_branch()
                self.info_mode = ""
                self.info_bar.hide()
                self.save_button.set_sensitive(not self.repository.merge_state().active)
                self.save_button.set_label("发布正式版本" if branch == "main" else "发布预发布版本")
                self.save_button.set_tooltip_text(
                    "创建全仓库正式标签，例如 v0.3.1"
                    if branch == "main" else f"创建仅属于 {branch} 的预发布标签，例如 {branch}/v0.3.0-beta.0.0.1"
                )
                branch_label = branch
            else:
                self.info_mode = "detached"
                label = html.escape(self.repository.head_label())
                self.info_label.set_markup(
                    f"<b>当前正在查看历史版本 {label}</b>  发布版本已禁用，请返回工作分支或新建分支。"
                )
                previous = self.repository.last_working_branch()
                self.info_action_button.set_label(f"返回 {previous}" if previous else "选择工作分支")
                self.info_bar.show_all()
                self.save_button.set_sensitive(False)
                branch_label = f"历史版本 {self.repository.head_label()}"
            self.repo_label.set_text(f"{self.repository.path}  ·  {branch_label}")
            self.storage_label.set_text(
                f"版本存储 {format_storage_size(self.repository.storage_size())}"
            )
            status = "工作区干净" if not changes else f"{len(changes)} 项修改"
            self.status_label.set_text(status)
            has_versions = bool(self.history_entries)
            self.current_button.set_sensitive(has_versions)
            self.compare_button.set_sensitive(len(self.history_entries) >= 2)
            self.export_button.set_sensitive(has_versions)
            self.edit_message_button.set_sensitive(has_versions)
            self.delete_button.set_sensitive(has_versions)
            self.changelog_button.set_sensitive(True)
            self.download_changelog_button.set_sensitive(True)
            if not update_remote:
                return
            if check_remote:
                self._refresh_remote_status()
            else:
                self._invalidate_remote_status()
                self._set_remote_status(RemoteSyncStatus(
                    "unavailable", "配置已保存，尚未验证远程状态。请点击“刷新”或“保存并验证连接”。",
                ))
        except GitError as error:
            self._invalidate_remote_status()
            self._set_remote_status(RemoteSyncStatus("unavailable", str(error)))
            self.show_error(str(error))

    def _invalidate_remote_status(self) -> None:
        self.remote_status_generation += 1
        self.remote_status_snapshot = None

    def _set_remote_status(self, status: RemoteSyncStatus) -> None:
        self.remote_status = status
        self.remote_summary_label.set_text(status.message.splitlines()[0][:200])
        self.remote_summary_label.set_tooltip_text(status.message)
        pending = bool(status.local_changes or status.remote_changes)
        self._set_sync_button(
            self.sync_local_button,
            status.can_sync_local,
            pending,
            status.message + "\n" + (status.local_message or "以远程分支和版本标签更新本地。"),
            status.local_message or status.message,
        )
        self._set_sync_button(
            self.sync_remote_button,
            status.can_sync_remote,
            pending,
            status.message + "\n" + (status.remote_message or "以本地分支和版本标签覆盖远程差异。"),
            status.remote_message or status.message,
        )

    def _set_sync_button(
        self, button: Gtk.Button, can_sync: bool, pending: bool,
        active_message: str, inactive_message: str,
    ) -> None:
        context = button.get_style_context()
        context.remove_class("sync-synced")
        context.remove_class("sync-pending")
        context.remove_class("sync-unavailable")
        if self.remote_status.state == "checking":
            context.add_class("sync-unavailable")
            message = "正在检查远程分支和版本标签。"
        elif pending:
            context.add_class("sync-pending")
            message = active_message if can_sync else (inactive_message or active_message)
        elif self.remote_status.state == "unavailable":
            context.add_class("sync-unavailable")
            message = inactive_message or self.remote_status.message
        else:
            context.add_class("sync-synced")
            message = "本地分支和版本标签已与远程一致。"
        button.set_tooltip_text(message)
        button.set_sensitive(pending and can_sync and not self.busy)

    def _refresh_remote_status(self) -> None:
        if self.busy:
            self._invalidate_remote_status()
            self._set_remote_status(RemoteSyncStatus("checking", "操作进行中，完成后重新检查远程状态。"))
            return
        repository = self.repository
        try:
            snapshot = repository.sync_snapshot()
        except GitError as error:
            self._invalidate_remote_status()
            self._set_remote_status(RemoteSyncStatus("unavailable", str(error)))
            return
        if self.remote_status_snapshot == snapshot and self.remote_status.state == "checking":
            return
        self._invalidate_remote_status()
        generation = self.remote_status_generation
        self.remote_status_snapshot = snapshot
        self._set_remote_status(RemoteSyncStatus("checking", "正在比较当前分支、版本标签并检查推送权限，不上传版本。"))

        def worker() -> None:
            try:
                status = repository.remote_sync_status(snapshot)
            except Exception as error:
                status = RemoteSyncStatus("unavailable", f"无法检查远程状态：{error}\n请点击“刷新”重试。")
            GLib.idle_add(self._remote_status_complete, repository, generation, snapshot, status)

        threading.Thread(target=worker, daemon=True).start()

    def _remote_status_complete(
        self, repository: GitRepository, generation: int,
        snapshot: RemoteSyncSnapshot, status: RemoteSyncStatus,
    ) -> bool:
        if generation != self.remote_status_generation or repository is not self.repository or self.busy:
            return False
        try:
            current_snapshot = repository.sync_snapshot()
        except GitError as error:
            self._invalidate_remote_status()
            self._set_remote_status(RemoteSyncStatus("unavailable", str(error)))
            return False
        if current_snapshot != snapshot:
            self._invalidate_remote_status()
            self._refresh_remote_status()
            return False
        self._set_remote_status(status)
        return False

    def selected_history(self) -> List[HistoryEntry]:
        model, paths = self.history_tree.get_selection().get_selected_rows()
        entries = []
        for path in paths:
            index = path.get_indices()[0]
            if 0 <= index < len(self.history_entries):
                entries.append(self.history_entries[index])
        return entries

    def on_open_workspace(self, _button) -> None:
        dialog = Gtk.FileChooserDialog(
            title="选择工作空间",
            parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dialog.add_buttons("取消", Gtk.ResponseType.CANCEL, "打开", Gtk.ResponseType.OK)
        dialog.set_current_folder(str(self.workspace_path))
        response = dialog.run()
        filename = dialog.get_filename()
        dialog.destroy()
        if response == Gtk.ResponseType.OK and filename:
            self.load_workspace(Path(filename))

    def on_recent_popover_show(self, popover: Gtk.Popover) -> None:
        child = popover.get_child()
        if child:
            child.destroy()

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        content.set_border_width(6)
        paths = recent_workspaces()
        if not paths:
            empty = Gtk.Label(label="暂无最近工作空间", xalign=0)
            empty.set_sensitive(False)
            content.pack_start(empty, False, False, 8)
        else:
            for path in paths:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
                label = f"当前  ·  {path}" if path == self.workspace_path else str(path)
                # GtkModelButton always interprets underscores as mnemonic markers.
                button = Gtk.ModelButton(text=label.replace("_", "__"))
                button.set_tooltip_text(str(path))
                button.connect("clicked", self.on_open_recent_workspace, path)
                row.pack_start(button, True, True, 0)
                remove_button = Gtk.Button.new_from_icon_name(
                    "window-close-symbolic", Gtk.IconSize.MENU
                )
                remove_button.set_relief(Gtk.ReliefStyle.NONE)
                remove_button.set_tooltip_text(f"从最近列表移除：{path}")
                remove_button.get_accessible().set_name(f"移除最近工作空间：{path}")
                remove_button.connect("clicked", self.on_remove_recent_workspace, path)
                row.pack_end(remove_button, False, False, 0)
                content.pack_start(row, False, False, 0)

            content.pack_start(Gtk.Separator(), False, False, 4)
            clear_button = Gtk.ModelButton(text="清空最近打开…")
            clear_button.set_property("icon", Gio.ThemedIcon.new("edit-clear-symbolic"))
            clear_button.connect("clicked", self.on_clear_recent_workspaces)
            content.pack_start(clear_button, False, False, 0)

        popover.add(content)
        content.show_all()

    def on_open_recent_workspace(self, _button, path: Path) -> None:
        self.recent_popover.popdown()
        if path == self.workspace_path:
            return
        self.load_workspace(path)

    def on_remove_recent_workspace(self, _button, path: Path) -> None:
        try:
            remove_recent_workspace(path)
            self.on_recent_popover_show(self.recent_popover)
            self.status_label.set_text(f"已从最近列表移除：{path}")
        except OSError as error:
            self.show_error(str(error))

    def on_clear_recent_workspaces(self, _button) -> None:
        self.recent_popover.popdown()
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text="清空最近打开的工作空间？",
        )
        dialog.format_secondary_text(
            "只会清空最近列表，不会删除工作空间或更改当前工作空间。"
        )
        dialog.add_button("取消", Gtk.ResponseType.CANCEL)
        clear_button = dialog.add_button("清空", Gtk.ResponseType.OK)
        clear_button.get_style_context().add_class("destructive-action")
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        try:
            clear_recent_workspaces()
            self.status_label.set_text("最近打开的工作空间已清空")
        except OSError as error:
            self.show_error(str(error))

    def on_refresh(self, _button) -> None:
        self.remote_sync_error = None
        self.refresh()
        self.on_refresh_remote_branches(None)

    def on_about(self, _button) -> None:
        dialog = Gtk.AboutDialog(transient_for=self, modal=True)
        dialog.set_program_name("Code Version Manager")
        dialog.set_version(APP_VERSION)
        dialog.set_comments("Git 工作空间版本管理器，支持本地版本与远程同步")
        dialog.set_logo_icon_name("code-version-manager")
        dialog.run()
        dialog.destroy()

    def on_settings(self, _button) -> None:
        if not self.repository:
            self.show_error("请先启用版本管理，再修改工作空间设置。")
            return
        try:
            configured_tool = self.repository.compare_tool()
        except GitError:
            configured_tool = DEFAULT_COMPARE_TOOL
        dialog = Gtk.Dialog(title="设置", transient_for=self, modal=True)
        dialog.add_buttons("取消", Gtk.ResponseType.CANCEL, "保存", Gtk.ResponseType.OK)
        dialog.set_default_size(560, 360)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin=20)
        title = Gtk.Label(label="工作空间设置", xalign=0)
        title.get_style_context().add_class("dialog-title")
        content.pack_start(title, False, False, 0)
        description = Gtk.Label(
            label="设置当前工作空间的对比工具和版本文件。设置保存在当前仓库，不影响其他工作空间。",
            xalign=0,
        )
        description.set_line_wrap(True)
        description.set_max_width_chars(60)
        content.pack_start(description, False, False, 0)
        grid = Gtk.Grid(column_spacing=14, row_spacing=10)
        combo = Gtk.ComboBoxText()
        for tool_id, (tool_label, _command) in COMPARE_TOOLS.items():
            combo.append(tool_id, tool_label)
        combo.set_active_id(configured_tool)
        combo.set_hexpand(True)
        combo.set_tooltip_text("选择发布版本比较时使用的图形对比工具")
        grid.attach(Gtk.Label(label="对比工具", xalign=0), 0, 0, 1, 1)
        grid.attach(combo, 1, 0, 1, 1)
        note = Gtk.Label(
            label="支持 Beyond Compare、Meld 和 KDiff3。所选工具需要先安装；默认使用 Beyond Compare。",
            xalign=0,
        )
        note.set_line_wrap(True)
        note.set_max_width_chars(60)
        grid.attach(note, 0, 1, 2, 1)
        content.pack_start(grid, False, False, 0)
        version_sync = Gtk.CheckButton(label="发布时生成并同步 VERSION 文件")
        version_sync.set_active(self.repository.sync_version_file_enabled())
        version_sync.set_tooltip_text("发布时写入不带 v 的版本号，并自动纳入版本提交和 ZIP；保存设置不会立即修改文件。")
        content.pack_start(version_sync, False, False, 0)
        content.pack_start(
            Gtk.Label(label=f"当前工作空间：{self.workspace_path}", xalign=0),
            False, False, 0,
        )
        dialog.get_content_area().add(content)
        dialog.show_all()
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            try:
                self.repository.configure_compare_tool(combo.get_active_id())
                self.repository.configure_version_file(version_sync.get_active())
                self.status_label.set_text("工作空间设置已保存")
            except (GitError, OSError, ValueError) as error:
                dialog.destroy()
                self.show_error(str(error))
                return
        dialog.destroy()

    def on_git_account(self, _button) -> None:
        repository = self.repository
        dialog = Gtk.Dialog(title="Git 账号与远程仓库", transient_for=self, modal=True)
        dialog.add_buttons(
            "取消", Gtk.ResponseType.CANCEL,
            "保存配置", Gtk.ResponseType.APPLY,
            "保存并验证连接", Gtk.ResponseType.OK,
        )
        dialog.get_widget_for_response(Gtk.ResponseType.APPLY).set_tooltip_text(
            "保存当前工作空间的仓库地址、提交身份和超时设置；不连接远程仓库"
        )
        dialog.get_widget_for_response(Gtk.ResponseType.OK).set_tooltip_text(
            "先保存配置，再检查仓库读取和推送权限；不上传提交或标签，完成后弹窗显示结果"
        )
        dialog.set_default_size(760, 560)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=18)
        grid = Gtk.Grid(column_spacing=14, row_spacing=12)
        configured_remote = repository.remote_url() if repository else ""
        suggested_remote = suggested_ssh_remote(configured_remote)
        remote_placeholder = f"git@github.com:<你的GitHub账号>/{workspace_repository_name(self.workspace_path.name)}.git"
        values = [
            ("SSH 仓库地址", suggested_remote or configured_remote, remote_placeholder),
            (
                "提交姓名",
                repository.run(["config", "user.name"], check=False).strip()
                if repository else os.environ.get("USER", ""),
                "Git 提交作者姓名",
            ),
            (
                "提交邮箱",
                repository.run(["config", "user.email"], check=False).strip()
                if repository else "",
                "name@example.com",
            ),
        ]
        entries = []
        for row, (label, value, placeholder) in enumerate(values):
            entry = Gtk.Entry(text=value)
            entry.set_placeholder_text(placeholder)
            entry.set_hexpand(True)
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            grid.attach(entry, 1, row, 1, 1)
            if row == 0:
                open_repository = Gtk.Button(label="打开网页")
                open_repository.set_tooltip_text("在浏览器中打开此 SSH 地址对应的仓库页面")
                open_repository.connect(
                    "clicked",
                    lambda _button: self.open_repository_web_page(dialog, entries[0]),
                )
                grid.attach(open_repository, 2, row, 1, 1)
            entries.append(entry)
        if suggested_remote:
            source = "已读取当前工作空间的 origin 地址。"
            if suggested_remote != configured_remote.strip():
                source = "已根据当前工作空间的 GitHub origin 地址转换为 SSH；保存后生效。"
        else:
            source = (
                f"SSH 地址示例：{remote_placeholder}\n"
                "请用仓库所属的账号或组织替换 <你的GitHub账号>，并确认仓库已创建；"
                "账号无法从本地目录或提交邮箱确定。"
            )
        address_hint = Gtk.Label(label=f"当前工作空间：{self.workspace_path}\n{source}", xalign=0)
        address_hint.set_selectable(True)
        address_hint.set_line_wrap(True)
        address_hint.set_max_width_chars(72)
        push_timeout = Gtk.SpinButton.new_with_range(1, MAX_PUSH_TIMEOUT // 60, 1)
        try:
            configured_timeout = repository.push_timeout_seconds() if repository else PUSH_TIMEOUT
        except GitError:
            configured_timeout = PUSH_TIMEOUT
        push_timeout.set_value(configured_timeout / 60)
        push_timeout.set_tooltip_text("仅限制推送传输时长，连接验证仍为 120 秒；超时不删除本地版本")
        advanced_grid = Gtk.Grid(column_spacing=14, row_spacing=8, margin_top=8)
        advanced_grid.attach(Gtk.Label(label="推送超时（分钟）", xalign=0), 0, 0, 1, 1)
        advanced_grid.attach(push_timeout, 1, 0, 1, 1)
        timeout_notice = Gtk.Label(
            label="推送超时默认 15 分钟，范围 1–60 分钟，仅用于实际上传。连接和推送预检查的时限为 120 秒。",
            xalign=0,
        )
        timeout_notice.set_line_wrap(True)
        timeout_notice.set_max_width_chars(68)
        advanced_grid.attach(timeout_notice, 0, 1, 2, 1)
        advanced = Gtk.Expander(label="高级设置：推送超时")
        advanced.add(advanced_grid)
        remote_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        create_remote = Gtk.Button(label="创建远程仓库（网页）")
        create_remote.set_tooltip_text("打开 GitHub 创建页面；登录并确认后才会创建，已有仓库无需重复创建")
        create_remote.connect(
            "clicked", lambda _button: self.open_github_repository_creation(dialog, entries[0])
        )
        ssh_settings = Gtk.Button(label="SSH 公钥设置")
        ssh_settings.connect("clicked", lambda _button: self.show_ssh_public_keys(dialog, entries[2].get_text()))
        remote_actions.pack_start(create_remote, False, False, 0)
        remote_actions.pack_start(ssh_settings, False, False, 0)
        notice = Gtk.Label(
            label="先在“SSH 公钥设置”中生成或复制公钥，再添加到 GitHub。"
            "提交姓名和邮箱用于记录作者，SSH 密钥决定登录身份。\n"
            "网页创建仓库时请勿初始化 README、.gitignore 或许可证，以免与本地历史冲突。"
            "创建后确认上方 SSH 地址。\n"
            "“保存配置”：仅保存到当前工作空间，不连接远程。\n"
            "“保存并验证连接”：保存后检查连接和推送权限，弹窗显示结果，不上传版本。\n"
            "发布版本只保存到本地。主窗口“同步本地”以远程为准；"
            "“同步远程”以本地为准，执行前会显示差异并要求确认。",
            xalign=0,
        )
        notice.set_line_wrap(True)
        notice.set_max_width_chars(72)
        content.pack_start(grid, False, False, 0)
        content.pack_start(address_hint, False, False, 0)
        content.pack_start(remote_actions, False, False, 0)
        content.pack_start(advanced, False, False, 0)
        content.pack_start(notice, False, False, 0)
        dialog.get_content_area().add(content)
        dialog.show_all()
        while True:
            response = dialog.run()
            if response not in (Gtk.ResponseType.OK, Gtk.ResponseType.APPLY):
                dialog.destroy()
                return
            remote_url, name, email = [entry.get_text() for entry in entries]
            try:
                remote_url = validate_remote_url(remote_url)
                validate_git_identity(name, email)
                if repository is None:
                    repository = create_repository(self.workspace_path)
                    self.repository = repository
                repository.configure_remote(
                    remote_url, name, email,
                    push_timeout=push_timeout.get_value_as_int() * 60,
                )
            except (GitError, OSError, ValueError) as error:
                self.show_error(str(error))
                continue
            dialog.destroy()
            self.remote_sync_error = None
            self.refresh(check_remote=False)
            if response == Gtk.ResponseType.OK:
                self.run_background(
                    "正在验证连接与推送权限（不上传版本）...", repository.check_remote_connection,
                    "远程验证完成。", show_result=True,
                )
            else:
                self.status_label.set_text("SSH 配置已保存，尚未验证连接；可重新打开“Git 账号”进行验证。")
            return

    def open_github_repository_creation(self, dialog, remote_entry) -> None:
        try:
            url = github_repository_creation_url(remote_entry.get_text(), self.workspace_path.name)
            Gtk.show_uri_on_window(dialog, url, Gdk.CURRENT_TIME)
        except (GitError, GLib.Error, OSError) as error:
            self.show_error(str(error))


    def open_repository_web_page(self, dialog, remote_entry) -> None:
        try:
            url = repository_web_url(remote_entry.get_text())
            Gtk.show_uri_on_window(dialog, url, Gdk.CURRENT_TIME)
        except (GitError, GLib.Error, OSError) as error:
            self.show_error(str(error))


    def show_ssh_public_keys(self, parent, email: str = "") -> None:
        dialog = Gtk.Dialog(title="GitHub SSH 公钥设置", transient_for=parent, modal=True)
        dialog.add_buttons("关闭", Gtk.ResponseType.CLOSE)
        dialog.set_default_size(720, 640)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=18)
        notice = Gtk.Label(
            label="1. 已有密钥可直接复制公钥；没有时填写邮箱并生成。\n"
            "2. 打开 GitHub 页面，选择 Authentication Key，粘贴公钥并保存。\n"
            "3. 返回“Git 账号”，点击“保存并验证连接”。仅将 .pub 公钥添加到 GitHub。\n"
            "首次连接请在终端运行 ssh -T git@github.com，核对官方主机指纹。"
            "加密私钥请先用 ssh-add 解锁。",
            xalign=0,
        )
        notice.set_line_wrap(True)
        notice.set_max_width_chars(72)
        content.pack_start(notice, False, False, 0)
        key_grid = Gtk.Grid(column_spacing=14, row_spacing=8)
        email_entry = Gtk.Entry(text=email)
        email_entry.set_placeholder_text("name@example.com")
        email_entry.set_hexpand(True)
        passphrase_entry = Gtk.Entry(visibility=False)
        passphrase_entry.set_placeholder_text("可选；设置后使用密钥前需用 ssh-add 解锁")
        confirmation = Gtk.Entry(visibility=False)
        confirmation.set_placeholder_text("再次输入密钥口令")
        for row, (label, entry) in enumerate((
            ("密钥邮箱", email_entry), ("密钥口令（可选）", passphrase_entry), ("确认口令", confirmation),
        )):
            key_grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            key_grid.attach(entry, 1, row, 1, 1)
        content.pack_start(key_grid, False, False, 0)
        generation_notice = Gtk.Label(
            label="密钥邮箱作为公钥备注。点击后在本机 ~/.ssh 中生成 Ed25519 密钥；"
            "已有同名文件时使用新文件名。私钥保存在本机，界面只显示公钥。",
            xalign=0,
        )
        generation_notice.set_line_wrap(True)
        generation_notice.set_max_width_chars(72)
        content.pack_start(generation_notice, False, False, 0)
        generate_button = Gtk.Button(label="生成新的 SSH 密钥")
        generate_button.set_halign(Gtk.Align.START)
        content.pack_start(generate_button, False, False, 0)
        status = Gtk.Label(xalign=0)
        status.set_line_wrap(True)
        status.set_selectable(True)
        status.set_max_width_chars(72)
        content.pack_start(status, False, False, 0)
        keys = {}
        selector = Gtk.ComboBoxText()
        key_usage = Gtk.Label(xalign=0)
        key_usage.set_line_wrap(True)
        key_usage.set_selectable(True)
        key_usage.set_max_width_chars(72)
        key_view = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.CHAR)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(90)
        scroll.add(key_view)
        copy_button = Gtk.Button(label="复制所选公钥")

        def select_key(combo) -> None:
            path = combo.get_active_id()
            key_view.get_buffer().set_text(keys.get(path, ""))
            if path:
                private_path = str(Path(path).with_suffix(""))
                key_usage.set_text("加载或解锁所选密钥的终端命令：ssh-add " + shlex.quote(private_path))
            else:
                key_usage.set_text("")

        def reload_keys(selected: Optional[str] = None) -> None:
            keys.clear()
            keys.update(ssh_public_keys())
            selector.remove_all()
            for path in keys:
                selector.append(path, path)
            if selected in keys:
                selector.set_active_id(selected)
            elif keys:
                selector.set_active(0)
            copy_button.set_sensitive(bool(keys))
            selector.set_sensitive(bool(keys))
            status.set_text(
                "已找到本机公钥，可直接复制并添加到 GitHub。" if keys else
                "未找到可用的 .pub 公钥。请在上方填写邮箱，点击“生成新的 SSH 密钥”。"
            )

        def copy_key(_button) -> None:
            public = keys.get(selector.get_active_id())
            if public:
                clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
                clipboard.set_text(public, -1)
                clipboard.store()
                status.set_text("公钥已复制。请打开 GitHub 添加页面，选择 Authentication Key 并粘贴保存。")

        def finish_generation(path: Optional[Path], error: Optional[str], encrypted: bool = False) -> bool:
            dialog.set_deletable(True)
            dialog.set_response_sensitive(Gtk.ResponseType.CLOSE, True)
            key_grid.set_sensitive(True)
            generate_button.set_sensitive(True)
            passphrase_entry.set_text("")
            confirmation.set_text("")
            if error:
                status.set_text("生成失败：" + error)
                return False
            reload_keys(str(path) + ".pub")
            message = (
                f"密钥已生成，公钥：{path}.pub\n"
                "请复制下方公钥并添加到 GitHub，再返回验证连接。"
            )
            if path.name != "id_ed25519" or encrypted:
                message += "\n使用前请在终端执行：ssh-add " + shlex.quote(str(path))
            status.set_text(message)
            return False

        def generate(_button) -> None:
            key_email = email_entry.get_text().strip()
            passphrase = passphrase_entry.get_text()
            if not re.fullmatch(r"[^@\s]+@[^@\s]+", key_email):
                status.set_text("请输入有效的密钥邮箱，例如 name@example.com。")
                email_entry.grab_focus()
                return
            if passphrase != confirmation.get_text():
                status.set_text("两次输入的密钥口令不一致，请重新输入。")
                return
            dialog.set_deletable(False)
            dialog.set_response_sensitive(Gtk.ResponseType.CLOSE, False)
            key_grid.set_sensitive(False)
            generate_button.set_sensitive(False)
            status.set_text("正在本机生成 Ed25519 密钥…")

            def worker() -> None:
                try:
                    path = generate_ssh_key(key_email, passphrase)
                except Exception as error:
                    GLib.idle_add(finish_generation, None, str(error))
                else:
                    GLib.idle_add(finish_generation, path, None, bool(passphrase))

            threading.Thread(target=worker, daemon=True).start()

        selector.connect("changed", select_key)
        copy_button.connect("clicked", copy_key)
        generate_button.connect("clicked", generate)

        def protect_generation(dialog, _response) -> None:
            if not generate_button.get_sensitive():
                dialog.stop_emission_by_name("response")

        dialog.connect("response", protect_generation)
        dialog.connect("delete-event", lambda *_args: not generate_button.get_sensitive())
        content.pack_start(selector, False, False, 0)
        content.pack_start(key_usage, False, False, 0)
        content.pack_start(scroll, True, True, 0)
        content.pack_start(copy_button, False, False, 0)
        reload_keys()
        link = Gtk.LinkButton.new_with_label("https://github.com/settings/ssh/new", "打开 GitHub 添加 SSH 公钥")
        link.set_halign(Gtk.Align.START)
        content.pack_start(link, False, False, 0)
        page_scroll = Gtk.ScrolledWindow()
        page_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        page_scroll.add(content)
        dialog.get_content_area().pack_start(page_scroll, True, True, 0)
        dialog.show_all()
        dialog.run()
        dialog.destroy()


    @staticmethod
    def _format_ref_oid(object_id: Optional[str]) -> str:
        return object_id[:12] if object_id else "（不存在）"

    @staticmethod
    def _sync_commit_text(commit: Optional[SyncCommit], object_id: Optional[str]) -> str:
        if not object_id:
            return "不存在"
        if not commit:
            return f"提交详情暂不可用\n引用：{object_id[:12]}"
        versions = "、".join(commit.versions) or "未标记版本"
        lines = [
            f"版本：{versions}", f"提交时间：{commit.date}",
            f"提交说明：{commit.subject}", f"提交：{commit.oid[:12]}",
        ]
        if commit.version_note:
            lines.append(f"版本说明：{commit.version_note}")
        if object_id != commit.oid:
            lines.append(f"标签对象：{object_id[:12]}")
        return "\n".join(lines)

    @staticmethod
    def _sync_label(text: str, style: str = "") -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=0, yalign=0)
        label.set_line_wrap(True)
        label.set_line_wrap_mode(2)  # Pango.WrapMode.WORD_CHAR
        label.set_max_width_chars(48)
        label.set_hexpand(True)
        label.set_selectable(True)
        if style:
            label.get_style_context().add_class(style)
        return label

    def _confirm_sync_direction(self, direction: str, status: RemoteSyncStatus) -> bool:
        branch_name = self.repository.current_branch() if self.repository else "当前"
        release_kind = "main 的正式标签" if branch_name == "main" else f"{branch_name} 的预发布标签"
        if direction == "local":
            title = f"确认以远程更新 {branch_name} 和所属标签？"
            description = (
                f"当前分支和{release_kind}将变为 origin/{branch_name} 的状态。"
                "本地独有提交和标签会保存在恢复点；未提交修改和未跟踪文件会先保存，"
                "完成后可恢复。会把可能被远程目标覆盖的被忽略文件移到恢复点。其他分支及非版本标签不变。"
            )
            action = "以远程为准，更新本地"
            differences = status.local_changes
        else:
            title = f"确认以本地更新 origin/{branch_name} 和所属标签？"
            description = (
                f"origin/{branch_name} 及其所属标签将变为本地状态。"
                "远程旧引用会先下载并保存在本机恢复点；远程协作者可能需要重新同步。"
                "其他分支及非版本标签不变。"
            )
            action = "以本地为准，覆盖远程"
            differences = status.remote_changes
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.add_button("取消", Gtk.ResponseType.CANCEL)
        confirm = dialog.add_button(action, Gtk.ResponseType.OK)
        confirm.get_style_context().add_class("destructive-action")
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        dialog.set_default_size(900, 680)
        self._add_maximize_button(dialog)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=16)
        summary = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        summary.get_style_context().add_class("sync-summary")
        if status.comparisons:
            branch = status.comparisons[0]
            summary.pack_start(self._sync_label(
                f"{branch.name}：{branch.summary}", "sync-heading",
            ), False, False, 0)
            summary.pack_start(self._sync_label(branch.effect(direction), "sync-effect"), False, False, 0)
        summary.pack_start(self._sync_label(
            "本次方向：远程 → 本地（以远程为准）" if direction == "local"
            else "本次方向：本地 → 远程（以本地为准）",
        ), False, False, 0)
        content.pack_start(summary, False, False, 0)
        content.pack_start(self._sync_label(
            "新旧依据提交的先后关系判断；提交时间和版本号供核对。未提交的工作区修改不参与新旧判断。",
            "sync-note",
        ), False, False, 0)

        rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        changed_refs = {difference.ref for difference in differences}
        for comparison in status.comparisons:
            if comparison.difference.ref not in changed_refs and comparison is not status.comparisons[0]:
                continue
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            row.get_style_context().add_class("sync-comparison")
            row.pack_start(self._sync_label(
                f"{comparison.name}  ·  {comparison.summary}", "sync-heading",
            ), False, False, 0)
            sides = Gtk.Grid(column_spacing=24, row_spacing=6, column_homogeneous=True)
            local_title = "本地（将被更新）" if direction == "local" else "本地（来源）"
            remote_title = "远程（来源）" if direction == "local" else "远程（将被覆盖）"
            sides.attach(self._sync_label(local_title, "sync-heading"), 0, 0, 1, 1)
            sides.attach(self._sync_label(remote_title, "sync-heading"), 1, 0, 1, 1)
            sides.attach(self._sync_label(self._sync_commit_text(
                comparison.local, comparison.difference.local_oid,
            )), 0, 1, 1, 1)
            sides.attach(self._sync_label(self._sync_commit_text(
                comparison.remote, comparison.difference.remote_oid,
            )), 1, 1, 1, 1)
            row.pack_start(sides, False, False, 0)
            row.pack_start(self._sync_label(comparison.effect(direction), "sync-effect"), False, False, 0)
            rows.pack_start(row, False, False, 0)
        if not status.comparisons:
            rows.pack_start(self._sync_label("暂时无法判断新旧，请核对以下引用差异。"), False, False, 0)
            for difference in differences:
                rows.pack_start(self._sync_label(
                    f"{difference.ref}\n本地：{self._format_ref_oid(difference.local_oid)}\n"
                    f"远程：{self._format_ref_oid(difference.remote_oid)}",
                ), False, False, 8)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(230)
        scroll.add(rows)
        content.pack_start(scroll, True, True, 0)
        if status.comparison_error:
            error_label = self._sync_label("部分提交详情获取失败，无法确认新旧。可取消后刷新重试。", "sync-effect")
            error_label.set_tooltip_text(status.comparison_error)
            content.pack_start(error_label, False, False, 0)
        content.pack_start(self._sync_label(description, "sync-note"), False, False, 0)
        dialog.get_content_area().pack_start(content, True, True, 0)
        dialog.show_all()
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.OK

    def _sync_direction(self, direction: str) -> None:
        repository = self.repository
        status = self.remote_status
        if not repository or self.busy:
            return
        can_sync = status.can_sync_local if direction == "local" else status.can_sync_remote
        if not can_sync:
            return
        try:
            snapshot = repository.sync_snapshot()
        except GitError as error:
            self._invalidate_remote_status()
            self._set_remote_status(RemoteSyncStatus("unavailable", str(error)))
            return
        if snapshot != self.remote_status_snapshot:
            self._refresh_remote_status()
            return
        if not self._confirm_sync_direction(direction, status):
            return
        if direction == "local":
            operation = lambda: repository.sync_local_from_remote(snapshot, status)
            busy = "正在以远程状态更新本地..."
        else:
            operation = lambda: repository.sync_remote_from_local(snapshot, status)
            busy = "正在以本地状态覆盖远程..."
        self.remote_sync_error = None
        self.run_background(busy, operation, "分支和版本标签已同步。", remote_sync=True)

    def on_sync_local(self, _button) -> None:
        self._sync_direction("local")

    def on_sync_remote(self, _button) -> None:
        self._sync_direction("remote")

    def on_return_main(self, _bar, response) -> None:
        if response != Gtk.ResponseType.OK:
            return
        if self.info_mode == "unversioned":
            self.on_enable_versioning()
            return
        if not self.repository:
            return
        repo = self.repository
        previous = repo.last_working_branch()
        if not previous or not any(branch.name == previous for branch in repo.local_branches()):
            self.branch_combo.grab_focus()
            self.branch_combo.popup()
            return
        self.run_background("正在返回工作分支…", lambda: repo.switch_branch(previous), "已返回工作分支。")

    def on_save_version(self, _button) -> None:
        if not self.repository:
            return
        repository = self.repository
        try:
            default_version = repository.next_version()
        except GitError as error:
            self.show_error(str(error))
            return
        metadata = self.prompt_version_metadata(display_version(default_version))
        if not metadata:
            return
        version, message, sync_version = metadata
        deleted = repository.filter_ignored(repository.deleted_files())
        ignored_removals = repository.ignored_tracked_files()
        groups = top_level_package_groups(self.workspace_path, deleted)
        allowed = set(repository.filter_ignored(expand_package_groups(groups, list(groups))))
        groups = {
            entry: [path for path in paths if path in allowed]
            for entry, paths in groups.items()
            if not paths or any(path in allowed for path in paths)
        }
        entries = list(groups)
        if not entries and not ignored_removals:
            self.show_error("工作空间中没有可以封装的文件。")
            return
        default_files = self.repository.default_package_files()
        changed_paths = self.repository.changed_paths()
        default_entries = set(top_level_groups_for_paths(groups, default_files))
        default_entries.update(top_level_groups_for_paths(groups, changed_paths))
        changed_entries = top_level_groups_for_paths(groups, changed_paths)
        selected_entries = self.prompt_file_selection(
            entries,
            sorted(default_entries),
            changed_entries,
            title="选择工作空间顶层文件或目录",
            deleted_entries=[entry for entry in entries if not groups[entry]],
            ignored_removals=ignored_removals,
        )
        if selected_entries is None:
            return
        selected = expand_package_groups(groups, selected_entries)
        self.run_background(
            "正在发布版本...",
            lambda: repository.save_version(
                version, message, selected, selected_scopes=selected_entries,
                sync_version_file=sync_version,
            ),
            f"版本 {version} 已发布到本地。需要上传时请单独点击“同步远程”。",
        )

    def on_enable_versioning(self) -> None:
        try:
            prepare_workspace_release_files(self.workspace_path)
        except (GitError, OSError) as error:
            self.show_error(str(error))
            return
        groups = top_level_package_groups(self.workspace_path)
        groups.pop(WORKSPACE_CHANGELOG, None)
        entries = list(groups)
        if not entries:
            self.show_error("工作空间中没有可以封装的文件。")
            return
        selected_entries = self.prompt_file_selection(
            entries,
            entries,
            [],
            title="选择工作空间顶层文件或目录",
        )
        if selected_entries is None:
            return
        selected = expand_package_groups(groups, selected_entries)
        metadata = self.prompt_version_metadata("v0.0.1")
        if not metadata:
            return
        version, message, sync_version = metadata

        def initialize() -> None:
            initialize_repository(
                self.workspace_path, version, message, selected,
                sync_version_file=sync_version,
            )

        self.run_background(
            "正在启用版本管理...",
            initialize,
            f"首个版本 {version} 已发布到本地。",
            reload_workspace=True,
        )

    def prompt_file_selection(
        self,
        files: Sequence[str],
        selected_files: Sequence[str],
        changed_files: Sequence[str],
        title: str = "选择版本封装文件",
        deleted_entries: Sequence[str] = (),
        ignored_removals: Sequence[str] = (),
    ) -> Optional[List[str]]:
        dialog = FileSelectionDialog(
            self, files, selected_files, changed_files, title=title,
            deleted_entries=deleted_entries, ignored_removals=ignored_removals,
        )
        dialog.show_all()
        response = dialog.run()
        selected = dialog.selected_files()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return None
        if not selected and not ignored_removals:
            self.show_error("至少选择一个需要封装的文件、目录或已删除项。")
            return None
        return selected

    def prompt_version_metadata(self, default_version: str):
        title = "发布正式版本" if self.repository and self.repository.current_branch() == "main" else "发布预发布版本"
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.add_buttons("取消", Gtk.ResponseType.CANCEL, "继续", Gtk.ResponseType.OK)
        dialog.set_default_size(720, 500)
        dialog.set_size_request(560, 360)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=18)
        content.set_hexpand(True)
        content.set_vexpand(True)
        version_row = Gtk.Grid(column_spacing=12)
        version_row.set_hexpand(True)
        version_entry = Gtk.Entry(text=default_version)
        version_entry.set_hexpand(True)
        version_row.attach(Gtk.Label(label="版本号", xalign=0), 0, 0, 1, 1)
        version_row.attach(version_entry, 1, 0, 1, 1)
        changes_view, changes_scroll = self._build_changes_editor()
        changes_buffer = changes_view.get_buffer()

        def load_changes(entry):
            message = workspace_version_message(self.workspace_path, entry.get_text().strip())
            if message is not None:
                changes_buffer.set_text(message)

        version_entry.connect("changed", load_changes)
        load_changes(version_entry)
        content.pack_start(version_row, False, False, 0)
        content.pack_start(Gtk.Label(
            label="main 发布正式版；开发分支发布带分支名的 beta 版本。Git 网站的标签页仍展示全仓库标签。",
            xalign=0, wrap=True,
        ), False, False, 0)
        content.pack_start(
            Gtk.Label(label="改动内容（每行一条）", xalign=0), False, False, 0
        )
        content.pack_start(changes_scroll, True, True, 0)
        note = Gtk.Label(
            label="版本号匹配 CHANGELOG.rst 中的版本节时，会自动载入对应说明；未匹配时保留当前内容。",
            xalign=0,
        )
        note.set_line_wrap(True)
        content.pack_start(note, False, False, 0)
        version_sync = Gtk.CheckButton(label="发布时生成并同步 VERSION 文件（自动纳入版本包）")
        version_sync.set_active(
            self.repository.sync_version_file_enabled() if self.repository
            else (self.workspace_path / WORKSPACE_VERSION).is_file()
        )
        content.pack_start(version_sync, False, False, 0)
        dialog.get_content_area().pack_start(content, True, True, 0)
        dialog.show_all()
        version_entry.grab_focus()
        version_entry.select_region(0, -1)
        response = dialog.run()
        buffer = changes_view.get_buffer()
        message = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True).strip()
        result = (version_entry.get_text().strip(), message, version_sync.get_active())
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return None
        if not result[1]:
            self.show_error("请填写修改说明。")
            return None
        return result

    def prompt_version_message(self, version: str, current_message: str) -> Optional[str]:
        dialog = Gtk.Dialog(title=f"修改 {version} 的说明", transient_for=self, modal=True)
        dialog.add_buttons("取消", Gtk.ResponseType.CANCEL, "保存", Gtk.ResponseType.OK)
        dialog.set_default_size(720, 500)
        dialog.set_size_request(560, 360)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=18)
        content.set_hexpand(True)
        content.set_vexpand(True)
        content.pack_start(Gtk.Label(label="改动内容（每行一条）", xalign=0), False, False, 0)
        changes_view, changes_scroll = self._build_changes_editor(current_message)
        content.pack_start(changes_scroll, True, True, 0)
        dialog.get_content_area().pack_start(content, True, True, 0)
        dialog.show_all()
        changes_view.grab_focus()
        response = dialog.run()
        buffer = changes_view.get_buffer()
        message = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True).strip()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return None
        if not message:
            self.show_error("请填写修改说明。")
            return None
        return message

    @staticmethod
    def _build_changes_editor(initial_message: str = ""):
        changes_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        changes_view.set_hexpand(True)
        changes_view.set_vexpand(True)
        changes_view.set_left_margin(10)
        changes_view.set_right_margin(10)
        changes_view.set_top_margin(8)
        changes_view.set_bottom_margin(8)
        changes_view.get_buffer().set_text(initial_message)
        changes_scroll = Gtk.ScrolledWindow()
        changes_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        changes_scroll.set_shadow_type(Gtk.ShadowType.IN)
        changes_scroll.set_min_content_height(280)
        changes_scroll.set_hexpand(True)
        changes_scroll.set_vexpand(True)
        changes_scroll.add(changes_view)
        return changes_view, changes_scroll

    def on_edit_version_message(self, _button) -> None:
        if not self.repository:
            return
        selected = self.selected_history()
        if len(selected) != 1:
            self.show_error("请在版本历史中选择一个需要修改说明的版本。")
            return
        if selected[0].read_only:
            self.show_error("所选版本是继承的正式版本或属于其他分支，只能比较和导出。")
            return
        versions = [tag for tag in selected[0].tags if parse_version_tag(tag)]
        if len(versions) != 1:
            self.show_error("所选记录没有唯一的版本标签，无法修改说明。")
            return
        version = versions[0]
        label = display_version(version)
        try:
            current_message = self.repository.version_message(version)
        except GitError as error:
            self.show_error(str(error))
            return
        message = self.prompt_version_message(label, current_message)
        if message is None:
            return
        self.run_background(
            "正在更新修改说明...",
            lambda: self.repository.update_version_message(version, message),
            f"版本 {label} 的修改说明已更新。",
        )

    def on_compare_versions(self, _button) -> None:
        selected = self.selected_history()
        if len(selected) != 2:
            self.show_error("请在版本历史中按住 Ctrl 选择两个版本。")
            return
        positions = [self.history_entries.index(entry) for entry in selected]
        older = selected[positions.index(max(positions))]
        newer = selected[positions.index(min(positions))]
        self.run_background(
            "对比工具已打开...",
            lambda: self.repository.compare(older.reference, newer.reference),
            "版本比较已关闭。",
        )

    def on_compare_current(self, _button) -> None:
        if not self.repository:
            return
        selected = self.selected_history()
        if len(selected) != 1:
            self.show_error("请在版本历史中选择一个要与当前工作空间比较的版本。")
            return
        entry = selected[0]
        self.run_background(
            "对比工具已打开...",
            lambda: self.repository.compare(entry.reference),
            "版本与当前工作空间的比较已关闭。",
        )

    def on_download_version(self, _button) -> None:
        selected = self.selected_history()
        if len(selected) != 1:
            self.show_error("请在版本历史中选择一个需要下载的版本。")
            return
        entry = selected[0]
        version_name = reference_folder_name(display_version(entry.tags[0]), "version") if entry.tags else entry.short_commit
        default_name = f"{self.workspace_path.name}_{version_name}.zip"
        dialog = Gtk.FileChooserDialog(
            title="下载版本",
            parent=self,
            action=Gtk.FileChooserAction.SAVE,
        )
        dialog.add_buttons("取消", Gtk.ResponseType.CANCEL, "下载", Gtk.ResponseType.OK)
        dialog.set_current_name(default_name)
        dialog.set_do_overwrite_confirmation(True)
        response = dialog.run()
        filename = dialog.get_filename()
        dialog.destroy()
        if response == Gtk.ResponseType.OK and filename:
            self.run_background(
                "正在下载版本...",
                lambda: self.repository.export(entry.reference, Path(filename)),
                f"版本已下载到 {filename}",
            )

    def on_delete_version(self, _button) -> None:
        if not self.repository:
            return
        selected = self.selected_history()
        if len(selected) != 1:
            self.show_error("请在版本历史中选择一个需要删除的版本。")
            return
        entry = selected[0]
        if entry.read_only:
            self.show_error("所选版本是继承的正式版本或属于其他分支，不能在当前分支删除。")
            return
        versions = [tag for tag in entry.tags if parse_version_tag(tag)]
        if len(versions) != 1:
            self.show_error("所选记录没有唯一的版本标签，无法删除。")
            return
        version = versions[0]
        label = display_version(version)

        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text=f"确定删除版本 {label}？",
        )
        dialog.format_secondary_text(
            "“仅删除版本”保留 Git 提交。\n"
            "“删除版本及提交”仅适用于 main 的最新版本；"
            "main 将退回上一提交，当前文件保留为未提交修改。\n"
            "已有暂存内容会保留；此时被删除提交的改动也会成为暂存中的修改。"
        )
        dialog.add_button("取消", Gtk.ResponseType.CANCEL)
        delete_tag_button = dialog.add_button("仅删除版本", DELETE_TAG_RESPONSE)
        delete_tag_button.get_style_context().add_class("destructive-action")
        delete_commit_button = dialog.add_button(
            "删除版本及提交", DELETE_COMMIT_RESPONSE
        )
        delete_commit_button.get_style_context().add_class("destructive-action")
        delete_commit_button.set_sensitive(self.repository.current_branch() == "main" and VERSION_PATTERN.fullmatch(version) is not None)
        response = dialog.run()
        dialog.destroy()
        if response == DELETE_TAG_RESPONSE:
            operation = lambda: self.repository.delete_version(version)
            busy = "正在删除版本..."
            success = f"版本 {label} 已删除，Git 提交已保留。"
        elif response == DELETE_COMMIT_RESPONSE:
            operation = lambda: self.repository.delete_latest_version_commit(version)
            busy = "正在删除版本及提交..."
            success = f"版本 {label} 及其 Git 提交已删除，当前文件和原有暂存内容已保留。"
        else:
            return
        self.run_background(busy, operation, success)

    def on_view_changelog(self, _button) -> None:
        if not self.repository:
            return
        dialog = Gtk.Dialog(title="版本管理 CHANGELOG", transient_for=self, modal=True)
        dialog.add_button("关闭", Gtk.ResponseType.CLOSE)
        dialog.set_default_size(760, 560)
        self._add_maximize_button(dialog)
        text = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        text.get_buffer().set_text(self.repository.changelog_text())
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(text)
        dialog.get_content_area().pack_start(scroll, True, True, 0)
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def on_download_changelog(self, _button) -> None:
        if not self.repository:
            return
        destination = self.repository.path / WORKSPACE_CHANGELOG
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            self.show_error("无法下载 CHANGELOG.rst：工作空间根目录同名路径必须是普通文件。")
            return
        if destination.exists():
            dialog = Gtk.MessageDialog(
                transient_for=self,
                modal=True,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.NONE,
                text="覆盖工作空间根目录的 CHANGELOG.rst？",
            )
            dialog.format_secondary_text(
                f"现有文件将被 Git 标签生成的 CHANGELOG 替换。\n{destination}"
            )
            dialog.add_button("取消", Gtk.ResponseType.CANCEL)
            overwrite_button = dialog.add_button("覆盖", Gtk.ResponseType.OK)
            overwrite_button.get_style_context().add_class("destructive-action")
            response = dialog.run()
            dialog.destroy()
            if response != Gtk.ResponseType.OK:
                return
        try:
            self.repository.download_changelog(destination)
            self.status_label.set_text(f"CHANGELOG 已下载到 {destination}")
        except (GitError, OSError) as error:
            self.show_error(str(error))

    def on_history_activated(self, _tree, path, _column) -> None:
        index = path.get_indices()[0]
        if not self.repository or index >= len(self.history_entries):
            return
        entry = self.history_entries[index]
        try:
            details = self.repository.commit_details(entry.reference)
        except GitError as error:
            self.show_error(str(error))
            return
        dialog = Gtk.Dialog(title="版本详情", transient_for=self, modal=True)
        dialog.add_button("关闭", Gtk.ResponseType.CLOSE)
        dialog.set_default_size(760, 520)
        text = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.NONE)
        text.get_buffer().set_text(details)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(text)
        dialog.get_content_area().pack_start(scroll, True, True, 0)
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def run_background(
        self, busy: str, operation, success: str, reload_workspace: bool = False,
        show_result: bool = False, remote_sync: bool = False, deletion_preview: bool = False,
    ) -> None:
        if self.busy:
            return
        self.busy = True
        self.branch_generation += 1
        self._invalidate_remote_status()
        self._set_remote_status(RemoteSyncStatus("checking", "操作进行中，完成后重新检查远程状态。"))
        self.header.set_sensitive(False)
        self.toolbar.set_sensitive(False)
        self.branch_bar.set_sensitive(False)
        self.merge_bar.set_sensitive(False)
        self.notebook.set_sensitive(False)
        self.info_action_button.set_sensitive(False)
        self.status_label.set_text(busy)
        started = time.monotonic()

        def update_elapsed() -> bool:
            if not self.busy:
                return False
            elapsed = int(time.monotonic() - started)
            self.status_label.set_text(f"{busy} · 已等待 {elapsed} 秒")
            return True

        self.background_timer = GLib.timeout_add_seconds(1, update_elapsed)

        def worker() -> None:
            try:
                result = operation()
                message = result if isinstance(result, str) and result else success
                if deletion_preview:
                    GLib.idle_add(self._background_deletion_ready, result)
                elif show_result:
                    GLib.idle_add(self._background_complete, message, reload_workspace, True)
                else:
                    GLib.idle_add(self._background_complete, message, reload_workspace)
            except Exception as error:  # GTK must receive subprocess errors on the main thread.
                if remote_sync:
                    GLib.idle_add(self._background_failed, str(error), True)
                else:
                    GLib.idle_add(self._background_failed, str(error))

        threading.Thread(target=worker, daemon=True).start()

    def _background_deletion_ready(self, plan: BranchDeletionPlan) -> bool:
        self._end_background()
        self.refresh()
        self._confirm_branch_deletion(plan)
        return False

    def _background_complete(
        self, message: str, reload_workspace: bool = False, show_result: bool = False
    ) -> bool:
        self._end_background()
        if reload_workspace:
            self.load_workspace(self.workspace_path)
        else:
            self.refresh()
        self.status_label.set_text(message)
        if "未提交修改和未跟踪文件保存在" in message and self.repository:
            dialog = Gtk.MessageDialog(
                transient_for=self, modal=True,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.NONE,
                text="本地已按远程状态同步",
            )
            dialog.format_secondary_text(
                message + "\n\n可以现在恢复同步前的未提交修改和未跟踪文件；"
                "恢复可能与当前远程文件产生冲突。"
            )
            dialog.add_button("稍后处理", Gtk.ResponseType.CANCEL)
            restore = dialog.add_button("恢复同步前修改", Gtk.ResponseType.OK)
            response = dialog.run()
            dialog.destroy()
            if response == Gtk.ResponseType.OK:
                try:
                    restored = self.repository.restore_local_sync_changes()
                    self.refresh()
                    self.status_label.set_text(restored)
                except GitError as error:
                    self.show_error(str(error))
            return False
        if show_result:
            limited = "未检查推送权限" in message
            dialog = Gtk.MessageDialog(
                transient_for=self, modal=True,
                message_type=Gtk.MessageType.WARNING if limited else Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.CLOSE,
                text="连接成功，推送尚未验证" if limited else "连接及推送预检查通过",
            )
            remote_url = self.repository.remote_url() if self.repository else ""
            dialog.format_secondary_text(f"仓库：{remote_url}\n\n{message}")
            dialog.run()
            dialog.destroy()
        return False

    def _background_failed(self, message: str, remote_sync: bool = False) -> bool:
        if remote_sync:
            self.remote_sync_error = message
        self._end_background()
        self.refresh()
        self.show_error(message)
        return False

    def _end_background(self) -> None:
        self.busy = False
        if self.background_timer is not None:
            GLib.source_remove(self.background_timer)
            self.background_timer = None
        self.header.set_sensitive(True)
        self.toolbar.set_sensitive(True)
        self.branch_bar.set_sensitive(True)
        self.merge_bar.set_sensitive(True)
        self.notebook.set_sensitive(True)
        self.info_action_button.set_sensitive(True)

    def show_error(self, message: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text="操作未完成",
        )
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()

    @staticmethod
    def _save_repository_path(path: Path) -> None:
        save_workspace_history(path)


class VersionManagerApplication(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self) -> None:
        window = self.props.active_window
        if not window:
            window = VersionManagerWindow(self, configured_repository())
        window.show_all()
        window.refresh()
        window.present()


def configured_repository() -> Path:
    environment_path = os.environ.get("CODE_VERSION_MANAGER_REPOSITORY")
    if environment_path:
        return Path(environment_path)
    config_file = settings_file_path()
    parser = _read_settings(config_file)
    if config_file.exists():
        saved = parser.get("repository", "path", fallback="")
        if saved and Path(saved).expanduser().is_dir():
            return Path(saved)
    return DEFAULT_REPOSITORY


def main() -> int:
    application = VersionManagerApplication()
    try:
        return application.run(sys.argv)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
