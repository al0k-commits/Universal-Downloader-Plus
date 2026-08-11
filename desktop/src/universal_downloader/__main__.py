"""Console-script / `python -m universal_downloader` entry point.

Launches the PyQt6 / PyQt6-WebEngine application (qt_app module).

PyInstaller is pointed at this file directly, so it executes as a top-level
script with ``__package__`` unset. A relative import raises ImportError in that
situation ("attempted relative import with no known parent package"), hence the
fallback chain below.
"""

try:
    # Normal package execution: `python -m universal_downloader`,
    # the `universal-downloader` console script, and the test suite.
    from .qt_app import main
except ImportError:  # pragma: no cover - only hit in a frozen build
    try:
        # PyInstaller keeps the package intact when it is collected as a
        # package rather than a loose module.
        from universal_downloader.qt_app import main
    except ImportError:
        # Last resort: qt_app was frozen as a top-level module.
        from qt_app import main


if __name__ == "__main__":
    main()
