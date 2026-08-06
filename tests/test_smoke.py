import sys

# Smoke test: the package and its modules must import cleanly.
# Heavy GUI deps (PyQt6, customtkinter, yt_dlp) are imported lazily by the
# modules themselves, so this stays fast and CI-friendly.


def test_package_importable():
    import universal_downloader

    assert universal_downloader.__version__


def test_main_module_importable():
    from universal_downloader import main as main_mod

    assert hasattr(main_mod, "main")
    assert callable(main_mod.main)


def test_app_module_importable():
    from universal_downloader import app as app_mod

    assert hasattr(app_mod, "main")
    assert callable(app_mod.main)


if __name__ == "__main__":
    test_package_importable()
    test_main_module_importable()
    test_app_module_importable()
    print("smoke tests passed", file=sys.stderr)
