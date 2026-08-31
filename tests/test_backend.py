#!/usr/bin/python3
import importlib.util
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
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
        self.assertEqual("0.0.10", MODULE.APP_VERSION)

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

    def test_compare_does_not_expand_directories_by_default(self):
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(MODULE.shutil, "which", return_value="/usr/bin/bcompare"), \
             mock.patch.object(self.repository, "materialize"), \
             mock.patch.object(self.repository, "materialize_current"), \
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
        self.assertIn("-filters=*", command)
        self.assertEqual(
            "/tmp/code-version-manager-config",
            run.call_args.kwargs["env"]["XDG_CONFIG_HOME"],
        )
        prepare_config.assert_called_once()

    def test_bcompare_config_forces_saved_sessions_to_stay_collapsed(self):
        source_home = self.path / "source-config"
        source = source_home / "bcompare"
        source.mkdir(parents=True)
        sessions = source / "BCSessions.xml"
        sessions.write_text(
            """<?xml version="1.0" encoding="UTF-8"?>
<BCSessions Version="1" MinVersion="1">
  <TSessionFolder><Items><TDirCompareSession><Rules>
    <ExpandAllOnLoad Value="True"/>
    <ExpandDiffsOnLoad Value="True"/>
  </Rules></TDirCompareSession></Items></TSessionFolder>
</BCSessions>
""",
            encoding="utf-8",
        )
        with mock.patch.dict(MODULE.os.environ, {"XDG_CONFIG_HOME": str(source_home)}):
            config_home = MODULE.prepare_bcompare_config(self.path / "isolated")

        copied = MODULE.ET.parse(config_home / "bcompare" / "BCSessions.xml")
        self.assertEqual(
            ["False"],
            [node.get("Value") for node in copied.findall(".//ExpandAllOnLoad")],
        )
        self.assertEqual(
            ["False"],
            [node.get("Value") for node in copied.findall(".//ExpandDiffsOnLoad")],
        )
        original = MODULE.ET.parse(sessions)
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


if __name__ == "__main__":
    unittest.main()
