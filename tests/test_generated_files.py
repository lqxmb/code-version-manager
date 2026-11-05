"""Generated workspace files, legacy migration and packaged ignore templates."""
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from test_backend import MODULE


class GeneratedWorkspaceFilesTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "workspace"
        self.path.mkdir()
        (self.path / "source.txt").write_text("base\n")
        self.repo = MODULE.initialize_repository(self.path, "v1.0.0", "base", ["source.txt"])

    def assert_generated_excluded(self, version):
        tree = self.repo.run(["ls-tree", "-r", "--name-only", version]).splitlines()
        manifest = self.repo.manifest(version)
        archive = self.root / "release.zip"
        self.repo.export(version, archive)
        with zipfile.ZipFile(archive) as bundle:
            for name in MODULE.WORKSPACE_GENERATED_FILES:
                self.assertNotIn(name, tree)
                self.assertNotIn(name, manifest)
                self.assertNotIn(name, bundle.namelist())
                self.assertTrue((self.path / name).is_file())

    def test_initial_release_copies_template_and_excludes_both_local_files(self):
        self.assertEqual(MODULE.workspace_gitignore_template_path().read_bytes(),
                         (self.path / ".gitignore").read_bytes())
        self.assertEqual("1.0.0\n", (self.path / "VERSION").read_text())
        self.assert_generated_excluded("v1.0.0")
        self.assertEqual([], self.repo.status())
        for name in MODULE.WORKSPACE_GENERATED_FILES:
            self.assertEqual(name + "\n", self.repo.run(["check-ignore", name]))

    def test_legacy_tracked_files_migrate_and_branch_roundtrip_uses_tags(self):
        (self.path / ".gitignore").write_bytes(b"# user rules\r\n*.tmp\r\n/CHANGELOG.rst\r\n")
        (self.path / "VERSION").write_text("stale inherited value\n")
        (self.path / "CHANGELOG.rst").write_text("old tracked notes\n")
        self.repo.run(["add", "-f", ".gitignore", "VERSION", "CHANGELOG.rst"])
        self.repo.run(["commit", "-m", "legacy release"])
        self.repo.run(["tag", "-a", "v1.0.1", "-m", "legacy"])
        self.repo.run(["branch", "legacy"])
        self.repo.save_version("v1.0.2", "migrate", ["source.txt"])
        self.assert_generated_excluded("v1.0.2")
        self.assertIn(".gitignore", self.repo.manifest("v1.0.2"))
        self.assertEqual(b"# user rules\r\n*.tmp\r\n/CHANGELOG.rst\r\n/VERSION\r\n",
                         (self.path / ".gitignore").read_bytes())
        self.assertIn("VERSION", self.repo.run(["ls-tree", "--name-only", "v1.0.1"]).splitlines())
        self.repo.switch_branch("legacy")
        self.assertEqual("1.0.1\n", (self.path / "VERSION").read_text())
        self.assertNotIn("1.0.2 (", (self.path / "CHANGELOG.rst").read_text())
        self.assertEqual([], self.repo.status())
        self.repo.run(["add", "-f", "VERSION"])
        (self.path / "VERSION").write_text("generated draft\n")
        self.repo.switch_branch("main")
        self.assertEqual("1.0.2\n", (self.path / "VERSION").read_text())
        self.assertEqual("", self.repo.run(["ls-files", "--", "VERSION", "CHANGELOG.rst"]))
        self.assertEqual([], self.repo.status())

    def test_release_can_only_remove_legacy_generated_files(self):
        self.repo.run(["add", "-f", "VERSION", "CHANGELOG.rst"])
        self.repo.run(["commit", "-m", "legacy tracked state"])
        self.repo.save_version("v1.0.1", "stop tracking generated files", [])
        self.assert_generated_excluded("v1.0.1")
        self.assertEqual("1.0.1\n", (self.path / "VERSION").read_text())
        self.assertEqual([], self.repo.status())

    def test_scoped_changelog_follows_branch_and_unpublished_commits(self):
        self.repo.create_branch("feature/camera", "v1.0.0")
        self.repo.save_version("feature/camera/v1.0.0-beta.0.0.1", "camera only", ["source.txt"])
        camera = "feature/camera/v1.0.0-beta.0.0.1"
        self.assertEqual(camera + "\n", (self.path / "VERSION").read_text())
        self.assert_generated_excluded(MODULE.canonical_version(camera))
        self.repo.switch_branch("main")
        self.repo.create_branch("other", "v1.0.0")
        self.repo.save_version("other/v1.0.0-beta.0.0.1", "other only", ["source.txt"])
        self.repo.switch_branch("main")
        self.repo.save_version("v1.1.0", "main progress", ["source.txt"])
        self.repo.switch_branch("feature/camera")
        self.repo.run(["commit", "--allow-empty", "-m", "unpublished"])
        self.repo.switch_branch("main")
        self.repo.switch_branch("feature/camera")
        self.assertEqual(camera + "\n", (self.path / "VERSION").read_text())
        notes = (self.path / "CHANGELOG.rst").read_text()
        self.assertIn("camera only", notes)
        self.assertIn("1.0.0 (", notes)
        self.assertNotIn("other only", notes)
        self.assertNotIn("1.1.0 (", notes)
        internal = (self.repo.metadata_path / "CHANGELOG.rst").read_text()
        self.assertIn("other only", internal)
        self.assertIn("1.1.0 (", internal)
        downloaded = self.root / "download.rst"
        self.assertEqual(notes, (self.path / "CHANGELOG.rst").read_text())
        window = mock.Mock()
        window.repository = self.repo
        window.run_background.side_effect = lambda _busy, operation, _success: operation()
        dialog = mock.Mock()
        dialog.run.return_value = MODULE.Gtk.ResponseType.OK
        dialog.get_filename.return_value = str(downloaded)
        for scope in ("current", "all"):
            with self.subTest(history_scope=scope):
                window.history_scope_combo.get_active_id.return_value = scope
                window.history_entries = self.repo.history(all_versions=scope == "all")
                with mock.patch.object(MODULE.Gtk, "FileChooserDialog", return_value=dialog):
                    MODULE.VersionManagerWindow.on_download_changelog(window, None)
                expected = self.repo.changelog_text(current_branch_only=scope == "current")
                self.assertEqual(expected, downloaded.read_text())
                self.assertIn("camera only", downloaded.read_text())
                self.assertEqual(scope == "all", "other only" in downloaded.read_text())
                self.assertEqual(scope == "all", "1.1.0 (" in downloaded.read_text())
                self.assertEqual(notes, (self.path / "CHANGELOG.rst").read_text())
        self.repo.switch_branch("main")
        self.repo.create_branch("fresh", "v1.0.0")
        self.assertEqual("1.0.0\n", (self.path / "VERSION").read_text())
        self.assertNotIn("beta", (self.path / "CHANGELOG.rst").read_text())

    def test_download_rejects_special_targets_outside_workspace(self):
        destination = self.root / "custom-history.rst"
        external = self.root / "external.rst"
        external.write_text("preserve external notes\n")
        notes = (self.path / "CHANGELOG.rst").read_bytes()
        refs = self.repo._local_refs()
        index = self.repo.run(["ls-files", "--stage", "-z"])
        for kind in ("symlink", "directory", "fifo"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    destination.symlink_to(external)
                elif kind == "directory":
                    destination.mkdir()
                else:
                    os.mkfifo(destination)
                with self.assertRaisesRegex(MODULE.GitError, "custom-history.rst.*目标路径必须是普通文件"):
                    self.repo.download_changelog(destination)
                self.assertEqual("preserve external notes\n", external.read_text())
                self.assertEqual(notes, (self.path / "CHANGELOG.rst").read_bytes())
                self.assertEqual(refs, self.repo._local_refs())
                self.assertEqual(index, self.repo.run(["ls-files", "--stage", "-z"]))
                self.assertEqual([], list(self.root.glob(".custom-history.rst.*")))
                if kind == "directory":
                    destination.rmdir()
                else:
                    destination.unlink()

    def test_download_atomically_replaces_custom_file_and_preserves_permissions(self):
        destination = self.root / "custom-history.rst"
        destination.write_text("manual notes\n")
        destination.chmod(0o640)
        self.repo.download_changelog(destination)
        self.assertEqual(self.repo.changelog_text(), destination.read_text())
        self.assertEqual(0o640, destination.stat().st_mode & 0o777)
        self.assertEqual([], list(self.root.glob(".custom-history.rst.*")))

    def test_download_failure_preserves_custom_file_and_cleans_temporary_file(self):
        destination = self.root / "custom-history.rst"
        destination.write_bytes(b"manual notes\r\n")
        destination.chmod(0o640)
        with mock.patch.object(MODULE.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaisesRegex(MODULE.GitError, "custom-history.rst.*disk error"):
                self.repo.download_changelog(destination)
        self.assertEqual(b"manual notes\r\n", destination.read_bytes())
        self.assertEqual(0o640, destination.stat().st_mode & 0o777)
        self.assertEqual([], list(self.root.glob(".custom-history.rst.*")))

    def test_refresh_without_tags_removes_stale_version_without_touching_index(self):
        self.repo.run(["tag", "-d", "v1.0.0"])
        index = self.repo.run(["ls-files", "--stage", "-z"])
        refs = self.repo._local_refs()
        self.repo.refresh_workspace_generated_files()
        self.assertFalse((self.path / "VERSION").exists())
        self.assertIn("尚无版本", (self.path / "CHANGELOG.rst").read_text())
        self.assertEqual(index, self.repo.run(["ls-files", "--stage", "-z"]))
        self.assertEqual(refs, self.repo._local_refs())

    def test_failed_checkout_restores_generated_files_and_exact_index(self):
        self.repo.create_branch("feature")
        self.repo.switch_branch("main")
        self.repo.run(["add", "-f", "VERSION"])
        (self.path / "VERSION").write_text("draft\n")
        originals = {name: (self.path / name).read_bytes() for name in MODULE.WORKSPACE_GENERATED_FILES}
        index = self.repo._git_path("index").read_bytes()
        original_run = self.repo.run

        def fail_switch(arguments, *args, **kwargs):
            if arguments[0] == "switch":
                raise MODULE.GitError("checkout failed")
            return original_run(arguments, *args, **kwargs)

        # Invoke the transition directly so the snapshot covers its own changes.
        with mock.patch.object(self.repo, "run", side_effect=fail_switch):
            with self.assertRaisesRegex(MODULE.GitError, "checkout failed"):
                self.repo._switch_generated(["switch", "feature"], "feature")
        self.assertEqual("main", self.repo.current_branch())
        self.assertEqual(index, self.repo._git_path("index").read_bytes())
        for name, content in originals.items():
            self.assertEqual(content, (self.path / name).read_bytes())

    def test_source_rename_to_generated_path_still_blocks_branch_switch(self):
        self.repo.create_branch("feature")
        self.repo.switch_branch("main")
        (self.path / "VERSION").unlink()
        self.repo.run(["mv", "-f", "source.txt", "VERSION"])
        with self.assertRaisesRegex(MODULE.GitError, "尚未发布"):
            self.repo.switch_branch("feature")
        self.assertIn("source.txt", self.repo.changed_paths())

    def test_unsafe_generated_targets_reject_before_repository_changes(self):
        self.repo.create_branch("feature")
        self.repo.switch_branch("main")
        external = self.root / "external"
        external.write_text("preserve\n")
        for name in MODULE.WORKSPACE_GENERATED_FILES:
            target = self.path / name
            original = target.read_bytes()
            for kind in ("symlink", "directory", "fifo"):
                with self.subTest(name=name, kind=kind):
                    target.unlink()
                    if kind == "symlink":
                        target.symlink_to(external)
                    elif kind == "directory":
                        target.mkdir()
                    else:
                        os.mkfifo(target)
                    refs = self.repo._local_refs()
                    index = self.repo._git_path("index").read_bytes()
                    config = self.repo._git_path("config").read_bytes()
                    operations = (
                        self.repo.refresh_workspace_generated_files,
                        lambda: self.repo.save_version("v1.0.1", "blocked", ["source.txt"]),
                        lambda: self.repo.switch_branch("feature"),
                        lambda: self.repo.create_branch("blocked"),
                        lambda: self.repo.delete_version("v1.0.0"),
                        lambda: self.repo.delete_branch("feature"),
                    )
                    for operation in operations:
                        with self.assertRaisesRegex(MODULE.GitError, name):
                            operation()
                    self.assertEqual(refs, self.repo._local_refs())
                    self.assertEqual(index, self.repo._git_path("index").read_bytes())
                    self.assertEqual(config, self.repo._git_path("config").read_bytes())
                    if kind == "directory":
                        target.rmdir()
                    else:
                        target.unlink()
                    target.write_bytes(original)
        self.assertEqual("preserve\n", external.read_text())

    def test_legacy_archives_are_filtered_with_and_without_manifests(self):
        self.repo.run(["add", "-f", "VERSION", "CHANGELOG.rst"])
        self.repo.run(["commit", "-m", "old tracked files"])
        self.repo.run(["tag", "v1.0.1"])
        for manifest in (None, ["source.txt", "VERSION", "CHANGELOG.rst"]):
            if manifest is not None:
                self.repo._write_manifest("v1.0.1", manifest)
            archive = self.root / "old.zip"
            self.repo.export("v1.0.1", archive)
            with zipfile.ZipFile(archive) as bundle:
                self.assertIn("source.txt", bundle.namelist())
                self.assertNotIn("VERSION", bundle.namelist())
                self.assertNotIn("CHANGELOG.rst", bundle.namelist())
            materialized = self.root / ("with-manifest" if manifest else "without-manifest")
            self.repo.materialize("v1.0.1", materialized)
            self.assertTrue((materialized / "source.txt").exists())
            self.assertFalse((materialized / "VERSION").exists())
            self.assertFalse((materialized / "CHANGELOG.rst").exists())

    def test_failed_sync_keeps_recovery_stash_ready_to_restore_index(self):
        remote = self.root / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        self.repo.configure_remote(str(remote), "Tester", "test@example.com")
        self.repo.sync_remote()
        peer_path = self.root / "peer"
        subprocess.run(["git", "clone", "-q", "--branch", "main", str(remote), str(peer_path)], check=True)
        peer = MODULE.GitRepository(peer_path)
        peer.configure_remote(str(remote), "Peer", "peer@example.com")
        (peer_path / "source.txt").write_text("remote\n")
        peer.save_version("v1.0.1", "remote update", ["source.txt"])
        peer.sync_remote()
        (self.path / "source.txt").write_text("staged local\n")
        self.repo.run(["add", "source.txt"])
        (self.path / "source.txt").write_text("unstaged local\n")
        snapshot = self.repo.sync_snapshot()
        status = self.repo.remote_sync_status(snapshot)
        with mock.patch.object(self.repo, "_replace_managed_refs", side_effect=MODULE.GitError("ref update failed")):
            with self.assertRaisesRegex(MODULE.GitError, "ref update failed"):
                self.repo.sync_local_from_remote(snapshot, status)
        self.assertEqual(dict(snapshot.refs), self.repo._local_refs())
        self.assertEqual("", self.repo.run(["diff", "--cached", "--name-only"]))
        self.repo.restore_local_sync_changes()
        self.assertEqual("staged local\n", self.repo.run(["show", ":source.txt"]))
        self.assertEqual("unstaged local\n", (self.path / "source.txt").read_text())
        self.assertEqual("1.0.0\n", (self.path / "VERSION").read_text())

    def test_broad_ignore_still_records_migration_rules(self):
        (self.path / ".gitignore").write_text("*\n")
        self.repo.save_version("v1.0.1", "ignore all build output", [])
        self.assertEqual([".gitignore"], self.repo.run(["ls-tree", "--name-only", "HEAD"]).splitlines())
        self.assertEqual([".gitignore"], self.repo.manifest("v1.0.1"))
        self.assertIn("/VERSION", self.repo.run(["show", "HEAD:.gitignore"]))
        self.assertIn("/CHANGELOG.rst", self.repo.run(["show", "HEAD:.gitignore"]))

    def test_partial_install_failure_restores_both_original_files(self):
        originals = {name: (self.path / name).read_bytes() for name in MODULE.WORKSPACE_GENERATED_FILES}
        replace = MODULE.os.replace

        def fail_notes(source, destination):
            if Path(destination).name == "CHANGELOG.rst":
                raise OSError("disk error")
            return replace(source, destination)

        self.repo.run(["tag", "v1.0.1"])
        with mock.patch.object(MODULE.os, "replace", side_effect=fail_notes):
            with self.assertRaisesRegex(MODULE.GitError, "disk error"):
                self.repo.refresh_workspace_generated_files()
        for name, content in originals.items():
            self.assertEqual(content, (self.path / name).read_bytes())
        self.assertEqual([], list(self.path.glob(".VERSION.*")))
        self.assertEqual([], list(self.path.glob(".CHANGELOG.rst.*")))


class WorkspaceIgnoreTemplateTest(unittest.TestCase):
    def test_custom_template_is_copied_for_new_workspaces(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            template = root / "custom.gitignore"
            content = b"# packaging customization\r\n/private/\r\n/VERSION\r\n/CHANGELOG.rst\r\n"
            template.write_bytes(content)
            workspace = root / "workspace"
            workspace.mkdir()
            with mock.patch.object(MODULE, "workspace_gitignore_template_path", return_value=template):
                MODULE.prepare_workspace_release_files(workspace)
            self.assertEqual(content, (workspace / ".gitignore").read_bytes())
            template.write_bytes(content + b"/extra/\r\n")
            other = root / "other"
            other.mkdir()
            with mock.patch.object(MODULE, "workspace_gitignore_template_path", return_value=template):
                MODULE.prepare_workspace_release_files(other)
            self.assertEqual(template.read_bytes(), (other / ".gitignore").read_bytes())
            self.assertEqual(content, (workspace / ".gitignore").read_bytes())

    def test_installed_template_lookup_and_missing_file_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            template = root / "workspace.gitignore"
            template.write_text("/custom/\n")
            with mock.patch.object(MODULE, "__file__", str(root / "code_version_manager.py")):
                self.assertEqual(template, MODULE.workspace_gitignore_template_path())
                template.unlink()
                with self.assertRaisesRegex(MODULE.GitError, "模板"):
                    MODULE.workspace_gitignore_template_path()

    def test_migration_preserves_newlines_and_does_not_duplicate_rules(self):
        for newline in (b"\n", b"\r\n", b"\r"):
            original = newline.join([b"# custom", b"*.tmp", b"/CHANGELOG.rst", b"/CHANGELOG.rst", b"!VERSION"]) + newline
            updated = MODULE.append_generated_ignore_rules(original)
            self.assertTrue(updated.startswith(original))
            self.assertEqual(original + b"/VERSION" + newline + b"/CHANGELOG.rst" + newline, updated)
            self.assertEqual(updated, MODULE.append_generated_ignore_rules(updated))
