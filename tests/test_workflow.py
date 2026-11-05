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
        snap = repo.sync_snapshot(scope="all")
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
        self.publish(self.repo, 'branch1/v0.0.3-beta.0.0.1', 'branch content\n')
        (self.path / 'feature.txt').write_text('feature file\n', encoding='utf-8')
        self.repo.run(['add', 'feature.txt'])
        self.repo.run(['commit', '--amend', '--no-edit', '-q'])
        self.repo.run(['tag', '-f', '-a', MODULE.canonical_version(
            'branch1/v0.0.3-beta.0.0.1'), '-m', 'branch beta'])
        self.assertEqual('branch1/v0.0.3-beta.0.0.1\n', (self.path / 'VERSION').read_text())
        self.repo.switch_branch('main')
        self.publish(self.repo, 'v0.0.6', 'main content\n')
        (self.path / 'main-only.txt').write_text('keep main work\n', encoding='utf-8')
        self.repo.run(['add', 'main-only.txt'])
        self.repo.run(['commit', '--amend', '--no-edit', '-q'])
        self.repo.run(['tag', '-f', '-a', 'v0.0.6', '-m', 'six'])
        self.repo.sync_remote(scope="all")
        self.repo.switch_branch('branch1')
        self.assertEqual('branches/branch1/v0.0.3-beta.0.0.2', self.repo.next_version())
        self.repo.switch_branch('main')
        plan = self.repo.plan_merge('branch1')
        self.assertEqual('branches/branch1/v0.0.3-beta.0.0.1', plan.source_tag)
        self.assertEqual('v0.0.6', plan.target_version)
        before_tags = {r: o for r, o in self.refs(self.path).items() if r.startswith('refs/tags/')}
        before_remote = self.refs(self.remote)
        main_head = self.repo.run(['rev-parse', 'HEAD']).strip()
        with mock.patch.object(self.repo, '_run_remote', side_effect=AssertionError('merge must be local')):
            session = self.repo.merge_to_main('branch1', expected=plan)
        directory, _data = self.repo._review_session_data(session)
        self.assertEqual(main_head, self.repo.run(['rev-parse', 'HEAD']).strip())
        self.assertEqual('main content\n', (self.path / 'shared.txt').read_text())
        (directory / 'main' / 'shared.txt').write_text('manual branch choice\n')
        (directory / 'main' / 'feature.txt').write_bytes(
            (directory / 'branch' / 'feature.txt').read_bytes(),
        )
        original = MODULE.subprocess.run
        def close_tool(command, **options):
            if command[0] == 'git':
                return original(command, **options)
            return subprocess.CompletedProcess(command, 0, stdout='', stderr='')
        with mock.patch.object(MODULE.shutil, 'which', return_value='/tool'), \
                mock.patch.object(MODULE.subprocess, 'run', side_effect=close_tool):
            self.repo.launch_merge_comparison(session)
        message = self.repo.complete_merge()
        self.assertIn('普通提交', message)
        remaining_tags = {ref: oid for ref, oid in before_tags.items()
                          if '/branches/branch1/' not in ref}
        self.assertEqual(remaining_tags, {r: o for r, o in self.refs(self.path).items() if r.startswith('refs/tags/')})
        self.assertEqual(before_remote, self.refs(self.remote))
        self.assertEqual('0.0.6\n', (self.path / 'VERSION').read_text())
        self.assertEqual(1, len(self.repo.run(['rev-list', '--parents', '-n', '1', 'HEAD']).split()) - 1)
        self.assertEqual([], self.repo.status())
        self.assertTrue((self.path / 'main-only.txt').exists())
        self.assertTrue((self.path / 'feature.txt').exists())
        self.assertNotIn('refs/heads/branch1', self.refs(self.path))
        self.assertFalse(any('/branches/branch1/' in ref for ref in self.refs(self.path)))
        self.assertIn('refs/heads/branch1', self.refs(self.remote))
        self.repo.sync_remote(scope="all")
        self.assertEqual(self.refs(self.path), self.refs(self.remote))
        self.assertTrue(self.repo.run(['for-each-ref', '--format=%(refname)', 'refs/code-version-manager/recovery/']).strip())

    def test_base_recovers_after_import_and_does_not_follow_main(self):
        self.repo.create_branch('feature/camera', 'v0.0.3')
        self.publish(self.repo, 'feature/camera/v0.0.3-beta.0.0.1', 'feature\n')
        self.repo.switch_branch('main')
        self.publish(self.repo, 'v0.0.6', 'main six\n')
        self.repo.sync_remote(scope="all")
        peer = self.peer()
        snap, status = self.status(peer)
        peer.sync_local_from_remote(snap, status)
        peer.switch_branch('feature/camera')
        self.assertEqual('v0.0.3', peer.branch_base())
        self.assertEqual('branches/feature%2Fcamera/v0.0.3-beta.0.0.2', peer.next_version())
        self.assertEqual('feature/camera/v0.0.3-beta.0.0.1\n', (peer.path / 'VERSION').read_text())

    def test_unpublished_branch_base_recovers_from_first_parent(self):
        self.repo.create_branch('feature', 'v0.0.3')
        self.repo.switch_branch('main')
        self.publish(self.repo, 'v0.0.6', 'six\n')
        self.repo.sync_remote(scope="all")
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
        self.repo.sync_remote(scope="all")
        peer = self.peer()
        snap, status = self.status(peer)
        peer.sync_local_from_remote(snap, status)
        peer.switch_branch('feature')
        with self.assertRaisesRegex(MODULE.GitError, '无法唯一恢复基线'):
            peer.branch_base()

    def test_beta_publication_updates_changed_version_and_rejects_wrong_base(self):
        self.repo.create_branch('branch1', 'v0.0.3')
        (self.path / 'VERSION').write_text('manually changed\n', encoding='utf-8')
        self.repo.save_version('branch1/v0.0.3-beta.0.0.1', 'preview', ['shared.txt'])
        self.assertEqual('branch1/v0.0.3-beta.0.0.1\n', (self.path / 'VERSION').read_text())
        self.assertNotIn('VERSION', self.repo.run([
            'ls-tree', '--name-only', 'branches/branch1/v0.0.3-beta.0.0.1',
        ]).splitlines())
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
        self.repo.sync_remote(scope="all")
        peer = self.peer()
        snap, status = self.status(peer)
        peer.sync_local_from_remote(snap, status)
        peer.run(['tag', '-f', '-a', 'v0.0.3', '-m', 'changed shared annotation'])
        peer.run(['branch', 'remote-only'])
        peer.run(['tag', 'external-note'])
        self.git(peer.path, 'push', '-q', 'origin', 'refs/tags/external-note')
        peer.sync_remote(scope="all")
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
        self.repo.sync_remote(scope="all")
        self.assertEqual(self.refs(self.remote), self.refs(self.path))
        self.assertIn('external-note', self.git(self.remote, 'tag').splitlines())

    def test_current_branch_deleted_by_remote_returns_to_main(self):
        self.repo.sync_remote(scope="all")
        self.repo.create_branch('local-only')
        snap, status = self.status()
        self.repo.sync_local_from_remote(snap, status)
        self.assertEqual('main', self.repo.current_branch())
        self.assertEqual(self.refs(self.remote), self.refs(self.path))
        self.assertEqual('0.0.3\n', (self.path / 'VERSION').read_text())

    def test_dirty_staged_untracked_and_ignored_files_have_recovery(self):
        self.repo.sync_remote(scope="all")
        peer = self.peer()
        (peer.path / 'collision.txt').write_text('remote file\n', encoding='utf-8')
        peer.save_version('v0.0.4', 'remote', ['shared.txt', 'collision.txt'])
        peer.sync_remote(scope="all")
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
        self.repo.sync_remote(scope="all")
        self.publish(self.repo, 'v0.0.4', 'four\n')
        snap, status = self.status()
        before = self.refs(self.remote)
        self.repo.run(['branch', 'late-change'])
        with self.assertRaisesRegex(MODULE.GitError, '已变化'):
            self.repo.sync_remote_from_local(snap, status)
        self.assertEqual(before, self.refs(self.remote))

    def test_other_worktree_blocks_local_mirror(self):
        self.repo.sync_remote(scope="all")
        self.repo.run(['branch', 'busy'])
        other = self.root / 'other-worktree'
        self.repo.run(['worktree', 'add', '-q', str(other), 'busy'])
        snap, status = self.status()
        self.assertFalse(status.can_sync_local)
        self.assertIn('另一个工作区', status.local_message)
        self.assertIn('refs/heads/busy', self.refs(self.path))

    def test_atomic_server_rejection_keeps_every_remote_ref(self):
        self.repo.sync_remote(scope="all")
        self.repo.create_branch('branch1')
        self.publish(self.repo, 'branch1/v0.0.3-beta.0.0.1', 'feature\n')
        self.repo.switch_branch('main')
        self.publish(self.repo, 'v0.0.4', 'four\n')
        self.git(self.remote, 'config', 'receive.advertiseAtomic', 'false')
        before = self.refs(self.remote)
        with self.assertRaises(MODULE.GitError):
            self.repo.sync_remote(scope="all")
        self.assertEqual(before, self.refs(self.remote))

    def test_missing_main_and_shallow_history_block_mirrors(self):
        self.repo.sync_remote(scope="all")
        self.repo.run(['branch', '-m', 'main', 'other'])
        snap, status = self.status()
        self.assertFalse(status.can_sync_remote)
        self.assertIn('main', status.remote_message)
        shallow = self.root / 'shallow'
        self.git(self.root, 'clone', '--depth=1', '--branch=main', '-q', self.remote.as_uri(), str(shallow))
        with self.assertRaisesRegex(MODULE.GitError, '浅克隆'):
            MODULE.GitRepository(shallow).sync_snapshot(scope="all")

    def test_unrelated_histories_can_be_mirrored_after_explicit_confirmation(self):
        self.repo.sync_remote(scope="all")
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
        self.repo.sync_remote(scope="all")
        self.repo.run(['branch', 'local-only'])
        before = self.refs(self.path)
        snap, status = self.status()
        with mock.patch.object(self.repo, '_replace_managed_refs', side_effect=MODULE.GitError('simulated lock failure')):
            with self.assertRaisesRegex(MODULE.GitError, '恢复点'):
                self.repo.sync_local_from_remote(snap, status)
        self.assertEqual(before, self.refs(self.path))
        self.assertEqual('main', self.repo.current_branch())


    def test_precise_lease_rejects_concurrent_remote_change_without_partial_tags(self):
        self.repo.sync_remote(scope="all")
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
        self.repo.sync_remote(scope="all")
        self.repo.run(['branch', 'local-only'])
        snap, status = self.status()
        before = self.refs(self.path)
        self.git(self.remote, 'update-ref', 'refs/heads/late-branch', before['refs/heads/main'])
        with self.assertRaisesRegex(MODULE.GitError, '远程.*已变化'):
            self.repo.sync_local_from_remote(snap, status)
        self.assertEqual(before, self.refs(self.path))
        self.assertEqual('main', self.repo.current_branch())

    def test_real_ref_lock_failure_is_a_batch_failure(self):
        self.repo.sync_remote(scope="all")
        peer = self.peer()
        self.publish(peer, 'v0.0.4', 'peer four\n')
        peer.run(['branch', 'new-branch'])
        peer.sync_remote(scope="all")
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
        self.repo.sync_remote(scope="all")
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
        self.repo.sync_remote(scope="all")
        peer = self.peer()
        self.publish(peer, 'v0.0.4', 'four\n')
        peer.sync_remote(scope="all")
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

    def test_manual_merge_is_blocked_from_switch_publish_and_sync_until_decision(self):
        self.repo.create_branch('branch1')
        self.publish(self.repo, 'branch1/v0.0.3-beta.0.0.1', 'feature conflict\n')
        self.repo.switch_branch('main')
        self.publish(self.repo, 'v0.0.4', 'main conflict\n')
        main = self.repo.run(['rev-parse', 'HEAD']).strip()
        tags = self.repo.tags()
        session = self.repo.merge_to_main('branch1')
        self.assertEqual(main, self.repo.run(['rev-parse', 'HEAD']).strip())
        self.assertFalse(self.repo._git_path('MERGE_HEAD').exists())
        with self.assertRaisesRegex(MODULE.GitError, '手动合并'):
            self.repo.switch_branch('branch1')
        with self.assertRaisesRegex(MODULE.GitError, '手动合并'):
            self.repo.sync_snapshot(scope="all")
        with self.assertRaisesRegex(MODULE.GitError, '关闭对比工具'):
            self.repo.complete_merge()
        self.assertEqual(tags, self.repo.tags())
        self.repo.abort_merge()
        self.assertEqual(main, self.repo.run(['rev-parse', 'HEAD']).strip())
        self.assertEqual(tags, self.repo.tags())


