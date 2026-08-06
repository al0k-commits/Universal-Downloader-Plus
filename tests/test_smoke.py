import sys

# Smoke test: the package and its modules must import cleanly.
# Heavy GUI deps (PyQt6, yt_dlp) are imported lazily by the modules
# themselves, so this stays fast and CI-friendly.


def test_package_importable():
    import universal_downloader

    assert universal_downloader.__version__


def test_qt_app_importable():
    from universal_downloader.qt_app import main

    assert callable(main)


if __name__ == "__main__":
    test_package_importable()
    test_qt_app_importable()
    print("smoke tests passed", file=sys.stderr)
