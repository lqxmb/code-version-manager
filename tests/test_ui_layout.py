#!/usr/bin/python3
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SOURCE = Path(__file__).parents[1] / "src" / "code_version_manager.py"
SPEC = importlib.util.spec_from_file_location("code_version_manager_ui", SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class HelpDocumentPathTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "checkout" / "src" / "code_version_manager.py"
        self.source.parent.mkdir(parents=True)
        self.source.touch()
        self.project_help = self.root / "checkout" / "HELP.md"
        self.installed_help = self.root / "installed-docs" / "HELP.md"
        source_patch = mock.patch.object(MODULE, "__file__", str(self.source))
        installed_patch = mock.patch.object(MODULE, "HELP_DOCUMENT_SYSTEM_PATH", self.installed_help)
        source_patch.start()
        installed_patch.start()
        self.addCleanup(source_patch.stop)
        self.addCleanup(installed_patch.stop)

    def test_installed_help_path_takes_precedence_over_source_copy(self):
        self.project_help.write_text("source help", encoding="utf-8")
        self.installed_help.parent.mkdir()
        self.installed_help.write_text("installed help", encoding="utf-8")
        self.assertEqual(self.installed_help, MODULE.help_document_path())

    def test_source_tree_help_is_used_when_installed_copy_is_missing(self):
        self.project_help.write_text("source help", encoding="utf-8")
        self.assertEqual(self.project_help, MODULE.help_document_path())

    def test_missing_help_reports_both_locations(self):
        with self.assertRaises(MODULE.GitError) as raised:
            MODULE.help_document_path()
        self.assertIn(str(self.installed_help), str(raised.exception))
        self.assertIn(str(self.project_help), str(raised.exception))


class WindowSettingsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.config_file = self.path / "config" / "settings.ini"
        patcher = mock.patch.object(MODULE, "settings_file_path", return_value=self.config_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_window(self):
        if not MODULE.Gtk.init_check()[0]:
            self.skipTest("GTK display is unavailable")
        with mock.patch.object(MODULE.VersionManagerWindow, "load_workspace"):
            window = MODULE.VersionManagerWindow(None, self.path)
        self.addCleanup(window.destroy)
        return window

    def test_missing_or_invalid_geometry_uses_original_default(self):
        self.assertEqual((1120, 720, False), MODULE.window_settings())
        self.config_file.parent.mkdir()
        for content in ("invalid ini", "[window]\nwidth = 1200\n",
                        "[window]\nwidth = zero\nheight = 800\n",
                        "[window]\nwidth = 0\nheight = 800\n",
                        "[window]\nwidth = 1200\nheight = -1\n",
                        "[window]\nwidth = 999999999999\nheight = 800\n"):
            with self.subTest(content=content):
                self.config_file.write_text(content, encoding="utf-8")
                self.assertEqual((1120, 720, False), MODULE.window_settings())

    def test_size_and_maximized_state_round_trip_and_preserve_other_sections(self):
        MODULE.save_git_default_settings("example", "Test User", "test@example.com")
        MODULE.save_workspace_history(self.path)
        MODULE.save_window_settings(1320, 820, True)
        self.assertEqual((1320, 820, True), MODULE.window_settings())
        self.assertEqual(("example", "Test User", "test@example.com"), MODULE.git_default_settings())
        self.assertEqual([self.path], MODULE.recent_workspaces())
        MODULE.save_git_default_settings("example", "New Name", "test@example.com")
        MODULE.save_workspace_history(self.path)
        self.assertEqual((1320, 820, True), MODULE.window_settings())
        self.config_file.write_text("[window]\nwidth=1320\nheight=820\nmaximized=invalid\n")
        self.assertEqual((1320, 820, False), MODULE.window_settings())

    def test_failed_atomic_save_preserves_previous_settings_and_cleans_temporary_file(self):
        MODULE.save_window_settings(1200, 780, False)
        before = self.config_file.read_bytes()
        with mock.patch.object(MODULE.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaisesRegex(OSError, "disk error"):
                MODULE.save_window_settings(1300, 800, False)
        self.assertEqual(before, self.config_file.read_bytes())
        self.assertEqual([self.config_file], list(self.config_file.parent.iterdir()))

    def test_main_window_restores_size_and_requests_maximization(self):
        MODULE.save_window_settings(1320, 820, True)
        with mock.patch.object(MODULE.VersionManagerWindow, "maximize") as maximize:
            window = self.make_window()
        self.assertEqual((1320, 820), tuple(window.get_default_size()))
        maximize.assert_called_once_with()
        self.assertEqual((1320, 820), window.window_normal_size)

    def test_resize_is_debounced_and_last_size_is_saved_before_close(self):
        window = self.make_window()
        native = mock.Mock(get_state=mock.Mock(return_value=0))
        with mock.patch.object(window, "get_mapped", return_value=True), \
                mock.patch.object(window, "get_window", return_value=native), \
                mock.patch.object(window, "get_size", side_effect=[(1250, 780), (1320, 820)]), \
                mock.patch.object(MODULE.GLib, "timeout_add", side_effect=[41, 42]) as timer, \
                mock.patch.object(MODULE.GLib, "source_remove") as remove:
            window._on_window_configure(window, mock.Mock(width=9999, height=9999))
            window._on_window_configure(window, mock.Mock(width=9999, height=9999))
        self.assertFalse(self.config_file.exists())
        self.assertEqual(2, timer.call_count)
        remove.assert_called_once_with(41)
        with mock.patch.object(MODULE.GLib, "source_remove") as remove:
            window.destroy()
        remove.assert_called_once_with(42)
        self.assertEqual((1320, 820, False), MODULE.window_settings())
        self.assertIsNone(window.window_settings_timer)
        reopened = self.make_window()
        self.assertEqual((1320, 820), tuple(reopened.get_default_size()))

    def test_timer_saves_resize_without_waiting_for_exit(self):
        window = self.make_window()
        native = mock.Mock(get_state=mock.Mock(return_value=0))
        with mock.patch.object(window, "get_mapped", return_value=True), \
                mock.patch.object(window, "get_window", return_value=native), \
                mock.patch.object(window, "get_size", return_value=(1320, 820)), \
                mock.patch.object(MODULE.GLib, "timeout_add", return_value=41) as timer:
            window._on_window_configure(window, None)
            self.assertEqual(400, timer.call_args.args[0])
            self.assertFalse(timer.call_args.args[1]())
        self.assertEqual((1320, 820, False), MODULE.window_settings())

    def test_maximized_fullscreen_and_minimized_configures_do_not_overwrite_normal_size(self):
        window = self.make_window()
        for state in (MODULE.Gdk.WindowState.MAXIMIZED, MODULE.Gdk.WindowState.FULLSCREEN,
                      MODULE.Gdk.WindowState.ICONIFIED):
            native = mock.Mock(get_state=mock.Mock(return_value=state))
            with self.subTest(state=state), mock.patch.object(window, "get_mapped", return_value=True), \
                    mock.patch.object(window, "get_window", return_value=native), \
                    mock.patch.object(window, "get_size", return_value=(1920, 1080)), \
                    mock.patch.object(MODULE.GLib, "timeout_add") as timer:
                window._on_window_configure(window, None)
            timer.assert_not_called()
        self.assertEqual((1120, 720), window.window_normal_size)
        event = mock.Mock(changed_mask=MODULE.Gdk.WindowState.MAXIMIZED,
                          new_window_state=MODULE.Gdk.WindowState.MAXIMIZED)
        with mock.patch.object(MODULE.GLib, "timeout_add", return_value=41) as timer:
            window._on_window_state(window, event)
            timer.call_args.args[1]()
        self.assertEqual((1120, 720, True), MODULE.window_settings())
        event.new_window_state = 0
        with mock.patch.object(MODULE.GLib, "timeout_add", return_value=42) as timer:
            window._on_window_state(window, event)
            timer.call_args.args[1]()
        self.assertEqual((1120, 720, False), MODULE.window_settings())

    def test_unmapped_window_and_unchanged_size_do_not_save(self):
        window = self.make_window()
        with mock.patch.object(window, "get_mapped", return_value=False), \
                mock.patch.object(window, "_queue_window_settings") as queue:
            window._on_window_configure(window, None)
        queue.assert_not_called()
        window.destroy()
        self.assertFalse(self.config_file.exists())

    def test_settings_write_failure_does_not_break_window_and_can_retry_on_close(self):
        window = self.make_window()
        window.window_normal_size = (1320, 820)
        with mock.patch.object(MODULE, "save_window_settings", side_effect=OSError("read only")), \
                mock.patch.object(MODULE.sys, "stderr") as stderr:
            self.assertFalse(window._save_window_settings())
        self.assertTrue(stderr.write.called)
        self.assertEqual((1120, 720, False), window.saved_window_settings)
        window.destroy()
        self.assertEqual((1320, 820, False), MODULE.window_settings())


class WindowLayoutTest(unittest.TestCase):
    def setUp(self):
        if not MODULE.Gtk.init_check()[0]:
            self.skipTest("GTK display is unavailable")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        settings = mock.patch.object(MODULE, "settings_file_path", return_value=self.path / "settings.ini")
        settings.start()
        self.addCleanup(settings.stop)
        with mock.patch.object(MODULE.VersionManagerWindow, "load_workspace"):
            self.window = MODULE.VersionManagerWindow(None, self.path)
        self.addCleanup(self.window.destroy)
        self.window.repository = mock.Mock()
        self.entries = [MODULE.HistoryEntry(
            commit=str(index) * 40, short_commit=str(index) * 7,
            date="2026-11-06", subject=f"release {index}", tags=(f"v0.0.{4 - index}",),
        ) for index in range(4)]
        self.window.history_entries = self.entries
        for entry in self.entries:
            self.window.history_store.append([
                entry.tags[0], entry.date, entry.subject, entry.short_commit, entry.commit, entry.remark,
            ])
        self.selection = self.window.history_tree.get_selection()
        self.window._update_history_actions()

    @staticmethod
    def drain_events():
        while MODULE.Gtk.events_pending():
            MODULE.Gtk.main_iteration_do(False)

    def select(self, *indices):
        for index in indices:
            self.selection.select_path(MODULE.Gtk.TreePath.new_from_indices([index]))

    def selected_indices(self):
        return [path.get_indices()[0] for path in self.selection.get_selected_rows()[1]]

    def test_layout_keeps_actions_visible_and_in_confirmed_order(self):
        window = self.window
        self.assertEqual("关于", window.about_button.get_label())
        self.assertEqual("帮助", window.help_button.get_label())
        self.assertIs(window.about_button.get_parent(), window.header)
        self.assertIs(window.help_button.get_parent(), window.header)
        window.show_all()
        self.drain_events()
        self.assertLess(window.about_button.get_allocation().x,
                        window.help_button.get_allocation().x)
        self.assertEqual([window.history_scope_combo, window.compare_button, window.delete_button,
                          window.export_button, window.edit_message_button, window.changelog_button,
                          window.download_changelog_button], window.toolbar.get_children())
        self.assertIs(window.save_button, window.main_actions.get_children()[0])
        self.assertIs(window.branch_combo.get_parent(), window.branch_bar)
        self.assertIs(window.sync_local_button.get_parent(), window.main_actions)
        self.assertIs(window.sync_remote_button.get_parent(), window.main_actions)
        self.assertEqual((MODULE.Gtk.PolicyType.AUTOMATIC, MODULE.Gtk.PolicyType.NEVER),
                         window.history_action_scroll.get_policy())
        self.assertTrue(window.advanced_button.get_popup().get_children())

    def test_help_button_opens_the_resolved_markdown_with_system_handler(self):
        document = self.path / "HELP.md"
        document.write_text("help", encoding="utf-8")
        with mock.patch.object(MODULE, "help_document_path", return_value=document), \
                mock.patch.object(MODULE.Gtk, "show_uri_on_window", return_value=True) as open_uri:
            self.window.on_help(self.window.help_button)
        open_uri.assert_called_once_with(
            self.window, document.as_uri(), MODULE.Gdk.CURRENT_TIME,
        )

    def test_help_open_errors_use_the_existing_error_dialog(self):
        with mock.patch.object(MODULE, "help_document_path", side_effect=MODULE.GitError("missing help")), \
                mock.patch.object(self.window, "show_error") as show_error:
            self.window.on_help(self.window.help_button)
        show_error.assert_called_once_with("missing help")

        document = self.path / "HELP.md"
        with mock.patch.object(MODULE, "help_document_path", return_value=document), \
                mock.patch.object(MODULE.Gtk, "show_uri_on_window", return_value=False), \
                mock.patch.object(self.window, "show_error") as show_error:
            self.window.on_help(self.window.help_button)
        show_error.assert_called_once()
        self.assertIn(str(document), show_error.call_args.args[0])

    def test_single_and_pair_selection_update_actions_and_dispatch_comparison(self):
        window = self.window
        self.assertFalse(window.compare_button.get_sensitive())
        self.assertEqual("已选 0 / 2", window.selection_count_label.get_text())
        self.select(0)
        self.assertEqual("所选版本与当前", window.compare_button.get_label())
        for button in (window.compare_button, window.delete_button, window.export_button, window.edit_message_button):
            self.assertTrue(button.get_sensitive())
        with mock.patch.object(window, "run_background") as background:
            window.on_compare_selected(None)
            background.call_args.args[1]()
        window.repository.compare.assert_called_once_with(self.entries[0].reference)
        window.repository.compare.reset_mock()
        self.select(2)
        self.assertEqual("比较所选版本", window.compare_button.get_label())
        self.assertEqual("已选 2 / 2", window.selection_count_label.get_text())
        for button in (window.delete_button, window.export_button, window.edit_message_button):
            self.assertFalse(button.get_sensitive())
        with mock.patch.object(window, "run_background") as background:
            window.on_compare_selected(None)
            background.call_args.args[1]()
        window.repository.compare.assert_called_once_with(self.entries[2].reference, self.entries[0].reference)
        self.selection.unselect_all()
        self.assertFalse(window.compare_button.get_sensitive())
        with mock.patch.object(window, "run_background") as background:
            window.on_compare_selected(None)
        background.assert_not_called()

    def test_third_selection_evicts_oldest_even_when_display_order_differs(self):
        self.select(2, 0, 1)
        self.assertEqual([0, 1], self.selected_indices())
        self.assertEqual([0, 1], self.window.history_selection_order)
        self.select(3)
        self.assertEqual([1, 3], self.selected_indices())

    def test_reselecting_makes_a_row_the_newest_selection(self):
        self.select(0, 1)
        self.selection.unselect_path(MODULE.Gtk.TreePath.new_from_indices([0]))
        self.select(0, 2)
        self.assertEqual([0, 2], self.selected_indices())
        self.assertEqual([0, 2], self.window.history_selection_order)

    def test_range_selection_and_select_all_keep_last_two_added_rows(self):
        self.selection.select_range(MODULE.Gtk.TreePath.new_from_indices([0]),
                                    MODULE.Gtk.TreePath.new_from_indices([3]))
        self.assertEqual([2, 3], self.selected_indices())
        self.selection.unselect_all()
        self.selection.select_all()
        self.assertEqual([2, 3], self.selected_indices())

    def test_refresh_clears_selection_and_recomputes_single_row_actions(self):
        self.select(0, 2)
        repository = self.window.repository
        repository.status.return_value = []
        repository.history.return_value = self.entries
        repository.current_branch.return_value = "main"
        repository.merge_state.return_value = MODULE.MergeState(False)
        repository.path = self.path
        repository.storage_size.return_value = 0
        with mock.patch.object(self.window, "_refresh_branch_choices"):
            self.window.refresh(update_remote=False)
        self.assertEqual([], self.selected_indices())
        self.assertEqual([], self.window.history_selection_order)
        self.assertFalse(self.window.compare_button.get_sensitive())
        self.assertFalse(self.window.delete_button.get_sensitive())

    def test_long_results_and_paths_do_not_change_main_window_geometry(self):
        window = self.window
        window.show_all()
        window.merge_bar.hide()
        window.info_bar.hide()
        self.drain_events()
        before = tuple(window.get_size())
        minimum = window.get_preferred_size()[0].width
        details = "同步完成。恢复资料：" + "/目录" * 3000 + "\n第二行"
        window.repo_label.set_text("/workspace" * 3000)
        window._set_operation_status(details)
        window.on_operation_log(None)
        self.drain_events()
        self.assertEqual(before, tuple(window.get_size()))
        self.assertEqual(minimum, window.get_preferred_size()[0].width)
        self.assertEqual("同步完成", window.status_label.get_text())
        self.assertEqual(details, window.status_label.get_tooltip_text())
        self.assertEqual(details, window.operation_logs[self.path][-1].details)
        self.assertLessEqual(window.log_window.get_size().width, 1000)

    def test_session_logs_keep_200_entries_per_workspace_and_copy_in_reverse_order(self):
        window = self.window
        window.on_operation_log(None)
        for index in range(205):
            window._set_operation_status(f"操作 {index}。完整结果 {index}")
        self.assertEqual(200, len(window.operation_logs[self.path]))
        self.assertEqual(200, len(window.log_list.get_children()))
        self.assertEqual("操作 5", window.operation_logs[self.path][0].summary)
        text = window._operation_log_text()
        self.assertLess(text.index("完整结果 204"), text.index("完整结果 203"))
        with mock.patch.object(MODULE.Gtk.Clipboard, "get") as clipboard:
            window.on_copy_operation_log(None)
        clipboard.return_value.set_text.assert_called_once_with(text, -1)
        other = self.path / "other"
        window.workspace_path = other
        self.assertEqual("", window._operation_log_text())
        window._set_operation_status("另一个工作空间")
        self.assertEqual(1, len(window.operation_logs[other]))
        self.assertEqual(200, len(window.operation_logs[self.path]))

    def test_log_window_reuses_reopens_and_updates_for_workspace_changes(self):
        window = self.window
        window._set_operation_status("已发布。完整说明")
        window.on_operation_log(None)
        first = window.log_window
        self.assertFalse(first.get_modal())
        window.on_operation_log(None)
        self.assertIs(first, window.log_window)
        first.destroy()
        self.assertIsNone(window.log_window)
        window.on_operation_log(None)
        self.assertIsNot(first, window.log_window)
        other = self.path / "other"
        other.mkdir()
        with mock.patch.object(MODULE, "GitRepository", side_effect=MODULE.GitError("not enabled")), \
                mock.patch.object(window, "_save_repository_path"):
            window.load_workspace(other)
        self.assertEqual([], window.history_selection_order)
        self.assertIn(str(other), window.log_workspace_label.get_text())
        self.assertFalse(window.copy_log_button.get_sensitive())
        window._set_operation_status("新工作空间操作")
        self.assertTrue(window.copy_log_button.get_sensitive())
        self.assertEqual(1, len(window.log_list.get_children()))

    def test_background_disables_moved_actions_keeps_logs_available_and_timer_unrecorded(self):
        window = self.window
        self.select(0)
        with mock.patch.object(window, "_set_remote_status"), \
                mock.patch.object(MODULE.time, "monotonic", side_effect=[100, 105]), \
                mock.patch.object(MODULE.GLib, "timeout_add_seconds", return_value=42) as timer, \
                mock.patch.object(MODULE.threading, "Thread"):
            window.run_background("正在同步…", mock.Mock(), "同步完成")
            timer.call_args.args[1]()
        for widget in (window.workspace_bar, window.main_actions, window.toolbar, window.branch_bar):
            self.assertFalse(widget.get_sensitive())
        self.assertTrue(window.log_button.get_sensitive())
        self.assertEqual("正在同步… · 已等待 5 秒", window.status_label.get_text())
        self.assertEqual({}, window.operation_logs)
        with mock.patch.object(MODULE.GLib, "source_remove"), \
                mock.patch.object(window, "refresh"):
            window._background_complete("同步完成。恢复位置：/backup")
        for widget in (window.workspace_bar, window.main_actions, window.toolbar, window.branch_bar):
            self.assertTrue(widget.get_sensitive())
        self.assertEqual(1, len(window.operation_logs[self.path]))
        self.assertTrue(window.compare_button.get_sensitive())

    def test_errors_preserve_details_and_existing_dialog(self):
        details = "同步未完成\n恢复位置：/backup\n详细原因"
        with mock.patch.object(MODULE.Gtk, "MessageDialog") as dialog:
            self.window.show_error(details)
        dialog.return_value.format_secondary_text.assert_called_once_with(details)
        entry = self.window.operation_logs[self.path][-1]
        self.assertEqual(("操作未完成", details, "error"), (entry.summary, entry.details, entry.level))

    def test_unversioned_workspace_disables_repository_actions(self):
        self.window.repository = None
        self.window.refresh()
        for button in (self.window.save_button, self.window.compare_button,
                       self.window.delete_button, self.window.sync_local_button,
                       self.window.sync_remote_button, self.window.clear_recovery_button):
            self.assertFalse(button.is_sensitive())
        self.assertTrue(self.window.log_button.is_sensitive())

    def test_detached_and_merge_states_preserve_branch_and_release_guards(self):
        window = self.window
        repository = window.repository
        repository.path = self.path
        repository.status.return_value = []
        repository.history.return_value = self.entries
        repository.storage_size.return_value = 0
        repository._sync_target.return_value = "local-origin"
        repository.run.return_value = "head-oid"
        repository.local_branches.return_value = [
            MODULE.BranchInfo("main", "local", "main-oid"),
            MODULE.BranchInfo("feature", "local", "feature-oid"),
        ]
        repository.current_branch.return_value = None
        repository.head_label.return_value = "v0.0.4"
        repository.last_working_branch.return_value = "main"
        repository.merge_state.return_value = MODULE.MergeState(False)
        window.refresh(update_remote=False)
        self.assertFalse(window.save_button.is_sensitive())
        self.assertFalse(window.new_branch_button.is_sensitive())
        self.assertEqual("detached", window.info_mode)
        repository.current_branch.return_value = "feature"
        window.refresh(update_remote=False)
        self.assertEqual("发布预发布版本", window.save_button.get_label())
        self.assertFalse(window.new_branch_button.is_sensitive())
        repository.merge_state.return_value = MODULE.MergeState(True, source="feature")
        window.refresh(update_remote=False)
        self.assertFalse(window.save_button.is_sensitive())
        self.assertFalse(window.branch_bar.is_sensitive())
        self.assertTrue(window.merge_bar.get_visible())

    def test_sync_states_keep_colors_and_permissions_in_main_action_row(self):
        window = self.window
        for state, css_class, enabled in (("pending", "sync-pending", True),
                                          ("synced", "sync-synced", False),
                                          ("checking", "sync-unavailable", False),
                                          ("unavailable", "sync-unavailable", False)):
            with self.subTest(state=state):
                status = MODULE.RemoteSyncStatus(state, "完整远程说明", can_sync_local=True,
                                                can_sync_remote=True)
                with mock.patch.object(window, "_refresh_branch_choices"):
                    window._set_remote_status(status)
                for button in (window.sync_local_button, window.sync_remote_button):
                    self.assertEqual(enabled, button.is_sensitive())
                    self.assertTrue(button.get_style_context().has_class(css_class))
                self.assertEqual("完整远程说明", window.remote_summary_label.get_tooltip_text())
                self.assertFalse(window.remote_summary_label.get_line_wrap())

    def test_advanced_sync_buttons_use_whole_repository_status_and_shared_colors(self):
        window = self.window
        snapshot = MODULE.RemoteSyncSnapshot("origin-url", "refs/heads/main", (), scope="all")
        cases = (
            ("synced", False, False, "sync-synced", (False, False), (50, 128, 79)),
            ("pending", True, True, "sync-pending", (True, True), (187, 48, 48)),
            ("pending", False, True, "sync-pending", (False, True), (187, 48, 48)),
            ("pending", True, False, "sync-pending", (True, False), (187, 48, 48)),
            ("pending", False, False, "sync-pending", (False, False), (187, 48, 48)),
            ("checking", True, True, "sync-unavailable", (False, False), (241, 244, 245)),
            ("unavailable", True, True, "sync-unavailable", (False, False), (241, 244, 245)),
        )
        for state, can_local, can_remote, css_class, enabled, rgb in cases:
            with self.subTest(state=state, can_local=can_local, can_remote=can_remote):
                window.remote_status = MODULE.RemoteSyncStatus(
                    "pending" if state == "synced" else "synced", "当前分支状态与整仓状态不同",
                )
                status = MODULE.RemoteSyncStatus(
                    state, "全部分支、版本标签和备注的检查结果", can_local, can_remote,
                    local_message="下载检查结果或禁用原因", remote_message="上传检查结果或禁用原因",
                    snapshot=snapshot,
                )

                def inspect_dialog(dialog):
                    for response, sensitive, message in (
                        (1, enabled[0], status.local_message),
                        (2, enabled[1], status.remote_message),
                    ):
                        button = dialog.get_widget_for_response(response)
                        context = button.get_style_context()
                        self.assertEqual(sensitive, button.is_sensitive())
                        self.assertTrue(context.has_class(css_class))
                        if state == "synced":
                            self.assertEqual("无需覆盖", button.get_tooltip_text())
                        else:
                            self.assertEqual(message or status.message, button.get_tooltip_text())
                        color = context.get_background_color(button.get_state_flags())
                        for actual, expected in zip((color.red, color.green, color.blue), rgb):
                            self.assertAlmostEqual(expected / 255, actual, places=4)
                    return MODULE.Gtk.ResponseType.CANCEL

                with mock.patch.object(window, "_end_background"), \
                        mock.patch.object(window, "refresh"), \
                        mock.patch.object(window, "run_background") as run_background, \
                        mock.patch.object(MODULE.Gtk, "Label", wraps=MODULE.Gtk.Label) as label_factory, \
                        mock.patch.object(MODULE.Gtk.Dialog, "run", autospec=True, side_effect=inspect_dialog):
                    window._background_advanced_sync_ready(window.repository, status)
                run_background.assert_not_called()
                label_text = label_factory.call_args.kwargs["label"]
                self.assertNotIn("下载：", label_text)
                self.assertNotIn("上传：", label_text)
                self.assertEqual(1, label_text.count("全部分支、版本标签和备注"))


if __name__ == "__main__":
    unittest.main()
