"""Version-tip withdrawal preserves source files and uses checked local refs."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_backend import MODULE


class VersionCommitDeletionTest(unittest.TestCase):
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
        self.branch = "feature/camera"
        self.first = MODULE.version_tag_name(self.branch, "v1.0.0-beta.0.0.1")
        self.second = MODULE.version_tag_name(self.branch, "v1.0.0-beta.0.0.2")

    def publish_betas(self, count=2):
        self.repo.create_branch(self.branch, "v1.0.0")
        (self.path / "source.txt").write_text("first beta\n")
        self.repo.save_version(self.first, "first beta", ["source.txt"])
        if count == 2:
            (self.path / "source.txt").write_text("second beta\n")
            (self.path / "added.txt").write_text("added in second beta\n")
            self.repo.save_version(self.second, "second beta", ["source.txt", "added.txt"])

    def manifest(self, version):
        return self.repo.metadata_path / "versions" / (version + ".manifest")

    def state(self):
        return (
            self.repo._local_refs(), self.repo._git_path("index").read_bytes(),
            {path.name: path.read_bytes() for path in self.path.iterdir() if path.is_file()},
        )

    def recovery(self):
        paths = list((self.repo.metadata_path / "recovery").glob("*/version-deletion.json"))
        self.assertEqual(1, len(paths))
        backup = paths[0].parent
        record = json.loads(paths[0].read_text())
        namespace = f"refs/code-version-manager/recovery/{backup.name}/delete-version-commit/before"
        return backup, record, namespace

    def assert_rejected(self, version, reason):
        before = self.state()
        plan = self.repo.preview_version_deletion(version)
        self.assertFalse(plan.can_delete_commit)
        self.assertIn(reason, plan.commit_error)
        with self.assertRaisesRegex(MODULE.GitError, reason):
            self.repo.delete_latest_version_commit(version, expected=plan)
        self.assertEqual(before, self.state())

    def test_beta_tip_removal_preserves_local_files_and_saves_recovery(self):
        self.publish_betas()
        self.repo.run(["branch", "unrelated", "main"])
        before = self.repo._local_refs()
        plan = self.repo.preview_version_deletion(self.second)
        index = self.repo._git_path("index").read_bytes()
        (self.path / "source.txt").write_text("local fix\n")
        (self.path / "untracked.txt").write_text("keep me\n")
        with mock.patch.object(self.repo, "_run_remote", side_effect=AssertionError("beta deletion must be local")):
            message = self.repo.delete_latest_version_commit(self.second, expected=plan)

        self.assertEqual(self.branch, self.repo.current_branch())
        self.assertEqual(plan.parent_oid, self.repo.run(["rev-parse", "HEAD"]).strip())
        after = dict(before)
        after[f"refs/heads/{self.branch}"] = plan.parent_oid
        del after[f"refs/tags/{self.second}"]
        self.assertEqual(after, self.repo._local_refs())
        self.assertEqual("local fix\n", (self.path / "source.txt").read_text())
        self.assertEqual("keep me\n", (self.path / "untracked.txt").read_text())
        self.assertEqual("added in second beta\n", (self.path / "added.txt").read_text())
        self.assertEqual("first beta\n", self.repo.run(["show", ":source.txt"]))
        self.assertEqual("", self.repo.run(["ls-files", "--", "added.txt"]))
        self.assertFalse(self.manifest(self.second).exists())
        self.assertTrue(self.manifest(self.first).is_file())
        self.assertEqual(MODULE.display_version(self.first) + "\n", (self.path / "VERSION").read_text())
        for changelog in (self.path / "CHANGELOG.rst", self.repo.metadata_path / "CHANGELOG.rst"):
            self.assertIn("first beta", changelog.read_text())
            self.assertNotIn("second beta", changelog.read_text())
        backup, record, namespace = self.recovery()
        self.assertEqual(index, (backup / "index").read_bytes())
        self.assertEqual(plan.head_oid, record["head_oid"])
        self.assertEqual(plan.head_oid, self.repo.run(["rev-parse", f"{namespace}/heads/{self.branch}"]).strip())
        self.assertEqual(plan.tag_oid, self.repo.run(["rev-parse", f"{namespace}/tags/{self.second}"]).strip())
        self.assertTrue((backup / "git-config").is_file())
        self.assertTrue((backup / "versions" / (self.second + ".manifest")).is_file())
        self.assertIn(str(backup), message)
        self.assertFalse(self.repo._git_path("index.lock").exists())

    def test_last_imported_beta_without_manifest_keeps_baseline_and_branch(self):
        self.publish_betas(count=1)
        key = f"branch.{self.branch}.codeVersionManagerBase"
        self.repo.run(["config", "--local", "--unset-all", key])
        self.manifest(self.first).unlink()
        plan = self.repo.preview_version_deletion(self.first)
        self.repo.delete_latest_version_commit(self.first, expected=plan)
        self.assertEqual(self.branch, self.repo.current_branch())
        self.assertEqual("v1.0.0", self.repo.branch_base())
        self.assertEqual("v1.0.0", self.repo.run(["config", "--local", "--get", key]).strip())
        self.assertEqual("1.0.0\n", (self.path / "VERSION").read_text())
        self.assertEqual(self.first, self.repo.next_version())
        self.assertNotIn("first beta", (self.path / "CHANGELOG.rst").read_text())
        self.assertEqual(plan.parent_oid, self.repo.run(["rev-parse", "main"]).strip())

    def test_missing_cache_does_not_replace_fixed_base_with_nearer_formal_tag(self):
        (self.path / "source.txt").write_text("new main\n")
        self.repo.save_version("v1.1.0", "new main", ["source.txt"])
        self.repo.create_branch(self.branch, "v1.1.0")
        key = f"branch.{self.branch}.codeVersionManagerBase"
        self.repo.run(["config", "--local", key, "v1.0.0"])
        (self.path / "source.txt").write_text("beta on inherited newer history\n")
        self.repo.save_version(self.first, "beta", ["source.txt"])
        self.repo.run(["config", "--local", "--unset-all", key])
        self.repo.delete_latest_version_commit(self.first)
        self.assertEqual("v1.0.0", self.repo.branch_base())
        self.assertEqual(self.first, self.repo.next_version())

    def test_partial_staging_preserves_exact_index_and_unstaged_content(self):
        self.publish_betas()
        (self.path / "source.txt").write_text("staged part\n")
        self.repo.run(["add", "source.txt"])
        (self.path / "source.txt").write_text("staged part plus unstaged fix\n")
        index = self.repo._git_path("index").read_bytes()
        self.repo.delete_latest_version_commit(self.second)
        self.assertEqual(index, self.repo._git_path("index").read_bytes())
        self.assertEqual("staged part\n", self.repo.run(["show", ":source.txt"]))
        self.assertEqual("staged part plus unstaged fix\n", (self.path / "source.txt").read_text())
        self.assertEqual("added in second beta\n", self.repo.run(["show", ":added.txt"]))

    def test_initial_historical_and_additionally_tagged_commits_are_rejected(self):
        self.assert_rejected("v1.0.0", "首个")
        self.publish_betas()
        self.assert_rejected(self.first, "历史中间")
        self.repo.run(["tag", "user-note"])
        self.assert_rejected(self.second, "其他标签")

    def test_merge_commit_is_rejected(self):
        self.publish_betas()
        tree = self.repo.run(["rev-parse", "HEAD^{tree}"]).strip()
        head = self.repo.run(["rev-parse", "HEAD"]).strip()
        base = self.repo.run(["rev-parse", "main"]).strip()
        merged = self.repo.run(["commit-tree", tree, "-p", head, "-p", base, "-m", "two parents"]).strip()
        self.repo.run(["update-ref", f"refs/heads/{self.branch}", merged, head])
        self.repo.run(["tag", "-f", "-a", self.second, "-m", "merge", merged])
        self.assert_rejected(self.second, "多个父提交")

    def test_shallow_history_is_rejected(self):
        self.publish_betas()
        clone = self.root / "shallow"
        self.repo.run(["clone", "--quiet", "--depth=1", "--branch", self.branch, self.path.as_uri(), str(clone)])
        self.repo = MODULE.GitRepository(clone)
        self.path = clone
        self.assert_rejected(self.second, "浅克隆")

    def test_unmerged_index_is_rejected(self):
        self.publish_betas()
        oid = self.repo.run(["rev-parse", "HEAD:source.txt"]).strip()
        self.repo.run(["update-index", "--index-info"], input=(
            "0 " + "0" * 40 + "\tsource.txt\n" +
            f"100644 {oid} 1\tsource.txt\n100644 {oid} 2\tsource.txt\n100644 {oid} 3\tsource.txt\n"
        ))
        self.assert_rejected(self.second, "冲突")

    def test_operations_wrong_owner_detached_and_shared_formal_tags_are_rejected(self):
        self.publish_betas()
        for marker in (self.repo._git_path("MERGE_HEAD"), self.repo.metadata_path / "merge.json"):
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("{}")
            before = self.state()
            with self.assertRaises(MODULE.GitError):
                self.repo.delete_latest_version_commit(self.second)
            self.assertEqual(before, self.state())
            marker.unlink()
        with self.assertRaisesRegex(MODULE.GitError, "只读"):
            self.repo.preview_version_deletion("v1.0.0")
        self.repo.switch_branch("main")
        with self.assertRaisesRegex(MODULE.GitError, "其他分支"):
            self.repo.preview_version_deletion(self.second)
        with self.assertRaisesRegex(MODULE.GitError, "活动分支"):
            self.repo.preview_version_deletion("v1.0.0")
        self.repo.run(["checkout", "--detach", "--quiet", "HEAD"])
        with self.assertRaisesRegex(MODULE.GitError, "只读"):
            self.repo.preview_version_deletion("v1.0.0")

    def test_confirmation_rejects_head_tag_base_and_additional_label_changes(self):
        for change in ("head", "tag", "base", "label", "branch"):
            with self.subTest(change=change):
                # Each case has its own branch and beta reference.
                branch = "case-" + change
                self.repo.create_branch(branch, "v1.0.0")
                version = MODULE.version_tag_name(branch, "v1.0.0-beta.0.0.1")
                self.repo.save_version(version, "case", ["source.txt"])
                plan = self.repo.preview_version_deletion(version)
                if change == "head":
                    self.repo.run(["commit", "--allow-empty", "-m", "concurrent commit"])
                elif change in ("tag", "base"):
                    self.repo.run(["tag", "-f", "-a", version if change == "tag" else "v1.0.0",
                                   "-m", "concurrent annotation", "HEAD" if change == "tag" else "main"])
                elif change == "label":
                    self.repo.run(["tag", "extra-label"])
                else:
                    self.repo.run(["switch", "main"])
                before = self.state()
                with self.assertRaises(MODULE.GitError):
                    self.repo.delete_latest_version_commit(version, expected=plan)
                self.assertEqual(before, self.state())
                if change == "label":
                    self.repo.run(["tag", "-d", "extra-label"])
                if self.repo.current_branch() != "main":
                    self.repo.switch_branch("main")

    def test_tag_only_deletion_preserves_head_and_rejects_replaced_confirmation(self):
        self.publish_betas()
        plan = self.repo.preview_version_deletion(self.first)
        self.assertFalse(plan.can_delete_commit)
        self.repo.run(["tag", "-f", "-a", self.first, "-m", "replaced label", "HEAD^"])
        before = self.state()
        with self.assertRaisesRegex(MODULE.GitError, "重新确认"):
            self.repo.delete_version(self.first, expected=plan)
        self.assertEqual(before, self.state())
        plan = self.repo.preview_version_deletion(self.first)
        self.repo.delete_version(self.first, expected=plan)
        self.assertEqual(plan.head_oid, self.repo.run(["rev-parse", "HEAD"]).strip())
        self.assertIn(self.second, self.repo.tags())
        self.assertNotIn(self.first, self.repo.tags())

    def test_current_branch_checked_out_in_another_worktree_is_rejected(self):
        self.publish_betas()
        self.repo.run(["worktree", "add", "--force", str(self.root / "other-worktree"), self.branch])
        self.assert_rejected(self.second, "另一个工作区")

    def test_late_tag_change_cannot_partially_rewind_branch(self):
        self.publish_betas()
        plan = self.repo.preview_version_deletion(self.second)
        original = self.repo._replace_managed_refs
        def replace(before, after):
            self.repo.run(["tag", "-f", "-a", self.second, "-m", "concurrent annotation"])
            original(before, after)
        with mock.patch.object(self.repo, "_replace_managed_refs", side_effect=replace):
            with self.assertRaisesRegex(MODULE.GitError, "未完成"):
                self.repo.delete_latest_version_commit(self.second, expected=plan)
        self.assertEqual(plan.head_oid, self.repo.run(["rev-parse", "HEAD"]).strip())
        self.assertNotEqual(plan.tag_oid, self.repo.run(["rev-parse", self.second]).strip())
        self.assertTrue(self.manifest(self.second).exists())

    def test_index_install_failure_rolls_back_refs_and_preserves_original_state(self):
        self.publish_betas()
        before = self.state()
        original = MODULE.os.replace
        def replace(source, destination):
            if Path(source) == self.repo._git_path("index.lock"):
                raise OSError("injected index installation failure")
            return original(source, destination)
        with mock.patch.object(MODULE.os, "replace", side_effect=replace):
            with self.assertRaisesRegex(MODULE.GitError, "index installation failure"):
                self.repo.delete_latest_version_commit(self.second)
        self.assertEqual(before, self.state())
        self.assertTrue(self.manifest(self.second).exists())
        self.assertTrue((self.recovery()[0] / "index").exists())
        self.assertFalse(self.repo._git_path("index.lock").exists())

    def test_failed_rollback_preserves_concurrent_refs_and_reports_recovery(self):
        self.publish_betas()
        plan = self.repo.preview_version_deletion(self.second)
        original = MODULE.os.replace
        def replace(source, destination):
            if Path(source) == self.repo._git_path("index.lock"):
                self.repo.run(["update-ref", f"refs/heads/{self.branch}", plan.head_oid, plan.parent_oid])
                raise OSError("index installation failed after concurrent ref change")
            return original(source, destination)
        with mock.patch.object(MODULE.os, "replace", side_effect=replace):
            with self.assertRaisesRegex(MODULE.GitError, "引用回滚未完成") as raised:
                self.repo.delete_latest_version_commit(self.second)
        self.assertIn("引用恢复点", str(raised.exception))
        self.assertEqual(plan.head_oid, self.repo.run(["rev-parse", "HEAD"]).strip())
        self.assertNotIn(self.second, self.repo.tags())
        self.assertEqual(plan.tag_oid, self.repo.run(["rev-parse", f"{self.recovery()[2]}/tags/{self.second}"]).strip())

    def test_recovery_and_index_preparation_failures_leave_refs_and_index_intact(self):
        self.publish_betas()
        before = self.state()
        with mock.patch.object(self.repo, "_save_metadata_recovery", side_effect=OSError("backup failed")):
            with self.assertRaisesRegex(MODULE.GitError, "backup failed"):
                self.repo.delete_latest_version_commit(self.second)
        self.assertEqual(before, self.state())
        run = self.repo.run
        def fail_read_tree(args, **kwargs):
            if args[0] == "read-tree":
                raise MODULE.GitError("index preparation failed")
            return run(args, **kwargs)
        with mock.patch.object(self.repo, "run", side_effect=fail_read_tree):
            with self.assertRaisesRegex(MODULE.GitError, "index preparation failed"):
                self.repo.delete_latest_version_commit(self.second)
        self.assertEqual(before, self.state())
        self.assertFalse(self.repo._git_path("index.lock").exists())

    def test_generated_file_failure_reports_completed_deletion(self):
        self.publish_betas()
        parent = self.repo.run(["rev-parse", "HEAD^"]).strip()
        with mock.patch.object(self.repo, "refresh_workspace_generated_files", side_effect=MODULE.GitError("disk full")):
            with self.assertRaisesRegex(MODULE.GitError, "已删除.*元数据或生成文件刷新未完成") as raised:
                self.repo.delete_latest_version_commit(self.second)
        self.assertIn("无需重复删除", str(raised.exception))
        self.assertEqual(parent, self.repo.run(["rev-parse", "HEAD"]).strip())
        self.assertNotIn(self.second, self.repo.tags())
        self.assertTrue(self.recovery()[0].is_dir())

    def test_existing_index_lock_is_preserved(self):
        self.publish_betas()
        lock = self.repo._git_path("index.lock")
        lock.write_text("other Git process")
        before = self.state()
        with self.assertRaisesRegex(MODULE.GitError, "锁定"):
            self.repo.delete_latest_version_commit(self.second)
        self.assertEqual("other Git process", lock.read_text())
        self.assertEqual(before, self.state())

    def test_unsafe_generated_file_rejects_before_any_reference_change(self):
        self.publish_betas()
        target = self.path / "VERSION"
        target.unlink()
        target.symlink_to(self.path / "source.txt")
        before = self.state()
        with self.assertRaisesRegex(MODULE.GitError, "VERSION"):
            self.repo.delete_latest_version_commit(self.second)
        self.assertEqual(before, self.state())


class VersionDeletionUITest(unittest.TestCase):
    def plan(self, error="", branch="feature/camera"):
        version = "v1.0.1" if branch == "main" else MODULE.version_tag_name(branch, "v1.0.0-beta.0.0.2")
        return MODULE.VersionDeletionPlan(version, branch, "a" * 40, "b" * 40, "c" * 40, commit_error=error)

    def test_selection_starts_background_preview_before_opening_dialog(self):
        window = mock.Mock(busy=False)
        plan = self.plan()
        window.selected_history.return_value = [MODULE.HistoryEntry("b" * 40, "bbbbbbb", "today", "beta", (plan.version,))]
        window.repository.preview_version_deletion.return_value = plan
        with mock.patch.object(MODULE.Gtk, "MessageDialog") as dialog:
            MODULE.VersionManagerWindow.on_delete_version(window, None)
        dialog.assert_not_called()
        operation = window.run_background.call_args.args[1]
        self.assertEqual(plan, operation())
        window.run_background.call_args.kwargs["result_callback"](plan)
        window._background_version_deletion_ready.assert_called_once_with(window.repository, plan)

    def test_confirmation_routes_both_actions_and_cancel_and_explains_disabled_commit(self):
        for branch, error, response in (
            ("main", "", MODULE.DELETE_COMMIT_RESPONSE),
            ("feature/camera", "", MODULE.DELETE_COMMIT_RESPONSE),
            ("feature/camera", "历史中间提交", MODULE.DELETE_TAG_RESPONSE),
            ("feature/camera", "首个 Git 提交", MODULE.DELETE_COMMIT_RESPONSE),
            ("feature/camera", "", MODULE.Gtk.ResponseType.CANCEL),
        ):
            with self.subTest(branch=branch, error=error, response=response):
                window = mock.Mock()
                repository = window.repository
                plan = self.plan(error, branch)
                buttons = [mock.Mock(), mock.Mock(), mock.Mock()]
                with mock.patch.object(MODULE.Gtk, "MessageDialog") as factory:
                    dialog = factory.return_value
                    dialog.add_button.side_effect = buttons
                    dialog.run.return_value = response
                    MODULE.VersionManagerWindow._background_version_deletion_ready(window, repository, plan)
                buttons[2].set_sensitive.assert_called_once_with(not error)
                description = dialog.format_secondary_text.call_args.args[0]
                self.assertIn(branch, description)
                if error:
                    self.assertIn(error, description)
                    buttons[2].set_tooltip_text.assert_called_once_with(error)
                else:
                    self.assertIn("c" * 12, description)
                if response == MODULE.Gtk.ResponseType.CANCEL or (error and response == MODULE.DELETE_COMMIT_RESPONSE):
                    window.run_background.assert_not_called()
                    window.refresh.assert_called_once_with()
                else:
                    window.run_background.call_args.args[1]()
                    if response == MODULE.DELETE_TAG_RESPONSE:
                        repository.delete_version.assert_called_once_with(plan.version, expected=plan)
                    else:
                        repository.delete_latest_version_commit.assert_called_once_with(plan.version, expected=plan)

    def test_stale_repository_callback_does_not_change_controls_or_open_dialog(self):
        window = mock.Mock()
        with mock.patch.object(MODULE.Gtk, "MessageDialog") as dialog:
            MODULE.VersionManagerWindow._background_version_deletion_ready(window, mock.Mock(), self.plan())
        dialog.assert_not_called()
        window._end_background.assert_not_called()
        window.run_background.assert_not_called()

    def test_repository_changed_during_confirmation_does_not_start_deletion(self):
        window = mock.Mock()
        repository = window.repository
        def respond():
            window.repository = mock.Mock()
            return MODULE.DELETE_COMMIT_RESPONSE
        with mock.patch.object(MODULE.Gtk, "MessageDialog") as factory:
            factory.return_value.run.side_effect = respond
            MODULE.VersionManagerWindow._background_version_deletion_ready(window, repository, self.plan())
        window.run_background.assert_not_called()
        repository.delete_latest_version_commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
