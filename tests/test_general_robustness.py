"""v0.5.7 regression cases; only temporary files, local Git and simulated gh."""
import copy
import json
import os
import stat
import subprocess
import tarfile
import threading
import unittest
import zipfile
import tempfile
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from test_backend import MODULE
from test_software_releases import MODULE as RELEASE_MODULE, FakePublisher, ReleaseFixtures


class WorkspaceFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = self.root / 'settings.ini'
        patcher = mock.patch.object(MODULE, 'settings_file_path', return_value=self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)

    def repository(self):
        workspace = self.root / 'workspace'
        workspace.mkdir()
        (workspace / 'source.txt').write_text('source\n')
        return MODULE.initialize_repository(workspace, 'v1.0.0', 'initial', ['source.txt'])


class SettingsRobustnessTest(WorkspaceFixture):
    def writers(self):
        return [lambda: MODULE.save_workspace_history(self.root),
                lambda: MODULE.remove_recent_workspace(self.root),
                lambda: MODULE.clear_recent_workspaces(),
                lambda: MODULE.save_window_settings(1200, 800, False),
                lambda: MODULE.save_git_default_settings('example', '100% User', 'a%b@example.com'),
                lambda: MODULE.save_application_update_settings(automatic=True)]

    def test_percent_paths_and_identity_round_trip_across_all_sections(self):
        workspace = self.root / '100% project'
        workspace.mkdir()
        MODULE.save_workspace_history(workspace)
        MODULE.save_git_default_settings('example', '100% User', 'a%b@example.com')
        MODULE.save_application_update_settings(automatic=False)
        MODULE.save_window_settings(1200, 800, True)
        self.assertEqual([workspace], MODULE.recent_workspaces())
        self.assertEqual(('example', '100% User', 'a%b@example.com'), MODULE.git_default_settings())
        self.assertEqual(workspace, MODULE.configured_repository())
        self.assertFalse(MODULE.application_update_settings()[0])
        self.assertEqual(0o600, stat.S_IMODE(self.settings.stat().st_mode))
        self.settings.chmod(0o640)
        MODULE.clear_recent_workspaces()
        self.assertEqual(0o640, stat.S_IMODE(self.settings.stat().st_mode))
        self.assertEqual(('example', '100% User', 'a%b@example.com'), MODULE.git_default_settings())

    def test_invalid_configuration_reads_defaults_and_all_writes_preserve_bytes(self):
        for content in (b'not an ini', b'[window]\nwidth=1\nwidth=2\n', b'\xff\xfe'):
            self.settings.write_bytes(content)
            self.assertEqual([], MODULE.recent_workspaces())
            self.assertEqual((*MODULE.DEFAULT_WINDOW_SIZE, False), MODULE.window_settings())
            for writer in self.writers():
                with self.subTest(content=content, writer=writer):
                    with self.assertRaises(MODULE.GitError):
                        writer()
                    self.assertEqual(content, self.settings.read_bytes())

    def test_every_writer_preserves_file_when_replace_or_flush_fails(self):
        MODULE.save_workspace_history(self.root)
        original = self.settings.read_bytes()
        for operation in ('replace', 'fsync'):
            for writer in self.writers():
                with self.subTest(operation=operation, writer=writer):
                    with mock.patch.object(MODULE.os, operation, side_effect=OSError('disk failure')):
                        with self.assertRaises(OSError):
                            writer()
                    self.assertEqual(original, self.settings.read_bytes())
                    self.assertEqual([], list(self.root.glob('.settings-*')))

    def test_symlink_and_fifo_configuration_never_followed(self):
        target = self.root / 'target'
        target.write_text('original')
        self.settings.symlink_to(target)
        self.assertEqual([], MODULE.recent_workspaces())
        with self.assertRaises(MODULE.GitError):
            MODULE.save_workspace_history(self.root)
        self.assertEqual('original', target.read_text())
        self.settings.unlink()
        os.mkfifo(self.settings)
        self.assertEqual([], MODULE.recent_workspaces())
        with self.assertRaises(MODULE.GitError):
            MODULE.save_window_settings(1200, 800, False)


