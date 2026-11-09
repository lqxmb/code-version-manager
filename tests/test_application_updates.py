#!/usr/bin/python3
import hashlib
import importlib.util
import io
import json
import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError


SOURCE = Path(__file__).parents[1] / "src" / "code_version_manager.py"
SPEC = importlib.util.spec_from_file_location("code_version_manager_updates", SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ApplicationUpdateFixtures:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = self.root / "config" / "settings.ini"
        for patcher in (mock.patch.object(MODULE, "settings_file_path", return_value=self.settings),
                        mock.patch.object(MODULE.GLib, "get_user_cache_dir", return_value=str(self.root / "cache"))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.record = {
            "tag_name": "v0.4.10", "draft": False, "prerelease": False, "body": "更新说明\n第二行",
            "assets": [
                {"name": "code-version-manager_0.4.10_all.deb", "size": 8,
                 "browser_download_url": "https://github.com/lqxmb/code-version-manager/releases/download/v0.4.10/code-version-manager_0.4.10_all.deb"},
                {"name": "code-version-manager_0.4.10_all.deb.sha256", "size": 104,
                 "browser_download_url": "https://github.com/lqxmb/code-version-manager/releases/download/v0.4.10/code-version-manager_0.4.10_all.deb.sha256"},
            ],
        }

    def release(self):
        with mock.patch.object(MODULE, "_open_update_url", return_value=io.BytesIO(json.dumps(self.record).encode())):
            return MODULE.check_application_update()

    def make_package(self):
        stage = self.root / "stage"
        (stage / "DEBIAN").mkdir(parents=True)
        (stage / "DEBIAN" / "control").write_text(
            "Package: code-version-manager\nVersion: 0.4.10\nArchitecture: all\n"
            "Maintainer: Test <test@example.com>\nDescription: update test\n", encoding="utf-8")
        package = self.root / "test.deb"
        subprocess.run(["dpkg-deb", "--build", str(stage), str(package)], check=True, stdout=subprocess.PIPE)
        data = package.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        release = replace(self.release(), package_size=len(data))
        checksum = (digest + "  " + release.filename + "\n").encode()
        return replace(release, checksum_size=len(checksum)), data, checksum, package, digest

class ApplicationUpdateTest(ApplicationUpdateFixtures, unittest.TestCase):
    def test_settings_defaults_interval_and_preserve_other_sections(self):
        self.assertEqual((True, 0), MODULE.application_update_settings())
        MODULE.save_git_default_settings("example", "User", "user@example.com")
        MODULE.save_window_settings(1200, 800, True)
        MODULE.save_application_update_settings(last_success=100)
        self.assertFalse(MODULE.application_update_due(100 + MODULE.UPDATE_INTERVAL - 1))
        self.assertTrue(MODULE.application_update_due(100 + MODULE.UPDATE_INTERVAL))
        MODULE.save_application_update_settings(automatic=False)
        self.assertFalse(MODULE.application_update_due(100 + MODULE.UPDATE_INTERVAL * 2))
        self.assertEqual((False, 100), MODULE.application_update_settings())
        self.assertEqual((1200, 800, True), MODULE.window_settings())
        self.assertEqual(("example", "User", "user@example.com"), MODULE.git_default_settings())
        MODULE.save_window_settings(1300, 900, False)
        self.assertEqual((False, 100), MODULE.application_update_settings())

    def test_invalid_settings_and_future_timestamp_fall_back(self):
        self.settings.parent.mkdir()
        for value in ("bad", "nan", "inf", "999999999999999", "-1"):
            self.settings.write_text("[updates]\nauto_check=invalid\nlast_success=" + value)
            self.assertEqual((True, 0), MODULE.application_update_settings())

    def test_failed_settings_save_keeps_previous_file(self):
        MODULE.save_application_update_settings(automatic=False)
        before = self.settings.read_bytes()
        with mock.patch.object(MODULE.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                MODULE.save_application_update_settings(automatic=True)
        self.assertEqual(before, self.settings.read_bytes())
        self.assertEqual([self.settings], list(self.settings.parent.iterdir()))

    def test_debian_version_order_and_invalid_versions(self):
        self.assertTrue(MODULE.application_version_is_newer("0.4.10", "0.4.9"))
        self.assertFalse(MODULE.application_version_is_newer("0.4.9", "0.4.9"))
        self.assertFalse(MODULE.application_version_is_newer("0.4.8", "0.4.9"))
        self.assertFalse(MODULE.application_version_is_newer("0.5.0~rc1", "0.5.0"))
        for value in ("unknown", "../1", "1/evil", "1;bad"):
            with self.assertRaises(MODULE.GitError):
                MODULE.application_version_is_newer(value, "0.4.9")

    def test_release_information_and_exclude_draft_prerelease(self):
        release = self.release()
        self.assertEqual("0.4.10", release.version)
        self.assertEqual("更新说明\n第二行", release.notes)
        for flag in ("draft", "prerelease"):
            self.record[flag] = True
            self.assertIsNone(self.release())
            self.record[flag] = False

    def test_no_release_network_rate_limit_and_invalid_json(self):
        for code in (404, 403, 429, 500):
            with mock.patch.object(MODULE, "_open_update_url",
                                   side_effect=HTTPError(MODULE.UPDATE_API_URL, code, "error", {}, None)):
                if code == 404:
                    self.assertIsNone(MODULE.check_application_update())
                else:
                    with self.assertRaises(MODULE.GitError):
                        MODULE.check_application_update()
        for error in (URLError("offline"), TimeoutError("timeout")):
            with mock.patch.object(MODULE, "_open_update_url", side_effect=error):
                with self.assertRaises(MODULE.GitError):
                    MODULE.check_application_update()
        for data in (b"invalid", b"[]", b"{}", b"x" * (2 * 1024 * 1024 + 1)):
            with mock.patch.object(MODULE, "_open_update_url", return_value=io.BytesIO(data)):
                with self.assertRaises(MODULE.GitError):
                    MODULE.check_application_update()

    def test_reject_missing_duplicate_wrong_size_and_wrong_asset_address(self):
        original = json.dumps(self.record)
        for mutation in (lambda: self.record["assets"].pop(),
                         lambda: self.record["assets"].append(self.record["assets"][0]),
                         lambda: self.record["assets"][0].update(size=-1),
                         lambda: self.record["assets"][0].update(browser_download_url="https://evil.example/file")):
            self.record = json.loads(original)
            mutation()
            with self.assertRaises(MODULE.GitError):
                self.release()

    def test_redirects_keep_https_and_allowed_hosts(self):
        for url in ("http://github.com/lqxmb/code-version-manager/releases/download/v1/a",
                    "https://github.com/other/project/releases/download/v1/a",
                    "https://github.com.evil.example/a", "https://evil.example/a",
                    "https://user@release-assets.githubusercontent.com/a",
                    "https://release-assets.githubusercontent.com:443/a"):
            with self.assertRaises(MODULE.GitError):
                MODULE._validate_update_url(url)
        url = "https://release-assets.githubusercontent.com/github-production-release-asset/a?token=test"
        self.assertEqual(url, MODULE._validate_update_url(url))
        handler = MODULE._UpdateRedirectHandler()
        with self.assertRaises(MODULE.GitError):
            handler.redirect_request(None, None, 302, "", {}, "http://evil.example")

    def test_download_validates_real_deb_and_keeps_complete_package_only(self):
        release, data, checksum, _, digest = self.make_package()
        progress = mock.Mock()
        with mock.patch.object(MODULE, "_open_update_url", side_effect=[io.BytesIO(checksum), io.BytesIO(data)]):
            update = MODULE.download_application_update(release, threading.Event(), progress)
        self.assertEqual(data, update.path.read_bytes())
        self.assertEqual(digest, update.sha256)
        self.assertEqual([update.path], list(update.path.parent.iterdir()))
        progress.assert_called_with(len(data), len(data))
        MODULE.validate_application_package(update.path, release, digest)

    def test_download_errors_cleanup_partials_and_refuse_wrong_package(self):
        release, data, checksum, _, digest = self.make_package()
        for changed_data, changed_checksum in ((data[:-1], checksum), (data + b"x", checksum),
                                                (b"x" * len(data), checksum), (data, b"bad")):
            with mock.patch.object(MODULE, "_open_update_url",
                                   side_effect=[io.BytesIO(changed_checksum), io.BytesIO(changed_data)]):
                with self.assertRaises(MODULE.GitError):
                    MODULE.download_application_update(release, threading.Event())
            self.assertEqual([], list(MODULE.application_update_cache().iterdir()))
        package = self.root / "wrong.deb"
        package.write_bytes(data)
        with self.assertRaises(MODULE.GitError):
            MODULE.validate_application_package(package, replace(release, version="0.4.11"), digest)
        package.unlink()
        package.symlink_to(self.root / "test.deb")
        with self.assertRaises(OSError):
            MODULE.validate_application_package(package, release, digest)

    def test_download_network_failure_preserves_other_completed_downloads(self):
        release, data, checksum, _, _ = self.make_package()
        with mock.patch.object(MODULE, "_open_update_url", side_effect=[io.BytesIO(checksum), io.BytesIO(data)]):
            completed = MODULE.download_application_update(release, threading.Event())
        with mock.patch.object(MODULE, "_open_update_url", side_effect=URLError("offline")):
            with self.assertRaises(MODULE.GitError):
                MODULE.download_application_update(release, threading.Event())
        self.assertTrue(completed.path.is_file())
        self.assertEqual([completed.path.parent], list(MODULE.application_update_cache().iterdir()))

    def test_cache_rejects_symlinks(self):
        cache = self.root / "cache"
        cache.mkdir()
        (cache / MODULE.APPLICATION_PACKAGE).symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(MODULE.GitError):
            MODULE.application_update_cache()

    def test_cancel_before_and_during_download_cleans_up(self):
        release, data, checksum, _, _ = self.make_package()
        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(MODULE, "_open_update_url") as network:
            with self.assertRaises(MODULE.UpdateCancelled):
                MODULE.download_application_update(release, cancel)
            network.assert_not_called()
        cancel.clear()
        with mock.patch.object(MODULE, "_open_update_url", side_effect=[io.BytesIO(checksum), io.BytesIO(data)]):
            with self.assertRaises(MODULE.UpdateCancelled):
                MODULE.download_application_update(release, cancel, lambda *_: cancel.set())
        self.assertEqual([], list(MODULE.application_update_cache().iterdir()))

    def test_install_source_rejected_and_authorization_cancel_or_apt_failure_reported(self):
        release, _, _, package, digest = self.make_package()
        update = MODULE.DownloadedApplicationUpdate(release, package, digest)
        with self.assertRaises(MODULE.GitError):
            MODULE.install_application_update(update)
        for code, message in ((126, "管理员授权"), (127, "管理员授权"), (100, "系统安装未完成")):
            with mock.patch.object(MODULE, "installed_application", return_value=True), \
                    mock.patch.object(MODULE, "validate_application_package"), \
                    mock.patch.object(MODULE.subprocess, "run", return_value=mock.Mock(returncode=code)) as run:
                with self.assertRaisesRegex(MODULE.GitError, message):
                    MODULE.install_application_update(update)
                command = run.call_args.args[0]
                self.assertEqual(["/usr/bin/pkexec", "/usr/bin/apt-get"], command[:2])
                self.assertIn("--no-remove", command)
                self.assertEqual(str(package), command[-1])
                self.assertTrue(package.exists())
                self.assertTrue((package.parent / "install.log").exists())

    def test_install_success_requires_installed_status_and_expected_version(self):
        release, _, _, package, digest = self.make_package()
        update = MODULE.DownloadedApplicationUpdate(release, package, digest)
        for output, success in (("install ok installed\n0.4.10\n", True),
                                ("install ok installed\n0.4.9\n", False),
                                ("install ok unpacked\n0.4.10\n", False)):
            with mock.patch.object(MODULE, "installed_application", return_value=True), \
                    mock.patch.object(MODULE, "validate_application_package"), \
                    mock.patch.object(MODULE.subprocess, "run", side_effect=[
                        mock.Mock(returncode=0), mock.Mock(returncode=0, stdout=output)]):
                if success:
                    self.assertIn("已安装", MODULE.install_application_update(update))
                else:
                    with self.assertRaisesRegex(MODULE.GitError, "未确认"):
                        MODULE.install_application_update(update)

    def test_modified_package_never_requests_administrator_authorization(self):
        release, data, _, package, digest = self.make_package()
        package.write_bytes(b"x" * len(data))
        update = MODULE.DownloadedApplicationUpdate(release, package, digest)
        with mock.patch.object(MODULE, "installed_application", return_value=True), \
                mock.patch.object(MODULE.subprocess, "run") as execute:
            with self.assertRaisesRegex(MODULE.GitError, "SHA-256"):
                MODULE.install_application_update(update)
            execute.assert_not_called()


class ApplicationUpdateUiTest(ApplicationUpdateFixtures, unittest.TestCase):
    def setUp(self):
        super().setUp()
        if not MODULE.Gtk.init_check()[0]:
            self.skipTest("GTK display is unavailable")
        with mock.patch.object(MODULE.VersionManagerWindow, "load_workspace"):
            self.window = MODULE.VersionManagerWindow(None, self.root)
        self.addCleanup(self.window.destroy)

    def test_schedule_only_once_and_destroy_removes_timer(self):
        with mock.patch.object(MODULE.GLib, "timeout_add_seconds", return_value=42) as timer, \
                mock.patch.object(MODULE.GLib, "source_remove") as remove:
            self.window._schedule_application_update(self.window)
            self.window._schedule_application_update(self.window)
            timer.assert_called_once_with(5, self.window._automatic_application_update)
            self.window.destroy()
            remove.assert_called_with(42)
        self.assertFalse(self.window.update_alive)

    def test_automatic_check_due_and_manual_bypass(self):
        with mock.patch.object(MODULE, "application_update_due", return_value=False), \
                mock.patch.object(self.window, "_check_application_update") as check:
            self.window._automatic_application_update()
            check.assert_not_called()
            self.window.on_check_application_update(None)
            check.assert_called_once_with(True)

    def test_auto_failure_silent_and_manual_failure_visible_no_timestamp(self):
        with mock.patch.object(self.window, "on_application_update_details") as details:
            self.window._application_update_checked(None, "offline")
            details.assert_not_called()
        self.assertFalse(self.settings.exists())
        self.window.update_manual_check = True
        self.window._application_update_checked(None, "offline")
        self.assertEqual("offline", self.window.update_description.get_text())
        self.assertFalse(self.settings.exists())

    def test_new_release_nonmodal_and_no_release_different_from_latest(self):
        release = self.release()
        self.window._application_update_checked((release, True), "")
        self.assertTrue(self.window.update_notice.get_visible())
        self.assertTrue(self.window.update_notice.get_child().get_visible())
        self.assertIs(self.window.update_notice.get_parent(), self.window.footer)
        self.assertIsNot(self.window.update_notice.get_parent(), self.window.header)
        self.assertIsNone(self.window.update_dialog)
        self.assertGreater(MODULE.application_update_settings()[1], 0)
        self.window.on_application_update_details(None)
        self.assertFalse(self.window.update_dialog.get_modal())
        self.window._application_update_checked((None, False), "")
        self.assertEqual("暂无已发布升级包。", self.window.update_description.get_text())
        self.window._application_update_checked((release, False), "")
        self.assertIn("已是最新版本", self.window.update_description.get_text())

    def test_install_blocked_for_background_or_active_merge(self):
        self.window.busy = True
        with self.assertRaisesRegex(MODULE.GitError, "后台操作"):
            self.window._assert_application_install_idle()
        self.window.busy = False
        self.window.remote_checks_running = 1
        with self.assertRaisesRegex(MODULE.GitError, "后台操作"):
            self.window._assert_application_install_idle()
        self.window.remote_checks_running = 0
        self.window.repository = mock.Mock()
        self.window.repository.merge_state.return_value = MODULE.MergeState(True)
        with self.assertRaisesRegex(MODULE.GitError, "手动合并"):
            self.window._assert_application_install_idle()
        self.window.repository.merge_state.return_value = MODULE.MergeState(False)
        self.window._assert_application_install_idle()

    def test_destroyed_window_drops_worker_result(self):
        callbacks = []
        callback = mock.Mock()
        with mock.patch.object(MODULE.threading, "Thread") as thread, \
                mock.patch.object(MODULE.GLib, "idle_add", side_effect=lambda *args: callbacks.append(args)):
            self.window._application_update_worker(lambda: "done", callback)
            thread.call_args.kwargs["target"]()
        self.window.destroy()
        callbacks[0][0](*callbacks[0][1:])
        callback.assert_not_called()

    def test_about_exposes_manual_check(self):
        with mock.patch.object(MODULE.Gtk, "AboutDialog") as dialog, \
                mock.patch.object(self.window, "on_check_application_update") as check:
            dialog.return_value.run.return_value = MODULE.Gtk.ResponseType.APPLY
            self.window.on_about(None)
            self.assertEqual([mock.call("帮助", MODULE.Gtk.ResponseType.HELP),
                              mock.call("检查更新", MODULE.Gtk.ResponseType.APPLY)],
                             dialog.return_value.add_button.call_args_list)
            check.assert_called_once_with(None)

        with mock.patch.object(MODULE.Gtk, "AboutDialog") as dialog, \
                mock.patch.object(self.window, "on_help") as help_action:
            dialog.return_value.run.return_value = MODULE.Gtk.ResponseType.HELP
            self.window.on_about(None)
            help_action.assert_called_once_with(None)

    def test_bottom_update_notice_opens_update_details(self):
        self.window.update_release = self.release()
        self.window.update_notice.show_all()
        self.window.update_notice.emit("clicked")
        self.assertIsNotNone(self.window.update_dialog)
        self.assertIn(self.window.update_release.version, self.window.update_description.get_text())

    def test_close_download_dialog_cancels_and_install_dialog_cannot_close(self):
        self.window.update_downloading = True
        self.assertFalse(self.window._application_update_dialog_delete())
        self.assertTrue(self.window.update_cancel.is_set())
        self.window.update_installing = True
        self.assertTrue(self.window._application_update_dialog_delete())

    def test_source_download_only_and_installation_restores_busy_state(self):
        release, _, _, package, digest = self.make_package()
        self.window.update_release = release
        self.window.downloaded_update = MODULE.DownloadedApplicationUpdate(release, package, digest)
        self.window.on_application_update_details(None)
        self.assertFalse(self.window.update_dialog.get_widget_for_response(MODULE.UPDATE_INSTALL_RESPONSE).get_visible())
        self.assertIn("手动安装", self.window.update_description.get_text())
        with mock.patch.object(MODULE, "installed_application", return_value=True), \
                mock.patch.object(MODULE.Gtk, "MessageDialog") as confirmation, \
                mock.patch.object(self.window, "_application_update_worker") as worker:
            confirmation.return_value.run.return_value = MODULE.Gtk.ResponseType.OK
            self.window._install_downloaded_application_update()
            self.assertTrue(self.window.busy)
            self.assertTrue(self.window.update_installing)
            self.assertFalse(self.window.workspace_bar.get_sensitive())
            worker.assert_called_once()
        with mock.patch.object(self.window, "refresh") as refresh:
            self.window._application_update_installed(None, "APT 锁占用\n安装日志：/tmp/test.log")
            refresh.assert_called_once_with(check_remote=False, update_remote=False)
        self.assertFalse(self.window.busy)
        self.assertFalse(self.window.update_installing)
        self.assertTrue(self.window.workspace_bar.get_sensitive())
        self.assertTrue(self.window.downloaded_update.path.exists())
        self.assertIn("APT 锁占用", self.window.update_description.get_text())
        with mock.patch.object(self.window, "refresh"):
            self.window._application_update_installed("done", "")
        self.assertTrue(self.window.update_dialog.get_widget_for_response(MODULE.UPDATE_RESTART_RESPONSE).get_visible())

    def test_user_settings_switch_preserves_update_timestamp(self):
        MODULE.save_application_update_settings(last_success=100)
        original = MODULE.Gtk.Dialog
        checkboxes = []
        def run(dialog):
            def visit(widget):
                if isinstance(widget, MODULE.Gtk.CheckButton) and widget.get_label() == "自动检查软件更新":
                    checkboxes.append(widget.get_active())
                    widget.set_active(False)
                if isinstance(widget, MODULE.Gtk.Container):
                    for child in widget.get_children():
                        visit(child)
            visit(dialog)
            return MODULE.Gtk.ResponseType.OK
        with mock.patch.object(original, "run", run):
            self.window.on_settings(None)
        self.assertEqual([True], checkboxes)
        self.assertEqual((False, 100), MODULE.application_update_settings())

    def test_restart_leaves_old_application_before_exec(self):
        application = mock.Mock(restart_requested=True)
        application.run.return_value = 0
        with mock.patch.object(MODULE, "VersionManagerApplication", return_value=application), \
                mock.patch.object(MODULE.os, "execv") as execute:
            self.assertEqual(0, MODULE.main())
        application.run.assert_called_once()
        execute.assert_called_once_with("/usr/bin/code-version-manager", ["/usr/bin/code-version-manager"])


if __name__ == "__main__":
    unittest.main()
