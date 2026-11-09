#!/usr/bin/python3
import copy
import hashlib
import importlib.util
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

SOURCE = Path(__file__).parents[1] / 'src' / 'code_version_manager.py'
SPEC = importlib.util.spec_from_file_location('code_version_manager_release', SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakePublisher(MODULE.SoftwareReleasePublisher):
    def __init__(self, package):
        super().__init__()
        self.executable = '/fake/gh'
        self.package = package
        self.records = []
        self.latest_id = None
        self.tag_oid = 'a' * 40
        self.commit_oid = 'b' * 40
        self.missing_tag = False
        self.permission = True
        self.calls = []
        self.fail_upload = None
        self.public_timeout = False
        self.public_no_effect = False
        self.cancel_after_upload = False
        self.public_count = 0

    def draft(self, assets=()):
        record = {'id': 12, 'tag_name': self.package.tag, 'draft': True, 'prerelease': False,
                  'name': 'Title', 'body': 'Notes', 'updated_at': '1', 'assets': list(assets)}
        self.records.append(record)
        return record

    def asset(self, path, number=20):
        return {'id': number, 'name': path.name, 'size': path.stat().st_size, 'state': 'uploaded',
                'digest': 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest(), 'updated_at': '1'}

    def _api(self, method, path, payload=None, missing=False):
        self.calls.append((method, path, copy.deepcopy(payload)))
        prefix = MODULE.RELEASE_API_PATH
        if path == 'user':
            return {'login': 'maintainer'}
        if path == prefix:
            return {'permissions': {'push': self.permission}}
        if '/git/ref/tags/' in path:
            return None if self.missing_tag else {'object': {'sha': self.tag_oid, 'type': 'tag'}}
        if '/git/tags/' in path:
            return {'object': {'sha': self.commit_oid, 'type': 'commit'}}
        if path == prefix + '/releases/latest':
            return copy.deepcopy(next((item for item in self.records if item['id'] == self.latest_id), None))
        if path.startswith(prefix + '/releases?'):
            return copy.deepcopy(self.records)
        if method == 'PATCH' and path.startswith(prefix + '/releases/'):
            record = next(item for item in self.records if item['id'] == int(path.rsplit('/', 1)[1]))
            if 'draft' in payload:
                self.public_count += 1
                if not self.public_no_effect:
                    record.update(draft=False, prerelease=False)
                    self.latest_id = record['id']
                if self.public_timeout:
                    raise MODULE.GitError('GitHub 操作超时。')
            else:
                record.update(name=payload['name'], body=payload['body'])
            record['updated_at'] = str(int(record['updated_at']) + 1)
            return copy.deepcopy(record)
        raise AssertionError((method, path, payload))

    def _run(self, arguments, data=None, output=None, timeout=120, raw_command=False):
        self.calls.append(tuple(arguments))
        if self.cancel.is_set():
            raise MODULE.UpdateCancelled('发布已取消。')
        if arguments[-1] == '--help':
            return 0, b'--hostname --method --input --web --git-protocol --skip-ssh-key --verify-tag --draft --notes-file', b''
        if arguments[:2] == ['auth', 'status']:
            return 0, b'', b''
        if arguments[:2] == ['release', 'create']:
            record = self.draft()
            record['name'] = arguments[arguments.index('--title') + 1]
            record['body'] = Path(arguments[arguments.index('--notes-file') + 1]).read_text()
            return 0, b'', b''
        if arguments[:2] == ['release', 'upload']:
            path = Path(arguments[3])
            if self.fail_upload == path.name:
                return 1, b'', b'upload failed'
            self.records[0]['assets'].append(self.asset(path, 20 + len(self.records[0]['assets'])))
            self.records[0]['updated_at'] = str(int(self.records[0]['updated_at']) + 1)
            if self.cancel_after_upload:
                self.cancel.set()
            return 0, b'', b''
        if arguments[0] == 'api' and output is not None:
            asset_id = int(arguments[arguments.index('--method') + 2].rsplit('/', 1)[1])
            asset = next(asset for item in self.records for asset in item['assets'] if asset['id'] == asset_id)
            path = next(path for path in self.package.assets if path.name == asset['name'])
            output.write(path.read_bytes())
            return 0, b'', b''
        raise AssertionError(arguments)


class ReleaseFixtures:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = self.root / 'settings.ini'
        for patcher in (mock.patch.object(MODULE.GLib, 'get_user_cache_dir', return_value=str(self.root / 'cache')),
                        mock.patch.object(MODULE, 'settings_file_path', return_value=self.settings)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.source = self.make_deb()
        self.package = MODULE.prepare_software_release(self.source, threading.Event())
        self.publisher = FakePublisher(self.package)
        patcher = mock.patch.object(MODULE, 'check_application_update', side_effect=self.client_release)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_deb(self, name='code-version-manager', version='0.4.10', architecture='all'):
        stage = self.root / ('stage-' + name + '-' + architecture)
        (stage / 'DEBIAN').mkdir(parents=True)
        (stage / 'DEBIAN' / 'control').write_text(
            f'Package: {name}\nVersion: {version}\nArchitecture: {architecture}\n'
            'Maintainer: Test <test@example.com>\nDescription: software release test\n')
        source = self.root / (name + '-' + architecture + '.deb')
        subprocess.run(['dpkg-deb', '--build', str(stage), str(source)], check=True, stdout=subprocess.PIPE)
        return source

    def client_release(self):
        return MODULE.ApplicationRelease(self.package.version, self.package.tag, 'Notes', 'url', 'deb',
                                         self.package.size, 'checksum', self.package.checksum_path.stat().st_size)

    def preview(self):
        return self.publisher.preview(self.package, 'Title', 'Notes')


class SoftwareReleaseBackendTest(ReleaseFixtures, unittest.TestCase):
    def test_prepare_private_copy_and_checksum(self):
        self.assertEqual('v0.4.10', self.package.tag)
        self.assertNotEqual(self.source, self.package.path)
        self.assertEqual(self.source.read_bytes(), self.package.path.read_bytes())
        self.assertEqual(0o700, self.package.path.parent.stat().st_mode & 0o777)
        self.assertEqual(0o400, self.package.path.stat().st_mode & 0o777)
        self.assertEqual(f'{self.package.sha256}  {self.package.path.name}\n', self.package.checksum_path.read_text())
        before = self.package.path.read_bytes()
        self.source.write_bytes(b'changed')
        self.assertEqual(before, self.package.path.read_bytes())

    def test_reject_wrong_package_architecture_and_bad_checksum(self):
        for source in (self.make_deb(name='another-package'), self.make_deb(architecture='amd64')):
            with self.assertRaisesRegex(MODULE.GitError, '只可发布'):
                MODULE.prepare_software_release(source, threading.Event())
        adjacent = self.source.with_name(self.source.name + '.sha256')
        adjacent.write_text('0' * 64 + '  ' + self.source.name + '\n')
        with self.assertRaisesRegex(MODULE.GitError, '不一致'):
            MODULE.prepare_software_release(self.source, threading.Event())
        adjacent.write_text(self.package.sha256 + '  ' + self.source.name + '\n')
        result = MODULE.prepare_software_release(self.source, threading.Event())
        self.assertEqual(self.package.sha256, result.sha256)

    def test_reject_symlinks_special_files_and_cancelled_preparation(self):
        linked = self.root / 'link.deb'
        linked.symlink_to(self.source)
        with self.assertRaises(OSError):
            MODULE.prepare_software_release(linked, threading.Event())
        import os
        fifo = self.root / 'fifo.deb'
        os.mkfifo(str(fifo))
        with self.assertRaises(MODULE.GitError):
            MODULE.prepare_software_release(fifo, threading.Event())
        event = threading.Event()
        event.set()
        before = list(MODULE.application_update_cache().iterdir())
        with self.assertRaises(MODULE.UpdateCancelled):
            MODULE.prepare_software_release(self.source, event)
        self.assertEqual(before, list(MODULE.application_update_cache().iterdir()))

    def test_preview_fixes_tag_commit_and_requires_remote_tag_permissions(self):
        plan = self.preview()
        self.assertEqual('a' * 40, plan.tag_oid)
        self.assertEqual('b' * 40, plan.commit_oid)
        self.assertIsNone(plan.draft)
        self.publisher.missing_tag = True
        with self.assertRaisesRegex(MODULE.GitError, '先将'):
            self.preview()
        self.publisher.missing_tag = False
        self.publisher.permission = False
        with self.assertRaisesRegex(MODULE.GitError, '发布权限'):
            self.preview()

    def test_public_or_duplicate_releases_rejected(self):
        draft = self.publisher.draft()
        draft['draft'] = False
        with self.assertRaisesRegex(MODULE.GitError, '禁止覆盖'):
            self.preview()
        draft['draft'] = True
        self.publisher.draft()
        with self.assertRaisesRegex(MODULE.GitError, '多份'):
            self.preview()

    def test_version_must_be_higher_than_latest(self):
        latest = self.publisher.draft()
        latest.update(draft=False, tag_name='v0.4.11')
        self.publisher.latest_id = latest['id']
        with self.assertRaisesRegex(MODULE.GitError, '必须高于'):
            self.preview()

    def test_new_draft_upload_verify_publish_client_interface(self):
        result = self.publisher.publish(self.preview())
        self.assertTrue(result.confirmed)
        self.assertEqual(1, self.publisher.public_count)
        self.assertFalse(self.publisher.records[0]['draft'])
        self.assertEqual(2, len(self.publisher.records[0]['assets']))
        commands = [call for call in self.publisher.calls if call[:2] == ('release', 'create') and '--help' not in call]
        self.assertEqual(1, len(commands))
        self.assertIn('--verify-tag', commands[0])
        self.assertIn('--draft', commands[0])
        for call in self.publisher.calls:
            self.assertNotIn('--clobber', call)

    def test_resume_draft_reuses_matching_assets_and_updates_notes(self):
        draft = self.publisher.draft([self.publisher.asset(self.package.path)])
        draft.update(name='Old title', body='Old notes')
        result = self.publisher.publish(self.preview())
        self.assertTrue(result.confirmed)
        uploads = [call for call in self.publisher.calls if call[:2] == ('release', 'upload')]
        self.assertEqual(1, len(uploads))
        self.assertEqual(str(self.package.checksum_path), uploads[0][3])
        self.assertEqual('Notes', draft['body'])

    def test_hash_fallback_downloads_existing_asset(self):
        asset = self.publisher.asset(self.package.path)
        asset['digest'] = None
        self.publisher.draft([asset])
        self.assertIsNotNone(self.preview().draft)
        self.assertTrue(any(call[0] == 'api' for call in self.publisher.calls))

    def test_extra_conflicting_duplicate_unfinished_assets_stop(self):
        for changes in ({'name': 'extra.zip'}, {'digest': 'sha256:' + '0' * 64},
                        {'state': 'starter'}, {'size': 1}):
            self.publisher.records = []
            asset = self.publisher.asset(self.package.path)
            asset.update(changes)
            self.publisher.draft([asset])
            with self.assertRaises(MODULE.GitError):
                self.preview()
        self.publisher.records = []
        asset = self.publisher.asset(self.package.path)
        self.publisher.draft([asset, copy.deepcopy(asset)])
        with self.assertRaisesRegex(MODULE.GitError, '重复'):
            self.preview()

    def test_confirmation_snapshot_changes_reject_before_writes(self):
        for change in ('tag', 'draft', 'latest'):
            publisher = FakePublisher(self.package)
            plan = publisher.preview(self.package, 'Title', 'Notes')
            if change == 'tag':
                publisher.tag_oid = 'c' * 40
            elif change == 'draft':
                publisher.draft()
            else:
                record = publisher.draft()
                record.update(tag_name='v0.4.9', draft=False)
                publisher.latest_id = record['id']
            with self.assertRaisesRegex(MODULE.GitError, '确认后'):
                publisher.publish(plan)
            self.assertFalse(any(call[:2] == ('release', 'create') and '--help' not in call for call in publisher.calls))

    def test_upload_failure_and_cancellation_leave_draft_unpublished(self):
        for cancel in (False, True):
            publisher = FakePublisher(self.package)
            if cancel:
                publisher.cancel_after_upload = True
            else:
                publisher.fail_upload = self.package.checksum_path.name
            with self.assertRaisesRegex(MODULE.GitError, '草稿'):
                publisher.publish(publisher.preview(self.package, 'Title', 'Notes'))
            self.assertTrue(publisher.records[0]['draft'])
            self.assertEqual(1, len(publisher.records[0]['assets']))
            self.assertEqual(0, publisher.public_count)

    def test_publish_timeout_reconciles_without_second_mutation(self):
        self.publisher.public_timeout = True
        result = self.publisher.publish(self.preview())
        self.assertTrue(result.confirmed)
        self.assertEqual(1, self.publisher.public_count)

    def test_local_attachment_modified_after_confirmation_blocks_mutation(self):
        plan = self.preview()
        self.package.path.chmod(0o600)
        self.package.path.write_bytes(b'x' * self.package.size)
        with self.assertRaisesRegex(MODULE.GitError, '附件发生变化'):
            self.publisher.publish(plan)
        self.assertEqual([], self.publisher.records)

    def test_tag_changes_during_upload_preserve_complete_unpublished_draft(self):
        plan = self.preview()
        original = self.publisher._run
        def run(arguments, **kwargs):
            result = original(arguments, **kwargs)
            if arguments[:2] == ['release', 'upload']:
                self.publisher.tag_oid = 'c' * 40
            return result
        with mock.patch.object(self.publisher, '_run', side_effect=run):
            with self.assertRaisesRegex(MODULE.GitError, '上传期间远程标签'):
                self.publisher.publish(plan)
        self.assertTrue(self.publisher.records[0]['draft'])
        self.assertEqual(2, len(self.publisher.records[0]['assets']))
        self.assertEqual(0, self.publisher.public_count)

    def test_current_workspace_and_user_config_are_unchanged(self):
        workspace = self.root / 'workspace'
        workspace.mkdir()
        subprocess.run(['git', 'init', str(workspace)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(['git', '-C', str(workspace), 'config', 'user.name', 'Local User'], check=True)
        subprocess.run(['git', '-C', str(workspace), 'config', 'user.email', 'local@example.com'], check=True)
        subprocess.run(['git', '-C', str(workspace), 'remote', 'add', 'origin', 'ssh://unrelated.example/workspace'], check=True)
        (workspace / 'file.txt').write_text('staged content')
        subprocess.run(['git', '-C', str(workspace), 'add', 'file.txt'], check=True)
        (workspace / 'file.txt').write_text('unstaged content')
        self.settings.write_text('[repository]\npath=/unrelated\n')
        def fingerprint():
            return {str(path.relative_to(workspace)): path.read_bytes()
                    for path in workspace.rglob('*') if path.is_file()}
        before = fingerprint()
        config = self.settings.read_bytes()
        self.assertTrue(self.publisher.publish(self.preview()).confirmed)
        self.assertEqual(before, fingerprint())
        self.assertEqual(config, self.settings.read_bytes())

    def test_publish_uncertain_or_client_invisible_reports_unknown(self):
        self.publisher.public_no_effect = True
        result = self.publisher.publish(self.preview())
        self.assertFalse(result.confirmed)
        self.assertIn('待确认', result.message)
        self.assertEqual(1, self.publisher.public_count)
        self.publisher = FakePublisher(self.package)
        with mock.patch.object(MODULE, 'check_application_update', return_value=None):
            result = self.publisher.publish(self.preview())
        self.assertFalse(result.confirmed)
        self.assertEqual(1, self.publisher.public_count)

    def test_missing_old_gh_auth_errors_and_no_secret_output(self):
        publisher = MODULE.SoftwareReleasePublisher()
        publisher.executable = None
        with self.assertRaisesRegex(MODULE.GitError, '未安装'):
            publisher.authentication()
        publisher.executable = '/fake/gh'
        with mock.patch.object(publisher, '_run', return_value=(0, b'old CLI', b'')):
            with self.assertRaisesRegex(MODULE.GitError, '版本不支持'):
                publisher.check_tools()
        with mock.patch.object(publisher, 'check_tools'), mock.patch.object(publisher, '_run', return_value=(1, b'token-secret', b'token-secret')):
            with self.assertRaisesRegex(MODULE.GitError, '尚未登录') as error:
                publisher.authentication()
            self.assertNotIn('token-secret', str(error.exception))
        for code in (401, 403, 500):
            with mock.patch.object(publisher, '_run', return_value=(1, b'', f'secret HTTP {code}'.encode())):
                with self.assertRaises(MODULE.GitError) as error:
                    publisher._api('GET', 'user')
                self.assertNotIn('secret', str(error.exception))

    def test_login_browser_ssh_flow_in_terminal(self):
        publisher = MODULE.SoftwareReleasePublisher()
        publisher.executable = '/fake/gh'
        with mock.patch.object(publisher, 'check_tools'), mock.patch.object(MODULE.shutil, 'which', side_effect=lambda name: '/usr/bin/gnome-terminal' if name == 'gnome-terminal' else None), mock.patch.object(publisher, '_run') as run, mock.patch.object(publisher, 'authentication', return_value='maintainer'):
            self.assertEqual('maintainer', publisher.login())
        command = run.call_args.args[0]
        self.assertEqual(['/usr/bin/gnome-terminal', '--wait', '--', '/fake/gh', 'auth', 'login'], command[:6])
        self.assertIn('--skip-ssh-key', command)
        self.assertIn('--web', command)
        self.assertIn('ssh', command)
        self.assertTrue(run.call_args.kwargs['raw_command'])


class SoftwareReleaseUiTest(ReleaseFixtures, unittest.TestCase):
    def setUp(self):
        super().setUp()
        if not MODULE.Gtk.init_check()[0]:
            self.skipTest('GTK display unavailable')
        with mock.patch.object(MODULE.VersionManagerWindow, 'load_workspace'):
            self.window = MODULE.VersionManagerWindow(None, self.root)
        self.addCleanup(self.window.destroy)
        self.window.on_publish_software(None)
        self.dialog = self.window.software_release_dialog

    def test_menu_opens_single_dialog_package_autofills_version(self):
        self.assertEqual('发布软件到 GitHub', self.window.publish_software_item.get_label())
        self.window.on_publish_software(None)
        self.assertIs(self.dialog, self.window.software_release_dialog)
        self.dialog._package_ready(self.package, '')
        self.assertEqual(self.package.tag, self.dialog.version_entry.get_text())
        self.assertFalse(self.dialog.version_entry.get_editable())
        self.assertIn(self.package.version, self.dialog.title_entry.get_text())
        self.assertTrue(self.dialog.get_widget_for_response(MODULE.Gtk.ResponseType.OK).get_sensitive())

    def test_login_worker_results_main_thread_callbacks(self):
        callbacks = []
        with mock.patch.object(MODULE.threading, 'Thread') as thread, mock.patch.object(MODULE.GLib, 'idle_add', side_effect=lambda *args: callbacks.append(args)), mock.patch.object(MODULE.SoftwareReleasePublisher, 'authentication', return_value='maintainer'):
            self.dialog._login(None, False)
            self.assertTrue(self.dialog.busy)
            self.assertFalse(self.dialog.inputs.get_sensitive())
            thread.call_args.kwargs['target']()
            self.assertNotIn('maintainer', self.dialog.auth_label.get_text())
            callbacks[0][0](*callbacks[0][1:])
        self.assertFalse(self.dialog.busy)
        self.assertIn('maintainer', self.dialog.auth_label.get_text())

    def test_cancel_keeps_window_and_blocks_install(self):
        self.dialog.busy = True
        self.dialog._response(self.dialog, MODULE.Gtk.ResponseType.CANCEL)
        self.assertTrue(self.dialog.cancel.is_set())
        self.assertTrue(self.dialog.alive)
        with self.assertRaisesRegex(MODULE.GitError, '发布操作'):
            self.window._assert_application_install_idle()
        self.dialog._finish('已取消，草稿保留')
        self.assertFalse(self.dialog.busy)

    def test_confirmation_snapshot_decline_does_not_publish(self):
        plan = self.preview()
        self.dialog.package = self.package
        self.dialog.busy = True
        with mock.patch.object(MODULE.Gtk, 'MessageDialog') as confirmation, mock.patch.object(self.dialog, '_worker') as worker:
            confirmation.return_value.run.return_value = MODULE.Gtk.ResponseType.CANCEL
            self.dialog._preview_ready(plan, '')
        text = confirmation.return_value.format_secondary_text.call_args.args[0]
        for expected in (plan.tag_oid, plan.commit_oid, plan.title, plan.notes, MODULE.UPDATE_REPOSITORY):
            self.assertIn(expected, text)
        worker.assert_not_called()
        self.assertFalse(self.dialog.busy)

    def test_completion_and_error_recovery_and_web_link(self):
        self.dialog._published(MODULE.SoftwareReleaseResult('https://github.com/lqxmb/code-version-manager/releases/tag/v0.4.10', False, '公开结果待确认'), '')
        self.assertIn('核对', self.dialog.link.get_label())
        self.dialog._published(None, '上传失败，草稿保留')
        buffer = self.dialog.details.get_buffer()
        self.assertIn('草稿保留', buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True))

    def test_destroy_discards_pending_results_cleans_package(self):
        callbacks = []
        with mock.patch.object(MODULE.threading, 'Thread') as thread, mock.patch.object(MODULE.GLib, 'idle_add', side_effect=lambda *args: callbacks.append(args)):
            callback = mock.Mock()
            self.dialog._worker(lambda cancel: self.package, callback)
            thread.call_args.kwargs['target']()
        self.dialog.destroy()
        callbacks[0][0](*callbacks[0][1:])
        callback.assert_not_called()
        self.assertFalse(self.package.path.exists())


class SoftwareReleaseProcessTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.executable = self.root / 'gh'
        self.executable.write_text('#!/usr/bin/python3\nimport json, os, sys, time\n'
            'if sys.argv[1] == "sleep":\n    time.sleep(10)\n'
            'else:\n    print(json.dumps({"args": sys.argv[1:], "input": sys.stdin.read(), '
            '"cwd": os.getcwd(), "host": os.environ.get("GH_HOST"), '
            '"prompt": os.environ.get("GH_PROMPT_DISABLED"), "debug": os.environ.get("GH_DEBUG")}))\n')
        self.executable.chmod(0o700)
        self.publisher = MODULE.SoftwareReleasePublisher()
        self.publisher.executable = str(self.executable)

    def test_process_receives_exact_arguments_json_stdin_and_isolated_cwd(self):
        import json
        payload = {'name': 'Title $(no-shell)', 'body': 'Line one\nLine two'}
        with mock.patch.dict(MODULE.os.environ, {'GH_DEBUG': 'api', 'GH_HOST': 'other.example'}):
            record = self.publisher._api('PATCH', MODULE.RELEASE_API_PATH + '/releases/1', payload)
        self.assertEqual(payload, json.loads(record['input']))
        self.assertEqual('github.com', record['host'])
        self.assertEqual(tempfile.gettempdir(), record['cwd'])
        self.assertEqual('1', record['prompt'])
        self.assertIsNone(record['debug'])
        self.assertIn('--input', record['args'])
        with self.assertRaisesRegex(MODULE.GitError, '其他发布目标'):
            self.publisher._api('POST', 'repos/other/project/releases', payload)

    def test_cancel_and_timeout_terminate_real_local_process(self):
        import time
        timer = threading.Timer(0.2, self.publisher.cancel.set)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        with self.assertRaises(MODULE.UpdateCancelled):
            self.publisher._run(['sleep'])
        self.assertLess(time.monotonic() - started, 3)
        timer.join()
        self.publisher.cancel.clear()
        with self.assertRaisesRegex(MODULE.GitError, '超时'):
            self.publisher._run(['sleep'], timeout=0.1)

    def test_terminal_login_removes_noninteractive_prompt_setting(self):
        import json
        code, output, _stderr = self.publisher._run([str(self.executable), 'login'], raw_command=True)
        self.assertEqual(0, code)
        self.assertIsNone(json.loads(output)['prompt'])


if __name__ == '__main__':
    unittest.main()
