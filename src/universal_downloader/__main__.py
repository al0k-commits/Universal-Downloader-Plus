"""Console-script / `python -m universal_downloader` entry point.

Launches the modern PyQt6 / PyQt6-WebEngine application (qt_app module).
For the legacy CustomTkinter GUI, run `python -m universal_downloader.app`.
"""

from .qt_app import main


if __name__ == "__main__":
    main()
