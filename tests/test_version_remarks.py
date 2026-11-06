"""Per-version remarks use independent notes and explicitly scoped sync."""
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

from test_backend import MODULE


class VersionRemarksTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        settings = mock.patch.object(MODULE, "settings_file_path", return_value=self.root / "settings.ini")
        settings.start()
        self.addCleanup(settings.stop)
        self.path = self.root / "workspace"
        self.path.mkdir()
        (self.path / "source.txt").write_text("base\n")
        self.repo = MODULE.initialize_repository(self.path, "v1.0.0", "base", ["source.txt"])
        self.remote = self.root / "origin.git"
        self.repo.run(["init", "--bare", "-q", str(self.remote)])
        self.repo.configure_remote(str(self.remote), "Test", "test@example.com")

    def note_ref(self, version="v1.0.0"):
        return MODULE.REMARK_REF_PREFIX + MODULE.canonical_version(version)

    def state(self, repo=None):
        repo = repo or self.repo
        return (repo.current_branch(), repo.run(["rev-parse", "HEAD"]),
                repo._git_path("index").read_bytes(),
                {path.name: path.read_bytes() for path in repo.path.iterdir() if path.is_file()},
                repo.run(["for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/", "refs/tags/"]),
                repo.run(["config", "--local", "--list"]))

    def peer(self):
        path = self.root / "peer"
        self.repo.run(["clone", "-q", "--branch", "main", str(self.remote), str(path)])
        repo = MODULE.GitRepository(path)
        repo.configure_remote(str(self.remote), "Peer", "peer@example.com")
        return repo

    def status(self, repo=None, scope="branch"):
        repo = repo or self.repo
        snapshot = repo.sync_snapshot(scope=scope)
        status = repo.remote_sync_status(snapshot)
        return snapshot, status

    def pull(self, repo):
        snapshot, status = self.status(repo)
        self.assertTrue(status.can_sync_local, status.local_message)
        return repo.sync_local_from_remote(snapshot, status)

    def beta(self, branch="feature/camera"):
        self.repo.create_branch(branch, "v1.0.0")
        (self.path / "source.txt").write_text("beta\n")
        version = MODULE.version_tag_name(branch, "v1.0.0-beta.0.0.1")
        self.repo.save_version(version, "beta", ["source.txt"])
        return version

    def test_multiline_roundtrip_keeps_code_index_config_and_release_outputs(self):
        (self.path / "source.txt").write_text("staged\n")
        self.repo.run(["add", "source.txt"])
        (self.path / "source.txt").write_text("unstaged\n")
        (self.path / "untracked.txt").write_text("untracked\n")
        before = self.state()
        snapshot = self.repo.preview_version_remark("v1.0.0")
        text = "A 现场已更新\nB 现场测试中"
        with mock.patch.object(self.repo, "_run_remote", side_effect=AssertionError("offline editing")):
            self.repo.update_version_remark("v1.0.0", text, expected=snapshot)
        self.assertEqual(before, self.state())
        self.assertEqual(text, MODULE.GitRepository(self.path).version_remark("v1.0.0"))
        self.assertEqual(text, self.repo.history()[0].remark)
        commit = self.repo.run(["rev-parse", "v1.0.0^{commit}"]).strip()
        self.assertEqual(text, self.repo.run(["notes", "--ref", self.note_ref(), "show", commit]).strip())
        self.assertNotIn(text, self.repo.changelog_text())
        destination = self.root / "version.zip"
        self.repo.export("v1.0.0", destination)
        with zipfile.ZipFile(destination) as archive:
            self.assertFalse(any("remark" in name for name in archive.namelist()))
        self.repo.update_version_remark("v1.0.0", " \n ")
        self.assertNotIn(self.note_ref(), self.repo._local_refs())
        self.assertEqual("", self.repo.version_remark("v1.0.0"))

    def test_same_text_produces_same_ref_and_same_commit_tags_have_independent_notes(self):
        self.repo.run(["tag", "v1.0.1"])
        self.repo.update_version_remark("v1.0.0", "测试中")
        oid = self.repo._local_refs()[self.note_ref()]
        self.repo.update_version_remark("v1.0.0", "测试中")
        self.assertEqual(oid, self.repo._local_refs()[self.note_ref()])
        self.repo.update_version_remark("v1.0.1", "已更新现场")
        self.assertEqual("测试中", self.repo.version_remark("v1.0.0"))
        self.assertEqual("已更新现场", self.repo.version_remark("v1.0.1"))

    def test_shared_baseline_remark_editable_on_main_and_read_only_on_beta(self):
        tag = self.repo._local_refs()["refs/tags/v1.0.0"]
        beta = self.beta()
        with self.assertRaisesRegex(MODULE.GitError, "只读"):
            self.repo.update_version_remark("v1.0.0", "inherited")
        self.repo.update_version_remark(beta, "测试中")
        self.repo.switch_branch("main")
        self.repo.update_version_remark("v1.0.0", "已更新现场")
        self.assertEqual(tag, self.repo._local_refs()["refs/tags/v1.0.0"])
        self.repo.switch_branch("feature/camera")
        self.assertEqual("v1.0.0", self.repo.branch_base("feature/camera"))
        inherited = next(entry for entry in self.repo.history() if entry.reference == "v1.0.0")
        self.assertTrue(inherited.read_only)
        self.assertEqual("已更新现场", inherited.remark)

    def test_tag_annotation_edit_preserves_note_but_new_target_does_not_inherit_it(self):
        self.repo.update_version_remark("v1.0.0", "测试中")
        oid = self.repo._local_refs()[self.note_ref()]
        self.repo.update_version_message("v1.0.0", "new description")
        self.assertEqual("测试中", self.repo.version_remark("v1.0.0"))
        (self.path / "source.txt").write_text("new\n")
        self.repo.save_version("v1.0.1", "new", ["source.txt"])
        self.repo.run(["tag", "-f", "v1.0.0", "HEAD"])
        self.assertEqual("", self.repo.version_remark("v1.0.0"))
        self.assertEqual(oid, self.repo._local_refs()[self.note_ref()])

    def test_edit_snapshot_rejects_note_tag_branch_and_repository_changes(self):
        snapshot = self.repo.preview_version_remark("v1.0.0")
        self.repo.update_version_remark("v1.0.0", "other writer")
        with self.assertRaisesRegex(MODULE.GitError, "已变化"):
            self.repo.update_version_remark("v1.0.0", "overwrite", expected=snapshot)
        snapshot = self.repo.preview_version_remark("v1.0.0")
        self.repo.update_version_message("v1.0.0", "tag changed")
        with self.assertRaisesRegex(MODULE.GitError, "已变化"):
            self.repo.update_version_remark("v1.0.0", "overwrite", expected=snapshot)
        snapshot = self.repo.preview_version_remark("v1.0.0")
        with self.assertRaisesRegex(MODULE.GitError, "已变化"):
            self.repo.update_version_remark("v1.0.0", "overwrite", expected=replace(snapshot, repository=self.root))
        self.beta()
        with self.assertRaises(MODULE.GitError):
            self.repo.update_version_remark("v1.0.0", "overwrite", expected=snapshot)

    def test_late_tag_change_and_head_lock_prevent_publication(self):
        original = self.repo.run
        def concurrent(args, *positional, **kwargs):
            if args == ["update-ref", "--stdin"]:
                original(["tag", "-f", "-a", "v1.0.0", "-m", "concurrent"])
            return original(args, *positional, **kwargs)
        with mock.patch.object(self.repo, "run", side_effect=concurrent):
            with self.assertRaises(MODULE.GitError):
                self.repo.update_version_remark("v1.0.0", "测试中")
        self.assertNotIn(self.note_ref(), self.repo._local_refs())
        self.repo._git_path("HEAD.lock").write_text("other writer")
        with self.assertRaisesRegex(MODULE.GitError, "锁定"):
            self.repo.update_version_remark("v1.0.0", "测试中")
        self.assertEqual("other writer", self.repo._git_path("HEAD.lock").read_text())

    def test_notes_only_upload_download_overwrite_and_clear_preserve_dirty_workspaces(self):
        self.repo.sync_remote()
        peer = self.peer()
        for repo in (self.repo, peer):
            (repo.path / "source.txt").write_text("staged\n")
            repo.run(["add", "source.txt"])
            (repo.path / "source.txt").write_text("unstaged\n")
        before = self.state()
        peer_before = self.state(peer)
        self.repo.update_version_remark("v1.0.0", "A 现场已更新\nB 测试中")
        snapshot, status = self.status()
        self.assertTrue(status.can_sync_remote, status.remote_message)
        comparison = status.comparisons[0]
        self.assertEqual("remark_changed", comparison.relationship)
        self.assertEqual("版本备注 v1.0.0", comparison.name)
        self.assertIn("新增", comparison.effect("remote"))
        with mock.patch.object(self.repo, "refresh_workspace_generated_files", side_effect=AssertionError("no files")):
            self.repo.sync_remote_from_local(snapshot, status)
        self.assertEqual(before, self.state())
        with mock.patch.object(peer, "_generated_file_transition", side_effect=AssertionError("no checkout")), \
                mock.patch.object(peer, "_assert_sync_worktrees", side_effect=AssertionError("no worktree checks")):
            self.pull(peer)
        self.assertEqual(peer_before, self.state(peer))
        self.assertEqual("A 现场已更新\nB 测试中", peer.version_remark("v1.0.0"))
        peer.update_version_remark("v1.0.0", "现场验收中")
        peer.sync_remote()
        self.pull(self.repo)
        self.assertEqual("现场验收中", self.repo.version_remark("v1.0.0"))
        recovery = self.repo.run(["for-each-ref", "--format=%(refname)", "refs/code-version-manager/recovery/"])
        self.assertIn("/before/notes/code-version-manager/remarks/v1.0.0", recovery)
        peer.update_version_remark("v1.0.0", "")
        snapshot, status = self.status(peer)
        self.assertIn("清空", status.comparisons[0].effect("remote"))
        peer.sync_remote_from_local(snapshot, status)
        self.pull(self.repo)
        self.assertEqual("", self.repo.version_remark("v1.0.0"))
        self.assertEqual(before, self.state())

    def test_confirmation_rejects_local_or_remote_note_change(self):
        self.repo.sync_remote()
        peer = self.peer()
        self.repo.update_version_remark("v1.0.0", "local")
        snapshot, status = self.status()
        peer.update_version_remark("v1.0.0", "remote")
        peer.sync_remote()
        with self.assertRaisesRegex(MODULE.GitError, "远程.*变化"):
            self.repo.sync_remote_from_local(snapshot, status)
        snapshot, status = self.status()
        self.repo.update_version_remark("v1.0.0", "new local")
        with self.assertRaisesRegex(MODULE.GitError, "本地.*变化"):
            self.repo.sync_local_from_remote(snapshot, status)

    def test_remote_lease_rejects_note_changed_after_final_confirmation(self):
        self.repo.sync_remote()
        peer = self.peer()
        self.repo.update_version_remark("v1.0.0", "local")
        snapshot, status = self.status()
        original = self.repo._run_remote
        def concurrent(args, *positional, **kwargs):
            if args[0] == "push" and "--dry-run" not in args:
                peer.update_version_remark("v1.0.0", "concurrent remote")
                peer.sync_remote()
            return original(args, *positional, **kwargs)
        with mock.patch.object(self.repo, "_run_remote", side_effect=concurrent):
            with self.assertRaises(MODULE.GitError):
                self.repo.sync_remote_from_local(snapshot, status)
        self.assertEqual(peer._local_refs()[self.note_ref()],
                         self.repo._remote_refs(str(self.remote))[self.note_ref()])

    def test_branch_scope_import_and_advanced_sync(self):
        self.repo.update_version_remark("v1.0.0", "main note")
        self.repo.sync_remote()
        beta = self.beta()
        self.repo.update_version_remark(beta, "beta note")
        self.repo.sync_remote()
        peer = self.peer()
        self.pull(peer)
        peer.switch_branch("feature/camera", source="remote")
        self.assertEqual("beta note", peer.version_remark(beta))
        self.repo.switch_branch("main")
        self.repo.update_version_remark("v1.0.0", "new main note")
        self.repo.switch_branch("feature/camera")
        self.repo.update_version_remark(beta, "new beta note")
        self.repo.sync_remote()
        self.pull(peer)
        self.assertEqual("main note", peer.version_remark("v1.0.0"))
        self.assertEqual("new beta note", peer.version_remark(beta))
        self.repo.sync_remote(scope="all")
        snapshot, status = self.status(peer, scope="all")
        peer.sync_local_from_remote(snapshot, status)
        self.assertEqual("new main note", peer.version_remark("v1.0.0"))

    def test_unrelated_notes_are_excluded_and_lease_is_precise(self):
        self.repo.run(["notes", "add", "-m", "unrelated"])
        self.repo.update_version_remark("v1.0.0", "remark")
        snapshot, status = self.status()
        args = self.repo._push_ref_differences(snapshot, dict(status.remote_refs))
        self.assertIn("--atomic", args)
        self.assertIn("--force-with-lease=" + self.note_ref() + ":", args)
        self.assertFalse(any(arg.startswith("+") for arg in args))
        self.repo.sync_remote()
        self.assertNotIn("refs/notes/commits", self.repo._remote_refs(str(self.remote)))
        self.assertEqual("", self.repo.run(["--git-dir=" + str(self.remote), "for-each-ref", "refs/notes/commits"]))

    def test_delete_tag_and_commit_remove_note_and_preserve_recovery(self):
        (self.path / "source.txt").write_text("second\n")
        self.repo.save_version("v1.0.1", "second", ["source.txt"])
        self.repo.update_version_remark("v1.0.1", "testing")
        self.repo.update_version_remark("v1.0.0", "keep")
        plan = self.repo.preview_version_deletion("v1.0.1")
        self.repo.delete_latest_version_commit("v1.0.1", expected=plan)
        self.assertNotIn(self.note_ref("v1.0.1"), self.repo._local_refs())
        saved = self.repo.run(["for-each-ref", "--format=%(refname) %(objectname)", "refs/code-version-manager/recovery/"])
        self.assertIn("/before/notes/code-version-manager/remarks/v1.0.1 " + plan.remark_oid, saved)
        self.assertEqual("keep", self.repo.version_remark("v1.0.0"))
        self.repo.delete_version("v1.0.0")
        self.assertNotIn(self.note_ref(), self.repo._local_refs())

    def test_delete_branch_removes_only_its_notes_locally_and_remotely(self):
        self.repo.update_version_remark("v1.0.0", "keep")
        self.repo.sync_remote()
        beta = self.beta()
        self.repo.update_version_remark(beta, "delete")
        self.repo.sync_remote()
        self.repo.switch_branch("main")
        preview = self.repo.preview_branch_deletion("feature/camera")
        self.repo.delete_branch_with_remote("feature/camera", preview.local, preview.remote)
        for refs in (self.repo._local_refs(), self.repo._remote_refs(str(self.remote))):
            self.assertIn(self.note_ref(), refs)
            self.assertNotIn(self.note_ref(beta), refs)

    def test_late_note_creation_cannot_partially_delete_tip(self):
        beta = self.beta()
        plan = self.repo.preview_version_deletion(beta)
        original = self.repo._replace_managed_refs
        def concurrent(before, after):
            self.repo.update_version_remark(beta, "concurrent note")
            original(before, after)
        with mock.patch.object(self.repo, "_replace_managed_refs", side_effect=concurrent):
            with self.assertRaises(MODULE.GitError):
                self.repo.delete_latest_version_commit(beta, expected=plan)
        self.assertEqual(plan.head_oid, self.repo._local_refs()["refs/heads/feature/camera"])
        self.assertEqual(plan.tag_oid, self.repo._local_refs()["refs/tags/" + beta])
        self.assertEqual("concurrent note", self.repo.version_remark(beta))

    def test_index_install_failure_rolls_back_note_with_branch_and_tag(self):
        beta = self.beta()
        self.repo.update_version_remark(beta, "keep on rollback")
        before = self.repo._local_refs()
        original = MODULE.os.replace
        index = self.repo._git_path("index")
        def fail_install(source, destination):
            if destination == index:
                raise OSError("index install failed")
            return original(source, destination)
        with mock.patch.object(MODULE.os, "replace", side_effect=fail_install):
            with self.assertRaisesRegex(MODULE.GitError, "index install failed"):
                self.repo.delete_latest_version_commit(beta)
        self.assertEqual(before, self.repo._local_refs())
        self.assertEqual("keep on rollback", self.repo.version_remark(beta))

    def test_isolated_workspace_verifier_handles_notes_without_changing_source(self):
        from verify_workspace_copy import verify
        (self.path / "source.txt").write_text("second\n")
        self.repo.save_version("v1.0.1", "second", ["source.txt"])
        self.repo.update_version_remark("v1.0.1", "测试中")
        report = verify(self.path)
        self.assertTrue(report["source_unchanged"])
        self.assertEqual("passed", report["tip_version_withdrawal"]["state"])
        self.assertEqual("测试中", self.repo.version_remark("v1.0.1"))