class IgnoreRulesTest(WorkspaceFixture):
    def test_pre_enable_nested_negation_and_directory_names_use_git(self):
        workspace = self.root / 'source'
        workspace.mkdir()
        (workspace / '.gitignore').write_text('*.tmp\nbuild/*\n!build/keep.txt\n')
        for name in ('build/keep.txt', 'build/drop.txt', 'install/app', 'logs/keep.txt', 'logs/drop.tmp'):
            path = workspace / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(name)
        (workspace / 'logs' / '.gitignore').write_text('*.txt\n!keep.txt\n')
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'secret').write_text('secret')
        (workspace / 'linked').symlink_to(outside, target_is_directory=True)
        groups = MODULE.top_level_package_groups(workspace)
        files = MODULE.expand_package_groups(groups, list(groups))
        self.assertIn('build/keep.txt', files)
        self.assertNotIn('build/drop.txt', files)
        self.assertIn('install/app', files)
        self.assertIn('logs/keep.txt', files)
        self.assertNotIn('logs/drop.tmp', files)
        self.assertNotIn('linked/secret', files)
        self.assertFalse((workspace / '.git').exists())
        # Existing repositories must use local info/exclude too.
        subprocess.run(['git', 'init', '-q', str(workspace)], check=True)
        (workspace / '.git' / 'info' / 'exclude').write_text('install/\n')
        groups = MODULE.top_level_package_groups(workspace, ['deleted/file.txt'])
        self.assertNotIn('install/', groups)
        self.assertEqual([], groups['deleted/'])
        self.assertIn('build/', groups)

    def test_ignored_remnants_do_not_hide_selected_deletion_scopes(self):
        repo = self.repository()
        directory = repo.path / 'leftovers'
        directory.mkdir()
        (directory / 'old.txt').write_text('old source')
        repo.save_version('v1.0.1', 'directory', ['leftovers/old.txt'])
        (directory / 'old.txt').unlink()
        (directory / 'cache.tmp').write_text('ignored cache')
        (repo.path / '.gitignore').write_text('/VERSION\n/CHANGELOG.rst\n*.tmp\n')
        groups = MODULE.top_level_package_groups(repo.path, repo.deleted_files())
        self.assertEqual([], groups['leftovers/'])
        repo.save_version('v1.0.2', 'delete old source', [], selected_scopes=['leftovers/'])
        self.assertEqual('', repo.run(['ls-tree', '-r', '--name-only', 'HEAD', '--', 'leftovers/old.txt']))
        self.assertEqual('ignored cache', (directory / 'cache.tmp').read_text())