class CurrentBranchSyncTest(unittest.TestCase):
    setUp = UnifiedWorkflowTest.setUp
    git = UnifiedWorkflowTest.git
    refs = UnifiedWorkflowTest.refs
    peer = UnifiedWorkflowTest.peer
    publish = UnifiedWorkflowTest.publish

    def status(self, repo=None):
        repo = repo or self.repo
        snapshot = repo.sync_snapshot()
        return snapshot, repo.remote_sync_status(snapshot)

    def feature(self, repo, name):
        repo.switch_branch('main')
        repo.create_branch(name, 'v0.0.3')
        self.publish(repo, name + '/v0.0.3-beta.0.0.1', name + '\n')

    def test_two_people_upload_different_branches_without_deleting_missing_branches(self):
        self.repo.sync_remote()
        peer = self.peer()
        self.feature(self.repo, 'retreat_teb')
        self.feature(peer, 'start_matching')
        self.repo.sync_remote()
        retreat = self.refs(self.remote)
        peer.sync_remote()
        after = self.refs(self.remote)
        for ref, oid in retreat.items():
            self.assertEqual(oid, after[ref])
        self.assertIn('refs/heads/start_matching', after)
        self.assertNotIn('refs/heads/retreat_teb', self.refs(peer.path))
        self.publish(self.repo, 'retreat_teb/v0.0.3-beta.0.0.2', 'retreat second\n')
        self.repo.sync_remote()
        self.assertEqual(after['refs/heads/start_matching'], self.refs(self.remote)['refs/heads/start_matching'])
        self.repo.switch_branch('main')
        self.publish(self.repo, 'v0.0.4', 'main second\n')
        before = self.refs(self.remote)
        self.repo.sync_remote()
        for ref, oid in before.items():
            if ref != 'refs/heads/main':
                self.assertEqual(oid, self.refs(self.remote)[ref])

    def test_download_preserves_stale_other_branch_tags_metadata_and_tracking(self):
        self.repo.sync_remote()
        self.feature(self.repo, 'retreat_teb')
        self.feature(self.repo, 'start_matching')
        self.repo.sync_remote(scope='all')
        self.repo.run(['config', '--local', 'branch.retreat_teb.codeVersionManagerBase', 'v0.0.3'])
        peer = self.peer()
        peer.switch_branch('retreat_teb', source='remote')
        self.publish(peer, 'retreat_teb/v0.0.3-beta.0.0.2', 'remote retreat\n')
        peer.sync_remote()
        peer.switch_branch('start_matching', source='remote')
        self.publish(peer, 'start_matching/v0.0.3-beta.0.0.2', 'remote start\n')
        peer.sync_remote()
        before = self.refs(self.path)
        tracking = self.repo._origin_tracking_refs()
        manifest = self.repo.metadata_path / 'versions/branches/retreat_teb/v0.0.3-beta.0.0.1.manifest'
        content = manifest.read_bytes()
        snapshot, status = self.status()
        self.assertIn('refs/heads/retreat_teb', dict(status.remote_refs))
        self.assertTrue(all('start_matching' in item.ref for item in status.local_changes))
        self.repo.sync_local_from_remote(snapshot, status)
        after = self.refs(self.path)
        for ref, oid in before.items():
            if 'start_matching' not in ref:
                self.assertEqual(oid, after[ref])
        self.assertEqual(content, manifest.read_bytes())
        self.assertEqual(tracking['refs/remotes/origin/retreat_teb'], self.repo._origin_tracking_refs()['refs/remotes/origin/retreat_teb'])
        self.assertEqual('v0.0.3', self.repo.run(['config', '--local', '--get', 'branch.retreat_teb.codeVersionManagerBase']).strip())
        self.assertEqual('remote start\n', (self.path / 'shared.txt').read_text())

    def test_directional_overwrite_rolls_back_branch_rewrites_and_deletes_owned_tags(self):
        self.repo.sync_remote()
        self.feature(self.repo, 'feature/camera')
        self.repo.sync_remote()
        peer = self.peer()
        peer.switch_branch('feature/camera', source='remote')
        self.publish(peer, 'feature/camera/v0.0.3-beta.0.0.2', 'newer\n')
        peer.sync_remote()
        self.repo.update_version_message('feature/camera/v0.0.3-beta.0.0.1', 'changed description')
        snapshot, status = self.status()
        self.assertTrue(any(item.relationship == 'remote_ahead' for item in status.comparisons))
        self.repo.sync_remote_from_local(snapshot, status)
        expected = self.refs(self.path)
        self.assertEqual(expected, self.refs(self.remote))
        snapshot, status = self.status(peer)
        peer.sync_local_from_remote(snapshot, status)
        self.assertEqual(expected, self.refs(peer.path))
        self.assertEqual('changed description', peer.version_message('feature/camera/v0.0.3-beta.0.0.1'))

    def test_development_branch_requires_main_and_matching_formal_baseline(self):
        self.feature(self.repo, 'retreat_teb')
        snapshot, status = self.status()
        self.assertFalse(status.can_sync_remote)
        self.assertIn('main', status.remote_message)
        self.assertEqual({}, self.refs(self.remote))
        self.repo.switch_branch('main')
        self.repo.sync_remote()
        self.repo.switch_branch('retreat_teb')
        self.repo.sync_remote()
        self.git(self.remote, 'update-ref', 'refs/tags/v0.0.3', self.git(self.remote, 'rev-parse', 'main'))
        snapshot, status = self.status()
        self.assertFalse(status.can_sync_local)
        self.assertFalse(status.can_sync_remote)
        self.assertIn('基线', status.remote_message)

    def test_remote_deleted_current_branch_returns_to_unchanged_local_main(self):
        self.repo.sync_remote()
        self.feature(self.repo, 'retreat_teb')
        self.repo.sync_remote()
        local_main = self.refs(self.path)['refs/heads/main']
        peer = self.peer()
        self.publish(peer, 'v0.0.4', 'remote main progress\n')
        peer.sync_remote()
        deletion = peer.plan_remote_branch_deletion('retreat_teb')
        peer.delete_remote_branch('retreat_teb', deletion)
        snapshot, status = self.status()
        self.repo.sync_local_from_remote(snapshot, status)
        self.assertEqual('main', self.repo.current_branch())
        self.assertEqual(local_main, self.refs(self.path)['refs/heads/main'])
        self.assertNotIn('v0.0.4', self.repo.tags())
        self.assertNotIn('refs/heads/retreat_teb', self.refs(self.path))
        self.assertEqual('0.0.3\n', (self.path / 'VERSION').read_text())

    def test_other_branch_worktree_does_not_block_current_branch_sync(self):
        self.repo.sync_remote()
        self.repo.run(['branch', 'busy'])
        self.repo.run(['worktree', 'add', '-q', str(self.root / 'other'), 'busy'])
        peer = self.peer()
        self.publish(peer, 'v0.0.4', 'new main\n')
        peer.sync_remote()
        snapshot, status = self.status()
        self.assertTrue(status.can_sync_local, status.local_message)
        self.repo.sync_local_from_remote(snapshot, status)
        self.assertIn('refs/heads/busy', self.refs(self.path))

    def test_other_remote_branch_changes_before_and_during_push_do_not_cancel(self):
        self.repo.sync_remote()
        self.feature(self.repo, 'retreat_teb')
        self.repo.sync_remote()
        self.publish(self.repo, 'retreat_teb/v0.0.3-beta.0.0.2', 'second\n')
        snapshot, status = self.status()
        main = self.refs(self.remote)['refs/heads/main']
        self.git(self.remote, 'update-ref', 'refs/heads/start_matching', main)
        original = self.repo._run_remote
        def race(args, remote_url):
            if args[0] == 'push' and '--dry-run' not in args:
                self.git(self.remote, 'update-ref', 'refs/heads/another', main)
            return original(args, remote_url)
        with mock.patch.object(self.repo, '_run_remote', side_effect=race):
            self.repo.sync_remote_from_local(snapshot, status)
        self.assertEqual(main, self.refs(self.remote)['refs/heads/start_matching'])
        self.assertEqual(main, self.refs(self.remote)['refs/heads/another'])

    def test_other_local_ref_change_after_confirmation_is_preserved_on_download(self):
        self.repo.sync_remote()
        peer = self.peer()
        self.publish(peer, 'v0.0.4', 'remote progress\n')
        peer.sync_remote()
        snapshot, status = self.status()
        self.repo.run(['branch', 'local-only'])
        oid = self.refs(self.path)['refs/heads/local-only']
        self.repo.sync_local_from_remote(snapshot, status)
        self.assertEqual(oid, self.refs(self.path)['refs/heads/local-only'])

    def test_diverged_branch_can_be_overwritten_in_either_direction(self):
        self.repo.sync_remote()
        self.feature(self.repo, 'retreat_teb')
        self.repo.sync_remote()
        peer = self.peer()
        peer.switch_branch('retreat_teb', source='remote')
        self.publish(self.repo, 'retreat_teb/v0.0.3-beta.0.0.2', 'local variant\n')
        self.publish(peer, 'retreat_teb/v0.0.3-beta.0.0.2', 'remote variant\n')
        peer.sync_remote()
        snapshot, status = self.status()
        self.assertTrue(any(item.relationship == 'diverged' for item in status.comparisons))
        self.repo.sync_local_from_remote(snapshot, status)
        self.assertEqual(self.refs(peer.path), self.refs(self.path))
        self.publish(self.repo, 'retreat_teb/v0.0.3-beta.0.0.3', 'local next\n')
        self.publish(peer, 'retreat_teb/v0.0.3-beta.0.0.3', 'remote next\n')
        peer.sync_remote()
        snapshot, status = self.status()
        self.repo.sync_remote_from_local(snapshot, status)
        self.assertEqual(self.refs(self.path), self.refs(self.remote))

    def test_remote_ref_deletion_after_final_check_preserves_unconfirmed_new_beta(self):
        self.repo.sync_remote()
        self.feature(self.repo, 'retreat_teb')
        self.repo.sync_remote()
        plan = self.repo.plan_remote_branch_deletion('retreat_teb')
        new_tag = 'refs/tags/branches/retreat_teb/v0.0.3-beta.0.0.2'
        head = self.refs(self.remote)['refs/heads/retreat_teb']
        original = self.repo._run_remote
        def race(args, remote_url):
            if args[0] == 'push':
                self.git(self.remote, 'update-ref', new_tag, head)
            return original(args, remote_url)
        with mock.patch.object(self.repo, '_run_remote', side_effect=race):
            with self.assertRaisesRegex(MODULE.GitError, '删除请求已执行'):
                self.repo.delete_remote_branch('retreat_teb', plan)
        self.assertEqual(head, self.refs(self.remote)[new_tag])
        self.assertIn('refs/heads/retreat_teb', self.refs(self.path))

    def test_scoped_remote_change_cancels_before_local_update(self):
        self.repo.sync_remote()
        self.publish(self.repo, 'v0.0.4', 'local progress\n')
        snapshot, status = self.status()
        before = self.refs(self.path)
        self.git(self.remote, 'update-ref', 'refs/tags/v0.0.5', self.refs(self.remote)['refs/heads/main'])
        with self.assertRaisesRegex(MODULE.GitError, '远程.*已变化'):
            self.repo.sync_remote_from_local(snapshot, status)
        self.assertEqual(before, self.refs(self.path))

    def test_local_delete_keeps_origin_and_remote_only_delete_preserves_other_refs(self):
        self.repo.sync_remote()
        self.feature(self.repo, 'retreat_teb')
        self.repo.sync_remote()
        self.feature(self.repo, 'start_matching')
        self.repo.sync_remote()
        preview = self.repo.preview_branch_deletion('retreat_teb')
        self.assertIsNotNone(preview.local)
        self.assertIsNotNone(preview.remote)
        before = self.refs(self.remote)
        self.repo.delete_branch('retreat_teb', expected=preview.local)
        self.repo.sync_remote()
        self.assertEqual(before, self.refs(self.remote))
        preview = self.repo.preview_branch_deletion('retreat_teb')
        self.assertIsNone(preview.local)
        self.git(self.remote, 'update-ref', 'refs/tags/external-note', before['refs/heads/main'])
        self.repo.delete_remote_branch('retreat_teb', preview.remote)
        remaining = self.refs(self.remote)
        for ref, oid in before.items():
            if 'retreat_teb' in ref:
                self.assertNotIn(ref, remaining)
            else:
                self.assertEqual(oid, remaining[ref])
        self.assertEqual(before['refs/heads/main'], self.git(self.remote, 'rev-parse', 'refs/tags/external-note'))
        self.assertTrue(self.repo.run(['for-each-ref', 'refs/code-version-manager/recovery/']).strip())

    def test_remote_deletion_rejects_changed_branch_or_new_beta(self):
        self.repo.sync_remote()
        self.feature(self.repo, 'feature/camera')
        self.repo.sync_remote()
        preview = self.repo.preview_branch_deletion('feature/camera')
        self.git(self.remote, 'update-ref', 'refs/tags/branches/feature%2Fcamera/v0.0.3-beta.0.0.2',
                 self.refs(self.remote)['refs/heads/feature/camera'])
        before = self.refs(self.remote)
        with self.assertRaisesRegex(MODULE.GitError, '已变化'):
            self.repo.delete_branch_with_remote('feature/camera', preview.local, preview.remote)
        self.assertEqual(before, self.refs(self.remote))
        self.assertIn('refs/heads/feature/camera', self.refs(self.path))

    def test_remote_deletion_atomic_lease_rejects_last_moment_change(self):
        self.repo.sync_remote()
        self.feature(self.repo, 'retreat_teb')
        self.repo.sync_remote()
        plan = self.repo.plan_remote_branch_deletion('retreat_teb')
        original = self.repo._run_remote
        def race(args, remote_url):
            if args[0] == 'push':
                self.git(self.remote, 'update-ref', 'refs/heads/retreat_teb', self.refs(self.remote)['refs/heads/main'])
            return original(args, remote_url)
        with mock.patch.object(self.repo, '_run_remote', side_effect=race):
            with self.assertRaises(MODULE.GitError):
                self.repo.delete_remote_branch('retreat_teb', plan)
        self.assertTrue(any('branches/retreat_teb/' in ref for ref in self.refs(self.remote)))

    def test_remote_failure_preserves_local_and_partial_success_is_explicit(self):
        self.repo.sync_remote()
        self.feature(self.repo, 'retreat_teb')
        self.repo.sync_remote()
        preview = self.repo.preview_branch_deletion('retreat_teb')
        hook = self.remote / 'hooks/pre-receive'
        hook.write_text('#!/bin/sh\nexit 1\n')
        hook.chmod(0o700)
        before = self.refs(self.path)
        with self.assertRaises(MODULE.GitError):
            self.repo.delete_branch_with_remote('retreat_teb', preview.local, preview.remote)
        self.assertEqual(before, self.refs(self.path))
        hook.unlink()
        with mock.patch.object(self.repo, 'delete_branch', side_effect=MODULE.GitError('local ref locked')):
            with self.assertRaisesRegex(MODULE.GitError, '远程.*已删除，但本地删除未完成'):
                self.repo.delete_branch_with_remote('retreat_teb', preview.local, preview.remote)
        self.assertEqual(before, self.refs(self.path))
        self.assertNotIn('refs/heads/retreat_teb', self.refs(self.remote))

    def test_offline_preview_still_allows_local_deletion_and_main_is_protected(self):
        self.feature(self.repo, 'retreat_teb')
        self.repo.configure_remote(str(self.root / 'missing.git'), 'Test', 'test@example.com')
        preview = self.repo.preview_branch_deletion('retreat_teb')
        self.assertIsNone(preview.remote)
        self.assertTrue(preview.remote_error)
        self.repo.delete_branch('retreat_teb', expected=preview.local)
        self.assertEqual('main', self.repo.current_branch())
        with self.assertRaisesRegex(MODULE.GitError, 'main'):
            self.repo.plan_remote_branch_deletion('main')

    def test_remote_deletion_timeout_deletes_local_and_allows_remote_only_retry(self):
        for accepted in (False, True):
            with self.subTest(remote_already_accepted=accepted):
                self.repo.sync_remote()
                self.feature(self.repo, 'timeout-feature-' + str(accepted))
                name = self.repo.current_branch()
                self.repo.sync_remote()
                preview = self.repo.preview_branch_deletion(name)
                original = MODULE.run_remote_process
                def timeout(command, **options):
                    if 'push' in command and '--dry-run' not in command:
                        if accepted:
                            original(command, **options)
                        raise subprocess.TimeoutExpired(command, options['timeout'])
                    return original(command, **options)
                with mock.patch.object(MODULE, 'run_remote_process', side_effect=timeout):
                    message = self.repo.delete_branch_with_remote(name, preview.local, preview.remote)
                self.assertIn('本地已删除，远程删除结果待确认', message)
                self.assertEqual('main', self.repo.current_branch())
                self.assertNotIn('refs/heads/' + name, self.refs(self.path))
                self.assertFalse(any(name in ref for ref in self.refs(self.path)))
                remaining = 'refs/heads/' + name in self.refs(self.remote)
                self.assertEqual(not accepted, remaining)
                # A new repository instance models reopening the app. Retry
                # needs only the live origin state, with no in-memory queue.
                reopened = MODULE.GitRepository(self.path)
                if remaining:
                    retry = reopened.preview_branch_deletion(name)
                    self.assertIsNone(retry.local)
                    reopened.delete_remote_branch(name, retry.remote)
                    self.assertNotIn('refs/heads/' + name, self.refs(self.remote))

    def test_ssh_connection_timeout_also_allows_local_deletion(self):
        self.repo.sync_remote()
        self.feature(self.repo, 'retreat_teb')
        self.repo.sync_remote()
        preview = self.repo.preview_branch_deletion('retreat_teb')
        original = MODULE.run_remote_process
        def timeout(command, **options):
            if 'push' in command:
                return subprocess.CompletedProcess(command, 128, stdout='', stderr='ssh: connect to host example.com port 22: Connection timed out')
            return original(command, **options)
        with mock.patch.object(MODULE, 'run_remote_process', side_effect=timeout):
            message = self.repo.delete_branch_with_remote('retreat_teb', preview.local, preview.remote)
        self.assertIn('本地已删除，远程删除结果待确认', message)
        self.assertNotIn('refs/heads/retreat_teb', self.refs(self.path))
        self.assertIn('refs/heads/retreat_teb', self.refs(self.remote))


