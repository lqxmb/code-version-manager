"""Controlled two-directory merge review, using disposable Git repositories."""
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_backend import MODULE


class MergeComparisonTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'workspace'
        self.path.mkdir()
        for name, text in (('conflict.txt', 'base\n'), ('same.txt', 'same\n'),
                           ('main-only.txt', 'base main\n'), ('extra.txt', 'base extra\n'),
                           ('binary.dat', b'\x00base'), ('hidden.zip', b'zip-bytes')):
            file = self.path / name
            file.write_bytes(text if isinstance(text, bytes) else text.encode())
        self.repo = MODULE.initialize_repository(self.path, 'v0.0.3', 'base',
                                                ['conflict.txt', 'same.txt', 'main-only.txt', 'extra.txt',
                                                 'binary.dat', 'hidden.zip'])
        ignore = self.path / '.gitignore'
        ignore.write_text(ignore.read_text().replace('*.zip\n', ''))
        (self.path / '.gitattributes').write_text('same.txt export-ignore\n')
        self.repo.run(['add', '.gitignore', 'hidden.zip', '.gitattributes'])
        self.repo.run(['commit', '--amend', '--no-edit'])
        self.repo.run(['tag', '-f', '-a', 'v0.0.3', '-m', 'base with tracked archive'])
        self.repo.create_branch('branch1', 'v0.0.3')
        (self.path / 'conflict.txt').write_text('branch\n')
        (self.path / 'added.txt').write_text('auto added\n')
        (self.path / 'binary.dat').write_bytes(b'\x00next')
        self.repo.save_version('branch1/v0.0.3-beta.0.0.1', 'branch',
                               ['conflict.txt', 'same.txt', 'added.txt', 'binary.dat'])
        self.repo.switch_branch('main')
        (self.path / 'conflict.txt').write_text('main-1\n')  # Same length as branch content.
        (self.path / 'main-only.txt').write_text('main later\n')
        self.repo.save_version('v0.0.6', 'main', ['conflict.txt', 'main-only.txt'])
        self.tags_before = self.repo.tags()
        self.repo.merge_to_main('branch1')
        self.assertTrue(self.repo.merge_state().active)

    def prepare(self, **options):
        session = self.repo.prepare_merge_comparison(**options)
        self.assertFalse(session.choice_paths)
        directory, data = self.repo._review_session_data(session)
        return session, directory, data

    def test_complete_trees_clean_main_and_no_manifest_filter(self):
        session, directory, data = self.prepare()
        left, right = directory / 'branch', directory / 'main'
        self.assertEqual('branch\n', (left / 'conflict.txt').read_text())
        self.assertEqual('main-1\n', (right / 'conflict.txt').read_text())
        self.assertIn('<<<<<<<', (self.path / 'conflict.txt').read_text())
        self.assertEqual('auto added\n', (right / 'added.txt').read_text())
        self.assertEqual('main later\n', (right / 'main-only.txt').read_text())
        self.assertTrue((left / 'same.txt').exists())
        self.assertTrue((left / 'hidden.zip').exists())
        self.assertTrue((right / 'hidden.zip').exists())
        self.assertEqual(b'\x00next', (left / 'binary.dat').read_bytes())
        self.assertNotIn('.git', data['paths'])
        self.assertIn('conflict.txt', data['editable'])
        self.assertNotIn('same.txt', data['editable'])
        self.assertNotIn('VERSION', data['editable'])
        for path in (left / 'conflict.txt', right / 'same.txt', right / 'VERSION'):
            self.assertFalse(path.stat().st_mode & 0o222)
        self.assertTrue((right / 'conflict.txt').stat().st_mode & 0o200)
        self.assertEqual(self.tags_before, self.repo.tags())

    def test_two_directory_tools_do_not_apply_or_resolve_automatically(self):
        session, directory, data = self.prepare()
        original = MODULE.subprocess.run
        commands = []
        def tool(command, **kwargs):
            if command[0] == 'git':
                return original(command, **kwargs)
            commands.append(command)
            self.assertIn(str(directory / 'branch'), command)
            self.assertIn(str(directory / 'main'), command)
            self.assertFalse(any('mergeoutput' in arg or arg.startswith('--output') for arg in command))
            (directory / 'main' / 'conflict.txt').write_text('main and branch\n')
            return subprocess.CompletedProcess(command, 0, stdout='', stderr='')
        before = (self.path / 'conflict.txt').read_bytes()
        for name in ('bcompare', 'meld', 'kdiff3'):
            self.repo.configure_compare_tool(name)
            with mock.patch.object(MODULE.shutil, 'which', return_value='/tool'), \
                    mock.patch.object(MODULE.subprocess, 'run', side_effect=tool):
                result = self.repo.launch_merge_comparison(session)
            self.assertIn(('conflict.txt', '更新'), result.changes)
            self.assertEqual(before, (self.path / 'conflict.txt').read_bytes())
            self.assertIn('conflict.txt', self.repo.merge_state().conflicts)
        self.assertIn('-leftreadonly', commands[0])
        self.assertTrue(any(arg.startswith('-lefttitle=') for arg in commands[0]))

    def test_apply_then_mark_solved_and_complete_does_not_publish(self):
        session, directory, _data = self.prepare()
        (directory / 'main' / 'conflict.txt').write_text('main-1\nbranch\n')
        (directory / 'main' / 'main-only.txt').write_text('main amended\n')
        result = self.repo.merge_comparison_result(session)
        self.repo.apply_merge_comparison(result)
        self.assertEqual('main-1\nbranch\n', (self.path / 'conflict.txt').read_text())
        self.assertIn('main-only.txt', self.repo.merge_state().conflicts)
        self.assertIn('conflict.txt', self.repo.merge_state().conflicts)
        self.assertEqual('0.0.6\n', (self.path / 'VERSION').read_text())
        self.assertEqual(self.tags_before, self.repo.tags())
        self.repo.resolve_merge_file('conflict.txt')
        self.repo.resolve_merge_file('main-only.txt')
        self.repo.complete_merge()
        self.assertEqual(self.tags_before, self.repo.tags())
        self.assertEqual('0.0.6\n', (self.path / 'VERSION').read_text())
        self.assertEqual('main amended\n', self.repo.run(['show', 'HEAD:main-only.txt']))

    def test_accept_unchanged_clean_main_still_requires_explicit_resolution(self):
        session, directory, _data = self.prepare()
        result = self.repo.merge_comparison_result(session)
        self.assertIn(('conflict.txt', '更新'), result.changes)
        self.repo.apply_merge_comparison(result)
        self.assertEqual('main-1\n', (self.path / 'conflict.txt').read_text())
        self.assertIn('conflict.txt', self.repo.merge_state().conflicts)
        self.repo.resolve_merge_file('conflict.txt')
        self.repo.complete_merge()
        self.assertEqual(self.tags_before, self.repo.tags())

    def test_reopen_and_recreate_repository_preserves_unapplied_and_applied_edits(self):
        session, directory, _data = self.prepare()
        (directory / 'main' / 'conflict.txt').write_text('edited\n')
        self.repo = MODULE.GitRepository(self.path)
        reopened, repeated, _data = self.prepare()
        self.assertEqual(session, reopened)
        self.assertEqual(directory, repeated)
        self.assertEqual('edited\n', (repeated / 'main' / 'conflict.txt').read_text())
        self.repo.apply_merge_comparison(self.repo.merge_comparison_result(session))
        self.repo.resolve_merge_file('conflict.txt')
        reopened, repeated, _data = self.prepare()
        self.assertEqual(session, reopened)
        self.assertEqual('edited\n', (repeated / 'main' / 'conflict.txt').read_text())

    def test_editing_allowed_file_to_match_branch_is_still_writable(self):
        session, directory, _data = self.prepare()
        target = directory / 'main' / 'conflict.txt'
        target.write_bytes((directory / 'branch' / 'conflict.txt').read_bytes())
        target.chmod(0o444)  # A copy operation may inherit the source's readonly mode.
        self.prepare()
        self.assertTrue(target.stat().st_mode & 0o200)
        result = self.repo.merge_comparison_result(session)
        self.assertIn(('conflict.txt', '更新'), result.changes)

    def test_protected_or_left_changes_reject_entire_writeback(self):
        for side, name in (('main', 'same.txt'), ('main', 'VERSION'), ('branch', 'conflict.txt')):
            with self.subTest(side=side, path=name):
                session, directory, _data = self.prepare()
                target = directory / side / name
                before = target.read_bytes()
                mode = stat.S_IMODE(target.stat().st_mode)
                actual = (self.path / 'conflict.txt').read_bytes()
                target.chmod(0o644)
                target.write_text('not allowed\n')
                with self.assertRaises(MODULE.GitError):
                    self.repo.merge_comparison_result(session)
                self.assertEqual(actual, (self.path / 'conflict.txt').read_bytes())
                target.write_bytes(before)
                target.chmod(mode)

    def test_unknown_file_empty_directory_or_symlink_is_rejected(self):
        session, directory, _data = self.prepare()
        for name, create in (
            ('unknown', lambda p: p.write_text('extra')),
            ('empty-dir', lambda p: p.mkdir()),
            ('alias', lambda p: p.symlink_to(self.path / 'conflict.txt')),
        ):
            target = directory / 'main' / name
            create(target)
            with self.assertRaises(MODULE.GitError):
                self.repo.merge_comparison_result(session)
            target.rmdir() if target.is_dir() and not target.is_symlink() else target.unlink()
        target = directory / 'main' / 'conflict.txt'
        target.unlink()
        target.symlink_to(self.path / 'same.txt')
        with self.assertRaisesRegex(MODULE.GitError, '符号链接'):
            self.repo.merge_comparison_result(session)

    def test_binary_edit_and_permitted_file_deletion_are_written_and_confirmed(self):
        session, directory, data = self.prepare()
        # branch equals auto-merged binary, so it is protected despite binary content.
        self.assertNotIn('binary.dat', data['editable'])
        (directory / 'main' / 'main-only.txt').unlink()
        self.repo.apply_merge_comparison(self.repo.merge_comparison_result(session))
        self.assertFalse((self.path / 'main-only.txt').exists())
        self.assertIn('main-only.txt', self.repo.merge_state().conflicts)
        self.repo.resolve_merge_file('main-only.txt', delete=True)

    def test_external_worktree_index_or_ref_change_stops_apply(self):
        session, directory, _data = self.prepare()
        result = self.repo.merge_comparison_result(session)
        (self.path / 'main-only.txt').write_text('external editor\n')
        before = (self.path / 'conflict.txt').read_bytes()
        with self.assertRaisesRegex(MODULE.GitError, '已变化'):
            self.repo.apply_merge_comparison(result)
        self.assertEqual(before, (self.path / 'conflict.txt').read_bytes())
        self.assertTrue(directory.exists())

    def test_output_changes_after_confirmation_stop_apply(self):
        session, directory, _data = self.prepare()
        result = self.repo.merge_comparison_result(session)
        (directory / 'main' / 'conflict.txt').write_text('late edit\n')
        with self.assertRaisesRegex(MODULE.GitError, '确认后'):
            self.repo.apply_merge_comparison(result)

    def test_ambiguous_existing_edit_requires_choice_and_is_backed_up(self):
        (self.path / 'conflict.txt').write_text('existing user resolution\n')
        question = self.repo.prepare_merge_comparison()
        self.assertEqual(('conflict.txt',), question.choice_paths)
        session, directory, _data = self.prepare(use_main=False)
        self.assertEqual('existing user resolution\n', (directory / 'main' / 'conflict.txt').read_text())
        self.assertEqual('existing user resolution\n', (directory / 'original' / 'conflict.txt').read_text())
        self.assertEqual('existing user resolution\n', (self.path / 'conflict.txt').read_text())

    def test_legacy_merge_without_initial_snapshot_can_choose_clean_main(self):
        record = json.loads((self.repo.metadata_path / 'merge.json').read_text())
        record.pop('initial_conflicts')
        (self.repo.metadata_path / 'merge.json').write_text(json.dumps(record))
        question = self.repo.prepare_merge_comparison()
        self.assertTrue(question.choice_paths)
        session, directory, _data = self.prepare(use_main=True)
        self.assertEqual('main-1\n', (directory / 'main' / 'conflict.txt').read_text())
        self.assertIn('<<<<<<<', (directory / 'original' / 'conflict.txt').read_text())

    def test_failed_tool_preserves_edit_and_actual_conflict(self):
        session, directory, _data = self.prepare()
        before = (self.path / 'conflict.txt').read_bytes()
        original = MODULE.subprocess.run
        def fail(command, **options):
            if command[0] == 'git':
                return original(command, **options)
            (directory / 'main' / 'conflict.txt').write_text('saved before failure\n')
            return subprocess.CompletedProcess(command, 2, stdout='', stderr='failed')
        with mock.patch.object(MODULE.shutil, 'which', return_value='/tool'), \
                mock.patch.object(MODULE.subprocess, 'run', side_effect=fail):
            with self.assertRaisesRegex(MODULE.GitError, '副本'):
                self.repo.launch_merge_comparison(session)
        self.assertEqual(before, (self.path / 'conflict.txt').read_bytes())
        self.assertEqual('saved before failure\n', (directory / 'main' / 'conflict.txt').read_text())

    def test_mid_write_failure_restores_already_written_files(self):
        session, directory, _data = self.prepare()
        (directory / 'main' / 'main-only.txt').write_text('change main\n')
        result = self.repo.merge_comparison_result(session)
        originals = {path: (self.path / path).read_bytes() for path, _action in result.changes}
        copy = self.repo._atomic_review_copy
        calls = []
        def fail_second(source, target, mode):
            calls.append(target)
            if len(calls) == 2:
                raise OSError('simulated disk failure')
            return copy(source, target, mode)
        with mock.patch.object(self.repo, '_atomic_review_copy', side_effect=fail_second):
            with self.assertRaisesRegex(MODULE.GitError, '恢复'):
                self.repo.apply_merge_comparison(result)
        for path, content in originals.items():
            self.assertEqual(content, (self.path / path).read_bytes())
        self.assertTrue(list((directory / 'writeback').glob('*/state.json')))

    def test_index_and_source_tag_changes_are_rejected(self):
        session, directory, _data = self.prepare()
        result = self.repo.merge_comparison_result(session)
        self.repo.run(['add', 'main-only.txt'])
        # Change an actual staged blob without touching any working file.
        oid = self.repo.run(['hash-object', '-w', '--stdin'], input='different staged data\n').strip()
        self.repo.run(['update-index', '--cacheinfo', '100644', oid, 'main-only.txt'])
        with self.assertRaisesRegex(MODULE.GitError, '已变化'):
            self.repo.apply_merge_comparison(result)

    def test_source_tag_change_rejects_reopening(self):
        session, directory, _data = self.prepare()
        self.repo.run(['tag', '-f', '-a', 'branches/branch1/v0.0.3-beta.0.0.1', '-m', 'moved', 'main'])
        with self.assertRaisesRegex(MODULE.GitError, '标签已经变化'):
            self.repo.prepare_merge_comparison()

    def test_abort_preserves_review_files_and_removes_merge_state(self):
        session, directory, _data = self.prepare()
        (directory / 'main' / 'conflict.txt').write_text('saved work\n')
        self.repo.abort_merge()
        self.assertFalse(self.repo.merge_state().active)
        self.assertEqual('saved work\n', (directory / 'main' / 'conflict.txt').read_text())
        self.assertEqual('main-1\n', (self.path / 'conflict.txt').read_text())
        self.assertEqual(self.tags_before, self.repo.tags())

    def test_special_files_are_readonly_placeholders_and_are_not_followed(self):
        path = self.root / 'with-link'
        path.mkdir()
        outside = self.root / 'outside-private.txt'
        outside.write_text('outside must never be read or overwritten\n')
        (path / 'alias').symlink_to(outside)
        (path / 'conflict.txt').write_text('base\n')
        repo = MODULE.initialize_repository(path, 'v0.0.3', 'base', ['alias', 'conflict.txt'])
        repo.create_branch('branch1')
        (path / 'conflict.txt').write_text('branch\n')
        repo.save_version('branch1/v0.0.3-beta.0.0.1', 'branch', ['conflict.txt'])
        repo.switch_branch('main')
        (path / 'conflict.txt').write_text('main\n')
        repo.save_version('v0.0.6', 'main', ['conflict.txt'])
        repo.merge_to_main('branch1')
        session = repo.prepare_merge_comparison()
        directory, data = repo._review_session_data(session)
        self.assertNotIn('alias', data['editable'])
        for side in ('main', 'branch'):
            display = directory / side / 'alias'
            self.assertFalse(display.is_symlink())
            self.assertFalse(display.stat().st_mode & 0o222)
            self.assertNotIn(outside.read_text(), display.read_text())
        repo.apply_merge_comparison(repo.merge_comparison_result(session))
        self.assertEqual('outside must never be read or overwritten\n', outside.read_text())
        self.assertTrue((path / 'alias').is_symlink())

    def test_binary_conflict_supports_exact_byte_results(self):
        path = self.root / 'binary-conflict'
        path.mkdir()
        (path / 'blob').write_bytes(b'\x00base')
        repo = MODULE.initialize_repository(path, 'v0.0.3', 'base', ['blob'])
        repo.create_branch('branch1')
        (path / 'blob').write_bytes(b'\x00feature')
        repo.save_version('branch1/v0.0.3-beta.0.0.1', 'branch', ['blob'])
        repo.switch_branch('main')
        (path / 'blob').write_bytes(b'\x00main')
        repo.save_version('v0.0.6', 'main', ['blob'])
        repo.merge_to_main('branch1')
        session = repo.prepare_merge_comparison()
        directory, data = repo._review_session_data(session)
        self.assertIn('blob', data['editable'])
        self.assertEqual(b'\x00main', (directory / 'main' / 'blob').read_bytes())
        (directory / 'main' / 'blob').write_bytes(b'\x00user choice')
        repo.apply_merge_comparison(repo.merge_comparison_result(session))
        self.assertEqual(b'\x00user choice', (path / 'blob').read_bytes())
        self.assertIn('blob', repo.merge_state().conflicts)

    def test_merge_comparison_config_compares_contents_but_regular_compare_is_unchanged(self):
        import xml.etree.ElementTree as ET
        for review in (False, True):
            directory = self.root / str(review)
            config = MODULE.prepare_bcompare_config(directory, merge_review=review)
            xml = ET.parse(config / 'bcompare' / 'BCSessions.xml')
            rules = xml.find('.//TDirCompareSession/Rules')
            self.assertEqual('True' if review else 'False', rules.find('UseContentComparison').get('Value'))
            if review:
                self.assertEqual('ccBinary', rules.find('ContentCompareKind').get('Value'))
                self.assertEqual('*;*', xml.find('.//NameFilter').get('Value'))


