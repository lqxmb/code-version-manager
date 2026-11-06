"""Current-branch sync and explicit deletion, using disposable peers and origins."""
import json
import subprocess
import unittest
from unittest import mock

import test_workflow as workflow
from test_backend import MODULE


class ScopedFixture:
    setUp = workflow.UnifiedWorkflowTest.setUp
    git = workflow.UnifiedWorkflowTest.git
    refs = workflow.UnifiedWorkflowTest.refs
    peer = workflow.UnifiedWorkflowTest.peer
    publish = workflow.UnifiedWorkflowTest.publish

    def seed(self, name='retreat_teb'):
        self.repo.sync_remote()
        self.repo.create_branch(name)
        self.publish(self.repo, f'{name}/v0.0.3-beta.0.0.1', name + '\n')
        self.repo.sync_remote()
        return name

    def status(self, repo=None, scope='current'):
        repo = repo or self.repo
        snapshot = repo.sync_snapshot(scope=scope)
        return snapshot, repo.remote_sync_status(snapshot)

    def pull(self, repo=None, scope='current'):
        repo = repo or self.repo
        snapshot, status = self.status(repo, scope)
        self.assertTrue(status.can_sync_local, status.local_message)
        return repo.sync_local_from_remote(snapshot, status)


class ScopedSyncTest(ScopedFixture, unittest.TestCase):
    def test_two_authors_upload_different_branches_after_confirmation(self):
        self.seed()
        self.repo.switch_branch('main')
        self.repo.create_branch('start_matching')
        self.publish(self.repo, 'start_matching/v0.0.3-beta.0.0.1', 'matching\n')
        self.repo.sync_remote()
        peer = self.peer()
        peer.switch_branch('start_matching', source='remote')
        self.repo.switch_branch('retreat_teb')
        self.publish(self.repo, 'retreat_teb/v0.0.3-beta.0.0.2', 'retreat two\n')
        snapshot, status = self.status()
        self.publish(peer, 'start_matching/v0.0.3-beta.0.0.2', 'matching two\n')
        peer.sync_remote()
        other_before = self.repo._scope_refs(self.refs(self.remote), 'start_matching')
        local_other = self.repo._scope_refs(self.repo._local_refs(), 'start_matching')
        self.repo.sync_remote_from_local(snapshot, status)
        self.assertEqual(other_before, self.repo._scope_refs(self.refs(self.remote), 'start_matching'))
        self.assertEqual(local_other, self.repo._scope_refs(self.repo._local_refs(), 'start_matching'))
        self.assertNotIn('refs/heads/retreat_teb', peer._local_refs())
        self.assertEqual('v0.0.3', self.repo.branch_base())

    def test_main_sync_preserves_remote_branch_never_downloaded(self):
        self.seed()
        self.repo.switch_branch('main')
        self.repo.delete_branch('retreat_teb')
        remote_branch = self.repo._scope_refs(self.refs(self.remote), 'retreat_teb')
        self.publish(self.repo, 'v0.0.4', 'main four\n')
        self.repo.sync_remote()
        self.assertEqual(remote_branch, self.repo._scope_refs(self.refs(self.remote), 'retreat_teb'))
        self.assertNotIn('refs/heads/retreat_teb', self.repo._local_refs())
        snapshot, status = self.status()
        self.assertEqual('synced', status.state)
        self.assertFalse(status.can_sync_remote)
        self.assertIn('refs/heads/retreat_teb', dict(status.remote_refs))
        self.assertFalse(any(self.repo._owned_ref(ref, 'retreat_teb') for ref, _ in snapshot.refs))

    def test_pull_all_beta_tags_preserves_other_refs_configs_and_local_changes(self):
        self.seed('feature/camera')
        peer = self.peer()
        peer.switch_branch('feature/camera', source='remote')
        self.publish(peer, 'feature/camera/v0.0.3-beta.0.0.2', 'camera two\n')
        self.publish(peer, 'feature/camera/v0.0.3-beta.0.0.3', 'camera three\n')
        peer.sync_remote()
        self.repo.run(['branch', 'unrelated', 'main'])
        self.repo.run(['config', 'branch.unrelated.codeVersionManagerBase', 'v0.0.3'])
        self.repo.run(['update-ref', 'refs/remotes/origin/unrelated', 'main'])
        other = {ref: oid for ref, oid in self.repo._local_refs().items()
                 if ref != 'refs/heads/feature/camera' and not self.repo._owned_ref(ref, 'feature/camera')}
        (self.path / 'shared.txt').write_text('local staged\n')
        self.repo.run(['add', 'shared.txt'])
        (self.path / 'shared.txt').write_text('local unstaged\n')
        (self.path / 'new.txt').write_text('untracked\n')
        self.pull()
        self.assertEqual(other, {ref: oid for ref, oid in self.repo._local_refs().items()
                                if ref != 'refs/heads/feature/camera' and not self.repo._owned_ref(ref, 'feature/camera')})
        self.assertEqual('v0.0.3', self.repo.run(['config', '--get', 'branch.unrelated.codeVersionManagerBase']).strip())
        self.assertTrue(self.repo.run(['rev-parse', 'refs/remotes/origin/unrelated']).strip())
        self.assertEqual('feature/camera/v0.0.3-beta.0.0.3\n', (self.path / 'VERSION').read_text())
        stash = self.repo.last_local_sync_stash_ref
        self.assertEqual('local staged\n', self.repo.run(['show', f'{stash}^2:shared.txt']))
        self.assertEqual('local unstaged\n', self.repo.run(['show', f'{stash}:shared.txt']))
        self.assertEqual('untracked\n', self.repo.run(['show', f'{stash}^3:new.txt']))

    def test_remote_deleted_current_branch_returns_to_unchanged_local_main(self):
        self.seed()
        main = self.repo._local_refs()['refs/heads/main']
        self.git(self.remote, 'update-ref', 'refs/heads/main', self.repo.run(['rev-parse', 'HEAD']).strip())
        self.git(self.remote, 'update-ref', '-d', 'refs/heads/retreat_teb')
        (self.path / 'untracked.txt').write_text('keep me\n')
        snapshot, status = self.status()
        self.assertEqual({item.ref for item in status.local_changes},
                         {item.difference.ref for item in status.local_comparisons})
        message = self.repo.sync_local_from_remote(snapshot, status)
        self.assertIn('转到 main', message)
        self.assertEqual('main', self.repo.current_branch())
        self.assertEqual(main, self.repo._local_refs()['refs/heads/main'])
        self.assertFalse(self.repo._scope_refs(self.repo._local_refs(), 'retreat_teb'))
        self.assertEqual('0.0.3\n', (self.path / 'VERSION').read_text())
        self.repo.restore_local_sync_changes()
        self.assertEqual('keep me\n', (self.path / 'untracked.txt').read_text())

    def test_baseline_annotation_conflict_blocks_until_main_is_processed(self):
        self.seed()
        peer = self.peer()
        peer.run(['tag', '-f', '-a', 'v0.0.3', '-m', 'new formal annotation', 'main'])
        peer.sync_remote()
        snapshot, status = self.status()
        self.assertEqual('blocked', status.state)
        self.assertIn('先处理 main', status.remote_message)
        self.assertFalse(status.can_sync_local)
        self.assertFalse(status.can_sync_remote)
        self.assertIn('refs/heads/retreat_teb', dict(status.remote_refs))
        self.repo.switch_branch('main')
        self.pull()
        self.repo.switch_branch('retreat_teb')
        self.assertEqual('synced', self.status()[1].state)

    def test_new_development_branch_requires_remote_formal_baseline(self):
        self.repo.create_branch('new-feature')
        snapshot, status = self.status()
        self.assertFalse(status.can_sync_remote)
        self.assertIn('main', status.remote_message)
        self.assertEqual({}, self.refs(self.remote))
        self.repo.switch_branch('main')
        self.repo.sync_remote()
        self.repo.switch_branch('new-feature')
        self.repo.sync_remote()
        self.assertIn('refs/heads/new-feature', self.refs(self.remote))

    def test_remote_import_requires_local_baseline_and_does_not_import_other_tags(self):
        self.seed()
        peer = self.peer()
        peer.run(['tag', '-d', 'v0.0.3'])
        before = peer._local_refs()
        with self.assertRaisesRegex(MODULE.GitError, '先处理 main'):
            peer.switch_branch('retreat_teb', source='remote')
        self.assertEqual(before, peer._local_refs())
        self.pull(peer)
        peer.switch_branch('retreat_teb', source='remote')
        self.assertEqual('v0.0.3', peer.branch_base())

    def test_in_scope_new_local_or_remote_tag_invalidates_confirmation(self):
        self.seed()
        self.publish(self.repo, 'retreat_teb/v0.0.3-beta.0.0.2', 'two\n')
        snapshot, status = self.status()
        before = self.refs(self.remote)
        tag = MODULE.version_tag_name('retreat_teb', 'v0.0.3-beta.0.0.3')
        self.repo.run(['tag', tag])
        with self.assertRaisesRegex(MODULE.GitError, '已变化'):
            self.repo.sync_remote_from_local(snapshot, status)
        self.assertEqual(before, self.refs(self.remote))
        self.repo.run(['tag', '-d', tag])
        snapshot, status = self.status()
        self.git(self.remote, 'update-ref', f'refs/tags/{tag}', before['refs/heads/retreat_teb'])
        with self.assertRaisesRegex(MODULE.GitError, '远程.*已变化'):
            self.repo.sync_remote_from_local(snapshot, status)

    def test_baseline_changes_after_confirmation_stop_branch_upload(self):
        self.seed()
        self.publish(self.repo, 'retreat_teb/v0.0.3-beta.0.0.2', 'local two\n')
        snapshot, status = self.status()
        branch_before = self.repo._scope_refs(self.refs(self.remote), 'retreat_teb')
        self.git(self.remote, 'tag', '-d', 'v0.0.3')
        with self.assertRaisesRegex(MODULE.GitError, '先处理 main'):
            self.repo.sync_remote_from_local(snapshot, status)
        self.assertEqual(branch_before, self.repo._scope_refs(self.refs(self.remote), 'retreat_teb'))

    def test_main_advancement_with_same_fixed_baseline_does_not_block_branch_upload(self):
        self.seed()
        peer = self.peer()
        self.publish(self.repo, 'retreat_teb/v0.0.3-beta.0.0.2', 'two\n')
        snapshot, status = self.status()
        local_main = self.repo._local_refs()['refs/heads/main']
        self.publish(peer, 'v0.0.4', 'new main\n')
        peer.sync_remote()
        remote_main = self.refs(self.remote)['refs/heads/main']
        self.repo.sync_remote_from_local(snapshot, status)
        self.assertEqual(local_main, self.repo._local_refs()['refs/heads/main'])
        self.assertEqual(remote_main, self.refs(self.remote)['refs/heads/main'])
        self.assertEqual('v0.0.3', self.repo.branch_base())

    def test_missing_local_baseline_and_multiple_beta_bases_block_sync(self):
        self.seed()
        original = self.repo._local_refs()['refs/tags/v0.0.3']
        self.repo.run(['tag', '-d', 'v0.0.3'])
        self.assertEqual('blocked', self.status()[1].state)
        self.repo.run(['update-ref', 'refs/tags/v0.0.3', original])
        self.repo.run(['tag', MODULE.version_tag_name('retreat_teb', 'v0.0.4-beta.1')])
        status = self.status()[1]
        self.assertFalse(status.can_sync_local)
        self.assertFalse(status.can_sync_remote)
        self.assertIn('多套', status.message)

    def test_pull_ignores_other_branch_change_after_confirmation(self):
        self.seed()
        peer = self.peer()
        peer.switch_branch('retreat_teb', source='remote')
        self.publish(peer, 'retreat_teb/v0.0.3-beta.0.0.2', 'peer two\n')
        peer.sync_remote()
        snapshot, status = self.status()
        self.repo.run(['branch', 'local-extra', 'main'])
        self.git(self.remote, 'update-ref', 'refs/heads/remote-extra', self.refs(self.remote)['refs/heads/main'])
        self.repo.sync_local_from_remote(snapshot, status)
        self.assertIn('refs/heads/local-extra', self.repo._local_refs())
        self.assertNotIn('refs/heads/remote-extra', self.repo._local_refs())
        self.assertIn('refs/heads/remote-extra', self.refs(self.remote))

    def test_branch_divergence_can_be_overwritten_and_beta_tags_deleted(self):
        self.seed()
        peer = self.peer()
        peer.switch_branch('retreat_teb', source='remote')
        self.publish(peer, 'retreat_teb/v0.0.3-beta.0.0.2', 'peer diverged\n')
        peer.sync_remote()
        self.publish(self.repo, 'retreat_teb/v0.0.3-beta.0.0.3', 'local diverged\n')
        snapshot, status = self.status()
        self.assertTrue(any(item.relationship == 'diverged' for item in status.comparisons))
        self.repo.sync_remote_from_local(snapshot, status)
        self.assertEqual(self.repo._scope_refs(self.repo._local_refs(), 'retreat_teb'),
                         self.repo._scope_refs(self.refs(self.remote), 'retreat_teb'))
        self.assertNotIn('refs/tags/' + MODULE.version_tag_name('retreat_teb', 'v0.0.3-beta.0.0.2'), self.refs(self.remote))
        # A later download may also explicitly rewind the branch.
        self.repo.run(['commit', '--allow-empty', '-m', 'unpublished local commit'])
        self.pull()
        self.assertEqual(self.refs(self.remote)['refs/heads/retreat_teb'], self.repo.run(['rev-parse', 'HEAD']).strip())

    def test_precise_branch_lease_is_atomic_with_beta_tags(self):
        self.seed()
        self.publish(self.repo, 'retreat_teb/v0.0.3-beta.0.0.2', 'local two\n')
        snapshot, status = self.status()
        old = self.refs(self.remote)
        original = self.repo._run_remote
        def race(args, url):
            if args[0] == 'push' and '--dry-run' not in args:
                self.git(self.remote, 'update-ref', 'refs/heads/retreat_teb', old['refs/heads/main'])
                self.assertFalse(any(arg.startswith('+') for arg in args))
            return original(args, url)
        with mock.patch.object(self.repo, '_run_remote', side_effect=race):
            with self.assertRaises(MODULE.GitError):
                self.repo.sync_remote_from_local(snapshot, status)
        self.assertNotIn('refs/tags/' + MODULE.version_tag_name('retreat_teb', 'v0.0.3-beta.0.0.2'), self.refs(self.remote))

    def test_advanced_scope_still_deletes_unrelated_refs(self):
        self.seed()
        self.repo.switch_branch('main')
        self.repo.delete_branch('retreat_teb')
        self.repo.sync_remote(scope='all')
        self.assertEqual(self.repo._local_refs(), self.refs(self.remote))


