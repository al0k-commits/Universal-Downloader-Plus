"""
Universal Downloader+ — 4K Video Downloader+ style desktop app.

PyQt6 + QWebEngine in-app browser with ad-blocking, download manager,
detailed format-selection modal, pause/resume/cancel via yt-dlp hooks.

Requires ffmpeg.exe / ffprobe.exe / yt-dlp.exe inside the ffmpeg/ folder at the
repository root (dev) or next to the frozen executable (PyInstaller build).
"""

import os
import re
import sys
import time
import ctypes

import requests
import yt_dlp
import qtawesome as qta
import qdarktheme

from PyQt6.QtCore import Qt, QUrl, QThread, pyqtSignal, QSize
from PyQt6.QtGui import QPixmap, QClipboard, QIcon
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QDialog, QLabel, QPushButton,
    QLineEdit, QComboBox, QCheckBox, QProgressBar, QFileDialog, QFrame,
    QVBoxLayout, QHBoxLayout, QGridLayout, QStackedWidget, QScrollArea,
    QRadioButton, QButtonGroup, QSizePolicy, QMessageBox, QToolButton,
)
from PyQt6.QtWebEngineCore import (
    QWebEngineUrlRequestInterceptor, QWebEngineProfile, QWebEngineScript,
)
from PyQt6.QtWebEngineWidgets import QWebEngineView


# ============================================================================
# Constants & helpers
# ============================================================================

def get_base_dir() -> str:
    if getattr(sys, "frozen", False):
        # Running as a PyInstaller executable: binaries sit next to the exe.
        return os.path.dirname(sys.executable)
    # Running from source: traverse up from
    # src/universal_downloader/qt_app.py to the repository root.
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


BASE_DIR = get_base_dir()