class MergeComparisonUITest(unittest.TestCase):
    def test_complete_directory_button_does_not_require_file_selection(self):
        window = mock.Mock()
        window.busy = False
        MODULE.VersionManagerWindow.on_merge_tool(window, None)
        window.run_background.assert_called_once()
        self.assertIs(window.repository.prepare_merge_comparison, window.run_background.call_args.args[1])
        window.conflict_combo.get_active_id.assert_not_called()

    def test_keep_copy_does_not_write_or_resolve(self):
        window = mock.Mock()
        result = MODULE.MergeComparisonResult(MODULE.MergeComparisonSession('a' * 32, 'branch1/beta'),
                                             (('conflict.txt', '更新'),), 'hash')
        with mock.patch.object(MODULE.Gtk, 'Dialog') as factory, \
                mock.patch.object(MODULE.Gtk, 'TextView'), \
                mock.patch.object(MODULE.Gtk, 'ScrolledWindow'), \
                mock.patch.object(MODULE.Gtk, 'Label'):
            factory.return_value.run.return_value = MODULE.Gtk.ResponseType.CANCEL
            MODULE.VersionManagerWindow._background_comparison_ready(window, result)
        window.run_background.assert_not_called()
        window.repository.apply_merge_comparison.assert_not_called()
        window.repository.resolve_merge_file.assert_not_called()