class BranchDeletionTest(ScopedFixture, unittest.TestCase):
    def test_local_only_then_origin_only_deletion_preserves_dirty_workspace(self):
        self.seed()
        self.repo.switch_branch('main')
        (self.path / 'shared.txt').write_text('staged\n')
        self.repo.run(['add', 'shared.txt'])
        (self.path / 'shared.txt').write_text('unstaged\n')
        (self.path / 'new.txt').write_text('untracked\n')
        before = self.repo.run(['status', '--porcelain=v1', '-z'])
        remote = self.refs(self.remote)
        result = self.repo.delete_branch('retreat_teb')
        self.assertTrue(result.local_deleted)
        self.assertEqual('not_requested', result.remote_state)
        self.assertEqual(remote, self.refs(self.remote))
        choices = MODULE.combine_branch_choices(self.repo.local_branches(), self.repo.remote_branches())
        self.assertEqual('retreat_teb（origin）', next(c.label for c in choices if c.name == 'retreat_teb'))
        # A new repository instance represents reopening the application.
        reopened = MODULE.GitRepository(self.path)
        plan = reopened.plan_branch_deletion('retreat_teb')
        result = reopened.delete_branch('retreat_teb', expected=plan, delete_local=False, delete_remote=True)
        self.assertFalse(result.local_deleted)
        self.assertEqual('deleted', result.remote_state)
        self.assertFalse(self.repo._scope_refs(self.refs(self.remote), 'retreat_teb'))
        self.assertEqual(before, self.repo.run(['status', '--porcelain=v1', '-z']))
        self.assertEqual('staged\n', self.repo.run(['show', ':shared.txt']))
        self.assertEqual('unstaged\n', (self.path / 'shared.txt').read_text())

    def test_remote_deletion_does_not_apply_origin_fetch_mapping_to_local_refs(self):
        self.seed()
        self.repo.run(['config', '--replace-all', 'remote.origin.fetch', '+refs/*:refs/*'])
        before = self.repo._local_refs()
        result = self.repo.delete_branch('retreat_teb', delete_local=False, delete_remote=True)
        self.assertEqual('deleted', result.remote_state)
        self.assertEqual(before, self.repo._local_refs())
        result = self.repo.delete_branch('retreat_teb')
        self.assertTrue(result.local_deleted)

    def test_both_ends_delete_has_local_and_remote_recovery(self):
        self.seed('feature/camera')
        before = self.repo._scope_refs(self.repo._local_refs(), 'feature/camera')
        result = self.repo.delete_branch('feature/camera', delete_remote=True)
        self.assertEqual('main', self.repo.current_branch())
        self.assertTrue(result.local_deleted)
        self.assertEqual('deleted', result.remote_state)
        self.assertFalse(self.repo._scope_refs(self.refs(self.remote), 'feature/camera'))
        self.assertFalse(self.repo._scope_refs(self.repo._local_refs(), 'feature/camera'))
        for ref, oid in before.items():
            suffix = ref[len('refs/'):]
            self.assertEqual(oid, self.repo.run(['rev-parse', result.recovery + '/before/' + suffix]).strip())
            self.assertEqual(oid, self.repo.run(['rev-parse', result.recovery + '/remote-before/' + suffix]).strip())
        record = next((self.repo.metadata_path / 'recovery').rglob('deletion.json'))
        self.assertTrue(json.loads(record.read_text())['local_deleted'])

    def test_offline_local_deletion_is_allowed(self):
        self.seed()
        with mock.patch.object(self.repo, '_remote_refs', side_effect=MODULE.GitError('offline')):
            result = self.repo.delete_branch('retreat_teb')
        self.assertTrue(result.local_deleted)
        self.assertIn('refs/heads/retreat_teb', self.refs(self.remote))

    def test_permission_rejection_preserves_local_branch_and_tags(self):
        self.seed()
        before, remote = self.repo._local_refs(), self.refs(self.remote)
        plan = self.repo.plan_branch_deletion('retreat_teb')
        original = self.repo._run_remote
        def denied(args, url):
            if args[0] == 'push':
                raise MODULE.GitError('permission denied')
            return original(args, url)
        with mock.patch.object(self.repo, '_run_remote', side_effect=denied):
            with self.assertRaisesRegex(MODULE.GitError, '本地分支已保留'):
                self.repo.delete_branch('retreat_teb', expected=plan, delete_remote=True)
        self.assertEqual(before, self.repo._local_refs())
        self.assertEqual(remote, self.refs(self.remote))

    def test_current_scope_changes_stop_deletion_but_unrelated_changes_do_not(self):
        self.seed()
        plan = self.repo.plan_branch_deletion('retreat_teb')
        self.repo.run(['branch', 'other-local', 'main'])
        self.git(self.remote, 'update-ref', 'refs/heads/other-remote', self.refs(self.remote)['refs/heads/main'])
        result = self.repo.delete_branch('retreat_teb', expected=plan, delete_remote=True)
        self.assertTrue(result.local_deleted)
        self.assertIn('refs/heads/other-local', self.repo._local_refs())
        self.assertIn('refs/heads/other-remote', self.refs(self.remote))
        self.seed()
        plan = self.repo.plan_branch_deletion('retreat_teb')
        self.git(self.remote, 'update-ref', 'refs/heads/retreat_teb', self.refs(self.remote)['refs/heads/main'])
        with self.assertRaisesRegex(MODULE.GitError, '远程.*已变化'):
            self.repo.delete_branch('retreat_teb', expected=plan, delete_remote=True)
        self.assertIn('refs/heads/retreat_teb', self.repo._local_refs())

    def test_dirty_current_branch_prevents_any_remote_deletion(self):
        self.seed()
        before = self.refs(self.remote)
        (self.path / 'new.txt').write_text('untracked\n')
        with self.assertRaisesRegex(MODULE.GitError, '工作区'):
            self.repo.delete_branch('retreat_teb', delete_remote=True)
        self.assertEqual(before, self.refs(self.remote))

    def test_local_edits_during_remote_delete_stop_local_cleanup_and_report_partial_result(self):
        self.seed()
        plan = self.repo.plan_branch_deletion('retreat_teb')
        original = self.repo._run_remote
        def changed(args, url):
            result = original(args, url)
            if args[0] == 'push':
                (self.path / 'new-edit.txt').write_text('keep local edit\n')
            return result
        with mock.patch.object(self.repo, '_run_remote', side_effect=changed):
            with self.assertRaisesRegex(MODULE.GitError, '远程状态：deleted'):
                self.repo.delete_branch('retreat_teb', expected=plan, delete_remote=True)
        self.assertIn('refs/heads/retreat_teb', self.repo._local_refs())
        self.assertNotIn('refs/heads/retreat_teb', self.refs(self.remote))
        self.assertEqual('keep local edit\n', (self.path / 'new-edit.txt').read_text())

    def test_server_without_atomic_support_preserves_both_ends(self):
        self.seed()
        before, remote = self.repo._local_refs(), self.refs(self.remote)
        self.git(self.remote, 'config', 'receive.advertiseAtomic', 'false')
        with self.assertRaises(MODULE.GitError):
            self.repo.delete_branch('retreat_teb', delete_remote=True)
        self.assertEqual(before, self.repo._local_refs())
        self.assertEqual(remote, self.refs(self.remote))

    def test_delete_lease_rejects_race_without_deleting_beta(self):
        self.seed()
        plan = self.repo.plan_branch_deletion('retreat_teb')
        remote = self.refs(self.remote)
        original = self.repo._run_remote
        def race(args, url):
            if args[0] == 'push':
                self.git(self.remote, 'update-ref', 'refs/heads/retreat_teb', remote['refs/heads/main'])
            return original(args, url)
        with mock.patch.object(self.repo, '_run_remote', side_effect=race):
            with self.assertRaises(MODULE.GitError):
                self.repo.delete_branch('retreat_teb', expected=plan, delete_remote=True)
        self.assertIn('refs/heads/retreat_teb', self.repo._local_refs())
        beta = 'refs/tags/' + MODULE.version_tag_name('retreat_teb', 'v0.0.3-beta.0.0.1')
        self.assertEqual(remote[beta], self.refs(self.remote)[beta])

    def test_timeout_in_each_remote_phase_continues_local_and_can_retry(self):
        for phase in ('recheck', 'backup', 'push_before_accept', 'push_after_accept', 'verify'):
            with self.subTest(phase=phase):
                name = 'timeout-' + phase
                self.seed(name)
                plan = self.repo.plan_branch_deletion(name)
                original = self.repo._run_remote
                sent = False
                def timeout(args, url):
                    nonlocal sent
                    fail = ((phase == 'recheck' and args[0] == 'ls-remote')
                            or (phase == 'backup' and args[0] == 'fetch')
                            or (phase == 'push_before_accept' and args[0] == 'push')
                            or (phase == 'verify' and sent and args[0] == 'ls-remote'))
                    if fail:
                        raise MODULE.RemoteTimeoutError('injected timeout', args[0])
                    result = original(args, url)
                    if args[0] == 'push':
                        sent = True
                        if phase == 'push_after_accept':
                            raise MODULE.RemoteTimeoutError('accepted then timeout', 'push')
                    return result
                with mock.patch.object(self.repo, '_run_remote', side_effect=timeout):
                    result = self.repo.delete_branch(name, expected=plan, delete_remote=True)
                self.assertTrue(result.local_deleted)
                self.assertEqual('main', self.repo.current_branch())
                self.assertEqual('not_sent' if phase in ('recheck', 'backup') else 'unknown', result.remote_state)
                reopened = MODULE.GitRepository(self.path)
                branches = {b.name for b in reopened.remote_branches()}
                if phase in ('push_after_accept', 'verify'):
                    self.assertNotIn(name, branches)
                else:
                    self.assertIn(name, branches)
                    result = reopened.delete_branch(name, delete_local=False, delete_remote=True)
                    self.assertEqual('deleted', result.remote_state)
                self.assertFalse(self.repo._scope_refs(self.refs(self.remote), name))

    def test_initial_remote_check_timeout_allows_local_delete_after_both_selected(self):
        self.seed()
        with mock.patch.object(self.repo, '_remote_refs', side_effect=MODULE.RemoteTimeoutError('offline', 'check')):
            result = self.repo.delete_branch('retreat_teb', delete_remote=True)
        self.assertTrue(result.local_deleted)
        self.assertEqual('not_sent', result.remote_state)

    def test_main_cannot_be_deleted_from_either_side(self):
        self.repo.sync_remote()
        for local, remote in ((True, False), (False, True), (True, True)):
            with self.subTest(local=local, remote=remote):
                with self.assertRaisesRegex(MODULE.GitError, 'main 分支不能删除'):
                    self.repo.delete_branch('main', delete_local=local, delete_remote=remote)


