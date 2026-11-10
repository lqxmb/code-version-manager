#!/usr/bin/python3
import copy
import base64
import hashlib
import importlib.util
import io
import json
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
    def __init__(self, package, repository=MODULE.UPDATE_REPOSITORY):
        super().__init__(repository)
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
        self.repository_id = 1
        self.private = False
        self.default_branch = 'main'
        self.branch_oid = 'd' * 40
        self.tag_type = 'tag'
        self.public_reads = 0

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
        if self.cancel.is_set():
            raise MODULE.UpdateCancelled('发布已取消。')
        prefix = self.api_path
        if path == 'user':
            return {'login': 'maintainer'}
        if path == prefix:
            return {'id': self.repository_id, 'full_name': self.repository, 'private': self.private,
                    'default_branch': self.default_branch, 'permissions': {'push': self.permission}}
        if '/git/ref/tags/' in path:
            return None if self.missing_tag else {'object': {'sha': self.tag_oid, 'type': self.tag_type}}
        if '/git/ref/heads/' in path:
            return {'object': {'sha': self.branch_oid, 'type': 'commit'}} if self.branch_oid else None
        if path == prefix + '/branches?per_page=1':
            return [{'name': 'other'}] if self.branch_oid else []
        if method == 'PUT' and path == prefix + '/contents/README.md':
            self.branch_oid = 'e' * 40
            return {'commit': {'sha': self.branch_oid, 'parents': []}}
        if method == 'POST' and path == prefix + '/git/refs':
            self.missing_tag = False
            self.tag_type = 'commit'
            self.tag_oid = self.commit_oid = payload['sha']
            return {'ref': payload['ref'], 'object': {'sha': self.tag_oid, 'type': 'commit'}}
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
            record = next(item for item in self.records if item['tag_name'] == self.package.tag)
            record['assets'].append(self.asset(path, 20 + len(record['assets'])))
            record['updated_at'] = str(int(record['updated_at']) + 1)
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

    def _verify_public_release(self, plan, state):
        self.public_reads += 1
        record = copy.deepcopy(next(item for item in self.records if item['id'] == self.latest_id))
        for asset in record['assets']:
            asset['browser_download_url'] = (f'https://github.com/{self.repository}/releases/download/'
                + MODULE.quote(plan.batch.tag, safe='') + '/' + MODULE.quote(asset['name'], safe=''))
        with mock.patch.object(MODULE, 'build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(record).encode())
            super()._verify_public_release(plan, state)
            request = opener.return_value.open.call_args.args[0]
            self.public_request_url = request.full_url


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

    def make_deb(self, name='code-version-manager', version='0.4.10', architecture='all'):
        stage = self.root / ('stage-' + name + '-' + architecture + '-' + version)
        (stage / 'DEBIAN').mkdir(parents=True)
        (stage / 'DEBIAN' / 'control').write_text(
            f'Package: {name}\nVersion: {version}\nArchitecture: {architecture}\n'
            'Maintainer: Test <test@example.com>\nDescription: software release test\n')
        source = self.root / (name + '-' + architecture + '-' + version + '.deb')
        subprocess.run(['dpkg-deb', '--build', str(stage), str(source)], check=True, stdout=subprocess.PIPE)
        return source

    def preview(self):
        return self.publisher.preview(self.package, 'Title', 'Notes')

    def repository(self, name, origin=''):
        workspace = self.root / name
        subprocess.run(['git', 'init', '-q', str(workspace)], check=True)
        if origin:
            subprocess.run(['git', '-C', str(workspace), 'remote', 'add', 'origin', origin], check=True)
        return MODULE.GitRepository(workspace)


class SoftwareReleaseBackendTest(ReleaseFixtures, unittest.TestCase):
    def test_prepare_private_copy_and_checksum(self):
        self.assertEqual('v0.4.10', self.package.tag)
        self.assertNotEqual(self.source, self.package.path)
        self.assertEqual(self.source.name, self.package.path.name)
        self.assertEqual(self.source.read_bytes(), self.package.path.read_bytes())
        self.assertEqual(0o700, self.package.path.parent.stat().st_mode & 0o777)
        self.assertEqual(0o400, self.package.path.stat().st_mode & 0o777)
        self.assertEqual(f'{self.package.sha256}  {self.package.path.name}\n', self.package.checksum_path.read_text())
        before = self.package.path.read_bytes()
        self.source.write_bytes(b'changed')
        self.assertEqual(before, self.package.path.read_bytes())

    def test_accept_other_package_architecture_and_reject_bad_checksum(self):
        for source in (self.make_deb(name='another-package'), self.make_deb(architecture='amd64')):
            package = MODULE.prepare_software_release(source, threading.Event())
            self.addCleanup(lambda package=package: MODULE.SoftwareReleaseBatch((package,)).cleanup())
            self.assertEqual(source.name, package.path.name)
        adjacent = self.source.with_name(self.source.name + '.sha256')
        adjacent.write_text('0' * 64 + '  ' + self.source.name + '\n')
        with self.assertRaisesRegex(MODULE.GitError, '不一致'):
            MODULE.prepare_software_release(self.source, threading.Event())
        adjacent.write_text(self.package.sha256 + '  ' + self.source.name + '\n')
        result = MODULE.prepare_software_release(self.source, threading.Event())
        self.assertEqual(self.package.sha256, result.sha256)

    def test_original_filenames_and_utf8_checksums_are_independent_of_package_metadata(self):
        source = self.make_deb(name='generic-editor', version='1.2.3', architecture='amd64')
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        for filename in ('sample_app-1.2.3-x86_64.deb', 'Different.Release_1.2.3-amd64.deb',
                         '发布包_1.2.3_x86_64.deb'):
            with self.subTest(filename=filename):
                original = self.root / filename
                original.write_bytes(content)
                adjacent = original.with_name(filename + '.sha256')
                adjacent.write_text(digest + ' *' + filename + '\r\n', encoding='utf-8')
                before = adjacent.read_bytes()
                package = MODULE.prepare_software_release(original, threading.Event())
                self.addCleanup(lambda package=package: MODULE.SoftwareReleaseBatch((package,)).cleanup())
                self.assertEqual(('generic-editor', '1.2.3', 'amd64'),
                                 (package.name, package.version, package.architecture))
                self.assertEqual(filename, package.path.name)
                self.assertEqual(filename + '.sha256', package.checksum_path.name)
                self.assertEqual((digest + '  ' + filename + '\n').encode('utf-8'),
                                 package.checksum_path.read_bytes())
                self.assertEqual(content, package.path.read_bytes())
                self.assertEqual(content, original.read_bytes())
                self.assertEqual(before, adjacent.read_bytes())
                MODULE.SoftwareReleasePublisher._verify_package(MODULE.SoftwareReleaseBatch((package,)))

    def test_adjacent_checksum_filename_must_match_original_and_failures_clean_cache(self):
        adjacent = self.source.with_name(self.source.name + '.sha256')
        legacy_name = f'{self.package.name}_{self.package.version}_{self.package.architecture}.deb'
        for filename in (legacy_name, 'unrelated.deb'):
            with self.subTest(filename=filename):
                adjacent.write_text(self.package.sha256 + '  ' + filename + '\n', encoding='utf-8')
                before = set(MODULE.application_update_cache().iterdir())
                with self.assertRaisesRegex(MODULE.GitError, '不一致'):
                    MODULE.prepare_software_release(self.source, threading.Event())
                self.assertEqual(before, set(MODULE.application_update_cache().iterdir()))
                self.assertEqual(self.package.path.read_bytes(), self.source.read_bytes())
        adjacent.write_bytes(b'\xff')
        before = set(MODULE.application_update_cache().iterdir())
        with self.assertRaisesRegex(MODULE.GitError, 'UTF-8'):
            MODULE.prepare_software_release(self.source, threading.Event())
        self.assertEqual(before, set(MODULE.application_update_cache().iterdir()))

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

    def test_preview_fixes_tag_commit_and_missing_tag_and_permissions(self):
        plan = self.preview()
        self.assertEqual('a' * 40, plan.tag_oid)
        self.assertEqual('b' * 40, plan.commit_oid)
        self.assertIsNone(plan.draft)
        self.publisher.missing_tag = True
        plan = self.preview()
        self.assertEqual('', plan.tag_oid)
        self.assertEqual(self.publisher.branch_oid, plan.target.branch_oid)
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
        with mock.patch.object(self.publisher, '_verify_public_release', side_effect=MODULE.GitError('匿名接口未显示版本')):
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


class GeneralSoftwareReleaseTest(ReleaseFixtures, unittest.TestCase):
    def batch(self):
        sources = []
        for architecture, label in (('arm64', 'aarch64'), ('amd64', 'x86_64')):
            source = self.make_deb(name='p7-lane-editor', version='1.2.3', architecture=architecture)
            sources.append(source.rename(source.with_name(f'custom_editor-1.2.3-{label}.deb')))
        return MODULE.prepare_software_release_batch(sources, threading.Event())

    def test_target_address_normalization_and_rejection(self):
        for value in (' lqxmb/p7_lane_editor ', 'https://github.com/lqxmb/p7_lane_editor',
                      'https://github.com/lqxmb/p7_lane_editor.git',
                      'https://github.com/lqxmb/p7_lane_editor/releases/'):
            self.assertEqual('lqxmb/p7_lane_editor', MODULE.validate_release_repository(value))
        for value in ('', '/owner/repo', 'owner/repo/extra', 'owner/../repo', 'owner/repo?x=1',
                      'https://github.com.evil.example/owner/repo', 'http://github.com/owner/repo',
                      'https://user:secret@github.com/owner/repo', 'https://github.com/owner/repo#x',
                      'https://github.com/owner/repo?x=1', 'https://github.com/owner/repo/releases/tag/v1',
                      'https://github.com/owner/repo%2Fextra', 'https://github.com:443/owner/repo'):
            with self.subTest(value=value), self.assertRaises(MODULE.GitError):
                MODULE.validate_release_repository(value)

    def test_multiarchitecture_packages_and_all_checksums_publish_to_selected_repository(self):
        batch = self.batch()
        self.assertEqual(('amd64', 'arm64'), tuple(package.architecture for package in batch.packages))
        publisher = FakePublisher(batch, 'lqxmb/p7_lane_editor')
        with mock.patch.object(MODULE, 'check_application_update', side_effect=AssertionError('wrong update source')):
            result = publisher.publish(publisher.preview(batch, 'P7 1.2.3', 'Changes'))
        self.assertTrue(result.confirmed, result.message)
        self.assertIn('/lqxmb/p7_lane_editor/', result.url)
        self.assertEqual(4, len(publisher.records[0]['assets']))
        self.assertEqual('https://api.github.com/repos/lqxmb/p7_lane_editor/releases/latest',
                         publisher.public_request_url)
        uploads = [call for call in publisher.calls if call[:2] == ('release', 'upload')]
        self.assertEqual(4, len(uploads))
        self.assertTrue(all(call[-1] == 'github.com/lqxmb/p7_lane_editor' for call in uploads))
        expected = {'custom_editor-1.2.3-x86_64.deb', 'custom_editor-1.2.3-x86_64.deb.sha256',
                    'custom_editor-1.2.3-aarch64.deb', 'custom_editor-1.2.3-aarch64.deb.sha256'}
        self.assertEqual(expected, {Path(call[3]).name for call in uploads})
        self.assertEqual(expected, {asset['name'] for asset in publisher.records[0]['assets']})

    def test_unicode_original_filename_publishes_and_missing_checksum_is_generated(self):
        source = self.source.rename(self.source.with_name('发布包_0.4.10.deb'))
        batch = MODULE.prepare_software_release_batch([source], threading.Event())
        publisher = FakePublisher(batch)
        result = publisher.publish(publisher.preview(batch, 'Title', 'Notes'))
        self.assertTrue(result.confirmed, result.message)
        self.assertEqual({'发布包_0.4.10.deb', '发布包_0.4.10.deb.sha256'},
                         {asset['name'] for asset in publisher.records[0]['assets']})
        package = batch.packages[0]
        self.assertEqual(package.sha256 + '  ' + source.name + '\n',
                         package.checksum_path.read_text(encoding='utf-8'))

    def test_old_renamed_draft_is_rejected_without_upload_or_replacement(self):
        asset = self.publisher.asset(self.package.path)
        asset['name'] = f'{self.package.name}_{self.package.version}_{self.package.architecture}.deb'
        draft = self.publisher.draft([asset])
        before = copy.deepcopy(draft)
        with self.assertRaisesRegex(MODULE.GitError, '额外附件'):
            self.preview()
        self.assertEqual(before, draft)
        self.assertFalse(any(call[:2] == ('release', 'upload') for call in self.publisher.calls))

    def test_batch_rejects_mixed_names_versions_duplicate_architectures_and_cleans_up(self):
        for other in (self.make_deb(name='different-package'), self.make_deb(version='0.4.11'), self.source):
            before = set(MODULE.application_update_cache().iterdir())
            with self.assertRaises(MODULE.GitError):
                MODULE.prepare_software_release_batch([self.source, other], threading.Event())
            self.assertEqual(before, set(MODULE.application_update_cache().iterdir()))
        with self.assertRaises(MODULE.GitError):
            MODULE.prepare_software_release_batch([], threading.Event())

    def test_different_architectures_with_same_filename_are_rejected_and_copies_cleaned(self):
        other = self.make_deb(architecture='arm64')
        directory = self.root / 'other-architecture'
        directory.mkdir()
        other = other.rename(directory / self.source.name)
        before = set(MODULE.application_update_cache().iterdir())
        with self.assertRaisesRegex(MODULE.GitError, '同名附件'):
            MODULE.prepare_software_release_batch([self.source, other], threading.Event())
        self.assertEqual(before, set(MODULE.application_update_cache().iterdir()))
        self.assertTrue(self.source.is_file())
        self.assertTrue(other.is_file())

    def test_version_must_be_valid_git_tag(self):
        source = self.make_deb(version='1:0.4.10')
        before = set(MODULE.application_update_cache().iterdir())
        with self.assertRaisesRegex(MODULE.GitError, 'Git 标签'):
            MODULE.prepare_software_release(source, threading.Event())
        self.assertEqual(before, set(MODULE.application_update_cache().iterdir()))

    def test_empty_distribution_repository_initialized_only_after_confirmation(self):
        batch = self.batch()
        publisher = FakePublisher(batch, 'lqxmb/p7_lane_editor')
        publisher.missing_tag = True
        publisher.branch_oid = ''
        plan = publisher.preview(batch, 'P7', 'Changes')
        self.assertEqual('', plan.target.branch_oid)
        self.assertFalse(any(call[0] in ('PUT', 'POST', 'PATCH') for call in publisher.calls))
        result = publisher.publish(plan)
        self.assertTrue(result.confirmed, result.message)
        mutations = [call for call in publisher.calls if call[0] in ('PUT', 'POST')]
        self.assertEqual(['PUT', 'POST'], [call[0] for call in mutations])
        self.assertTrue(mutations[0][1].endswith('/contents/README.md'))
        readme = base64.b64decode(mutations[0][2]['content']).decode()
        self.assertIn('p7_lane_editor', readme)
        self.assertNotIn('core', readme)
        self.assertEqual({'ref': 'refs/tags/v1.2.3', 'sha': 'e' * 40}, mutations[1][2])

    def test_missing_tag_created_at_confirmed_default_branch_without_initialization(self):
        self.publisher.missing_tag = True
        plan = self.preview()
        self.assertTrue(self.publisher.publish(plan).confirmed)
        writes = [call for call in self.publisher.calls if call[0] in ('PUT', 'POST')]
        self.assertEqual(1, len(writes))
        self.assertEqual({'ref': 'refs/tags/v0.4.10', 'sha': 'd' * 40}, writes[0][2])

    def test_target_snapshot_changes_block_all_remote_mutations(self):
        for field, value in (('repository_id', 2), ('private', True), ('default_branch', 'release'),
                             ('branch_oid', 'f' * 40)):
            publisher = FakePublisher(self.package)
            publisher.missing_tag = True
            plan = publisher.preview(self.package, 'Title', 'Notes')
            setattr(publisher, field, value)
            with self.subTest(field=field), self.assertRaisesRegex(MODULE.GitError, '确认后'):
                publisher.publish(plan)
            self.assertFalse(any(call[0] in ('PUT', 'POST', 'PATCH') for call in publisher.calls))

    def test_plan_cannot_be_published_to_another_repository(self):
        plan = self.preview()
        publisher = FakePublisher(self.package, 'other/project')
        with self.assertRaisesRegex(MODULE.GitError, '确认后目标'):
            publisher.publish(plan)
        self.assertEqual([], publisher.calls)

    def test_canonical_repository_case_is_used_for_release_links_and_assets(self):
        publisher = FakePublisher(self.package, 'lqxmb/p7_lane_editor')
        original = publisher._api
        def api(method, path, payload=None, missing=False):
            result = original(method, path, payload, missing)
            if path.lower() == 'repos/lqxmb/p7_lane_editor':
                result['full_name'] = 'lqxmb/P7_Lane_Editor'
            return result
        with mock.patch.object(publisher, '_api', side_effect=api):
            plan = publisher.preview(self.package, 'Title', 'Notes')
            result = publisher.publish(plan)
        self.assertTrue(result.confirmed, result.message)
        self.assertEqual('lqxmb/P7_Lane_Editor', plan.target.repository)
        self.assertIn('/lqxmb/P7_Lane_Editor/', result.url)

    def test_initialization_or_tag_retained_when_later_stage_fails(self):
        self.publisher.branch_oid = ''
        self.publisher.missing_tag = True
        self.publisher.fail_upload = self.package.path.name
        with self.assertRaises(MODULE.GitError) as error:
            self.publisher.publish(self.preview())
        self.assertIn('已创建发行 README', str(error.exception))
        self.assertIn('已创建标签', str(error.exception))
        self.assertEqual('e' * 40, self.publisher.branch_oid)
        self.assertFalse(self.publisher.missing_tag)
        self.assertTrue(self.publisher.records[0]['draft'])
        self.assertFalse(any(call[0] == 'DELETE' for call in self.publisher.calls))

    def test_initialization_detects_concurrent_commit(self):
        self.publisher.branch_oid = ''
        self.publisher.missing_tag = True
        original = self.publisher._api
        def api(method, path, payload=None, missing=False):
            record = original(method, path, payload, missing)
            if method == 'PUT':
                record['commit']['parents'] = [{'sha': 'f' * 40}]
            return record
        plan = self.preview()
        with mock.patch.object(self.publisher, '_api', side_effect=api):
            with self.assertRaisesRegex(MODULE.GitError, '并发修改'):
                self.publisher.publish(plan)
        self.assertTrue(self.publisher.missing_tag)
        self.assertEqual([], self.publisher.records)

    def test_private_release_uses_authorized_verification_and_explains_client_auth(self):
        self.publisher.private = True
        with mock.patch.object(self.publisher, '_verify_public_release') as anonymous:
            result = self.publisher.publish(self.preview())
        self.assertTrue(result.confirmed)
        self.assertIn('客户端下载也需要', result.message)
        anonymous.assert_not_called()

    def test_visibility_changed_during_upload_stops_publication(self):
        plan = self.preview()
        original = self.publisher._run
        def run(arguments, **kwargs):
            result = original(arguments, **kwargs)
            if arguments[:2] == ['release', 'upload']:
                self.publisher.private = True
            return result
        with mock.patch.object(self.publisher, '_run', side_effect=run):
            with self.assertRaisesRegex(MODULE.GitError, '可见性.*发生变化'):
                self.publisher.publish(plan)
        self.assertEqual(0, self.publisher.public_count)

    def test_batch_cancellation_after_first_package_cleans_prepared_copies(self):
        second = self.make_deb(architecture='arm64')
        cancel = threading.Event()
        original = MODULE.prepare_software_release
        def prepare(source, event):
            package = original(source, event)
            cancel.set()
            return package
        before = set(MODULE.application_update_cache().iterdir())
        with mock.patch.object(MODULE, 'prepare_software_release', side_effect=prepare):
            with self.assertRaises(MODULE.UpdateCancelled):
                MODULE.prepare_software_release_batch([self.source, second], cancel)
        self.assertEqual(before, set(MODULE.application_update_cache().iterdir()))

    def test_multiarchitecture_partial_upload_can_resume(self):
        batch = self.batch()
        publisher = FakePublisher(batch, 'lqxmb/p7_lane_editor')
        publisher.fail_upload = batch.packages[1].path.name
        with self.assertRaises(MODULE.GitError):
            publisher.publish(publisher.preview(batch, 'Title', 'Notes'))
        self.assertEqual(2, len(publisher.records[0]['assets']))
        publisher.fail_upload = None
        result = publisher.publish(publisher.preview(batch, 'Title', 'Notes'))
        self.assertTrue(result.confirmed, result.message)
        self.assertEqual(4, len(publisher.records[0]['assets']))
        names = [asset['name'] for asset in publisher.records[0]['assets']]
        self.assertEqual({path.name for path in batch.assets}, set(names))
        self.assertEqual(1, names.count('custom_editor-1.2.3-x86_64.deb'))
        self.assertEqual(1, names.count('custom_editor-1.2.3-aarch64.deb'))

    def test_anonymous_release_response_and_redirects_are_checked_without_credentials(self):
        draft = self.publisher.draft([self.publisher.asset(path, 20 + i)
                                      for i, path in enumerate(self.package.assets)])
        draft['draft'] = False
        for asset in draft['assets']:
            asset['browser_download_url'] = (f'https://github.com/{self.publisher.repository}/releases/download/'
                                             f'{self.package.tag}/{asset["name"]}')
        state = self.publisher._state(draft)
        plan = MODULE.SoftwareReleasePlan(MODULE.SoftwareReleaseBatch((self.package,)),
            self.publisher._target(False), 'Title', 'Notes', 'a' * 40, 'b' * 40, None, None)
        for mutation in ('none', 'missing', 'url', 'size', 'prerelease', 'malformed', 'oversize'):
            record = copy.deepcopy(draft)
            if mutation == 'missing':
                record['assets'].pop()
            elif mutation == 'url':
                record['assets'][0]['browser_download_url'] = 'https://evil.example/package.deb'
            elif mutation == 'size':
                record['assets'][0]['size'] += 1
            elif mutation == 'prerelease':
                record['prerelease'] = True
            raw = json.dumps(record).encode()
            if mutation == 'malformed':
                raw = b'[]'
            elif mutation == 'oversize':
                raw = b'x' * (2 * 1024 * 1024 + 1)
            with self.subTest(mutation=mutation), mock.patch.object(MODULE, 'build_opener') as opener:
                opener.return_value.open.return_value = io.BytesIO(raw)
                if mutation == 'none':
                    MODULE.SoftwareReleasePublisher._verify_public_release(self.publisher, plan, state)
                else:
                    with self.assertRaises(MODULE.GitError):
                        MODULE.SoftwareReleasePublisher._verify_public_release(self.publisher, plan, state)
                request = opener.return_value.open.call_args.args[0]
                self.assertNotIn('Authorization', dict(request.header_items()))
                handler = opener.call_args.args[0]
                with self.assertRaises(MODULE.GitError):
                    handler.redirect_request(None, None, 302, '', {}, 'https://evil.example')


class ReleaseRepositorySettingsTest(ReleaseFixtures, unittest.TestCase):
    def test_targets_are_local_and_origin_is_only_a_default(self):
        first = self.repository('core', 'git@github.com:lqxmb/p7_lane_editor_core.git')
        second = self.repository('other', 'git@github.com:other/project.git')
        self.assertEqual('lqxmb/p7_lane_editor_core', first.release_repository())
        first.configure_release_repository('https://github.com/lqxmb/p7_lane_editor/releases')
        self.assertEqual('lqxmb/p7_lane_editor', first.release_repository())
        self.assertEqual([], first.release_repository_history())
        self.assertEqual('other/project', second.release_repository())
        self.assertEqual('git@github.com:lqxmb/p7_lane_editor_core.git', first.remote_url())
        self.assertFalse(self.settings.exists())
        with self.assertRaises(MODULE.GitError):
            first.configure_release_repository('https://evil.example/owner/repo')
        self.assertEqual('lqxmb/p7_lane_editor', first.release_repository())

    def test_without_github_origin_no_hardcoded_default(self):
        for index, origin in enumerate(('', 'git@gitlab.com:owner/project.git')):
            repo = self.repository(str(index), origin)
            self.assertEqual('', repo.release_repository())

    def test_success_history_persists_all_targets_newest_first_and_deduplicates(self):
        first = self.repository('first', 'git@github.com:owner/source.git')
        second = self.repository('second', 'git@github.com:other/source.git')
        first.configure_release_repository('owner/legacy')
        for index in range(12):
            first.record_release_repository(f'owner/release-{index}')
        first.record_release_repository('https://github.com/Owner/Release-0/releases')
        history = ['Owner/Release-0'] + [f'owner/release-{index}' for index in range(11, 0, -1)]
        reopened = MODULE.GitRepository(first.path)
        self.assertEqual(history, reopened.release_repository_history())
        self.assertEqual(history[0], reopened.release_repository())
        self.assertEqual('owner/legacy', first.run([
            'config', '--local', '--get', 'codeversionmanager.releaseRepository']).strip())
        self.assertEqual([], second.release_repository_history())
        self.assertEqual('other/source', second.release_repository())
        self.assertEqual('git@github.com:owner/source.git', first.remote_url())
        self.assertFalse(self.settings.exists())

    def test_invalid_history_is_filtered_without_writing_config(self):
        repo = self.repository('workspace', 'git@github.com:owner/source.git')
        config = repo.metadata_path.parent / 'config'
        for value, expected in (
                ('invalid JSON', []), ('null', []), ('{}', []), ('"owner/release"', []),
                (json.dumps([None, 5, {}, 'https://evil.example/owner/repo', 'owner/first',
                             'OWNER/FIRST', 'https://github.com/owner/second/releases']),
                 ['owner/first', 'owner/second'])):
            with self.subTest(value=value):
                repo.run(['config', '--local', 'codeversionmanager.releaseRepositoryHistory', value])
                before = config.read_bytes()
                self.assertEqual(expected, repo.release_repository_history())
                self.assertEqual(expected[0] if expected else 'owner/source', repo.release_repository())
                self.assertEqual(before, config.read_bytes())

    def test_invalid_target_and_config_write_failure_preserve_previous_history(self):
        repo = self.repository('workspace')
        repo.record_release_repository('owner/first')
        with self.assertRaises(MODULE.GitError):
            repo.record_release_repository('https://evil.example/owner/repo')
        self.assertEqual(['owner/first'], repo.release_repository_history())
        config = repo.metadata_path.parent / 'config'
        before = config.read_bytes()
        config.with_name('config.lock').touch()
        with self.assertRaises(MODULE.GitError):
            repo.record_release_repository('owner/second')
        self.assertEqual(before, config.read_bytes())
        self.assertEqual(['owner/first'], repo.release_repository_history())


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

    def open_repository(self, repo):
        self.dialog.destroy()
        self.window.repository = repo
        self.window.on_publish_software(None)
        self.dialog = self.window.software_release_dialog

    def confirmed_publish_worker(self, plan):
        self.dialog.batch = plan.batch
        self.dialog.busy = True
        with mock.patch.object(MODULE.Gtk, 'MessageDialog') as confirmation, \
                mock.patch.object(self.dialog, '_worker') as worker:
            confirmation.return_value.run.return_value = MODULE.Gtk.ResponseType.OK
            self.dialog._preview_ready(plan, '')
        return worker.call_args.args

    def test_target_history_dropdown_and_legacy_prefill_support_manual_input(self):
        repo = self.repository('workspace', 'git@github.com:owner/source.git')
        repo.configure_release_repository('owner/legacy')
        self.open_repository(repo)
        self.assertEqual('owner/legacy', self.dialog.repository_entry.get_text())
        self.assertEqual(0, len(self.dialog.repository_combo.get_model()))
        repo.record_release_repository('owner/first')
        repo.record_release_repository('owner/second')
        self.open_repository(repo)
        self.assertEqual('owner/second', self.dialog.repository_entry.get_text())
        self.assertEqual(['owner/second', 'owner/first'],
                         [row[0] for row in self.dialog.repository_combo.get_model()])
        self.dialog.repository_combo.set_active(1)
        self.assertEqual('owner/first', self.dialog.repository_entry.get_text())
        self.assertEqual('https://github.com/owner/first/releases', self.dialog.link.get_uri())
        self.dialog.repository_entry.set_text('https://github.com/owner/new/releases')
        self.assertEqual('https://github.com/owner/new/releases', self.dialog.repository_entry.get_text())
        self.assertEqual(['owner/second', 'owner/first'], repo.release_repository_history())
        self.open_repository(repo)
        self.assertEqual('owner/second', self.dialog.repository_entry.get_text())

    def test_menu_opens_single_dialog_package_autofills_version(self):
        self.assertEqual('发布软件到 GitHub', self.window.publish_software_item.get_label())
        self.window.on_publish_software(None)
        self.assertIs(self.dialog, self.window.software_release_dialog)
        self.dialog.repository_entry.set_text(MODULE.UPDATE_REPOSITORY)
        self.dialog._package_ready(MODULE.SoftwareReleaseBatch((self.package,)), '')
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
        self.dialog.batch = MODULE.SoftwareReleaseBatch((self.package,))
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
            self.dialog._worker(lambda cancel: MODULE.SoftwareReleaseBatch((self.package,)), callback)
            thread.call_args.kwargs['target']()
        self.dialog.destroy()
        callbacks[0][0](*callbacks[0][1:])
        callback.assert_not_called()
        self.assertFalse(self.package.path.exists())

    def test_multiple_packages_are_selected_and_displayed(self):
        second = self.make_deb(architecture='arm64')
        self.dialog.repository_entry.set_text('lqxmb/p7_lane_editor')
        with mock.patch.object(MODULE.Gtk, 'FileChooserDialog') as chooser, mock.patch.object(self.dialog, '_worker') as worker:
            chooser.return_value.run.return_value = MODULE.Gtk.ResponseType.OK
            chooser.return_value.get_filenames.return_value = [str(self.source), str(second)]
            self.dialog._select_package(None)
            chooser.return_value.set_select_multiple.assert_called_once_with(True)
            batch = worker.call_args.args[0](threading.Event())
        self.dialog._package_ready(batch, '')
        buffer = self.dialog.package_details.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        for expected in ('包内信息：包名', '架构 all', '架构 arm64', self.source.name, second.name + '.sha256'):
            self.assertIn(expected, text)
        self.assertEqual(4, len(self.dialog.batch.assets))

    def test_original_filename_and_internal_architecture_displayed_and_confirmed(self):
        source = self.make_deb(name='generic-editor', version='1.2.3', architecture='amd64')
        source = source.rename(source.with_name('custom_editor-1.2.3-x86_64.deb'))
        batch = MODULE.prepare_software_release_batch([source], threading.Event())
        self.dialog.repository_entry.set_text('owner/releases')
        self.dialog._package_ready(batch, '')
        buffer = self.dialog.package_details.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        for expected in ('包名 generic-editor', '版本 1.2.3', '架构 amd64',
                         '附件：' + source.name, source.name + '.sha256'):
            self.assertIn(expected, text)
        publisher = FakePublisher(batch, 'owner/releases')
        plan = publisher.preview(batch, 'Title', 'Notes')
        with mock.patch.object(MODULE.Gtk, 'MessageDialog') as confirmation, \
                mock.patch.object(self.dialog, '_worker') as worker:
            confirmation.return_value.run.return_value = MODULE.Gtk.ResponseType.CANCEL
            self.dialog._preview_ready(plan, '')
        text = confirmation.return_value.format_secondary_text.call_args.args[0]
        self.assertIn(source.name, text)
        self.assertIn(source.name + '.sha256', text)
        worker.assert_not_called()

    def test_dialog_keeps_original_workspace_target_after_workspace_switch(self):
        first = self.repository('first', 'git@github.com:lqxmb/p7_lane_editor_core.git')
        second = self.repository('second', 'git@github.com:other/project.git')
        self.open_repository(first)
        self.assertEqual('lqxmb/p7_lane_editor_core', self.dialog.repository_entry.get_text())
        self.window.repository = second
        self.dialog.repository_entry.set_text('https://github.com/lqxmb/p7_lane_editor/releases')
        self.dialog._package_ready(MODULE.SoftwareReleaseBatch((self.package,)), '')
        self.dialog.notes.get_buffer().set_text('Changes')
        self.publisher.repository = 'lqxmb/p7_lane_editor'
        plan = self.preview()
        with mock.patch.object(MODULE.SoftwareReleasePublisher, 'preview', return_value=plan) as preview, \
                mock.patch.object(self.dialog, '_worker') as worker:
            self.dialog._response(self.dialog, MODULE.Gtk.ResponseType.OK)
            worker.call_args.args[0](threading.Event())
        self.assertEqual('lqxmb/p7_lane_editor_core', first.release_repository())
        self.assertEqual([], first.release_repository_history())
        self.assertEqual('other/project', second.release_repository())
        self.assertEqual('lqxmb/p7_lane_editor', self.dialog.repository_entry.get_text())
        preview.assert_called_once()
        self.assertIn('/lqxmb/p7_lane_editor/releases', self.dialog.link.get_uri())
        operation, callback = self.confirmed_publish_worker(plan)
        with mock.patch.object(MODULE.SoftwareReleasePublisher, 'publish', return_value=MODULE.SoftwareReleaseResult(
                'https://github.com/lqxmb/p7_lane_editor/releases/tag/' + self.package.tag, True, '发布成功')):
            result = operation(threading.Event())
        self.assertEqual(['lqxmb/p7_lane_editor'], first.release_repository_history())
        self.assertEqual([], second.release_repository_history())
        callback(result, '')
        self.assertEqual(['lqxmb/p7_lane_editor'], [row[0] for row in self.dialog.repository_combo.get_model()])
        self.assertIn('公开', self.dialog.visibility_label.get_text())
        self.assertEqual(result.url, self.dialog.link.get_uri())
        self.open_repository(first)
        self.assertEqual('lqxmb/p7_lane_editor', self.dialog.repository_entry.get_text())

    def test_unconfirmed_and_failed_publications_do_not_change_saved_target(self):
        repo = self.repository('workspace')
        repo.record_release_repository('owner/success')
        self.open_repository(repo)
        plan = self.preview()
        for outcome in (MODULE.SoftwareReleaseResult(self.publisher.web_url, False, '发布结果待确认'),
                        MODULE.GitError('上传失败，草稿保留'), MODULE.UpdateCancelled('发布已取消')):
            with self.subTest(outcome=outcome):
                operation, callback = self.confirmed_publish_worker(plan)
                with mock.patch.object(MODULE.SoftwareReleasePublisher, 'publish', side_effect=[outcome]):
                    if isinstance(outcome, Exception):
                        with self.assertRaises(type(outcome)):
                            operation(threading.Event())
                        callback(None, str(outcome))
                    else:
                        callback(operation(threading.Event()), '')
                self.assertEqual(['owner/success'], repo.release_repository_history())
                self.assertEqual('owner/success', repo.release_repository())

    def test_failed_precheck_and_declined_confirmation_do_not_save_target(self):
        repo = self.repository('workspace')
        repo.configure_release_repository('owner/legacy')
        self.open_repository(repo)
        self.dialog.repository_entry.set_text(MODULE.UPDATE_REPOSITORY)
        self.dialog.batch = MODULE.SoftwareReleaseBatch((self.package,))
        with mock.patch.object(MODULE.SoftwareReleasePublisher, 'preview', side_effect=MODULE.GitError('预检查失败')), \
                mock.patch.object(self.dialog, '_worker') as worker:
            self.dialog._response(self.dialog, MODULE.Gtk.ResponseType.OK)
            with self.assertRaisesRegex(MODULE.GitError, '预检查失败'):
                worker.call_args.args[0](threading.Event())
        self.dialog._preview_ready(None, '预检查失败')
        with mock.patch.object(MODULE.Gtk, 'MessageDialog') as confirmation, \
                mock.patch.object(self.dialog, '_worker') as worker:
            confirmation.return_value.run.return_value = MODULE.Gtk.ResponseType.CANCEL
            self.dialog._preview_ready(self.preview(), '')
        worker.assert_not_called()
        self.assertEqual([], repo.release_repository_history())
        self.assertEqual('owner/legacy', repo.release_repository())

    def test_creating_repository_only_updates_current_form(self):
        repo = self.repository('workspace')
        repo.record_release_repository('owner/success')
        self.open_repository(repo)
        self.dialog.repository_entry.set_text('owner/new')
        creation_plan = MODULE.SoftwareReleaseRepositoryCreationPlan('owner/new', 'owner', False)
        target = MODULE.SoftwareReleaseTarget('owner/new', 12, False, 'main', '')
        outcome = MODULE.SoftwareReleaseRepositoryCreationResult(creation_plan, target, '已创建')
        with mock.patch.object(MODULE.Gtk.Dialog, 'run', return_value=MODULE.Gtk.ResponseType.OK), \
                mock.patch.object(MODULE.SoftwareReleasePublisher, 'create_repository', return_value=outcome), \
                mock.patch.object(self.dialog, '_worker') as worker:
            self.dialog._repository_creation_ready(creation_plan, '')
            operation, callback = worker.call_args.args
            callback(operation(threading.Event()), '')
        self.assertEqual('owner/new', self.dialog.repository_entry.get_text())
        self.assertEqual(['owner/success'], repo.release_repository_history())
        self.assertEqual('owner/success', repo.release_repository())

    def test_success_with_history_save_failure_keeps_published_result_and_link(self):
        repo = self.repository('workspace')
        repo.record_release_repository('owner/success')
        self.open_repository(repo)
        plan = self.preview()
        operation, callback = self.confirmed_publish_worker(plan)
        published = MODULE.SoftwareReleaseResult(self.publisher.web_url, True, '正式 Release 已发布')
        with mock.patch.object(MODULE.SoftwareReleasePublisher, 'publish', return_value=published), \
                mock.patch.object(repo, 'record_release_repository', side_effect=MODULE.GitError('config locked')):
            result = operation(threading.Event())
        self.assertTrue(result.confirmed)
        callback(result, '')
        buffer = self.dialog.details.get_buffer()
        self.assertIn('历史保存失败', buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True))
        self.assertEqual(published.url, self.dialog.link.get_uri())
        self.assertEqual('打开已发布版本', self.dialog.link.get_label())
        self.assertEqual(['owner/success'], repo.release_repository_history())

    def test_confirmed_target_is_recorded_even_if_dialog_is_destroyed(self):
        repo = self.repository('workspace')
        self.open_repository(repo)
        plan = self.preview()
        operation, _callback = self.confirmed_publish_worker(plan)
        self.dialog.destroy()
        with mock.patch.object(MODULE.SoftwareReleasePublisher, 'publish', return_value=MODULE.SoftwareReleaseResult(
                self.publisher.web_url, True, '已发布')):
            operation(threading.Event())
        self.assertEqual([plan.target.repository], repo.release_repository_history())

    def test_confirmation_lists_initialization_private_visibility_and_all_assets(self):
        batch = MODULE.SoftwareReleaseBatch((self.package,))
        self.publisher.private = True
        self.publisher.branch_oid = ''
        self.publisher.missing_tag = True
        plan = self.preview()
        self.dialog.batch = batch
        self.dialog.busy = True
        with mock.patch.object(MODULE.Gtk, 'MessageDialog') as confirmation, mock.patch.object(self.dialog, '_worker') as worker:
            confirmation.return_value.run.return_value = MODULE.Gtk.ResponseType.CANCEL
            self.dialog._preview_ready(plan, '')
        text = confirmation.return_value.format_secondary_text.call_args.args[0]
        for expected in ('初始化空仓库', '将创建标签', '私有', '客户端下载也需要', self.package.checksum_path.name):
            self.assertIn(expected, text)
        worker.assert_not_called()

    def test_invalid_target_cannot_start_preview(self):
        self.dialog.batch = MODULE.SoftwareReleaseBatch((self.package,))
        self.dialog.repository_entry.set_text('https://evil.example/owner/repo')
        with mock.patch.object(self.dialog, '_worker') as worker:
            self.dialog._response(self.dialog, MODULE.Gtk.ResponseType.OK)
        worker.assert_not_called()
        self.assertFalse(self.dialog.link.get_sensitive())

    def test_release_form_can_scroll_without_pushing_actions_below_window(self):
        while MODULE.Gtk.events_pending():
            MODULE.Gtk.main_iteration_do(False)
        self.assertLessEqual(self.dialog.get_preferred_height().minimum_height, 740)
        self.assertTrue(self.dialog.get_widget_for_response(MODULE.Gtk.ResponseType.OK).get_visible())


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
        self.publisher = MODULE.SoftwareReleasePublisher(MODULE.UPDATE_REPOSITORY)
        self.publisher.executable = str(self.executable)

    def test_process_receives_exact_arguments_json_stdin_and_isolated_cwd(self):
        import json
        payload = {'name': 'Title $(no-shell)', 'body': 'Line one\nLine two'}
        with mock.patch.dict(MODULE.os.environ, {'GH_DEBUG': 'api', 'GH_HOST': 'other.example'}):
            record = self.publisher._api('PATCH', self.publisher.api_path + '/releases/1', payload)
        self.assertEqual(payload, json.loads(record['input']))
        self.assertEqual('github.com', record['host'])
        self.assertEqual(tempfile.gettempdir(), record['cwd'])
        self.assertEqual('1', record['prompt'])
        self.assertIsNone(record['debug'])
        self.assertIn('--input', record['args'])
        with self.assertRaisesRegex(MODULE.GitError, '其他发布目标'):
            self.publisher._api('POST', 'repos/other/project/releases', payload)

    def test_missing_refs_in_empty_repository_allow_http_409_only_for_ref_reads(self):
        with mock.patch.object(self.publisher, '_run', return_value=(1, b'', b'HTTP 409 repository empty')):
            self.assertIsNone(self.publisher._api('GET', self.publisher.api_path + '/git/ref/tags/v1', missing=True))
            with self.assertRaises(MODULE.GitError):
                self.publisher._api('GET', self.publisher.api_path + '/releases/latest', missing=True)

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
