"""Recovery cleanup uses disposable repositories and preserves user state."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_backend import MODULE


class RecoveryCleanupTest(unittest.TestCase):
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
        self.recovery_root = self.repo.metadata_path / "recovery"

    def recovery(self, name="1700000000-1234abcd", operation="push", refs=True):
        namespace = f"refs/code-version-manager/recovery/{name}/{operation}"
        if refs:
            self.repo._save_recovery_refs(namespace, self.repo._local_refs(), "before")
        backup = self.repo._save_metadata_recovery(namespace)
        return backup, namespace

    def refs(self):
        return self.repo._recovery_cleanup_refs()

    def state(self):
        return (
            self.repo.run(["for-each-ref", "--format=%(refname) %(objectname)",
                           "refs/heads/", "refs/tags/", "refs/remotes/", "refs/stash"]),
            self.repo._git_path("HEAD").read_bytes(),
            self.repo._git_path("index").read_bytes(),
            self.repo._git_path("config").read_bytes(),
            {str(path.relative_to(self.path)): path.read_bytes()
             for path in self.path.rglob("*") if path.is_file() and ".git" not in path.relative_to(self.path).parts},
            {str(path.relative_to(self.repo.metadata_path)): path.read_bytes()
             for path in self.repo.metadata_path.rglob("*") if path.is_file()
             and "recovery" not in path.relative_to(self.repo.metadata_path).parts},
        )

    def test_cleans_backups_and_corresponding_refs_without_touching_dirty_workspace(self):
        self.recovery()
        backup2, _ = self.recovery("1700000001-1234abcd", "delete-version-commit")
        (backup2 / "index").write_bytes(self.repo._git_path("index").read_bytes())
        self.repo.run(["update-ref", "refs/code-version-manager/recovery/1600000000-abcdef12/push/before/heads/main",
                       self.repo.run(["rev-parse", "HEAD"]).strip()])
        (self.path / "source.txt").write_text("user stash\n")
        self.repo.run(["stash", "push", "-m", "user stash"])
        (self.path / "source.txt").write_text("staged\n")
        self.repo.run(["add", "source.txt"])
        (self.path / "source.txt").write_text("unstaged\n")
        (self.path / "untracked.txt").write_text("keep\n")
        before = self.state()
        plan = self.repo.plan_recovery_cleanup()
        self.assertEqual(2, len(plan.entries))
        self.assertGreater(plan.size, 0)
        self.assertEqual(self.recovery_root, plan.root)
        with mock.patch.object(self.repo, "_run_remote", side_effect=AssertionError("cleanup is local")):
            message = self.repo.clear_recovery_files(plan)
        self.assertIn("成功 2 个，保留 0 项，失败 0 个", message)
        self.assertEqual(before, self.state())
        self.assertEqual([], list(self.recovery_root.iterdir()))
        self.assertEqual(1, len(self.refs()))
        self.assertIn("1600000000-abcdef12", next(iter(self.refs())))
        self.assertEqual((), self.repo.plan_recovery_cleanup().entries)
        self.assertIn("成功 0 个", self.repo.clear_recovery_files(self.repo.plan_recovery_cleanup()))

    def test_preserves_stash_and_ignored_files_across_repository_reopening(self):
        self.recovery()
        stash_backup, namespace = self.recovery("1700000001-1234abcd", "pull")
        (self.path / "source.txt").write_text("unrecovered source\n")
        self.repo.run(["stash", "push", "-m", "sync saved edits"])
        stash_oid = self.repo.run(["rev-parse", "refs/stash"]).strip()
        self.repo.run(["update-ref", namespace + "/before/worktree", stash_oid])
        ignored_backup, _ = self.recovery("1700000002-1234abcd", "pull")
        ignored = ignored_backup / "ignored-worktree"
        ignored.mkdir()
        (ignored / "user.log").write_text("only saved copy\n")
        self.repo = MODULE.GitRepository(self.path)
        self.assertIsNone(self.repo.last_local_sync_stash_ref)
        plan = self.repo.plan_recovery_cleanup()
        self.assertEqual(1, len(plan.entries))
        self.assertEqual(2, len(plan.preserved))
        self.repo.clear_recovery_files(plan)
        self.assertTrue(stash_backup.is_dir())
        self.assertEqual("only saved copy\n", (ignored / "user.log").read_text())
        self.assertEqual(stash_oid, self.repo.run(["rev-parse", namespace + "/before/worktree"]).strip())
        self.assertEqual("unrecovered source\n", self.repo.run(["show", stash_oid + ":source.txt"]))
        self.assertEqual(stash_oid, self.repo.run(["rev-parse", "refs/stash"]).strip())

    def test_missing_empty_and_unreferenced_backup_directories(self):
        missing = self.repo.plan_recovery_cleanup()
        self.assertEqual((), missing.entries)
        self.assertFalse(self.recovery_root.exists())
        self.repo.clear_recovery_files(missing)
        self.assertFalse(self.recovery_root.exists())
        self.recovery_root.mkdir()
        self.assertEqual((), self.repo.plan_recovery_cleanup().entries)
        backup, _ = self.recovery(refs=False)
        self.repo.clear_recovery_files(self.repo.plan_recovery_cleanup())
        self.assertFalse(backup.exists())

    def test_unknown_names_symlinks_and_special_files_are_preserved(self):
        normal, _ = self.recovery()
        unknown = self.recovery_root / "user-backup"
        unknown.mkdir()
        (unknown / "important.txt").write_text("keep\n")
        external = self.root / "external"
        external.mkdir()
        (external / "important.txt").write_text("outside\n")
        (self.recovery_root / "1700000001-1234abcd").symlink_to(external, target_is_directory=True)
        with_link, _ = self.recovery("1700000002-1234abcd")
        (with_link / "link").symlink_to(external)
        with_fifo, _ = self.recovery("1700000003-1234abcd")
        os.mkfifo(with_fifo / "fifo")
        plan = self.repo.plan_recovery_cleanup()
        self.assertEqual([normal.name], [entry.name for entry in plan.entries])
        self.assertEqual(4, len(plan.preserved))
        self.repo.clear_recovery_files(plan)
        self.assertTrue(unknown.is_dir())
        self.assertTrue(with_link.is_dir())
        self.assertTrue(with_fifo.is_dir())
        self.assertEqual("outside\n", (external / "important.txt").read_text())

    def test_recovery_or_metadata_root_symlink_is_rejected(self):
        backup, _ = self.recovery()
        external = self.root / "external"
        external.mkdir()
        (external / "sentinel").write_text("keep\n")
        original = self.recovery_root.with_name("original-recovery")
        self.recovery_root.rename(original)
        self.recovery_root.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(MODULE.GitError, "普通目录"):
            self.repo.plan_recovery_cleanup()
        self.recovery_root.unlink()
        original.rename(self.recovery_root)
        metadata = self.repo.metadata_path
        moved = metadata.with_name("original-metadata")
        metadata.rename(moved)
        metadata.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(MODULE.GitError, "普通目录"):
            self.repo.plan_recovery_cleanup()
        metadata.unlink()
        moved.rename(metadata)
        self.assertTrue(backup.exists())
        self.assertEqual("keep\n", (external / "sentinel").read_text())

    def test_confirmation_rejects_changed_files_or_refs_before_any_deletion(self):
        for change in ("file", "ref", "new-ref", "stash", "ignored", "removed", "replaced-root"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                # Separate copies keep each stale-confirmation case independent.
                original_repo, original_root = self.repo, self.recovery_root
                copy = Path(temporary) / "copy"
                shutil.copytree(self.path, copy)
                self.repo = MODULE.GitRepository(copy)
                self.recovery_root = self.repo.metadata_path / "recovery"
                try:
                    backup, namespace = self.recovery()
                    untouched, _ = self.recovery("1700000001-1234abcd")
                    plan = self.repo.plan_recovery_cleanup()
                    if change == "file":
                        (backup / "state.json").write_text("changed\n")
                    elif change == "ref":
                        oid = self.repo.run(["rev-parse", "v1.0.0"]).strip()
                        self.repo.run(["update-ref", namespace + "/before/heads/main", oid])
                    elif change in ("new-ref", "stash"):
                        self.repo.run(["update-ref", namespace + ("/before/worktree" if change == "stash" else "/incoming/heads/main"),
                                       self.repo.run(["rev-parse", "HEAD"]).strip()])
                    elif change == "ignored":
                        (backup / "ignored-worktree").mkdir()
                    elif change == "removed":
                        shutil.rmtree(backup)
                    else:
                        moved = self.recovery_root.with_name("old-recovery")
                        self.recovery_root.rename(moved)
                        shutil.copytree(moved, self.recovery_root)
                    refs = self.refs()
                    with self.assertRaisesRegex(MODULE.GitError, "重新检查"):
                        self.repo.clear_recovery_files(plan)
                    self.assertTrue(untouched.exists())
                    self.assertEqual(refs, self.refs())
                finally:
                    self.repo, self.recovery_root = original_repo, original_root

    def test_new_recovery_points_are_left_for_next_cleanup(self):
        backup, _ = self.recovery()
        plan = self.repo.plan_recovery_cleanup()
        added, namespace = self.recovery("1700000001-1234abcd")
        message = self.repo.clear_recovery_files(plan)
        self.assertFalse(backup.exists())
        self.assertTrue(added.exists())
        self.assertIn("保留 1 项", message)
        self.assertTrue(all(ref.startswith(namespace + "/") for ref in self.refs()))

    def test_failed_deletion_preserves_remaining_files_and_restores_refs(self):
        backup, _ = self.recovery()
        (backup / "delete-first.txt").write_text("old backup\n")
        later, _ = self.recovery("1700000001-1234abcd")
        refs = self.refs()
        remove = MODULE.GitRepository._remove_recovery_tree
        def fail_after_partial(parent_fd, name):
            if name == backup.name:
                descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                try:
                    os.unlink("delete-first.txt", dir_fd=descriptor)
                finally:
                    os.close(descriptor)
                raise OSError("injected permission failure")
            return remove(parent_fd, name)
        with mock.patch.object(self.repo, "_remove_recovery_tree", side_effect=fail_after_partial):
            message = self.repo.clear_recovery_files(self.repo.plan_recovery_cleanup())
        self.assertIn("成功 0 个，保留 0 项，失败 1 个，未处理 1 个", message)
        self.assertIn(str(backup), message)
        self.assertFalse((backup / "delete-first.txt").exists())
        self.assertTrue((backup / "git-config").is_file())
        self.assertTrue(later.is_dir())
        self.assertEqual(refs, self.refs())

    def test_ref_transaction_failure_keeps_files_and_concurrent_reference(self):
        backup, namespace = self.recovery()
        plan = self.repo.plan_recovery_cleanup()
        run = self.repo.run
        replacement = self.repo.run(["rev-parse", "v1.0.0"]).strip()
        def replace_before_transaction(args, **kwargs):
            if args == ["update-ref", "--stdin"]:
                run(["update-ref", namespace + "/before/heads/main", replacement])
            return run(args, **kwargs)
        with mock.patch.object(self.repo, "run", side_effect=replace_before_transaction):
            message = self.repo.clear_recovery_files(plan)
        self.assertIn("失败 1 个", message)
        self.assertTrue((backup / "git-config").is_file())
        self.assertEqual(replacement, self.refs()[namespace + "/before/heads/main"])

    def test_new_worktree_ref_during_deletion_preserves_backup(self):
        backup, namespace = self.recovery()
        run = self.repo.run
        def add_after_transaction(args, **kwargs):
            result = run(args, **kwargs)
            if args == ["update-ref", "--stdin"] and "delete " in kwargs.get("input", ""):
                run(["update-ref", namespace + "/before/worktree", run(["rev-parse", "HEAD"]).strip()])
            return result
        with mock.patch.object(self.repo, "run", side_effect=add_after_transaction):
            message = self.repo.clear_recovery_files(self.repo.plan_recovery_cleanup())
        self.assertIn("失败 1 个", message)
        self.assertTrue(backup.is_dir())
        self.assertIn(namespace + "/before/worktree", self.refs())

    def test_root_replacement_during_deletion_is_rejected(self):
        backup, _ = self.recovery()
        plan = self.repo.plan_recovery_cleanup()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "sentinel").write_text("preserve\n")
        run = self.repo.run
        moved = self.recovery_root.with_name("moved-recovery")
        def replace_root(args, **kwargs):
            result = run(args, **kwargs)
            if args == ["update-ref", "--stdin"] and "delete " in kwargs.get("input", ""):
                self.recovery_root.rename(moved)
                self.recovery_root.symlink_to(outside, target_is_directory=True)
            return result
        refs = self.refs()
        with mock.patch.object(self.repo, "run", side_effect=replace_root):
            message = self.repo.clear_recovery_files(plan)
        self.assertIn("失败 1 个", message)
        self.assertTrue((moved / backup.name / "git-config").is_file())
        self.assertEqual(refs, self.refs())
        self.assertEqual("preserve\n", (outside / "sentinel").read_text())

    def test_restore_reference_failure_reports_remaining_files(self):
        backup, _ = self.recovery()
        run = self.repo.run
        def fail_restore(args, **kwargs):
            if args == ["update-ref", "--stdin"] and "create " in kwargs.get("input", ""):
                raise MODULE.GitError("concurrent recovery ref")
            return run(args, **kwargs)
        with mock.patch.object(self.repo, "run", side_effect=fail_restore), \
                mock.patch.object(self.repo, "_remove_recovery_tree", side_effect=OSError("permission denied")):
            message = self.repo.clear_recovery_files(self.repo.plan_recovery_cleanup())
        self.assertIn("恢复引用未还原", message)
        self.assertIn(str(backup), message)
        self.assertTrue(backup.is_dir())

    def test_unfinished_operations_block_preview_and_execution(self):
        self.recovery()
        plan = self.repo.plan_recovery_cleanup()
        for marker in (self.repo._git_path("MERGE_HEAD"), self.repo.metadata_path / "merge.json"):
            marker.write_text("{}")
            try:
                with self.assertRaises(MODULE.GitError):
                    self.repo.plan_recovery_cleanup()
                with self.assertRaises(MODULE.GitError):
                    self.repo.clear_recovery_files(plan)
                self.assertTrue(self.recovery_root.is_dir())
            finally:
                marker.unlink()

    def test_linked_worktree_only_cleans_its_own_metadata(self):
        main_backup, _ = self.recovery()
        worktree = self.root / "linked"
        self.repo.run(["worktree", "add", "-b", "linked", str(worktree)])
        repo = MODULE.GitRepository(worktree)
        namespace = "refs/code-version-manager/recovery/1700000002-1234abcd/push"
        repo._save_recovery_refs(namespace, repo._local_refs(), "before")
        linked_backup = repo._save_metadata_recovery(namespace)
        plan = repo.plan_recovery_cleanup()
        self.assertEqual(linked_backup.parent, plan.root)
        repo.clear_recovery_files(plan)
        self.assertFalse(linked_backup.exists())
        self.assertTrue(main_backup.is_dir())
        self.assertTrue(self.refs())


class RecoveryCleanupUITest(unittest.TestCase):
    def plan(self, empty=False):
        entry = MODULE.RecoveryCleanupEntry("1700000000-1234abcd", "signature", 8192, ())
        return MODULE.RecoveryCleanupPlan(Path("/tmp/workspace/.git/code-version-manager/recovery"),
                                          (1, 2), () if empty else (entry,),
                                          (("1700000001-1234abcd", "含工作区 stash"),))

    def test_click_uses_background_scan_and_busy_or_missing_repository_does_nothing(self):
        window = mock.Mock(busy=False, branch_generation=7)
        MODULE.VersionManagerWindow.on_clear_recovery_files(window, None)
        args = window.run_background.call_args
        self.assertEqual(window.repository.plan_recovery_cleanup, args.args[1])
        args.kwargs["result_callback"](self.plan())
        window._background_recovery_cleanup_ready.assert_called_once_with(window.repository, 8, self.plan())
        args.kwargs["error_callback"]("error")
        window._background_recovery_cleanup_failed.assert_called_once_with(window.repository, 8, "error")
        for repository, busy in ((None, False), (mock.Mock(), True)):
            window = mock.Mock(repository=repository, busy=busy)
            MODULE.VersionManagerWindow.on_clear_recovery_files(window, None)
            window.run_background.assert_not_called()

    def test_button_disabled_for_operation_busy_or_missing_repository(self):
        for state in ("normal", "busy", "no-repo", "operation"):
            with self.subTest(state=state):
                window = mock.Mock(busy=state == "busy")
                if state == "no-repo":
                    window.repository = None
                elif state == "operation":
                    window.repository._assert_no_operation.side_effect = MODULE.GitError("先完成合并")
                MODULE.VersionManagerWindow._refresh_recovery_cleanup_button(window)
                window.clear_recovery_button.set_sensitive.assert_called_once_with(state == "normal")

    def test_background_failure_uses_main_thread_error_callback_and_disables_button(self):
        window = mock.Mock(busy=False, branch_generation=7)
        error_callback = mock.Mock()
        operation = mock.Mock(side_effect=MODULE.GitError("cleanup failed"))
        with mock.patch.object(MODULE.GLib, "timeout_add_seconds", return_value=42), \
                mock.patch.object(MODULE.GLib, "idle_add") as idle_add, \
                mock.patch.object(MODULE.threading, "Thread") as thread:
            MODULE.VersionManagerWindow.run_background(
                window, "正在清理…", operation, "", error_callback=error_callback,
            )
            thread.call_args.kwargs["target"]()
        window.clear_recovery_button.set_sensitive.assert_called_once_with(False)
        idle_add.assert_called_once_with(error_callback, "cleanup failed")
        error_callback.assert_not_called()

    def test_real_window_places_cleanup_button_next_to_storage(self):
        if not MODULE.Gtk.init_check()[0]:
            self.skipTest("GTK display unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            window = None
            try:
                application = MODULE.Gtk.Application()
                application.register(None)
                with mock.patch.object(MODULE.VersionManagerWindow, "_save_repository_path"), \
                        mock.patch.object(MODULE.VersionManagerWindow, "_refresh_remote_status"), \
                        mock.patch.object(MODULE, "settings_file_path", return_value=path / "settings.ini"):
                    window = MODULE.VersionManagerWindow(application, path)
                    self.assertEqual("清空恢复文件", window.clear_recovery_button.get_label())
                    parent = window.clear_recovery_button.get_parent()
                    self.assertIs(parent, window.storage_label.get_parent())
                    self.assertFalse(window.clear_recovery_button.get_sensitive())
            finally:
                if window is not None:
                    window.destroy()

    def test_confirm_cancel_and_empty_preview(self):
        for response, empty in ((MODULE.Gtk.ResponseType.OK, False),
                                (MODULE.Gtk.ResponseType.CANCEL, False),
                                (MODULE.Gtk.ResponseType.OK, True)):
            with self.subTest(response=response, empty=empty):
                window = mock.Mock(branch_generation=8)
                repository = window.repository
                plan = self.plan(empty)
                with mock.patch.object(MODULE.Gtk, "MessageDialog") as factory:
                    dialog = factory.return_value
                    dialog.run.return_value = response
                    MODULE.VersionManagerWindow._background_recovery_cleanup_ready(window, repository, 8, plan)
                description = dialog.format_secondary_text.call_args.args[0]
                self.assertIn(str(plan.root), description)
                self.assertIn("保留", description)
                dialog.set_default_response.assert_called_once_with(MODULE.Gtk.ResponseType.CANCEL)
                if response == MODULE.Gtk.ResponseType.OK and not empty:
                    self.assertIn("8.0 KB", description)
                    args = window.run_background.call_args
                    args.args[1]()
                    repository.clear_recovery_files.assert_called_once_with(plan)
                    args.kwargs["result_callback"]("done")
                    window._background_recovery_cleanup_complete.assert_called_once_with(repository, 9, "done")
                else:
                    window.run_background.assert_not_called()
                    repository.clear_recovery_files.assert_not_called()

    def test_old_success_error_and_preview_callbacks_do_not_change_new_workspace(self):
        for stale in ("repository", "generation"):
            window = mock.Mock(branch_generation=8)
            repository = mock.Mock() if stale == "repository" else window.repository
            generation = 8 if stale == "repository" else 7
            with mock.patch.object(MODULE.Gtk, "MessageDialog") as dialog:
                MODULE.VersionManagerWindow._background_recovery_cleanup_ready(window, repository, generation, self.plan())
                MODULE.VersionManagerWindow._background_recovery_cleanup_complete(window, repository, generation, "done")
                MODULE.VersionManagerWindow._background_recovery_cleanup_failed(window, repository, generation, "error")
            dialog.assert_not_called()
            window._end_background.assert_not_called()
            window.show_error.assert_not_called()

    def test_workspace_change_during_confirmation_cancels_execution(self):
        window = mock.Mock(branch_generation=8)
        repository = window.repository
        def change_workspace():
            window.repository = mock.Mock()
            return MODULE.Gtk.ResponseType.OK
        with mock.patch.object(MODULE.Gtk, "MessageDialog") as factory:
            factory.return_value.run.side_effect = change_workspace
            MODULE.VersionManagerWindow._background_recovery_cleanup_ready(window, repository, 8, self.plan())
        window.run_background.assert_not_called()

    def test_completion_refreshes_without_remote_contact_and_shows_result(self):
        window = mock.Mock(branch_generation=8)
        message = "恢复文件清理结果：成功 1 个，保留 1 项，失败 0 个，未处理 0 个。"
        with mock.patch.object(MODULE.Gtk, "MessageDialog") as factory:
            MODULE.VersionManagerWindow._background_recovery_cleanup_complete(window, window.repository, 8, message)
        window.refresh.assert_called_once_with(update_remote=False)
        window._set_operation_status.assert_called_once_with(message, level="info")
        factory.return_value.format_secondary_text.assert_called_once_with(message)
        self.assertEqual(MODULE.Gtk.MessageType.INFO, factory.call_args.kwargs["message_type"])


if __name__ == "__main__":
    unittest.main()
