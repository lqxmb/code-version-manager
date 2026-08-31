#!/usr/bin/python3
import configparser
import html
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402


APP_ID = "cn.local.CodeVersionManager"
APP_NAME = "工作空间版本管理"
DEFAULT_REPOSITORY = Path.home() / "catkin_ws"
VERSION_PATTERN = re.compile(r"^v(\d+(?:\.\d+){2,})$")
METADATA_DIRECTORY = "code-version-manager"
MANIFEST_DIRECTORY = "versions"
MANIFEST_SUFFIX = ".manifest"
MANAGER_CHANGELOG = "CHANGELOG.rst"
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


class GitError(RuntimeError):
    pass


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
            self.path = top
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

    def _tags_by_commit(self) -> Dict[str, List[str]]:
        mapping: Dict[str, List[str]] = {}
        for tag in self.tags():
            commit = self.run(["rev-list", "-n", "1", tag]).strip()
            mapping.setdefault(commit, []).append(tag)
        return mapping

    def history(self, limit: int = 500) -> List[HistoryEntry]:
        tags = self._tags_by_commit()
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
            message = self.run(["show", "-s", "--format=%B", commit]).strip()
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
            raise GitError("当前正在查看历史版本。请先返回 main，再保存新版本。")
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
            raise GitError("Git 暂存区已有其他文件。请先处理这些暂存内容再保存版本。")
        scopes = validate_top_level_scopes(selected_scopes or [])
        if scopes:
            self.run(["add", "-u", "--", *scopes])
        self._add_paths(selected)
        self.metadata_path.mkdir(parents=True, exist_ok=True)
        self.run(["commit", "--allow-empty", "-m", commit_message])
        self.run(["tag", "-a", version, "-m", commit_message])
        self._write_manifest(version, selected)
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

    def materialize_current(self, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        for relative in self.candidate_files():
            source = self.path / relative
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                target.symlink_to(os.readlink(source))
            else:
                shutil.copy2(source, target)

    def compare(self, left: str, right: Optional[str] = None) -> None:
        if not shutil.which("bcompare"):
            raise GitError("没有找到 Beyond Compare。")
        with tempfile.TemporaryDirectory(prefix="code-version-manager-compare-") as temporary:
            root = Path(temporary)
            left_directory = root / reference_folder_name(left, "left")
            right_label = right or "current"
            right_directory = root / reference_folder_name(right_label, "right")
            self.materialize(left, left_directory)
            if right:
                self.materialize(right, right_directory)
            else:
                self.materialize_current(right_directory)
            config_home = prepare_bcompare_config(root)
            environment = os.environ.copy()
            environment["XDG_CONFIG_HOME"] = str(config_home)
            result = subprocess.run(
                [
                    "bcompare",
                    "-solo",
                    "-readonly",
                    "-filters=*",
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


def prepare_bcompare_config(temporary_root: Path) -> Path:
    config_home = temporary_root / "config"
    target = config_home / "bcompare"
    source_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    source = source_home / "bcompare"
    if source.is_dir():
        shutil.copytree(
            source,
            target,
            ignore=shutil.ignore_patterns("BCLOCK_*", "BcCrashReport-*.log"),
        )
    else:
        target.mkdir(parents=True, exist_ok=True)

    sessions_path = target / "BCSessions.xml"
    try:
        tree = ET.parse(sessions_path)
        root = tree.getroot()
    except (OSError, ET.ParseError):
        root = ET.Element("BCSessions", Version="1", MinVersion="1")
        session_folder = ET.SubElement(root, "TSessionFolder")
        items = ET.SubElement(session_folder, "Items")
        session = ET.SubElement(items, "TDirCompareSession")
        ET.SubElement(session, "Rules")
        tree = ET.ElementTree(root)

    rules_nodes = root.findall(".//TDirCompareSession/Rules")
    if not rules_nodes:
        session_folder = root.find("TSessionFolder")
        if session_folder is None:
            session_folder = ET.SubElement(root, "TSessionFolder")
        items = session_folder.find("Items")
        if items is None:
            items = ET.SubElement(session_folder, "Items")
        session = ET.SubElement(items, "TDirCompareSession")
        rules_nodes = [ET.SubElement(session, "Rules")]

    for rules in rules_nodes:
        expand_all = rules.find("ExpandAllOnLoad")
        if expand_all is None:
            expand_all = ET.SubElement(rules, "ExpandAllOnLoad")
        expand_all.set("Value", "False")
        expand_differences = rules.find("ExpandDiffsOnLoad")
        if expand_differences is not None:
            expand_differences.set("Value", "False")

    tree.write(sessions_path, encoding="UTF-8", xml_declaration=True)
    return config_home


def ensure_git_identity(repository: GitRepository) -> None:
    if not repository.run(["config", "user.name"], check=False).strip():
        repository.run(["config", "user.name", os.environ.get("USER", "Local User")])
    if not repository.run(["config", "user.email"], check=False).strip():
        user = os.environ.get("USER", "user")
        repository.run(["config", "user.email", f"{user}@localhost"])


def initialize_repository(
    path: Path, version: str, message: str, selected_files: Sequence[str]
) -> GitRepository:
    root = path.expanduser().resolve()
    selected = validate_selected_files(root, selected_files)
    if not selected:
        raise GitError("至少选择一个需要封装的文件。")
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

        self._build_header()
        self._build_content()
        self._apply_style()
        self.load_workspace(initial_path)

    def _build_header(self) -> None:
        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.props.title = APP_NAME
        self.set_titlebar(header)

        open_button = self._command_button(
            "打开工作空间", "folder-open-symbolic", self.on_open_workspace
        )
        header.pack_start(open_button)
        refresh_button = self._icon_button("view-refresh-symbolic", "刷新", self.on_refresh)
        header.pack_end(refresh_button)

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
        toolbar.get_style_context().add_class("command-bar")
        self.save_button = self._command_button("保存版本", "document-save-symbolic", self.on_save_version)
        self.compare_button = self._command_button("比较所选版本", "edit-copy-symbolic", self.on_compare_versions)
        self.current_button = self._command_button(
            "所选版本与当前", "document-open-recent-symbolic", self.on_compare_current
        )
        self.export_button = self._command_button("下载版本", "document-save-symbolic", self.on_download_version)
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
        self._save_repository_path(resolved)
        try:
            self.repository = GitRepository(resolved)
            self.workspace_path = self.repository.path
        except GitError:
            self.repository = None
        self.refresh()

    def refresh(self) -> None:
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
            self.compare_button.set_sensitive(False)
            self.current_button.set_sensitive(False)
            self.export_button.set_sensitive(False)
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
                    f"<b>当前正在查看历史版本 {label}</b>  保存版本已禁用，返回 main 不会强制覆盖文件。"
                )
                self.info_action_button.set_label("返回 main")
                self.info_bar.show_all()
                self.save_button.set_sensitive(False)
                branch_label = f"历史版本 {self.repository.head_label()}"
            self.repo_label.set_text(f"{self.repository.path}  ·  {branch_label}")
            self.storage_label.set_text(
                f"版本存储 {format_storage_size(self.repository.storage_size())}"
            )
            self.status_label.set_text("工作区干净" if not changes else f"{len(changes)} 项修改")
            has_versions = bool(self.history_entries)
            self.current_button.set_sensitive(has_versions)
            self.compare_button.set_sensitive(len(self.history_entries) >= 2)
            self.export_button.set_sensitive(has_versions)
            self.delete_button.set_sensitive(has_versions)
            self.changelog_button.set_sensitive(True)
            self.download_changelog_button.set_sensitive(True)
        except GitError as error:
            self.show_error(str(error))

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

    def on_refresh(self, _button) -> None:
        self.refresh()

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
            "正在保存版本...",
            lambda: self.repository.save_version(
                version, message, selected, selected_scopes=selected_entries
            ),
            f"版本 {version} 已保存。",
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
            f"首个版本 {version} 已保存。",
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
        dialog = Gtk.Dialog(title="版本信息", transient_for=self, modal=True)
        dialog.add_buttons("取消", Gtk.ResponseType.CANCEL, "继续", Gtk.ResponseType.OK)
        dialog.set_default_size(580, 340)
        grid = Gtk.Grid(column_spacing=12, row_spacing=10, margin=16)
        version_entry = Gtk.Entry(text=default_version)
        changes_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        changes_scroll = Gtk.ScrolledWindow()
        changes_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        changes_scroll.set_min_content_height(170)
        changes_scroll.add(changes_view)
        grid.attach(Gtk.Label(label="版本号", xalign=0), 0, 0, 1, 1)
        grid.attach(version_entry, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="改动内容（每行一条）", xalign=0, yalign=0), 0, 1, 1, 1)
        grid.attach(changes_scroll, 1, 1, 1, 1)
        dialog.get_content_area().add(grid)
        dialog.show_all()
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
        default_name = (entry.tags[0] if entry.tags else entry.short_commit) + ".zip"
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
            "该版本将从版本列表和 CHANGELOG 中移除。Git 提交和工作空间文件不会删除。"
        )
        dialog.add_button("取消", Gtk.ResponseType.CANCEL)
        delete_button = dialog.add_button("删除版本", Gtk.ResponseType.OK)
        delete_button.get_style_context().add_class("destructive-action")
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        self.run_background(
            "正在删除版本...",
            lambda: self.repository.delete_version(version),
            f"版本 {version} 已删除。",
        )

    def on_view_changelog(self, _button) -> None:
        if not self.repository:
            return
        dialog = Gtk.Dialog(title="版本管理 CHANGELOG", transient_for=self, modal=True)
        dialog.add_button("关闭", Gtk.ResponseType.CLOSE)
        dialog.set_default_size(760, 560)
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
            details = self.repository.commit_details(entry.commit)
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
        self, busy: str, operation, success: str, reload_workspace: bool = False
    ) -> None:
        self.status_label.set_text(busy)

        def worker() -> None:
            try:
                operation()
                GLib.idle_add(self._background_complete, success, reload_workspace)
            except Exception as error:  # GTK must receive subprocess errors on the main thread.
                GLib.idle_add(self._background_failed, str(error))

        threading.Thread(target=worker, daemon=True).start()

    def _background_complete(self, message: str, reload_workspace: bool = False) -> bool:
        if reload_workspace:
            self.load_workspace(self.workspace_path)
        else:
            self.refresh()
        self.status_label.set_text(message)
        return False

    def _background_failed(self, message: str) -> bool:
        self.refresh()
        self.show_error(message)
        return False

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
        config_dir = Path(GLib.get_user_config_dir()) / "code-version-manager"
        config_dir.mkdir(parents=True, exist_ok=True)
        parser = configparser.ConfigParser()
        parser["repository"] = {"path": str(path)}
        with (config_dir / "settings.ini").open("w", encoding="utf-8") as handle:
            parser.write(handle)


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
    config_file = Path(GLib.get_user_config_dir()) / "code-version-manager" / "settings.ini"
    parser = configparser.ConfigParser()
    if config_file.exists():
        parser.read(config_file, encoding="utf-8")
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
