"""Whole repository workflow; every writable repository is temporary."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_backend import MODULE


class UnifiedWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'workspace'
        self.path.mkdir()
        (self.path / 'shared.txt').write_text('base\n', encoding='utf-8')
        self.repo = MODULE.initialize_repository(self.path, 'v0.0.3', 'base', ['shared.txt'])
        self.remote = self.root / 'origin.git'
        self.git(self.root, 'init', '--bare', '-q', str(self.remote))
        self.repo.configure_remote(str(self.remote), 'Test', 'test@example.com')

    def git(self, path, *args):
        return subprocess.run(['git', '-C', str(path), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()

    def refs(self, path):
        output = self.git(path, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads/', 'refs/tags/')
        return {ref: oid for ref, oid in (line.split() for line in output.splitlines()) if MODULE.GitRepository._managed_ref(ref)}

    def status(self, repo=None):
        repo = repo or self.repo
        snap = repo.sync_snapshot()
        return snap, repo.remote_sync_status(snap)

    def peer(self):
        path = self.root / 'peer'
        self.git(self.root, 'clone', '--branch', 'main', '-q', str(self.remote), str(path))
        repo = MODULE.GitRepository(path)
        repo.configure_remote(str(self.remote), 'Peer', 'peer@example.com')
        return repo

    def publish(self, repo, version, content, path='shared.txt'):
        (repo.path / path).write_text(content, encoding='utf-8')
        repo.save_version(version, version, [path])

    def test_full_example_merges_without_publication_then_cleans_and_mirrors(self):
        self.repo.create_branch('branch1', 'v0.0.3')
        for n in range(1, 4):
            version = f'branch1/v0.0.3-beta.0.0.{n}'
            self.publish(self.repo, version, f'branch content {n}\n')
            (self.path / f'feature{n}.txt').write_text(f'feature {n}\n', encoding='utf-8')
            # Extra tracked changes must also be part of the published beta.
            self.repo.run(['add', f'feature{n}.txt'])
            self.repo.run(['commit', '--amend', '--no-edit', '-q'])
            self.repo.run(['tag', '-f', '-a', MODULE.canonical_version(version), '-m', version])
        self.assertEqual('0.0.3\n', (self.path / 'VERSION').read_text())
        self.repo.switch_branch('main')
        for n in range(4, 7):
            self.publish(self.repo, f'v0.0.{n}', f'main content {n}\n')
        (self.path / 'main-only.txt').write_text('keep main work\n', encoding='utf-8')
        self.repo.run(['add', 'main-only.txt'])
        self.repo.run(['commit', '--amend', '--no-edit', '-q'])
        self.repo.run(['tag', '-f', '-a', 'v0.0.6', '-m', 'six'])
        self.repo.sync_remote()
        self.repo.switch_branch('branch1')
        self.assertEqual('branches/branch1/v0.0.3-beta.0.0.4', self.repo.next_version())
        self.repo.switch_branch('main')
        plan = self.repo.plan_merge('branch1')
        self.assertEqual('branches/branch1/v0.0.3-beta.0.0.3', plan.source_tag)
        self.assertEqual('v0.0.6', plan.target_version)
        before_tags = {r: o for r, o in self.refs(self.path).items() if r.startswith('refs/tags/')}
        before_remote = self.refs(self.remote)
        with mock.patch.object(self.repo, '_run_remote', side_effect=AssertionError('merge must be local')):
            self.repo.merge_to_main('branch1', expected=plan)
            self.assertTrue(self.repo.merge_state().active)
            self.assertIn('shared.txt', self.repo.merge_state().conflicts)
            (self.path / 'shared.txt').write_text('main content 6\nbranch content 3\n', encoding='utf-8')
            self.repo.resolve_merge_file('shared.txt')
            message = self.repo.complete_merge()
        self.assertIn('尚未发布', message)
        self.assertEqual(before_tags, {r: o for r, o in self.refs(self.path).items() if r.startswith('refs/tags/')})
        self.assertEqual(before_remote, self.refs(self.remote))
        self.assertEqual('0.0.6\n', (self.path / 'VERSION').read_text())
        self.assertEqual(2, len(self.repo.run(['rev-list', '--parents', '-n', '1', 'HEAD']).split()) - 1)
        self.assertTrue((self.path / 'main-only.txt').exists())
        for n in range(1, 4):
            self.assertTrue((self.path / f'feature{n}.txt').exists())
        with self.assertRaisesRegex(MODULE.GitError, '发布'):
            self.repo.delete_merged_branch('branch1')
        self.repo.save_version('v0.0.7', 'merged release', ['shared.txt', 'main-only.txt', 'feature1.txt', 'feature2.txt', 'feature3.txt'])
        self.assertEqual('0.0.7\n', (self.path / 'VERSION').read_text())
        deletion = self.repo.plan_branch_deletion('branch1')
        self.repo.delete_merged_branch('branch1', expected=deletion)
        self.assertNotIn('refs/heads/branch1', self.refs(self.path))
        self.assertFalse(any('/branches/branch1/' in ref for ref in self.refs(self.path)))
        self.assertIn('refs/heads/branch1', self.refs(self.remote))
        self.repo.sync_remote()
        self.assertEqual(self.refs(self.path), self.refs(self.remote))
        self.assertTrue(self.repo.run(['for-each-ref', '--format=%(refname)', 'refs/code-version-manager/recovery/']).strip())

    def test_base_recovers_after_import_and_does_not_follow_main(self):
        self.repo.create_branch('feature/camera', 'v0.0.3')
        self.publish(self.repo, 'feature/camera/v0.0.3-beta.0.0.1', 'feature\n')
        self.repo.switch_branch('main')
        self.publish(self.repo, 'v0.0.6', 'main six\n')
        self.repo.sync_remote()
        peer = self.peer()
        snap, status = self.status(peer)
        peer.sync_local_from_remote(snap, status)
        peer.switch_branch('feature/camera')
        self.assertEqual('v0.0.3', peer.branch_base())
        self.assertEqual('branches/feature%2Fcamera/v0.0.3-beta.0.0.2', peer.next_version())
        self.assertEqual('0.0.3\n', (peer.path / 'VERSION').read_text())

    def test_unpublished_branch_base_recovers_from_first_parent(self):
        self.repo.create_branch('feature', 'v0.0.3')
        self.repo.switch_branch('main')
        self.publish(self.repo, 'v0.0.6', 'six\n')
        self.repo.sync_remote()
        peer = self.peer()
        snap, status = self.status(peer)
        peer.sync_local_from_remote(snap, status)
        peer.switch_branch('feature')
        self.assertEqual('branches/feature/v0.0.3-beta.0.0.1', peer.next_version())

    def test_import_without_beta_does_not_guess_between_formal_labels_at_same_commit(self):
        self.repo.run(['tag', 'v0.0.4', 'main'])
        self.repo.create_branch('feature', 'v0.0.3')
        self.assertEqual('v0.0.3', self.repo.branch_base())
        self.repo.switch_branch('main')
        self.repo.sync_remote()
        peer = self.peer()
        snap, status = self.status(peer)
        peer.sync_local_from_remote(snap, status)
        peer.switch_branch('feature')
        with self.assertRaisesRegex(MODULE.GitError, '无法唯一恢复基线'):
            peer.branch_base()

    def test_beta_rejects_changed_version_and_wrong_base(self):
        self.repo.create_branch('branch1', 'v0.0.3')
        before = self.refs(self.path)
        (self.path / 'VERSION').write_text('0.0.3-beta.0.0.1\n', encoding='utf-8')
        with self.assertRaisesRegex(MODULE.GitError, 'VERSION'):
            self.repo.save_version('branch1/v0.0.3-beta.0.0.1', 'bad', ['shared.txt', 'VERSION'])
        self.assertEqual(before, self.refs(self.path))
        self.repo.run(['restore', 'VERSION'])
        with self.assertRaisesRegex(MODULE.GitError, '基线'):
            self.repo.save_version('branch1/v0.0.6-beta.0.0.1', 'bad', ['shared.txt'])

    def test_multiple_legacy_bases_remain_readable_but_cannot_publish(self):
        self.repo.create_branch('branch1', 'v0.0.3')
        for v in ('v0.0.3-beta.1', 'v0.0.4-beta.1'):
            self.repo.run(['tag', '-a', MODULE.version_tag_name('branch1', v), '-m', v])
        self.assertEqual(3, len(self.repo.history()))
        with self.assertRaisesRegex(MODULE.GitError, '多套'):
            self.repo.next_version()
        with self.assertRaisesRegex(MODULE.GitError, '多套'):
            self.repo.plan_merge('branch1')

    def test_merge_refuses_unpublished_commits_and_changed_confirmation(self):
        self.repo.create_branch('branch1')
        self.publish(self.repo, 'branch1/v0.0.3-beta.0.0.1', 'feature\n')
        self.repo.run(['commit', '--allow-empty', '-m', 'unpublished'])
        with self.assertRaisesRegex(MODULE.GitError, '新的 beta'):
            self.repo.plan_merge('branch1')
        self.repo.save_version('branch1/v0.0.3-beta.0.0.2', 'published', ['shared.txt'])
        plan = self.repo.plan_merge('branch1')
        self.repo.run(['tag', 'v0.0.2', 'main'])
        with self.assertRaisesRegex(MODULE.GitError, '已变化'):
            self.repo.merge_to_main('branch1', expected=plan)

    def test_bidirectional_mirror_rewrites_shared_tags_and_deletes_extras(self):
        self.repo.create_branch('branch1')
        self.publish(self.repo, 'branch1/v0.0.3-beta.0.0.1', 'feature\n')
        self.repo.switch_branch('main')
        self.repo.sync_remote()
        peer = self.peer()
        snap, status = self.status(peer)
        peer.sync_local_from_remote(snap, status)
        peer.run(['tag', '-f', '-a', 'v0.0.3', '-m', 'changed shared annotation'])
        peer.run(['branch', 'remote-only'])
        peer.run(['tag', 'external-note'])
        self.git(peer.path, 'push', '-q', 'origin', 'refs/tags/external-note')
        peer.sync_remote()
        self.repo.run(['branch', 'local-only'])
        self.repo.run(['tag', 'local-note'])
        snap, status = self.status()
        self.assertTrue(status.can_sync_local, status.local_message)
        self.assertTrue(status.can_sync_remote, status.remote_message)
        self.repo.sync_local_from_remote(snap, status)
        self.assertEqual(self.refs(self.remote), self.refs(self.path))
        self.assertIn('local-note', self.repo.tags())
        self.assertNotIn('external-note', self.repo.tags())
        self.assertNotIn('refs/heads/local-only', self.refs(self.path))
        self.assertIn('refs/heads/remote-only', self.refs(self.path))
        self.repo.run(['branch', '-d', 'remote-only'])
        self.repo.sync_remote()
        self.assertEqual(self.refs(self.remote), self.refs(self.path))
        self.assertIn('external-note', self.git(self.remote, 'tag').splitlines())

    def test_current_branch_deleted_by_remote_returns_to_main(self):
        self.repo.sync_remote()
        self.repo.create_branch('local-only')
        snap, status = self.status()
        self.repo.sync_local_from_remote(snap, status)
        self.assertEqual('main', self.repo.current_branch())
        self.assertEqual(self.refs(self.remote), self.refs(self.path))
        self.assertEqual('0.0.3\n', (self.path / 'VERSION').read_text())

    def test_dirty_staged_untracked_and_ignored_files_have_recovery(self):
        self.repo.sync_remote()
        peer = self.peer()
        (peer.path / 'collision.txt').write_text('remote file\n', encoding='utf-8')
        peer.save_version('v0.0.4', 'remote', ['shared.txt', 'collision.txt'])
        peer.sync_remote()
        (self.path / '.gitignore').write_text('/collision.txt\n', encoding='utf-8')
        self.repo.run(['add', '.gitignore'])
        (self.path / 'shared.txt').write_text('staged\n', encoding='utf-8')
        self.repo.run(['add', 'shared.txt'])
        (self.path / 'shared.txt').write_text('unstaged\n', encoding='utf-8')
        (self.path / 'untracked.txt').write_text('keep\n', encoding='utf-8')
        (self.path / 'collision.txt').write_text('ignored original\n', encoding='utf-8')
        snap, status = self.status()
        self.repo.sync_local_from_remote(snap, status)
        stash = self.repo.last_local_sync_stash_ref
        self.assertTrue(stash)
        self.assertEqual('staged\n', self.repo.run(['show', f'{stash}^2:shared.txt']))
        self.assertEqual('unstaged\n', self.repo.run(['show', f'{stash}:shared.txt']))
        self.assertEqual('keep\n', self.repo.run(['show', f'{stash}^3:untracked.txt']))
        backups = list((self.repo.metadata_path / 'recovery').rglob('ignored-worktree/collision.txt'))
        self.assertEqual(1, len(backups))
        self.assertEqual('ignored original\n', backups[0].read_text())
        self.assertEqual('remote file\n', (self.path / 'collision.txt').read_text())
        self.assertTrue(list((self.repo.metadata_path / 'recovery').rglob('git-config')))
        self.repo.restore_local_sync_changes()
        self.assertEqual('staged\n', self.repo.run(['show', ':shared.txt']))
        self.assertEqual('unstaged\n', (self.path / 'shared.txt').read_text())
        self.assertTrue((self.path / 'untracked.txt').exists())

    def test_snapshot_changes_cancel_before_writing_refs(self):
        self.repo.sync_remote()
        self.publish(self.repo, 'v0.0.4', 'four\n')
        snap, status = self.status()
        before = self.refs(self.remote)
        self.repo.run(['branch', 'late-change'])
        with self.assertRaisesRegex(MODULE.GitError, '已变化'):
            self.repo.sync_remote_from_local(snap, status)
        self.assertEqual(before, self.refs(self.remote))

    def test_other_worktree_blocks_local_mirror(self):
        self.repo.sync_remote()
        self.repo.run(['branch', 'busy'])
        other = self.root / 'other-worktree'
        self.repo.run(['worktree', 'add', '-q', str(other), 'busy'])
        snap, status = self.status()
        self.assertFalse(status.can_sync_local)
        self.assertIn('另一个工作区', status.local_message)
        self.assertIn('refs/heads/busy', self.refs(self.path))

    def test_atomic_server_rejection_keeps_every_remote_ref(self):
        self.repo.sync_remote()
        self.repo.create_branch('branch1')
        self.publish(self.repo, 'branch1/v0.0.3-beta.0.0.1', 'feature\n')
        self.repo.switch_branch('main')
        self.publish(self.repo, 'v0.0.4', 'four\n')
        self.git(self.remote, 'config', 'receive.advertiseAtomic', 'false')
        before = self.refs(self.remote)
        with self.assertRaises(MODULE.GitError):
            self.repo.sync_remote()
        self.assertEqual(before, self.refs(self.remote))

    def test_missing_main_and_shallow_history_block_mirrors(self):
        self.repo.sync_remote()
        self.repo.run(['branch', '-m', 'main', 'other'])
        snap, status = self.status()
        self.assertFalse(status.can_sync_remote)
        self.assertIn('main', status.remote_message)
        shallow = self.root / 'shallow'
        self.git(self.root, 'clone', '--depth=1', '--branch=main', '-q', self.remote.as_uri(), str(shallow))
        with self.assertRaisesRegex(MODULE.GitError, '浅克隆'):
            MODULE.GitRepository(shallow).sync_snapshot()

    def test_unrelated_histories_can_be_mirrored_after_explicit_confirmation(self):
        self.repo.sync_remote()
        other = self.root / 'other'
        other.mkdir()
        (other / 'different.txt').write_text('different root\n', encoding='utf-8')
        repo = MODULE.initialize_repository(other, 'v2.0.0', 'new root', ['different.txt'])
        repo.configure_remote(str(self.remote), 'Other', 'other@example.com')
        snap, status = self.status(repo)
        self.assertTrue(any(item.relationship == 'unrelated' for item in status.comparisons))
        repo.sync_remote_from_local(snap, status)
        self.assertEqual(self.refs(other), self.refs(self.remote))
        self.assertTrue(repo.run(['for-each-ref', 'refs/code-version-manager/recovery/']).strip())

    def test_failed_local_ref_transaction_preserves_heads_and_branch_identity(self):
        self.repo.sync_remote()
        self.repo.run(['branch', 'local-only'])
        before = self.refs(self.path)
        snap, status = self.status()
        with mock.patch.object(self.repo, '_replace_managed_refs', side_effect=MODULE.GitError('simulated lock failure')):
            with self.assertRaisesRegex(MODULE.GitError, '恢复点'):
                self.repo.sync_local_from_remote(snap, status)
        self.assertEqual(before, self.refs(self.path))
        self.assertEqual('main', self.repo.current_branch())


    def test_precise_lease_rejects_concurrent_remote_change_without_partial_tags(self):
        self.repo.sync_remote()
        peer = self.peer()
        (peer.path / 'peer.txt').write_text('concurrent change\n')
        peer.run(['add', 'peer.txt'])
        peer.run(['commit', '-m', 'concurrent remote head'])
        peer_head = peer.run(['rev-parse', 'HEAD']).strip()
        self.publish(self.repo, 'v0.0.4', 'local four\n')
        snap, status = self.status()
        original = self.repo._run_remote
        def race(args, remote_url):
            if args[0] == 'push' and '--dry-run' not in args:
                self.git(peer.path, 'push', '-q', 'origin', 'main')
            return original(args, remote_url)
        with mock.patch.object(self.repo, '_run_remote', side_effect=race):
            with self.assertRaises(MODULE.GitError):
                self.repo.sync_remote_from_local(snap, status)
        self.assertEqual(peer_head, self.git(self.remote, 'rev-parse', 'main'))
        self.assertNotIn('refs/tags/v0.0.4', self.refs(self.remote))
        self.assertIn('refs/tags/v0.0.4', self.refs(self.path))

    def test_remote_snapshot_change_cancels_before_backup_or_local_refs_change(self):
        self.repo.sync_remote()
        self.repo.run(['branch', 'local-only'])
        snap, status = self.status()
        before = self.refs(self.path)
        self.git(self.remote, 'update-ref', 'refs/heads/late-branch', before['refs/heads/main'])
        with self.assertRaisesRegex(MODULE.GitError, '远程.*已变化'):
            self.repo.sync_local_from_remote(snap, status)
        self.assertEqual(before, self.refs(self.path))
        self.assertEqual('main', self.repo.current_branch())

    def test_real_ref_lock_failure_is_a_batch_failure(self):
        self.repo.sync_remote()
        peer = self.peer()
        self.publish(peer, 'v0.0.4', 'peer four\n')
        peer.run(['branch', 'new-branch'])
        peer.sync_remote()
        before = self.refs(self.path)
        snap, status = self.status()
        lock = self.path / '.git' / 'refs' / 'heads' / 'main.lock'
        lock.touch()
        try:
            with self.assertRaisesRegex(MODULE.GitError, '恢复点'):
                self.repo.sync_local_from_remote(snap, status)
        finally:
            lock.unlink()
        self.assertEqual(before, self.refs(self.path))
        self.assertEqual('main', self.repo.current_branch())
        self.assertEqual('base\n', (self.path / 'shared.txt').read_text())

    def test_empty_local_repository_import_populates_worktree(self):
        self.repo.sync_remote()
        empty = self.root / 'empty'
        empty.mkdir()
        repo = MODULE.create_repository(empty)
        repo.configure_remote(str(self.remote), 'Empty', 'empty@example.com')
        snap, status = self.status(repo)
        self.assertTrue(status.can_sync_local, status.local_message)
        self.assertFalse(status.can_sync_remote)
        repo.sync_local_from_remote(snap, status)
        self.assertEqual(self.refs(self.remote), self.refs(empty))
        self.assertEqual('0.0.3\n', (empty / 'VERSION').read_text())
        self.assertEqual('base\n', (empty / 'shared.txt').read_text())
        self.assertEqual([], repo.status())

    def test_import_invalidates_stale_manifest_for_previously_missing_tag(self):
        self.repo.sync_remote()
        peer = self.peer()
        self.publish(peer, 'v0.0.4', 'four\n')
        peer.sync_remote()
        manifest = self.repo.metadata_path / 'versions' / 'v0.0.4.manifest'
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text('wrong-file.txt\n')
        snap, status = self.status()
        self.repo.sync_local_from_remote(snap, status)
        self.assertFalse(manifest.exists())
        exported = self.root / 'export'
        self.repo.materialize('v0.0.4', exported)
        self.assertEqual('four\n', (exported / 'shared.txt').read_text())
        self.assertTrue(list((self.repo.metadata_path / 'recovery').rglob('v0.0.4.manifest')))

    def test_conflict_completion_cannot_stage_beta_version_or_create_formal_tag(self):
        self.repo.create_branch('branch1')
        self.publish(self.repo, 'branch1/v0.0.3-beta.0.0.1', 'feature conflict\n')
        self.repo.switch_branch('main')
        self.publish(self.repo, 'v0.0.4', 'main conflict\n')
        main = self.repo.run(['rev-parse', 'HEAD']).strip()
        tags = self.repo.tags()
        self.repo.merge_to_main('branch1')
        (self.path / 'shared.txt').write_text('resolved by user\n')
        self.repo.resolve_merge_file('shared.txt')
        (self.path / 'VERSION').write_text('0.0.3-beta.0.0.1\n')
        self.repo.run(['add', 'VERSION'])
        with self.assertRaisesRegex(MODULE.GitError, 'VERSION 必须保留'):
            self.repo.complete_merge()
        self.assertEqual(main, self.repo.run(['rev-parse', 'HEAD']).strip())
        self.assertEqual(tags, self.repo.tags())
        self.repo.run(['restore', '--source=HEAD', '--staged', '--worktree', 'VERSION'])
        self.repo.complete_merge()
        self.assertEqual(tags, self.repo.tags())
        self.assertEqual('0.0.4\n', (self.path / 'VERSION').read_text())


class RepositoryStatusUITest(unittest.TestCase):
    def test_old_background_result_never_reenables_buttons(self):
        window = mock.Mock()
        repo = mock.Mock()
        window.repository = repo
        window.remote_status_generation = 5
        window.busy = False
        MODULE.VersionManagerWindow._remote_status_complete(window, repo, 4, mock.sentinel.snapshot, mock.sentinel.status)
        window._set_remote_status.assert_not_called()
        repo.sync_snapshot.assert_not_called()

    def test_changed_repository_snapshot_rechecks_instead_of_applying_result(self):
        window = mock.Mock()
        repo = mock.Mock()
        window.repository = repo
        window.remote_status_generation = 5
        window.busy = False
        repo.sync_snapshot.return_value = mock.sentinel.new_snapshot
        MODULE.VersionManagerWindow._remote_status_complete(window, repo, 5, mock.sentinel.old_snapshot, mock.sentinel.status)
        window._set_remote_status.assert_not_called()
        window._invalidate_remote_status.assert_called_once()
        window._refresh_remote_status.assert_called_once()

    def test_merge_preview_says_no_publication_and_uses_confirmed_plan(self):
        window = mock.Mock()
        plan = MODULE.BranchMergePlan('branch1', 'branches/branch1/v0.0.3-beta.0.0.3', 'a' * 40, 'v0.0.6', 'b' * 40, ())
        with mock.patch.object(MODULE.Gtk, 'MessageDialog') as factory:
            dialog = factory.return_value
            dialog.run.return_value = MODULE.Gtk.ResponseType.OK
            MODULE.VersionManagerWindow._background_merge_ready(window, plan)
        self.assertIn('不发布、不创建标签', dialog.format_secondary_text.call_args.args[0])
        operation = window.run_background.call_args.args[1]
        operation()
        window.repository.merge_to_main.assert_called_once_with('branch1', expected=plan)
        window.repository.save_version.assert_not_called()
