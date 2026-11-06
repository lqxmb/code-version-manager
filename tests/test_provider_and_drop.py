"""Workspace folder drops and per-workspace Git provider configuration."""
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from test_backend import MODULE


class GitProviderTest(unittest.TestCase):
    def test_gitlab_instance_validation_and_provider_inference(self):
        self.assertEqual("https://gitlab.com", MODULE.validate_gitlab_host(""))
        self.assertEqual("https://gitlab.example.org", MODULE.validate_gitlab_host("gitlab.example.org/"))
        self.assertEqual("https://gitlab.example.org:8443", MODULE.validate_gitlab_host(
            "https://GitLab.Example.org:8443",
        ))
        for host in (
            "http://gitlab.example.org", "https://gitlab.example.org/group", "https://user@gitlab.org",
            "https://gitlab.org?admin=true", "https://gitlab.org#section", "https://gitlab.org:99999",
        ):
            with self.subTest(host=host), self.assertRaises(MODULE.GitError):
                MODULE.validate_gitlab_host(host)
        self.assertEqual(MODULE.GIT_PROVIDER_GITHUB, MODULE.infer_git_provider(
            "git@github.com:owner/repo.git",
        ))
        self.assertEqual(MODULE.GIT_PROVIDER_GITLAB, MODULE.infer_git_provider(
            "https://gitlab.com/group/nested/repo.git",
        ))
        self.assertIsNone(MODULE.infer_git_provider("git@gitlab.example.org:group/repo.git"))
        self.assertEqual(MODULE.GIT_PROVIDER_GITLAB, MODULE.provider_for_remote(
            "git@gitlab.example.org:group/nested/repo.git", "https://gitlab.example.org",
        ))
        self.assertIsNone(MODULE.provider_for_remote(
            "git@gitlab.example.org.evil:group/repo.git", "https://gitlab.example.org",
        ))

    def test_provider_examples_conversion_and_creation_urls(self):
        self.assertEqual(
            "git@github.com:example-org/workspace.git",
            MODULE.provider_ssh_example(MODULE.GIT_PROVIDER_GITHUB, "workspace", "example-org"),
        )
        self.assertEqual(
            "git@gitlab.example.org:<GitLab用户或组路径>/workspace.git",
            MODULE.provider_ssh_example(
                MODULE.GIT_PROVIDER_GITLAB, "workspace", gitlab_host="https://gitlab.example.org",
            ),
        )
        self.assertEqual(
            "git@gitlab.example.org:group/nested/repo.git",
            MODULE.provider_suggested_ssh_remote(
                "https://gitlab.example.org/group/nested/repo", MODULE.GIT_PROVIDER_GITLAB,
                "https://gitlab.example.org",
            ),
        )
        existing = "git@github.com:owner/repo.git"
        self.assertEqual(existing, MODULE.provider_suggested_ssh_remote(
            existing, MODULE.GIT_PROVIDER_GITLAB,
        ))
        self.assertEqual("https://gitlab.example.org/projects/new", MODULE.provider_repository_creation_url(
            MODULE.GIT_PROVIDER_GITLAB, "", "workspace", "https://gitlab.example.org",
        ))
        self.assertEqual("https://gitlab.example.org/-/user_settings/ssh_keys",
                         MODULE.provider_ssh_key_url(MODULE.GIT_PROVIDER_GITLAB,
                                                     "https://gitlab.example.org"))
        self.assertEqual("https://github.com/settings/ssh/new",
                         MODULE.provider_ssh_key_url(MODULE.GIT_PROVIDER_GITHUB))

    def test_provider_settings_are_per_workspace_and_switching_does_not_rewrite_origin(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_path = root / "first"
            first_path.mkdir()
            (first_path / "file.txt").write_text("first\n")
            first = MODULE.initialize_repository(first_path, "v1.0.0", "initial", ["file.txt"])
            origin = "git@github.com:owner/first.git"
            first.configure_remote(origin, "Test", "test@example.com")
            self.assertEqual(MODULE.GIT_PROVIDER_GITHUB, first.git_provider())
            first.configure_remote(origin, "Test", "test@example.com",
                                   git_provider=MODULE.GIT_PROVIDER_GITLAB,
                                   gitlab_host="gitlab.example.org")
            self.assertEqual(origin, first.remote_url())
            self.assertEqual(MODULE.GIT_PROVIDER_GITLAB, first.git_provider())
            self.assertEqual("https://gitlab.example.org", first.gitlab_host())

            second_path = root / "second"
            second_path.mkdir()
            (second_path / "file.txt").write_text("second\n")
            second = MODULE.initialize_repository(second_path, "v1.0.0", "initial", ["file.txt"])
            second.run(["remote", "add", "origin", "git@gitlab.com:group/second.git"])
            self.assertEqual(MODULE.GIT_PROVIDER_GITLAB, second.git_provider())
            first.run(["config", "--local", "--unset-all", "codeversionmanager.gitProvider"])
            self.assertEqual(MODULE.GIT_PROVIDER_GITHUB, first.git_provider())

    def test_gitlab_ssh_errors_use_the_actual_custom_origin_host(self):
        repository = mock.Mock()
        repository.gitlab_host.return_value = "https://gitlab.example.org"
        result = subprocess.CompletedProcess(
            [], 128, stdout="", stderr="git@gitlab.example.org: Permission denied (publickey).",
        )
        with mock.patch.object(MODULE, "run_remote_process", return_value=result):
            with self.assertRaises(MODULE.GitError) as raised:
                MODULE.GitRepository._run_remote(
                    repository, ["ls-remote", "git@gitlab.example.org:group/repo.git"],
                    "git@gitlab.example.org:group/repo.git",
                )
        self.assertIn("GitLab（gitlab.example.org）", str(raised.exception))
        self.assertIn("GitLab SSH 公钥设置", str(raised.exception))


class WorkspaceDropTest(unittest.TestCase):
    def setUp(self):
        if not MODULE.Gtk.init_check()[0]:
            self.skipTest("GTK display unavailable")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = mock.patch.object(MODULE, "settings_file_path", return_value=self.root / "settings.ini")
        patcher.start()
        self.addCleanup(patcher.stop)
        with mock.patch.object(MODULE.VersionManagerWindow, "load_workspace"):
            self.window = MODULE.VersionManagerWindow(None, self.root)
        self.addCleanup(self.window.destroy)
        self.context = mock.Mock()
        self.selection = mock.Mock()

    def drop(self, *uris):
        self.selection.get_uris.return_value = list(uris)
        with mock.patch.object(MODULE.Gtk, "drag_finish") as finish:
            self.window.on_workspace_drop(self.window, self.context, 0, 0, self.selection, 0, 7)
        return finish

    def uri(self, path):
        return MODULE.Gio.File.new_for_path(str(path)).get_uri()

    def test_single_local_folder_opens_and_finishes_drop(self):
        folder = self.root / "dropped workspace"
        folder.mkdir()
        with mock.patch.object(self.window, "load_workspace") as load:
            finish = self.drop(self.uri(folder))
        load.assert_called_once_with(folder)
        finish.assert_called_once_with(self.context, True, False, 7)

    def test_files_multiple_items_remote_uris_and_busy_state_are_rejected(self):
        folder = self.root / "folder"
        folder.mkdir()
        file_path = self.root / "file.txt"
        file_path.write_text("file")
        invalid_drops = (
            (self.uri(file_path),),
            (self.uri(folder), self.uri(folder)),
            ("sftp://host/path",),
        )
        for uris in invalid_drops:
            with self.subTest(uris=uris), mock.patch.object(self.window, "load_workspace") as load, \
                    mock.patch.object(self.window, "show_error") as show_error:
                finish = self.drop(*uris)
            load.assert_not_called()
            show_error.assert_called_once()
            finish.assert_called_once_with(self.context, False, False, 7)
        self.window.busy = True
        with mock.patch.object(self.window, "load_workspace") as load, \
                mock.patch.object(self.window, "show_error") as show_error:
            finish = self.drop(self.uri(folder))
        load.assert_not_called()
        show_error.assert_called_once()
        finish.assert_called_once_with(self.context, False, False, 7)

    def test_provider_specific_create_action_only_opens_selected_service_page(self):
        self.window.workspace_path = self.root / "workspace"
        entry, dialog = mock.Mock(), mock.Mock()
        entry.get_text.return_value = ""
        with mock.patch.object(MODULE.Gtk, "show_uri_on_window") as open_page:
            self.window.open_github_repository_creation(
                dialog, entry, MODULE.GIT_PROVIDER_GITLAB, "https://gitlab.example.org",
            )
        self.assertEqual("https://gitlab.example.org/projects/new", open_page.call_args.args[1])
        open_page.assert_called_once()

    def test_gitlab_ssh_key_dialog_links_to_selected_instance(self):
        def close_and_check():
            dialog = next(item for item in MODULE.Gtk.Window.list_toplevels()
                          if isinstance(item, MODULE.Gtk.Dialog)
                          and item.get_title() == "GitLab SSH 公钥设置")
            links = [item for item in self.walk(dialog) if isinstance(item, MODULE.Gtk.LinkButton)]
            self.assertEqual(1, len(links))
            self.assertEqual("https://gitlab.example.org/-/user_settings/ssh_keys", links[0].get_uri())
            dialog.response(MODULE.Gtk.ResponseType.CLOSE)
            return False

        MODULE.GLib.idle_add(close_and_check)
        with mock.patch.object(MODULE, "ssh_public_keys", return_value={}):
            self.window.show_ssh_public_keys(
                self.window, "test@example.com", MODULE.GIT_PROVIDER_GITLAB,
                "https://gitlab.example.org",
            )

    @staticmethod
    def walk(widget):
        yield widget
        if isinstance(widget, MODULE.Gtk.Container):
            for child in widget.get_children():
                yield from WorkspaceDropTest.walk(child)

    def test_git_account_switch_keeps_origin_and_routes_provider_actions(self):
        workspace = self.root / "account-workspace"
        workspace.mkdir()
        (workspace / "source.txt").write_text("base\n")
        repository = MODULE.initialize_repository(workspace, "v1.0.0", "initial", ["source.txt"])
        origin = "git@github.com:owner/repo.git"
        repository.configure_remote(origin, "Test", "test@example.com")
        self.window.workspace_path = workspace
        self.window.repository = repository

        def widgets(widget):
            yield widget
            if isinstance(widget, MODULE.Gtk.Container):
                for child in widget.get_children():
                    yield from widgets(child)

        def interact():
            dialog = next(item for item in MODULE.Gtk.Window.list_toplevels()
                          if isinstance(item, MODULE.Gtk.Dialog)
                          and item.get_title() == "Git 账号与远程仓库")
            controls = list(widgets(dialog))
            provider = next(item for item in controls if isinstance(item, MODULE.Gtk.ComboBoxText))
            provider.set_active_id(MODULE.GIT_PROVIDER_GITLAB)
            entries = [item for item in controls if isinstance(item, MODULE.Gtk.Entry)]
            remote_entry = next(item for item in entries if item.get_text() == origin)
            self.assertEqual(origin, remote_entry.get_text())
            host_entry = next(item for item in entries if item.get_text() == MODULE.GITLAB_DEFAULT_HOST)
            host_entry.set_text("https://gitlab.example.org")
            buttons = [item for item in widgets(dialog) if isinstance(item, MODULE.Gtk.Button)]
            create = next(item for item in buttons if item.get_label() == "创建 GitLab 仓库（网页）")
            ssh = next(item for item in buttons if item.get_label() == "GitLab SSH 公钥设置")
            with mock.patch.object(MODULE.Gtk, "show_uri_on_window") as open_page, \
                    mock.patch.object(self.window, "show_ssh_public_keys") as show_keys:
                create.clicked()
                ssh.clicked()
            self.assertEqual("https://gitlab.example.org/projects/new", open_page.call_args.args[1])
            show_keys.assert_called_once_with(dialog, "test@example.com", MODULE.GIT_PROVIDER_GITLAB,
                                              "https://gitlab.example.org")
            self.assertEqual(origin, remote_entry.get_text())
            dialog.response(MODULE.Gtk.ResponseType.APPLY)
            return False

        MODULE.GLib.idle_add(interact)
        with mock.patch.object(self.window, "refresh"), \
                mock.patch.object(self.window, "_set_operation_status"):
            self.window.on_git_account(None)
        self.assertEqual(origin, repository.remote_url())
        self.assertEqual(MODULE.GIT_PROVIDER_GITLAB, repository.git_provider())
        self.assertEqual("https://gitlab.example.org", repository.gitlab_host())

