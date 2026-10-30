#!/usr/bin/python3
import importlib.util
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest import mock


SOURCE = Path(__file__).parents[1] / "src" / "code_version_manager.py"
SPEC = importlib.util.spec_from_file_location("code_version_manager", SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class GitRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Test User")
        self.git("config", "user.email", "test@example.com")
        (self.path / "robot.txt").write_text("one\n", encoding="utf-8")
        (self.path / "other.txt").write_text("other\n", encoding="utf-8")
        self.git("add", "robot.txt", "other.txt")
        self.git("commit", "-q", "-m", "initial")
        self.git("tag", "v0.0.1")
        self.repository = MODULE.GitRepository(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.path), *args], check=True)

    def test_history_status_and_save(self):
        self.assertEqual("v0.0.2", self.repository.next_version())
        (self.path / "robot.txt").write_text("two\n", encoding="utf-8")
        self.assertEqual(1, len(self.repository.status()))
        self.repository.save_version(
            "v0.0.2",
            "second\n- 添加相机\n* 调整控制器",
            ["robot.txt"],
        )
        self.assertEqual([], self.repository.status())
        self.assertIn("v0.0.2", self.repository.tags())
        self.assertEqual("second", self.repository.history()[0].subject)
        git_directory = Path(
            self.repository.run(["rev-parse", "--absolute-git-dir"]).strip()
        )
        manager_changelog = (
            git_directory / MODULE.METADATA_DIRECTORY / MODULE.MANAGER_CHANGELOG
        )
        self.assertTrue(manager_changelog.exists())
        self.assertIn("0.0.2 (", manager_changelog.read_text(encoding="utf-8"))
        manifest = (
            git_directory
            / MODULE.METADATA_DIRECTORY
            / MODULE.MANIFEST_DIRECTORY
            / f"v0.0.2{MODULE.MANIFEST_SUFFIX}"
        )
        self.assertEqual("robot.txt\n", manifest.read_text(encoding="utf-8"))
        self.assertEqual([".git"], sorted(path.name for path in self.path.glob(".*")))
        downloaded_changelog = self.path / "CHANGELOG.rst"
        self.repository.download_changelog(downloaded_changelog)
        changelog_text = downloaded_changelog.read_text(encoding="utf-8")
        changelog_lines = changelog_text.splitlines()
        title = f"Changelog for {self.path.name}"
        marker = "^" * max(39, len(title))
        self.assertEqual([marker, title, marker, ""], changelog_lines[:4])
        version_index = next(
            index for index, line in enumerate(changelog_lines) if line.startswith("0.0.2 (")
        )
        self.assertEqual(
            "-" * len(changelog_lines[version_index]),
            changelog_lines[version_index + 1],
        )
        self.assertEqual("* second", changelog_lines[version_index + 2])
        self.assertNotIn("##", changelog_text)
        self.assertNotIn("v0.0.2", changelog_text)
        self.assertIn("* second", changelog_text)
        self.assertIn("* 添加相机", changelog_text)
        self.assertIn("* 调整控制器", changelog_text)
        self.assertIn("0.0.1 (", changelog_text)
        archive = self.path / "v0.0.2.zip"
        self.repository.export("v0.0.2", archive)
        with zipfile.ZipFile(archive) as bundle:
            self.assertIn("robot.txt", bundle.namelist())
            self.assertNotIn("other.txt", bundle.namelist())
        snapshot = self.path / "snapshot"
        self.repository.materialize("v0.0.2", snapshot)
        self.assertTrue((snapshot / "robot.txt").exists())
        self.assertFalse((snapshot / "other.txt").exists())

    def test_storage_size_reports_allocated_git_space(self):
        initial_size = self.repository.storage_size()
        git_directory = Path(
            self.repository.run(["rev-parse", "--absolute-git-dir"]).strip()
        )
        (git_directory / "storage-probe.bin").write_bytes(b"x" * (128 * 1024))

        self.assertGreater(initial_size, 0)
        self.assertGreater(self.repository.storage_size(), initial_size)

    def test_storage_size_format(self):
        self.assertEqual("0 B", MODULE.format_storage_size(0))
        self.assertEqual("1.0 KB", MODULE.format_storage_size(1024))
        self.assertEqual("2.5 MB", MODULE.format_storage_size(5 * 1024 * 1024 // 2))

    def test_application_version_uses_project_version_file(self):
        expected = (SOURCE.parents[1] / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(expected, MODULE.APP_VERSION)

    def test_recent_workspaces_are_deduplicated_limited_and_persisted(self):
        config_file = self.path / "config" / "settings.ini"
        workspaces = []
        for index in range(MODULE.RECENT_WORKSPACE_LIMIT + 2):
            workspace = self.path / f"workspace-{index}"
            workspace.mkdir()
            workspaces.append(workspace.resolve())
            MODULE.save_workspace_history(workspace, config_file)

        MODULE.save_workspace_history(workspaces[5], config_file)

        recent = MODULE.recent_workspaces(config_file)
        self.assertEqual(MODULE.RECENT_WORKSPACE_LIMIT, len(recent))
        self.assertEqual(workspaces[5], recent[0])
        self.assertEqual(len(recent), len(set(recent)))
        parser = MODULE._read_settings(config_file)
        self.assertEqual(str(workspaces[5]), parser.get("repository", "path"))

    def test_recent_workspaces_ignore_invalid_paths_and_malformed_settings(self):
        config_file = self.path / "settings.ini"
        valid = self.path / "valid"
        valid.mkdir()
        parser = MODULE.configparser.ConfigParser()
        parser["recent_workspaces"] = {
            "paths": MODULE.json.dumps(
                [str(valid), str(self.path / "missing"), str(valid), 42]
            )
        }
        with config_file.open("w", encoding="utf-8") as handle:
            parser.write(handle)
        self.assertEqual([valid.resolve()], MODULE.recent_workspaces(config_file))

        config_file.write_text("[recent_workspaces]\npaths = not-json\n", encoding="utf-8")
        self.assertEqual([], MODULE.recent_workspaces(config_file))

    def test_workspace_history_preserves_other_settings_sections(self):
        config_file = self.path / "settings.ini"
        config_file.write_text("[display]\ncompact = true\n", encoding="utf-8")

        MODULE.save_workspace_history(self.path, config_file)

        parser = MODULE._read_settings(config_file)
        self.assertTrue(parser.getboolean("display", "compact"))

    def test_clear_recent_workspaces_preserves_current_and_other_settings(self):
        config_file = self.path / "settings.ini"
        config_file.write_text("[display]\ncompact = true\n", encoding="utf-8")
        MODULE.save_workspace_history(self.path, config_file)

        MODULE.clear_recent_workspaces(config_file)

        self.assertEqual([], MODULE.recent_workspaces(config_file))
        parser = MODULE._read_settings(config_file)
        self.assertEqual(str(self.path.resolve()), parser.get("repository", "path"))
        self.assertTrue(parser.getboolean("display", "compact"))

    def test_multi_part_version_numbers(self):
        self.git("tag", "v1.7.8.9.7")

        self.assertEqual((1, 7, 8, 9, 7), MODULE.version_key("v1.7.8.9.7"))
        self.assertEqual("v1.7.8.9.8", self.repository.next_version())
        self.assertIsNotNone(MODULE.VERSION_PATTERN.match("v1.7.8.5"))
        self.assertIsNotNone(MODULE.VERSION_PATTERN.match("v1.7.8.9.7"))
        self.assertIsNone(MODULE.VERSION_PATTERN.match("v1.7"))

    def test_history_is_sorted_by_version_number(self):
        (self.path / "robot.txt").write_text("two\n", encoding="utf-8")
        self.git("add", "robot.txt")
        self.git("commit", "-q", "-m", "higher version")
        self.git("tag", "v2.0.0")
        (self.path / "robot.txt").write_text("three\n", encoding="utf-8")
        self.git("add", "robot.txt")
        self.git("commit", "-q", "-m", "later commit with lower version")
        self.git("tag", "v1.0.0")

        displayed_versions = [entry.tags[0] for entry in self.repository.history()]
        self.assertEqual(["v2.0.0", "v1.0.0", "v0.0.1"], displayed_versions)

    def test_detached_head_cannot_save(self):
        self.git("checkout", "-q", "v0.0.1")
        with self.assertRaises(MODULE.GitError):
            self.repository.save_version("v0.0.2", "blocked", ["robot.txt"])

    def test_delete_version_removes_tag_without_rewriting_commit(self):
        commit = self.repository.run(["rev-parse", "v0.0.1"]).strip()
        head_before = self.repository.run(["rev-parse", "HEAD"]).strip()

        self.repository.delete_version("v0.0.1")

        self.assertNotIn("v0.0.1", self.repository.tags())
        self.assertEqual(head_before, self.repository.run(["rev-parse", "HEAD"]).strip())
        self.assertEqual(commit, self.repository.run(["rev-parse", "HEAD"]).strip())
        self.assertEqual([], self.repository.history())
        self.assertTrue((self.path / "robot.txt").exists())
        self.assertNotIn("0.0.1 (", self.repository.changelog_text())

    def test_delete_version_rejects_unknown_version(self):
        with self.assertRaises(MODULE.GitError):
            self.repository.delete_version("v9.9.9")

    def test_delete_latest_version_commit_keeps_files_as_current_changes(self):
        self.git("branch", "-m", "main")
        (self.path / "robot.txt").write_text("bad version\n", encoding="utf-8")
        self.repository.save_version("v0.0.2", "bad version", ["robot.txt"])
        bad_commit = self.repository.run(["rev-parse", "HEAD"]).strip()
        parent = self.repository.run(["rev-parse", "HEAD^"]).strip()
        manifest = (
            self.repository.metadata_path
            / MODULE.MANIFEST_DIRECTORY
            / f"v0.0.2{MODULE.MANIFEST_SUFFIX}"
        )
        self.assertTrue(manifest.exists())
        (self.path / "robot.txt").write_text("local fix\n", encoding="utf-8")

        self.repository.delete_latest_version_commit("v0.0.2")

        self.assertEqual("main", self.repository.current_branch())
        self.assertEqual(parent, self.repository.run(["rev-parse", "HEAD"]).strip())
        self.assertNotEqual(bad_commit, self.repository.run(["rev-parse", "HEAD"]).strip())
        self.assertNotIn("v0.0.2", self.repository.tags())
        self.assertFalse(manifest.exists())
        self.assertEqual("local fix\n", (self.path / "robot.txt").read_text(encoding="utf-8"))
        self.assertIn("robot.txt", self.repository.changed_paths())
        self.assertNotIn("0.0.2 (", self.repository.changelog_text())

    def test_delete_latest_version_commit_rejects_older_version(self):
        self.git("branch", "-m", "main")
        (self.path / "robot.txt").write_text("two\n", encoding="utf-8")
        self.repository.save_version("v0.0.2", "second", ["robot.txt"])

        with self.assertRaises(MODULE.GitError):
            self.repository.delete_latest_version_commit("v0.0.1")

        self.assertIn("v0.0.1", self.repository.tags())
        self.assertIn("v0.0.2", self.repository.tags())

    def test_delete_latest_version_commit_preserves_staged_changes(self):
        self.git("branch", "-m", "main")
        (self.path / "robot.txt").write_text("two\n", encoding="utf-8")
        self.repository.save_version("v0.0.2", "second", ["robot.txt"])
        (self.path / "other.txt").write_text("staged\n", encoding="utf-8")
        self.repository.run(["add", "other.txt"])
        staged_before = self.repository.run(["ls-files", "--stage", "-z"])
        parent = self.repository.run(["rev-parse", "HEAD^"]).strip()
        (self.path / "other.txt").write_text("unstaged edits\n", encoding="utf-8")

        self.repository.delete_latest_version_commit("v0.0.2")

        self.assertNotIn("v0.0.2", self.repository.tags())
        self.assertEqual(parent, self.repository.run(["rev-parse", "HEAD"]).strip())
        self.assertEqual(staged_before, self.repository.run(["ls-files", "--stage", "-z"]))
        self.assertEqual("staged\n", self.repository.run(["show", ":other.txt"]))
        self.assertEqual("unstaged edits\n", (self.path / "other.txt").read_text(encoding="utf-8"))
        self.assertEqual("two\n", (self.path / "robot.txt").read_text(encoding="utf-8"))

    def test_delete_latest_version_commit_rejects_initial_commit(self):
        self.git("branch", "-m", "main")
        with self.assertRaises(MODULE.GitError):
            self.repository.delete_latest_version_commit("v0.0.1")

        self.assertIn("v0.0.1", self.repository.tags())

    def test_update_version_message_updates_tag_without_rewriting_commit(self):
        commit_before = self.repository.run(["rev-list", "-n", "1", "v0.0.1"]).strip()
        head_before = self.repository.run(["rev-parse", "HEAD"]).strip()

        self.repository.update_version_message(
            "v0.0.1", "新的说明\n- 补充细节"
        )

        self.assertEqual(
            commit_before,
            self.repository.run(["rev-list", "-n", "1", "v0.0.1"]).strip(),
        )
        self.assertEqual(head_before, self.repository.run(["rev-parse", "HEAD"]).strip())
        self.assertEqual("tag", self.repository.run(["cat-file", "-t", "v0.0.1"]).strip())
        self.assertEqual("新的说明\n补充细节", self.repository.version_message("v0.0.1"))
        self.assertEqual("新的说明", self.repository.history()[0].subject)
        changelog = self.repository.changelog_text()
        self.assertIn("* 新的说明", changelog)
        self.assertIn("* 补充细节", changelog)
        self.assertNotIn("* initial", changelog)
        git_directory = Path(
            self.repository.run(["rev-parse", "--absolute-git-dir"]).strip()
        )
        stored_changelog = (
            git_directory / MODULE.METADATA_DIRECTORY / MODULE.MANAGER_CHANGELOG
        ).read_text(encoding="utf-8")
        self.assertIn("* 新的说明", stored_changelog)

    def test_update_version_message_rejects_empty_or_unknown_version(self):
        with self.assertRaises(MODULE.GitError):
            self.repository.update_version_message("v0.0.1", "  \n")
        with self.assertRaises(MODULE.GitError):
            self.repository.update_version_message("v9.9.9", "missing")

    def test_full_snapshots_include_unchanged_files(self):
        (self.path / "robot.txt").write_text("two\n", encoding="utf-8")
        self.repository.save_version(
            "v0.0.2", "second", ["robot.txt", "other.txt"]
        )
        left = self.path / "left"
        right = self.path / "right"
        self.repository.materialize("v0.0.1", left)
        self.repository.materialize("v0.0.2", right)
        self.assertEqual(
            ["other.txt", "robot.txt"],
            sorted(path.name for path in left.iterdir()),
        )
        self.assertEqual(
            ["other.txt", "robot.txt"],
            sorted(path.name for path in right.iterdir()),
        )
        self.assertEqual(
            (left / "other.txt").read_text(encoding="utf-8"),
            (right / "other.txt").read_text(encoding="utf-8"),
        )

    def test_compare_current_uses_editable_workspace(self):
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(MODULE.shutil, "which", return_value="/usr/bin/bcompare"), \
             mock.patch.object(self.repository, "materialize"), \
             mock.patch.object(
                 MODULE,
                 "prepare_bcompare_config",
                 return_value=Path("/tmp/code-version-manager-config"),
             ) as prepare_config, \
             mock.patch.object(MODULE.subprocess, "run", return_value=completed) as run:
            self.repository.compare("v0.0.1")

        command = run.call_args.args[0]
        self.assertEqual("bcompare", command[0])
        self.assertNotIn("-expandall", command)
        self.assertIn("-leftreadonly", command)
        self.assertNotIn("-readonly", command)
        filter_option = next(item for item in command if item.startswith("-filters="))
        self.assertEqual(f"-filters={MODULE.BCOMPARE_FILTER}", filter_option)
        self.assertFalse(filter_option.endswith("\\"))
        self.assertEqual(str(self.path), command[-1])
        self.assertEqual(
            "/tmp/code-version-manager-config",
            run.call_args.kwargs["env"]["XDG_CONFIG_HOME"],
        )
        prepare_config.assert_called_once()

    def test_compare_versions_keeps_both_snapshots_read_only(self):
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(MODULE.shutil, "which", return_value="/usr/bin/bcompare"), \
             mock.patch.object(self.repository, "materialize") as materialize, \
             mock.patch.object(MODULE, "prepare_bcompare_config"), \
             mock.patch.object(MODULE.subprocess, "run", return_value=completed) as run:
            self.repository.compare("v0.0.1", "v0.0.2")

        command = run.call_args.args[0]
        self.assertIn("-readonly", command)
        self.assertNotIn("-leftreadonly", command)
        self.assertFalse(
            next(item for item in command if item.startswith("-filters=")).endswith("\\")
        )
        self.assertNotEqual(str(self.path), command[-1])
        self.assertEqual(
            ["v0.0.1", "v0.0.2"],
            [call.args[0] for call in materialize.call_args_list],
        )

    def test_compare_requires_beyond_compare_before_loading_config(self):
        with mock.patch.object(MODULE.shutil, "which", return_value=None), \
             mock.patch.object(MODULE, "prepare_bcompare_config") as prepare_config:
            with self.assertRaises(MODULE.GitError):
                self.repository.compare("v0.0.1")

        prepare_config.assert_not_called()

    def test_bcompare_config_uses_bundled_defaults_and_stays_collapsed(self):
        source_home = self.path / "source-config"
        source = source_home / "bcompare"
        source.mkdir(parents=True)
        sessions = source / "BCSessions.xml"
        sessions.write_text(
            """<?xml version="1.0" encoding="UTF-8"?>
<BCSessions Version="1" MinVersion="1">
  <TSessionFolder><Items><TDirCompareSession>
  <Filters><NameFilter Value="*.txt"/></Filters><Rules>
    <ExpandAllOnLoad Value="True"/>
    <ExpandDiffsOnLoad Value="True"/>
  </Rules></TDirCompareSession></Items></TSessionFolder>
</BCSessions>
""",
            encoding="utf-8",
        )
        with mock.patch.dict(MODULE.os.environ, {"XDG_CONFIG_HOME": str(source_home)}):
            config_home = MODULE.prepare_bcompare_config(self.path / "isolated")

        copied_path = config_home / "bcompare" / "BCSessions.xml"
        self.assertEqual(
            MODULE.bundled_bcompare_sessions_path().read_bytes(),
            copied_path.read_bytes(),
        )
        copied = ET.parse(copied_path)
        self.assertEqual(
            ["False"],
            [node.get("Value") for node in copied.findall(".//ExpandAllOnLoad")],
        )
        self.assertEqual(
            ["False"],
            [node.get("Value") for node in copied.findall(".//ExpandDiffsOnLoad")],
        )
        session = copied.find(".//TDirCompareSession")
        self.assertIsNotNone(session)
        self.assertEqual(
            MODULE.BCOMPARE_FILTER,
            session.find("./Filters/NameFilter").get("Value"),
        )
        rules = session.find("./Rules")
        self.assertIsNotNone(rules)
        self.assertEqual(
            {
                "UseSizeComparison": "True",
                "UseTimestampComparison": "False",
                "TimestampTolerance": "2",
                "IgnoreDST": "False",
                "IgnoreTimeZone": "False",
                "UseCaseSensitiveComparison": "False",
                "IgnoreFileExts": "False",
                "NormalizedAlignment": "True",
                "UseContentComparison": "False",
                "ContentCompareKind": "ccRulesBased",
                "ContentCompareSkipSame": "False",
                "ContentCompareOverrides": "True",
                "ExpandAllOnLoad": "False",
                "ExpandDiffsOnLoad": "False",
            },
            {
                node.tag: node.get("Value") for node in rules
            },
        )
        original = ET.parse(sessions)
        self.assertEqual("True", original.find(".//ExpandAllOnLoad").get("Value"))

    def test_initialize_unversioned_workspace(self):
        other_root = Path(self.temp.name) / "new-workspace"
        other_root.mkdir()
        (other_root / "keep.txt").write_text("keep\n", encoding="utf-8")
        (other_root / "skip.txt").write_text("skip\n", encoding="utf-8")
        build = other_root / "build"
        build.mkdir()
        (build / "generated.txt").write_text("generated\n", encoding="utf-8")
        source = other_root / "src"
        source.mkdir()
        (source / "main.txt").write_text("main\n", encoding="utf-8")
        nested = source / "config"
        nested.mkdir()
        (nested / "robot.yaml").write_text("robot: true\n", encoding="utf-8")
        vendor = source / "vendor"
        vendor.mkdir()
        (vendor / "ignored.bin").write_bytes(b"ignored")
        (source / ".gitignore").write_text("vendor/\n", encoding="utf-8")
        (source / "CMakeLists.txt").symlink_to(
            "/opt/ros/noetic/share/catkin/cmake/toplevel.cmake"
        )
        groups = MODULE.top_level_package_groups(other_root)
        self.assertEqual(["keep.txt", "skip.txt", "src/"], list(groups))
        self.assertNotIn("src/main.txt", groups)
        selected = MODULE.expand_package_groups(groups, ["src/"])
        self.assertIn("src/CMakeLists.txt", selected)
        self.assertIn("src/config/robot.yaml", selected)
        self.assertIn("src/main.txt", selected)
        self.assertIn("src/vendor/ignored.bin", selected)
        repository = MODULE.initialize_repository(
            other_root, "v0.0.1", "initial", selected
        )
        self.assertEqual("main", repository.current_branch())
        archive = other_root / "initial.zip"
        repository.export("v0.0.1", archive)
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            self.assertIn("src/CMakeLists.txt", names)
            self.assertIn("src/config/robot.yaml", names)
            self.assertIn("src/main.txt", names)
            self.assertNotIn("src/vendor/ignored.bin", names)

    def test_nested_workspace_does_not_use_parent_repository(self):
        nested = self.path / "lane_editor" / "lane_editor_core"
        nested.mkdir(parents=True)
        (nested / "core.txt").write_text("core\n", encoding="utf-8")

        with self.assertRaises(MODULE.GitError):
            MODULE.GitRepository(nested)

        repository = MODULE.initialize_repository(
            nested, "v0.0.1", "initial", ["core.txt"]
        )

        self.assertEqual(nested.resolve(), repository.path)
        self.assertTrue((nested / ".git").is_dir())
        self.assertEqual("main", repository.current_branch())
        self.assertIn("v0.0.1", repository.tags())

    def test_top_level_scope_records_nested_deletions(self):
        source = self.path / "src"
        source.mkdir()
        (source / "keep.txt").write_text("one\n", encoding="utf-8")
        (source / "obsolete.txt").write_text("obsolete\n", encoding="utf-8")
        self.git("add", "src")
        self.git("commit", "-q", "-m", "add source")
        (source / "keep.txt").write_text("two\n", encoding="utf-8")
        (source / "obsolete.txt").unlink()
        groups = MODULE.top_level_package_groups(self.path)
        self.assertIn("src/", groups)
        entries = MODULE.top_level_groups_for_paths(
            groups, self.repository.changed_paths()
        )
        self.assertIn("src/", entries)
        selected = MODULE.expand_package_groups(groups, ["src/"])
        self.repository.save_version(
            "v0.0.2", "更新源码\n删除旧文件", selected, selected_scopes=["src/"]
        )
        self.assertEqual([], self.repository.status())
        archive = self.path / "source.zip"
        self.repository.export("v0.0.2", archive)
        with zipfile.ZipFile(archive) as bundle:
            self.assertIn("src/keep.txt", bundle.namelist())
            self.assertNotIn("src/obsolete.txt", bundle.namelist())


class RemoteProcessTest(unittest.TestCase):
    def test_remote_process_returns_output_and_exit_status(self):
        result = MODULE.run_remote_process(
            [sys.executable, "-c", "import sys; print('output'); print('progress', file=sys.stderr)"],
            timeout=5, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.assertEqual(0, result.returncode)
        self.assertEqual("output\n", result.stdout)
        self.assertEqual("progress\n", result.stderr)

    def test_timeout_kills_transport_children_without_waiting_for_inherited_pipes(self):
        command = [
            sys.executable, "-c",
            "import subprocess, sys, time; "
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "print(child.pid, flush=True); "
            "print('Writing objects: 50%', file=sys.stderr, flush=True); time.sleep(60)",
        ]
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as raised:
            MODULE.run_remote_process(
                command, timeout=0.5, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )

        self.assertLess(time.monotonic() - started, 5)
        self.assertIn("Writing objects: 50%", raised.exception.stderr)
        child_pid = int(raised.exception.stdout.strip())
        status = Path(f"/proc/{child_pid}/stat")
        deadline = time.monotonic() + 1
        while True:
            try:
                state = status.read_text().split()[2]
            except FileNotFoundError:
                break
            if state == "Z":
                break
            self.assertLess(time.monotonic(), deadline, "Git transport child was not terminated")
            time.sleep(0.01)

    def test_timeout_tolerates_process_group_already_exiting(self):
        process = mock.Mock()
        process.pid = 123456
        process.communicate.side_effect = [subprocess.TimeoutExpired("git", 1), ("output", "progress")]
        context_manager = mock.MagicMock()
        context_manager.__enter__.return_value = process
        with mock.patch.object(MODULE.subprocess, "Popen", return_value=context_manager) as popen, \
             mock.patch.object(MODULE.os, "killpg", side_effect=ProcessLookupError):
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                MODULE.run_remote_process(["git", "push"], timeout=1)

        self.assertEqual("progress", raised.exception.stderr)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(2, process.communicate.call_count)


class RemoteRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "workspace"
        self.path.mkdir()
        (self.path / "robot.txt").write_text("initial\n", encoding="utf-8")
        self.repository = MODULE.initialize_repository(
            self.path, "v0.0.1", "initial", ["robot.txt"]
        )
        self.remote = self.root / "remote.git"
        self.git(self.root, "init", "--bare", "-q", str(self.remote))

    def git(self, path, *args):
        return subprocess.run(
            ["git", "-C", str(path), *args], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ).stdout.strip()

    def configure(self, remote=None):
        self.repository.configure_remote(
            str(remote or self.remote), "Test User", "test@example.com"
        )

    def publish(self, version):
        (self.path / "robot.txt").write_text(version + "\n", encoding="utf-8")
        self.repository.save_version(version, "changes " + version, ["robot.txt"])

    def test_configure_is_local_and_preserves_unrelated_remotes(self):
        self.repository.run(["remote", "add", "backup", str(self.root / "backup.git")])
        self.repository.run(["config", "--local", "codeversionmanager.username", "old-account"])
        self.repository.run(["config", "--local", "codeversionmanager.autoSync", "true"])
        self.configure()

        self.assertEqual(str(self.remote), self.repository.remote_url())
        self.assertEqual("", self.repository.run(["config", "--local", "--get", "codeversionmanager.autoSync"], check=False))
        self.assertEqual("Test User", self.git(self.path, "config", "--local", "user.name"))
        self.assertEqual("", self.repository.run(["config", "--local", "--get", "codeversionmanager.username"], check=False))
        self.assertEqual(["backup", "origin"], self.git(self.path, "remote").splitlines())
        result = self.repository.check_remote_connection()
        self.assertIn("推送预检查通过", result)
        self.assertEqual("", self.git(self.remote, "for-each-ref"))


    def test_github_readable_but_write_denied_fails_during_dry_run_preflight(self):
        remote_url = "git@github.com:lqxmb/code-version-manager.git"
        self.configure(remote=remote_url)
        read_success = subprocess.CompletedProcess([], 0, stdout="refs\n", stderr="")
        write_denied = subprocess.CompletedProcess(
            [], 128, stdout="",
            stderr=(
                "remote: Permission to lqxmb/code-version-manager.git denied to lqxmb.\n"
                "fatal: Could not read from remote repository."
            ),
        )
        with mock.patch.object(
            MODULE, "run_remote_process", side_effect=[read_success, write_denied]
        ) as run:
            with self.assertRaises(MODULE.GitError) as raised:
                self.repository.check_remote_connection()

        self.assertIn("SSH 账号写入仓库", str(raised.exception))
        self.assertIn("本地提交和版本标签均已保留", str(raised.exception))
        self.assertEqual(2, run.call_count)
        preflight = run.call_args_list[1]
        self.assertIn("--dry-run", preflight.args[0])
        self.assertEqual(MODULE.REMOTE_TIMEOUT, preflight.kwargs["timeout"])
        self.assertEqual("", self.git(self.remote, "for-each-ref"))

    def test_detached_head_reports_read_only_check_without_dry_run(self):
        self.configure()
        self.git(self.path, "checkout", "--detach", "-q", "HEAD")
        completed = subprocess.CompletedProcess([], 0, stdout="refs\n", stderr="")
        with mock.patch.object(MODULE, "run_remote_process", return_value=completed) as run:
            result = self.repository.check_remote_connection()

        self.assertIn("未检查推送权限", result)
        self.assertEqual(1, run.call_count)
        self.assertIn("ls-remote", run.call_args.args[0])
        self.assertNotIn("push", run.call_args.args[0])

    def test_empty_repository_reports_that_write_permission_was_not_checked(self):
        empty = self.root / "empty"
        empty.mkdir()
        self.git(empty, "init", "-q")
        repository = MODULE.GitRepository(empty)
        repository.configure_remote(
            str(self.remote), "Test", "test@example.com"
        )
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(MODULE, "run_remote_process", return_value=completed) as run:
            result = repository.check_remote_connection()

        self.assertIn("尚无本地提交，未检查推送权限", result)
        self.assertEqual(1, run.call_count)
        self.assertNotIn("push", run.call_args.args[0])

    def test_dry_run_does_not_change_refs_upstream_or_run_local_push_hook(self):
        self.configure()
        before_refs = self.git(self.path, "for-each-ref")
        before_config = self.git(self.path, "config", "--local", "--list")
        marker = self.root / "push-hook-ran"
        hook = self.path / ".git" / "hooks" / "pre-push"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
        hook.chmod(0o755)

        self.repository.check_remote_connection()

        self.assertEqual("", self.git(self.remote, "for-each-ref"))
        self.assertEqual(before_refs, self.git(self.path, "for-each-ref"))
        self.assertEqual(before_config, self.git(self.path, "config", "--local", "--list"))
        self.assertFalse(marker.exists())

    def test_configure_replaces_different_push_destination(self):
        self.repository.run(["remote", "add", "origin", str(self.root / "old.git")])
        self.repository.run(["config", "remote.origin.pushurl", str(self.root / "other.git")])
        self.configure()

        self.assertEqual(str(self.remote), self.repository._sync_target())

    def test_manual_sync_pushes_branch_all_versions_and_sets_upstream(self):
        self.configure()
        self.publish("v0.0.2")
        self.assertEqual("", self.git(self.remote, "for-each-ref"))
        self.repository.sync_remote()

        self.assertEqual(
            self.git(self.path, "rev-parse", "HEAD"),
            self.git(self.remote, "rev-parse", "refs/heads/main"),
        )
        self.assertEqual(["v0.0.1", "v0.0.2"], self.git(self.remote, "tag").splitlines())
        self.assertEqual("origin/main", self.git(self.path, "rev-parse", "--abbrev-ref", "@{u}"))
        self.assertEqual("robot.txt", self.git(self.remote, "ls-tree", "-r", "--name-only", "main"))
        self.repository.sync_remote()
        self.assertEqual("2", self.git(self.remote, "rev-list", "--count", "main"))

    def test_offline_versions_all_sync_on_manual_retry(self):
        missing = self.root / "unavailable.git"
        self.configure(missing)
        for version in ("v0.0.2", "v0.0.3", "v1.2.3.4"):
            self.publish(version)
            self.assertIn(version, self.repository.tags())
            self.assertEqual([], self.repository.status())
            self.assertEqual(["robot.txt"], self.repository.manifest(version))
            self.assertIn(version[1:], self.repository.changelog_text())

        self.git(self.root, "init", "--bare", "-q", str(missing))
        self.repository.sync_remote()

        self.assertEqual("4", self.git(missing, "rev-list", "--count", "main"))
        self.assertEqual(self.repository.tags(), self.git(missing, "tag", "--sort=version:refname").splitlines())
        self.assertEqual(
            self.git(self.path, "rev-parse", "HEAD"), self.git(missing, "rev-parse", "main")
        )
        self.repository.sync_remote()
        self.assertEqual("4", self.git(missing, "rev-list", "--count", "main"))

    def test_later_release_stays_local_until_manual_sync(self):
        missing = self.root / "unavailable.git"
        self.configure(missing)
        for version in ("v0.0.2", "v0.0.3"):
            self.publish(version)
        self.git(self.root, "init", "--bare", "-q", str(missing))

        self.publish("v0.0.4")
        self.assertEqual("", self.git(missing, "for-each-ref"))
        self.repository.sync_remote()

        self.assertEqual("4", self.git(missing, "rev-list", "--count", "main"))
        self.assertEqual(self.repository.tags(), self.git(missing, "tag", "--sort=version:refname").splitlines())

    def test_local_release_does_not_contact_remote(self):
        self.configure(self.root / "unavailable.git")
        with mock.patch.object(self.repository, "_run_remote") as run:
            self.publish("v0.0.2")
        run.assert_not_called()
        self.assertIn("v0.0.2", self.repository.tags())

    def test_missing_remote_also_keeps_local_release(self):
        self.repository.run(["config", "codeversionmanager.autoSync", "true"])
        with mock.patch.object(self.repository, "_run_remote") as run:
            self.publish("v0.0.2")
        run.assert_not_called()
        self.assertIn("v0.0.2", self.repository.tags())

    def test_manual_sync_includes_lightweight_version_tags_but_not_other_tags(self):
        self.configure()
        self.repository.run(["tag", "v2.0.0"])
        self.repository.run(["tag", "internal-note"])
        self.publish("v2.0.1")
        self.repository.sync_remote()

        self.assertEqual(["v0.0.1", "v2.0.0", "v2.0.1"], self.git(self.remote, "tag").splitlines())

    def test_remote_ahead_is_not_overwritten_and_new_tags_are_not_partially_pushed(self):
        self.configure()
        self.repository.sync_remote()
        old_head = self.git(self.path, "rev-parse", "HEAD")
        self.publish("v0.0.2")
        self.repository.sync_remote()
        remote_head = self.git(self.remote, "rev-parse", "main")
        self.repository.run(["reset", "--mixed", old_head])

        self.publish("v0.0.3")
        with self.assertRaises(MODULE.GitError):
            self.repository.sync_remote()

        self.assertEqual(remote_head, self.git(self.remote, "rev-parse", "main"))
        self.assertEqual(["v0.0.1", "v0.0.2"], self.git(self.remote, "tag").splitlines())
        self.assertIn("v0.0.3", self.repository.tags())

    def test_conflicting_tag_does_not_update_remote_branch(self):
        self.configure()
        self.repository.sync_remote()
        remote_head = self.git(self.remote, "rev-parse", "main")
        self.repository.run(["tag", "-f", "-a", "v0.0.1", "-m", "changed description"])

        self.publish("v0.0.2")
        with self.assertRaises(MODULE.GitError):
            self.repository.sync_remote()

        self.assertEqual(remote_head, self.git(self.remote, "rev-parse", "main"))
        self.assertEqual(["v0.0.1"], self.git(self.remote, "tag").splitlines())

    def test_server_without_atomic_push_fails_without_partial_updates(self):
        self.configure()
        self.git(self.remote, "config", "receive.advertiseAtomic", "false")

        self.publish("v0.0.2")
        with self.assertRaises(MODULE.GitError):
            self.repository.sync_remote()

        self.assertEqual("", self.git(self.remote, "for-each-ref"))
        self.assertIn("v0.0.2", self.repository.tags())

    def test_push_permission_failure_keeps_local_version(self):
        self.configure()
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o700)

        self.publish("v0.0.2")
        with self.assertRaises(MODULE.GitError):
            self.repository.sync_remote()

        self.assertEqual("", self.git(self.remote, "for-each-ref"))
        self.assertIn("v0.0.2", self.repository.tags())

    def test_sync_rejects_detached_head(self):
        self.configure()
        self.repository.run(["switch", "--detach", "v0.0.1"])
        with self.assertRaisesRegex(MODULE.GitError, "查看历史版本时不能同步"):
            self.repository.sync_remote()
        self.assertEqual("", self.git(self.remote, "for-each-ref"))

    def test_empty_repository_can_be_configured_before_first_release(self):
        empty = self.root / "empty"
        empty.mkdir()
        self.repository = MODULE.create_repository(empty)
        self.configure()
        self.assertEqual([], self.repository.history())
        with self.assertRaisesRegex(MODULE.GitError, "尚无可同步的版本"):
            self.repository.sync_remote()
        (empty / "hello.txt").write_text("hello\n", encoding="utf-8")
        self.repository.save_version("v1.0.0", "first", ["hello.txt"])
        self.assertEqual("", self.git(self.remote, "for-each-ref"))
        self.repository.sync_remote()
        self.assertEqual(["v1.0.0"], self.git(self.remote, "tag").splitlines())


    def test_uses_actual_working_branch_instead_of_assuming_main(self):
        self.repository.run(["switch", "-c", "release"])
        self.configure()
        self.publish("v0.0.2")
        self.repository.sync_remote()

        self.assertEqual(
            "refs/heads/release", self.git(self.remote, "for-each-ref", "--format=%(refname)", "refs/heads/")
        )
        self.assertEqual(["v0.0.1", "v0.0.2"], self.git(self.remote, "tag").splitlines())

    def test_multiple_push_destinations_are_rejected_without_losing_local_release(self):
        self.configure()
        self.repository.run(["config", "--add", "remote.origin.pushurl", str(self.remote)])
        self.repository.run(["config", "--add", "remote.origin.pushurl", str(self.root / "other.git")])

        self.publish("v0.0.2")
        with self.assertRaises(MODULE.GitError):
            self.repository.sync_remote()

        self.assertEqual("", self.git(self.remote, "for-each-ref"))
        self.assertIn("v0.0.2", self.repository.tags())


    def test_connection_timeout_uses_ssh_without_creating_helper_files(self):
        with mock.patch.object(
            MODULE, "run_remote_process", side_effect=subprocess.TimeoutExpired("git", MODULE.REMOTE_TIMEOUT)
        ) as run:
            with self.assertRaisesRegex(MODULE.GitError, "远程操作超时"):
                self.repository._run_remote(["ls-remote", "origin"], "git@example.com:repo.git")
        self.assertEqual("/bin/false", run.call_args.kwargs["env"]["GIT_ASKPASS"])

    def test_push_has_longer_timeout_than_connection_verification(self):
        self.configure()
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(MODULE, "run_remote_process", return_value=completed) as run:
            self.repository.sync_remote()
            self.assertEqual(MODULE.PUSH_TIMEOUT, run.call_args.kwargs["timeout"])
            self.assertIn("--progress", run.call_args.args[0])
            self.repository.check_remote_connection()
            self.assertEqual(MODULE.REMOTE_TIMEOUT, run.call_args.kwargs["timeout"])
            self.assertIn("--dry-run", run.call_args.args[0])

    def test_push_timeout_is_editable_local_and_preserved_when_not_overridden(self):
        self.repository.configure_remote(
            str(self.remote), "Test", "test@example.com", push_timeout=1800
        )
        self.assertEqual(1800, self.repository.push_timeout_seconds())
        self.assertEqual("1800", self.git(self.path, "config", "--local", "codeversionmanager.pushTimeout"))
        self.configure()
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(MODULE, "run_remote_process", return_value=completed) as run:
            self.repository.sync_remote()
        self.assertEqual(1800, run.call_args.kwargs["timeout"])

    def test_invalid_timeout_is_rejected_before_changing_repository(self):
        for value in (0, 59, 3601, 900.5, "900"):
            with self.subTest(value=value), self.assertRaises(MODULE.GitError):
                self.repository.configure_remote(
                    str(self.remote), "Test", "test@example.com", push_timeout=value
                )
        self.assertEqual("", self.repository.remote_url())
        for value in ("invalid", "0", "3601"):
            self.repository.run(["config", "codeversionmanager.pushTimeout", value])
            with self.subTest(value=value), self.assertRaises(MODULE.GitError):
                self.repository.push_timeout_seconds()

    def test_timeout_preserves_output_and_marks_unknown_remote_state(self):
        remote = "git@example.com:repo.git"
        failure = subprocess.TimeoutExpired(
            "git", MODULE.PUSH_TIMEOUT, stderr=b"Writing objects: 75%\rconnection stalled"
        )
        with mock.patch.object(MODULE, "run_remote_process", side_effect=failure):
            with self.assertRaises(MODULE.GitError) as raised:
                self.repository._run_remote(["push", "origin"], remote)

        message = str(raised.exception)
        self.assertIn(f"超过 {MODULE.PUSH_TIMEOUT} 秒", message)
        self.assertIn("Writing objects: 75%", message)
        self.assertIn("不能确认远程是否已经接收更新", message)
        self.assertIn("默认 15 分钟", message)

    def test_timeout_with_no_git_output_still_explains_stage_and_keeps_local_release(self):
        self.configure()
        self.publish("v0.0.2")
        with mock.patch.object(
            MODULE, "run_remote_process", side_effect=subprocess.TimeoutExpired("git", MODULE.PUSH_TIMEOUT)
        ):
            with self.assertRaisesRegex(MODULE.GitError, "远程操作超时") as raised:
                self.repository.sync_remote()

        self.assertIn("推送分支与版本标签", str(raised.exception))
        self.assertNotIn("Git 最后输出", str(raised.exception))
        self.assertEqual(["v0.0.1", "v0.0.2"], self.repository.tags())
        self.assertEqual(["robot.txt"], self.repository.manifest("v0.0.2"))
        self.assertEqual("", self.git(self.remote, "for-each-ref"))

    def test_retry_after_timeout_when_remote_already_accepted_updates_is_idempotent(self):
        self.configure()
        self.publish("v0.0.2")
        original_runner = MODULE.run_remote_process

        def accepted_then_timeout(command, **kwargs):
            result = original_runner(command, **kwargs)
            self.assertEqual(0, result.returncode)
            raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=result.stdout, stderr=result.stderr)

        with mock.patch.object(MODULE, "run_remote_process", side_effect=accepted_then_timeout):
            with self.assertRaisesRegex(MODULE.GitError, "不能确认远程是否已经接收更新"):
                self.repository.sync_remote()

        self.assertEqual(["v0.0.1", "v0.0.2"], self.git(self.remote, "tag").splitlines())
        self.repository.sync_remote()
        self.assertEqual("2", self.git(self.remote, "rev-list", "--count", "main"))
        self.assertEqual("2", self.git(self.path, "rev-list", "--count", "HEAD"))


    def test_invalid_remote_addresses_and_identity_are_rejected(self):
        for value in (
            "", "--upload-pack=bad", "http://example.com/repo.git", "ext::bad", "relative/path",
            "https://example.com/repo.git", "ssh://git:password@example.com/repo.git",
            "ssh://example.com/repo.git?query=value", "ssh://host/repo.git\nother",
        ):
            with self.subTest(value=value), self.assertRaises(MODULE.GitError):
                MODULE.validate_remote_url(value)
        for value in (
            "git@example.com:team/repo.git", "ssh://git@example.com/repo.git",
            "ssh://git@custom-host:2222/repo.git", "git@custom-alias:team/repo.git", str(self.remote),
        ):
            self.assertEqual(value, MODULE.validate_remote_url(value))
        for name, email in (("", "email@example.com"), ("Test", ""), ("Test\nother", "a@b")):
            with self.subTest(name=name), self.assertRaises(MODULE.GitError):
                self.repository.configure_remote(str(self.remote), name, email)
        self.assertEqual("", self.repository.remote_url())


class GitHubSetupTest(unittest.TestCase):
    def test_github_helpers_accept_https_for_web_creation_but_sync_requires_ssh(self):
        https_url = "https://github.com/lqxmb/code-version-manager.git"
        ssh_url = "git@github.com:lqxmb/code-version-manager.git"
        expected = ("lqxmb", "code-version-manager")
        self.assertEqual(expected, MODULE.github_repository_coordinates(https_url))
        self.assertEqual(expected, MODULE.github_repository_coordinates(ssh_url))
        with self.assertRaises(MODULE.GitError):
            MODULE.validate_remote_url(https_url)
        self.assertEqual(ssh_url, MODULE.validate_remote_url(ssh_url))

    def test_github_helpers_reject_unrelated_or_unsafe_addresses(self):
        for remote_url in (
            "", "https://gitlab.com/owner/repo.git", "https://github.com.evil.test/owner/repo.git",
            "https://github.com/owner", "https://github.com/owner/repo/extra",
            "https://github.com/owner/..", "https://github.com/owner/repo?query=example",
            "https://user:password@github.com/owner/repo", "git@github.com:/",
            "file:///github.com/owner/repo",
        ):
            with self.subTest(remote_url=remote_url):
                self.assertIsNone(MODULE.github_repository_coordinates(remote_url))

    def test_create_page_uses_owner_from_url_and_defaults_to_private(self):
        url = MODULE.github_repository_creation_url(
            "git@github.com:lqxmb/code-version-manager.git", "unrelated"
        )
        self.assertEqual("https://github.com/new", url.split("?", 1)[0])
        parameters = parse_qs(urlsplit(url).query)
        self.assertEqual(["lqxmb"], parameters["owner"])
        self.assertEqual(["code-version-manager"], parameters["name"])
        self.assertEqual(["private"], parameters["visibility"])
        self.assertNotIn("auto_init", parameters)

    def test_empty_remote_can_open_creation_before_local_repository_exists(self):
        url = MODULE.github_repository_creation_url("", "my workspace")
        self.assertEqual(["my-workspace"], parse_qs(urlsplit(url).query)["name"])
        url = MODULE.github_repository_creation_url("", "中文工作空间")
        parameters = parse_qs(urlsplit(url).query)
        self.assertNotIn("owner", parameters)
        self.assertEqual(["workspace"], parameters["name"])
        with self.assertRaises(MODULE.GitError):
            MODULE.github_repository_creation_url("https://example.com/repo", "workspace")

    def test_creation_button_opens_official_page_without_creating_or_pushing(self):
        window = mock.Mock()
        window.workspace_path = Path("/temporary/my-workspace")
        remote_entry, dialog = mock.Mock(), mock.Mock()
        remote_entry.get_text.return_value = ""
        with mock.patch.object(MODULE.Gtk, "show_uri_on_window") as open_page:
            MODULE.VersionManagerWindow.open_github_repository_creation(window, dialog, remote_entry)
        arguments = open_page.call_args.args
        self.assertIs(dialog, arguments[0])
        self.assertEqual(["my-workspace"], parse_qs(urlsplit(arguments[1]).query)["name"])
        window.repository.configure_remote.assert_not_called()
        window.repository.sync_remote.assert_not_called()
        window.show_error.assert_not_called()

    def test_public_key_list_does_not_read_private_keys_or_follow_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            public = directory / "id_ed25519.pub"
            public.write_text("ssh-ed25519 AAAA test@example.com\n", encoding="utf-8")
            private = directory / "id_ed25519"
            private.write_text("private-key-must-not-be-read", encoding="utf-8")
            (directory / "linked.pub").symlink_to(private)
            (directory / "invalid.pub").write_text("-----BEGIN OPENSSH PRIVATE KEY-----", encoding="utf-8")
            (directory / "large.pub").write_text("x" * 16385, encoding="utf-8")
            original_read = Path.read_text

            def read_text(path, *args, **kwargs):
                self.assertEqual(".pub", path.suffix)
                self.assertFalse(path.is_symlink())
                return original_read(path, *args, **kwargs)

            with mock.patch.object(Path, "read_text", read_text):
                keys = MODULE.ssh_public_keys(directory)
            self.assertEqual({str(public): "ssh-ed25519 AAAA test@example.com"}, keys)
            self.assertEqual({}, MODULE.ssh_public_keys(directory / "missing"))

    def test_ssh_permission_errors_offer_key_setup_and_preserve_local_versions(self):
        remote_url = "git@github.com:lqxmb/code-version-manager.git"
        repository = mock.Mock()
        for raw_error, expected in (
            ("git@github.com: Permission denied (publickey).", "SSH 公钥设置"),
            ("Host key verification failed.", "不要关闭主机密钥检查"),
            ("Permission to lqxmb/code-version-manager.git denied to other.", "仓库写权限"),
        ):
            result = subprocess.CompletedProcess([], 128, stdout="", stderr=raw_error)
            with self.subTest(raw_error=raw_error), \
                 mock.patch.object(MODULE, "run_remote_process", return_value=result):
                with self.assertRaises(MODULE.GitError) as raised:
                    MODULE.GitRepository._run_remote(repository, ["ls-remote", "origin"], remote_url)
            self.assertIn(expected, str(raised.exception))
            self.assertIn("本地提交和版本标签均已保留", str(raised.exception))

    def test_ssh_transport_uses_agent_and_preserves_custom_ssh_command(self):
        repository = mock.Mock()
        remote = "git@custom-alias:team/repo.git"
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.dict(MODULE.os.environ, {"GIT_SSH_COMMAND": "ssh -F /custom/config", "SSH_AUTH_SOCK": "/session/agent"}), \
             mock.patch.object(MODULE, "run_remote_process", return_value=completed) as run:
            MODULE.GitRepository._run_remote(repository, ["ls-remote", remote], remote)
        environment = run.call_args.kwargs["env"]
        self.assertEqual("ssh -F /custom/config", environment["GIT_SSH_COMMAND"])
        self.assertEqual("/session/agent", environment["SSH_AUTH_SOCK"])
        self.assertEqual("0", environment["GIT_TERMINAL_PROMPT"])
        self.assertEqual("never", environment["SSH_ASKPASS_REQUIRE"])

    def test_ssh_transport_rejects_old_https_configuration_before_contacting_remote(self):
        repository = mock.Mock()
        with mock.patch.object(MODULE, "run_remote_process") as run:
            with self.assertRaisesRegex(MODULE.GitError, "仅支持 SSH"):
                MODULE.GitRepository._run_remote(repository, ["push", "origin"], "https://github.com/owner/repo.git")
        run.assert_not_called()


class BackgroundProgressTest(unittest.TestCase):
    def test_background_completion_uses_verification_status_instead_of_static_success(self):
        for result in ("仅验证读取，未检查推送权限", None):
            with self.subTest(result=result):
                window = mock.Mock()
                window.busy = False
                operation = mock.Mock(return_value=result)
                with mock.patch.object(MODULE.GLib, "timeout_add_seconds", return_value=42), \
                     mock.patch.object(MODULE.GLib, "idle_add") as idle_add, \
                     mock.patch.object(MODULE.threading, "Thread") as thread:
                    MODULE.VersionManagerWindow.run_background(
                        window, "正在验证...", operation, "默认成功提示"
                    )
                    thread.call_args.kwargs["target"]()

                idle_add.assert_called_once_with(
                    window._background_complete, result or "默认成功提示", False
                )

    def test_background_timer_updates_elapsed_time_and_is_removed_after_completion(self):
        window = mock.Mock()
        window.busy = False
        window.background_timer = None
        operation = mock.Mock()
        with mock.patch.object(MODULE.time, "monotonic", side_effect=[100, 105]), \
             mock.patch.object(MODULE.GLib, "timeout_add_seconds", return_value=42) as add_timer, \
             mock.patch.object(MODULE.threading, "Thread") as thread:
            MODULE.VersionManagerWindow.run_background(window, "正在同步远程仓库...", operation, "已同步")
            callback = add_timer.call_args.args[1]
            self.assertTrue(callback())

        self.assertEqual(42, window.background_timer)
        window.status_label.set_text.assert_called_with("正在同步远程仓库... · 已等待 5 秒")
        thread.return_value.start.assert_called_once_with()
        operation.assert_not_called()
        with mock.patch.object(MODULE.GLib, "source_remove") as remove_timer:
            MODULE.VersionManagerWindow._end_background(window)
        remove_timer.assert_called_once_with(42)
        self.assertIsNone(window.background_timer)
        self.assertFalse(window.busy)
        self.assertFalse(callback())


class CompareCurrentSelectionTest(unittest.TestCase):
    def test_compares_selected_version_with_current_workspace(self):
        entry = MODULE.HistoryEntry(
            commit="a" * 40,
            short_commit="aaaaaaa",
            date="2026-08-29",
            subject="selected version",
            tags=("v0.0.1",),
        )

        class Repository:
            def __init__(self):
                self.comparisons = []

            def compare(self, left, right=None):
                self.comparisons.append((left, right))

        class Window:
            def __init__(self):
                self.repository = Repository()
                self.errors = []

            def selected_history(self):
                return [entry]

            def show_error(self, message):
                self.errors.append(message)

            def run_background(self, _status, operation, _success):
                operation()

        window = Window()
        MODULE.VersionManagerWindow.on_compare_current(window, None)

        self.assertEqual([("v0.0.1", None)], window.repository.comparisons)
        self.assertEqual([], window.errors)

    def test_requires_exactly_one_selected_version(self):
        class Window:
            repository = object()

            def __init__(self):
                self.errors = []

            def selected_history(self):
                return []

            def show_error(self, message):
                self.errors.append(message)

        window = Window()
        MODULE.VersionManagerWindow.on_compare_current(window, None)

        self.assertEqual(1, len(window.errors))
        self.assertIn("选择一个", window.errors[0])


class DownloadVersionTest(unittest.TestCase):
    def test_download_name_includes_workspace_name(self):
        for tags, expected_name in (
            (("v1.2.0",), "m9_ws_v1.2.0.zip"),
            ((), "m9_ws_aaaaaaa.zip"),
        ):
            with self.subTest(tags=tags):
                entry = MODULE.HistoryEntry(
                    commit="a" * 40,
                    short_commit="aaaaaaa",
                    date="2026-10-23",
                    subject="selected version",
                    tags=tags,
                )
                destination = Path("/downloads/custom-name.zip")
                window = mock.Mock()
                window.workspace_path = Path("/workspaces/m9_ws")
                window.selected_history.return_value = [entry]
                window.run_background.side_effect = (
                    lambda _status, operation, _success: operation()
                )
                dialog = mock.Mock()
                dialog.run.return_value = MODULE.Gtk.ResponseType.OK
                dialog.get_filename.return_value = str(destination)

                with mock.patch.object(
                    MODULE.Gtk, "FileChooserDialog", return_value=dialog
                ):
                    MODULE.VersionManagerWindow.on_download_version(window, None)

                dialog.set_current_name.assert_called_once_with(expected_name)
                window.repository.export.assert_called_once_with(
                    entry.reference, destination
                )
                window.show_error.assert_not_called()


class EditVersionMessageTest(unittest.TestCase):
    def test_edits_selected_version_message(self):
        entry = MODULE.HistoryEntry(
            commit="a" * 40,
            short_commit="aaaaaaa",
            date="2026-09-01",
            subject="before",
            tags=("v1.2.3",),
        )

        class Repository:
            def __init__(self):
                self.updates = []

            def version_message(self, version):
                self.requested_version = version
                return "before\ndetail"

            def update_version_message(self, version, message):
                self.updates.append((version, message))

        class Window:
            def __init__(self):
                self.repository = Repository()
                self.prompted = []
                self.errors = []

            def selected_history(self):
                return [entry]

            def prompt_version_message(self, version, current_message):
                self.prompted.append((version, current_message))
                return "after\nnew detail"

            def run_background(self, _status, operation, _success):
                operation()

            def show_error(self, message):
                self.errors.append(message)

        window = Window()
        MODULE.VersionManagerWindow.on_edit_version_message(window, None)

        self.assertEqual([("v1.2.3", "before\ndetail")], window.prompted)
        self.assertEqual([("v1.2.3", "after\nnew detail")], window.repository.updates)
        self.assertEqual([], window.errors)


class DialogMaximizeTest(unittest.TestCase):
    def test_maximize_button_toggles_window_state(self):
        dialog = mock.Mock()
        dialog.is_maximized.return_value = False

        MODULE.VersionManagerWindow._on_toggle_dialog_maximized(None, dialog)

        dialog.maximize.assert_called_once_with()
        dialog.unmaximize.assert_not_called()

        dialog.reset_mock()
        dialog.is_maximized.return_value = True

        MODULE.VersionManagerWindow._on_toggle_dialog_maximized(None, dialog)

        dialog.unmaximize.assert_called_once_with()
        dialog.maximize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