# Application assets (icon.png, icon.ico, ...) live in resources/.
# In dev this is src/universal_downloader/resources; when frozen by PyInstaller
# the bundled folder is extracted to sys._MEIPASS/resources.
def get_resource_path(filename: str) -> str:
    """Resolve a file path inside the bundled resources/ folder."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base = sys._MEIPASS
    else:
        # __file__ is .../src/universal_downloader/qt_app.py; resources/ is a
        # sibling of this module inside the same package directory.
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources")
    return os.path.join(base, filename)


# Third-party binaries (ffmpeg.exe, ffprobe.exe, yt-dlp.exe) live here.
# In dev this is <repo_root>/ffmpeg; when frozen it is <exe_dir>/ffmpeg.
FFMPEG_DIR = os.path.join(BASE_DIR, "ffmpeg")

AD_DOMAINS = (
    "doubleclick.net", "googlesyndication.com", "googleadservices.com",
    "google-analytics.com", "adservice.google.com", "googletagmanager.com",
    "googletagservices.com", "ads.youtube.com", "adnxs.com", "taboola.com",
    "outbrain.com", "scorecardresearch.com", "moatads.com", "adsafeprotected.com",
    "amazon-adsystem.com", "criteo.com", "pubmatic.com", "rubiconproject.com",
)

AD_PATH_MARKERS = ("/pagead", "/ptracking", "/ad_break", "/api/stats/ads")

PLATFORMS = [
    ("YouTube", "https://www.youtube.com", "#FF0000", "fa5b.youtube"),
    ("SoundCloud", "https://soundcloud.com", "#FF5500", "fa5b.soundcloud"),
    ("Facebook", "https://www.facebook.com/watch", "#1877F2", "fa5b.facebook"),
    ("Vimeo", "https://vimeo.com/watch", "#1AB7EA", "fa5b.vimeo-v"),
    ("TikTok", "https://www.tiktok.com", "#EE1D52", "fa5b.tiktok"),
    ("Twitch", "https://www.twitch.tv", "#9146FF", "fa5b.twitch"),
    ("Bilibili", "https://www.bilibili.com", "#00A1D6", "fa5s.play-circle"),
    ("Dailymotion", "https://www.dailymotion.com", "#0066DC", "fa5s.video"),
]


def safe_icon(name: str, color: str):
    """qtawesome icon with graceful fallback if the glyph is missing."""
    try:
        return qta.icon(name, color=color)
    except Exception:
        return qta.icon("fa5s.globe", color=color)

VIDEO_URL_RE = re.compile(
    r"(youtube\.com/(watch|shorts|playlist)|youtu\.be/"
    r"|vimeo\.com/\d+"
    r"|tiktok\.com/@[^/]+/video"
    r"|twitch\.tv/videos"
    r"|soundcloud\.com/[^/]+/[^/]+"
    r"|facebook\.com/(watch|reel|.+/videos)"
    r"|bilibili\.com/video"
    r"|dailymotion\.com/video)",
    re.IGNORECASE,
)

AD_SKIP_JS = r"""
(function() {
    if (window.__udl_adskip) return;
    window.__udl_adskip = true;
    setInterval(function() {
        // Skip button variants
        var skip = document.querySelector(
            '.ytp-ad-skip-button, .ytp-ad-skip-button-modern, ' +
            '.ytp-skip-ad-button, button.ytp-ad-skip-button-container'
        );
        if (skip) skip.click();
        // Fast-forward unskippable ads, muted
        var adVideo = document.querySelector('.ad-showing video, .ad-interrupting video');
        if (adVideo) {
            adVideo.muted = true;
            if (isFinite(adVideo.duration) && adVideo.duration > 0) {
                adVideo.currentTime = adVideo.duration;
            }
        }
        // Close ad overlays / popups
        var closers = document.querySelectorAll(
            '.ytp-ad-overlay-close-button, #dismiss-button button'
        );
        closers.forEach(function(b) { b.click(); });
        document.querySelectorAll('tp-yt-iron-overlay-backdrop').forEach(
            function(e) { e.remove(); }
        );
    }, 500);
})();
"""

GREEN = "#2ea043"
GREEN_HOVER = "#3fb950"

# Layout/shape-only QSS layered on top of qdarktheme's base colors.
# Green accent buttons are forced explicitly (central to the 4K aesthetic).
CUSTOM_QSS = """
QLineEdit#urlbox { font-size: 14px; padding: 9px 16px; border-radius: 8px; }
QPushButton#green {
    background-color: #2ea043; border: none; color: white;
    font-weight: bold; border-radius: 8px; padding: 7px 16px;
}
QPushButton#green:hover { background-color: #3fb950; }
QPushButton#green:pressed { background-color: #26843a; }
QPushButton#green:disabled { background-color: #21502c; color: gray; }
QPushButton#danger {
    background-color: #c93c3c; border: none; color: white;
    border-radius: 8px; padding: 7px 16px;
}
QPushButton#danger:hover { background-color: #e04b4b; }
QPushButton#navtab {
    background: transparent; border: none; padding: 8px 16px;
    font-weight: bold; border-radius: 0;
}
QPushButton#navtab:checked { border-bottom: 2px solid #2ea043; }
QToolButton#service {
    border-radius: 15px; font-size: 14px; font-weight: bold; padding: 12px;
    border: 1px solid rgba(128, 128, 128, 0.25);
    background-color: rgba(128, 128, 128, 0.07);
}
QToolButton#service:hover {
    background-color: rgba(128, 128, 128, 0.18);
    border-color: rgba(128, 128, 128, 0.45);
}
QToolButton#service:pressed { background-color: rgba(128, 128, 128, 0.28); }
QProgressBar { border-radius: 5px; height: 10px; color: transparent; }
QProgressBar::chunk { background-color: #2ea043; border-radius: 5px; }
QFrame#card { border-radius: 10px; }
"""


def human_size(n) -> str:
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_duration(seconds) -> str:
    if not seconds:
        return "?"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def quality_name(height) -> str:
    return {4320: "8K", 2160: "4K", 1440: "2K", 1080: "1080p",
            720: "720p", 480: "480p", 360: "360p", 240: "240p",
            144: "144p"}.get(height, f"{height}p")


class DownloadCancelled(Exception):
    pass


# ============================================================================
# Ad blocker
# ============================================================================

class AdBlockInterceptor(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info):
        url = info.requestUrl()
        host = url.host().lower()
        path = url.path().lower()
        if any(host == d or host.endswith("." + d) for d in AD_DOMAINS):
            info.block(True)
            return
        if any(m in path for m in AD_PATH_MARKERS):
            info.block(True)


# ============================================================================
# Workers
# ============================================================================

class AnalyzeWorker(QThread):
    """Fetch metadata + thumbnail bytes off the UI thread."""
    result = pyqtSignal(dict, bytes)
    error = pyqtSignal(str)

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        try:
            opts = {"skip_download": True, "quiet": True, "no_warnings": True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(self.url, download=False)

            display = info
            if info.get("_type") == "playlist" and info.get("entries"):
                entries = [e for e in info["entries"] if e]
                if entries:
                    display = entries[0]

            thumb = b""
            thumb_url = display.get("thumbnail")
            if thumb_url:
                try:
                    r = requests.get(thumb_url, timeout=10)
                    r.raise_for_status()
                    thumb = r.content
                except Exception:
                    thumb = b""

            self.result.emit(info, thumb)
        except yt_dlp.utils.DownloadError as e:
            self.error.emit(str(e).replace("ERROR:", "").strip())
        except Exception as e:
            self.error.emit(str(e))


class DownloadWorker(QThread):
    """Runs yt-dlp; pause via flag loop, cancel via exception in hook."""
    progress = pyqtSignal(dict)
    done = pyqtSignal(bool, str)

    def __init__(self, url: str, ydl_opts: dict):
        super().__init__()
        self.url = url
        self.ydl_opts = dict(ydl_opts)
        self.is_paused = False
        self.is_cancelled = False

    def _hook(self, d: dict):
        if self.is_cancelled:
            raise DownloadCancelled("Download cancelled by user")
        while self.is_paused:
            time.sleep(0.4)
            if self.is_cancelled:
                raise DownloadCancelled("Download cancelled by user")

        status = d.get("status")
        if status == "downloading":
            downloaded = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            self.progress.emit({
                "fraction": (downloaded / total) if total else 0.0,
                "speed": d.get("speed"),
                "eta": d.get("eta"),
                "downloaded": downloaded,
                "total": total,
            })
        elif status == "finished":
            self.progress.emit({"fraction": 1.0, "processing": True})

    def run(self):
        try:
            opts = self.ydl_opts
            opts["progress_hooks"] = [self._hook]
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([self.url])
            self.done.emit(True, "Completed")
        except DownloadCancelled:
            self.done.emit(False, "Cancelled")
        except yt_dlp.utils.DownloadError as e:
            if "cancelled by user" in str(e).lower():
                self.done.emit(False, "Cancelled")
            else:
                self.done.emit(False, str(e).replace("ERROR:", "").strip()[:200])
        except Exception as e:
            self.done.emit(False, str(e)[:200])


# ============================================================================
# Download modal dialog
# ============================================================================

class DownloadModal(QDialog):
    def __init__(self, parent, info: dict, thumb: bytes, save_dir: str):
        super().__init__(parent)
        self.info = info
        self.display = info
        if info.get("_type") == "playlist" and info.get("entries"):
            entries = [e for e in info["entries"] if e]
            if entries:
                self.display = entries[0]
        self.save_dir = save_dir
        self.selected_opts = None
        self.selected_kind = "video"

        self.setWindowTitle("Download")
        self.setMinimumSize(640, 620)
        self._build_ui(thumb)
        self._repopulate_formats()

    def _build_ui(self, thumb: bytes):
        root = QVBoxLayout(self)
        root.setSpacing(10)

        # --- Header: thumbnail + title + duration ---
        head = QHBoxLayout()
        self.thumb_label = QLabel()
        self.thumb_label.setFixedSize(200, 112)
        self.thumb_label.setStyleSheet(
            "background-color: rgba(128,128,128,0.15); border-radius: 8px;")
        self.thumb_label.setScaledContents(True)
        if thumb:
            pix = QPixmap()
            pix.loadFromData(thumb)
            self.thumb_label.setPixmap(pix)
        head.addWidget(self.thumb_label)

        meta_col = QVBoxLayout()
        title = self.info.get("title") or self.display.get("title", "Unknown")
        if self.info.get("_type") == "playlist":
            count = len([e for e in self.info.get("entries") or [] if e])
            title = f"[Playlist · {count} videos] {title}"
        t = QLabel(title)
        t.setWordWrap(True)
        t.setStyleSheet("font-size: 15px; font-weight: bold;")
        meta_col.addWidget(t)
        dur = QLabel("Duration: " + human_duration(self.display.get("duration")))
        dur.setStyleSheet("color: gray;")
        meta_col.addWidget(dur)
        meta_col.addStretch()
        head.addLayout(meta_col, 1)
        root.addLayout(head)

        # --- Option dropdown grid ---
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)

        self.type_combo = QComboBox()
        self.type_combo.addItems(["Video", "Audio"])
        self.container_combo = QComboBox()
        self.container_combo.addItems(["Auto", "MP4", "MKV", "MP3"])
        self.codec_combo = QComboBox()
        self.codec_combo.addItems(["Auto", "H264", "AV01", "VP9"])
        self.fps_combo = QComboBox()
        self.fps_combo.addItems(["Highest", "60fps", "30fps"])
        self.subs_combo = QComboBox()
        self.subs_combo.addItems(["None", "Embedded", "Auto-generated"])

        for col, (label, combo) in enumerate([
            ("Type", self.type_combo), ("Container", self.container_combo),
            ("Codec", self.codec_combo), ("Frame Rate", self.fps_combo),
            ("Subtitles", self.subs_combo),
        ]):
            lab = QLabel(label)
            lab.setStyleSheet("color: gray; font-size: 11px;")
            grid.addWidget(lab, 0, col)
            grid.addWidget(combo, 1, col)

        self.type_combo.currentTextChanged.connect(self._repopulate_formats)
        self.codec_combo.currentTextChanged.connect(self._repopulate_formats)
        self.fps_combo.currentTextChanged.connect(self._repopulate_formats)
        root.addLayout(grid)

        # --- Format radio list ---
        self.fmt_group = QButtonGroup(self)
        self.fmt_scroll = QScrollArea()
        self.fmt_scroll.setWidgetResizable(True)
        self.fmt_container = QWidget()
        self.fmt_layout = QVBoxLayout(self.fmt_container)
        self.fmt_layout.setSpacing(2)
        self.fmt_layout.addStretch()
        self.fmt_scroll.setWidget(self.fmt_container)
        root.addWidget(self.fmt_scroll, 1)

        # --- Action buttons ---
        actions = QHBoxLayout()
        actions.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        dl_btn = QPushButton("Download")
        dl_btn.setObjectName("green")
        dl_btn.setMinimumWidth(140)
        dl_btn.clicked.connect(self._accept_download)
        actions.addWidget(cancel_btn)
        actions.addWidget(dl_btn)
        root.addLayout(actions)

    # -- format list -------------------------------------------------------
    def _clear_formats(self):
        for btn in self.fmt_group.buttons():
            self.fmt_group.removeButton(btn)
            btn.deleteLater()
        while self.fmt_layout.count() > 1:
            item = self.fmt_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _best_audio_size(self) -> int:
        best = 0
        for f in self.display.get("formats") or []:
            if f.get("acodec") not in (None, "none") and \
               f.get("vcodec") in (None, "none"):
                size = f.get("filesize") or f.get("filesize_approx") or 0
                best = max(best, size)
        return best

    def _repopulate_formats(self, *_):
        self._clear_formats()
        formats = self.display.get("formats") or []
        is_audio = self.type_combo.currentText() == "Audio"
        codec_filter = self.codec_combo.currentText()
        fps_filter = self.fps_combo.currentText()
        audio_size = self._best_audio_size()

        rows = []
        if is_audio:
            for f in formats:
                if f.get("acodec") in (None, "none") or \
                   f.get("vcodec") not in (None, "none"):
                    continue
                abr = f.get("abr") or 0
                size = f.get("filesize") or f.get("filesize_approx")
                label = (f"{abr:.0f} kbps    {(f.get('ext') or '?').upper()}"
                         f"    {(f.get('acodec') or '?').split('.')[0]}"
                         f"    {human_size(size)}")
                rows.append((abr, label, f))
            rows.sort(key=lambda r: -r[0])
        else:
            codec_prefix = {"H264": "avc", "AV01": "av01", "VP9": "vp"}.get(
                codec_filter)
            seen = set()
            for f in formats:
                if f.get("vcodec") in (None, "none") or not f.get("height"):
                    continue
                vcodec = (f.get("vcodec") or "").lower()
                if codec_prefix and not vcodec.startswith(codec_prefix):
                    continue
                fps = f.get("fps") or 0
                if fps_filter == "60fps" and fps < 50:
                    continue
                if fps_filter == "30fps" and fps > 35:
                    continue
                h = f["height"]
                codec_short = vcodec.split(".")[0]
                key = (h, codec_short, round(fps))
                if key in seen:
                    continue
                seen.add(key)
                size = f.get("filesize") or f.get("filesize_approx") or 0
                total = size + audio_size if size else None
                label = (f"{quality_name(h):<7}  "
                         f"{(f.get('ext') or '?').upper():<5}  "
                         f"{codec_short:<6}  {fps:.0f}fps  "
                         f"~{human_size(total)}")
                rows.append((h * 1000 + fps, label, f))
            rows.sort(key=lambda r: -r[0])

        for i, (_, label, f) in enumerate(rows[:24]):
            rb = QRadioButton(label)
            rb.setStyleSheet("font-family: Consolas, monospace;")
            rb.setProperty("fmt", f)
            self.fmt_group.addButton(rb)
            self.fmt_layout.insertWidget(self.fmt_layout.count() - 1, rb)
            if i == 0:
                rb.setChecked(True)

    # -- accept ------------------------------------------------------------
    def _accept_download(self):
        checked = self.fmt_group.checkedButton()
        fmt = checked.property("fmt") if checked else None
        is_audio = self.type_combo.currentText() == "Audio"
        container = self.container_combo.currentText()
        subs = self.subs_combo.currentText()
        is_playlist = self.info.get("_type") == "playlist"

        outtmpl = os.path.join(self.save_dir, "%(title)s.%(ext)s")
        if is_playlist:
            outtmpl = os.path.join(
                self.save_dir, "%(playlist_title)s",
                "%(playlist_index)s - %(title)s.%(ext)s")

        opts = {
            "outtmpl": outtmpl,
            "ffmpeg_location": FFMPEG_DIR,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": not is_playlist,
            "ignoreerrors": is_playlist,
        }

        if is_audio:
            opts["format"] = (f"{fmt['format_id']}/bestaudio/best"
                              if fmt else "bestaudio/best")
            codec = "mp3" if container in ("Auto", "MP3") else container.lower()
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": codec if codec in ("mp3",) else "mp3",
                "preferredquality": "192",
            }]
            self.selected_kind = "audio"
        else:
            if fmt:
                opts["format"] = f"{fmt['format_id']}+bestaudio/best"
            else:
                opts["format"] = "bestvideo+bestaudio/best"
            merge = {"MP4": "mp4", "MKV": "mkv"}.get(container, "mp4")
            opts["merge_output_format"] = merge
            self.selected_kind = "playlist" if is_playlist else "video"

            if subs != "None":
                opts["writesubtitles"] = True
                if subs == "Auto-generated":
                    opts["writeautomaticsub"] = True
                opts["subtitleslangs"] = ["en"]
                opts["postprocessors"] = [{"key": "FFmpegEmbedSubtitle"}]

        self.selected_opts = opts
        self.accept()


# ============================================================================
# Download manager item
# ============================================================================

class DownloadItem(QFrame):
    def __init__(self, title: str, meta: str, thumb: bytes, kind: str,
                 worker: DownloadWorker):
        super().__init__()
        self.setObjectName("card")
        self.kind = kind
        self.worker = worker

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)

        thumb_label = QLabel()
        thumb_label.setFixedSize(120, 68)
        thumb_label.setScaledContents(True)
        thumb_label.setStyleSheet(
            "background-color: rgba(128,128,128,0.15); border-radius: 6px;")
        if thumb:
            pix = QPixmap()
            pix.loadFromData(thumb)
            thumb_label.setPixmap(pix)
        lay.addWidget(thumb_label)

        mid = QVBoxLayout()
        self.title_label = QLabel(title)
        self.title_label.setStyleSheet("font-weight: bold;")
        self.title_label.setWordWrap(True)
        mid.addWidget(self.title_label)

        self.meta_label = QLabel(meta)
        self.meta_label.setStyleSheet("color: gray; font-size: 11px;")
        mid.addWidget(self.meta_label)

        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setFixedHeight(8)
        mid.addWidget(self.bar)

        self.stat_label = QLabel("Queued...")
        self.stat_label.setStyleSheet("color: gray; font-size: 11px;")
        mid.addWidget(self.stat_label)
        lay.addLayout(mid, 1)

        btns = QVBoxLayout()
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setFixedWidth(90)
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("danger")
        self.cancel_btn.setFixedWidth(90)
        self.cancel_btn.clicked.connect(self.cancel)
        btns.addWidget(self.pause_btn)
        btns.addWidget(self.cancel_btn)
        lay.addLayout(btns)

        worker.progress.connect(self.on_progress)
        worker.done.connect(self.on_done)

    def toggle_pause(self):
        self.worker.is_paused = not self.worker.is_paused
        if self.worker.is_paused:
            self.pause_btn.setText("Resume")
            self.stat_label.setText("Paused")
        else:
            self.pause_btn.setText("Pause")
            self.stat_label.setText("Resuming...")

    def cancel(self):
        self.worker.is_cancelled = True
        self.worker.is_paused = False
        self.stat_label.setText("Cancelling...")

    def on_progress(self, d: dict):
        self.bar.setValue(int(d.get("fraction", 0) * 1000))
        if d.get("processing"):
            self.stat_label.setText("Processing with ffmpeg...")
            return
        speed = d.get("speed")
        eta = d.get("eta")
        parts = [f"{d.get('fraction', 0) * 100:.1f}%"]
        if d.get("total"):
            parts.append(f"{human_size(d['downloaded'])} / "
                         f"{human_size(d['total'])}")
        if speed:
            parts.append(f"{speed / 1048576:.2f} MB/s")
        if eta:
            parts.append(f"ETA {eta}s")
        self.stat_label.setText("  ·  ".join(parts))

    def on_done(self, ok: bool, msg: str):
        self.pause_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        if ok:
            self.bar.setValue(1000)
            self.stat_label.setText("✅ " + msg)
            self.stat_label.setStyleSheet(
                "color: #3fb950; font-size: 11px; font-weight: bold;")
        else:
            self.stat_label.setText("❌ " + msg)
            self.stat_label.setStyleSheet("color: #ff5555; font-size: 11px;")


# ============================================================================
# Pages
# ============================================================================

class HomePage(QWidget):
    open_url = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.addStretch(1)

        title = QLabel("Choose a service or paste a link")
        title.setStyleSheet("font-size: 21px; font-weight: bold;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(title)

        subtitle = QLabel("Videos, playlists, channels and audio from your "
                          "favorite platforms")
        subtitle.setStyleSheet("color: gray; font-size: 13px;")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(subtitle)
        lay.addSpacing(32)

        # Centered grid: stretch columns either side + stretch rows around
        grid_holder = QHBoxLayout()
        grid_holder.addStretch(1)
        grid = QGridLayout()
        grid.setSpacing(18)
        for i, (name, url, color, icon_name) in enumerate(PLATFORMS):
            btn = QToolButton()
            btn.setObjectName("service")
            btn.setText(name)
            btn.setIcon(safe_icon(icon_name, color))
            btn.setIconSize(QSize(44, 44))
            btn.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            btn.setFixedSize(160, 120)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, u=url: self.open_url.emit(u))
            grid.addWidget(btn, i // 4, i % 4,
                           Qt.AlignmentFlag.AlignCenter)
        grid_holder.addLayout(grid)
        grid_holder.addStretch(1)
        lay.addLayout(grid_holder)
        lay.addStretch(2)


class BrowserPage(QWidget):
    request_download = pyqtSignal(str)

    def __init__(self, profile: QWebEngineProfile):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Nav bar
        nav = QHBoxLayout()
        nav.setContentsMargins(8, 8, 8, 8)
        self.back_btn = QPushButton("◀")
        self.fwd_btn = QPushButton("▶")
        self.reload_btn = QPushButton("↻")
        self.home_btn = QPushButton("⌂")
        for b in (self.back_btn, self.fwd_btn, self.reload_btn, self.home_btn):
            b.setFixedSize(34, 30)
            nav.addWidget(b)

        self.addr = QLineEdit()
        self.addr.setPlaceholderText("Enter URL or search...")
        nav.addWidget(self.addr, 1)
        lay.addLayout(nav)

        self.view = QWebEngineView(profile, self)
        lay.addWidget(self.view, 1)

        # Floating download button
        self.dl_btn = QPushButton("⬇  Download", self)
        self.dl_btn.setObjectName("green")
        self.dl_btn.setFixedSize(180, 44)
        self.dl_btn.hide()
        self.dl_btn.clicked.connect(
            lambda: self.request_download.emit(self.view.url().toString()))

        # Wiring
        self.back_btn.clicked.connect(self.view.back)
        self.fwd_btn.clicked.connect(self.view.forward)
        self.reload_btn.clicked.connect(self.view.reload)
        self.home_btn.clicked.connect(
            lambda: self.navigate("https://www.youtube.com"))
        self.addr.returnPressed.connect(self._addr_entered)
        self.view.urlChanged.connect(self._url_changed)

        self.navigate("https://www.youtube.com")

    def navigate(self, url: str):
        if not url.startswith(("http://", "https://")):
            if "." in url and " " not in url:
                url = "https://" + url
            else:
                url = ("https://www.youtube.com/results?search_query="
                       + requests.utils.quote(url))
        self.view.setUrl(QUrl(url))

    def _addr_entered(self):
        self.navigate(self.addr.text().strip())

    def _url_changed(self, qurl: QUrl):
        url = qurl.toString()
        self.addr.setText(url)
        if VIDEO_URL_RE.search(url):
            self.dl_btn.show()
            self.dl_btn.raise_()
        else:
            self.dl_btn.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.dl_btn.move((self.width() - self.dl_btn.width()) // 2,
                         self.height() - self.dl_btn.height() - 24)


class DownloadsPage(QWidget):
    TABS = ["All", "Video", "Audio", "Playlists", "Channels", "Subscriptions"]

    def __init__(self):
        super().__init__()
        self.items = []

        lay = QVBoxLayout(self)
        tab_row = QHBoxLayout()
        tab_row.setContentsMargins(8, 8, 8, 0)
        self.tab_btns = []
        for name in self.TABS:
            b = QPushButton(name)
            b.setObjectName("navtab")
            b.setCheckable(True)
            b.clicked.connect(lambda _, n=name: self.set_filter(n))
            tab_row.addWidget(b)
            self.tab_btns.append(b)
        tab_row.addStretch()
        self.tab_btns[0].setChecked(True)
        lay.addLayout(tab_row)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.list_holder = QWidget()
        self.list_lay = QVBoxLayout(self.list_holder)
        self.list_lay.setSpacing(8)
        self.list_lay.addStretch()
        self.scroll.setWidget(self.list_holder)
        lay.addWidget(self.scroll, 1)

        self.empty_label = QLabel("No downloads yet.")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setStyleSheet("color: gray; padding: 30px;")
        self.list_lay.insertWidget(0, self.empty_label)

        self.current_filter = "All"

    def add_item(self, item: DownloadItem):
        self.empty_label.hide()
        self.items.append(item)
        self.list_lay.insertWidget(self.list_lay.count() - 1, item)
        self.set_filter(self.current_filter)

    def set_filter(self, name: str):
        self.current_filter = name
        for b in self.tab_btns:
            b.setChecked(b.text() == name)
        kind_map = {"Video": "video", "Audio": "audio",
                    "Playlists": "playlist", "Channels": "channel",
                    "Subscriptions": "subscription"}
        for item in self.items:
            visible = (name == "All") or (item.kind == kind_map.get(name))
            item.setVisible(visible)


# ============================================================================
# Main window
# ============================================================================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Universal Downloader+")
        self.resize(1180, 760)

        self.save_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        self.workers = []           # keep refs so QThreads aren't GC'd
        self.analyze_worker = None
        self.dark = True

        # --- Web profile with ad blocking + ad-skip JS ---
        self.interceptor = AdBlockInterceptor()
        self.profile = QWebEngineProfile("udl", self)
        self.profile.setUrlRequestInterceptor(self.interceptor)
        script = QWebEngineScript()
        script.setName("adskip")
        script.setSourceCode(AD_SKIP_JS)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(True)
        self.profile.scripts().insert(script)

        self._build_ui()

    # ------------------------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.setCentralWidget(central)

        # ================= Header =================
        header = QFrame()
        header_col = QVBoxLayout(header)
        header_col.setContentsMargins(16, 14, 16, 14)
        header_col.setSpacing(12)

        # --- Row 1: URL entry + paste + go ---
        url_row = QHBoxLayout()
        url_row.setSpacing(10)

        self.url_edit = QLineEdit()
        self.url_edit.setObjectName("urlbox")
        self.url_edit.setPlaceholderText(
            "Paste video or playlist link here...")
        self.url_edit.setMinimumHeight(40)
        self.url_edit.returnPressed.connect(self.go_clicked)
        url_row.addWidget(self.url_edit, 1)

        self.paste_btn = QPushButton("  Paste Link")
        self.paste_btn.setIcon(qta.icon("fa5s.clipboard", color="#e6edf3"))
        self.paste_btn.setFixedHeight(40)
        self.paste_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.paste_btn.setToolTip("Paste clipboard contents into the URL box")
        self.paste_btn.clicked.connect(self.paste_link)
        url_row.addWidget(self.paste_btn)

        self.go_btn = QPushButton("  Download")
        self.go_btn.setObjectName("green")
        self.go_btn.setIcon(qta.icon("fa5s.download", color="white"))
        self.go_btn.setFixedHeight(40)
        self.go_btn.setMinimumWidth(130)
        self.go_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.go_btn.clicked.connect(self.go_clicked)
        url_row.addWidget(self.go_btn)

        header_col.addLayout(url_row)

        # --- Row 2: presets + toggles + status ---
        h = QHBoxLayout()
        h.setSpacing(14)

        self.smart_toggle = QCheckBox("Smart Mode")
        self.smart_toggle.setToolTip(
            "Skip the dialog — download instantly with the presets below")
        h.addWidget(self.smart_toggle)

        h.addSpacing(8)

        self.preset_format = QComboBox()
        self.preset_format.addItems(["Video", "Audio"])
        self.preset_quality = QComboBox()
        self.preset_quality.addItems(
            ["Best", "2160p", "1440p", "1080p", "720p", "480p"])
        self.preset_container = QComboBox()
        self.preset_container.addItems(["Auto", "MP4", "MKV"])
        for lab, combo in [("Format", self.preset_format),
                           ("Quality", self.preset_quality),
                           ("Container", self.preset_container)]:
            wrap = QVBoxLayout()
            wrap.setSpacing(3)
            lw = QLabel(lab)
            lw.setStyleSheet("color: gray; font-size: 10px;")
            wrap.addWidget(lw)
            wrap.addWidget(combo)
            h.addLayout(wrap)

        dir_wrap = QVBoxLayout()
        dir_wrap.setSpacing(3)
        dl = QLabel("Save to")
        dl.setStyleSheet("color: gray; font-size: 10px;")
        dir_wrap.addWidget(dl)
        self.dir_btn = QPushButton(
            "  " + (os.path.basename(self.save_dir) or self.save_dir))
        self.dir_btn.setIcon(qta.icon("fa5s.folder-open", color="#e6edf3"))
        self.dir_btn.setToolTip(self.save_dir)
        self.dir_btn.clicked.connect(self.pick_dir)
        dir_wrap.addWidget(self.dir_btn)
        h.addLayout(dir_wrap)

        h.addStretch()

        self.status_dot = QLabel("● 0 active")
        self.status_dot.setStyleSheet("color: gray;")
        h.addWidget(self.status_dot)

        self.theme_btn = QPushButton()
        self.theme_btn.setIcon(qta.icon("fa5s.sun", color="#e6edf3"))
        self.theme_btn.setFixedSize(38, 38)
        self.theme_btn.setToolTip("Toggle light/dark theme")
        self.theme_btn.clicked.connect(self.toggle_theme)
        h.addWidget(self.theme_btn)

        self.settings_btn = QPushButton()
        self.settings_btn.setIcon(qta.icon("fa5s.cog", color="#e6edf3"))
        self.settings_btn.setFixedSize(38, 38)
        self.settings_btn.setToolTip("Settings")
        self.settings_btn.clicked.connect(self.open_settings)
        h.addWidget(self.settings_btn)

        header_col.addLayout(h)
        root.addWidget(header)

        # ================= Nav tabs =================
        tabs = QFrame()
        tl = QHBoxLayout(tabs)
        tl.setContentsMargins(12, 0, 12, 0)
        self.nav_btns = []
        for i, name in enumerate(["Home", "Browser", "Downloads"]):
            b = QPushButton(name)
            b.setObjectName("navtab")
            b.setCheckable(True)
            b.clicked.connect(lambda _, idx=i: self.switch_page(idx))
            tl.addWidget(b)
            self.nav_btns.append(b)
        tl.addStretch()
        root.addWidget(tabs)

        # ================= Pages =================
        self.stack = QStackedWidget()
        self.home_page = HomePage()
        self.browser_page = BrowserPage(self.profile)
        self.downloads_page = DownloadsPage()
        self.stack.addWidget(self.home_page)
        self.stack.addWidget(self.browser_page)
        self.stack.addWidget(self.downloads_page)
        root.addWidget(self.stack, 1)

        self.home_page.open_url.connect(self.open_in_browser)
        self.browser_page.request_download.connect(self.analyze_url)

        # ================= Status bar =================
        self.statusBar().showMessage("Ready.")
        self.switch_page(0)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    def switch_page(self, idx: int):
        self.stack.setCurrentIndex(idx)
        for i, b in enumerate(self.nav_btns):
            b.setChecked(i == idx)

    def open_in_browser(self, url: str):
        self.switch_page(1)
        self.browser_page.navigate(url)

    def pick_dir(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose download folder", self.save_dir)
        if folder:
            self.save_dir = folder
            self.dir_btn.setText("  " + (os.path.basename(folder) or folder))
            self.dir_btn.setToolTip(folder)

    def toggle_theme(self):
        self.dark = not self.dark
        self.apply_theme()

    def apply_theme(self):
        """Switch qdarktheme dynamically; keep custom layout/green QSS."""
        qdarktheme.setup_theme(
            "dark" if self.dark else "light",
            additional_qss=CUSTOM_QSS,
        )
        fg = "#e6edf3" if self.dark else "#1f2328"
        # Sun shown in dark mode (click for light), moon in light mode
        self.theme_btn.setIcon(
            qta.icon("fa5s.sun" if self.dark else "fa5s.moon", color=fg))
        self.theme_btn.setToolTip(
            "Switch to light theme" if self.dark else "Switch to dark theme")
        self.settings_btn.setIcon(qta.icon("fa5s.cog", color=fg))
        self.paste_btn.setIcon(qta.icon("fa5s.clipboard", color=fg))
        self.dir_btn.setIcon(qta.icon("fa5s.folder-open", color=fg))

    def open_settings(self):
        QMessageBox.information(
            self, "Settings",
            f"Save directory: {self.save_dir}\n"
            f"ffmpeg: {FFMPEG_DIR}\n"
            f"Ad blocking: enabled\n"
            f"Theme: {'Dark' if self.dark else 'Light'}")

    # ------------------------------------------------------------------
    # Analyze / paste flow
    # ------------------------------------------------------------------
    def paste_link(self):
        """Paste clipboard into the URL box (no auto-analyze)."""
        text = QApplication.clipboard().text(QClipboard.Mode.Clipboard).strip()
        if not text:
            self.statusBar().showMessage("Clipboard is empty.")
            return
        self.url_edit.setText(text)
        self.url_edit.setFocus()
        if text.startswith(("http://", "https://")):
            self.statusBar().showMessage(
                "Link pasted. Click Download to analyze.")
        else:
            self.statusBar().showMessage(
                "Pasted text is not a URL — check it before downloading.")

    def go_clicked(self):
        url = self.url_edit.text().strip()
        if not url:
            self.statusBar().showMessage("Enter or paste a link first.")
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
            self.url_edit.setText(url)
        self.analyze_url(url)

    def analyze_url(self, url: str):
        if self.analyze_worker and self.analyze_worker.isRunning():
            self.statusBar().showMessage("Already analyzing a link...")
            return
        self.statusBar().showMessage(f"Analyzing: {url}")
        self.paste_btn.setEnabled(False)
        self.go_btn.setEnabled(False)
        self.analyze_worker = AnalyzeWorker(url)
        self.analyze_worker.result.connect(
            lambda info, thumb, u=url: self.on_analyzed(u, info, thumb))
        self.analyze_worker.error.connect(self.on_analyze_error)
        self.analyze_worker.start()

    def on_analyze_error(self, msg: str):
        self.paste_btn.setEnabled(True)
        self.go_btn.setEnabled(True)
        self.statusBar().showMessage(f"Analyze failed: {msg}")

    def on_analyzed(self, url: str, info: dict, thumb: bytes):
        self.paste_btn.setEnabled(True)
        self.go_btn.setEnabled(True)
        self.statusBar().showMessage("Ready.")

        if self.smart_toggle.isChecked():
            opts, kind = self.build_smart_opts(info)
            self.start_download(url, opts, info, kind, thumb)
            return

        modal = DownloadModal(self, info, thumb, self.save_dir)
        if modal.exec() == QDialog.DialogCode.Accepted and modal.selected_opts:
            self.start_download(
                url, modal.selected_opts, info, modal.selected_kind, thumb)

    # ------------------------------------------------------------------
    # Smart mode opts from global presets
    # ------------------------------------------------------------------
    def build_smart_opts(self, info: dict):
        is_playlist = info.get("_type") == "playlist"
        is_audio = self.preset_format.currentText() == "Audio"
        quality = self.preset_quality.currentText()
        container = self.preset_container.currentText()

        outtmpl = os.path.join(self.save_dir, "%(title)s.%(ext)s")
        if is_playlist:
            outtmpl = os.path.join(
                self.save_dir, "%(playlist_title)s",
                "%(playlist_index)s - %(title)s.%(ext)s")

        opts = {
            "outtmpl": outtmpl,
            "ffmpeg_location": FFMPEG_DIR,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": not is_playlist,
            "ignoreerrors": is_playlist,
        }

        if is_audio:
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
            return opts, "audio"

        if quality == "Best":
            opts["format"] = "bestvideo+bestaudio/best"
        else:
            h = quality.rstrip("p")
            opts["format"] = (f"bestvideo[height<={h}]+bestaudio"
                              f"/best[height<={h}]")
        opts["merge_output_format"] = {"MKV": "mkv"}.get(container, "mp4")
        return opts, ("playlist" if is_playlist else "video")

    # ------------------------------------------------------------------
    # Download management
    # ------------------------------------------------------------------
    def start_download(self, url, ydl_opts, info, kind, thumb):
        display = info
        if info.get("_type") == "playlist" and info.get("entries"):
            entries = [e for e in info["entries"] if e]
            if entries:
                display = entries[0]

        title = info.get("title") or display.get("title", "Unknown")
        if kind == "playlist":
            count = len([e for e in info.get("entries") or [] if e])
            title = f"[Playlist · {count}] {title}"

        meta_parts = [human_duration(display.get("duration"))]
        if display.get("height"):
            meta_parts.append(f"{display['height']}p")
        if display.get("fps"):
            meta_parts.append(f"{display['fps']:.0f}fps")
        if display.get("ext"):
            meta_parts.append(display["ext"].upper())
        uploader = display.get("uploader") or display.get("channel")
        if uploader:
            meta_parts.append(uploader)
        meta = "  ·  ".join(str(p) for p in meta_parts if p and p != "?")

        worker = DownloadWorker(url, ydl_opts)
        item = DownloadItem(title, meta, thumb, kind, worker)
        self.downloads_page.add_item(item)
        self.workers.append(worker)
        worker.done.connect(lambda *_: self.update_active_count())
        worker.start()
        self.update_active_count()

        self.switch_page(2)
        self.statusBar().showMessage(f"Download started: {title}")

    def update_active_count(self):
        active = sum(1 for w in self.workers if w.isRunning())
        color = "#3fb950" if active else "#6b7280"
        self.status_dot.setText(f"● {active} active")
        self.status_dot.setStyleSheet(f"color: {color};")

    def closeEvent(self, event):
        for w in self.workers:
            w.is_cancelled = True
            w.is_paused = False
        event.accept()


# ============================================================================
# Entry point
# ============================================================================

def main():
    # Force Windows to use our custom icon in the taskbar instead of the
    # default Python logo. Must run before the QApplication/window is created.
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "mycompany.universaldownloader.1.0"
        )
    except (AttributeError, OSError):
        # Not on Windows, or the call is unavailable — safe to ignore.
        pass

    qdarktheme.enable_hi_dpi()
    app = QApplication(sys.argv)

    # Application icon (taskbar / system tray). Use the .ico so Windows picks
    # the correct native resolution for the taskbar and title bar.
    app_icon_path = get_resource_path("icon.ico")
    if os.path.exists(app_icon_path):
        app.setWindowIcon(QIcon(app_icon_path))

    qdarktheme.setup_theme("dark", additional_qss=CUSTOM_QSS)
    win = MainWindow()
    win.apply_theme()  # sync icons/tooltips with the dark default

    # Window icon (top-left of the main window).
    if os.path.exists(app_icon_path):
        win.setWindowIcon(QIcon(app_icon_path))

    missing = [
        name for name in ("ffmpeg.exe", "ffprobe.exe", "yt-dlp.exe")
        if not os.path.exists(os.path.join(FFMPEG_DIR, name))
    ]
    if missing:
        QMessageBox.warning(
            win, "ffmpeg missing",
            "The following binaries were not found in:\n"
            f"{FFMPEG_DIR}\n\n"
            f"Missing: {', '.join(missing)}\n\n"
            "Merging video+audio and MP3 extraction will fail.")

    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