class ArchiveRobustnessTest(WorkspaceFixture):
    def setUp(self):
        super().setUp()
        self.repo = self.repository()

    def test_colliding_reference_names_receive_independent_snapshots(self):
        with mock.patch.object(self.repo, 'compare_tool', return_value='meld'), \
             mock.patch.object(MODULE.shutil, 'which', return_value='/fake/meld'), \
             mock.patch.object(self.repo, 'materialize') as materialize, \
             mock.patch.object(MODULE.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')):
            self.repo.compare('feature/a', 'feature_a')
        first, second = [call.args[1] for call in materialize.call_args_list]
        self.assertNotEqual(first, second)
        self.assertEqual(('left', 'right'), (first.name, second.name))

    def test_malicious_tar_members_are_rejected_without_writing_outside(self):
        for name, kind in (('../escape', tarfile.REGTYPE), ('/escape', tarfile.REGTYPE),
                           ('link', tarfile.LNKTYPE), ('device', tarfile.CHRTYPE),
                           ('pipe', tarfile.FIFOTYPE)):
            with self.subTest(name=name, kind=kind):
                archive = self.root / 'evil.tar'
                with tarfile.open(archive, 'w') as bundle:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = '../outside'
                    bundle.addfile(member)
                with tarfile.open(archive) as bundle, \
                     mock.patch.object(self.repo, '_history_archive', return_value=bundle):
                    with self.assertRaises(MODULE.GitError):
                        self.repo.materialize('unused', self.root / 'snapshot')
        self.assertFalse((self.root / 'escape').exists())

    def test_real_git_symlinks_are_read_only_notes_but_remain_links_in_zip(self):
        secret = self.root / 'secret'
        secret.write_text('must not copy')
        (self.repo.path / 'link').symlink_to(secret)
        self.repo.save_version('v1.0.1', 'link', ['link'])
        snapshot = self.root / 'snapshot'
        with mock.patch.object(self.repo, 'run_bytes', side_effect=AssertionError('in-memory archive')):
            self.repo.materialize('v1.0.1', snapshot)
        note = snapshot / 'link'
        self.assertFalse(note.is_symlink())
        self.assertIn('[read-only symbolic link]', note.read_text())
        self.assertNotIn('must not copy', note.read_text())
        self.assertEqual(0, note.stat().st_mode & 0o222)
        target = self.root / 'release.zip'
        self.repo.export('v1.0.1', target)
        with zipfile.ZipFile(target) as bundle:
            info = bundle.getinfo('link')
            self.assertTrue(stat.S_ISLNK(info.external_attr >> 16))
            self.assertEqual(str(secret).encode(), bundle.read('link'))

    def test_each_manifest_mode_exports_atomically_and_replace_failure_preserves_target(self):
        destination = self.root / 'release.zip'
        for paths in (['source.txt'], [], None):
            with self.subTest(paths=paths), mock.patch.object(self.repo, 'manifest', return_value=paths):
                destination.write_bytes(b'old archive')
                with mock.patch.object(MODULE.os, 'replace', side_effect=OSError('interrupted')):
                    with self.assertRaises(OSError):
                        self.repo.export('v1.0.0', destination)
                self.assertEqual(b'old archive', destination.read_bytes())
                self.assertEqual([], list(self.root.glob('.cvm-export-*')))
                self.repo.export('v1.0.0', destination)
                with zipfile.ZipFile(destination) as bundle:
                    self.assertEqual(paths == [], not bool(bundle.namelist()))
                    self.assertNotIn('VERSION', bundle.namelist())
                    self.assertNotIn('CHANGELOG.rst', bundle.namelist())

    def test_copy_failure_symlink_and_concurrent_target_change_preserve_existing_files(self):
        destination = self.root / 'release.zip'
        destination.write_bytes(b'original')
        with mock.patch.object(MODULE.shutil, 'copyfileobj', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                self.repo.export('v1.0.0', destination)
        self.assertEqual(b'original', destination.read_bytes())
        original = self.repo.run
        def run(arguments, **kwargs):
            result = original(arguments, **kwargs)
            if 'archive' in arguments:
                destination.write_bytes(b'concurrent edit')
            return result
        with mock.patch.object(self.repo, 'run', side_effect=run):
            with self.assertRaisesRegex(MODULE.GitError, '发生变化'):
                self.repo.export('v1.0.0', destination)
        self.assertEqual(b'concurrent edit', destination.read_bytes())
        link = self.root / 'link.zip'
        link.symlink_to(destination)
        with self.assertRaises(MODULE.GitError):
            self.repo.export('v1.0.0', link)
        self.assertEqual(b'concurrent edit', destination.read_bytes())
        self.assertEqual([], list(self.root.glob('.cvm-export-*')))

    def test_materialize_cannot_write_through_existing_parent_symlink(self):
        snapshot = self.root / 'snapshot'
        snapshot.mkdir()
        outside = self.root / 'outside'
        outside.mkdir()
        (snapshot / 'dir').symlink_to(outside, target_is_directory=True)
        (self.repo.path / 'dir').mkdir()
        (self.repo.path / 'dir' / 'file').write_text('data')
        self.repo.save_version('v1.0.1', 'dir', ['dir/file'])
        with self.assertRaises(MODULE.GitError):
            self.repo.materialize('v1.0.1', snapshot)
        self.assertFalse((outside / 'file').exists())

    def test_malicious_zip_members_leave_existing_export_intact(self):
        destination = self.root / 'release.zip'
        original = self.repo.run
        for filename, mode in (('../escape', stat.S_IFREG), ('pipe', stat.S_IFIFO)):
            with self.subTest(filename=filename):
                destination.write_bytes(b'previous export')
                def run(arguments, **kwargs):
                    if 'archive' not in arguments:
                        return original(arguments, **kwargs)
                    output = next(value.split('=', 1)[1] for value in arguments if value.startswith('--output='))
                    with zipfile.ZipFile(output, 'w') as bundle:
                        info = zipfile.ZipInfo(filename)
                        info.external_attr = (mode | 0o644) << 16
                        bundle.writestr(info, b'bad')
                    return ''
                with mock.patch.object(self.repo, 'run', side_effect=run):
                    with self.assertRaises(MODULE.GitError):
                        self.repo.export('v1.0.0', destination)
                self.assertEqual(b'previous export', destination.read_bytes())
                self.assertEqual([], list(self.root.glob('.cvm-export-*')))


class ReleaseRobustnessTest(ReleaseFixtures, unittest.TestCase):
    def test_special_original_names_are_encoded_and_uploaded_as_binary(self):
        source = self.source.rename(self.source.with_name('发布 # 100%.deb'))
        package = RELEASE_MODULE.prepare_software_release(source, threading.Event())
        self.addCleanup(lambda: RELEASE_MODULE.shutil.rmtree(package.path.parent, ignore_errors=True))
        publisher = FakePublisher(package)
        self.assertTrue(publisher.publish(publisher.preview(package, 'Title', 'Notes')).confirmed)
        uploads = [call for call in publisher.calls if call[0] == 'api' and '--input' in call]
        self.assertEqual(2, len(uploads))
        for call in uploads:
            url = next(item for item in call if item.startswith('https://uploads.github.com/'))
            parsed = urlsplit(url)
            self.assertEqual('', parsed.fragment)
            self.assertIn('%23', url)
            self.assertNotIn(' ', url)
            path = Path(call[call.index('--input') + 1])
            self.assertEqual([path.name], parse_qs(parsed.query)['name'])
            self.assertIn('Content-Type: application/octet-stream', call)

    def test_unsafe_names_rejected_before_creating_copies_and_size_limit_is_independent(self):
        for name in ('bad\\name.deb', 'bad\nname.deb', 'bad\x7fname.deb'):
            source = self.root / name
            source.write_bytes(self.source.read_bytes())
            with self.assertRaisesRegex(RELEASE_MODULE.GitError, '文件名|附件名'):
                RELEASE_MODULE.prepare_software_release(source, threading.Event())
        with mock.patch.object(RELEASE_MODULE, 'UPDATE_MAX_PACKAGE_SIZE', 1):
            package = RELEASE_MODULE.prepare_software_release(self.source, threading.Event())
            self.addCleanup(lambda: RELEASE_MODULE.shutil.rmtree(package.path.parent, ignore_errors=True))
        with mock.patch.object(RELEASE_MODULE, 'SOFTWARE_RELEASE_MAX_ASSET_SIZE', 1):
            with self.assertRaises(RELEASE_MODULE.GitError):
                RELEASE_MODULE.prepare_software_release(self.source, threading.Event())

    def test_server_renamed_asset_reports_actual_name_and_keeps_draft(self):
        original = self.publisher._run
        def run(arguments, **kwargs):
            result = original(arguments, **kwargs)
            if arguments[0] == 'api' and '--input' in arguments:
                record = self.publisher.records[0]['assets'][-1]
                record['name'] = 'server-renamed.deb'
                return 0, json.dumps(record).encode(), b''
            return result
        with mock.patch.object(self.publisher, '_run', side_effect=run):
            with self.assertRaisesRegex(RELEASE_MODULE.GitError, 'server-renamed.deb'):
                self.publisher.publish(self.preview())
        self.assertTrue(self.publisher.records[0]['draft'])
        self.assertEqual(0, self.publisher.public_count)

    def test_malformed_remote_release_and_asset_fields_are_rejected(self):
        record = self.publisher.draft([self.publisher.asset(self.package.path)])
        for key, value in (('name', []), ('body', 7), ('updated_at', {}), ('assets', {}), ('draft', 1), ('id', True)):
            invalid = copy.deepcopy(record)
            invalid[key] = value
            with self.subTest(key=key), self.assertRaises(RELEASE_MODULE.GitError):
                self.publisher._state(invalid)
        for key, value in (('digest', []), ('updated_at', 7), ('state', {}), ('name', None), ('id', True), ('size', True)):
            invalid = copy.deepcopy(record)
            invalid['assets'][0][key] = value
            with self.subTest(key=key), self.assertRaises(RELEASE_MODULE.GitError):
                self.publisher._state(invalid)

    def test_authentication_does_not_require_login_creation_or_publish_commands(self):
        original = self.publisher._run
        def run(arguments, **kwargs):
            if arguments[-1] == '--help' and arguments[:2] not in (['api', '--help'], ['auth', 'status']):
                return 1, b'', b'unsupported command'
            return original(arguments, **kwargs)
        with mock.patch.object(self.publisher, '_run', side_effect=run):
            self.assertEqual('maintainer', self.publisher.authentication())
            with self.assertRaises(RELEASE_MODULE.GitError):
                self.preview()
            with self.assertRaises(RELEASE_MODULE.GitError):
                self.publisher.check_tools('create')


class OperationRobustnessTest(WorkspaceFixture):
    def test_shared_worktree_storage_includes_common_objects_once(self):
        repo = self.repository()
        worktree = self.root / 'linked'
        repo.run(['worktree', 'add', '-b', 'linked', str(worktree)])
        linked = MODULE.GitRepository(worktree)
        common = Path(repo.run(['rev-parse', '--absolute-git-dir']).strip())
        # A unique block allocated inside the shared object storage must count.
        (common / 'objects' / 'large-test-data').write_bytes(b'x' * (1024 * 1024))
        expected = MODULE.allocated_disk_usage(common)
        self.assertEqual(expected, linked.storage_size())
        self.assertEqual(expected, repo.storage_size())

    def test_commit_and_tag_survive_manifest_failure_with_precise_stage(self):
        repo = self.repository()
        before = repo.run(['rev-parse', 'HEAD']).strip()
        with mock.patch.object(repo, '_write_manifest', side_effect=OSError('disk failure')):
            with self.assertRaisesRegex(MODULE.GitError, '已发布到本地') as error:
                repo.save_version('v1.0.1', 'next', ['source.txt'])
        actual = repo.run(['rev-parse', 'HEAD']).strip()
        self.assertNotEqual(before, actual)
        self.assertIn(actual, str(error.exception))
        self.assertEqual(actual, repo.run(['rev-parse', 'v1.0.1^{commit}']).strip())

    def test_tag_edit_and_branch_deletion_report_completed_refs_and_recovery(self):
        repo = self.repository()
        with mock.patch.object(repo, '_write_changelog', side_effect=OSError('disk failure')):
            with self.assertRaisesRegex(MODULE.GitError, '版本说明修改已完成') as error:
                repo.update_version_message('v1.0.0', 'changed')
        self.assertIn(repo.run(['rev-parse', 'v1.0.0']).strip(), str(error.exception))
        self.assertIn('recovery/', str(error.exception))
        repo.run(['branch', 'feature'])
        with mock.patch.object(repo, '_remove_branch_metadata', side_effect=OSError('disk failure')):
            with self.assertRaisesRegex(MODULE.GitError, '分支删除已完成') as error:
                repo.delete_branch('feature')
        self.assertNotIn('refs/heads/feature', repo._local_refs())
        self.assertIn('recovery/', str(error.exception))

    def test_damaged_merge_record_remains_cancellable_without_touching_refs(self):
        repo = self.repository()
        repo.metadata_path.mkdir(exist_ok=True)
        path = repo.metadata_path / 'merge.json'
        before = repo._local_refs()
        for data in ([], None, {'comparison_session': []}, {'schema': 1, 'local_refs': [1]}):
            path.write_text(json.dumps(data))
            state = repo.merge_state()
            self.assertTrue(state.active)
            self.assertFalse(state.ready)
            self.assertTrue(state.tool_closed)
            with self.assertRaises(MODULE.GitError):
                repo.complete_merge()
            self.assertIn('已取消', repo.abort_merge())
            self.assertEqual(before, repo._local_refs())


class BackgroundLifecycleTest(WorkspaceFixture):
    def make_window(self):
        if not MODULE.Gtk.init_check()[0]:
            self.skipTest('GTK display unavailable')
        with mock.patch.object(MODULE.VersionManagerWindow, 'load_workspace'):
            window = MODULE.VersionManagerWindow(None, self.root)
        self.addCleanup(window.destroy)
        return window

    def test_success_error_custom_and_timer_callbacks_reject_stale_tasks(self):
        for stale in ('destroy', 'repository', 'generation'):
            for failure, custom in ((False, False), (True, False), (False, True), (True, True)):
                with self.subTest(stale=stale, failure=failure, custom=custom):
                    window = self.make_window()
                    operation = mock.Mock(side_effect=MODULE.GitError('failure')) if failure else mock.Mock(return_value='ok')
                    result_callback, error_callback = mock.Mock(), mock.Mock()
                    with mock.patch.object(MODULE.GLib, 'idle_add') as idle, \
                         mock.patch.object(MODULE.GLib, 'timeout_add_seconds', return_value=42) as timer, \
                         mock.patch.object(MODULE.GLib, 'source_remove'), \
                         mock.patch.object(MODULE.threading, 'Thread') as thread, \
                         mock.patch.object(window, '_background_complete') as complete, \
                         mock.patch.object(window, '_background_failed') as failed:
                        window.run_background('busy', operation, 'done',
                                              result_callback=result_callback if custom else None,
                                              error_callback=error_callback if custom else None)
                        thread.call_args.kwargs['target']()
                        if stale == 'destroy':
                            window.destroy()
                        elif stale == 'repository':
                            window.repository = object()
                        else:
                            window.background_generation += 1
                        callback, result, error = idle.call_args.args
                        self.assertFalse(callback(result, error))
                        self.assertFalse(timer.call_args.args[1]())
                        complete.assert_not_called()
                        failed.assert_not_called()
                        result_callback.assert_not_called()
                        error_callback.assert_not_called()
                        window.background_timer = None
                        window.busy = False

    def test_busy_close_is_blocked_and_load_survives_history_failure(self):
        window = self.make_window()
        window.busy = True
        self.assertTrue(window.emit('delete-event', None))
        self.assertTrue(window.background_alive)
        window.busy = False
        with mock.patch.object(window, '_save_repository_path', side_effect=OSError('read-only settings')), \
             mock.patch.object(window, 'refresh') as refresh, \
             mock.patch.object(window, 'show_error') as show:
            MODULE.VersionManagerWindow.load_workspace(window, self.root)
        self.assertEqual(self.root, window.workspace_path)
        refresh.assert_called_once_with()
        self.assertIn('工作空间已打开', show.call_args.args[0])

    def test_merge_confirmation_cannot_continue_in_another_workspace(self):
        window = self.make_window()
        window.repository = mock.Mock()
        before = window.repository
        plan = MODULE.BranchMergePlan('feature', 'branches/feature/v1.0.0-beta.0.0.1', 'a' * 40,
                                      'v1.0.0', 'b' * 40, ())
        def respond():
            window.repository = mock.Mock()
            return MODULE.Gtk.ResponseType.OK
        with mock.patch.object(window, '_end_background'), mock.patch.object(window, 'refresh'), \
             mock.patch.object(window, 'run_background') as run, mock.patch.object(MODULE.Gtk, 'MessageDialog') as dialog:
            dialog.return_value.run.side_effect = respond
            window._background_merge_ready(plan)
        run.assert_not_called()
        before.merge_to_main.assert_not_called()

    def test_update_result_survives_invalid_settings_and_reports_save_failure(self):
        window = self.make_window()
        window.update_manual_check = True
        self.settings.write_text('broken ini')
        # Use the real dialog labels; all remote work is already represented by the result.
        window.on_application_update_details(None)
        with mock.patch.object(window, '_set_operation_status') as status:
            window._application_update_checked((None, False), '')
        self.assertFalse(window.update_checking)
        self.assertIsNone(window.update_release)
        self.assertIn('无法保存检查时间', window.update_description.get_text())
        self.assertIn('无法保存检查时间', status.call_args.args[0])
        self.assertEqual('broken ini', self.settings.read_text())

    def test_invalid_release_target_disables_publish(self):
        if not RELEASE_MODULE.Gtk.init_check()[0]:
            self.skipTest('GTK display unavailable')
        with mock.patch.object(RELEASE_MODULE, 'software_release_dependency_message', return_value=''), \
             mock.patch.object(RELEASE_MODULE.Gtk.Dialog, 'show_all'):
            dialog = RELEASE_MODULE.SoftwareReleaseDialog(None)
        self.addCleanup(dialog.destroy)
        dialog.batch = mock.Mock()
        dialog.repository_entry.set_text('invalid target')
        self.assertFalse(dialog.get_widget_for_response(RELEASE_MODULE.Gtk.ResponseType.OK).get_sensitive())
        dialog.repository_entry.set_text('owner/repo')
        self.assertTrue(dialog.get_widget_for_response(RELEASE_MODULE.Gtk.ResponseType.OK).get_sensitive())
        dialog.batch = None


class SyncResultRobustnessTest(WorkspaceFixture):
    def test_push_and_pull_refresh_failures_report_committed_refs(self):
        repo = self.repository()
        remote = self.root / 'origin.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
        repo.configure_remote(str(remote), 'Test', 'test@example.com')
        snapshot = repo.sync_snapshot()
        status = repo.remote_sync_status(snapshot)
        with mock.patch.object(repo, '_refresh_tracking_refs', side_effect=OSError('tracking refresh failed')):
            with self.assertRaisesRegex(MODULE.GitError, '远程覆盖已完成') as pushed:
                repo.sync_remote_from_local(snapshot, status)
        old_oid = repo.run(['rev-parse', 'HEAD']).strip()
        self.assertIn(old_oid, str(pushed.exception))
        self.assertIn('recovery/', str(pushed.exception))
        self.assertEqual(old_oid, subprocess.check_output(
            ['git', '--git-dir=' + str(remote), 'rev-parse', 'refs/heads/main'], text=True).strip())
        peer_path = self.root / 'peer'
        subprocess.run(['git', 'clone', '-q', '--branch', 'main', str(remote), str(peer_path)], check=True)
        peer = MODULE.GitRepository(peer_path)
        peer.configure_remote(str(remote), 'Peer', 'peer@example.com')
        (peer.path / 'source.txt').write_text('new remote version')
        peer.save_version('v1.0.1', 'remote change', ['source.txt'])
        peer_snapshot = peer.sync_snapshot()
        peer.sync_remote_from_local(peer_snapshot, peer.remote_sync_status(peer_snapshot))
        snapshot = repo.sync_snapshot()
        status = repo.remote_sync_status(snapshot)
        with mock.patch.object(repo, '_write_changelog', side_effect=OSError('notes refresh failed')):
            with self.assertRaisesRegex(MODULE.GitError, '本地覆盖已完成') as pulled:
                repo.sync_local_from_remote(snapshot, status)
        current = repo.run(['rev-parse', 'HEAD']).strip()
        self.assertNotEqual(old_oid, current)
        self.assertIn(current, str(pulled.exception))
        self.assertIn('recovery/', str(pulled.exception))
        self.assertEqual('new remote version', (repo.path / 'source.txt').read_text())
