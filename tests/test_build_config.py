"""Build configuration, update trust boundaries and isolated Debian packaging."""
import importlib.util
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_application_updates import MODULE, SOURCE


class BuildConfigurationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        for name in ("src", "data", "debian"):
            shutil.copytree(SOURCE.parents[1] / name, self.project / name,
                            ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("build_deb.sh", "build_config.json", "README.md", "INSTALL.md", "HELP.md"):
            shutil.copyfile(SOURCE.parents[1] / name, self.project / name)
        (self.project / "VERSION").write_text("0.4.10\n", encoding="utf-8")
        self.config = self.project / "build_config.json"

    def load_module(self, source=None):
        spec = importlib.util.spec_from_file_location(
            "code_version_manager_build_configuration", source or self.project / "src" / SOURCE.name)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def configure(self, repository):
        self.config.write_text(json.dumps({"update_repository": repository}), encoding="utf-8")

    def build(self):
        return subprocess.run(["sh", str(self.project / "build_deb.sh")], cwd=self.root,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)

    def invalid_configurations(self):
        invalid_values = (None, 5, [], "", "owner", "owner/repo/extra", "owner/..",
                          "owner/.", "owner/repo?query", "https://github.com/owner/repo",
                          " owner/repo", "owner/repo\n", "bad_owner/repo")
        return (None, b"{", b"\xff", b"[]", b"{}") + tuple(
            json.dumps({"update_repository": value}).encode() for value in invalid_values)

    def write_configuration(self, content):
        if content is None:
            self.config.unlink(missing_ok=True)
        else:
            self.config.write_bytes(content)

    def test_repository_comes_from_project_configuration(self):
        configured = json.loads((SOURCE.parents[1] / "build_config.json").read_text(encoding="utf-8"))
        self.assertEqual(configured["update_repository"], MODULE.UPDATE_REPOSITORY)
        self.assertEqual("", MODULE.UPDATE_CONFIG_ERROR)

    def test_source_and_installed_locations_ignore_cwd_and_other_files(self):
        self.configure("example/source-updates")
        (self.project / "src" / "build_config.json").write_text("invalid", encoding="utf-8")
        with mock.patch.object(MODULE, "__file__", str(self.project / "src" / SOURCE.name)):
            self.assertEqual(self.config, MODULE.application_build_config_path())
            self.assertEqual("example/source-updates", MODULE.load_application_update_repository())
        installed = self.root / "usr" / "lib" / "code-version-manager" / SOURCE.name
        installed.parent.mkdir(parents=True)
        installed_config = installed.with_name("build_config.json")
        installed_config.write_text('{"update_repository": "example/installed-updates"}', encoding="utf-8")
        installed.parent.parent.joinpath("build_config.json").write_bytes(self.config.read_bytes())
        with mock.patch.object(MODULE, "__file__", str(installed)):
            self.assertEqual(installed_config, MODULE.application_build_config_path())
            self.assertEqual("example/installed-updates", MODULE.load_application_update_repository())
            installed_config.unlink()
            with self.assertRaisesRegex(ValueError, "软件更新配置无效"):
                MODULE.load_application_update_repository()

    def test_custom_repository_controls_api_assets_page_and_redirects(self):
        self.configure("example-tools/desktop-updates")
        module = self.load_module()
        api = "https://api.github.com/repos/example-tools/desktop-updates/releases/latest"
        self.assertEqual("example-tools/desktop-updates", module.UPDATE_REPOSITORY)
        self.assertEqual(api, module.UPDATE_API_URL)
        base = "https://github.com/example-tools/desktop-updates/releases/"
        filename = "code-version-manager_0.4.10_all.deb"
        record = {"tag_name": "v0.4.10", "assets": [
            {"name": name, "size": 8, "browser_download_url": base + "download/v0.4.10/" + name}
            for name in (filename, filename + ".sha256")]}
        with mock.patch.object(module, "_open_update_url", return_value=io.BytesIO(json.dumps(record).encode())) as opened:
            release = module.check_application_update()
            opened.assert_called_once_with(api)
        self.assertEqual(base + "tag/v0.4.10", release.page_url)
        for url in (api, release.package_url, release.checksum_url):
            self.assertEqual(url, module._validate_update_url(url))
        for url in ("https://api.github.com/repos/other/project/releases/latest",
                    "https://github.com/other/project/releases/download/v0.4.10/" + filename):
            with self.assertRaises(module.GitError):
                module._validate_update_url(url)
        with self.assertRaises(module.GitError):
            module._UpdateRedirectHandler().redirect_request(
                None, None, 302, "", {}, "https://github.com/other/project/releases/download/v1/asset")
        record["assets"][0]["browser_download_url"] = "https://github.com/other/project/releases/download/v0.4.10/" + filename
        with mock.patch.object(module, "_open_update_url", return_value=io.BytesIO(json.dumps(record).encode())):
            with self.assertRaisesRegex(module.GitError, "附件地址"):
                module.check_application_update()

    def test_invalid_runtime_configuration_blocks_network_but_allows_local_release(self):
        for content in self.invalid_configurations():
            with self.subTest(content=content):
                self.write_configuration(content)
                module = self.load_module()
                self.assertEqual("", module.UPDATE_REPOSITORY)
                self.assertEqual("", module.UPDATE_API_URL)
                self.assertIn(str(self.config), module.UPDATE_CONFIG_ERROR)
                with mock.patch.object(module, "build_opener") as opener:
                    with self.assertRaisesRegex(module.GitError, "软件更新配置无效"):
                        module.check_application_update()
                    with self.assertRaisesRegex(module.GitError, "软件更新配置无效"):
                        module._validate_update_url("https://objects.githubusercontent.com/asset")
                    opener.assert_not_called()
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "source.txt").write_text("local source\n", encoding="utf-8")
        repository = module.create_repository(workspace)
        repository.run(["config", "user.name", "Test User"])
        repository.run(["config", "user.email", "test@example.com"])
        repository.save_version("v1.0.0", "local release", ["source.txt"])
        self.assertEqual(["v1.0.0"], repository.tags())
        self.assertEqual("local source\n", (workspace / "source.txt").read_text(encoding="utf-8"))

    def test_invalid_configuration_stops_build_before_creating_outputs(self):
        for content in self.invalid_configurations():
            with self.subTest(content=content):
                self.write_configuration(content)
                result = self.build()
                self.assertNotEqual(0, result.returncode)
                self.assertIn("构建配置无效", result.stderr)
                self.assertIn(str(self.config), result.stderr)
                self.assertFalse((self.project / "build").exists())
                self.assertFalse((self.project / "dist").exists())

    def test_deb_preserves_configuration_permissions_and_checksum_on_rebuild(self):
        original = json.loads(self.config.read_text(encoding="utf-8"))["update_repository"]
        for index, repository in enumerate((original, "example/rebuilt-updates")):
            with self.subTest(repository=repository):
                self.configure(repository)
                result = self.build()
                self.assertEqual(0, result.returncode, result.stderr)
                dist = self.project / "dist"
                filename = "code-version-manager_0.4.10_all.deb"
                subprocess.run(["sha256sum", "-c", filename + ".sha256"], cwd=dist,
                               check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                extracted = self.root / ("extracted-" + str(index))
                subprocess.run(["dpkg-deb", "--extract", str(dist / filename), str(extracted)],
                               check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                installed = extracted / "usr" / "lib" / "code-version-manager"
                packaged_config = installed / "build_config.json"
                self.assertEqual(self.config.read_bytes(), packaged_config.read_bytes())
                self.assertEqual(0o644, packaged_config.stat().st_mode & 0o777)
                self.assertEqual(repository, self.load_module(installed / SOURCE.name).UPDATE_REPOSITORY)


if __name__ == "__main__":
    unittest.main()
