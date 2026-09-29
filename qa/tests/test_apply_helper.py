"""The autofill extension in the apply browser — found, copied, optional."""

from backend.apply import helper


class Cfg(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def install(root, profile, version, ext="abc"):
    folder = root / profile / "Extensions" / ext / version
    (folder / "_metadata").mkdir(parents=True)
    (folder / "manifest.json").write_text("{}")
    (folder / "_metadata" / "verified_contents.json").write_text("{}")
    return folder


def test_the_newest_installed_copy_is_found(tmp_path):
    install(tmp_path, "Profile 2", "2.9.0_0")
    newest = install(tmp_path, "Profile 3", "2.28.0_0")
    assert helper.find_installed("abc", [tmp_path]) == newest


def test_it_is_copied_without_the_store_metadata(tmp_path):
    install(tmp_path / "chrome", "Default", "1.0_0")
    cfg = Cfg({"apply.helper_extension.enabled": True, "apply.helper_extension.id": "abc"})
    target = helper.prepare(cfg, chrome_dirs=[tmp_path / "chrome"], browser_dir=tmp_path / "b")
    assert (target / "manifest.json").exists() and not (target / "_metadata").exists()


def test_off_or_missing_means_a_plain_browser(tmp_path):
    assert helper.prepare(Cfg(), chrome_dirs=[tmp_path]) is None
    on = Cfg({"apply.helper_extension.enabled": True, "apply.helper_extension.id": "zzz"})
    assert helper.prepare(on, chrome_dirs=[tmp_path], browser_dir=tmp_path / "b") is None
