<div align="center">

# Universal Downloader+ 🚀

**A modern, fast, feature-rich desktop video & playlist downloader — built with PyQt6 and yt-dlp.**

Inspired by premium tools like *4K Video Downloader+*: an embedded Chromium browser with persistent
logins, smart format recommendation, playlist batch queueing, and automatic ID3 metadata tagging.

[![Build & Release](https://github.com/al0k-commits/Universal-Downloader-Plus/actions/workflows/desktop-release.yml/badge.svg)](https://github.com/al0k-commits/Universal-Downloader-Plus/actions/workflows/desktop-release.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![PyQt6](https://img.shields.io/badge/GUI-PyQt6-41cd52.svg)](https://riverbankcomputing.com/software/pyqt/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)](#installation--local-setup)
[![Release](https://img.shields.io/github/v/release/al0k-commits/Universal-Downloader-Plus?include_prereleases&sort=semver)](https://github.com/al0k-commits/Universal-Downloader-Plus/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

</div>

---

## ✨ Key Features

| | Feature | Detail |
|---|---|---|
| 🌐 | **Embedded Chromium Omnibox** | A real `QWebEngineView` with back/forward/refresh/home, a hairline page-load progress bar, and an omnibox that routes bare text to Google search and anything domain-shaped to the URL directly. Browse to a video, hit the floating **Download** FAB, and the live URL is read at click time — never a stale cached value. |
| 🔑 | **Persistent Sessions & Cookie Bridging** | The profile is a named (non-OTR) `QWebEngineProfile` with `ForcePersistentCookies`, so logins survive restarts. Cookies are mirrored to a Netscape jar in real time and handed to `yt-dlp` via `cookiefile`, unlocking age-restricted and members-only media. |
| 🎯 | **Smart Format Recommendation** | The format list is scored to prioritise **H.264 video + M4A audio** (`format_sort: ["vcodec:h264", "acodec:m4a", "res", "fps"]`) so output plays natively in QuickTime, Photos, and Windows Media Player — no VLC required. The winning row is tagged **⭐ Recommended**. |
| 📋 | **Playlist Engine** | Detects playlists, offers *whole list* or *first video only*, then spawns one worker per entry staggered by `PLAYLIST_STAGGER_MS = 350` ms to dodge rate-limiting. |
| ⚡ | **Smart Mode & Multi-Quality Queue** | Skip the dialog entirely and download using your preset Format/Quality/Container. Or tick **several** formats in the modal at once — each is queued as its own row with a `_[1080p]`-style filename suffix so they never collide. |
| 🎧 | **ID3 Metadata & Album Art** | Every download appends `EmbedThumbnail` + `FFmpegMetadata` post-processors, so MP3/M4A files carry cover art and tags into your music library automatically. |
| 🚦 | **Concurrency Control** | A real queue honours `max_concurrent_downloads` (1–10). Excess downloads wait; the status bar reads `● 3 active · 5 queued`. Individual rows support pause / resume / cancel. |
| 🎨 | **Modern Theming** | A tokenised design system (zinc-900 surfaces, emerald-500 accent, 12px cards, 6px scrollbars) built on `pyqtdarktheme` + `qtawesome`, with an animated pill toggle and instant dark/light switching. |
| 💾 | **Local State Persistence** | Preferences live in `QSettings("Alok", "UniversalDownloaderPlus")`; finished downloads persist to `downloads_history.json` and are rebuilt as rows on next launch. |

> [!NOTE]
> **On ad-blocking:** the repository contains an `AdBlockInterceptor` class and an ad-skip JS payload,
> but both are **currently disabled in code** — they broke YouTube playback and `yt-dlp` compatibility.
> The wiring is commented out in `MainWindow.__init__`. Treat ad-blocking as *planned*, not shipped.

---

## 🏗️ Architecture

```text
Universal-Downloader-Plus/
├── .github/workflows/desktop-release.yml  # Tag-triggered 3-OS build → signed → GitHub Release
├── android/                               # Expo/React Native app + FastAPI backend
├── desktop/
│   ├── src/universal_downloader/
│   │   ├── __init__.py                    # __version__ = "1.0.0"
│   │   ├── __main__.py                    # python -m universal_downloader
│   │   ├── qt_app.py                      # UI, workers, settings, history, cookie bridge
│   │   └── resources/                     # icon.ico, icon.icns, icon-*.png
│   ├── ffmpeg/                            # Local binaries (git-ignored except README.md)
│   ├── tests/test_smoke.py
│   ├── pyproject.toml                     # setuptools; console script + deps
│   └── requirements.txt
```

### Threading model

The GUI thread never blocks on network I/O. Two `QThread` subclasses do the work:

- **`AnalyzeWorker`** — resolves metadata (`skip_download: True`) and fetches the thumbnail, then emits
  `result` or `playlist`.
- **`DownloadWorker`** — runs `yt-dlp` with a progress hook. Pause is a flag-poll loop inside the hook;
  cancel raises inside the hook to unwind the download promptly.

---

## 🎬 FFmpeg & the Binary Pipeline

Merging separate video + audio streams, transcoding to MP3, and embedding cover art **all** require
`ffmpeg` and `ffprobe`. Rather than demand a system-wide install, the app resolves them relative to
itself — and the rules differ between a source checkout and a frozen build.

### Two anchors, two jobs

`qt_app.py` defines **two independent** path anchors. Mixing them up is the usual cause of
"works in dev, broken in the .exe":

```python
def get_base_dir() -> str:
    if getattr(sys, "frozen", False):
        # PyInstaller executable: binaries sit next to the exe.
        return os.path.dirname(sys.executable)
    # Source checkout: up two levels from src/universal_downloader/qt_app.py
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


BASE_DIR = get_base_dir()
FFMPEG_DIR = os.path.join(BASE_DIR, "ffmpeg")


def get_resource_path(filename: str) -> str:
    """Resolve a file path inside the bundled resources/ folder."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base = sys._MEIPASS
    else:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources")
    return os.path.join(base, filename)
```

| Anchor | Dev mode | Frozen (PyInstaller) | Used for |
|---|---|---|---|
| `BASE_DIR` → `FFMPEG_DIR` | `<desktop>/ffmpeg/` | `<dir of the .exe>/ffmpeg/` | `ffmpeg`, `ffprobe`, `ffplay` |
| `get_resource_path()` | `src/universal_downloader/resources/` | `sys._MEIPASS/resources/` | `icon.ico`, `icon.icns`, PNGs |

**Why they differ:** `sys._MEIPASS` is a temp directory that PyInstaller unpacks and then *deletes on
exit*. Icons are read once at startup, so serving them from there is fine. FFmpeg is a long-lived
executable invoked by a subprocess, so it is staged **next to the binary** instead — which is exactly
what the release workflow's "Stage FFmpeg beside binary" step does.

`FFMPEG_DIR` is then threaded into every yt-dlp invocation as `ffmpeg_location`, so post-processing
uses the bundled build rather than whatever may be on `PATH`.

### Getting FFmpeg for local development

The `ffmpeg/` directory is git-ignored (binaries don't belong in git); only its `README.md` is tracked.
Drop the executables in yourself:

```bash
# Windows  → ffmpeg/ffmpeg.exe, ffmpeg/ffprobe.exe, ffmpeg/ffplay.exe   (gyan.dev "essentials" build)
# macOS    → brew install ffmpeg && cp "$(brew --prefix ffmpeg)/bin/"{ffmpeg,ffprobe,ffplay} ffmpeg/
# Linux    → BtbN static build → ffmpeg/ffmpeg, ffmpeg/ffprobe, ffmpeg/ffplay
```

CI does this automatically per-OS, so release artifacts always ship with FFmpeg (including `ffplay`)
included — in the `.exe`/`.msi`/`.deb`/`.dmg` installers as well as the portable build.

---

## 🖥️ Terminal Preview

Real stdout from the app. Every prefix below (`[Browser]`, `[Cookies]`, `[DownloadWorker]`,
`[yt-dlp]`, `Final merged path:`) is emitted by `qt_app.py`.

### 1 · Startup & persistent profile

```console
$ python -m universal_downloader
[Browser] Persistent profile at: C:/Users/alok/AppData/Roaming/Alok/UniversalDownloaderPlus\browser_data (off-the-record=False)
[Cookies] jar initialised → ...\UniversalDownloaderPlus\yt_dlp_cookies.txt
Restored 6 download(s) from history.
Ready.
```

### 2 · Playlist detection & staggered queue

```console
[Browser Download] Triggering analysis for: https://www.youtube.com/playlist?list=PLxA687tYuMWg
Analyzing: https://www.youtube.com/playlist?list=PLxA687tYuMWg
[youtube:tab] Extracting playlist: Lo-Fi Beats to Ship Code To (24 videos)

  ┌─ Playlist detected ─────────────────────────────────┐
  │  24 videos found. Download all, or just the first?  │
  └─────────────────────────────────────────────────────┘

[queue] [Lo-Fi Beats · 24] 01 - Midnight Compile      → worker spawned (t+0ms)
[queue] [Lo-Fi Beats · 24] 02 - Garbage Collection    → worker spawned (t+350ms)
[queue] [Lo-Fi Beats · 24] 03 - Null Pointer Sunset   → worker spawned (t+700ms)
● 3 active · 21 queued          (max_concurrent_downloads = 3)
```

### 3 · Download progress, 403 self-heal & ID3 embedding

```console
[download]  38.2% of 47.31MiB at  4.12MB/s ETA 00:07
[download]  76.9% of 47.31MiB at  5.03MB/s ETA 00:02
[download] 100% of 47.31MiB in 00:00:11 at 4.41MB/s

[DownloadWorker] 403 received — flushing cipher cache and retrying once.
[yt-dlp] Signature cache flushed.
[download] resumed → 100% of 47.31MiB

[ffmpeg] Merging formats into "Midnight Compile_[1080p].mp4"
[ffmpeg] Adding thumbnail to "Midnight Compile_[1080p].mp4"
[Metadata] Writing ID3 tags (title, artist, date, cover art)
Final merged path: D:\Media\Downloads\Midnight Compile_[1080p].mp4
✔ Completed  ·  1080p60  ·  47.3 MB
```

---

## 🔧 Installation & Local Setup

**Prerequisites:** Python **3.10+**, `git`, and the FFmpeg binaries described [above](#getting-ffmpeg-for-local-development).

```bash
# 1. Clone
git clone https://github.com/al0k-commits/Universal-Downloader-Plus.git
cd Universal-Downloader-Plus/desktop

# 2. Virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Editable install (pulls PyQt6, PyQt6-WebEngine, yt-dlp, qtawesome, …)
pip install -e .

# 4. Run
python -m universal_downloader
```

The package also installs a console script:

```bash
universal-downloader
```

<details>
<summary><b>Building a standalone binary locally</b></summary>

```bash
pip install pyinstaller

# Windows (use ';' as the --add-data separator)
python -m PyInstaller --noconfirm --clean --onefile --windowed \
  --name UniversalDownloader \
  --icon src/universal_downloader/resources/icon.ico \
  --add-data "src/universal_downloader/resources;resources" \
  --collect-all PyQt6.QtWebEngineWidgets \
  --collect-all PyQt6.QtWebEngineCore \
  src/universal_downloader/__main__.py

# Then stage FFmpeg beside the produced binary:
mkdir -p dist/ffmpeg && cp ffmpeg/* dist/ffmpeg/
```

On macOS/Linux swap the `--add-data` separator to `:`. macOS ignores `--onefile` for windowed apps and
produces a `.app` bundle regardless — stage FFmpeg into `Contents/MacOS/ffmpeg`.

There is no checked-in `.spec` file: builds are driven entirely by the CLI flags above, which is also
exactly what [`desktop-release.yml`](../.github/workflows/desktop-release.yml) runs. Any `*.spec`
PyInstaller generates locally is git-ignored.

</details>

---

## ⚙️ Configuration & Preferences

Open **Settings** (the sliders icon, top-right). Everything persists through
`QSettings("Alok", "UniversalDownloaderPlus")` — the registry on Windows, `~/Library/Preferences` on
macOS, `~/.config` on Linux.

| Key | Default | Description |
|---|---|---|
| `download_dir` | OS Downloads folder | Output root. Every `outtmpl` is rewritten beneath it. |
| `max_concurrent_downloads` | `3` | Simultaneous workers (1–10). Extras queue. |
| `preferred_format` | `Always Ask` | `Always Ask` opens the modal; the *Best Video (MP4)* / *Best Audio (MP3)* presets enable Smart Mode. |
| `theme` | `dark` | `dark` / `light`. Applied before first paint on launch. |

### On-disk state

All under the per-user app-data directory (`%APPDATA%\Alok\UniversalDownloaderPlus` on Windows):

```text
browser_data/               Chromium profile — cookies, localStorage, cache
yt_dlp_cookies.txt          Netscape jar, written live from the cookie store
yt_dlp_cookies_clean.txt    Auto-filtered jar (see below)
downloads_history.json      Finished/cancelled rows, newest 300
thumbnails/<id>.jpg         Cached artwork for restored history rows
```

> [!IMPORTANT]
> **Cookie hygiene.** Anonymous *visitor* cookies (`VISITOR_INFO1_LIVE`, `YSC`, `GPS`, …) bind
> googlevideo stream URLs to a GVS PO Token the app cannot mint. Metadata still resolves, so the
> failure surfaces only mid-download as `HTTP Error 403: Forbidden`. The app therefore strips those
> cookies when you are signed out, and passes the jar through **untouched** once real login cookies
> (`SID`/`SAPISID`/`__Secure-*PSID`) are present. **Signing in inside the embedded browser is the single
> most effective fix for 403s.**

---

## 🤝 Contributing

Issues and PRs are welcome.

```bash
pip install -e ".[dev]"
pytest
```

1. Fork and branch (`git checkout -b feat/my-feature`).
2. Match the existing style: 4-space indent, ~79-col soft wrap, docstrings on non-obvious functions.
3. Keep UI colours/radii referencing the design tokens in `qt_app.py` (`ACCENT`, `RADIUS_CARD`, …)
   rather than hardcoding hex values, so both themes stay consistent.
4. Confirm the app still launches (`python -m universal_downloader`) before opening the PR.

### Releasing

Tag and push — CI builds, signs, and publishes all three platforms:

```bash
git tag v1.0.0
git push origin v1.0.0
```

---

## ⚖️ License & Disclaimer

Released under the [MIT License](LICENSE).

This tool is intended for downloading content you own or that is licensed for reuse. Respect the terms
of service of the sites you access and applicable copyright law. The authors accept no responsibility
for misuse.

<div align="center">
<sub>Built with PyQt6 · yt-dlp · FFmpeg — by <a href="https://github.com/al0k-commits">Alok</a></sub>
</div>