class ScopedSyncUITest(unittest.TestCase):
    def test_advanced_entry_checks_all_before_presenting_direction(self):
        window = mock.Mock()
        window.busy = False
        snapshot = MODULE.RemoteSyncSnapshot('origin', 'refs/heads/main', (), scope='all')
        window.repository.sync_snapshot.return_value = snapshot
        window.repository.remote_sync_status.return_value = mock.sentinel.status
        MODULE.VersionManagerWindow.on_advanced_sync(window, None)
        window.repository.sync_snapshot.assert_not_called()
        operation = window.run_background.call_args.args[1]
        self.assertEqual((snapshot, mock.sentinel.status), operation())
        window.repository.sync_snapshot.assert_called_once_with(scope='all')
        window._confirm_sync_direction.assert_not_called()

    def test_advanced_direction_then_full_confirmation_then_background_execution(self):
        window = mock.Mock()
        repo = window.repository
        snapshot = MODULE.RemoteSyncSnapshot('origin', 'refs/heads/main', (), scope='all')
        status = MODULE.RemoteSyncStatus('pending', 'all changes', can_sync_local=True,
                                        can_sync_remote=True, snapshot=snapshot)
        window._confirm_sync_direction.return_value = True
        with mock.patch.object(MODULE.Gtk, 'Dialog') as factory:
            factory.return_value.run.return_value = 2
            MODULE.VersionManagerWindow._background_advanced_ready(window, repo, (snapshot, status))
        window._confirm_sync_direction.assert_called_once_with('remote', status)
        window._set_remote_status.assert_not_called()
        operation = window.run_background.call_args.args[1]
        repo.sync_remote_from_local.assert_not_called()
        operation()
        repo.sync_remote_from_local.assert_called_once_with(snapshot, status)

    def test_cancel_advanced_confirmation_does_not_execute(self):
        window = mock.Mock()
        status = MODULE.RemoteSyncStatus('pending', 'changes', can_sync_local=True)
        window._confirm_sync_direction.return_value = False
        with mock.patch.object(MODULE.Gtk, 'Dialog') as factory:
            factory.return_value.run.return_value = 1
            MODULE.VersionManagerWindow._background_advanced_ready(window, window.repository, (mock.sentinel.snapshot, status))
        window.run_background.assert_not_called()
        window.refresh.assert_called_once()

    def test_delete_remote_option_defaults_to_local_only(self):
        window = mock.Mock()
        plan = MODULE.BranchDeletionPlan('retreat_teb', (('refs/heads/retreat_teb', 'a' * 40),),
                                         'origin', (('refs/heads/retreat_teb', 'a' * 40),))
        with mock.patch.object(MODULE.Gtk, 'Dialog') as factory, \
                mock.patch.object(MODULE.Gtk, 'Box'), mock.patch.object(MODULE.Gtk, 'CheckButton') as check:
            factory.return_value.run.return_value = MODULE.Gtk.ResponseType.OK
            check.return_value.get_active.return_value = False
            MODULE.VersionManagerWindow._background_deletion_ready(window, window.repository, plan)
        check.return_value.set_active.assert_called_once_with(False)
        operation = window.run_background.call_args.args[1]
        operation()
        window.repository.delete_branch.assert_called_once_with('retreat_teb', expected=plan,
                                                               delete_local=True, delete_remote=False)

    def test_origin_only_selection_requests_remote_only(self):
        window = mock.Mock()
        plan = MODULE.BranchDeletionPlan('feature/camera', (), 'origin',
                                         (('refs/heads/feature/camera', 'a' * 40),))
        with mock.patch.object(MODULE.Gtk, 'Dialog') as factory, \
                mock.patch.object(MODULE.Gtk, 'Box'), mock.patch.object(MODULE.Gtk, 'CheckButton'):
            factory.return_value.run.return_value = MODULE.Gtk.ResponseType.OK
            MODULE.VersionManagerWindow._background_deletion_ready(window, window.repository, plan)
        window.run_background.call_args.args[1]()
        window.repository.delete_branch.assert_called_once_with('feature/camera', expected=plan,
                                                               delete_local=False, delete_remote=True)

    def test_timeout_result_is_shown_explicitly(self):
        window = mock.Mock()
        result = MODULE.BranchDeletionResult('feature', True, 'unknown', 'recovery', '远程结果待确认')
        with mock.patch.object(MODULE.Gtk, 'MessageDialog') as factory:
            MODULE.VersionManagerWindow._background_deletion_complete(window, window.repository, result)
        factory.return_value.format_secondary_text.assert_called_once_with(result.message)
        factory.return_value.run.assert_called_once()
        window.refresh.assert_called_once()

    def test_background_dispatch_discards_previous_generation_and_repository(self):
        window = mock.Mock()
        window.busy = False
        window.branch_generation = 0
        with mock.patch.object(MODULE.GLib, 'timeout_add_seconds'), \
                mock.patch.object(MODULE.GLib, 'idle_add') as idle, mock.patch.object(MODULE.threading, 'Thread') as thread:
            MODULE.VersionManagerWindow.run_background(window, 'check', lambda: 'done', 'done')
            thread.call_args.kwargs['target']()
        deliver, callback, *args = idle.call_args.args
        window.branch_generation += 1
        deliver(callback, *args)
        window._background_complete.assert_not_called()
        window.branch_generation -= 1
        window.repository = mock.sentinel.new_repository
        deliver(callback, *args)
        window._background_complete.assert_not_called()

    def test_old_repository_preview_and_deletion_results_are_discarded(self):
        for callback, result in ((MODULE.VersionManagerWindow._background_advanced_ready, mock.sentinel.result),
                                 (MODULE.VersionManagerWindow._background_deletion_ready, mock.sentinel.plan),
                                 (MODULE.VersionManagerWindow._background_deletion_complete, mock.sentinel.deleted)):
            with self.subTest(callback=callback.__name__):
                window = mock.Mock()
                callback(window, mock.sentinel.old_repository, result)
                window.run_background.assert_not_called()
                window.refresh.assert_not_called()

    def test_blocked_baseline_keeps_successfully_read_origin_choices(self):
        window = mock.Mock()
        status = MODULE.RemoteSyncStatus('blocked', '先处理 main',
                                        remote_refs=(('refs/heads/feature', 'a' * 40),),
                                        snapshot=MODULE.RemoteSyncSnapshot('origin', 'refs/heads/feature', ()))
        MODULE.VersionManagerWindow._set_remote_status(window, status)
        self.assertEqual([MODULE.BranchInfo('feature', 'remote', 'a' * 40)], window.remote_branch_choices)