class RepositoryStatusUITest(unittest.TestCase):
    def test_delete_confirmation_routes_local_remote_both_and_cancel(self):
        for case in ('local', 'both', 'remote', 'offline', 'cancel'):
            with self.subTest(case=case):
                window = mock.Mock()
                repository = window.repository
                local = MODULE.BranchDeletionPlan('retreat_teb', ())
                remote = MODULE.RemoteBranchDeletionPlan('origin-url', 'retreat_teb', (('refs/heads/retreat_teb', 'a' * 40),))
                preview = MODULE.BranchDeletionPreview(
                    'retreat_teb', None if case == 'remote' else local,
                    None if case == 'offline' else remote, 'offline' if case == 'offline' else '',
                )
                with mock.patch.object(MODULE.Gtk, 'Dialog') as dialog_factory, \
                        mock.patch.object(MODULE.Gtk, 'Box'), \
                        mock.patch.object(MODULE.Gtk, 'Label'), \
                        mock.patch.object(MODULE.Gtk, 'CheckButton') as checkbox:
                    dialog_factory.return_value.run.return_value = (
                        MODULE.Gtk.ResponseType.CANCEL if case == 'cancel' else MODULE.Gtk.ResponseType.OK
                    )
                    checkbox.return_value.get_active.return_value = case == 'both'
                    MODULE.VersionManagerWindow._background_branch_deletion_ready(window, repository, preview)
                checkbox.return_value.set_active.assert_called_once_with(False)
                if case == 'cancel':
                    window.run_background.assert_not_called()
                    repository.delete_branch.assert_not_called()
                    continue
                operation = window.run_background.call_args.args[1]
                operation()
                if case == 'remote':
                    repository.delete_remote_branch.assert_called_once_with('retreat_teb', remote)
                    repository.delete_branch.assert_not_called()
                elif case == 'both':
                    repository.delete_branch_with_remote.assert_called_once_with('retreat_teb', local, remote)
                else:
                    repository.delete_branch.assert_called_once_with('retreat_teb', expected=local)
                    repository.delete_remote_branch.assert_not_called()

    def test_sync_confirmation_displays_branch_scope_and_missing_branch_fallback(self):
        window = mock.Mock()
        window._sync_label.side_effect = lambda value, *_args: value
        snapshot = MODULE.RemoteSyncSnapshot('origin-url', 'refs/heads/retreat_teb', ())
        status = MODULE.RemoteSyncStatus('pending', 'changed', snapshot=snapshot)
        with mock.patch.object(MODULE.Gtk, 'Dialog') as dialog_factory, \
                mock.patch.object(MODULE.Gtk, 'Box'), \
                mock.patch.object(MODULE.Gtk, 'ScrolledWindow'):
            dialog_factory.return_value.run.return_value = MODULE.Gtk.ResponseType.OK
            self.assertTrue(MODULE.VersionManagerWindow._confirm_sync_direction(window, 'local', status))
        self.assertIn('retreat_teb', dialog_factory.call_args.kwargs['title'])
        messages = '\n'.join(call.args[0] for call in window._sync_label.call_args_list)
        self.assertIn('返回本地 main', messages)
        self.assertIn('main 不随此次同步更新', messages)

    def test_advanced_entry_explicitly_uses_whole_repository_scope(self):
        window = mock.Mock()
        window.busy = False
        repository = window.repository
        MODULE.VersionManagerWindow.on_advanced_sync(window, None)
        operation = window.run_background.call_args.args[1]
        operation()
        repository.sync_snapshot.assert_called_once_with(scope='all')
        repository.remote_sync_status.assert_called_once_with(repository.sync_snapshot.return_value)

    def test_local_delete_after_remote_timeout_shows_result_warning(self):
        window = mock.Mock()
        message = '本地已删除，远程删除结果待确认：retreat_teb'
        with mock.patch.object(MODULE.Gtk, 'MessageDialog') as dialog_factory:
            MODULE.VersionManagerWindow._background_complete(window, message)
        self.assertEqual(MODULE.Gtk.MessageType.WARNING, dialog_factory.call_args.kwargs['message_type'])
        dialog_factory.return_value.format_secondary_text.assert_called_once_with(message)
        dialog_factory.return_value.run.assert_called_once()
        window.refresh.assert_called_once()

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

    def test_remote_status_refreshes_origin_branches_from_its_snapshot(self):
        window = mock.Mock()
        snapshot = MODULE.RemoteSyncSnapshot("origin-url", "refs/heads/main", ())
        status = MODULE.RemoteSyncStatus(
            "synced", "一致", snapshot=snapshot,
            remote_refs=(("refs/heads/main", "a" * 40),
                         ("refs/heads/feature/camera", "b" * 40)),
        )
        MODULE.VersionManagerWindow._set_remote_status(window, status)
        self.assertEqual("origin-url", window.remote_branch_url)
        self.assertEqual(
            [MODULE.BranchInfo("feature/camera", "remote", "b" * 40),
             MODULE.BranchInfo("main", "remote", "a" * 40)],
            window.remote_branch_choices,
        )
        window._refresh_branch_choices.assert_called_once()
        window._set_sync_button.assert_any_call(
            window.sync_local_button, status.can_sync_local, False,
            mock.ANY, mock.ANY,
        )

    def test_unavailable_remote_status_clears_cached_origin_branches(self):
        window = mock.Mock()
        repo = mock.Mock()
        snapshot = MODULE.RemoteSyncSnapshot("origin-url", "refs/heads/main", ())
        status = MODULE.RemoteSyncStatus("unavailable", "offline", snapshot=snapshot)
        window.repository = repo
        window.remote_status_generation = 7
        window.busy = False
        window.remote_branch_choices = [MODULE.BranchInfo("stale", "remote", "a" * 40)]
        repo.sync_snapshot.return_value = snapshot
        MODULE.VersionManagerWindow._set_remote_status(window, status)
        self.assertEqual([], window.remote_branch_choices)
        window._refresh_branch_choices.assert_called_once()

    def test_removed_remote_branch_refresh_button_has_no_handler(self):
        self.assertFalse(hasattr(MODULE.VersionManagerWindow, "on_refresh_remote_branches"))

    def test_merge_preview_says_manual_commit_and_uses_confirmed_plan(self):
        window = mock.Mock()
        plan = MODULE.BranchMergePlan('branch1', 'branches/branch1/v0.0.3-beta.0.0.3', 'a' * 40, 'v0.0.6', 'b' * 40, ())
        with mock.patch.object(MODULE.Gtk, 'MessageDialog') as factory:
            dialog = factory.return_value
            dialog.run.return_value = MODULE.Gtk.ResponseType.OK
            MODULE.VersionManagerWindow._background_merge_ready(window, plan)
        description = dialog.format_secondary_text.call_args.args[0]
        self.assertIn('普通提交', description)
        self.assertIn('.gitignore', description)
        operation = window.run_background.call_args.args[1]
        operation()
        window.repository.merge_to_main.assert_called_once_with('branch1', expected=plan)
        window.repository.save_version.assert_not_called()
