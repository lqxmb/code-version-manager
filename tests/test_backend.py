#!/usr/bin/python3
import importlib.util
import os
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
        self.git("branch", "-M", "main")
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
        self.assertEqual(".gitignore\nrobot.txt\n", manifest.read_text(encoding="utf-8"))
        self.assertEqual([".git", ".gitignore"], sorted(path.name for path in self.path.glob(".*")))
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

    def test_main_version_is_mandatory_and_ignores_old_optional_setting(self):
        self.repository.run(["config", "--local", "codeversionmanager.syncVersionFile", "false"])
        self.repository.save_version("v0.0.2", "mandatory version", ["robot.txt"])
        self.assertEqual("0.0.2\n", (self.path / "VERSION").read_text())
        self.assertNotIn("VERSION", self.repository.manifest("v0.0.2"))
        other = self.path / "other-workspace"
        other.mkdir()
        MODULE.create_repository(other)
        self.assertFalse((other / "VERSION").exists())

    def test_publish_syncs_existing_version_even_when_not_selected(self):
        version_path = self.path / "VERSION"
        version_path.write_text("0.0.1\n", encoding="utf-8")
        self.git("add", "-f", "VERSION")
        self.git("commit", "-q", "-m", "add VERSION")
        version_path.write_text("staged version\n", encoding="utf-8")
        (self.path / "other.txt").write_text("staged other\n", encoding="utf-8")
        self.git("add", "VERSION", "other.txt")
        staged_other = self.repository.run(["show", ":other.txt"])
        (self.path / "other.txt").write_text("unstaged other\n", encoding="utf-8")

        self.repository.save_version("v1.7.8.9.7", "new release", ["robot.txt"])

        self.assertEqual("1.7.8.9.7\n", version_path.read_text(encoding="utf-8"))
        self.assertNotIn("VERSION", self.repository.run(["ls-tree", "--name-only", "v1.7.8.9.7"]).splitlines())
        self.assertEqual("", self.repository.run(["ls-files", "--", "VERSION"]))
        self.assertEqual(staged_other, self.repository.run(["show", ":other.txt"]))
        self.assertEqual("unstaged other\n", (self.path / "other.txt").read_text(encoding="utf-8"))
        self.assertEqual([".gitignore", "robot.txt"], self.repository.manifest("v1.7.8.9.7"))
        archive = self.path / "release.zip"
        self.repository.export("v1.7.8.9.7", archive)
        with zipfile.ZipFile(archive) as bundle:
            self.assertNotIn("VERSION", bundle.namelist())

    def test_main_publication_always_creates_and_updates_version(self):
        for version in ("v0.0.2", "v0.0.3", "v0.0.4"):
            self.repository.save_version(version, "formal release", ["robot.txt"])
            self.assertEqual(version[1:] + "\n", (self.path / "VERSION").read_text())
            self.assertNotIn("VERSION", self.repository.manifest(version))

    def test_current_version_uses_nearest_history_tag_and_ignores_unrelated_tags(self):
        self.assertEqual("v0.0.1", self.repository.current_version_tag())
        self.repository.save_version("v0.0.2", "second", ["robot.txt"])
        self.repository.run(["tag", "external-note"])
        self.repository.run(["commit", "--allow-empty", "-m", "unpublished"])
        self.assertEqual("v0.0.2", self.repository.current_version_tag())
        self.repository.save_version("v0.0.0", "lower number, later commit", ["robot.txt"])
        self.assertEqual("v0.0.0", self.repository.current_version_tag())
        self.assertEqual("0.0.0\n", (self.path / "VERSION").read_text())

    def test_no_version_tags_refresh_clears_stale_version(self):
        self.repository.run(["tag", "-d", "v0.0.1"])
        self.repository.run(["tag", "unmanaged-tag"])
        self.assertIsNone(self.repository.current_version_tag())
        self.assertFalse((self.path / "VERSION").exists())
        (self.path / "VERSION").write_text("custom value\n")
        self.assertIsNone(self.repository.current_version_tag())
        self.repository.refresh_workspace_generated_files()
        self.assertFalse((self.path / "VERSION").exists())
        self.assertIn("尚无版本", (self.path / "CHANGELOG.rst").read_text())

    def test_failed_commit_restores_version_and_staged_files(self):
        hook = Path(self.repository.run(["rev-parse", "--absolute-git-dir"]).strip()) / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        for original in (None, b"manual draft\r\n"):
            with self.subTest(original=original):
                version_path = self.path / "VERSION"
                if original is not None:
                    version_path.write_bytes(original)
                    self.git("add", "-f", "VERSION")
                head = self.repository.run(["rev-parse", "HEAD"])
                index = self.repository.run(["ls-files", "--stage", "-z"])
                with self.assertRaises(MODULE.GitError):
                    self.repository.save_version(
                        "v0.0.2", "rejected", ["robot.txt"]
                    )
                self.assertEqual(head, self.repository.run(["rev-parse", "HEAD"]))
                self.assertEqual(index, self.repository.run(["ls-files", "--stage", "-z"]))
                self.assertNotIn("v0.0.2", self.repository.tags())
                self.assertEqual(original is not None, version_path.exists())
                if original is not None:
                    self.assertEqual(original, version_path.read_bytes())

    def test_version_sync_respects_ignores_and_does_not_follow_symlinks(self):
        version_path = self.path / "VERSION"
        (self.path / ".gitignore").write_text("/VERSION\n", encoding="utf-8")
        self.repository.save_version("v0.0.2", "local version", ["robot.txt"])
        self.assertEqual("0.0.2\n", version_path.read_text())
        self.assertNotIn("VERSION", self.repository.manifest("v0.0.2"))
        version_path.unlink()
        version_path.symlink_to(self.path / "other.txt")
        with self.assertRaisesRegex(MODULE.GitError, "普通文件"):
            self.repository.save_version("v0.0.3", "blocked", ["robot.txt"])
        self.assertEqual("other\n", (self.path / "other.txt").read_text(encoding="utf-8"))

    def test_publish_rejects_unsafe_changelog_target_without_changing_repository(self):
        changelog = self.path / "CHANGELOG.rst"
        external = self.path / "external.rst"
        external.write_text("preserve\n", encoding="utf-8")
        for create_target in (
            lambda: changelog.symlink_to(external),
            lambda: changelog.mkdir(),
            lambda: os.mkfifo(changelog),
        ):
            with self.subTest(target=changelog):
                create_target()
                before_refs = self.repository._local_refs()
                before_head = self.repository.run(["rev-parse", "HEAD"]).strip()
                before_index = self.repository.run(["ls-files", "--stage", "-z"])
                with self.assertRaisesRegex(MODULE.GitError, "CHANGELOG.rst"):
                    self.repository.save_version("v0.0.2", "blocked", ["robot.txt"])
                self.assertEqual(before_refs, self.repository._local_refs())
                self.assertEqual(before_head, self.repository.run(["rev-parse", "HEAD"]).strip())
                self.assertEqual(before_index, self.repository.run(["ls-files", "--stage", "-z"]))
                if changelog.is_dir():
                    changelog.rmdir()
                else:
                    changelog.unlink()
        self.assertEqual("preserve\n", external.read_text(encoding="utf-8"))

    def test_changelog_prepare_failure_does_not_publish(self):
        refs = self.repository._local_refs()
        index = self.repository.run(["ls-files", "--stage", "-z"])
        with mock.patch.object(
            self.repository, "_prepare_changelog_download", side_effect=MODULE.GitError("磁盘已满"),
        ):
            with self.assertRaisesRegex(MODULE.GitError, "磁盘已满"):
                self.repository.save_version("v0.0.2", "second", ["robot.txt"])
        self.assertEqual(refs, self.repository._local_refs())
        self.assertEqual(index, self.repository.run(["ls-files", "--stage", "-z"]))
        self.assertFalse((self.path / "VERSION").exists())

    def test_final_changelog_failure_reports_completed_release_and_preserves_notes(self):
        notes = self.path / "CHANGELOG.rst"
        notes.write_text("keep until download succeeds\n")
        with mock.patch.object(MODULE.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(MODULE.GitError, "已发布到本地.*自动下载.*失败"):
                self.repository.save_version("v0.0.2", "second", ["robot.txt"])
        self.assertIn("v0.0.2", self.repository.tags())
        self.assertEqual("keep until download succeeds\n", notes.read_text())
        self.assertEqual([], list(self.path.glob(".CHANGELOG.rst.*")))
        self.repository.download_changelog(notes)
        self.assertEqual(self.repository.changelog_text(), notes.read_text())

    def test_generated_changelog_preserves_file_permissions(self):
        notes = self.path / "CHANGELOG.rst"
        notes.write_text("old notes\n")
        notes.chmod(0o444)
        self.repository.save_version("v0.0.2", "second", ["robot.txt"])
        self.assertEqual(0o444, notes.stat().st_mode & 0o777)
        self.assertEqual(self.repository.changelog_text(), notes.read_text())

    def test_first_publication_rejects_unsafe_changelog_before_preparing_files(self):
        empty = self.path / "empty"
        empty.mkdir()
        repository = MODULE.create_repository(empty)
        (empty / "file.txt").write_text("content\n")
        (empty / "CHANGELOG.rst").symlink_to(self.path / "other.txt")
        before_config = repository._git_path("config").read_bytes()
        before_status = repository.run(["status", "--porcelain=v1", "-z"])
        with self.assertRaisesRegex(MODULE.GitError, "CHANGELOG.rst"):
            repository.save_version("v0.0.1", "first", ["file.txt"])
        self.assertEqual(before_config, repository._git_path("config").read_bytes())
        self.assertEqual(before_status, repository.run(["status", "--porcelain=v1", "-z"]))
        self.assertEqual({}, repository._local_refs())
        self.assertFalse((empty / "VERSION").exists())
        self.assertFalse((empty / ".gitignore").exists())
        self.assertEqual("other\n", (self.path / "other.txt").read_text())

    def test_publish_refreshes_workspace_changelog_without_including_it(self):
        notes = b"0.0.2 (2026-11-02)\n------------------\n* manual notes\n"
        changelog = self.path / "CHANGELOG.rst"
        changelog.write_bytes(notes)
        self.git("add", "CHANGELOG.rst")
        self.git("commit", "-q", "-m", "old tracked notes")

        self.repository.save_version("v0.0.2", "confirmed in dialog", ["robot.txt", "CHANGELOG.rst"])
        self.assertNotIn("CHANGELOG.rst", self.repository.run(["ls-tree", "-r", "--name-only", "v0.0.2"]).splitlines())
        self.assertNotIn("CHANGELOG.rst", self.repository.manifest("v0.0.2"))
        generated = changelog.read_text(encoding="utf-8")
        self.assertIn("0.0.2 (", generated)
        self.assertIn("* confirmed in dialog", generated)
        self.assertNotIn("* manual notes", generated)
        archive = self.path / "release.zip"
        self.repository.export("v0.0.2", archive)
        with zipfile.ZipFile(archive) as bundle:
            self.assertNotIn("CHANGELOG.rst", bundle.namelist())
        self.repository.update_version_message("v0.0.2", "edited description")
        self.repository.delete_version("v0.0.2")
        self.assertNotIn("0.0.2 (", changelog.read_text(encoding="utf-8"))
        self.assertEqual("0.0.1\n", (self.path / "VERSION").read_text())

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
            [".gitignore", "other.txt", "robot.txt"],
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
        self.assertTrue((other_root / "CHANGELOG.rst").exists())
        self.assertEqual("0.0.1\n", (other_root / "VERSION").read_text())
        self.assertEqual("CHANGELOG.rst\n", repository.run(["check-ignore", "CHANGELOG.rst"]))
        self.assertIn(".gitignore", repository.manifest("v0.0.1"))
        archive = other_root / "initial.zip"
        repository.export("v0.0.1", archive)
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            self.assertIn("src/CMakeLists.txt", names)
            self.assertIn("src/config/robot.yaml", names)
            self.assertIn("src/main.txt", names)
            self.assertNotIn("src/vendor/ignored.bin", names)
            self.assertIn(".gitignore", names)
            self.assertNotIn("CHANGELOG.rst", names)

    def test_initialize_refreshes_notes_and_preserves_existing_gitignore(self):
        root = self.path / "new-workspace"
        root.mkdir()
        (root / "keep.txt").write_text("keep\n", encoding="utf-8")
        notes = b"manual release notes\r\n"
        (root / "CHANGELOG.rst").write_bytes(notes)
        (root / ".gitignore").write_bytes(b"# custom rules\r\n*.tmp")
        repository = MODULE.initialize_repository(
            root, "v1.2.3", "initial", ["keep.txt", "CHANGELOG.rst"]
        )
        self.assertEqual(repository.changelog_text(), (root / "CHANGELOG.rst").read_text())
        self.assertIn("* initial", (root / "CHANGELOG.rst").read_text())
        self.assertEqual(b"# custom rules\r\n*.tmp\r\n/VERSION\r\n/CHANGELOG.rst\r\n", (root / ".gitignore").read_bytes())
        self.assertEqual("1.2.3\n", (root / "VERSION").read_text())
        self.assertEqual([".gitignore", "keep.txt"], repository.manifest("v1.2.3"))
        self.assertEqual([], repository.status())

    def test_first_publish_in_preconfigured_empty_repository_creates_reference_files(self):
        root = self.path / "configured-workspace"
        root.mkdir()
        (root / "keep.txt").write_text("keep\n", encoding="utf-8")
        (root / "VERSION").write_text("0.0.0\n", encoding="utf-8")
        repository = MODULE.create_repository(root)
        repository.save_version("v1.0.0", "first release", ["keep.txt"])
        self.assertTrue((root / "CHANGELOG.rst").exists())
        self.assertEqual("CHANGELOG.rst\n", repository.run(["check-ignore", "CHANGELOG.rst"]))
        self.assertEqual([".gitignore", "keep.txt"], repository.manifest("v1.0.0"))
        self.assertEqual("1.0.0\n", (root / "VERSION").read_text())
        self.assertEqual([], repository.status())

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


class GitDefaultsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_file = self.root / "config" / "settings.ini"

    def test_git_defaults_preserve_other_settings_and_validate_account(self):
        self.config_file.parent.mkdir(parents=True)
        self.config_file.write_text("[display]\ncompact = true\n", encoding="utf-8")

        MODULE.save_git_default_settings(
            "example-org", "Release User", "release@example.com", self.config_file,
        )

        self.assertEqual(
            ("example-org", "Release User", "release@example.com"),
            MODULE.git_default_settings(self.config_file),
        )
        parser = MODULE._read_settings(self.config_file)
        self.assertTrue(parser.getboolean("display", "compact"))
        with self.assertRaisesRegex(MODULE.GitError, "GitHub 账号"):
            MODULE.save_git_default_settings("bad/account", "", "", self.config_file)

    def test_ssh_example_uses_configured_account_or_placeholder(self):
        self.assertEqual(
            "git@github.com:example-org/m9_ws.git",
            MODULE.github_ssh_example("example-org", "m9_ws"),
        )
        self.assertEqual(
            "git@github.com:<你的GitHub账号>/m9_ws.git",
            MODULE.github_ssh_example("", "m9_ws"),
        )

    def test_new_repository_uses_defaults_and_blank_fields_fall_back(self):
        settings_path = mock.patch.object(
            MODULE, "settings_file_path", return_value=self.config_file,
        )
        settings_path.start()
        self.addCleanup(settings_path.stop)
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "file.txt").write_text("content\n", encoding="utf-8")
        MODULE.save_git_default_settings(
            "example-org", "Configured User", "configured@example.com", self.config_file,
        )
        repository = MODULE.initialize_repository(
            workspace, "v1.0.0", "initial", ["file.txt"], self.config_file,
        )
        self.assertEqual(
            "Configured User", repository.run(["config", "--local", "user.name"]).strip(),
        )
        self.assertEqual(
            "configured@example.com", repository.run(["config", "--local", "user.email"]).strip(),
        )

        fallback_workspace = self.root / "fallback-workspace"
        fallback_workspace.mkdir()
        fallback_repository = MODULE.create_repository(fallback_workspace)
        fallback_repository.run(["config", "--local", "user.name", "Existing User"])
        fallback_repository.run(["config", "--local", "user.email", "existing@example.com"])
        MODULE.save_git_default_settings("", "Configured Name", "", self.config_file)
        MODULE.ensure_git_identity(fallback_repository, self.config_file)
        self.assertEqual(
            "Configured Name", fallback_repository.run(["config", "--local", "user.name"]).strip(),
        )
        self.assertEqual(
            "existing@example.com", fallback_repository.run(["config", "--local", "user.email"]).strip(),
        )

    def test_identity_falls_back_to_local_user_when_git_has_no_identity(self):
        with mock.patch.object(MODULE, "_effective_git_config_value", return_value=""):
            with mock.patch.dict(MODULE.os.environ, {"USER": "fallback-user"}, clear=False):
                self.assertEqual(
                    ("fallback-user", "fallback-user@localhost"),
                    MODULE.effective_git_identity(config_file=self.config_file),
                )

    def test_git_account_identity_prefers_each_nonblank_default(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        repository = MODULE.create_repository(workspace)
        repository.run(["config", "user.name", "Repository User"])
        repository.run(["config", "user.email", "repository@example.com"])

        MODULE.save_git_default_settings(
            "", "Default User", "default@example.com", self.config_file,
        )
        self.assertEqual(
            ("Default User", "default@example.com"),
            MODULE.git_account_identity(repository, self.config_file),
        )

        MODULE.save_git_default_settings("", "Name Default", "", self.config_file)
        self.assertEqual(
            ("Name Default", "repository@example.com"),
            MODULE.git_account_identity(repository, self.config_file),
        )

        MODULE.save_git_default_settings("", "", "", self.config_file)
        self.assertEqual(
            ("Repository User", "repository@example.com"),
            MODULE.git_account_identity(repository, self.config_file),
        )

    def test_git_account_identity_does_not_write_defaults_until_saved(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        repository = MODULE.create_repository(workspace)
        repository.run(["config", "user.name", "Repository User"])
        repository.run(["config", "user.email", "repository@example.com"])
        MODULE.save_git_default_settings(
            "", "Default User", "default@example.com", self.config_file,
        )

        self.assertEqual(
            ("Default User", "default@example.com"),
            MODULE.git_account_identity(repository, self.config_file),
        )
        self.assertEqual(
            "Repository User", repository.run(["config", "--local", "user.name"]).strip(),
        )
        self.assertEqual(
            "repository@example.com",
            repository.run(["config", "--local", "user.email"]).strip(),
        )

        repository.configure_remote(
            str(self.root / "remote.git"), "Default User", "default@example.com",
        )
        self.assertEqual(
            "Default User", repository.run(["config", "--local", "user.name"]).strip(),
        )
        self.assertEqual(
            "default@example.com",
            repository.run(["config", "--local", "user.email"]).strip(),
        )


class BranchReleaseWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "workspace"
        self.path.mkdir()
        (self.path / "robot.txt").write_text("base\n", encoding="utf-8")
        self.repository = MODULE.initialize_repository(
            self.path, "v0.2.0", "base release", ["robot.txt"],
        )

    @staticmethod
    def git(path, *args):
        return subprocess.run(
            ["git", "-C", str(path), *args], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ).stdout.strip()

    def test_preview_tags_are_branch_owned_and_release_tags_are_shared_read_only_baselines(self):
        self.assertEqual("main", self.repository.current_branch())
        self.repository.create_branch("test1")
        self.assertEqual("branches/test1/v0.2.0-beta.0.0.1", self.repository.next_version())
        (self.path / "robot.txt").write_text("test preview\n", encoding="utf-8")
        self.repository.save_version("test1/v0.2.0-beta.0.0.1", "test preview", ["robot.txt"])
        self.assertEqual("branches/test1/v0.2.0-beta.0.0.2", self.repository.next_version())
        (self.path / "robot.txt").write_text("second test preview\n", encoding="utf-8")
        self.repository.save_version("test1/v0.2.0-beta.0.0.2", "second test preview", ["robot.txt"])
        preview_ref = "refs/tags/branches/test1/v0.2.0-beta.0.0.1"
        self.assertTrue(self.repository.run(["show-ref", "--verify", preview_ref]).strip())
        self.assertEqual("v0.2.0-beta.0.0.2", self.repository.version_tags()[0].name[len("branches/test1/"):])
        history = self.repository.history()
        self.assertEqual(
            {"v0.2.0", "branches/test1/v0.2.0-beta.0.0.1", "branches/test1/v0.2.0-beta.0.0.2"},
            {entry.tags[0] for entry in history},
        )
        baseline = next(entry for entry in history if entry.tags == ("v0.2.0",))
        self.assertTrue(baseline.read_only)

        self.repository.switch_branch("main")
        self.assertEqual("v0.2.1", self.repository.next_version())
        self.assertEqual(["v0.2.0"], [entry.tags[0] for entry in self.repository.history()])
        with self.assertRaisesRegex(MODULE.GitError, "test1"):
            self.repository.delete_version("v0.2.0")

        self.assertEqual("branches/feature%2Fcamera/v0.2.0-beta.0.0.1", MODULE.version_tag_name(
            "feature/camera", "v0.2.0-beta.0.0.1",
        ))
        self.assertEqual("feature/camera/v0.2.0-beta.0.0.1", MODULE.display_version(
            "branches/feature%2Fcamera/v0.2.0-beta.0.0.1",
        ))
        self.assertEqual(
            "feature/camera", MODULE.parse_version_tag("refs/tags/branches/feature%2Fcamera/v0.2.0-beta.0.0.1")[0],
        )
        self.assertEqual(
            ("test1", "v0.2.0-beta.1"),
            MODULE.parse_version_tag("branches/test1/v0.2.0-beta.1"),
        )
        self.assertEqual(1, MODULE.prerelease_sequence("v0.2.0-beta.0.0.1"))
        self.assertEqual(2, MODULE.prerelease_sequence("v0.2.0-beta.0.0.2"))

    def test_development_branch_requires_formal_base_and_rejects_new_legacy_format(self):
        self.repository.run(["tag", "-d", "v0.2.0"])
        with self.assertRaisesRegex(MODULE.GitError, "正式版本"):
            self.repository.create_branch("without-release")
        self.repository.run(["tag", "v0.2.0"])
        self.repository.create_branch("test1")
        with self.assertRaisesRegex(MODULE.GitError, "beta.0.0.序号"):
            self.repository.save_version("test1/v0.2.0-beta.1", "legacy format", ["robot.txt"])

    def test_development_branch_can_only_be_created_from_main(self):
        self.repository.create_branch("feature")
        with self.assertRaisesRegex(MODULE.GitError, "只能在当前检出的 main"):
            self.repository.create_branch("nested-feature")
        self.assertNotIn("refs/heads/nested-feature", self.repository._local_refs())

        self.repository.switch_branch("main")
        self.git(self.path, "checkout", "-q", "v0.2.0")
        with self.assertRaisesRegex(MODULE.GitError, "只能在当前检出的 main"):
            self.repository.create_branch("detached-feature")
        self.assertNotIn("refs/heads/detached-feature", self.repository._local_refs())

    def test_preview_sequence_sorts_numerically_and_reads_legacy_labels(self):
        self.repository.create_branch("test1")
        self.assertEqual("v0.2.0", self.repository.current_version_tag())
        self.repository.save_version(
            "test1/v0.2.0-beta.0.0.1", "preview one", ["robot.txt", "VERSION"]
        )
        self.assertEqual("test1/v0.2.0-beta.0.0.1\n", (self.path / "VERSION").read_text())
        self.assertNotIn("VERSION", self.repository.run([
            "ls-tree", "--name-only", "branches/test1/v0.2.0-beta.0.0.1",
        ]).splitlines())
        legacy = "branches/test1/v0.2.0-beta.9"
        self.repository.run(["tag", "-a", legacy, "-m", "legacy nine"])
        version = "branches/test1/v0.2.0-beta.0.0.10"
        self.assertEqual(version, self.repository.next_version())
        self.repository.save_version(version, "preview ten", ["robot.txt", "VERSION"])
        self.assertEqual("test1/v0.2.0-beta.0.0.10\n", (self.path / "VERSION").read_text())
        self.assertEqual(
            self.repository.changelog_text(), (self.path / "CHANGELOG.rst").read_text(),
        )
        history = [entry.tags[0] for entry in self.repository.history()]
        self.assertLess(history.index(version), history.index(legacy))
        self.assertIn("test1/v0.2.0-beta.0.0.10 (", self.repository.changelog_text())
        self.repository.update_version_message(legacy, "edited legacy nine")
        self.assertEqual("edited legacy nine", self.repository.version_message(legacy))

        archive = self.root / "preview.zip"
        self.repository.export(version, archive)
        with zipfile.ZipFile(archive) as bundle:
            self.assertNotIn("VERSION", bundle.namelist())
        notes = "test1/v0.2.0-beta.0.0.10 (2026-11-03)"
        (self.path / "CHANGELOG.rst").write_text(
            f"{notes}\n{'-' * len(notes)}\n* preview notes\n", encoding="utf-8",
        )
        self.assertEqual("preview notes", MODULE.workspace_version_message(self.path, version))
        remote = self.root / "origin.git"
        self.git(self.root, "init", "--bare", "-q", str(remote))
        self.repository.configure_remote(str(remote), "Test User", "test@example.com")
        snapshot = self.repository.sync_snapshot(scope="all")
        status = self.repository.remote_sync_status(snapshot)
        self.repository.sync_remote_from_local(snapshot, status)
        self.assertIn(version, self.git(remote, "tag").splitlines())
        self.assertIn(legacy, self.git(remote, "tag").splitlines())

        self.repository.switch_branch("main")
        self.assertEqual("0.2.0\n", (self.path / "VERSION").read_text())
        self.repository.save_version("v0.3.0", "new formal", ["robot.txt"])
        self.repository.switch_branch("test1")
        self.assertEqual("test1/v0.2.0-beta.0.0.10\n", (self.path / "VERSION").read_text())
        self.repository.run(["commit", "--allow-empty", "-m", "unpublished work"])
        self.assertEqual(version, self.repository.current_version_tag())
        self.assertEqual("test1/v0.2.0-beta.0.0.10\n", (self.path / "VERSION").read_text())
        self.repository.switch_branch("main")
        self.assertEqual("0.3.0\n", (self.path / "VERSION").read_text())
        self.repository.switch_branch("test1")
        self.assertEqual("branches/test1/v0.2.0-beta.0.0.11", self.repository.next_version())
        self.repository.switch_branch("main")
        self.repository.create_branch("test2")
        self.assertEqual("branches/test2/v0.3.0-beta.0.0.1", self.repository.next_version())

    def test_beta_version_uses_unencoded_branch_name_and_restores_after_failed_commit(self):
        self.repository.create_branch("feature/camera", "v0.2.0")
        version_file = self.path / "VERSION"
        notes = self.path / "CHANGELOG.rst"
        original_notes = notes.read_bytes()
        version_file.write_text("staged draft\n")
        self.repository.run(["add", "-f", "VERSION"])
        version_file.write_text("unstaged draft\n")
        before_index = self.repository.run(["ls-files", "--stage", "-z"])
        before_refs = self.repository._local_refs()
        hook = self.repository._git_path("hooks/pre-commit")
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        version = "branches/feature%2Fcamera/v0.2.0-beta.0.0.1"
        with self.assertRaises(MODULE.GitError):
            self.repository.save_version(version, "preview", ["robot.txt"])
        self.assertEqual(before_index, self.repository.run(["ls-files", "--stage", "-z"]))
        self.assertEqual(before_refs, self.repository._local_refs())
        self.assertEqual("unstaged draft\n", version_file.read_text())
        self.assertEqual(original_notes, notes.read_bytes())
        self.assertEqual([], list(self.path.glob(".CHANGELOG.rst.*")))

        hook.unlink()
        self.repository.save_version(version, "preview", ["robot.txt"])
        self.assertEqual("feature/camera/v0.2.0-beta.0.0.1\n", version_file.read_text())
        self.assertEqual(version, self.repository.current_version_tag())
        self.assertNotIn("VERSION", self.repository.manifest(version))
        self.assertEqual([], self.repository.status())

    def test_manual_merge_isolated_until_single_parent_commit_and_optional_cleanup(self):
        repo = self.repository
        repo.create_branch("test1")
        (self.path / "robot.txt").write_text("branch edit\n")
        repo.save_version("test1/v0.2.0-beta.0.0.1", "feature", ["robot.txt"])
        source_oid = repo.run(["rev-parse", "HEAD"]).strip()
        repo.switch_branch("main")
        (self.path / "robot.txt").write_text("main edit\n")
        repo.save_version("v0.3.0", "formal", ["robot.txt"])
        main_oid = repo.run(["rev-parse", "HEAD"]).strip()
        tags = repo.tags()
        refs = dict(repo._local_refs())

        session = repo.merge_to_main("test1")
        root, _data = repo._review_session_data(session)
        self.assertEqual("main", repo.current_branch())
        self.assertEqual(main_oid, repo.run(["rev-parse", "HEAD"]).strip())
        self.assertEqual(refs, repo._local_refs())
        self.assertFalse(repo._git_path("MERGE_HEAD").exists())
        self.assertEqual("main edit\n", (self.path / "robot.txt").read_text())
        (root / "main" / "robot.txt").write_text("manual result\n")

        original = MODULE.subprocess.run
        def close_tool(command, **options):
            if command[0] == "git":
                return original(command, **options)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        with mock.patch.object(MODULE.shutil, "which", return_value="/tool"), \
                mock.patch.object(MODULE.subprocess, "run", side_effect=close_tool):
            result = repo.launch_merge_comparison(session)
        self.assertEqual((("robot.txt", "更新"),), result.changes)
        repo.complete_merge(delete_source=False)

        parents = repo.run(["rev-list", "--parents", "-n", "1", "HEAD"]).split()
        self.assertEqual(2, len(parents))
        self.assertEqual(main_oid, parents[1])
        self.assertNotEqual(source_oid, parents[1])
        self.assertEqual("manual result\n", (self.path / "robot.txt").read_text())
        self.assertEqual("0.3.0\n", (self.path / "VERSION").read_text())
        self.assertEqual(tags, repo.tags())
        self.assertIn("refs/heads/test1", repo._local_refs())

    def test_delete_unmerged_or_current_branch_and_beta_tags_with_recovery(self):
        repo = self.repository
        repo.create_branch("test1")
        (self.path / "feature.txt").write_text("unmerged feature\n")
        repo.save_version("test1/v0.2.0-beta.0.0.1", "feature", ["feature.txt"])
        beta = MODULE.canonical_version("test1/v0.2.0-beta.0.0.1")
        repo.delete_branch("test1", expected=repo.plan_branch_deletion("test1"))
        self.assertNotIn("refs/heads/test1", repo._local_refs())
        self.assertNotIn(beta, repo.tags())
        self.assertTrue(repo.run(["for-each-ref", "--format=%(refname)", "refs/code-version-manager/recovery/"]).strip())
        self.assertEqual("main", repo.current_branch())

        repo.create_branch("current-feature")
        repo.switch_branch("current-feature")
        result = repo.delete_branch("current-feature")
        self.assertIn("current-feature", result)
        self.assertEqual("main", repo.current_branch())
        with self.assertRaisesRegex(MODULE.GitError, "main 分支不能删除"):
            repo.plan_branch_deletion("main")

    def test_delete_noncurrent_branch_preserves_dirty_worktree_and_switch_stays_blocked(self):
        repo = self.repository
        repo.create_branch("test1")
        (self.path / "feature.txt").write_text("feature\n", encoding="utf-8")
        beta_name = "test1/v0.2.0-beta.0.0.1"
        repo.save_version(beta_name, "feature", ["feature.txt"])
        beta = MODULE.canonical_version(beta_name)
        manifest = repo.metadata_path / MODULE.MANIFEST_DIRECTORY / f"{beta}{MODULE.MANIFEST_SUFFIX}"
        self.assertTrue(manifest.is_file())
        repo.switch_branch("main")

        repo.create_branch("switch-target")
        repo.switch_branch("main")
        (self.path / "robot.txt").write_text("staged\n", encoding="utf-8")
        repo.run(["add", "robot.txt"])
        (self.path / "robot.txt").write_text("unstaged\n", encoding="utf-8")
        (self.path / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        status_before = repo.run(["status", "--porcelain=v1", "-z", "--untracked-files=all"])
        with self.assertRaisesRegex(MODULE.GitError, "工作区有尚未发布的修改"):
            repo.switch_branch("switch-target")
        self.assertEqual("main", repo.current_branch())

        plan = repo.plan_branch_deletion("test1")
        self.assertEqual("test1", plan.name)
        result = repo.delete_branch("test1", expected=plan)
        self.assertIn("恢复点", result)
        self.assertEqual("main", repo.current_branch())
        self.assertEqual("staged\n", repo.run(["show", ":robot.txt"]))
        self.assertEqual("unstaged\n", (self.path / "robot.txt").read_text(encoding="utf-8"))
        self.assertEqual("untracked\n", (self.path / "untracked.txt").read_text(encoding="utf-8"))
        self.assertEqual(status_before, repo.run(["status", "--porcelain=v1", "-z", "--untracked-files=all"]))
        self.assertNotIn("refs/heads/test1", repo._local_refs())
        self.assertNotIn(beta, repo.tags())
        self.assertFalse(manifest.exists())
        self.assertTrue(any(path.name == manifest.name for path in (repo.metadata_path / "recovery").rglob("*.manifest")))
        self.assertTrue(repo.run(["for-each-ref", "--format=%(refname)", "refs/code-version-manager/recovery/"]).strip())

    def test_delete_current_branch_requires_clean_worktree_and_other_worktree_use_is_blocked(self):
        repo = self.repository
        repo.create_branch("test1")
        repo.switch_branch("test1")
        (self.path / "robot.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(MODULE.GitError, "工作区有尚未发布的修改"):
            repo.plan_branch_deletion("test1")
        with self.assertRaisesRegex(MODULE.GitError, "工作区有尚未发布的修改"):
            repo.delete_branch("test1")
        self.assertEqual("test1", repo.current_branch())
        self.assertIn("refs/heads/test1", repo._local_refs())

        (self.path / "robot.txt").write_text("base\n", encoding="utf-8")
        repo.switch_branch("main")
        worktree = self.root / "other-worktree"
        repo.run(["worktree", "add", str(worktree), "test1"])
        with self.assertRaisesRegex(MODULE.GitError, "另一个工作区使用"):
            repo.plan_branch_deletion("test1")
        repo.run(["worktree", "remove", str(worktree)])
        repo.delete_branch("test1")
        self.assertNotIn("refs/heads/test1", repo._local_refs())

    def legacy_test_merge_keeps_main_formal_version_and_merges_branch_history(self):
        repo = self.repository
        repo.create_branch("test1")
        (self.path / "robot.txt").write_text("feature side\n")
        repo.save_version("test1/v0.2.0-beta.0.0.1", "feature", ["robot.txt"])
        (self.path / "VERSION").write_text("0.2.0-beta.1\n")
        repo.run(["add", "VERSION"])
        repo.run(["commit", "-m", "legacy beta version"])
        repo.run(["tag", "-a", "branches/test1/v0.2.0-beta.0.0.2", "-m", "legacy"])
        feature = repo.run(["rev-parse", "HEAD"]).strip()
        repo.switch_branch("main")
        (self.path / "robot.txt").write_text("main side\n")
        repo.save_version("v0.3.0", "main release", ["robot.txt"])
        oid = repo.run(["rev-parse", "v0.3.0"]).strip()
        message = repo.merge_to_main("test1")
        self.assertIn("VERSION 保留 main 的 0.3.0", message)
        self.assertEqual("0.3.0\n", (self.path / "VERSION").read_text())
        self.assertEqual(("robot.txt",), repo.merge_state().conflicts)
        with self.assertRaisesRegex(MODULE.GitError, "未解决"):
            repo.complete_merge()
        (self.path / "robot.txt").write_text("main side\nfeature side\n")
        repo.resolve_merge_file("robot.txt")
        repo.complete_merge()
        self.assertEqual(oid, repo.run(["rev-parse", "v0.3.0"]).strip())
        self.assertEqual("0.3.0\n", repo.run(["show", "main:VERSION"]))
        self.git(self.path, "merge-base", "--is-ancestor", feature, "main")
        self.assertNotIn("v0.3.1", repo.tags())

    def legacy_test_merge_handles_both_branches_adding_version_and_can_abort(self):
        self.repository.create_branch("branch1")
        (self.path / "robot.txt").write_text("branch1 side\n", encoding="utf-8")
        (self.path / "branch1.txt").write_text("feature\n", encoding="utf-8")
        self.repository.save_version(
            "branch1/v0.2.0-beta.0.0.1", "preview", ["robot.txt", "branch1.txt"]
        )
        self.repository.switch_branch("main")
        (self.path / "robot.txt").write_text("main side\n", encoding="utf-8")
        self.repository.save_version("v0.3.0", "formal", ["robot.txt"])
        head = self.repository.run(["rev-parse", "HEAD"])

        message = self.repository.merge_to_main("branch1")

        self.assertIn("VERSION 保留 main 的", message)
        self.assertEqual(("robot.txt",), self.repository.merge_state().conflicts)
        self.assertEqual("0.3.0\n", (self.path / "VERSION").read_text())
        self.assertEqual("feature\n", (self.path / "branch1.txt").read_text())
        with self.assertRaisesRegex(MODULE.GitError, "未完成"):
            self.repository.sync_snapshot()
        self.repository.abort_merge()
        self.assertEqual(head, self.repository.run(["rev-parse", "HEAD"]))
        self.assertEqual("0.3.0\n", (self.path / "VERSION").read_text())
        self.assertFalse((self.path / "branch1.txt").exists())

    def legacy_test_legacy_merge_does_not_introduce_beta_version_to_main(self):
        self.repository.run(["rm", "VERSION"])
        self.repository.run(["commit", "-m", "legacy main without VERSION"])
        self.repository.run(["tag", "-f", "-a", "v0.2.0", "-m", "legacy main"])
        self.repository.create_branch("test1")
        (self.path / "VERSION").write_text("0.2.0-beta.1\n")
        self.repository.run(["add", "VERSION"])
        self.repository.run(["commit", "-m", "legacy beta VERSION"])
        self.repository.run(["tag", "-a", "branches/test1/v0.2.0-beta.1", "-m", "legacy beta"])
        self.repository.switch_branch("main")
        result = self.repository.merge_to_main("test1")
        self.assertIn("合并已完成", result)
        self.assertFalse((self.path / "VERSION").exists())
        self.assertNotIn("v0.2.1", self.repository.tags())

    def legacy_test_merge_keeps_newer_main_version_when_source_still_has_inherited_formal_number(self):
        self.repository.create_branch("test1")
        (self.path / "feature.txt").write_text("feature\n", encoding="utf-8")
        self.repository.save_version(
            "test1/v0.2.0-beta.0.0.1", "feature", ["feature.txt"]
        )
        self.repository.switch_branch("main")
        self.repository.save_version("v0.3.0", "main release", ["robot.txt"])

        self.repository.merge_to_main("test1")

        self.assertFalse(self.repository.merge_state().active)
        self.assertEqual("0.3.0\n", (self.path / "VERSION").read_text())
        self.assertTrue((self.path / "feature.txt").is_file())

    def legacy_test_legacy_custom_version_does_not_replace_main_formal_version(self):
        self.repository.create_branch("test1")
        (self.path / "VERSION").write_text("nightly-custom\n")
        (self.path / "feature.txt").write_text("feature\n")
        self.repository.run(["add", "VERSION", "feature.txt"])
        self.repository.run(["commit", "-m", "legacy custom version"])
        self.repository.run(["tag", "-a", "branches/test1/v0.2.0-beta.1", "-m", "legacy"])
        self.repository.switch_branch("main")
        self.repository.merge_to_main("test1")
        self.assertFalse(self.repository.merge_state().active)
        self.assertEqual("0.2.0\n", (self.path / "VERSION").read_text())
        self.assertTrue((self.path / "feature.txt").exists())
        self.assertNotIn("v0.2.1", self.repository.tags())

    def legacy_test_merge_does_not_automatically_resolve_symlink_version_file(self):
        self.repository.save_version("v0.2.1", "main version", ["robot.txt"])
        self.repository.create_branch("test1")
        (self.path / "VERSION").unlink()
        (self.path / "VERSION").symlink_to("robot.txt")
        self.repository.run(["add", "VERSION"])
        self.repository.run(["commit", "-m", "legacy symlink VERSION"])
        self.repository.run(["tag", "-a", "branches/test1/v0.2.1-beta.0.0.1", "-m", "legacy"])
        self.repository.switch_branch("main")

        self.repository.merge_to_main("test1")

        self.assertEqual(("VERSION",), self.repository.merge_state().conflicts)
        self.assertEqual("base\n", (self.path / "robot.txt").read_text())
        self.repository.abort_merge()
        self.assertFalse((self.path / "VERSION").is_symlink())
        self.assertEqual("0.2.1\n", (self.path / "VERSION").read_text())

    def legacy_test_merge_preserves_labels_until_formal_publication_and_explicit_cleanup(self):
        self.repository.create_branch("test1")
        (self.path / "feature.txt").write_text("feature\n")
        self.repository.save_version("test1/v0.2.0-beta.0.0.1", "feature", ["feature.txt"])
        beta = self.repository.run(["rev-parse", "HEAD"]).strip()
        self.repository.switch_branch("main")
        self.repository.save_version("v0.3.0", "formal", ["robot.txt"])
        tags = self.repository.tags()
        self.repository.merge_to_main("test1")
        self.assertEqual(tags, self.repository.tags())
        self.git(self.path, "merge-base", "--is-ancestor", beta, "main")
        with self.assertRaisesRegex(MODULE.GitError, "发布"):
            self.repository.plan_branch_deletion("test1")
        self.repository.save_version("v0.3.1", "merged formal", ["robot.txt", "feature.txt"])
        plan = self.repository.plan_branch_deletion("test1")
        self.assertIn("refs/tags/branches/test1/v0.2.0-beta.0.0.1", dict(plan.local_refs))
        self.repository.delete_merged_branch("test1", expected=plan)
        self.assertNotIn("test1", [branch.name for branch in self.repository.local_branches()])
        self.assertNotIn("branches/test1/v0.2.0-beta.0.0.1", self.repository.tags())
        self.assertIn("v0.2.0", self.repository.tags())
        self.git(self.path, "merge-base", "--is-ancestor", beta, "main")

    def legacy_test_merge_conflicts_block_switch_and_publish_until_resolved_and_can_abort(self):
        self.repository.create_branch("test1")
        (self.path / "robot.txt").write_text("test side\n", encoding="utf-8")
        self.repository.save_version("test1/v0.2.0-beta.0.0.1", "test edit", ["robot.txt"])
        test_head = self.repository.run(["rev-parse", "HEAD"]).strip()
        self.repository.switch_branch("main")
        (self.path / "robot.txt").write_text("main side\n", encoding="utf-8")
        self.repository.save_version("v0.2.1", "main edit", ["robot.txt"])
        main_head = self.repository.run(["rev-parse", "HEAD"]).strip()

        message = self.repository.merge_to_main("test1")

        self.assertIn("冲突", message)
        state = self.repository.merge_state()
        self.assertTrue(state.active)
        self.assertEqual(("robot.txt",), state.conflicts)
        with self.assertRaisesRegex(MODULE.GitError, "未完成"):
            self.repository.switch_branch("test1")
        with self.assertRaisesRegex(MODULE.GitError, "未完成"):
            self.repository.save_version("v0.2.2", "blocked", ["robot.txt"])
        with self.assertRaisesRegex(MODULE.GitError, "未解决"):
            self.repository.complete_merge()

        (self.path / "robot.txt").write_text("merged result\n", encoding="utf-8")
        self.repository.resolve_merge_file("robot.txt")
        self.assertTrue(self.repository.merge_state().ready)
        self.repository.complete_merge()
        self.assertEqual("merged result\n", (self.path / "robot.txt").read_text())
        self.assertFalse(self.repository.merge_state().active)
        self.git(self.path, "merge-base", "--is-ancestor", test_head, "main")

        self.repository.switch_branch("test1")
        (self.path / "robot.txt").write_text("another test side\n", encoding="utf-8")
        self.repository.save_version("test1/v0.2.0-beta.0.0.2", "second test edit", ["robot.txt"])
        self.repository.switch_branch("main")
        self.repository.run(["checkout", "-q", main_head, "--", "robot.txt"])
        (self.path / "robot.txt").write_text("another main side\n", encoding="utf-8")
        self.repository.save_version("v0.2.2", "second main edit", ["robot.txt"])
        before_abort = self.repository.run(["rev-parse", "HEAD"]).strip()
        self.repository.merge_to_main("test1")
        self.assertTrue(self.repository.merge_state().active)
        self.repository.abort_merge()
        self.assertFalse(self.repository.merge_state().active)
        self.assertEqual(before_abort, self.repository.run(["rev-parse", "HEAD"]).strip())

    def legacy_test_merge_is_local_and_does_not_implicitly_fast_forward_remote_changes(self):
        remote = self.root / "origin.git"
        self.git(self.root, "init", "--bare", "-q", str(remote))
        self.repository.configure_remote(str(remote), "Test", "test@example.com")
        self.repository.sync_remote()
        self.repository.create_branch("test1")
        (self.path / "feature.txt").write_text("feature\n")
        self.repository.save_version("test1/v0.2.0-beta.0.0.1", "feature", ["feature.txt"])
        self.repository.switch_branch("main")
        peer = self.root / "peer"
        self.git(self.root, "clone", "-q", "--branch", "main", str(remote), str(peer))
        self.git(peer, "config", "user.name", "Peer")
        self.git(peer, "config", "user.email", "peer@example.com")
        (peer / "remote.txt").write_text("remote only\n")
        self.git(peer, "add", "remote.txt")
        self.git(peer, "commit", "-q", "-m", "remote only")
        self.git(peer, "push", "-q", "origin", "main")
        remote_head = self.git(remote, "rev-parse", "main")
        with mock.patch.object(self.repository, "_run_remote", side_effect=AssertionError("merge contacted remote")):
            self.repository.merge_to_main("test1")
        self.assertTrue((self.path / "feature.txt").exists())
        self.assertFalse((self.path / "remote.txt").exists())
        self.assertEqual(remote_head, self.git(remote, "rev-parse", "main"))

    def legacy_test_whole_sync_propagates_branch_and_beta_cleanup_after_formal_publication(self):
        remote = self.root / "origin.git"
        self.git(self.root, "init", "--bare", "-q", str(remote))
        self.repository.configure_remote(str(remote), "Test", "test@example.com")
        self.repository.create_branch("test1")
        (self.path / "feature.txt").write_text("feature\n")
        self.repository.save_version("test1/v0.2.0-beta.0.0.1", "feature", ["feature.txt"])
        self.repository.sync_remote()
        self.assertIn("refs/heads/main", self.git(remote, "for-each-ref", "--format=%(refname)"))
        self.repository.merge_to_main("test1")
        self.repository.save_version("v0.2.1", "merged formal", ["robot.txt", "feature.txt"])
        self.repository.delete_merged_branch("test1", expected=self.repository.plan_branch_deletion("test1"))
        self.assertTrue(self.git(remote, "show-ref", "--verify", "refs/heads/test1"))
        self.repository.sync_remote()
        self.assertNotIn("refs/heads/test1", self.git(remote, "for-each-ref", "--format=%(refname)"))
        self.assertEqual(["v0.2.0", "v0.2.1"], self.git(remote, "tag").splitlines())

    def test_whole_sync_can_unify_shared_tags_but_manual_delete_remains_protected(self):
        remote = self.root / "origin.git"
        self.git(self.root, "init", "--bare", "-q", str(remote))
        self.repository.configure_remote(str(remote), "Test User", "test@example.com")
        self.repository.sync_remote()
        self.repository.create_branch("test1")
        self.repository.switch_branch("main")
        peer = self.root / "peer"
        self.git(self.root, "clone", "-q", "--branch", "main", str(remote), str(peer))
        self.git(peer, "config", "user.name", "Peer")
        self.git(peer, "config", "user.email", "peer@example.com")
        (peer / "robot.txt").write_text("remote update\n", encoding="utf-8")
        self.git(peer, "add", "robot.txt")
        self.git(peer, "commit", "-q", "-m", "remote release update")
        self.git(peer, "tag", "-f", "-a", "v0.2.0", "-m", "rewritten remote label")
        self.git(peer, "push", "-q", "--force", "origin", "main", "refs/tags/v0.2.0")

        snapshot = self.repository.sync_snapshot(scope="all")
        status = self.repository.remote_sync_status(snapshot)

        self.assertTrue(status.can_sync_remote, status.remote_message)
        self.assertTrue(status.can_sync_local, status.local_message)
        with self.assertRaisesRegex(MODULE.GitError, "test1"):
            self.repository.delete_version("v0.2.0")

    def test_remote_branch_import_then_whole_sync_fetches_all_branches_and_versions(self):
        remote = self.root / "origin.git"
        self.git(self.root, "init", "--bare", "-q", str(remote))
        self.repository.configure_remote(str(remote), "Test User", "test@example.com")
        self.repository.sync_remote()
        self.repository.create_branch("feature/camera")
        (self.path / "camera.txt").write_text("camera\n", encoding="utf-8")
        self.repository.save_version("feature/camera/v0.2.0-beta.0.0.1", "camera preview", ["camera.txt"])
        self.repository.sync_remote()
        self.repository.switch_branch("main")
        (self.path / "robot.txt").write_text("main progress\n", encoding="utf-8")
        self.repository.save_version("v0.3.0", "main progress", ["robot.txt"])
        self.repository.sync_remote()

        imported = self.root / "imported"
        imported.mkdir()
        repo = MODULE.create_repository(imported)
        repo.configure_remote(str(remote), "Test User", "test@example.com")
        branch = next(branch for branch in repo.remote_branches() if branch.name == "feature/camera")
        repo.switch_branch(branch.name, source="remote", expected_oid=branch.oid)

        self.assertEqual("feature/camera", repo.current_branch())
        self.assertIn("v0.2.0", repo.tags())
        self.assertNotIn("v0.3.0", repo.tags())
        self.assertEqual(["branches/feature%2Fcamera/v0.2.0-beta.0.0.1", "v0.2.0"], [entry.tags[0] for entry in repo.history()])
        self.assertEqual("feature/camera/v0.2.0-beta.0.0.1\n", (imported / "VERSION").read_text())
        notes = (imported / "CHANGELOG.rst").read_text()
        self.assertIn("camera preview", notes)
        self.assertNotIn("0.3.0 (", notes)
        snapshot = repo.sync_snapshot(scope="all")
        status = repo.remote_sync_status(snapshot)
        self.assertTrue(status.can_sync_local, status.local_message)
        repo.sync_local_from_remote(snapshot, status)
        self.assertIn("branches/feature%2Fcamera/v0.2.0-beta.0.0.1", repo.tags())
        self.assertIn("v0.2.0", repo.tags())
        self.assertIn("v0.3.0", repo.tags())
        self.assertIn("main", [branch.name for branch in repo.local_branches()])
        self.assertEqual("origin/feature/camera", self.git(imported, "rev-parse", "--abbrev-ref", "@{u}"))

    def test_whole_mirror_can_restore_or_delete_a_missing_shared_formal_tag(self):
        remote = self.root / "origin.git"
        self.git(self.root, "init", "--bare", "-q", str(remote))
        self.repository.configure_remote(str(remote), "Test User", "test@example.com")
        self.repository.sync_remote()
        self.repository.create_branch("test1")
        self.repository.switch_branch("main")
        self.git(self.path, "tag", "-d", "v0.2.0")

        snapshot = self.repository.sync_snapshot(scope="all")
        status = self.repository.remote_sync_status(snapshot)

        self.assertTrue(status.can_sync_local, status.local_message)
        self.assertTrue(status.can_sync_remote, status.remote_message)
        self.repository.sync_local_from_remote(snapshot, status)
        self.assertIn("v0.2.0", self.repository.tags())
        self.assertNotIn("test1", [branch.name for branch in self.repository.local_branches()])

    def legacy_test_merge_tools_receive_two_directories_and_require_explicit_application(self):
        self.repository.create_branch("test1")
        (self.path / "robot.txt").write_text("incoming side\n")
        self.repository.save_version("test1/v0.2.0-beta.0.0.1", "incoming", ["robot.txt"])
        self.repository.switch_branch("main")
        (self.path / "robot.txt").write_text("main side\n")
        self.repository.save_version("v0.2.1", "main", ["robot.txt"])
        self.repository.merge_to_main("test1")
        session = self.repository.prepare_merge_comparison()
        directory, _data = self.repository._review_session_data(session)
        original_run = MODULE.subprocess.run
        calls = []
        def run_tool(command, **kwargs):
            if command[0] == "git":
                return original_run(command, **kwargs)
            calls.append(command[0])
            self.assertIn(str(directory / "branch"), command)
            self.assertIn(str(directory / "main"), command)
            self.assertEqual("incoming side\n", (directory / "branch" / "robot.txt").read_text())
            (directory / "main" / "robot.txt").write_text("resolved by tool\n")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        for tool in ("bcompare", "meld", "kdiff3"):
            self.repository.configure_compare_tool(tool)
            with mock.patch.object(MODULE.shutil, "which", return_value="/tool"), \
                    mock.patch.object(MODULE.subprocess, "run", side_effect=run_tool):
                result = self.repository.launch_merge_comparison(session)
            self.assertIn("<<<<<<<", (self.path / "robot.txt").read_text())
            self.assertEqual(("robot.txt",), self.repository.merge_state().conflicts)
        self.assertEqual(["bcompare", "meld", "kdiff3"], calls)
        self.repository.apply_merge_comparison(result)
        self.repository.resolve_merge_file("robot.txt")
        self.repository.complete_merge()

    def legacy_test_directory_comparison_accepts_add_add_conflicts(self):
        self.repository.create_branch("test1")
        (self.path / "shared.txt").write_text("feature copy\n")
        self.repository.save_version("test1/v0.2.0-beta.0.0.1", "feature", ["shared.txt"])
        self.repository.switch_branch("main")
        (self.path / "shared.txt").write_text("main copy\n")
        self.repository.save_version("v0.2.1", "main", ["shared.txt"])
        self.repository.merge_to_main("test1")
        session = self.repository.prepare_merge_comparison()
        root, _data = self.repository._review_session_data(session)
        self.assertEqual("feature copy\n", (root / "branch" / "shared.txt").read_text())
        self.assertEqual("main copy\n", (root / "main" / "shared.txt").read_text())
        (root / "main" / "shared.txt").write_text("combined\n")
        self.repository.apply_merge_comparison(self.repository.merge_comparison_result(session))
        self.assertEqual(("shared.txt",), self.repository.merge_state().conflicts)
        self.repository.resolve_merge_file("shared.txt")
        self.repository.complete_merge()

    def legacy_test_directory_comparison_accepts_modify_delete_conflicts(self):
        for deleted_side in ("main", "test1"):
            with self.subTest(deleted_side=deleted_side):
                path = self.root / ("deleted-" + deleted_side)
                path.mkdir()
                (path / "file.txt").write_text("base\n")
                repository = MODULE.initialize_repository(path, "v0.2.0", "base", ["file.txt"])
                repository.create_branch("test1")
                if deleted_side == "test1":
                    repository.run(["rm", "file.txt"])
                    repository.run(["commit", "-m", "feature removes file"])
                else:
                    (path / "file.txt").write_text("feature edit\n")
                    repository.save_version("test1/v0.2.0-beta.0.0.1", "feature", ["file.txt"])
                repository.switch_branch("main")
                if deleted_side == "main":
                    repository.run(["rm", "file.txt"])
                    repository.run(["commit", "-m", "main removes file"])
                else:
                    (path / "file.txt").write_text("main edit\n")
                    repository.save_version("v0.2.1", "main", ["file.txt"])
                source = repository.run(["rev-parse", "test1"]).strip()
                main = repository.run(["rev-parse", "main"]).strip()
                repository.run(["tag", "-f", "-a", "branches/test1/v0.2.0-beta.0.0.1", "-m", "preview", source])
                repository.run(["tag", "-f", "-a", "v0.2.1", "-m", "main release", main])
                repository.merge_to_main("test1")
                session = repository.prepare_merge_comparison()
                directory, _data = repository._review_session_data(session)
                self.assertFalse((directory / ("main" if deleted_side == "main" else "branch") / "file.txt").exists())
                if deleted_side == "main":
                    (directory / "main" / "file.txt").write_text("restore feature\n")
                else:
                    (directory / "main" / "file.txt").unlink()
                repository.apply_merge_comparison(repository.merge_comparison_result(session))
                self.assertEqual(("file.txt",), repository.merge_state().conflicts)
                repository.resolve_merge_file("file.txt", delete=deleted_side == "test1")
                repository.complete_merge()
                self.assertEqual(deleted_side == "main", (path / "file.txt").exists())


class WorkspaceReleaseFilesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def write_sections(self):
        sections = []
        for version, changes in (("1.2.3", "* 第一条\n  补充描述\n- 第二条"), ("v1.2.4.5", "* 另一个版本")):
            heading = f"{version} (2026-11-02)"
            sections.append(f"{heading}\n{'-' * len(heading)}\n{changes}\n")
        content = "\n".join(sections)
        (self.path / "CHANGELOG.rst").write_text(content, encoding="utf-8")
        return content

    def test_reads_only_requested_release_section_without_rewriting(self):
        content = self.write_sections()
        self.assertEqual("第一条 补充描述\n第二条", MODULE.workspace_version_message(self.path, "v1.2.3"))
        self.assertEqual("另一个版本", MODULE.workspace_version_message(self.path, "v1.2.4.5"))
        for version in ("v1.2.4", "v9.9.9", "invalid", "1.2.3"):
            with self.subTest(version=version):
                self.assertIsNone(MODULE.workspace_version_message(self.path, version))
        self.assertEqual(content, (self.path / "CHANGELOG.rst").read_text(encoding="utf-8"))

    def test_template_and_missing_or_invalid_reference_do_not_supply_fake_notes(self):
        self.assertIsNone(MODULE.workspace_version_message(self.path, "v0.0.1"))
        MODULE.prepare_workspace_release_files(self.path)
        self.assertIsNone(MODULE.workspace_version_message(self.path, "v0.0.1"))
        (self.path / "CHANGELOG.rst").write_bytes(b"\xff\xfe")
        self.assertIsNone(MODULE.workspace_version_message(self.path, "v0.0.1"))

    def test_generated_ignores_cover_artifacts_and_both_generated_files(self):
        MODULE.prepare_workspace_release_files(self.path)
        subprocess.run(["git", "init", "-q", str(self.path)], check=True)
        repository = MODULE.GitRepository(self.path)
        ignored = [
            "build/output", "devel/output", "install/output", "build_isolated/output",
            "devel_isolated/output", "install_isolated/output", ".catkin_workspace",
            ".vscode/settings.json", "log/run.log", "logs/run.log", ".cache/data",
            "src/__pycache__/x.pyc", "src/x.pyc", "x.swp", "x~", ".pytest_cache/data",
            "release.zip", "CHANGELOG.rst",
        ]
        self.assertEqual([], repository.filter_ignored(ignored))
        self.assertEqual(["src/main.py"], repository.filter_ignored(["VERSION", "src/main.py"]))
        self.assertEqual([], MODULE.prepare_workspace_release_files(self.path))
        self.assertEqual(1, (self.path / ".gitignore").read_text().splitlines().count("/CHANGELOG.rst"))

    def test_existing_ignore_rule_and_notes_are_kept_byte_for_byte(self):
        notes = self.write_sections().encode("utf-8")
        original = b"*.tmp\r\n/CHANGELOG.rst\r\n"
        (self.path / ".gitignore").write_bytes(original)
        self.assertEqual([".gitignore"], MODULE.prepare_workspace_release_files(self.path))
        self.assertEqual(original + b"/VERSION\r\n", (self.path / ".gitignore").read_bytes())
        self.assertEqual([], MODULE.prepare_workspace_release_files(self.path))
        self.assertEqual(notes, (self.path / "CHANGELOG.rst").read_bytes())

    def test_enable_prepares_root_files_before_empty_workspace_check(self):
        window = mock.Mock()
        window.workspace_path = self.path
        window.prompt_file_selection.return_value = None

        MODULE.VersionManagerWindow.on_enable_versioning(window)

        self.assertTrue((self.path / "CHANGELOG.rst").is_file())
        self.assertTrue((self.path / ".gitignore").is_file())
        self.assertEqual([".gitignore"], window.prompt_file_selection.call_args.args[0])
        window.show_error.assert_not_called()
        window.run_background.assert_not_called()

    def test_publish_dialog_loads_matches_and_preserves_manual_input_on_miss(self):
        self.write_sections()
        window = mock.Mock()
        window.workspace_path = self.path
        window.repository = None
        changes_view = mock.Mock()
        buffer = changes_view.get_buffer.return_value
        text_state = {"text": ""}
        buffer.set_text.side_effect = lambda text: text_state.update(text=text)
        buffer.get_text.side_effect = lambda *_args: text_state["text"]
        window._build_changes_editor.return_value = (changes_view, mock.Mock())
        entry = mock.Mock()
        entry.get_text.return_value = "v1.2.3"
        dialog = mock.Mock()

        def interact():
            self.assertEqual("第一条 补充描述\n第二条", text_state["text"])
            callback = entry.connect.call_args.args[1]
            buffer.set_text("用户手动填写的说明")
            entry.get_text.return_value = "v9.9.9"
            callback(entry)
            self.assertEqual("用户手动填写的说明", text_state["text"])
            entry.get_text.return_value = "v1.2.4.5"
            callback(entry)
            self.assertEqual("另一个版本", text_state["text"])
            return MODULE.Gtk.ResponseType.OK

        dialog.run.side_effect = interact
        label_factory = mock.Mock()
        with mock.patch.multiple(
            MODULE.Gtk,
            Dialog=mock.Mock(return_value=dialog), Box=mock.Mock(), Grid=mock.Mock(),
            Entry=mock.Mock(return_value=entry), Label=label_factory,
        ):
            result = MODULE.VersionManagerWindow.prompt_version_metadata(window, "v1.2.3")
        self.assertEqual(("v1.2.4.5", "另一个版本"), result)
        labels = [call.kwargs.get("label", "") for call in label_factory.call_args_list]
        self.assertFalse(any("版本号匹配 CHANGELOG.rst" in label for label in labels))
        self.assertFalse(any("main 发布时自动更新 VERSION" in label for label in labels))


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
            except (FileNotFoundError, ProcessLookupError):
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
        self.assertEqual([".gitignore", "robot.txt"], self.git(self.remote, "ls-tree", "-r", "--name-only", "main").splitlines())
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

    def test_confirmed_whole_sync_overwrites_divergence_with_recovery(self):
        self.configure()
        self.repository.sync_remote()
        old_head = self.git(self.path, "rev-parse", "HEAD")
        self.publish("v0.0.2")
        self.repository.sync_remote()
        remote_head = self.git(self.remote, "rev-parse", "main")
        self.repository.run(["reset", "--mixed", old_head])

        self.publish("v0.0.3")
        self.repository.sync_remote()
        self.assertNotEqual(remote_head, self.git(self.remote, "rev-parse", "main"))
        self.assertEqual(self.git(self.path, "rev-parse", "main"), self.git(self.remote, "rev-parse", "main"))
        self.assertEqual(["v0.0.1", "v0.0.2", "v0.0.3"], self.git(self.remote, "tag").splitlines())
        self.assertIn(remote_head, self.git(self.path, "for-each-ref", "--format=%(objectname)", "refs/code-version-manager/recovery/"))

    def test_confirmed_whole_sync_updates_conflicting_tag_and_branch_together(self):
        self.configure()
        self.repository.sync_remote()
        remote_head = self.git(self.remote, "rev-parse", "main")
        self.repository.run(["tag", "-f", "-a", "v0.0.1", "-m", "changed description"])

        self.publish("v0.0.2")
        self.repository.sync_remote()
        self.assertNotEqual(remote_head, self.git(self.remote, "rev-parse", "main"))
        self.assertEqual(self.git(self.path, "rev-parse", "v0.0.1"), self.git(self.remote, "rev-parse", "v0.0.1"))
        self.assertEqual(["v0.0.1", "v0.0.2"], self.git(self.remote, "tag").splitlines())

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
        self.configure()
        self.repository.sync_remote()
        self.repository.run(["switch", "-c", "release"])
        self.publish("v0.0.1-beta.0.0.1")
        self.repository.sync_remote()

        self.assertEqual(
            "refs/heads/main\nrefs/heads/release", self.git(self.remote, "for-each-ref", "--format=%(refname)", "refs/heads/")
        )
        self.assertEqual(["branches/release/v0.0.1-beta.0.0.1", "v0.0.1"], self.git(self.remote, "tag").splitlines())

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
        original = MODULE.run_remote_process
        with mock.patch.object(MODULE, "run_remote_process", wraps=original) as run:
            self.repository.sync_remote()
            pushes = [call for call in run.call_args_list if "push" in call.args[0] and "--dry-run" not in call.args[0]]
            self.assertEqual(MODULE.PUSH_TIMEOUT, pushes[0].kwargs["timeout"])
            self.assertIn("--progress", pushes[0].args[0])
            self.publish("v0.0.2")
            self.repository.check_remote_connection()
            checks = [call for call in run.call_args_list if "--dry-run" in call.args[0]]
            self.assertEqual(MODULE.REMOTE_TIMEOUT, checks[-1].kwargs["timeout"])

    def test_push_timeout_is_editable_local_and_preserved_when_not_overridden(self):
        self.repository.configure_remote(str(self.remote), "Test", "test@example.com", push_timeout=1800)
        self.assertEqual(1800, self.repository.push_timeout_seconds())
        self.configure()
        original = MODULE.run_remote_process
        with mock.patch.object(MODULE, "run_remote_process", wraps=original) as run:
            self.repository.sync_remote()
        pushes = [call for call in run.call_args_list if "push" in call.args[0] and "--dry-run" not in call.args[0]]
        self.assertEqual(1800, pushes[0].kwargs["timeout"])

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
        original = MODULE.run_remote_process
        def timeout_on_push(command, **kwargs):
            if "push" in command and "--dry-run" not in command:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return original(command, **kwargs)
        with mock.patch.object(MODULE, "run_remote_process", side_effect=timeout_on_push):
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
            if "push" in command and "--dry-run" not in command:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=result.stdout, stderr=result.stderr)
            return result

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
                window.branch_generation = 0
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
        window.branch_generation = 0
        window.background_timer = None
        operation = mock.Mock()
        with mock.patch.object(MODULE.time, "monotonic", side_effect=[100, 105]), \
             mock.patch.object(MODULE.GLib, "timeout_add_seconds", return_value=42) as add_timer, \
             mock.patch.object(MODULE.threading, "Thread") as thread:
            MODULE.VersionManagerWindow.run_background(window, "正在同步远程仓库...", operation, "已同步")
            callback = add_timer.call_args.args[1]
            self.assertTrue(callback())

        self.assertEqual(42, window.background_timer)
        window._set_operation_status.assert_called_with("正在同步远程仓库... · 已等待 5 秒", record=False)
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


class DownloadChangelogTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.destination = self.root / "custom-history.rst"
        self.window = mock.Mock()
        self.window.repository.path = self.workspace
        self.window.history_scope_combo.get_active_id.return_value = "current"
        self.window.repository.download_changelog.side_effect = (
            lambda path, all_versions=False: path.write_text(
                "all history\n" if all_versions else "current history\n", encoding="utf-8",
            )
        )
        self.dialog = mock.Mock()
        self.dialog.run.return_value = MODULE.Gtk.ResponseType.OK
        self.dialog.get_filename.return_value = str(self.destination)
        chooser_patch = mock.patch.object(MODULE.Gtk, "FileChooserDialog", return_value=self.dialog)
        self.chooser = chooser_patch.start()
        self.addCleanup(chooser_patch.stop)

    def test_download_chooses_location_and_runs_in_background(self):
        root_notes = self.workspace / "CHANGELOG.rst"
        root_notes.write_text("workspace notes\n")
        MODULE.VersionManagerWindow.on_download_changelog(self.window, None)

        self.chooser.assert_called_once_with(
            title="下载 CHANGELOG", parent=self.window, action=MODULE.Gtk.FileChooserAction.SAVE,
        )
        self.dialog.set_current_folder.assert_called_once_with(str(self.workspace))
        self.dialog.set_current_name.assert_called_once_with("CHANGELOG.rst")
        self.dialog.set_do_overwrite_confirmation.assert_called_once_with(True)
        self.dialog.destroy.assert_called_once_with()
        self.window.repository.download_changelog.assert_not_called()
        busy, operation, success = self.window.run_background.call_args.args
        self.assertEqual("正在下载 CHANGELOG...", busy)
        self.assertEqual(f"CHANGELOG 已下载到 {self.destination}", success)
        operation()
        self.window.repository.download_changelog.assert_called_once_with(
            self.destination, all_versions=False,
        )
        self.assertEqual("current history\n", self.destination.read_text())
        self.assertEqual("workspace notes\n", root_notes.read_text())
        self.window.show_error.assert_not_called()

    def test_all_versions_scope_is_passed_to_background_download(self):
        self.window.history_scope_combo.get_active_id.return_value = "all"
        self.window.run_background.side_effect = lambda _busy, operation, _success: operation()

        MODULE.VersionManagerWindow.on_download_changelog(self.window, None)

        self.window.repository.download_changelog.assert_called_once_with(
            self.destination, all_versions=True,
        )
        self.assertEqual("all history\n", self.destination.read_text())

    def test_existing_file_uses_save_dialog_overwrite_confirmation(self):
        self.destination.write_text("manual notes\n")
        with mock.patch.object(MODULE.Gtk, "MessageDialog") as message_dialog:
            MODULE.VersionManagerWindow.on_download_changelog(self.window, None)
        self.dialog.set_do_overwrite_confirmation.assert_called_once_with(True)
        self.assertEqual("manual notes\n", self.destination.read_text())
        self.window.run_background.call_args.args[1]()
        self.assertEqual("current history\n", self.destination.read_text())
        message_dialog.assert_not_called()

    def test_cancel_preserves_existing_file(self):
        self.destination.write_text("manual notes\n")
        self.dialog.run.return_value = MODULE.Gtk.ResponseType.CANCEL
        MODULE.VersionManagerWindow.on_download_changelog(self.window, None)

        self.assertEqual("manual notes\n", self.destination.read_text())
        self.window.repository.download_changelog.assert_not_called()
        self.window.run_background.assert_not_called()
        self.dialog.get_filename.assert_not_called()
        self.dialog.destroy.assert_called_once_with()

    def test_empty_filename_does_not_start_download(self):
        self.dialog.get_filename.return_value = None
        MODULE.VersionManagerWindow.on_download_changelog(self.window, None)
        self.window.run_background.assert_not_called()
        self.dialog.destroy.assert_called_once_with()

    def test_no_repository_does_not_open_save_dialog(self):
        self.window.repository = None
        MODULE.VersionManagerWindow.on_download_changelog(self.window, None)
        self.chooser.assert_not_called()
        self.window.run_background.assert_not_called()

    def test_download_errors_use_background_error_handler(self):
        for error in (MODULE.GitError("目标路径必须是普通文件"), OSError("磁盘写入失败")):
            with self.subTest(error=error):
                self.window.busy = False
                self.window.branch_generation = 0
                self.window.repository.download_changelog.side_effect = error
                self.window.run_background.side_effect = lambda *args: MODULE.VersionManagerWindow.run_background(
                    self.window, *args,
                )
                with mock.patch.object(MODULE.GLib, "timeout_add_seconds", return_value=42), \
                        mock.patch.object(MODULE.GLib, "idle_add") as idle_add, \
                        mock.patch.object(MODULE.threading, "Thread") as thread:
                    MODULE.VersionManagerWindow.on_download_changelog(self.window, None)
                    self.window.repository.download_changelog.assert_not_called()
                    thread.call_args.kwargs["target"]()
                idle_add.assert_called_once_with(self.window._background_failed, str(error))
                self.window.repository.download_changelog.reset_mock()
                self.assertFalse(self.destination.exists())


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


class BranchPresentationTest(unittest.TestCase):
    def test_local_and_remote_refs_share_one_display_row(self):
        local = [
            MODULE.BranchInfo("main", "local", "local-main", True),
            MODULE.BranchInfo("feature", "local", "local-feature"),
        ]
        remote = [
            MODULE.BranchInfo("main", "remote", "remote-main"),
            MODULE.BranchInfo("test1", "remote", "remote-test1"),
        ]

        choices = MODULE.combine_branch_choices(local, remote)

        self.assertEqual(["feature", "main", "test1"], [choice.name for choice in choices])
        by_name = {choice.name: choice for choice in choices}
        self.assertEqual("main（本地+origin）", by_name["main"].label)
        self.assertEqual("local", by_name["main"].source)
        self.assertEqual("local-main", by_name["main"].oid)
        self.assertEqual("feature（本地）", by_name["feature"].label)
        self.assertEqual("test1（origin）", by_name["test1"].label)
        self.assertEqual("#32804f", by_name["main"].foreground)
        self.assertIsNone(by_name["feature"].foreground)
        self.assertEqual("remote", by_name["test1"].source)
        self.assertEqual("remote-test1", by_name["test1"].oid)

    def test_gtk_branch_model_colors_current_branch_and_resets_after_switch(self):
        if not MODULE.Gtk.init_check()[0]:
            self.skipTest("GTK display is unavailable")
        with mock.patch.object(MODULE.VersionManagerWindow, "load_workspace"):
            window = MODULE.VersionManagerWindow(None, Path(tempfile.gettempdir()))
        self.addCleanup(window.destroy)
        repository = mock.Mock()
        repository._sync_target.return_value = "local-origin"
        repository.run.return_value = "commit"
        repository.merge_state.return_value = MODULE.MergeState(False)
        repository.current_branch.return_value = "main"
        repository.local_branches.return_value = [
            MODULE.BranchInfo("main", "local", "main-oid", True),
            MODULE.BranchInfo("feature", "local", "feature-oid"),
        ]
        window.repository = repository
        window.remote_branch_url = "local-origin"
        window.remote_branch_choices = [MODULE.BranchInfo("main", "remote", "origin-oid")]
        window._refresh_branch_choices()
        self.assertTrue(window.new_branch_button.get_sensitive())
        rows = {row[0]: list(row) for row in window.branch_model}
        self.assertEqual(2, len(rows))
        self.assertEqual("branch:main", window.branch_combo.get_active_id())
        self.assertEqual(["main（本地+origin）", "#32804f", True], rows["branch:main"][1:])
        self.assertFalse(rows["branch:feature"][3])
        renderer = window.branch_combo.get_cells()[0]
        window.branch_combo.get_area().apply_attributes(
            window.branch_model, window.branch_model.get_iter("1"), False, False,
        )
        self.assertTrue(renderer.get_property("foreground-set"))
        window.branch_combo.get_area().apply_attributes(
            window.branch_model, window.branch_model.get_iter("0"), False, False,
        )
        self.assertFalse(renderer.get_property("foreground-set"))

        repository.current_branch.return_value = "feature"
        repository.local_branches.return_value = [
            MODULE.BranchInfo("main", "local", "main-oid"),
            MODULE.BranchInfo("feature", "local", "feature-oid", True),
        ]
        window._refresh_branch_choices()
        self.assertFalse(window.new_branch_button.get_sensitive())
        rows = {row[0]: list(row) for row in window.branch_model}
        self.assertEqual("branch:feature", window.branch_combo.get_active_id())
        self.assertTrue(rows["branch:feature"][3])
        self.assertFalse(rows["branch:main"][3])

    def test_delete_branch_selection_checks_remote_before_confirmation(self):
        window = mock.Mock()
        window.repository = mock.Mock()
        window.busy = False
        window._choose_development_branch.return_value = "retreat_teb"

        MODULE.VersionManagerWindow.on_delete_branch(window, None)

        busy, operation, success = window.run_background.call_args.args
        self.assertEqual("正在检查分支和远程状态…", busy)
        self.assertEqual("", success)
        operation()
        window.repository.preview_branch_deletion.assert_called_once_with("retreat_teb")
        window.repository.delete_branch.assert_not_called()
        self.assertIn("result_callback", window.run_background.call_args.kwargs)

    def test_inherited_formal_release_is_marked_read_only(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name)
        subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
        subprocess.run(
            ["git", "-C", str(path), "config", "user.name", "Test User"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "config", "user.email", "test@example.com"],
            check=True,
        )
        (path / "file.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(path), "add", "file.txt"], check=True)
        subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "release"], check=True)
        subprocess.run(["git", "-C", str(path), "branch", "-M", "main"], check=True)
        subprocess.run(["git", "-C", str(path), "tag", "-a", "v0.1.0", "-m", "release"], check=True)
        repository = MODULE.GitRepository(path)
        repository.create_branch("test1")

        history = repository.history()

        self.assertEqual("[继承的正式版本 · 只读] release", history[0].subject)
        self.assertTrue(history[0].read_only)


if __name__ == "__main__":
    unittest.main()
