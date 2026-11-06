#!/usr/bin/python3
"""Read a workspace, exercise independent copies, verify the source is unchanged."""
import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / 'src' / 'code_version_manager.py'
SPEC = importlib.util.spec_from_file_location('cvm_isolated_verification', SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def git(path, *args):
    environment = dict(os.environ, GIT_OPTIONAL_LOCKS='0')
    result = subprocess.run(['git', '-C', str(path), *args], check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=environment)
    return result.stdout.strip()


def fingerprint(root, exclude_root=()):
    digest = hashlib.sha256()
    for directory, directories, filenames in os.walk(root, followlinks=False):
        if Path(directory) == root:
            directories[:] = [name for name in directories if name not in exclude_root]
            filenames = [name for name in filenames if name not in exclude_root]
        directories.sort()
        for name in sorted(directories + filenames):
            path = Path(directory) / name
            digest.update(str(path.relative_to(root)).encode('utf-8', errors='surrogateescape'))
            digest.update(str(path.lstat().st_mode).encode('ascii'))
            if path.is_symlink():
                digest.update(os.fsencode(os.readlink(path)))
            elif path.is_file():
                with path.open('rb') as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                        digest.update(chunk)
    return digest.hexdigest()


def managed_refs(path):
    output = git(path, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads/', 'refs/tags/')
    return {ref: oid for ref, oid in (line.split() for line in output.splitlines())
            if MODULE.GitRepository._managed_ref(ref)}


def verify_tip_withdrawal(repo, root, remote):
    """Withdraw the copied branch's tip using a second independent checkout."""
    version = repo.current_version_tag()
    if not version:
        return {'state': 'skipped', 'reason': '当前分支没有已发布版本'}
    plan = repo.preview_version_deletion(version)
    if not plan.can_delete_commit:
        return {'state': 'skipped', 'reason': plan.commit_error}
    path = root / 'tip-withdrawal-workspace'
    git(root, 'clone', '--no-local', '--no-hardlinks', '--branch', plan.branch,
        '-q', str(repo.path), str(path))
    withdrawal = MODULE.GitRepository(path)
    withdrawal.configure_remote(str(remote), 'Isolated Verification', 'verification@example.invalid')
    # A normal clone checks out one branch; recreate the other copied local
    # heads so the baseline dependency and complete preservation can be checked.
    before_refs = repo._local_refs()
    for ref, oid in before_refs.items():
        git(path, 'update-ref', ref, oid)
    assert withdrawal._sync_target() == str(remote)
    assert withdrawal._local_refs() == before_refs
    withdrawal.refresh_workspace_generated_files()
    before_files = fingerprint(path, ('.git', *MODULE.WORKSPACE_GENERATED_FILES))
    before_remote = managed_refs(remote)
    checked = withdrawal.preview_version_deletion(version)
    withdrawal.delete_latest_version_commit(version, expected=checked)
    desired = dict(before_refs)
    desired[f'refs/heads/{plan.branch}'] = checked.parent_oid
    del desired[f'refs/tags/{version}']
    assert withdrawal._local_refs() == desired
    assert fingerprint(path, ('.git', *MODULE.WORKSPACE_GENERATED_FILES)) == before_files
    assert managed_refs(remote) == before_remote
    withdrawal.sync_remote()
    assert managed_refs(remote) == desired
    assert (withdrawal.metadata_path / 'recovery').is_dir()
    # The other verification scenarios continue from the original copied refs.
    repo.sync_remote(scope='all')
    assert managed_refs(remote) == before_remote
    return {'state': 'passed', 'branch': plan.branch, 'version': MODULE.display_version(version),
            'before': checked.head_oid, 'after': checked.parent_oid,
            'source_files_preserved': True, 'explicit_branch_upload': 'passed'}


def verify(workspace):
    source = MODULE.GitRepository(workspace)
    before = fingerprint(source.path)
    original_refs = source._local_refs()
    if 'refs/heads/main' not in original_refs:
        raise MODULE.GitError('验证素材必须有 main；程序不会修改来源来补齐。')
    report = {'workspace': str(source.path), 'source_sha256': before,
              'source_branches': sum(ref.startswith('refs/heads/') for ref in original_refs),
              'source_version_tags': sum(ref.startswith('refs/tags/') for ref in original_refs)}
    with tempfile.TemporaryDirectory(prefix='cvm-workspace-verification-') as temporary:
        root = Path(temporary)
        copy = root / 'workspace'
        # Both files and Git storage are independent; never reuse a .git file
        # pointing back to a source worktree, or hard-link writable objects.
        shutil.copytree(source.path, copy, symlinks=True, ignore=shutil.ignore_patterns('.git'))
        bare = root / 'independent.git'
        git(root, 'clone', '--mirror', '--no-local', '--no-hardlinks', '-q', str(source.path), str(bare))
        shutil.move(str(bare), str(copy / '.git'))
        git(copy, 'config', 'core.bare', 'false')
        git(copy, 'config', '--unset-all', 'remote.origin.mirror')
        git(copy, 'reset', '--mixed', 'HEAD')
        if source.metadata_path.exists():
            if any(path.is_symlink() for path in source.metadata_path.rglob('*')):
                raise MODULE.GitError('版本元数据含符号链接，无法保证副本隔离；请先检查该元数据目录。')
            shutil.copytree(source.metadata_path, copy / '.git' / MODULE.METADATA_DIRECTORY,
                            symlinks=True, dirs_exist_ok=True)
        remote = root / 'temporary-origin.git'
        git(root, 'init', '--bare', '-q', str(remote))
        git(remote, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        repo = MODULE.GitRepository(copy)
        repo.configure_remote(str(remote), 'Isolated Verification', 'verification@example.invalid')
        assert repo._sync_target() == str(remote)
        assert repo._local_refs() == original_refs
        if source.merge_state().active:
            tags = repo.tags()
            merge = json.loads((repo.metadata_path / "merge.json").read_text(encoding="utf-8"))
            session = MODULE.MergeComparisonSession(
                merge["comparison_session"], MODULE.display_version(merge["source_tag"]),
            )
            directory, data = repo._review_session_data(session)
            for path in data["editable"]:
                right = directory / "main" / path
                if right.is_file():
                    right.write_bytes(right.read_bytes() + b"\nmanual review\n")
                    break
            original_run = MODULE.subprocess.run
            def close_tool(command, **options):
                if command[0] == "git":
                    return original_run(command, **options)
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            with mock.patch.object(MODULE.shutil, "which", return_value="/isolated-tool"), \
                    mock.patch.object(MODULE.subprocess, "run", side_effect=close_tool):
                repo.launch_merge_comparison(session)
            repo.complete_merge(delete_source=False)
            assert repo.tags() == tags
            assert len(git(copy, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 2
            report["manual_two_directory_merge_without_publication"] = "passed"
            # Continue the existing mirror checks from the copied original refs.
            git(copy, "reset", "--hard", original_refs["refs/heads/main"])
            assert repo._local_refs() == original_refs
        anomalies = {}
        for branch in repo.local_branches():
            if branch.name != 'main':
                try:
                    anomalies[branch.name] = {'base': repo.branch_base(branch.name)}
                except MODULE.GitError as error:
                    anomalies[branch.name] = {'expected_legacy_warning': str(error)}
        report['branch_baselines'] = anomalies
        repo.sync_remote(scope="all")
        assert managed_refs(remote) == original_refs
        report['initial_whole_push'] = 'passed'
        report['tip_version_withdrawal'] = verify_tip_withdrawal(repo, root, remote)
        main = original_refs['refs/heads/main']
        git(remote, 'update-ref', 'refs/heads/cvm-verification-remote-only', main)
        git(remote, 'update-ref', 'refs/tags/cvm-verification-note', main)
        formal = next((ref for ref in original_refs if MODULE.parse_version_tag(ref)
                       and MODULE.parse_version_tag(ref)[0] is None), None)
        if formal:
            git(remote, 'update-ref', formal, main)
        snap = repo.sync_snapshot(scope="all")
        status = repo.remote_sync_status(snap)
        assert status.can_sync_local, status.local_message
        repo.sync_local_from_remote(snap, status)
        assert repo._local_refs() == managed_refs(remote)
        assert 'cvm-verification-note' not in repo.tags()
        report['whole_pull_with_shared_tag_change'] = 'passed'
        repo.run(['branch', '-D', 'cvm-verification-remote-only'])
        repo.run(['branch', 'cvm-verification-local-only', 'main'])
        repo.sync_remote(scope="all")
        assert repo._local_refs() == managed_refs(remote)
        assert git(remote, 'show-ref', '--verify', 'refs/tags/cvm-verification-note')
        assert not git(remote, 'for-each-ref', '--format=%(refname)', 'refs/code-version-manager/')
        assert list((repo.metadata_path / 'recovery').rglob('git-config'))
        report['whole_push_with_branch_deletion'] = 'passed'
        report['private_refs_excluded_and_recovery_created'] = 'passed'
        # Exercise the default current-branch scope against the same temporary
        # origin, including independent branches absent on the other side.
        repo.switch_branch('main')
        git(copy, 'branch', 'cvm-verification-kept-local', 'main')
        git(remote, 'update-ref', 'refs/heads/cvm-verification-kept-remote', git(remote, 'rev-parse', 'main'))
        kept_local = git(copy, 'rev-parse', 'cvm-verification-kept-local')
        kept_remote = git(remote, 'rev-parse', 'cvm-verification-kept-remote')
        git(copy, 'commit', '--allow-empty', '-q', '-m', 'isolated current branch upload')
        repo.sync_remote()
        assert git(remote, 'rev-parse', 'cvm-verification-kept-remote') == kept_remote
        assert not git(remote, 'for-each-ref', 'refs/heads/cvm-verification-kept-local')
        remote_main = git(remote, 'rev-parse', 'main')
        git(copy, 'commit', '--allow-empty', '-q', '-m', 'isolated current branch download')
        snap = repo.sync_snapshot()
        status = repo.remote_sync_status(snap)
        repo.sync_local_from_remote(snap, status)
        assert git(copy, 'rev-parse', 'main') == remote_main
        assert git(copy, 'rev-parse', 'cvm-verification-kept-local') == kept_local
        assert not git(copy, 'for-each-ref', 'refs/heads/cvm-verification-kept-remote')
        report['current_branch_push_and_pull_preserve_other_branches'] = 'passed'
    after = fingerprint(source.path)
    report['source_unchanged'] = after == before
    if after != before:
        raise MODULE.GitError('验证期间来源内容发生变化，请核对是否有其他程序正在操作；本工具未向来源或真实 origin 写入。')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workspace', type=Path)
    options = parser.parse_args()
    print(json.dumps(verify(options.workspace.expanduser().resolve()), ensure_ascii=False, indent=2))
