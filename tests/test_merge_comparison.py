"""Manual two-directory merge sessions use disposable Git repositories."""
import json
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
        self.path = self.root / "workspace"
        self.path.mkdir()
        (self.path / "shared.txt").write_text("base\n")
        (self.path / "main-only.txt").write_text("main base\n")
        self.repo = MODULE.initialize_repository(
            self.path, "v0.0.3", "base", ["shared.txt", "main-only.txt"],
        )
        self.repo.create_branch("branch1", "v0.0.3")
        (self.path / "shared.txt").write_text("branch\n")
        (self.path / "branch-only.txt").write_text("branch file\n")
        self.repo.save_version(
            "branch1/v0.0.3-beta.0.0.1", "branch", ["shared.txt", "branch-only.txt"],
        )
        self.repo.switch_branch("main")
        (self.path / "shared.txt").write_text("main\n")
        (self.path / "main-only.txt").write_text("main release\n")
        self.repo.save_version("v0.0.6", "main", ["shared.txt", "main-only.txt"])
        self.tags_before = self.repo.tags()
        self.refs_before = dict(self.repo._local_refs())
        self.main_head = self.repo.run(["rev-parse", "HEAD"]).strip()

    def begin(self, branch="main"):
        if self.repo.current_branch() != branch:
            self.repo.switch_branch(branch)
        session = self.repo.merge_to_main("branch1")
        directory, data = self.repo._review_session_data(session)
        return session, directory, data

    def launch(self, session, edit=None, tools=("meld",)):
        original = MODULE.subprocess.run
        calls = []

        def run_tool(command, **options):
            if command[0] == "git":
                return original(command, **options)
            calls.append(command)
            root, _data = self.repo._review_session_data(session)
            self.assertIn(str(root / "branch"), command)
            self.assertIn(str(root / "main"), command)
            if edit:
                edit(root)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        results = []
        for tool in tools:
            self.repo.configure_compare_tool(tool)
            with mock.patch.object(MODULE.shutil, "which", return_value="/tool"), \
                    mock.patch.object(MODULE.subprocess, "run", side_effect=run_tool):
                results.append(self.repo.launch_merge_comparison(session))
        return results[-1], calls

    def test_start_isolated_and_cancel_changes_no_repository_state(self):
        session, directory, _data = self.begin("branch1")
        original_head = self.repo.run(["rev-parse", "HEAD"]).strip()
        original_content = (self.path / "shared.txt").read_text()
        recovery_refs = self.repo.run([
            "for-each-ref", "--format=%(refname) %(objectname)", "refs/code-version-manager/recovery/",
        ])
        self.assertEqual("branch1", self.repo.current_branch())
        self.assertEqual(self.refs_before, self.repo._local_refs())
        self.assertEqual(recovery_refs, self.repo.run([
            "for-each-ref", "--format=%(refname) %(objectname)", "refs/code-version-manager/recovery/",
        ]))
        self.assertEqual(original_head, self.repo.run(["rev-parse", "HEAD"]).strip())
        self.assertFalse(self.repo._git_path("MERGE_HEAD").exists())
        self.assertEqual("branch\n", (self.path / "shared.txt").read_text())
        self.assertEqual("branch\n", (directory / "branch" / "shared.txt").read_text())
        self.assertEqual(0, directory.joinpath("branch").stat().st_mode & 0o222)
        self.assertEqual(0, (directory / "branch" / "shared.txt").stat().st_mode & 0o222)
        (directory / "main" / "shared.txt").write_text("manual choice\n")
        self.repo.abort_merge()
        self.assertEqual("branch1", self.repo.current_branch())
        self.assertEqual(self.refs_before, self.repo._local_refs())
        self.assertEqual(recovery_refs, self.repo.run([
            "for-each-ref", "--format=%(refname) %(objectname)", "refs/code-version-manager/recovery/",
        ]))
        self.assertEqual(original_head, self.repo.run(["rev-parse", "HEAD"]).strip())
        self.assertEqual(original_content, (self.path / "shared.txt").read_text())
        self.assertFalse(directory.exists())
        self.assertFalse(self.repo.merge_state().active)

    def test_complete_creates_single_parent_commit_and_can_keep_source(self):
        session, directory, _data = self.begin()
        (directory / "main" / "shared.txt").write_text("manual choice\n")
        (directory / "main" / "branch-only.txt").write_text("copy from branch\n")
        (directory / "main" / "main-only.txt").unlink()
        result, _calls = self.launch(session)
        self.assertEqual({"shared.txt", "branch-only.txt", "main-only.txt"},
                         {path for path, _ in result.changes})
        message = self.repo.complete_merge(delete_source=False)
        self.assertIn("普通提交", message)
        self.assertEqual("main", self.repo.current_branch())
        self.assertEqual("manual choice\n", (self.path / "shared.txt").read_text())
        self.assertEqual("copy from branch\n", (self.path / "branch-only.txt").read_text())
        self.assertFalse((self.path / "main-only.txt").exists())
        self.assertEqual("0.0.6\n", (self.path / "VERSION").read_text())
        parents = self.repo.run(["rev-list", "--parents", "-n", "1", "HEAD"]).split()
        self.assertEqual(2, len(parents))
        self.assertEqual(self.tags_before, self.repo.tags())
        self.assertIn("branch1", [branch.name for branch in self.repo.local_branches()])
        self.assertFalse(self.repo.merge_state().active)

    def test_complete_defaults_to_deleting_source_and_all_beta_tags(self):
        self.repo.update_version_remark("v0.0.3", "已更新现场")
        self.repo.switch_branch("branch1")
        beta = MODULE.canonical_version("branch1/v0.0.3-beta.0.0.1")
        self.repo.update_version_remark(beta, "测试中")
        note_oid = self.repo._local_refs()[MODULE.REMARK_REF_PREFIX + beta]
        session, directory, _data = self.begin("branch1")
        (directory / "main" / "shared.txt").write_text("selected\n")
        self.launch(session)
        self.repo.complete_merge()
        refs = self.repo._local_refs()
        self.assertNotIn("refs/heads/branch1", refs)
        self.assertFalse(any("/branches/branch1/" in ref for ref in refs))
        self.assertIn("refs/tags/v0.0.3", refs)
        self.assertIn("refs/tags/v0.0.6", refs)
        self.assertEqual("已更新现场", self.repo.version_remark("v0.0.3"))
        recovery = self.repo.run(["for-each-ref", "--format=%(refname) %(objectname)",
                                  "refs/code-version-manager/recovery/"])
        self.assertIn("/before/notes/code-version-manager/remarks/" + beta + " " + note_oid, recovery)
        self.assertEqual("selected\n", (self.path / "shared.txt").read_text())
        self.assertEqual("0.0.6\n", (self.path / "VERSION").read_text())
        self.assertNotIn("beta", (self.path / "CHANGELOG.rst").read_text())
        self.assertEqual([], self.repo.status())

    def test_all_tools_receive_isolated_directories_and_never_write_main(self):
        session, directory, _data = self.begin()
        actual = (self.path / "shared.txt").read_bytes()

        def edit(root):
            (root / "main" / "shared.txt").write_text("tool edit\n")

        _result, calls = self.launch(session, edit, tools=("bcompare", "meld", "kdiff3"))
        self.assertEqual(["bcompare", "meld", "kdiff3"], [call[0] for call in calls])
        self.assertIn("-leftreadonly", calls[0])
        self.assertEqual(actual, (self.path / "shared.txt").read_bytes())
        self.assertTrue(self.repo.merge_state().ready)
        self.repo.abort_merge()

    def test_source_and_main_gitignore_union_hides_nested_paths_and_blocks_writeback(self):
        for name in ("main-hidden.txt", "branch-hidden.txt", "nested/main-cache/cache.txt",
                     "nested/branch-cache/cache.txt"):
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("tracked base\n")
        self.repo.run(["add", "-f", "main-hidden.txt", "branch-hidden.txt",
                       "nested/main-cache/cache.txt", "nested/branch-cache/cache.txt"])
        self.repo.run(["commit", "-m", "track ignore fixtures"])
        self.repo.run(["tag", "-f", "-a", "v0.0.3", "-m", "base"])
        self.repo.run(["branch", "-f", "branch1", "v0.0.3"])
        self.repo.switch_branch("branch1")
        with (self.path / ".gitignore").open("a") as stream:
            stream.write("/branch-hidden.txt\n")
        (self.path / "nested" / ".gitignore").write_text("branch-cache/\n")
        (self.path / "branch-hidden.txt").write_text("branch hidden\n")
        (self.path / "nested" / "branch-cache" / "cache.txt").write_text("branch cache\n")
        self.repo.run(["add", ".gitignore", "nested/.gitignore"])
        self.repo.run(["add", "-f", "branch-hidden.txt", "nested/branch-cache/cache.txt"])
        self.repo.run(["commit", "-m", "branch ignore rules"])
        self.repo.run(["tag", "-f", "-a", "branches/branch1/v0.0.3-beta.0.0.1", "-m", "beta"])
        self.repo.switch_branch("main")
        with (self.path / ".gitignore").open("a") as stream:
            stream.write("/main-hidden.txt\n")
        (self.path / "nested" / ".gitignore").write_text("main-cache/\n")
        (self.path / "main-hidden.txt").write_text("main hidden\n")
        (self.path / "nested" / "main-cache" / "cache.txt").write_text("main cache\n")
        self.repo.run(["add", ".gitignore", "nested/.gitignore"])
        self.repo.run(["add", "-f", "main-hidden.txt", "nested/main-cache/cache.txt"])
        self.repo.run(["commit", "-m", "main ignore rules"])
        self.repo.run(["tag", "-f", "-a", "v0.0.6", "-m", "main"])
        self.repo.run(["config", "--local", "branch.branch1.codeVersionManagerBase", "v0.0.3"])
        self.repo.run(["config", "--local", "--unset-all", "branch.branch1.codeVersionManagerBase"], check=False)
        self.repo.switch_branch("branch1")
        self.repo.run(["config", "--local", "branch.branch1.codeVersionManagerBase", "v0.0.3"])
        self.repo.switch_branch("main")

        session = self.repo.merge_to_main("branch1")
        root, data = self.repo._review_session_data(session)
        expected = {"main-hidden.txt", "branch-hidden.txt",
                    "nested/main-cache/cache.txt", "nested/branch-cache/cache.txt"}
        self.assertTrue(expected.issubset(set(data["ignored_paths"])))
        for side in ("branch", "main"):
            for path in expected:
                self.assertFalse((root / side / path).exists())
        for path in expected:
            (root / "main" / path).parent.mkdir(parents=True, exist_ok=True)
            (root / "main" / path).write_text("attempted bypass\n")
        with self.assertRaisesRegex(MODULE.GitError, "gitignore|.gitignore"):
            self.repo.merge_comparison_result(session)
        self.repo.abort_merge()

    def test_tag_or_worktree_change_invalidates_session(self):
        session, _directory, _data = self.begin()
        tag = "refs/tags/branches/branch1/v0.0.3-beta.0.0.1"
        self.repo.run(["update-ref", tag, "refs/heads/main"])
        with self.assertRaisesRegex(MODULE.GitError, "引用已变化"):
            self.repo.launch_merge_comparison(session)
        self.repo.abort_merge()

    def test_damaged_session_structure_blocks_writeback_and_preserves_copy_on_cancel(self):
        session, directory, data = self.begin()
        before_refs = self.repo._local_refs()
        before_source = (self.path / "shared.txt").read_bytes()
        path = directory / "session.json"
        cases = [[], {**data, "editable": "shared.txt"}, {**data, "write_modes": []},
                 {**data, "snapshot": []}, {**data, "left": {"../escape": {}}},
                 {**data, "right": {"shared.txt": {"kind": "file", "mode": 0o644, "sha256": []}}}]
        for damaged in cases:
            with self.subTest(damaged=damaged):
                path.write_text(json.dumps(damaged))
                with self.assertRaises(MODULE.GitError):
                    self.repo.prepare_merge_comparison()
                with self.assertRaises(MODULE.GitError):
                    self.repo.merge_comparison_result(session)
                self.assertEqual(before_refs, self.repo._local_refs())
                self.assertEqual(before_source, (self.path / "shared.txt").read_bytes())
        message = self.repo.abort_merge()
        self.assertIn("已保留", message)
        self.assertTrue(directory.is_dir())
        self.assertFalse((self.repo.metadata_path / "merge.json").exists())

    def test_damaged_merge_field_types_block_completion_and_keep_safe_cancel(self):
        session, directory, _data = self.begin()
        path = self.repo.metadata_path / "merge.json"
        original = json.loads(path.read_text())
        for key, value in (("local_refs", [1]), ("comparison_session", []), ("source_oid", 1),
                           ("source", "../escape"), ("tool_closed", "yes"), ("output_signature", [])):
            with self.subTest(key=key):
                path.write_text(json.dumps({**original, key: value}))
                self.assertFalse(self.repo.merge_state().ready)
                with self.assertRaises(MODULE.GitError):
                    self.repo.complete_merge()
        self.repo.abort_merge()
        self.assertEqual(self.refs_before, self.repo._local_refs())


class MergeComparisonUITest(unittest.TestCase):
    def test_merge_preview_describes_manual_workflow(self):
        window = mock.Mock()
        plan = MODULE.BranchMergePlan(
            "branch1", "branches/branch1/v0.0.3-beta.0.0.3", "a" * 40,
            "v0.0.6", "b" * 40, (),
        )
        with mock.patch.object(MODULE.Gtk, "MessageDialog") as factory:
            factory.return_value.run.return_value = MODULE.Gtk.ResponseType.OK
            MODULE.VersionManagerWindow._background_merge_ready(window, plan)
        description = factory.return_value.format_secondary_text.call_args.args[0]
        self.assertIn("普通提交", description)
        self.assertIn(".gitignore", description)
        operation = window.run_background.call_args.args[1]
        operation()
        window.repository.merge_to_main.assert_called_once_with("branch1", expected=plan)
