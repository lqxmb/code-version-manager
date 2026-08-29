#!/usr/bin/python3
import importlib.util
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path


SOURCE = Path(__file__).parents[1] / "src" / "fetch_version_manager.py"
SPEC = importlib.util.spec_from_file_location("fetch_version_manager", SOURCE)
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
        manager_changelog = self.path / MODULE.MANAGER_CHANGELOG
        self.assertTrue(manager_changelog.exists())
        self.assertIn("## 0.0.2 (", manager_changelog.read_text(encoding="utf-8"))
        downloaded_changelog = self.path / "CHANGELOG.rst"
        self.repository.download_changelog(downloaded_changelog)
        changelog_text = downloaded_changelog.read_text(encoding="utf-8")
        self.assertIn(f"Changelog for {self.path.name}", changelog_text)
        self.assertIn("## 0.0.2 (", changelog_text)
        self.assertNotIn("v0.0.2", changelog_text)
        self.assertIn("* second", changelog_text)
        self.assertIn("* 添加相机", changelog_text)
        self.assertIn("* 调整控制器", changelog_text)
        self.assertIn("## 0.0.1 (", changelog_text)
        archive = self.path / "v0.0.2.zip"
        self.repository.export("v0.0.2", archive)
        with zipfile.ZipFile(archive) as bundle:
            self.assertIn("robot.txt", bundle.namelist())
            self.assertNotIn("other.txt", bundle.namelist())
        snapshot = self.path / "snapshot"
        self.repository.materialize("v0.0.2", snapshot)
        self.assertTrue((snapshot / "robot.txt").exists())
        self.assertFalse((snapshot / "other.txt").exists())

    def test_detached_head_cannot_save(self):
        self.git("checkout", "-q", "v0.0.1")
        with self.assertRaises(MODULE.GitError):
            self.repository.save_version("v0.0.2", "blocked", ["robot.txt"])

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


if __name__ == "__main__":
    unittest.main()