class VersionRemarksUITest(unittest.TestCase):
    def setUp(self):
        if not MODULE.Gtk.init_check()[0]:
            self.skipTest("GTK display unavailable")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        patcher = mock.patch.object(MODULE, "settings_file_path", return_value=self.path / "settings.ini")
        patcher.start()
        self.addCleanup(patcher.stop)
        with mock.patch.object(MODULE.VersionManagerWindow, "load_workspace"):
            self.window = MODULE.VersionManagerWindow(None, self.path)
        self.addCleanup(self.window.destroy)

    def test_remark_column_dispatch_and_full_tooltip(self):
        window = self.window
        window.repository = mock.Mock()
        entry = MODULE.HistoryEntry("a" * 40, "aaaaaaa", "today", "base", ("v1.0.0",), remark="A 现场\n测试中")
        window.history_entries = [entry]
        window.history_store.append(["v1.0.0", "today", "base", "aaaaaaa", entry.commit, "A 现场 测试中"])
        self.assertEqual("备注", window.history_tree.get_columns()[-1].get_title())
        path = MODULE.Gtk.TreePath.new_from_indices([0])
        with mock.patch.object(window, "run_background") as background:
            window.on_history_activated(window.history_tree, path, window.remark_column)
            background.call_args.args[1]()
        window.repository.preview_version_remark.assert_called_once_with("v1.0.0")
        tree = mock.Mock()
        tree.get_cursor.return_value = (path, window.remark_column)
        tooltip = mock.Mock()
        self.assertTrue(window._history_remark_tooltip(tree, 0, 0, True, tooltip))
        tooltip.set_text.assert_called_once_with(entry.remark)
        window.history_entries = [replace(entry, read_only=True)]
        with mock.patch.object(window, "prompt_version_remark") as prompt:
            window.on_history_activated(window.history_tree, path, window.remark_column)
        prompt.assert_called_once_with("v1.0.0", entry.remark, read_only=True)

    def test_cancel_and_stale_repository_do_not_save(self):
        window = self.window
        repo = mock.Mock()
        window.repository = repo
        snapshot = MODULE.VersionRemarkSnapshot(self.path, "v1.0.0", "main", "a" * 40, "b" * 40, None, "")
        with mock.patch.object(window, "prompt_version_remark", return_value=None), \
                mock.patch.object(window, "refresh"), mock.patch.object(window, "run_background") as background:
            window._background_remark_ready(repo, snapshot)
        background.assert_not_called()
        repo.update_version_remark.assert_not_called()
        window.repository = mock.Mock()
        with mock.patch.object(window, "prompt_version_remark") as prompt:
            window._background_remark_ready(repo, snapshot)
        prompt.assert_not_called()

    def test_editor_shortcuts_multiline_clear_and_cancel(self):
        def widgets(widget):
            yield widget
            if isinstance(widget, MODULE.Gtk.Container):
                for child in widget.get_children():
                    yield from widgets(child)
        for shortcut, response, expected in (("测试中", MODULE.Gtk.ResponseType.OK, "测试中"),
                                              ("已更新现场", MODULE.Gtk.ResponseType.OK, "已更新现场"),
                                              ("清空", MODULE.Gtk.ResponseType.OK, ""),
                                              (None, MODULE.Gtk.ResponseType.OK, "A 现场\nB 测试中"),
                                              (None, MODULE.Gtk.ResponseType.CANCEL, None)):
            def interact():
                dialog = next(window for window in MODULE.Gtk.Window.list_toplevels()
                              if isinstance(window, MODULE.Gtk.Dialog) and "备注" in window.get_title())
                if shortcut:
                    next(widget for widget in widgets(dialog) if isinstance(widget, MODULE.Gtk.Button)
                         and widget.get_label() == shortcut).clicked()
                dialog.response(response)
                return False
            MODULE.GLib.idle_add(interact)
            self.assertEqual(expected, self.window.prompt_version_remark("v1.0.0", "A 现场\nB 测试中"))
