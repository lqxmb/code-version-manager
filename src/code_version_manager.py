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
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlencode, urlsplit

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
METADATA_DIRECTORY = "code-version-manager"
MANIFEST_DIRECTORY = "versions"
MANIFEST_SUFFIX = ".manifest"
MANAGER_CHANGELOG = "CHANGELOG.rst"
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
# Ubuntu's bcompare launcher re-quotes arguments, so this must not end in a backslash.
# The order is the same as Beyond Compare's Name Filters view: include files,
# include folders, then the excluded file/folder masks.
BCOMPARE_FILTER = "*;*;-.git\\;-*.zip;-__pycache__/"

class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class RemoteSyncSnapshot:
    remote_url: str
    branch_ref: str
    refs: Tuple[Tuple[str, str], ...]


@dataclass(frozen=True)
class RemoteSyncStatus:
    state: str
    message: str
    can_sync: bool = False
    deletion_action: str = ""
    remote_refs: Tuple[Tuple[str, str], ...] = ()


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

    def run(self, args: Sequence[str], check: bool = True) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.path), *args],
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

    def configure_remote(
        self,
        remote_url: str,
        name: str,
        email: str,
        push_timeout: Optional[int] = None,
    ) -> None:
        remote_url = validate_remote_url(remote_url)
        validate_git_identity(name, email)
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

    def _run_remote(self, args: Sequence[str], remote_url: str) -> str:
        remote_url = validate_remote_url(remote_url)
        checking_push = bool(args and args[0] == "push" and "--dry-run" in args)
        pushing = bool(args and args[0] == "push" and not checking_push)
        timeout = self.push_timeout_seconds() if pushing else REMOTE_TIMEOUT
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
            phase = "推送分支与版本标签" if pushing else "连接或读取远程仓库"
            if checking_push:
                phase = "预检查推送权限（不上传版本）"
            message = (
                f"远程操作超时：{phase}超过 {timeout} 秒。\n"
                "本地提交和版本标签均已保留，不要重新发布同一版本。\n"
                "超时不能确认远程是否已经接收更新；网络恢复后点击“同步远程”可安全补齐。\n"
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
        remote_url = self._sync_target()
        self._run_remote(["ls-remote", "--", remote_url], remote_url)
        branch = self.current_branch()
        if not branch:
            return "远程仓库可读取；当前正在查看历史版本，未检查推送权限。请返回工作分支再验证。"
        branch_ref = f"refs/heads/{branch}"
        if not self.run(["rev-parse", "--verify", branch_ref], check=False).strip():
            return "远程仓库可读取；尚无本地提交，未检查推送权限。首次同步时仍需验证登录与写入权限。"
        snapshot = self.sync_snapshot(require_versions=False)
        self._run_remote(self._push_arguments(snapshot, dry_run=True), remote_url)
        return "连接及推送预检查通过，未上传版本；正式同步仍需满足分支规则和服务器检查。"

    def _sync_target(self) -> str:
        urls = self.run(
            ["remote", "get-url", "--push", "--all", "origin"], check=False
        ).splitlines()
        if len(urls) != 1:
            raise GitError("请先通过“Git 账号”配置唯一的 origin 远程仓库地址。")
        return validate_remote_url(urls[0])

    def sync_snapshot(self, require_versions: bool = True) -> RemoteSyncSnapshot:
        branch = self.current_branch()
        if not branch:
            raise GitError("查看历史版本时不能同步远程，请先返回工作分支。")
        remote_url = self._sync_target()
        branch_ref = f"refs/heads/{branch}"
        commit = self.run(["rev-parse", "--verify", branch_ref], check=False).strip()
        if not commit:
            raise GitError("当前分支尚无可同步的版本，请先发布版本。")
        tag_refs = []
        for line in self.run([
            "for-each-ref", f"--merged={branch_ref}", "--format=%(refname) %(objectname)", "refs/tags/",
        ]).splitlines():
            ref, object_id = line.split()
            if VERSION_PATTERN.fullmatch(ref[len("refs/tags/"):]):
                tag_refs.append((ref, object_id))
        if require_versions and not tag_refs:
            raise GitError("当前分支尚无可同步的版本，请先发布版本。")
        return RemoteSyncSnapshot(remote_url, branch_ref, ((branch_ref, commit), *tag_refs))

    @staticmethod
    def _push_arguments(snapshot: RemoteSyncSnapshot, dry_run: bool = False) -> List[str]:
        options = ["--dry-run", "--no-verify"] if dry_run else ["--progress", "--set-upstream"]
        return [
            "push", "--atomic", "--porcelain", "--no-follow-tags", *options,
            "origin", *[f"{ref}:{ref}" for ref, _object_id in snapshot.refs],
        ]

    def remote_sync_status(self, snapshot: RemoteSyncSnapshot) -> RemoteSyncStatus:
        try:
            output = self._run_remote(["ls-remote", "--", snapshot.remote_url], snapshot.remote_url)
        except GitError as error:
            return RemoteSyncStatus("unavailable", f"无法检查远程状态：{error}\n修复后点击“刷新”重新检查。")
        remote_refs = {}
        for line in output.splitlines():
            fields = line.split()
            if len(fields) == 2:
                object_id, ref = fields
                remote_refs[ref] = object_id
        local_refs = dict(snapshot.refs)
        changed = [ref for ref, object_id in snapshot.refs if remote_refs.get(ref) != object_id]
        remote_version_tags = []
        # Detect removed local tags retained on the remote, including a latest tag whose commit was rewound locally.
        extra = []
        for ref, object_id in remote_refs.items():
            if not ref.startswith("refs/tags/") or ref in local_refs:
                continue
            if not VERSION_PATTERN.fullmatch(ref[len("refs/tags/"):]):
                continue
            target = remote_refs.get(ref + "^{}", object_id)
            remote_version_tags.append((ref, object_id, target))
            ancestor = self.run(
                ["merge-base", target, local_refs[snapshot.branch_ref]], check=False
            ).strip()
            if ancestor == target:
                extra.append(ref[len("refs/tags/"):])
        remote_branch = remote_refs.get(snapshot.branch_ref)
        local_branch = local_refs[snapshot.branch_ref]
        branch_is_one_commit_ahead = False
        if remote_branch and remote_branch != local_branch:
            base = self.run(["merge-base", local_branch, remote_branch], check=False).strip()
            count = self.run(
                ["rev-list", "--count", f"{local_branch}..{remote_branch}"], check=False
            ).strip()
            branch_is_one_commit_ahead = base == local_branch and count == "1"
        latest_tag_refs = []
        if branch_is_one_commit_ahead:
            latest_tag_refs = [
                (ref, object_id) for ref, object_id, target in remote_version_tags
                if target == remote_branch
            ]
        if branch_is_one_commit_ahead and latest_tag_refs:
            versions = "、".join(ref[len("refs/tags/"):] for ref, _oid in latest_tag_refs)
            return self._preflight_remote_deletion(
                snapshot,
                RemoteSyncStatus(
                    "pending",
                    f"本地已删除最新版本 {versions} 及其提交；远程仍指向该版本。"
                    "确认后可删除远程版本标签并将远程分支回退一个提交。",
                    True,
                    "rewind_latest",
                    ((snapshot.branch_ref, remote_branch), *latest_tag_refs),
                ),
            )
        if extra:
            extra_refs = tuple(("refs/tags/" + tag, remote_refs["refs/tags/" + tag]) for tag in extra)
            return self._preflight_remote_deletion(
                snapshot,
                RemoteSyncStatus(
                    "pending", "本地与远程不一致，本地已删除的当前分支版本标签仍保留在远程："
                    + "、".join(extra) + "。确认后可只删除远程版本标签，保留提交历史。",
                    True,
                    "delete_tags",
                    extra_refs,
                ),
            )
        if not changed:
            return RemoteSyncStatus("synced", "当前分支和版本标签与远程一致，无需同步。")
        try:
            self.push_timeout_seconds()
            self._run_remote(self._push_arguments(snapshot, dry_run=True), snapshot.remote_url)
        except GitError as error:
            return RemoteSyncStatus(
                "pending", f"本地与远程存在差异，当前无法同步：{error}\n修复后点击“刷新”重新检查。",
            )
        return RemoteSyncStatus(
            "pending", f"当前分支或版本标签有 {len(changed)} 项待同步，推送预检查已通过。点击上传本地版本。", True,
        )

    def _deletion_push_arguments(
        self, snapshot: RemoteSyncSnapshot, status: RemoteSyncStatus, dry_run: bool = False,
    ) -> List[str]:
        arguments = ["push", "--atomic", "--porcelain"]
        arguments.extend(["--dry-run", "--no-verify"] if dry_run else ["--progress"])
        refspecs = []
        if status.deletion_action == "rewind_latest":
            branch_ref, expected_branch = status.remote_refs[0]
            arguments.append(f"--force-with-lease={branch_ref}:{expected_branch}")
            refspecs.append(f"{snapshot.branch_ref}:{branch_ref}")
            tag_refs = status.remote_refs[1:]
        elif status.deletion_action == "delete_tags":
            tag_refs = status.remote_refs
        else:
            raise GitError("没有可同步的版本删除，请点击“刷新”重新检查远程状态。")
        for tag_ref, expected_tag in tag_refs:
            arguments.append(f"--force-with-lease={tag_ref}:{expected_tag}")
            refspecs.append(f":{tag_ref}")
        if not refspecs:
            raise GitError("远程删除内容为空，请点击“刷新”重新检查远程状态。")
        return [*arguments, "origin", *refspecs]

    def _preflight_remote_deletion(
        self, snapshot: RemoteSyncSnapshot, status: RemoteSyncStatus,
    ) -> RemoteSyncStatus:
        try:
            self.push_timeout_seconds()
            self._run_remote(
                self._deletion_push_arguments(snapshot, status, dry_run=True), snapshot.remote_url
            )
        except GitError as error:
            return RemoteSyncStatus(
                "pending", f"本地与远程存在已删除版本差异，但远程删除预检查失败：{error}",
                False, status.deletion_action, status.remote_refs,
            )
        return status

    def sync_remote(self) -> None:
        snapshot = self.sync_snapshot()
        self.push_timeout_seconds()
        self._run_remote(self._push_arguments(snapshot), snapshot.remote_url)

    def sync_remote_deletions(
        self, snapshot: RemoteSyncSnapshot, status: RemoteSyncStatus,
    ) -> None:
        if status.deletion_action not in ("delete_tags", "rewind_latest") or not status.remote_refs:
            raise GitError("没有可同步的版本删除，请点击“刷新”重新检查远程状态。")
        if self.sync_snapshot() != snapshot:
            raise GitError("本地版本状态已变化，请点击“刷新”重新检查后再处理远程差异。")
        if status.deletion_action == "rewind_latest" and len(status.remote_refs) < 2:
            raise GitError("无法确认远程最新版本引用，请点击“刷新”重新检查。")
        self.push_timeout_seconds()
        self._run_remote(self._deletion_push_arguments(snapshot, status), snapshot.remote_url)

    def current_branch(self) -> Optional[str]:
        branch = self.run(["symbolic-ref", "--quiet", "--short", "HEAD"], check=False).strip()
        return branch or None

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
        return sorted(set(paths))

    def tags(self) -> List[str]:
        output = self.run(["tag", "--sort=version:refname"])
        return [line for line in output.splitlines() if line]

    def version_message(self, version: str) -> str:
        if not VERSION_PATTERN.match(version) or version not in self.tags():
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

    def history(self, limit: int = 500) -> List[HistoryEntry]:
        tags = self._tags_by_commit()
        if not tags and not self.run(["rev-parse", "--verify", "HEAD"], check=False).strip():
            return []
        output = self.run(
            [
                "log",
                "--all",
                f"--max-count={limit}",
                "--date=short",
                "--pretty=format:%H%x1f%h%x1f%ad%x1f%s%x1e",
            ]
        )
        entries: List[HistoryEntry] = []
        for record in output.split("\x1e"):
            record = record.strip()
            if not record:
                continue
            fields = record.split("\x1f")
            if len(fields) != 4:
                continue
            commit, short_commit, date, subject = fields
            commit_tags = tuple(
                tag for tag in tags.get(commit, []) if VERSION_PATTERN.match(tag)
            )
            if not commit_tags:
                continue
            primary_tag = max(commit_tags, key=version_key)
            changes = normalize_changes(self._version_message_unchecked(primary_tag))
            if changes:
                subject = changes[0]
            entries.append(
                HistoryEntry(commit, short_commit, date, subject, commit_tags)
            )
        return sorted(
            entries,
            key=lambda entry: max(version_key(tag) for tag in entry.tags),
            reverse=True,
        )

    def next_version(self) -> str:
        versions = []
        for tag in self.tags():
            match = VERSION_PATTERN.match(tag)
            if match:
                versions.append(tuple(int(value) for value in match.group(1).split(".")))
        if not versions:
            return "v0.0.1"
        latest = max(versions)
        next_version = (*latest[:-1], latest[-1] + 1)
        return "v" + ".".join(str(value) for value in next_version)

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
        if reference in self.tags():
            return reference
        tags = self.run(["tag", "--points-at", reference], check=False).splitlines()
        return sorted(tags, key=version_key)[-1] if tags else None

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
        for tag in sorted(self.tags(), key=version_key, reverse=True):
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
            display_version = version[1:] if version.startswith("v") else version
            heading = f"{display_version} ({version_date})"
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

    def _add_paths(self, paths: Sequence[str]) -> None:
        for start in range(0, len(paths), 200):
            self.run(["add", "--", *paths[start : start + 200]])

    def filter_ignored(self, paths: Sequence[str]) -> List[str]:
        if not paths:
            return []
        payload = "\0".join(paths) + "\0"
        result = subprocess.run(
            ["git", "-C", str(self.path), "check-ignore", "--stdin", "-z"],
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
        return [path for path in paths if path not in ignored]

    def save_version(
        self,
        version: str,
        message: str,
        selected_files: Sequence[str],
        selected_scopes: Optional[Sequence[str]] = None,
    ) -> None:
        if self.current_branch() is None:
            raise GitError("当前正在查看历史版本。请先返回 main，再发布新版本。")
        if not VERSION_PATTERN.match(version):
            raise GitError("版本号应以 v 开头并至少包含三段数字，例如 v1.7.8.9.7。")
        if version in self.tags():
            raise GitError(f"版本 {version} 已存在。")
        ensure_git_identity(self)
        changes = normalize_changes(message)
        if not changes:
            raise GitError("至少填写一条改动内容。")
        commit_message = changes[0]
        if len(changes) > 1:
            commit_message += "\n\n" + "\n".join(changes[1:])
        selected = validate_selected_files(self.path, selected_files)
        selected = self.filter_ignored(selected)
        if not selected:
            raise GitError("所选文件全部被 .gitignore 排除，无法创建版本。")
        staged = self.run(["diff", "--cached", "--name-only"]).strip()
        if staged:
            raise GitError("Git 暂存区已有其他文件。请先处理这些暂存内容再发布版本。")
        scopes = validate_top_level_scopes(selected_scopes or [])
        if scopes:
            self.run(["add", "-u", "--", *scopes])
        self._add_paths(selected)
        self.metadata_path.mkdir(parents=True, exist_ok=True)
        self.run(["commit", "--allow-empty", "-m", commit_message])
        self.run(["tag", "-a", version, "-m", commit_message])
        self._write_manifest(version, selected)
        self._write_changelog(None)

    def update_version_message(self, version: str, message: str) -> None:
        if not VERSION_PATTERN.match(version) or version not in self.tags():
            raise GitError(f"版本 {version} 不存在。")
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
        branches = self.run(["for-each-ref", "--format=%(refname:short)", "refs/heads/"]).splitlines()
        if "main" not in branches:
            raise GitError("该仓库没有 main 分支。")
        self.run(["switch", "main"])

    def delete_version(self, version: str) -> None:
        if not VERSION_PATTERN.match(version):
            raise GitError("只能删除以 v 开头且至少包含三段数字的版本标签。")
        if version not in self.tags():
            raise GitError(f"版本 {version} 不存在或已经删除。")
        self.run(["tag", "--delete", version])
        self._remove_version_metadata(version)

    def delete_latest_version_commit(self, version: str) -> None:
        if not VERSION_PATTERN.match(version) or version not in self.tags():
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
        if self.run(["diff", "--cached", "--name-only"]).strip():
            raise GitError("暂存区已有文件。请先处理暂存内容再删除提交。")
        self.run(["reset", "--mixed", parent])
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
        args = ["archive", "--format=tar", reference]
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
        if not shutil.which("bcompare"):
            raise GitError("没有找到 Beyond Compare。")
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
            config_home = prepare_bcompare_config(root)
            environment = os.environ.copy()
            environment["XDG_CONFIG_HOME"] = str(config_home)
            result = subprocess.run(
                [
                    "bcompare",
                    "-solo",
                    read_only_option,
                    f"-filters={BCOMPARE_FILTER}",
                    str(left_directory),
                    str(right_directory),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            if result.returncode not in (0, 1):
                raise GitError(result.stderr.strip() or "Beyond Compare 启动失败。")

    def export(self, reference: str, destination: Path) -> None:
        paths = self.manifest(reference)
        args = ["archive", "--format=zip", f"--output={destination}", reference]
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


def top_level_package_groups(path: Path) -> Dict[str, List[str]]:
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
    return groups


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
    path: Path, version: str, message: str, selected_files: Sequence[str]
) -> GitRepository:
    selected = validate_selected_files(path.expanduser().resolve(), selected_files)
    if not selected:
        raise GitError("至少选择一个需要封装的文件。")
    repository = create_repository(path)
    ensure_git_identity(repository)
    repository.save_version(version, message, selected)
    return repository


class FileSelectionDialog(Gtk.Dialog):
    def __init__(
        self,
        parent: Gtk.Window,
        files: Sequence[str],
        selected_files: Sequence[str],
        changed_files: Sequence[str],
        title: str = "选择版本封装文件",
    ):
        super().__init__(title=title, transient_for=parent, modal=True)
        self.set_default_size(820, 620)
        self.add_buttons("取消", Gtk.ResponseType.CANCEL, "继续", Gtk.ResponseType.OK)
        selected = set(selected_files)
        changed = set(changed_files)

        content = self.get_content_area()
        content.set_spacing(8)
        content.set_border_width(12)

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
            self.store.append(
                [path in selected, "已修改" if is_changed else item_type, path, is_changed]
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
        self.connect("destroy", lambda _window: self._invalidate_remote_status())

        self._build_header()
        self._build_content()
        self._apply_style()
        self.load_workspace(initial_path)

    def _build_header(self) -> None:
        header = Gtk.HeaderBar()
        self.header = header
        header.set_show_close_button(True)
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

        self.git_account_button = self._command_button(
            "Git 账号", "avatar-default-symbolic", self.on_git_account
        )
        header.pack_end(self.git_account_button)
        self.sync_button = self._command_button(
            "同步远程", "emblem-synchronizing-symbolic", self.on_sync_remote
        )
        self.sync_button.set_sensitive(False)
        header.pack_end(self.sync_button)

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
        header.set_title(dialog.get_title())
        header.set_show_close_button(True)
        button = Gtk.Button.new_from_icon_name(
            "view-fullscreen-symbolic", Gtk.IconSize.BUTTON
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
        icon = "view-restore-symbolic" if maximized else "view-fullscreen-symbolic"
        tooltip = "还原窗口" if maximized else "最大化窗口"
        button.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))
        button.set_tooltip_text(tooltip)
        return False

    def _build_content(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(root)

        self.info_bar = Gtk.InfoBar()
        self.info_bar.set_message_type(Gtk.MessageType.WARNING)
        self.info_label = Gtk.Label(xalign=0)
        self.info_label.set_use_markup(True)
        self.info_bar.get_content_area().add(self.info_label)
        self.info_action_button = self.info_bar.add_button("返回 main", Gtk.ResponseType.OK)
        self.info_bar.connect("response", self.on_return_main)
        root.pack_start(self.info_bar, False, False, 0)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.toolbar = toolbar
        toolbar.get_style_context().add_class("command-bar")
        self.save_button = self._command_button("发布版本", "document-save-symbolic", self.on_save_version)
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
        self.changelog_button = self._command_button(
            "查看 CHANGELOG", "document-properties-symbolic", self.on_view_changelog
        )
        self.download_changelog_button = self._command_button(
            "下载 CHANGELOG", "document-save-symbolic", self.on_download_changelog
        )
        for button in (
            self.save_button,
            self.compare_button,
            self.current_button,
            self.export_button,
            self.edit_message_button,
            self.delete_button,
            self.changelog_button,
            self.download_changelog_button,
        ):
            toolbar.pack_start(button, False, False, 0)
        root.pack_start(toolbar, False, False, 0)

        self.notebook = Gtk.Notebook()
        self.notebook.append_page(self._build_history_view(), Gtk.Label(label="版本历史"))
        self.notebook.append_page(self._build_changes_view(), Gtk.Label(label="当前修改"))
        root.pack_start(self.notebook, True, True, 0)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        footer.get_style_context().add_class("footer")
        self.repo_label = Gtk.Label(xalign=0, ellipsize=3)
        self.storage_label = Gtk.Label(xalign=1)
        self.storage_label.set_tooltip_text(
            "当前工作空间 Git 增量版本历史实际占用的磁盘空间，不包含工作空间源文件"
        )
        self.status_label = Gtk.Label(xalign=1)
        footer.pack_start(self.repo_label, True, True, 0)
        footer.pack_end(self.status_label, False, False, 0)
        footer.pack_end(self.storage_label, False, False, 0)
        root.pack_end(footer, False, False, 0)

    def _build_history_view(self) -> Gtk.Widget:
        self.history_store = Gtk.ListStore(str, str, str, str, str)
        self.history_tree = Gtk.TreeView(model=self.history_store)
        self.history_tree.set_headers_clickable(True)
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
        .command-bar { padding: 8px 10px; border-bottom: 1px solid alpha(@borders, 0.8); }
        .command-bar button { min-height: 30px; padding: 2px 10px; }
        .footer { padding: 7px 12px; border-top: 1px solid alpha(@borders, 0.8); }
        treeview { padding: 2px; }
        button.remote-synced, button.remote-synced:disabled {
            background-image: none; background-color: #278044; color: white;
            border-color: #206b39; text-shadow: none; box-shadow: none; opacity: 1;
        }
        button.remote-pending, button.remote-pending:disabled {
            background-image: none; background-color: #bb3030; color: white;
            border-color: #982727; text-shadow: none; box-shadow: none; opacity: 1;
        }
        button.remote-pending:hover { background-color: #a32929; }
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
        self._invalidate_remote_status()
        try:
            self.repository = GitRepository(resolved)
            self.workspace_path = self.repository.path
        except GitError:
            self.repository = None
        self._save_repository_path(self.workspace_path)
        self.refresh()

    def refresh(self, check_remote: bool = True) -> None:
        if not self.repository:
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
            changes = self.repository.status()
            self.history_entries = self.repository.history()
            self.history_store.clear()
            for entry in self.history_entries:
                version = ", ".join(entry.tags)
                self.history_store.append(
                    [version, entry.date, entry.subject, entry.short_commit, entry.commit]
                )
            self.change_store.clear()
            for change in changes:
                self.change_store.append([change.status, change.path])

            branch = self.repository.current_branch()
            if branch:
                self.info_mode = ""
                self.info_bar.hide()
                self.save_button.set_sensitive(True)
                branch_label = branch
            else:
                self.info_mode = "detached"
                label = html.escape(self.repository.head_label())
                self.info_label.set_markup(
                    f"<b>当前正在查看历史版本 {label}</b>  发布版本已禁用，返回 main 不会强制覆盖文件。"
                )
                self.info_action_button.set_label("返回 main")
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
        context = self.sync_button.get_style_context()
        context.remove_class("remote-synced")
        context.remove_class("remote-pending")
        if status.state == "synced":
            label, icon = "远程已同步", "emblem-ok-symbolic"
            context.add_class("remote-synced")
        elif status.state == "pending":
            if status.can_sync and status.deletion_action == "delete_tags":
                label = "同步删除到远程"
            elif status.can_sync and status.deletion_action == "rewind_latest":
                label = "同步删除并回退远程"
            else:
                label = "同步远程（待同步）" if status.can_sync else "远程有差异（不可同步）"
            icon = "document-send-symbolic" if status.can_sync else "dialog-warning-symbolic"
            context.add_class("remote-pending")
        elif status.state == "checking":
            label, icon = "正在检查远程…", "emblem-synchronizing-symbolic"
        else:
            label, icon = "同步远程（不可用）", "network-offline-symbolic"
        self.sync_button.set_label(label)
        self.sync_button.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))
        self.sync_button.set_tooltip_text(status.message)
        self.sync_button.set_sensitive(status.can_sync and not self.busy)

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
        if status.can_sync and self.remote_sync_error:
            status = RemoteSyncStatus(
                "pending", "上次上传失败，当前仍有待同步版本：\n" + self.remote_sync_error
                + "\n修复后点击“刷新”重新检查，再尝试同步。",
                True, status.deletion_action, status.remote_refs,
            )
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
            popover.remove(child)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        content.set_border_width(6)
        paths = recent_workspaces()
        if not paths:
            empty = Gtk.Label(label="暂无最近工作空间", xalign=0)
            empty.set_sensitive(False)
            content.pack_start(empty, False, False, 8)
        else:
            for path in paths:
                label = f"当前  ·  {path}" if path == self.workspace_path else str(path)
                # GtkModelButton always interprets underscores as mnemonic markers.
                button = Gtk.ModelButton(text=label.replace("_", "__"))
                button.set_tooltip_text(str(path))
                button.connect("clicked", self.on_open_recent_workspace, path)
                content.pack_start(button, False, False, 0)

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

    def on_about(self, _button) -> None:
        dialog = Gtk.AboutDialog(transient_for=self, modal=True)
        dialog.set_program_name("Code Version Manager")
        dialog.set_version(APP_VERSION)
        dialog.set_comments("Git 工作空间版本管理器，支持本地版本与远程同步")
        dialog.set_logo_icon_name("code-version-manager")
        dialog.run()
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
            label="默认 15 分钟，范围 1–60 分钟，仅用于实际上传。连接和推送预检查的时限为 120 秒。",
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
            "发布版本只保存到本地。实际上传请单独点击主窗口的红色“同步远程”按钮；"
            "绿色表示已一致，禁用表示当前无需同步或无法同步。",
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


    def on_sync_remote(self, _button) -> None:
        repository = self.repository
        if repository and not self.busy and self.remote_status.can_sync:
            try:
                snapshot = repository.sync_snapshot()
            except GitError as error:
                self._invalidate_remote_status()
                self._set_remote_status(RemoteSyncStatus("unavailable", str(error)))
                return
            if snapshot != self.remote_status_snapshot:
                self._refresh_remote_status()
                return
            status = self.remote_status
            if status.deletion_action:
                if status.deletion_action == "rewind_latest":
                    title = "确认同步删除并回退远程分支？"
                    action = "远程分支将回退一个已同步提交，并删除对应的远程版本标签。"
                    button_label = "删除远程版本并回退分支"
                else:
                    versions = "、".join(
                        ref[len("refs/tags/"):] for ref, _oid in status.remote_refs
                    )
                    title = "确认删除远程版本标签？"
                    action = f"将删除远程标签 {versions}，保留对应提交历史和分支。"
                    button_label = "删除远程标签"
                confirmation = Gtk.MessageDialog(
                    transient_for=self, modal=True,
                    message_type=Gtk.MessageType.WARNING,
                    buttons=Gtk.ButtonsType.NONE, text=title,
                )
                confirmation.format_secondary_text(
                    action + "\n\n此操作会影响所有使用该远程仓库的协作者。"
                    "只有远程引用仍与本次检查结果一致时才会执行。"
                )
                confirmation.add_button("取消", Gtk.ResponseType.CANCEL)
                destructive = confirmation.add_button(button_label, Gtk.ResponseType.OK)
                destructive.get_style_context().add_class("destructive-action")
                confirmation.set_default_response(Gtk.ResponseType.CANCEL)
                response = confirmation.run()
                confirmation.destroy()
                if response != Gtk.ResponseType.OK:
                    return
                self.remote_sync_error = None
                self.run_background(
                    "正在同步版本删除到远程...",
                    lambda: repository.sync_remote_deletions(snapshot, status),
                    "远程版本删除已同步。",
                    remote_sync=True,
                )
                return
            self.remote_sync_error = None
            self.run_background(
                "正在同步远程仓库...", repository.sync_remote,
                "当前分支和版本标签已同步到远程仓库。", remote_sync=True,
            )

    def on_return_main(self, _bar, response) -> None:
        if response != Gtk.ResponseType.OK:
            return
        if self.info_mode == "unversioned":
            self.on_enable_versioning()
            return
        if not self.repository:
            return
        try:
            self.repository.switch_main()
            self.refresh()
        except GitError as error:
            self.show_error("Git 为保护当前文件而停止切换：\n\n" + str(error))

    def on_save_version(self, _button) -> None:
        if not self.repository:
            return
        repository = self.repository
        metadata = self.prompt_version_metadata(self.repository.next_version())
        if not metadata:
            return
        version, message = metadata
        groups = top_level_package_groups(self.workspace_path)
        entries = list(groups)
        if not entries:
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
        )
        if selected_entries is None:
            return
        selected = expand_package_groups(groups, selected_entries)
        self.run_background(
            "正在发布版本...",
            lambda: repository.save_version(
                version, message, selected, selected_scopes=selected_entries
            ),
            f"版本 {version} 已发布到本地。需要上传时请单独点击“同步远程”。",
        )

    def on_enable_versioning(self) -> None:
        groups = top_level_package_groups(self.workspace_path)
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
        version, message = metadata

        def initialize() -> None:
            initialize_repository(self.workspace_path, version, message, selected)

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
    ) -> Optional[List[str]]:
        dialog = FileSelectionDialog(self, files, selected_files, changed_files, title=title)
        dialog.show_all()
        response = dialog.run()
        selected = dialog.selected_files()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return None
        if not selected:
            self.show_error("至少选择一个需要封装的文件。")
            return None
        return selected

    def prompt_version_metadata(self, default_version: str):
        dialog = Gtk.Dialog(title="发布版本", transient_for=self, modal=True)
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
        content.pack_start(version_row, False, False, 0)
        content.pack_start(
            Gtk.Label(label="改动内容（每行一条）", xalign=0), False, False, 0
        )
        content.pack_start(changes_scroll, True, True, 0)
        dialog.get_content_area().pack_start(content, True, True, 0)
        dialog.show_all()
        version_entry.grab_focus()
        version_entry.select_region(0, -1)
        response = dialog.run()
        buffer = changes_view.get_buffer()
        message = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True).strip()
        result = (version_entry.get_text().strip(), message)
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
        versions = [tag for tag in selected[0].tags if VERSION_PATTERN.match(tag)]
        if len(versions) != 1:
            self.show_error("所选记录没有唯一的版本标签，无法修改说明。")
            return
        version = versions[0]
        try:
            current_message = self.repository.version_message(version)
        except GitError as error:
            self.show_error(str(error))
            return
        message = self.prompt_version_message(version, current_message)
        if message is None:
            return
        self.run_background(
            "正在更新修改说明...",
            lambda: self.repository.update_version_message(version, message),
            f"版本 {version} 的修改说明已更新。",
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
            "Beyond Compare 已打开...",
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
            "Beyond Compare 已打开...",
            lambda: self.repository.compare(entry.reference),
            "版本与当前工作空间的比较已关闭。",
        )

    def on_download_version(self, _button) -> None:
        selected = self.selected_history()
        if len(selected) != 1:
            self.show_error("请在版本历史中选择一个需要下载的版本。")
            return
        entry = selected[0]
        version_name = entry.tags[0] if entry.tags else entry.short_commit
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
        versions = [tag for tag in entry.tags if VERSION_PATTERN.match(tag)]
        if len(versions) != 1:
            self.show_error("所选记录没有唯一的版本标签，无法删除。")
            return
        version = versions[0]

        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text=f"确定删除版本 {version}？",
        )
        dialog.format_secondary_text(
            "“仅删除版本”保留 Git 提交。\n"
            "“删除版本及提交”仅适用于 main 的最新版本；"
            "main 将退回上一提交，当前文件保留为未提交修改。"
        )
        dialog.add_button("取消", Gtk.ResponseType.CANCEL)
        delete_tag_button = dialog.add_button("仅删除版本", DELETE_TAG_RESPONSE)
        delete_tag_button.get_style_context().add_class("destructive-action")
        delete_commit_button = dialog.add_button(
            "删除版本及提交", DELETE_COMMIT_RESPONSE
        )
        delete_commit_button.get_style_context().add_class("destructive-action")
        response = dialog.run()
        dialog.destroy()
        if response == DELETE_TAG_RESPONSE:
            operation = lambda: self.repository.delete_version(version)
            busy = "正在删除版本..."
            success = f"版本 {version} 已删除，Git 提交已保留。"
        elif response == DELETE_COMMIT_RESPONSE:
            operation = lambda: self.repository.delete_latest_version_commit(version)
            busy = "正在删除版本及提交..."
            success = f"版本 {version} 及其 Git 提交已删除。"
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
        dialog = Gtk.FileChooserDialog(
            title="下载版本管理 CHANGELOG",
            parent=self,
            action=Gtk.FileChooserAction.SAVE,
        )
        dialog.add_buttons("取消", Gtk.ResponseType.CANCEL, "下载", Gtk.ResponseType.OK)
        dialog.set_current_name("CHANGELOG.rst")
        dialog.set_do_overwrite_confirmation(True)
        response = dialog.run()
        filename = dialog.get_filename()
        dialog.destroy()
        if response == Gtk.ResponseType.OK and filename:
            try:
                self.repository.download_changelog(Path(filename))
                self.status_label.set_text(f"CHANGELOG 已下载到 {filename}")
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
        show_result: bool = False, remote_sync: bool = False,
    ) -> None:
        if self.busy:
            return
        self.busy = True
        self._invalidate_remote_status()
        self._set_remote_status(RemoteSyncStatus("checking", "操作进行中，完成后重新检查远程状态。"))
        self.header.set_sensitive(False)
        self.toolbar.set_sensitive(False)
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
                if show_result:
                    GLib.idle_add(self._background_complete, message, reload_workspace, True)
                else:
                    GLib.idle_add(self._background_complete, message, reload_workspace)
            except Exception as error:  # GTK must receive subprocess errors on the main thread.
                if remote_sync:
                    GLib.idle_add(self._background_failed, str(error), True)
                else:
                    GLib.idle_add(self._background_failed, str(error))

        threading.Thread(target=worker, daemon=True).start()

    def _background_complete(
        self, message: str, reload_workspace: bool = False, show_result: bool = False
    ) -> bool:
        self._end_background()
        if reload_workspace:
            self.load_workspace(self.workspace_path)
        else:
            self.refresh()
        self.status_label.set_text(message)
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
