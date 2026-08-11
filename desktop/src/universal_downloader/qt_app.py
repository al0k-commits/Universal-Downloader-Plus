"""
Universal Downloader+ — 4K Video Downloader+ style desktop app.

PyQt6 + QWebEngine in-app browser with ad-blocking, download manager,
detailed format-selection modal, pause/resume/cancel via yt-dlp hooks.

Requires ffmpeg / ffprobe (ffmpeg.exe / ffprobe.exe on Windows) inside the
ffmpeg/ folder at the repository root (dev) or next to the frozen executable
(PyInstaller build). yt-dlp is bundled as a Python package.
"""

import os
import re
import sys
import time
import json
import uuid
import ctypes
import subprocess
import threading
from urllib.parse import (urlparse, urlunparse, parse_qs, parse_qsl,
                          urlencode)

import requests
import yt_dlp
import qtawesome as qta
import qdarktheme

from PyQt6.QtCore import (Qt, QUrl, QThread, pyqtSignal, QSize,
                          QPropertyAnimation, QEasingCurve, pyqtProperty,
                          QTimer, QSettings, QStandardPaths, QRectF)
from PyQt6.QtGui import (QPixmap, QClipboard, QIcon, QPainter, QColor, QBrush,
                         QPen, QDesktopServices, QPainterPath)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QDialog, QLabel, QPushButton,
    QLineEdit, QComboBox, QCheckBox, QProgressBar, QFileDialog, QFrame,
    QVBoxLayout, QHBoxLayout, QGridLayout, QStackedWidget, QScrollArea,
    QSizePolicy, QMessageBox, QToolButton, QSpinBox, QGraphicsDropShadowEffect,
)
from PyQt6.QtNetwork import QNetworkCookie
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


# Third-party binaries (ffmpeg, ffprobe) live here.
# In dev this is <repo_root>/ffmpeg; when frozen it is <exe_dir>/ffmpeg.
FFMPEG_DIR = os.path.join(BASE_DIR, "ffmpeg")


# ---------------------------------------------------------------------------
# Persistent app data (browser profile + exported cookies for yt-dlp)
# ---------------------------------------------------------------------------

def app_data_dir() -> str:
    """Writable per-user app data folder, created on demand."""
    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppDataLocation)
    if not base:
        base = os.path.join(os.path.expanduser("~"),
                            ".universal_downloader_plus")
    os.makedirs(base, exist_ok=True)
    return base


def browser_data_dir() -> str:
    """Chromium storage/cache root for the persistent QWebEngineProfile."""
    path = os.path.join(app_data_dir(), "browser_data")
    os.makedirs(path, exist_ok=True)
    return path


def cookie_file_path() -> str:
    """Netscape cookie jar handed to yt-dlp via 'cookiefile'."""
    return os.path.join(app_data_dir(), "yt_dlp_cookies.txt")


def history_file_path() -> str:
    """JSON file holding the persistent download history."""
    return os.path.join(app_data_dir(), "downloads_history.json")


def thumb_cache_dir() -> str:
    """Local cache of download thumbnails referenced by the history file."""
    path = os.path.join(app_data_dir(), "thumbnails")
    os.makedirs(path, exist_ok=True)
    return path


def ydl_cookie_opts() -> dict:
    """Common yt-dlp options that share the embedded browser's login state.

    Returns {} when the jar holds nothing but anonymous visitor tokens, which
    would otherwise trigger HTTP 403 on the media fetch.
    """
    path = sanitized_cookie_file()
    return {"cookiefile": path} if path and os.path.isfile(path) else {}


# ---------------------------------------------------------------------------
# Anti-bot / HTTP 403 mitigation
# ---------------------------------------------------------------------------
# YouTube rejects requests whose player client looks automated. Adding extra
# InnerTube clients alongside the default gives yt-dlp fallbacks when one
# client is throttled or 403s.
#
# NOTE: "default" MUST stay in the list. Pinning explicit clients such as
# android/ios/tv or web+android strips every adaptive format and caps
# downloads at 360p (measured 2026-08-08: 31 formats / 2160p with default,
# 5 formats / 360p with ["web", "android"]).
#
# "-tv_simply" subtracts that one client from the default set. tv_simply
# requires a GVS PO Token we cannot mint, so it emits
#   "tv_simply client https formats require a GVS PO Token"
# and contributes no usable formats. Removing it silences the warning while
# keeping the full 2160p ladder.
YOUTUBE_PLAYER_CLIENTS = ["default", "-tv_simply"]

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
              " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _fragment_retry_sleep(n):
    """Wait 2s between fragment retries (module-level so it stays picklable)."""
    return 2


def ydl_antibot_opts() -> dict:
    """Options that keep YouTube from returning HTTP 403 Forbidden."""
    return {
        # Correct schema: {extractor: {arg_key: [values]}} — a bare
        # ['client=...'] list is silently ignored by yt-dlp.
        "extractor_args": {
            "youtube": {"player_client": list(YOUTUBE_PLAYER_CLIENTS)},
        },
        "http_headers": {"User-Agent": USER_AGENT},
        # 403s on media URLs are often transient; let yt-dlp re-resolve them.
        "retries": 5,
        "extractor_retries": 3,
        # Survive single dropped packets mid-download instead of aborting.
        "fragment_retries": 10,
        "retry_sleep_functions": {"fragment": _fragment_retry_sleep},
    }


# ---------------------------------------------------------------------------
# Cookie hygiene (the actual cause of mid-download 403s)
# ---------------------------------------------------------------------------
# Anonymous "visitor" cookies bind googlevideo stream URLs to a GVS PO Token
# that we cannot mint. Metadata extraction still succeeds, so the failure only
# surfaces mid-download as: "unable to download video data: HTTP Error 403".
#
# Measured 2026-08-08 on the same video and client set:
#   no cookies                -> download OK
#   full exported jar         -> HTTP 403
#   jar minus these cookies   -> download OK
#
# Real login cookies (SID/SAPISID/__Secure-*PSID) are NOT in this list: those
# authenticate the request and are exempt from the visitor-token binding.
VISITOR_COOKIES = frozenset({
    "VISITOR_INFO1_LIVE",
    "VISITOR_PRIVACY_METADATA",
    "YSC",
    "GPS",
    "__Secure-ROLLOUT_TOKEN",
})

# Presence of any of these means the user is genuinely signed in.
LOGIN_COOKIES = frozenset({
    "SID", "SSID", "HSID", "APISID", "SAPISID",
    "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO",
})


def _read_cookie_records(path: str) -> list:
    """Return non-comment Netscape cookie lines from the jar."""
    try:
        with open(path, encoding="utf-8") as fh:
            return [l for l in fh
                    if l.strip() and not l.startswith("#") and "\t" in l]
    except OSError:
        return []


def sanitized_cookie_file() -> str:
    """Path to a cookie jar safe to hand yt-dlp, or "" if none is usable.

    When signed in, the jar is passed through untouched. When signed out, the
    visitor cookies are stripped (they cause 403s and grant no access), and if
    nothing meaningful remains we return "" so yt-dlp runs cookie-free.
    """
    src = cookie_file_path()
    records = _read_cookie_records(src)
    if not records:
        return ""

    names = {r.split("\t")[5] for r in records if len(r.split("\t")) > 5}
    if names & LOGIN_COOKIES:
        return src          # authenticated: keep everything

    keep = [r for r in records
            if r.split("\t")[5] not in VISITOR_COOKIES]
    if not keep:
        return ""           # nothing but visitor tokens — better to send none

    clean = os.path.join(app_data_dir(), "yt_dlp_cookies_clean.txt")
    try:
        with open(clean, "w", encoding="utf-8") as fh:
            fh.write("# Netscape HTTP Cookie File\n")
            fh.write("# Auto-filtered by Universal Downloader+ "
                     "(visitor tokens removed to avoid HTTP 403).\n")
            fh.writelines(keep)
        return clean
    except OSError:
        return ""


def ydl_base_opts() -> dict:
    """Cookies + anti-bot options shared by every yt-dlp invocation."""
    opts = ydl_antibot_opts()
    opts.update(ydl_cookie_opts())
    return opts


def flush_ydl_cache():
    """Drop yt-dlp's cached YouTube ciphers (equivalent of --rm-cache-dir).

    'rm_cachedir' is a CLI-only flag: passing it to YoutubeDL(...) is a silent
    no-op. The programmatic equivalent is ydl.cache.remove().
    """
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            ydl.cache.remove()
        print("[yt-dlp] Signature cache flushed.")
    except Exception as e:
        print(f"[yt-dlp] Cache flush failed: {e}")

AD_DOMAINS = (
    "doubleclick.net", "googlesyndication.com", "googleadservices.com",
    "google-analytics.com", "adservice.google.com", "googletagmanager.com",
    "googletagservices.com", "ads.youtube.com", "adnxs.com", "taboola.com",
    "outbrain.com", "scorecardresearch.com", "moatads.com", "adsafeprotected.com",
    "amazon-adsystem.com", "criteo.com", "pubmatic.com", "rubiconproject.com",
    "adsystem.com",       # generic AdSystem network
    "googlesyndication.org",
)

AD_PATH_MARKERS = (
    "/pagead", "/pagead/", "/pagead2", "/pagead2/",
    "/ptracking", "/ad_break", "/api/stats/ads", "/yt/ads",
    "/get_midroll_info", "/youtubei/v1/player/ads",
    "/companion=1", "/gfpcs", "/ad_data",
)

AD_HOST_MARKERS = ("adsystem", "ads.", "adserver", "adsense", "adservice")

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

# yt-dlp decorates status strings with ANSI color codes (e.g. "\x1b[0;94m").
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

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

# Query params that carry no media identity (tracking, timestamps, referrers).
TRACKING_PARAMS = {
    "si", "pp", "t", "start_radio", "feature", "ab_channel", "app",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "spm_id_from", "vd_source", "rco", "themeRefresh",
}
# Params that actually identify what to download.
MEDIA_PARAMS = ("v", "list", "index", "video_id", "story_fbid", "id")


def clean_media_url(url: str) -> str:
    """Normalise a browser URL into a downloadable media URL.

    Returns an empty string when the URL is not a concrete video/playlist
    page (home pages, search results, about:blank, feeds, ...).
    """
    url = (url or "").strip()
    if not url or url.lower().startswith(("about:", "chrome:", "data:",
                                          "view-source:")):
        return ""
    if not url.startswith(("http://", "https://")):
        if "." not in url or " " in url:
            return ""
        url = "https://" + url

    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":")[0]
    host = host[4:] if host.startswith("www.") else host
    path = parsed.path or "/"

    # --- YouTube family -------------------------------------------------
    if host in ("youtube.com", "m.youtube.com", "music.youtube.com",
                "youtube-nocookie.com"):
        m = re.match(r"^/(?:shorts|live|embed|v)/([A-Za-z0-9_-]{6,})", path)
        if m:
            return f"https://www.youtube.com/watch?v={m.group(1)}"
        params = parse_qs(parsed.query, keep_blank_values=False)
        if path == "/watch" and params.get("v"):
            vid = params["v"][0]
            plist = (params.get("list") or [""])[0]
            clean = f"https://www.youtube.com/watch?v={vid}"
            if plist and not plist.startswith("RD"):
                clean += f"&list={plist}"
            return clean
        if path == "/playlist" and params.get("list"):
            return f"https://www.youtube.com/playlist?list={params['list'][0]}"
        # Home page, /results, /feed/*, channel pages: nothing to download.
        return ""

    if host == "youtu.be":
        vid = path.strip("/").split("/")[0]
        return f"https://www.youtube.com/watch?v={vid}" if vid else ""

    # --- Everything else: keep only meaningful query params --------------
    if not VIDEO_URL_RE.search(url):
        return ""
    kept = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=False)
            if k in MEDIA_PARAMS or k not in TRACKING_PARAMS]
    return urlunparse(parsed._replace(query=urlencode(kept), fragment=""))

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

# ---------------------------------------------------------------------------
# Design system
# ---------------------------------------------------------------------------
# Single source of truth for colour/shape/typography. Both themes share the
# same geometry; only the palette swaps, so every screen stays consistent.

FONT_STACK = '"Segoe UI", "SF Pro Display", "Inter", sans-serif'
FONT_MONO = '"Cascadia Mono", "SF Mono", Consolas, monospace'

RADIUS_CARD = 12       # cards, dialogs, surfaces
RADIUS_INPUT = 8       # inputs, buttons
RADIUS_PILL = 999      # badges, toggles

ACCENT = "#22c55e"           # emerald-500
ACCENT_HOVER = "#4ade80"     # emerald-400
ACCENT_PRESSED = "#16a34a"   # emerald-600
DANGER = "#ef4444"           # red-500
DANGER_HOVER = "#f87171"
INFO = "#60a5fa"             # blue-400

NAV_IDLE_DARK = "#a1a1aa"    # zinc-400
NAV_IDLE_LIGHT = "#52525b"   # zinc-600

DARK_TOKENS = {
    "bg": "#18181b",          # zinc-900
    "surface": "#27272a",     # zinc-800
    "surface_alt": "#2f2f34",
    "border": "#3f3f46",      # zinc-700
    "text": "#fafafa",
    "text_muted": "#a1a1aa",  # zinc-400
    "hover": "rgba(255, 255, 255, 0.08)",
    "pressed": "rgba(255, 255, 255, 0.14)",
    "scroll": "rgba(161, 161, 170, 0.35)",
    "scroll_hover": "rgba(212, 212, 216, 0.60)",
}

LIGHT_TOKENS = {
    "bg": "#f4f4f5",          # zinc-100
    "surface": "#ffffff",
    "surface_alt": "#fafafa",
    "border": "#d4d4d8",      # zinc-300
    "text": "#18181b",
    "text_muted": "#52525b",  # zinc-600
    "hover": "rgba(24, 24, 27, 0.06)",
    "pressed": "rgba(24, 24, 27, 0.12)",
    "scroll": "rgba(82, 82, 91, 0.30)",
    "scroll_hover": "rgba(63, 63, 70, 0.55)",
}


def theme_tokens(dark: bool = True) -> dict:
    return dict(DARK_TOKENS if dark else LIGHT_TOKENS)


def rounded_thumbnail(data: bytes, w: int, h: int, radius: int = 8) -> QPixmap:
    """Center-cropped, anti-aliased thumbnail clipped to a rounded rect."""
    src = QPixmap()
    canvas = QPixmap(w, h)
    canvas.fill(Qt.GlobalColor.transparent)
    if not data or not src.loadFromData(data):
        return canvas

    scaled = src.scaled(w, h,
                        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation)
    painter = QPainter(canvas)
    painter.setRenderHints(QPainter.RenderHint.Antialiasing
                           | QPainter.RenderHint.SmoothPixmapTransform)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0, 0, w, h), radius, radius)
    painter.setClipPath(clip)
    painter.drawPixmap((w - scaled.width()) // 2,
                       (h - scaled.height()) // 2, scaled)
    painter.end()
    return canvas


def build_qss(dark: bool = True) -> str:
    """Compose the full application stylesheet for the given theme."""
    t = theme_tokens(dark)
    nav_idle = NAV_IDLE_DARK if dark else NAV_IDLE_LIGHT
    nav_hover_fg = "#ffffff" if dark else "#18181b"
    nav_hover_bg = ("rgba(255, 255, 255, 0.06)" if dark
                    else "rgba(24, 24, 27, 0.06)")
    return f"""
/* ===================== Base typography & surfaces ===================== */
* {{
    font-family: {FONT_STACK};
    font-size: 13px;
    font-weight: 400;
}}
QMainWindow, QDialog {{
    background-color: {t['bg']};
    color: {t['text']};
}}
QWidget {{ color: {t['text']}; }}
QLabel {{ background: transparent; }}
QLabel#title {{ font-size: 18px; font-weight: 600; }}
QLabel#sectionHeader {{
    font-size: 12px; font-weight: 500; color: {t['text_muted']};
    text-transform: uppercase; letter-spacing: 0.6px;
}}
QLabel#subtext {{ font-size: 11px; font-weight: 400; color: {t['text_muted']}; }}
QToolTip {{
    background-color: {t['surface']};
    color: {t['text']};
    border: 1px solid {t['border']};
    border-radius: {RADIUS_INPUT}px;
    padding: 6px 9px;
}}

/* ============================ Surfaces =============================== */
QFrame#card, QFrame#downloadCard, QFrame#surface {{
    background-color: {t['surface']};
    border: 1px solid {t['border']};
    border-radius: {RADIUS_CARD}px;
}}
QFrame#downloadCard {{ padding: 12px; }}
QFrame#downloadCard:hover {{ border-color: rgba(34, 197, 94, 0.40); }}
QFrame#header, QFrame#navbar {{
    background-color: {t['bg']};
    border: none;
    border-bottom: 1px solid {t['border']};
}}
QFrame#row {{
    background-color: {t['surface_alt']};
    border: 1px solid {t['border']};
    border-radius: {RADIUS_INPUT}px;
}}
QFrame#row:hover {{ border-color: rgba(34, 197, 94, 0.45); }}
QFrame#divider {{ background-color: {t['border']}; border: none; }}

/* ============================= Inputs ================================ */
QLineEdit, QComboBox, QSpinBox, QAbstractSpinBox {{
    background-color: {t['surface']};
    border: 1px solid {t['border']};
    border-radius: {RADIUS_INPUT}px;
    padding: 8px 12px;
    color: {t['text']};
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover {{
    border-color: {t['text_muted']};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{
    border: 1px solid {ACCENT};
    outline: none;
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
    color: {t['text_muted']};
    background-color: {t['surface_alt']};
}}
QLineEdit#urlbox {{ font-size: 14px; padding: 10px 16px; }}

/* Compact Smart Mode preset row.
   Qt adds padding+border on top of min/max-height, so the box values are
   reduced by 10px to land on a 24-28px rendered control. */
QComboBox#preset, QPushButton#preset {{
    padding: 4px 10px;
    font-size: 12px;
    font-weight: 400;
    border-radius: 6px;
    min-height: 14px;
    max-height: 18px;
}}
QComboBox#preset::drop-down {{ width: 18px; }}
QPushButton#preset {{ text-align: left; }}
QLabel#presetLabel {{
    font-size: 10px;
    font-weight: 500;
    color: {NAV_IDLE_DARK if dark else NAV_IDLE_LIGHT};
    letter-spacing: 0.3px;
    padding-left: 2px;
}}
QLineEdit#addrbar {{
    font-size: 12px;
    border-radius: {RADIUS_CARD}px;
    padding: 6px 12px;
    background-color: {t['surface']};
    border: 1px solid {t['border']};
}}
QLineEdit#addrbar:hover {{ background-color: {t['surface_alt']}; }}
QLineEdit#addrbar:focus {{
    border: 1px solid {ACCENT};
    background-color: {t['surface']};
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ width: 0; height: 0; }}
QComboBox QAbstractItemView {{
    background-color: {t['surface']};
    border: 1px solid {t['border']};
    border-radius: {RADIUS_INPUT}px;
    padding: 4px;
    outline: none;
    selection-background-color: rgba(34, 197, 94, 0.22);
    selection-color: {t['text']};
}}
QSpinBox::up-button, QSpinBox::down-button {{ width: 16px; border: none; }}

/* ============================ Buttons ================================ */
QPushButton {{
    background-color: {t['surface']};
    border: 1px solid {t['border']};
    border-radius: {RADIUS_INPUT}px;
    padding: 8px 16px;
    font-weight: 500;
    color: {t['text']};
}}
QPushButton:hover {{ background-color: {t['hover']}; border-color: {t['text_muted']}; }}
QPushButton:pressed {{ background-color: {t['pressed']}; }}
QPushButton:disabled {{ color: {t['text_muted']}; border-color: {t['border']}; }}

QPushButton#green {{
    background-color: {ACCENT}; border: none; color: #06240f;
    font-weight: 600; border-radius: {RADIUS_INPUT}px; padding: 8px 18px;
}}
QPushButton#green:hover {{ background-color: {ACCENT_HOVER}; }}
QPushButton#green:pressed {{ background-color: {ACCENT_PRESSED}; }}
QPushButton#green:disabled {{ background-color: rgba(34, 197, 94, 0.25); color: {t['text_muted']}; }}

QPushButton#danger {{
    background-color: {DANGER}; border: none; color: #ffffff;
    font-weight: 500; border-radius: {RADIUS_INPUT}px; padding: 8px 16px;
}}
QPushButton#danger:hover {{ background-color: {DANGER_HOVER}; }}

/* Compact semantic row actions (Show / Delete / Clear) */
QPushButton#actionShow, QPushButton#actionDelete, QPushButton#actionClear {{
    border-radius: {RADIUS_INPUT}px;
    padding: 6px 12px;
    font-size: 12px;
    font-weight: 500;
    text-align: left;
}}
QPushButton#actionShow {{
    background-color: rgba(96, 165, 250, 0.14);
    border: 1px solid rgba(96, 165, 250, 0.35);
    color: {INFO};
}}
QPushButton#actionShow:hover {{ background-color: rgba(96, 165, 250, 0.26); }}
QPushButton#actionDelete {{
    background-color: rgba(239, 68, 68, 0.12);
    border: 1px solid rgba(239, 68, 68, 0.32);
    color: {DANGER_HOVER};
}}
QPushButton#actionDelete:hover {{ background-color: rgba(239, 68, 68, 0.24); }}
QPushButton#actionClear {{
    background-color: {t['hover']};
    border: 1px solid {t['border']};
    color: {t['text_muted']};
}}
QPushButton#actionClear:hover {{ background-color: {t['pressed']}; color: {t['text']}; }}

/* Segmented filter tabs */
QPushButton#navtab {{
    background: transparent; border: 1px solid transparent;
    padding: 6px 14px; font-weight: 500;
    border-radius: {RADIUS_PILL}px; color: {t['text_muted']};
}}
QPushButton#navtab:hover {{ background-color: {t['hover']}; color: {t['text']}; }}
QPushButton#navtab:checked {{
    background-color: rgba(34, 197, 94, 0.16);
    border-color: rgba(34, 197, 94, 0.45);
    color: {ACCENT_HOVER};
    font-weight: 600;
}}

/* Main navigation: strictly icon-only, compact pill */
QToolButton#mainnav {{
    background: transparent;
    border: none;
    padding: 0;
    border-radius: {RADIUS_INPUT}px;
    color: {nav_idle};
}}
QToolButton#mainnav:hover {{
    background: {nav_hover_bg};
    color: {nav_hover_fg};
}}
QToolButton#mainnav:pressed {{ background: {t['pressed']}; }}
QToolButton#mainnav:checked {{
    background: rgba(34, 197, 94, 0.15);
    color: {ACCENT};
}}
QToolButton#mainnav:checked:hover {{ background: rgba(34, 197, 94, 0.22); }}

/* Icon-only utility + browser buttons */
QToolButton#utilnav, QToolButton#browsernav {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: {RADIUS_INPUT}px;
    padding: 8px;
    margin: 2px;
}}
QToolButton#utilnav:hover, QToolButton#browsernav:hover {{
    background-color: {t['hover']};
    border-color: {t['border']};
}}
QToolButton#utilnav:pressed,
QToolButton#browsernav:pressed {{ background-color: {t['pressed']}; }}
QToolButton#browsernav {{ border-radius: 8px; }}
QToolButton#browsernav:disabled {{
    background: transparent;
    border-color: transparent;
}}

/* Floating action button inside the browser */
QPushButton#fab {{
    background-color: {ACCENT};
    border: none;
    color: #000000;
    font-size: 14px;
    font-weight: 600;
    border-radius: 20px;
    padding: 10px 20px;
}}
QPushButton#fab:hover {{ background-color: {ACCENT_HOVER}; }}
QPushButton#fab:pressed {{ background-color: {ACCENT_PRESSED}; }}

QToolButton#service {{
    border-radius: {RADIUS_CARD}px; font-size: 14px; font-weight: 500;
    padding: 12px;
    border: 1px solid {t['border']};
    background-color: {t['surface']};
    color: {t['text']};
}}
QToolButton#service:hover {{
    background-color: {t['hover']};
    border-color: rgba(34, 197, 94, 0.45);
}}
QToolButton#service:pressed {{ background-color: {t['pressed']}; }}

/* ========================= Status badges ============================= */
QLabel#badge {{
    border-radius: {RADIUS_PILL}px;
    padding: 3px 10px;
    font-size: 11px;
    font-weight: 600;
}}
QLabel#badgeQueued {{
    background-color: {t['hover']}; color: {t['text_muted']};
    border: 1px solid {t['border']};
}}
QLabel#badgeActive {{
    background-color: rgba(96, 165, 250, 0.16); color: {INFO};
    border: 1px solid rgba(96, 165, 250, 0.38);
}}
QLabel#badgeDone {{
    background-color: rgba(34, 197, 94, 0.16); color: {ACCENT_HOVER};
    border: 1px solid rgba(34, 197, 94, 0.40);
}}
QLabel#badgeError {{
    background-color: rgba(239, 68, 68, 0.14); color: {DANGER_HOVER};
    border: 1px solid rgba(239, 68, 68, 0.36);
}}
QLabel#badgeQuality {{
    background-color: rgba(96, 165, 250, 0.14);
    color: {INFO};
    border: 1px solid rgba(96, 165, 250, 0.34);
    border-radius: {RADIUS_PILL}px;
    padding: 3px 10px;
    font-size: 11px;
    font-weight: 600;
}}
QLabel#badgeRecommended {{
    background-color: rgba(34, 197, 94, 0.18);
    color: {ACCENT_HOVER};
    border: 1px solid rgba(34, 197, 94, 0.55);
    border-radius: {RADIUS_PILL}px;
    padding: 2px 9px;
    font-size: 10px;
    font-weight: 600;
}}

/* ========================== Progress bars ============================ */
QProgressBar {{
    background-color: {t['border']};
    border: none;
    border-radius: 4px;
    max-height: 8px;
    min-height: 8px;
    color: transparent;
    text-align: center;
}}
QProgressBar::chunk {{ background-color: {ACCENT}; border-radius: 4px; }}

/* Hairline page-load indicator under the browser nav bar */
QProgressBar#browserProgress {{
    background: transparent;
    border: none;
    border-radius: 0;
    max-height: 3px;
    min-height: 3px;
}}
QProgressBar#browserProgress::chunk {{
    background-color: {ACCENT};
    border-radius: 0;
}}

/* =========================== Checkboxes ============================== */
QCheckBox {{ spacing: 10px; background: transparent; }}
QCheckBox::indicator {{
    width: 18px; height: 18px;
    border-radius: 6px;
    border: 1px solid {t['border']};
    background-color: {t['surface']};
}}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
    image: none;
}}

/* =========================== Scrollbars ============================== */
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{
    background: transparent;
    width: 6px;
    margin: 2px 2px 2px 0;
}}
QScrollBar::handle:vertical {{
    background: {t['scroll']};
    border-radius: 3px;
    min-height: 32px;
}}
QScrollBar::handle:vertical:hover {{ background: {t['scroll_hover']}; }}
QScrollBar:horizontal {{
    background: transparent;
    height: 6px;
    margin: 0 2px 2px 2px;
}}
QScrollBar::handle:horizontal {{
    background: {t['scroll']};
    border-radius: 3px;
    min-width: 32px;
}}
QScrollBar::handle:horizontal:hover {{ background: {t['scroll_hover']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ============================ Statusbar ============================== */
QStatusBar {{
    background-color: {t['bg']};
    border-top: 1px solid {t['border']};
    color: {t['text_muted']};
}}
QStatusBar::item {{ border: none; }}
"""


# Kept for backwards compatibility with existing call sites.
CUSTOM_QSS = build_qss(True)


# ============================================================================
# Custom toggle switch (iOS/macOS style pill)
# ============================================================================

class ToggleSwitch(QWidget):
    """Animated pill-style toggle switch with an adjacent label."""

    toggled = pyqtSignal(bool)

    _PILL_W = 46
    _PILL_H = 26
    _KNOB_R = 10
    _MARGIN = 3

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._checked = False
        self._knob_x = float(self._MARGIN)

        self._label = QLabel(text, self)
        self._label.setStyleSheet("font-size: 13px;")

        self.setFixedHeight(self._PILL_H + 4)
        self._update_width()

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Toggle on/off")

        # --- animation ---
        self._anim = QPropertyAnimation(self, b"knobX")
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)

    # -- public API (drop-in for QCheckBox) -------------------------------
    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, on: bool):
        if on == self._checked:
            return
        self._checked = on
        self._animate()
        self.toggled.emit(on)

    def toggle(self):
        self.setChecked(not self._checked)

    # -- animation helpers -------------------------------------------------
    def _animate(self):
        self._anim.stop()
        target = self._PILL_W - self._KNOB_R * 2 - self._MARGIN if self._checked else self._MARGIN
        self._anim.setStartValue(self._knob_x)
        self._anim.setEndValue(target)
        self._anim.start()

    @pyqtProperty(float)
    def knobX(self):
        return self._knob_x

    @knobX.setter
    def knobX(self, x):
        self._knob_x = x
        self.update()

    # -- geometry ----------------------------------------------------------
    def _update_width(self):
        self.setFixedWidth(self._PILL_W + 8 + self._label.sizeHint().width())

    def sizeHint(self):
        return QSize(
            self._PILL_W + 8 + self._label.sizeHint().width(),
            self._PILL_H + 4,
        )

    # -- events ------------------------------------------------------------
    def mousePressEvent(self, _event):
        self.toggle()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # pill background
        pill_x = 0
        pill_y = (self.height() - self._PILL_H) // 2
        pill_rect_w = self._PILL_W
        pill_rect_h = self._PILL_H

        bg = QColor("#2ea44f") if self._checked else QColor("#555555")
        p.setBrush(QBrush(bg))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(pill_x, pill_y, pill_rect_w, pill_rect_h,
                          pill_rect_h // 2, pill_rect_h // 2)

        # knob
        knob_y = self.height() // 2 - self._KNOB_R
        p.setBrush(QBrush(QColor("white")))
        p.drawEllipse(int(self._knob_x), knob_y,
                      self._KNOB_R * 2, self._KNOB_R * 2)

        # label
        self._label.move(pill_rect_w + 8, 0)
        p.end()


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
# Ad blocker (DISABLED - blocks yt-dlp API requests, causing YouTube to hang
# on "Analyzing..."). Kept as a reference; re-enable with care.
# ============================================================================
#
# class AdBlockInterceptor(QWebEngineUrlRequestInterceptor):
#     def interceptRequest(self, info):
#         url = info.requestUrl()
#         host = url.host().lower()
#         path = url.path().lower()
#         if any(host == d or host.endswith("." + d) for d in AD_DOMAINS):
#             info.block(True)
#             return
#         if any(m in host for m in AD_HOST_MARKERS):
#             info.block(True)
#             return
#         if any(m in path for m in AD_PATH_MARKERS):
#             info.block(True)


# ============================================================================
# Workers
# ============================================================================

class AnalyzeWorker(QThread):
    """Fetch metadata + thumbnail bytes off the UI thread."""
    result = pyqtSignal(dict, bytes)
    playlist = pyqtSignal(dict, bytes)
    error = pyqtSignal(str)

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        # NOTE: no 'quiet'/'no_warnings' — we WANT yt-dlp's exact stderr in the
        # terminal to diagnose network/retry hangs. Options below cap the hang.
        opts = {
            "skip_download": True,
            "socket_timeout": 10,        # give up if the network stalls 10s
            "ffmpeg_location": FFMPEG_DIR,
            # Anti-403: PO-token-free player clients, desktop UA, bounded
            # retries, plus the embedded browser's sanitized cookie jar.
            **ydl_base_opts(),
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(self.url, download=False)

            is_playlist = (
                info.get("_type") == "playlist"
                and info.get("entries"))

            display = info
            if is_playlist:
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

            if is_playlist:
                # Pause the standard flow - let the Main Window decide.
                self.playlist.emit(info, thumb)
            else:
                self.result.emit(info, thumb)
        except yt_dlp.utils.DownloadError as e:
            msg = str(e).replace("ERROR:", "").strip()
            print(f"[AnalyzeWorker] DownloadError: {msg}")
            self.error.emit(msg)
        except Exception as e:
            msg = str(e)
            print(f"[AnalyzeWorker] Unexpected error: {msg}")
            self.error.emit(msg)


class DownloadWorker(QThread):
    """Runs yt-dlp; pause via flag loop, cancel via exception in hook."""
    progress = pyqtSignal(dict)
    progress_update = pyqtSignal(float, str)   # percent (0.0-100.0), speed str
    thumbnail_ready = pyqtSignal(bytes)
    done = pyqtSignal(bool, str, str)   # ok, message, file_path

    def __init__(self, url: str, ydl_opts: dict, thumb_url: str = ""):
        super().__init__()
        self.url = url
        self.ydl_opts = dict(ydl_opts)
        self.thumb_url = thumb_url
        self.is_paused = False
        self._is_canceled = False
        self._cancel_lock = threading.Lock()

    @property
    def is_cancelled(self) -> bool:
        with self._cancel_lock:
            return self._is_canceled

    @is_cancelled.setter
    def is_cancelled(self, value: bool):
        with self._cancel_lock:
            self._is_canceled = bool(value)

    @staticmethod
    def _parse_percent(s) -> float:
        """' 12.3%' / '\\x1b[0;94m 12.3%\\x1b[0m' -> 12.3"""
        if isinstance(s, (int, float)):
            return float(s)
        match = re.search(r"\d+(?:\.\d+)?", ANSI_RE.sub("", str(s or "")))
        return float(match.group()) if match else 0.0

    @staticmethod
    def _format_speed(d: dict) -> str:
        """Format yt-dlp speed string (' 3.5MiB/s') into '3.5 MB/s'."""
        raw = ANSI_RE.sub("", str(d.get("_speed_str") or "")).strip()
        if not raw:
            return ""
        return (raw.replace("KiB", "KB").replace("MiB", "MB")
                .replace("GiB", "GB").strip())

    def _hook(self, d: dict):
        if self.is_cancelled:
            raise yt_dlp.utils.DownloadError("Canceled by user")
        while self.is_paused:
            time.sleep(0.4)
            if self.is_cancelled:
                raise yt_dlp.utils.DownloadError("Canceled by user")

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
            self.progress_update.emit(
                self._parse_percent(d.get("_percent_str")),
                self._format_speed(d),
            )
        elif status == "finished":
            self.progress.emit({"fraction": 1.0, "processing": True})

    def run(self):
        try:
            # Fetch thumbnail bytes in this worker thread so the GUI event loop
            # never blocks on the network request.
            if self.thumb_url:
                try:
                    r = requests.get(self.thumb_url, timeout=10)
                    r.raise_for_status()
                    self.thumbnail_ready.emit(r.content)
                except Exception:
                    pass

            opts = self.ydl_opts
            # Embed thumbnail + metadata (ID3 tags for audio) into the final
            # file. Reusing whatever postprocessors the caller built (e.g.
            # FFmpegExtractAudio for MP3) and appending the embedders at the
            # end of the chain.
            opts["writethumbnail"] = True
            pps = list(opts.get("postprocessors") or [])
            pps.append({"key": "EmbedThumbnail"})
            pps.append({"key": "FFmpegMetadata", "add_metadata": True})
            opts["postprocessors"] = pps
            opts["progress_hooks"] = [self._hook]

            # Anti-403: extra player clients + desktop UA + retries, plus the
            # embedded browser's cookies. Caller-supplied headers win.
            base = ydl_base_opts()
            headers = {**base.pop("http_headers", {}),
                       **(opts.get("http_headers") or {})}
            for key, value in base.items():
                opts.setdefault(key, value)
            opts["http_headers"] = headers

            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info_dict = ydl.extract_info(self.url, download=True)
            except yt_dlp.utils.DownloadError as e:
                # A mid-download 403 usually means the cached YouTube cipher
                # went stale. Flush it and retry once before giving up.
                if "403" not in str(e) or self.is_cancelled:
                    raise
                print("[DownloadWorker] 403 received — flushing cipher cache "
                      "and retrying once.")
                flush_ydl_cache()
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info_dict = ydl.extract_info(self.url, download=True)

            final_path = None
            if "requested_downloads" in info_dict:
                final_path = info_dict["requested_downloads"][0].get("filepath")
            if not final_path:
                final_path = info_dict.get("filepath") or info_dict.get("_filename")
            final_path = os.path.normpath(final_path) if final_path else ""

            print(f"Final merged path: {final_path}")
            self.done.emit(True, "Completed", final_path)
        except DownloadCancelled:
            self.done.emit(False, "Cancelled", "")
        except yt_dlp.utils.DownloadError as e:
            if "cancel" + "ed by user" in str(e).lower():
                self.done.emit(False, "Cancelled", "")
            else:
                self.done.emit(False, str(e).replace("ERROR:", "").strip()[:200], "")
        except Exception as e:
            self.done.emit(False, str(e)[:200], "")


def _recommend_format(formats: list, is_audio: bool = False) -> dict | None:
    """Pick the best format based on the download type.

    Video: prioritizes h264/avc codecs, then highest height, then fps.
    Audio: prioritizes formats with a codec (acodec != none), highest bitrate.
    Returns the winning format dict, or None if no suitable formats exist.
    """
    best = None
    if is_audio:
        for f in formats:
            if f.get("acodec") in (None, "none"):
                continue
            abr = f.get("abr") or f.get("tbr") or 0
            if best is None:
                best = (abr, f)
                continue
            if abr > best[0]:
                best = (abr, f)
    else:
        for f in formats:
            if f.get("vcodec") in (None, "none") or not f.get("height"):
                continue
            vcodec = (f.get("vcodec") or "").lower()
            is_h264 = "h264" in vcodec or "avc" in vcodec
            h = f["height"]
            fps = f.get("fps") or 0
            if best is None:
                best = (is_h264, h, fps, f)
                continue
            prev_h264, prev_h, prev_fps, _ = best
            if (is_h264, h, fps) > (prev_h264, prev_h, fps):
                best = (is_h264, h, fps, f)
    return best[-1] if best else None


def format_quality_label(fmt: dict | None, is_audio: bool = False) -> str:
    """Human label for the *selected* format: "1080p", "128kbps", "Audio".

    Falls back to the container/format_id when a format dict lacks height, so
    the UI never silently shows a stale resolution.
    """
    if is_audio:
        abr = (fmt or {}).get("abr") or (fmt or {}).get("tbr")
        return f"Audio · {abr:.0f}kbps" if abr else "Audio"
    if not fmt:
        return "Best"
    height = fmt.get("height")
    if height:
        label = f"{height}p"
        fps = fmt.get("fps") or 0
        if fps >= 50:
            label += f"{fps:.0f}"      # 1080p60
        return label
    return (fmt.get("format_note")
            or (fmt.get("ext") or "").upper()
            or str(fmt.get("format_id") or "Best"))


def get_best_format(info, media_type: str = "video") -> dict | None:
    """Headless best-format recommendation (Smart Mode / external callers).

    Accepts either a yt-dlp info dict (handles playlist unwrapping) or a
    bare list of formats. Returns the winning format dict or None.
    """
    if isinstance(info, dict):
        display = info
        if info.get("_type") == "playlist" and info.get("entries"):
            entries = [e for e in info["entries"] if e]
            if entries:
                display = entries[0]
        formats = display.get("formats") or []
    else:
        formats = list(info or [])
    return _recommend_format(formats, is_audio=(media_type == "audio"))


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
        # list of (opts_dict, kind_str, quality_str)
        self.selected_opts_list = []
        self.fmt_checkboxes = []       # QCheckBox instances

        self.setWindowTitle("Download")
        self.setMinimumSize(720, 660)
        self._build_ui(thumb)
        self._repopulate_formats()

    def _build_ui(self, thumb: bytes):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(14)

        # --- Title header: icon + text ---
        title_bar = QHBoxLayout()
        title_bar.setSpacing(10)
        icon_lab = QLabel()
        icon_lab.setPixmap(
            safe_icon("fa5s.photo-video", ACCENT).pixmap(QSize(22, 22)))
        title_bar.addWidget(icon_lab)
        heading = QLabel("Choose a format")
        heading.setObjectName("title")
        title_bar.addWidget(heading)
        title_bar.addStretch()
        root.addLayout(title_bar)

        # --- Header card: thumbnail + title + duration ---
        head_card = QFrame()
        head_card.setObjectName("card")
        head = QHBoxLayout(head_card)
        head.setContentsMargins(14, 14, 14, 14)
        head.setSpacing(14)

        self.thumb_label = QLabel()
        self.thumb_label.setFixedSize(200, 112)
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb_label.setStyleSheet(
            "background-color: rgba(128,128,128,0.14);"
            " border-radius: 8px; border: none;")
        if thumb:
            self.thumb_label.setPixmap(
                rounded_thumbnail(thumb, 200, 112, radius=8))
        head.addWidget(self.thumb_label)

        meta_col = QVBoxLayout()
        meta_col.setSpacing(6)
        title = self.info.get("title") or self.display.get("title", "Unknown")
        if self.info.get("_type") == "playlist":
            count = len([e for e in self.info.get("entries") or [] if e])
            title = f"[Playlist · {count} videos] {title}"
        t = QLabel(title)
        t.setWordWrap(True)
        t.setStyleSheet("font-size: 15px; font-weight: 600;")
        meta_col.addWidget(t)
        dur = QLabel("Duration: " + human_duration(self.display.get("duration")))
        dur.setObjectName("subtext")
        meta_col.addWidget(dur)
        meta_col.addStretch()
        head.addLayout(meta_col, 1)
        root.addWidget(head_card)

        # --- Option dropdown grid ---
        opts_header = QLabel("Filters")
        opts_header.setObjectName("sectionHeader")
        root.addWidget(opts_header)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(5)

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
        self.container_combo.currentIndexChanged.connect(self._repopulate_formats)
        self.codec_combo.currentTextChanged.connect(self._repopulate_formats)
        self.fps_combo.currentTextChanged.connect(self._repopulate_formats)
        root.addLayout(grid)

        # --- Format list ---
        fmt_header = QLabel("Available formats")
        fmt_header.setObjectName("sectionHeader")
        root.addWidget(fmt_header)

        self.fmt_scroll = QScrollArea()
        self.fmt_scroll.setWidgetResizable(True)
        self.fmt_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.fmt_container = QWidget()
        self.fmt_layout = QVBoxLayout(self.fmt_container)
        self.fmt_layout.setContentsMargins(0, 0, 6, 0)
        self.fmt_layout.setSpacing(6)
        self.fmt_layout.addStretch()
        self.fmt_scroll.setWidget(self.fmt_container)
        root.addWidget(self.fmt_scroll, 1)

        # --- Bottom action bar ---
        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setMinimumSize(120, 40)
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.clicked.connect(self.reject)
        actions.addWidget(cancel_btn)

        dl_btn = QPushButton("  Download")
        dl_btn.setObjectName("green")
        dl_btn.setIcon(safe_icon("fa5s.download", "#06240f"))
        dl_btn.setIconSize(QSize(15, 15))
        dl_btn.setMinimumSize(160, 40)
        dl_btn.setDefault(True)
        dl_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        dl_btn.clicked.connect(self._accept_download)
        actions.addWidget(dl_btn)
        root.addLayout(actions)

    # -- format list -------------------------------------------------------
    def _clear_formats(self):
        # Rows own their checkboxes, so dropping the row frames is enough.
        self.fmt_checkboxes.clear()
        while self.fmt_layout.count() > 1:
            item = self.fmt_layout.takeAt(0)
            if item.widget():
                item.widget().hide()
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
        container = self.container_combo.currentText()
        codec_filter = self.codec_combo.currentText()
        fps_filter = self.fps_combo.currentText()
        audio_size = self._best_audio_size()

        # Container → extension filter
        _CONTAINER_EXT = {"MP4": "mp4", "MKV": "mkv", "MP3": "mp3"}
        target_ext = _CONTAINER_EXT.get(container)

        rows = []
        if is_audio:
            for f in formats:
                if f.get("acodec") in (None, "none") or \
                   f.get("vcodec") not in (None, "none"):
                    continue
                if target_ext:
                    fmt_ext = (f.get("ext") or "").lower()
                    if target_ext == "mp3" and fmt_ext not in ("mp3", "m4a"):
                        continue
                    elif target_ext == "mkv" and fmt_ext not in ("mkv", "webm"):
                        continue
                    elif target_ext == "mp4" and fmt_ext not in ("mp4", "m4a"):
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
                if target_ext:
                    fmt_ext = (f.get("ext") or "").lower()
                    if target_ext == "mp4" and fmt_ext not in ("mp4", "m4a"):
                        continue
                    elif target_ext == "mkv" and fmt_ext not in ("mkv", "webm"):
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

        # Recommendation engine — pass filtered rows, not raw formats
        rec_fmts = [row[2] for row in rows]
        recommended = get_best_format(rec_fmts, "audio" if is_audio else "video")

        for _, label, f in rows[:24]:
            is_rec = (recommended is not None
                      and f.get("format_id") == recommended.get("format_id"))
            row = self._make_format_row(label, f, is_rec)
            self.fmt_layout.insertWidget(self.fmt_layout.count() - 1, row)

    def _make_format_row(self, label: str, fmt: dict,
                         is_rec: bool) -> QFrame:
        """One elevated row card: styled checkbox + optional recommended badge."""
        row = QFrame()
        row.setObjectName("row")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(12, 9, 12, 9)
        lay.setSpacing(10)

        cb = QCheckBox(label)
        cb.setProperty("fmt", fmt)
        cb.setCursor(Qt.CursorShape.PointingHandCursor)
        cb.setStyleSheet(
            f"font-family: {FONT_MONO}; font-size: 12px;"
            + (f" color: {ACCENT_HOVER}; font-weight: 600;" if is_rec else ""))
        if is_rec:
            cb.setChecked(True)
        lay.addWidget(cb, 1)

        if is_rec:
            badge = QLabel("⭐ Recommended")
            badge.setObjectName("badgeRecommended")
            glow = QGraphicsDropShadowEffect(badge)
            glow.setBlurRadius(18)
            glow.setOffset(0, 0)
            glow.setColor(QColor(34, 197, 94, 190))
            badge.setGraphicsEffect(glow)
            lay.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)

        self.fmt_checkboxes.append(cb)
        return row

    # -- accept ------------------------------------------------------------
    def _accept_download(self):
        checked = [cb for cb in self.fmt_checkboxes if cb.isChecked()]
        if not checked:
            return

        is_audio = self.type_combo.currentText() == "Audio"
        container = self.container_combo.currentText()
        subs = self.subs_combo.currentText()
        is_playlist = self.info.get("_type") == "playlist"

        self.selected_opts_list = []
        for cb in checked:
            fmt = cb.property("fmt")
            outtmpl = os.path.join(self.save_dir, "%(title)s.%(ext)s")
            if is_playlist:
                outtmpl = os.path.join(
                    self.save_dir, "%(playlist_title)s",
                    "%(playlist_index)s - %(title)s.%(ext)s")

            fmt_id = fmt.get("format_id", "best") if fmt else "best"
            height = fmt.get("height")
            suffix = f"_[{height}p]" if height else f"_[{fmt_id}]"
            base, ext = os.path.splitext(outtmpl)
            outtmpl_unique = f"{base}{suffix}{ext}"

            opts = {
                "outtmpl": outtmpl_unique,
                "ffmpeg_location": FFMPEG_DIR,
                "quiet": True,
                "no_warnings": True,
                "noplaylist": not is_playlist,
                "ignoreerrors": is_playlist,
                "format_sort": ["vcodec:h264", "acodec:m4a", "res", "fps"],
                "concurrent_fragment_downloads": 10,
            }

            if is_audio:
                opts["format"] = (f"{fmt_id}/bestaudio/best"
                                  if fmt else "bestaudio/best")
                codec = ("mp3" if container in ("Auto", "MP3")
                         else container.lower())
                opts["postprocessors"] = [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": codec if codec in ("mp3",) else "mp3",
                    "preferredquality": "192",
                }]
                kind = "audio"
            else:
                opts["format"] = f"{fmt_id}+bestaudio/best"
                merge = {"MP4": "mp4", "MKV": "mkv"}.get(container, "mp4")
                opts["merge_output_format"] = merge
                kind = "playlist" if is_playlist else "video"

                if subs != "None":
                    opts["writesubtitles"] = True
                    if subs == "Auto-generated":
                        opts["writeautomaticsub"] = True
                    opts["subtitleslangs"] = ["en"]
                    opts["postprocessors"] = [
                        {"key": "FFmpegEmbedSubtitle"}]

            # Carry the *chosen* format's quality to the UI row so it can't
            # fall back to the info dict's default resolution.
            quality_str = format_quality_label(fmt, is_audio)
            self.selected_opts_list.append((opts, kind, quality_str))
        self.accept()


# ============================================================================
# Download manager item
# ============================================================================

class DownloadItem(QFrame):
    THUMB_W, THUMB_H = 120, 68

    def __init__(self, title: str, meta: str, thumb: bytes, kind: str,
                 worker: DownloadWorker | None, quality_str: str = ""):
        super().__init__()
        self.setObjectName("downloadCard")
        self.kind = kind
        self.worker = worker
        self.file_path = ""
        self.quality_str = quality_str or ""

        # History wiring (set by MainWindow / restore_history).
        self.history = None          # DownloadHistory instance
        self.entry_id = ""           # id of this row's JSON record
        self.source_url = ""
        self.thumb_path = ""
        self.title_text = title
        self.meta_text = meta
        self._thumb_bytes = thumb or b""

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(14)

        self.thumb_label = QLabel()
        self.thumb_label.setFixedSize(self.THUMB_W, self.THUMB_H)
        self.thumb_label.setScaledContents(False)
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb_label.setStyleSheet(
            "background-color: rgba(128,128,128,0.14);"
            " border-radius: 8px; border: none;")
        lay.addWidget(self.thumb_label, 0, Qt.AlignmentFlag.AlignTop)

        mid = QVBoxLayout()
        mid.setSpacing(7)

        # Title row: title + status badge
        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        self.title_label = QLabel(title)
        self.title_label.setStyleSheet("font-size: 13px; font-weight: 600;")
        self.title_label.setWordWrap(True)
        title_row.addWidget(self.title_label, 1)

        # Quality chip reflects the format the user actually picked.
        self.quality_label = QLabel(self.quality_str)
        self.quality_label.setObjectName("badgeQuality")
        self.quality_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.quality_label.setVisible(bool(self.quality_str))
        title_row.addWidget(self.quality_label, 0, Qt.AlignmentFlag.AlignTop)

        self.badge = QLabel("Queued")
        self.badge.setObjectName("badgeQueued")
        self.badge.setProperty("class", "badge")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_row.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignTop)
        mid.addLayout(title_row)

        self.meta_label = QLabel(meta)
        self.meta_label.setObjectName("subtext")
        mid.addWidget(self.meta_label)

        # Progress bar + live speed label on one row
        pbar_row = QHBoxLayout()
        pbar_row.setSpacing(10)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        pbar_row.addWidget(self.bar, 1)
        self.speed_label = QLabel("")
        self.speed_label.setObjectName("subtext")
        self.speed_label.setFixedWidth(150)
        self.speed_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        pbar_row.addWidget(self.speed_label)
        mid.addLayout(pbar_row)

        self.stat_label = QLabel("Queued...")
        self.stat_label.setObjectName("subtext")
        mid.addWidget(self.stat_label)
        lay.addLayout(mid, 1)

        self.btns_layout = QVBoxLayout()
        self.btns_layout.setSpacing(6)
        self.btns_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.pause_btn = QPushButton("  Pause")
        self.pause_btn.setIcon(safe_icon("fa5s.pause", "#a1a1aa"))
        self.pause_btn.setObjectName("actionClear")
        self.pause_btn.setFixedWidth(112)
        self.pause_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.cancel_btn = QPushButton("  Cancel")
        self.cancel_btn.setIcon(safe_icon("fa5s.times", "#f87171"))
        self.cancel_btn.setObjectName("actionDelete")
        self.cancel_btn.setFixedWidth(112)
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.clicked.connect(self.cancel)
        self.btns_layout.addWidget(self.pause_btn)
        self.btns_layout.addWidget(self.cancel_btn)
        lay.addLayout(self.btns_layout)

        if worker is not None:
            worker.progress.connect(self.on_progress)
            worker.progress_update.connect(self.on_progress_update)
            worker.thumbnail_ready.connect(self.on_thumbnail_ready)
            worker.done.connect(self.on_done)

        if thumb:
            self._set_cropped_thumbnail(thumb)

    # -- status badge ------------------------------------------------------
    def set_badge(self, text: str, state: str):
        """state: queued | active | done | error — drives the pill tint."""
        self.badge.setText(text)
        self.badge.setObjectName({
            "queued": "badgeQueued",
            "active": "badgeActive",
            "done": "badgeDone",
            "error": "badgeError",
        }.get(state, "badgeQueued"))
        # Re-evaluate the stylesheet now the objectName changed.
        self.badge.style().unpolish(self.badge)
        self.badge.style().polish(self.badge)

    # -- thumbnail (async bytes -> square, centered crop) ---------------
    def on_thumbnail_ready(self, data: bytes):
        if data:
            self._thumb_bytes = data
            self._set_cropped_thumbnail(data)

    def _set_cropped_thumbnail(self, data: bytes):
        """Center-crop into a rounded frame with anti-aliased smooth scaling."""
        if not data:
            return
        self.thumb_label.setScaledContents(False)
        self.thumb_label.setPixmap(
            rounded_thumbnail(data, self.THUMB_W, self.THUMB_H, radius=8))

    # -- progress --------------------------------------------------------
    def on_progress_update(self, percent: float, speed: str):
        self.bar.setValue(int(percent * 10))   # 0-1000
        if speed:
            self.speed_label.setText(speed)
            self.set_badge("Downloading", "active")
        elif self.bar.value() >= 1000:
            self.speed_label.setText("Done")

    def toggle_pause(self):
        self.worker.is_paused = not self.worker.is_paused
        if self.worker.is_paused:
            self.pause_btn.setText("  Resume")
            self.pause_btn.setIcon(safe_icon("fa5s.play", "#a1a1aa"))
            self.stat_label.setText("Paused")
            self.set_badge("Paused", "queued")
        else:
            self.pause_btn.setText("  Pause")
            self.pause_btn.setIcon(safe_icon("fa5s.pause", "#a1a1aa"))
            self.stat_label.setText("Resuming...")
            self.set_badge("Downloading", "active")

    def cancel(self):
        self.worker.is_cancelled = True
        self.worker.is_paused = False
        self.stat_label.setText("Cancelling...")
        self.speed_label.setText("")
        self.set_badge("Canceled", "error")
        self._hide_ctrl_buttons()
        self._inject_remove_button()

    def _hide_ctrl_buttons(self):
        self.pause_btn.hide()
        self.cancel_btn.hide()

    @staticmethod
    def _row_action(text: str, icon: str, color: str, obj: str,
                    slot, tip: str = "") -> QPushButton:
        """Compact icon + text action button with a semantic tint."""
        b = QPushButton("  " + text)
        b.setIcon(safe_icon(icon, color))
        b.setIconSize(QSize(13, 13))
        b.setObjectName(obj)
        b.setFixedWidth(112)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        if tip:
            b.setToolTip(tip)
        b.clicked.connect(slot)
        return b

    def _inject_remove_button(self):
        if getattr(self, "_remove_btn", None) is not None:
            return
        self._remove_btn = self._row_action(
            "Remove", "fa5s.trash", "#a1a1aa", "actionClear",
            self.clear_row, "Remove this row from the list and history")
        self.btns_layout.addWidget(self._remove_btn)

    def on_progress(self, d: dict):
        self.bar.setValue(int(d.get("fraction", 0) * 1000))
        if d.get("processing"):
            self.stat_label.setText("Processing with ffmpeg...")
            self.speed_label.setText("")
            return
        eta = d.get("eta")
        parts = [f"{d.get('fraction', 0) * 100:.1f}%"]
        if d.get("total"):
            parts.append(f"{human_size(d['downloaded'])} / "
                         f"{human_size(d['total'])}")
        if eta:
            parts.append(f"ETA {eta}s")
        self.stat_label.setText("  ·  ".join(parts))

    # -- history -----------------------------------------------------------
    def bind_history(self, history, url: str, entry_id: str = ""):
        """Attach this row to the JSON history log."""
        self.history = history
        self.source_url = url or ""
        self.entry_id = entry_id or uuid.uuid4().hex

    def _save_history(self, status: str, message: str):
        """Persist this row's final state (completed / canceled / failed)."""
        if self.history is None:
            return
        if not self.thumb_path and self._thumb_bytes:
            self.thumb_path = self.history.cache_thumbnail(
                self._thumb_bytes, self.entry_id)
        self.history.add({
            "id": self.entry_id,
            "title": self.title_text,
            "meta": self.meta_text,
            "url": self.source_url,
            "file_path": self.file_path,
            "thumb_path": self.thumb_path,
            "kind": self.kind,
            "quality": self.quality_str,
            "status": status,
            "message": message,
        })

    # Legacy records (written before the quality chip existed) baked the info
    # dict's default resolution into the meta string. Lift it out so old rows
    # don't keep displaying a resolution the user never chose.
    _LEGACY_RES_RE = re.compile(r"\s*·\s*\d{3,4}p(?:\d{2})?|\s*·\s*\d+fps")

    @classmethod
    def from_history(cls, entry: dict, history) -> "DownloadItem":
        """Rebuild a finished row from a saved JSON record (no worker)."""
        thumb = history.read_thumbnail(entry.get("thumb_path", ""))
        meta = entry.get("meta") or ""
        quality = entry.get("quality") or ""
        if not quality:
            # Recover the resolution from the old meta string, then strip it.
            found = re.search(r"\b(\d{3,4}p(?:\d{2})?)\b", meta)
            if found:
                quality = found.group(1)
            meta = cls._LEGACY_RES_RE.sub("", meta).strip(" ·")

        item = cls(entry.get("title") or "Unknown",
                   meta,
                   thumb,
                   entry.get("kind") or "video",
                   None,
                   quality)
        item.history = history
        item.entry_id = entry.get("id") or uuid.uuid4().hex
        item.source_url = entry.get("url") or ""
        item.thumb_path = entry.get("thumb_path") or ""
        item.file_path = entry.get("file_path") or ""
        item.restore_finished(entry.get("status") or "completed",
                              entry.get("message") or "")
        return item

    def restore_finished(self, status: str, message: str):
        """Paint a history row in its terminal state, no live controls."""
        self.bar.hide()
        self.speed_label.setText("")
        self.pause_btn.hide()
        self.cancel_btn.hide()

        when = ""
        if status == "completed":
            self.set_badge("Completed", "done")
            self.stat_label.setText(message or "Completed")
            self.stat_label.setStyleSheet(
                f"color: {ACCENT_HOVER}; font-size: 11px; font-weight: 500;")
            self._swap_buttons_success()
        else:
            label = "Canceled" if status == "canceled" else "Failed"
            self.set_badge(label, "error")
            self.stat_label.setText(message or label)
            self.stat_label.setStyleSheet(
                f"color: {DANGER_HOVER}; font-size: 11px;")
            self._inject_remove_button()

    def on_done(self, ok: bool, msg: str, file_path: str):
        self.file_path = file_path

        if ok:
            self.bar.setValue(1000)
            self.speed_label.setText("")
            self.set_badge("Completed", "done")
            self.stat_label.setText(msg)
            self.stat_label.setStyleSheet(
                f"color: {ACCENT_HOVER}; font-size: 11px; font-weight: 500;")
            self._swap_buttons_success()
            self._save_history("completed", msg)
        else:
            self.pause_btn.setEnabled(False)
            self.cancel_btn.setEnabled(False)
            self._hide_ctrl_buttons()
            self._inject_remove_button()
            cancelled = msg.strip().lower().startswith("cancel")
            self.set_badge("Canceled" if cancelled else "Failed", "error")
            self.stat_label.setText(msg)
            self.stat_label.setStyleSheet(
                f"color: {DANGER_HOVER}; font-size: 11px;")
            self._save_history("canceled" if cancelled else "failed", msg)

    def _swap_buttons_success(self):
        self.pause_btn.hide()
        self.cancel_btn.hide()

        self.show_btn = self._row_action(
            "Show File", "fa5s.folder-open", INFO, "actionShow",
            self.show_file, "Reveal the file in your file manager")
        self.btns_layout.addWidget(self.show_btn)

        self.delete_btn = self._row_action(
            "Delete", "fa5s.trash", DANGER_HOVER, "actionDelete",
            self.delete_file, "Delete the file from disk")
        self.btns_layout.addWidget(self.delete_btn)

        self.clear_btn = self._row_action(
            "Clear", "fa5s.eraser", "#a1a1aa", "actionClear",
            self.clear_row,
            "Remove from the list and history (keeps the file on disk)")
        self.btns_layout.addWidget(self.clear_btn)

    def clear_row(self):
        """Drop the row from the UI *and* from downloads_history.json."""
        self._forget_history()
        self._remove_from_list()

    def _forget_history(self):
        """Delete this row's JSON record and its cached thumbnail."""
        if self.history is None or not self.entry_id:
            return
        self.history.remove(self.entry_id)
        if self.thumb_path and os.path.isfile(self.thumb_path):
            try:
                os.remove(self.thumb_path)
            except OSError:
                pass
        self.entry_id = ""

    def show_file(self):
        if not self.file_path or not os.path.isfile(self.file_path):
            self.stat_label.setText("File not found on disk.")
            self.stat_label.setStyleSheet("color: #ff5555; font-size: 11px;")
            return
        if sys.platform == "win32":
            subprocess.Popen(rf'explorer /select,"{self.file_path}"')
        else:
            dir_path = os.path.dirname(self.file_path)
            QDesktopServices.openUrl(QUrl.fromLocalFile(dir_path))

    def delete_file(self):
        if not self.file_path:
            self._remove_from_list()
            return
        reply = QMessageBox.question(
            self, "Delete File",
            f"Are you sure you want to delete this file?\n\n{self.file_path}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                if os.path.isfile(self.file_path):
                    os.remove(self.file_path)
            except OSError as e:
                self.stat_label.setText(f"❌ Delete failed: {e}")
                self.stat_label.setStyleSheet(
                    "color: #ff5555; font-size: 11px;")
                return
            self._forget_history()
            self._remove_from_list()

    def _remove_from_list(self):
        page = self.parent()
        while page is not None and not isinstance(page, DownloadsPage):
            page = page.parent()
        if isinstance(page, DownloadsPage):
            page.remove_item(self)
        else:
            parent = self.parent()
            if parent and parent.layout():
                parent.layout().removeWidget(self)
        if self.worker is not None:
            self.worker.is_cancelled = True
            self.worker.is_paused = False
        self.deleteLater()


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

        # --- Compact navigation bar ---
        navbar = QFrame()
        navbar.setObjectName("navbar")
        nav = QHBoxLayout(navbar)
        nav.setContentsMargins(10, 8, 10, 8)
        nav.setSpacing(4)

        self.back_btn = QToolButton()
        self.fwd_btn = QToolButton()
        self.reload_btn = QToolButton()
        self.home_btn = QToolButton()
        nav_buttons = (
            (self.back_btn, "fa5s.arrow-left", "Back"),
            (self.fwd_btn, "fa5s.arrow-right", "Forward"),
            (self.reload_btn, "fa5s.redo", "Refresh"),
            (self.home_btn, "fa5s.home", "Home"),
        )
        for btn, icon, tip in nav_buttons:
            btn.setObjectName("browsernav")
            btn.setAutoRaise(True)
            btn.setIcon(safe_icon(icon, "#a1a1aa"))
            btn.setIconSize(QSize(16, 16))
            btn.setToolTip(tip)
            btn.setFixedSize(34, 34)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            nav.addWidget(btn)
        nav.addSpacing(6)

        # --- Omnibox ---
        self.urlbox = QLineEdit()
        self.urlbox.setObjectName("addrbar")
        self.urlbox.setPlaceholderText("Search Google or enter a URL...")
        self.urlbox.setClearButtonEnabled(True)
        self.urlbox.setMinimumHeight(34)
        self.urlbox.setToolTip("Type a URL, or any text to search Google")
        self.addr = self.urlbox            # backwards-compatible alias
        nav.addWidget(self.urlbox, 1)
        lay.addWidget(navbar)

        # --- Page load progress (hairline under the nav bar) ---
        self.browser_progress = QProgressBar()
        self.browser_progress.setObjectName("browserProgress")
        self.browser_progress.setRange(0, 100)
        self.browser_progress.setValue(0)
        self.browser_progress.setTextVisible(False)
        self.browser_progress.setFixedHeight(3)
        self.browser_progress.hide()
        lay.addWidget(self.browser_progress)

        self.view = QWebEngineView(profile, self)
        self.browser = self.view
        lay.addWidget(self.view, 1)

        # --- Floating action button (FAB) ---
        self.dl_btn = QPushButton("  Download", self)
        self.dl_btn.setObjectName("fab")
        self.dl_btn.setIcon(safe_icon("fa5s.arrow-circle-down", "#000000"))
        self.dl_btn.setIconSize(QSize(18, 18))
        self.dl_btn.setFixedSize(176, 44)
        self.dl_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dl_btn.setToolTip("Download the media on this page")
        shadow = QGraphicsDropShadowEffect(self.dl_btn)
        shadow.setBlurRadius(26)
        shadow.setOffset(0, 5)
        shadow.setColor(QColor(0, 0, 0, 165))
        self.dl_btn.setGraphicsEffect(shadow)
        self.dl_btn.hide()
        self.dl_btn.clicked.connect(self.on_download_clicked)

        # --- Wiring ---
        self.back_btn.clicked.connect(self.view.back)
        self.fwd_btn.clicked.connect(self.view.forward)
        self.reload_btn.clicked.connect(self.view.reload)
        self.home_btn.clicked.connect(
            lambda: self.navigate("https://www.google.com"))
        self.urlbox.returnPressed.connect(self._addr_entered)
        self.view.urlChanged.connect(self._url_changed)

        # Loading progress
        self.view.loadStarted.connect(self._on_load_started)
        self.view.loadProgress.connect(self.browser_progress.setValue)
        self.view.loadFinished.connect(self._on_load_finished)

        self.back_btn.setEnabled(False)
        self.fwd_btn.setEnabled(False)

        self.navigate("https://www.youtube.com")

    # -- load progress / history state -------------------------------------
    def _on_load_started(self):
        self.browser_progress.setValue(0)
        self.browser_progress.show()
        self.reload_btn.setIcon(safe_icon("fa5s.times", "#a1a1aa"))
        self.reload_btn.setToolTip("Stop loading")

    def _on_load_finished(self, ok: bool):
        self.browser_progress.hide()
        self.browser_progress.setValue(0)
        self.reload_btn.setIcon(safe_icon("fa5s.redo", "#a1a1aa"))
        self.reload_btn.setToolTip("Refresh")
        self._update_nav_state()

    def _update_nav_state(self):
        """Grey out Back/Forward when there is nowhere to go."""
        history = self.browser.history()
        for btn, icon, enabled in (
            (self.back_btn, "fa5s.arrow-left", history.canGoBack()),
            (self.fwd_btn, "fa5s.arrow-right", history.canGoForward()),
        ):
            btn.setEnabled(enabled)
            btn.setIcon(safe_icon(icon, "#a1a1aa" if enabled else "#52525b"))

    def on_download_clicked(self):
        """Read the live URL at click time, sanitise it, then analyse."""
        current_url = self.browser.url().toString().strip()
        sanitized_url = clean_media_url(current_url)
        if not sanitized_url:
            QMessageBox.information(
                self, "Select a Video",
                "Please open a specific video or playlist page first "
                "before clicking Download.")
            return
        print(f"[Browser Download] Triggering analysis for: {sanitized_url}")
        self.request_download.emit(sanitized_url)

    # -- omnibox -----------------------------------------------------------
    @staticmethod
    def resolve_omnibox_input(text: str) -> str:
        """URL or Google search? Return the address to load."""
        text = (text or "").strip()
        if not text:
            return ""
        if text.startswith(("http://", "https://", "file://", "about:")):
            return text

        # A bare host needs a dot and no spaces to count as a domain.
        host = text.split("/", 1)[0].split("?", 1)[0]
        tld = host.rsplit(".", 1)[-1] if "." in host else ""
        looks_like_domain = ("." in host
                             and " " not in text
                             and not host.endswith(".")
                             and (tld.split(":")[0].isalpha() and len(tld) >= 2))
        is_ip = re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}(:\d+)?", host) is not None
        if looks_like_domain or is_ip or host.split(":")[0] == "localhost":
            return "https://" + text
        return ("https://www.google.com/search?q="
                + requests.utils.quote(text))

    def navigate(self, url: str):
        target = self.resolve_omnibox_input(url)
        if target:
            self.view.setUrl(QUrl(target))

    def _addr_entered(self):
        self.navigate(self.urlbox.text().strip())

    def _url_changed(self, qurl: QUrl):
        url = qurl.toString()
        self.urlbox.setText(url)
        self.urlbox.setCursorPosition(0)
        self._update_nav_state()
        if VIDEO_URL_RE.search(url):
            self.dl_btn.show()
            self.dl_btn.raise_()
        else:
            self.dl_btn.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_fab()

    def _place_fab(self):
        """Pin the FAB to the bottom-right corner of the page area."""
        margin = 24
        self.dl_btn.move(self.width() - self.dl_btn.width() - margin,
                         self.height() - self.dl_btn.height() - margin)


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

    def add_item(self, item: DownloadItem, at_end: bool = False):
        self.empty_label.hide()
        self.items.append(item)
        # Live downloads go on top; restored history appends below.
        index = self.list_lay.count() - 1 if at_end else 0
        self.list_lay.insertWidget(index, item)
        self.set_filter(self.current_filter)

    def remove_item(self, item: DownloadItem):
        if item in self.items:
            self.items.remove(item)
        self.list_lay.removeWidget(item)
        if not self.items:
            self.empty_label.show()

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
# Download history (JSON)
# ============================================================================

class DownloadHistory:
    """Append-only JSON log of finished/cancelled downloads.

    Each record: {id, title, meta, url, file_path, thumb_path, kind,
                  status, message, timestamp}
    """

    MAX_ENTRIES = 300

    def __init__(self, path: str | None = None):
        self.path = path or history_file_path()
        self._lock = threading.Lock()
        self.entries = self._read()

    # -- disk I/O ---------------------------------------------------------
    def _read(self) -> list:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return [e for e in data if isinstance(e, dict)] \
                if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _write(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.entries, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as e:
            print(f"[History] Save failed: {e}")

    # -- public API -------------------------------------------------------
    def all(self) -> list:
        """Newest first."""
        return list(reversed(self.entries))

    def add(self, record: dict) -> str:
        """Insert (or replace by id) one record and flush to disk."""
        with self._lock:
            entry_id = record.get("id") or uuid.uuid4().hex
            record = dict(record)
            record["id"] = entry_id
            record.setdefault("timestamp", time.time())
            self.entries = [e for e in self.entries if e.get("id") != entry_id]
            self.entries.append(record)
            if len(self.entries) > self.MAX_ENTRIES:
                self.entries = self.entries[-self.MAX_ENTRIES:]
            self._write()
        return entry_id

    def remove(self, entry_id: str):
        """Drop one record (used by the row's Clear/Remove buttons)."""
        if not entry_id:
            return
        with self._lock:
            before = len(self.entries)
            self.entries = [e for e in self.entries
                            if e.get("id") != entry_id]
            if len(self.entries) != before:
                self._write()

    def clear(self):
        with self._lock:
            self.entries = []
            self._write()

    # -- thumbnail cache --------------------------------------------------
    @staticmethod
    def cache_thumbnail(data: bytes, entry_id: str) -> str:
        """Persist thumbnail bytes so history rows keep their artwork."""
        if not data:
            return ""
        path = os.path.join(thumb_cache_dir(), f"{entry_id}.jpg")
        try:
            with open(path, "wb") as fh:
                fh.write(data)
            return path
        except OSError:
            return ""

    @staticmethod
    def read_thumbnail(path: str) -> bytes:
        if not path or not os.path.isfile(path):
            return b""
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            return b""


# ============================================================================
# Persistent settings
# ============================================================================

SETTINGS_ORG = "Alok"
SETTINGS_APP = "UniversalDownloaderPlus"

FORMAT_CHOICES = ["Always Ask", "Best Video (MP4)", "Best Audio (MP3)"]
THEME_CHOICES = ["Dark", "Light"]


def default_download_dir() -> str:
    """OS Downloads folder, falling back to ~/Downloads."""
    path = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.DownloadLocation)
    if not path:
        path = os.path.join(os.path.expanduser("~"), "Downloads")
    return os.path.normpath(path)


def get_settings() -> QSettings:
    return QSettings(SETTINGS_ORG, SETTINGS_APP)


def load_settings() -> dict:
    """Read persisted preferences, coercing types and applying defaults."""
    s = get_settings()

    download_dir = s.value("download_dir", "", type=str) or ""
    if not download_dir or not os.path.isdir(download_dir):
        download_dir = default_download_dir()

    theme = s.value("theme", "dark", type=str)
    if theme not in ("dark", "light"):
        theme = "dark"

    try:
        max_concurrent = int(s.value("max_concurrent_downloads", 3))
    except (TypeError, ValueError):
        max_concurrent = 3
    max_concurrent = max(1, min(10, max_concurrent))

    preferred_format = s.value("preferred_format", FORMAT_CHOICES[0], type=str)
    if preferred_format not in FORMAT_CHOICES:
        preferred_format = FORMAT_CHOICES[0]

    return {
        "download_dir": download_dir,
        "theme": theme,
        "max_concurrent_downloads": max_concurrent,
        "preferred_format": preferred_format,
    }


def save_settings(values: dict):
    s = get_settings()
    for key in ("download_dir", "theme", "max_concurrent_downloads",
                "preferred_format"):
        if key in values:
            s.setValue(key, values[key])
    s.sync()


class SettingsModal(QDialog):
    """Preferences dialog backed by QSettings."""

    def __init__(self, parent, values: dict):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.setMinimumWidth(560)
        self.values = dict(values)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(14)

        # --- Title header ---
        title_bar = QHBoxLayout()
        title_bar.setSpacing(10)
        icon_lab = QLabel()
        icon_lab.setPixmap(
            safe_icon("fa5s.sliders-h", ACCENT).pixmap(QSize(22, 22)))
        title_bar.addWidget(icon_lab)
        head_col = QVBoxLayout()
        head_col.setSpacing(2)
        title = QLabel("Settings")
        title.setObjectName("title")
        head_col.addWidget(title)
        subtitle = QLabel("Preferences are saved and restored on next launch.")
        subtitle.setObjectName("subtext")
        head_col.addWidget(subtitle)
        title_bar.addLayout(head_col)
        title_bar.addStretch()
        root.addLayout(title_bar)

        # --- Section: Storage Preferences ---
        storage, sform = self._section("Storage Preferences", root)
        sform.addWidget(self._label("Download Directory"), 0, 0)
        self.dir_edit = QLineEdit(self.values["download_dir"])
        self.dir_edit.setMinimumHeight(36)
        self.dir_edit.setPlaceholderText("Where finished files are saved")
        sform.addWidget(self.dir_edit, 0, 1)

        self.browse_btn = QPushButton("  Browse")
        self.browse_btn.setIcon(safe_icon("fa5s.folder-open", "#a1a1aa"))
        self.browse_btn.setMinimumHeight(36)
        self.browse_btn.setFixedWidth(118)
        self.browse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.browse_btn.clicked.connect(self.browse_dir)
        sform.addWidget(self.browse_btn, 0, 2)

        # --- Section: Download Limits ---
        limits, lform = self._section("Download Limits", root)
        lform.addWidget(self._label("Max Concurrent Downloads"), 0, 0)
        self.concurrent_spin = QSpinBox()
        self.concurrent_spin.setRange(1, 10)
        self.concurrent_spin.setValue(self.values["max_concurrent_downloads"])
        self.concurrent_spin.setMinimumHeight(36)
        self.concurrent_spin.setToolTip(
            "Extra downloads beyond this limit wait in the queue.")
        lform.addWidget(self.concurrent_spin, 0, 1, 1, 2)

        lform.addWidget(self._label("Default Format"), 1, 0)
        self.format_combo = QComboBox()
        self.format_combo.addItems(FORMAT_CHOICES)
        self.format_combo.setCurrentText(self.values["preferred_format"])
        self.format_combo.setMinimumHeight(36)
        self.format_combo.setToolTip(
            "\"Always Ask\" opens the format dialog for every download.")
        lform.addWidget(self.format_combo, 1, 1, 1, 2)

        # --- Section: Theme ---
        theme_card, tform = self._section("Theme", root)
        tform.addWidget(self._label("Appearance"), 0, 0)
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(THEME_CHOICES)          # ["Dark", "Light"]
        self.theme_combo.setCurrentText(self.values["theme"].capitalize())
        self.theme_combo.setMinimumHeight(36)
        self.theme_combo.setToolTip("Applied immediately after saving.")
        tform.addWidget(self.theme_combo, 0, 1, 1, 2)

        root.addStretch()

        info = QLabel(f"ffmpeg: {FFMPEG_DIR}")
        info.setObjectName("subtext")
        info.setWordWrap(True)
        root.addWidget(info)

        # --- Buttons --------------------------------------------------------
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.addStretch()

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setMinimumSize(120, 40)
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.cancel_btn)

        self.save_btn = QPushButton("  Save")
        self.save_btn.setObjectName("green")
        self.save_btn.setIcon(safe_icon("fa5s.check", "#06240f"))
        self.save_btn.setIconSize(QSize(14, 14))
        self.save_btn.setMinimumSize(140, 40)
        self.save_btn.setDefault(True)
        self.save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_btn.clicked.connect(self.on_save)
        btn_row.addWidget(self.save_btn)

        root.addLayout(btn_row)

    @staticmethod
    def _section(title: str, parent_layout) -> tuple:
        """Titled card group; returns (card, grid) with aligned columns."""
        header = QLabel(title)
        header.setObjectName("sectionHeader")
        parent_layout.addWidget(header)

        card = QFrame()
        card.setObjectName("card")
        grid = QGridLayout(card)
        grid.setContentsMargins(16, 16, 16, 16)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        grid.setColumnMinimumWidth(0, 190)
        grid.setColumnStretch(1, 1)
        parent_layout.addWidget(card)
        return card, grid

    @staticmethod
    def _label(text: str) -> QLabel:
        lab = QLabel(text)
        lab.setStyleSheet("font-size: 12px; font-weight: 500;")
        lab.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return lab

    def browse_dir(self):
        start = self.dir_edit.text().strip() or default_download_dir()
        folder = QFileDialog.getExistingDirectory(
            self, "Choose download folder", start)
        if folder:
            self.dir_edit.setText(os.path.normpath(folder))

    def on_save(self):
        folder = self.dir_edit.text().strip()
        if not folder:
            folder = default_download_dir()
        folder = os.path.normpath(folder)
        if not os.path.isdir(folder):
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError as e:
                QMessageBox.warning(
                    self, "Invalid folder",
                    f"Could not use this download folder:\n{folder}\n\n{e}")
                return

        self.values.update({
            "download_dir": folder,
            "max_concurrent_downloads": self.concurrent_spin.value(),
            "preferred_format": self.format_combo.currentText(),
            "theme": self.theme_combo.currentText().lower(),
        })
        save_settings(self.values)
        self.accept()


# ============================================================================
# Main window
# ============================================================================

class MainWindow(QMainWindow):
    # (accessible name, qtawesome icon, tooltip) — rendered icon-only.
    NAV_ITEMS = (
        ("Home", "fa5s.home", "Home"),
        ("Browser", "fa5s.compass", "Browser"),
        ("Downloads", "fa5s.arrow-circle-down", "Downloads"),
    )

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Universal Downloader+")
        self.resize(1180, 760)

        # --- Persisted preferences (loaded on startup) ---
        self.settings = load_settings()
        self.save_dir = self.settings["download_dir"]
        self.max_concurrent = self.settings["max_concurrent_downloads"]
        self.preferred_format = self.settings["preferred_format"]

        self.workers = []           # keep refs so QThreads aren't GC'd
        self.queue = []             # pending (args) beyond the concurrency cap
        self.analyze_worker = None
        self.dark = self.settings["theme"] == "dark"

        # Persistent download history (downloads_history.json).
        self.history = DownloadHistory()

        # --- Persistent web profile: logins/cookies survive restarts and are
        # mirrored to a Netscape jar so yt-dlp inherits the session. ---
        self.cookie_path = cookie_file_path()
        self._init_cookie_jar()

        storage = browser_data_dir()
        cache = os.path.join(storage, "cache")
        os.makedirs(cache, exist_ok=True)
        if not os.access(storage, os.W_OK):
            print(f"[Browser] WARNING: profile path not writable: {storage}")

        # Named (non-OTR) profile: logins persist across restarts.
        self.profile = QWebEngineProfile("udl", self)
        self.profile.setPersistentStoragePath(storage)
        self.profile.setCachePath(cache)
        self.profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
        self.profile.setHttpCacheType(
            QWebEngineProfile.HttpCacheType.DiskHttpCache)
        print(f"[Browser] Persistent profile at: {storage} "
              f"(off-the-record={self.profile.isOffTheRecord()})")
        self.profile.cookieStore().cookieAdded.connect(
            self._export_cookie_to_netscape)
        # self.interceptor = AdBlockInterceptor()          # disabled
        # self.profile.setUrlRequestInterceptor(self.interceptor)
        # script = QWebEngineScript()                       # disabled
        # script.setName("adskip")
        # script.setSourceCode(AD_SKIP_JS)
        # script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
        # script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        # script.setRunsOnSubFrames(True)
        # self.profile.scripts().insert(script)

        self._build_ui()
        self.apply_settings()
        self.restore_history()

    # ------------------------------------------------------------------
    # Download history
    # ------------------------------------------------------------------
    def restore_history(self):
        """Rebuild saved download rows in the Downloads tab on startup."""
        entries = self.history.all()          # newest first
        if not entries:
            return
        restored = 0
        for entry in entries:
            try:
                item = DownloadItem.from_history(entry, self.history)
            except Exception as e:
                print(f"[History] Skipped a bad record: {e}")
                continue
            self.downloads_page.add_item(item, at_end=True)
            restored += 1
        if restored:
            self.statusBar().showMessage(
                f"Restored {restored} download(s) from history.")

    # ------------------------------------------------------------------
    # Cookie export (QWebEngine -> Netscape jar for yt-dlp)
    # ------------------------------------------------------------------
    def _init_cookie_jar(self):
        """Start a fresh jar each launch so stale/expired cookies don't pile up."""
        self._seen_cookies = set()
        try:
            with open(self.cookie_path, "w", encoding="utf-8") as fh:
                fh.write("# Netscape HTTP Cookie File\n")
                fh.write("# Generated by Universal Downloader+ — do not edit.\n")
        except OSError as e:
            print(f"[Cookies] Could not create jar: {e}")

    def _export_cookie_to_netscape(self, cookie: QNetworkCookie):
        """Append one QNetworkCookie as a strict 7-column Netscape record.

        Columns: domain, include_subdomains, path, secure, expiration,
        name, value — tab separated.
        """
        try:
            domain = bytes(cookie.domain()).decode() if isinstance(
                cookie.domain(), (bytes, bytearray)) else cookie.domain()
            domain = (domain or "").strip()
            if not domain:
                return

            include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
            path = cookie.path() or "/"
            secure = "TRUE" if cookie.isSecure() else "FALSE"

            expiry = cookie.expirationDate()
            if cookie.isSessionCookie() or not expiry.isValid():
                # Session cookies have no expiry; give yt-dlp a usable window.
                expiration = int(time.time()) + 86400
            else:
                expiration = int(expiry.toSecsSinceEpoch())

            name = bytes(cookie.name()).decode("utf-8", "replace")
            value = bytes(cookie.value()).decode("utf-8", "replace")

            key = (domain, path, name)
            if key in self._seen_cookies:
                return
            self._seen_cookies.add(key)

            line = "\t".join([domain, include_subdomains, path, secure,
                              str(expiration), name, value])
            with open(self.cookie_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception as e:
            print(f"[Cookies] Export failed: {e}")

    # ------------------------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.setCentralWidget(central)

        # ================= Header =================
        header = QFrame()
        header.setObjectName("header")
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
        h.setSpacing(8)

        self.smart_toggle = ToggleSwitch("Smart Mode")
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
        for lab, combo, width in [("Format", self.preset_format, 90),
                                  ("Quality", self.preset_quality, 92),
                                  ("Container", self.preset_container, 90)]:
            combo.setObjectName("preset")
            combo.setFixedWidth(width)
            combo.setCursor(Qt.CursorShape.PointingHandCursor)
            h.addLayout(self._preset_column(lab, combo))

        self.dir_btn = QPushButton(
            "  " + (os.path.basename(self.save_dir) or self.save_dir))
        self.dir_btn.setObjectName("preset")
        self.dir_btn.setIcon(safe_icon("fa5s.folder-open", NAV_IDLE_DARK))
        self.dir_btn.setIconSize(QSize(12, 12))
        self.dir_btn.setMaximumWidth(170)
        self.dir_btn.setToolTip(self.save_dir)
        self.dir_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dir_btn.clicked.connect(self.pick_dir)
        h.addLayout(self._preset_column("Save to", self.dir_btn))

        h.addStretch()

        self.status_dot = QLabel("● 0 active")
        self.status_dot.setStyleSheet("color: gray;")
        h.addWidget(self.status_dot)

        # --- Utility controls (icon-only, matches the main nav bar) ---
        h.addSpacing(4)

        self.theme_btn = self._make_util_button(
            "fa5s.moon", "Switch to Light Mode", self.toggle_theme)
        h.addWidget(self.theme_btn)

        self.settings_btn = self._make_util_button(
            "fa5s.sliders-h", "Settings", self.open_settings)
        h.addWidget(self.settings_btn)

        header_col.addLayout(h)
        root.addWidget(header)

        # ================= Nav tabs (strictly icon-only) =================
        tabs = QFrame()
        tl = QHBoxLayout(tabs)
        tl.setContentsMargins(12, 4, 12, 4)
        tl.setSpacing(4)
        self.nav_btns = []
        for i, (name, icon, tip) in enumerate(self.NAV_ITEMS):
            b = QToolButton()
            b.setObjectName("mainnav")
            b.setCheckable(True)
            b.setAutoRaise(True)
            b.setIcon(safe_icon(icon, NAV_IDLE_DARK))
            b.setIconSize(QSize(22, 22))
            b.setFixedSize(40, 40)          # compact; never stretches the row
            b.setToolTip(tip)
            b.setAccessibleName(name)
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
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
        self.browser_page.request_download.connect(self.on_browser_download)

        # ================= Status bar =================
        self.statusBar().showMessage("Ready.")
        self.switch_page(0)

    @staticmethod
    def _preset_column(label: str, widget) -> QVBoxLayout:
        """Muted micro-header stacked above a compact preset control."""
        col = QVBoxLayout()
        col.setSpacing(2)
        col.setContentsMargins(0, 0, 0, 0)
        lw = QLabel(label)
        lw.setObjectName("presetLabel")
        col.addWidget(lw)
        col.addWidget(widget)
        return col

    @staticmethod
    def _make_util_button(icon: str, tip: str, slot) -> QToolButton:
        """Icon-only top-bar utility button styled like the main nav."""
        b = QToolButton()
        b.setObjectName("utilnav")
        b.setAutoRaise(True)
        b.setIcon(safe_icon(icon, "#c9d1d9"))
        b.setIconSize(QSize(24, 24))
        b.setFixedSize(44, 42)
        b.setToolTip(tip)
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(slot)
        return b

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    def switch_page(self, idx: int):
        self.stack.setCurrentIndex(idx)
        for i, b in enumerate(self.nav_btns):
            b.setChecked(i == idx)
        self.refresh_nav_icons()

    def refresh_nav_icons(self):
        """Tint nav icons to match the QSS text colour for each state."""
        idle = NAV_IDLE_DARK if self.dark else NAV_IDLE_LIGHT
        for i, b in enumerate(self.nav_btns):
            color = ACCENT if b.isChecked() else idle
            b.setIcon(safe_icon(self.NAV_ITEMS[i][1], color))

    def open_in_browser(self, url: str):
        self.switch_page(1)
        self.browser_page.navigate(url)

    def on_browser_download(self, url: str):
        """Feed a URL coming from the embedded browser into the main pipeline."""
        sanitized_url = clean_media_url(url)
        if not sanitized_url:
            QMessageBox.information(
                self, "Select a Video",
                "Please open a specific video or playlist page first "
                "before clicking Download.")
            return
        self.url_edit.setText(sanitized_url)
        self.analyze_url(sanitized_url)

    def pick_dir(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose download folder", self.save_dir)
        if folder:
            self.set_save_dir(os.path.normpath(folder))
            save_settings({"download_dir": self.save_dir})

    def set_save_dir(self, folder: str):
        self.save_dir = folder
        self.settings["download_dir"] = folder
        self.dir_btn.setText("  " + (os.path.basename(folder) or folder))
        self.dir_btn.setToolTip(folder)

    def toggle_theme(self):
        """Flip dark/light, persist the choice, and refresh the sun/moon icon."""
        self.dark = not self.dark
        self.settings["theme"] = "dark" if self.dark else "light"
        save_settings({"theme": self.settings["theme"]})
        self.apply_theme()
        self.statusBar().showMessage(
            f"{'Dark' if self.dark else 'Light'} mode enabled.")

    def apply_theme(self):
        """Switch qdarktheme dynamically; layer the design-system QSS on top."""
        qdarktheme.setup_theme(
            "dark" if self.dark else "light",
            additional_qss=build_qss(self.dark),
        )
        t = theme_tokens(self.dark)
        fg = t["text"]
        util_fg = t["text_muted"]

        # Moon while in dark mode, sun while in light mode.
        self.theme_btn.setIcon(
            safe_icon("fa5s.moon" if self.dark else "fa5s.sun", util_fg))
        self.theme_btn.setToolTip(
            "Switch to Light Mode" if self.dark else "Switch to Dark Mode")
        self.settings_btn.setIcon(safe_icon("fa5s.sliders-h", util_fg))

        self.paste_btn.setIcon(qta.icon("fa5s.clipboard", color=fg))
        self.dir_btn.setIcon(safe_icon("fa5s.folder-open", util_fg))
        self.refresh_nav_icons()

    def open_settings(self):
        dlg = SettingsModal(self, self.settings)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.settings.update(dlg.values)
            self.apply_settings()
            self.statusBar().showMessage("Settings saved.")

    def apply_settings(self):
        """Push the current settings dict into the live UI/runtime state."""
        self.set_save_dir(self.settings["download_dir"])
        self.max_concurrent = self.settings["max_concurrent_downloads"]
        self.preferred_format = self.settings["preferred_format"]

        # Default format preset mirrors the saved preference.
        if self.preferred_format == "Best Audio (MP3)":
            self.preset_format.setCurrentText("Audio")
            self.preset_container.setCurrentText("Auto")
            self.smart_toggle.setChecked(True)
        elif self.preferred_format == "Best Video (MP4)":
            self.preset_format.setCurrentText("Video")
            self.preset_quality.setCurrentText("Best")
            self.preset_container.setCurrentText("MP4")
            self.smart_toggle.setChecked(True)
        else:                                   # "Always Ask"
            self.smart_toggle.setChecked(False)

        self.dark = self.settings["theme"] == "dark"
        self.apply_theme()
        self.pump_queue()

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
        self.go_btn.setText("  Analyzing...")
        self.analyze_worker = AnalyzeWorker(url)
        self.analyze_worker.result.connect(
            lambda info, thumb, u=url: self.on_analyzed(u, info, thumb))
        self.analyze_worker.playlist.connect(
            lambda info, thumb, u=url: self.on_playlist_found(u, info, thumb))
        self.analyze_worker.error.connect(self.on_analyze_error)
        self.analyze_worker.start()

    def on_analyze_error(self, msg: str):
        self.paste_btn.setEnabled(True)
        self.go_btn.setEnabled(True)
        self.statusBar().showMessage("Ready.")
        self.go_btn.setText("  Download")
        QMessageBox.warning(
            self, "Unsupported link",
            "Could not find a supported video on this page.\n"
            "Please try another link.",
            QMessageBox.StandardButton.Ok,
            QMessageBox.StandardButton.Ok)

    def on_analyzed(self, url: str, info: dict, thumb: bytes):
        self.paste_btn.setEnabled(True)
        self.go_btn.setEnabled(True)
        self.go_btn.setText("  Download")
        self.statusBar().showMessage("Ready.")

        if self.smart_toggle.isChecked():
            self.smart_download_headless(url, info, thumb)
            return

        modal = DownloadModal(self, info, thumb, self.save_dir)
        if modal.exec() == QDialog.DialogCode.Accepted and modal.selected_opts_list:
            for opts, kind, quality in modal.selected_opts_list:
                self.start_download(url, opts, info, kind, thumb, quality)

    # ------------------------------------------------------------------
    # Playlist handling
    # ------------------------------------------------------------------
    def on_playlist_found(self, url: str, info: dict, thumb: bytes):
        self.paste_btn.setEnabled(True)
        self.go_btn.setEnabled(True)
        self.go_btn.setText("  Download")
        self.statusBar().showMessage("Ready.")

        if self.smart_toggle.isChecked():
            # Smart Mode treats everything headlessly - just grab the first
            # video (or use playlist form). Keep it simple: download whole list.
            self.smart_download_headless(url, info, thumb)
            return

        entries = [e for e in info.get("entries") or [] if e]
        count = len(entries)
        if not count:
            self.statusBar().showMessage("Playlist has no playable videos.")
            return

        choice = self._ask_playlist(count)
        if choice == "all":
            self._download_playlist_all(url, info, entries, thumb)
        elif choice == "single":
            self._download_playlist_single(url, info, entries[0], thumb)
        # else: cancelled, do nothing

    def _ask_playlist(self, count: int):
        """Dark-themed QDialog offering playlist vs. single-video download."""
        box = QDialog(self)
        box.setWindowTitle("Playlist detected")
        box.setMinimumWidth(420)

        lay = QVBoxLayout(box)
        lay.setSpacing(14)

        msg = QLabel(
            f"This link contains a playlist with <b>{count}</b> videos.\n"
            "What would you like to do?")
        msg.setWordWrap(True)
        msg.setStyleSheet("font-size: 14px;")
        lay.addWidget(msg)

        btn_all = QPushButton("Download Entire Playlist")
        btn_all.setObjectName("green")
        btn_all.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_all.clicked.connect(lambda: box.done(1))
        lay.addWidget(btn_all)

        btn_one = QPushButton("Download Single Video")
        btn_one.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_one.clicked.connect(lambda: box.done(2))
        lay.addWidget(btn_one)

        btn_cancel = QPushButton("Cancel")
        btn_cancel.setObjectName("danger")
        btn_cancel.clicked.connect(box.reject)
        lay.addWidget(btn_cancel)

        result = box.exec()
        if result == 1:
            return "all"
        if result == 2:
            return "single"
        return "cancel"

    def _download_playlist_single(self, url: str, info: dict, first, thumb: bytes):
        """Download only the first entry via the normal modal flow."""
        single_url = (first.get("webpage_url")
                      or first.get("url")
                      or url)
        modal = DownloadModal(self, first, thumb, self.save_dir)
        if modal.exec() == QDialog.DialogCode.Accepted and modal.selected_opts_list:
            for opts, kind, quality in modal.selected_opts_list:
                self.start_download(single_url, opts, first, kind, thumb,
                                    quality)

    def _download_playlist_all(self, url: str, info: dict, entries: list,
                               thumb: bytes):
        """Headless batch: best format, one worker per video, limited
        concurrency to avoid rate-limiting."""
        media_type = ("audio"
                      if self.preset_format.currentText() == "Audio"
                      else "video")
        best = get_best_format(info, media_type)
        playlist_title = info.get("title") or "Playlist"
        total = len(entries)

        # Build a playlist-scoped opt template once, then clone per entry.
        opts, kind = self.build_smart_opts(info, best)
        # Each batch worker downloads a single video, so drop the playlist
        # context: fallback title keeps the folder name sane.
        outtmpl = os.path.join(
            self.save_dir, "%(playlist_title|Playlist)s",
            "%(title)s.%(ext)s")
        opts["outtmpl"] = outtmpl
        opts["noplaylist"] = True

        quality = self.smart_quality_label(best, media_type)

        for i, entry in enumerate(entries[: self.PLAYLIST_MAX]):
            entry_url = (entry.get("webpage_url")
                         or entry.get("url")
                         or url)
            per_entry = dict(opts)
            QTimer.singleShot(  # stagger starts to dodge rate limiting
                i * self.PLAYLIST_STAGGER_MS,
                lambda u=entry_url, o=per_entry, e=entry, q=quality: (
                    self._start_playlist_entry(u, o, e, playlist_title, total,
                                               q)))

    def _start_playlist_entry(self, url, opts, entry, playlist_title, total,
                              quality_str: str = ""):
        """Spawn one DownloadWorker + UI row for a single playlist entry."""
        title = entry.get("title") or "Unknown"
        display_title = f"[{playlist_title} · {total}] {title}"

        # No per-entry height here either: the chip shows the requested
        # quality, which is uniform across the batch.
        meta_parts = [human_duration(entry.get("duration"))]
        if entry.get("ext"):
            meta_parts.append(entry["ext"].upper())
        uploader = entry.get("uploader") or entry.get("channel")
        if uploader:
            meta_parts.append(uploader)
        meta = "  ·  ".join(str(p) for p in meta_parts if p and p != "?")

        worker = DownloadWorker(
            url, self.with_save_dir(opts),
            thumb_url=entry.get("thumbnail") or "")
        item = DownloadItem(display_title, meta, b"", "playlist", worker,
                            quality_str)
        item.bind_history(self.history, url)
        self.downloads_page.add_item(item)
        self.enqueue_worker(worker)

    PLAYLIST_STAGGER_MS = 350   # per-video start delay (rate-limit guard)
    PLAYLIST_MAX = 999

    # ------------------------------------------------------------------
    # Smart Mode (headless) — no dialog
    # ------------------------------------------------------------------
    def smart_download_headless(self, url: str, info: dict, thumb: bytes):
        """Bypass the modal: pick the best format with the current presets and
        enqueue it straight to a DownloadWorker. Errors surface in a dialog
        instead of crashing the app."""
        try:
            media_type = ("audio"
                          if self.preset_format.currentText() == "Audio"
                          else "video")
            best = get_best_format(info, media_type)
            opts, kind = self.build_smart_opts(info, best)
            self.start_download(url, opts, info, kind, thumb,
                                self.smart_quality_label(best, media_type))
        except Exception as e:
            print(f"Smart Mode failed: {e}")
            QMessageBox.critical(
                self, "Smart Mode",
                f"Automatic download failed:\n{e}")

    def smart_quality_label(self, best: dict | None, media_type: str) -> str:
        """Quality chip for Smart Mode rows.

        The preset dropdown is authoritative when it pins a resolution
        (e.g. "720p"); otherwise fall back to whatever format was picked.
        """
        if media_type == "audio":
            return format_quality_label(best, is_audio=True)
        preset = self.preset_quality.currentText()
        if preset != "Best":
            return preset                      # user pinned 1080p/720p/...
        return format_quality_label(best, is_audio=False)

    # ------------------------------------------------------------------
    # Smart mode opts from global presets
    # ------------------------------------------------------------------
    def build_smart_opts(self, info: dict, best: dict | None = None):
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
            "format_sort": ["vcodec:h264", "acodec:m4a", "res", "fps"],
            "concurrent_fragment_downloads": 10,
        }

        # Use the headless recommendation when available (Smart Mode).
        best_id = (best or {}).get("format_id")

        if is_audio:
            opts["format"] = (f"{best_id}/bestaudio/best"
                              if best_id else "bestaudio/best")
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
            return opts, "audio"

        if quality == "Best":
            opts["format"] = ("bestvideo+bestaudio/best"
                              if not best_id else
                              f"{best_id}+bestaudio/best")
        else:
            h = quality.rstrip("p")
            opts["format"] = (f"bestvideo[height<={h}]+bestaudio"
                              f"/best[height<={h}]")
        opts["merge_output_format"] = {"MKV": "mkv"}.get(container, "mp4")
        return opts, ("playlist" if is_playlist else "video")

    # ------------------------------------------------------------------
    # Download management
    # ------------------------------------------------------------------
    def start_download(self, url, ydl_opts, info, kind, thumb,
                       quality_str: str = ""):
        display = info
        if info.get("_type") == "playlist" and info.get("entries"):
            entries = [e for e in info["entries"] if e]
            if entries:
                display = entries[0]

        title = info.get("title") or display.get("title", "Unknown")
        if kind == "playlist":
            count = len([e for e in info.get("entries") or [] if e])
            title = f"[Playlist · {count}] {title}"

        # Quality comes from the *selected* format. Never fall back to
        # display["height"]: that is the info dict's default stream and was
        # the source of every row reading "1080p".
        if not quality_str:
            quality_str = format_quality_label(None, kind == "audio")

        meta_parts = [human_duration(display.get("duration"))]
        if display.get("ext"):
            meta_parts.append(display["ext"].upper())
        uploader = display.get("uploader") or display.get("channel")
        if uploader:
            meta_parts.append(uploader)
        meta = "  ·  ".join(str(p) for p in meta_parts if p and p != "?")

        worker = DownloadWorker(
            url, self.with_save_dir(ydl_opts),
            thumb_url=display.get("thumbnail") or "")
        item = DownloadItem(title, meta, thumb, kind, worker, quality_str)
        item.bind_history(self.history, url)
        self.downloads_page.add_item(item)
        self.enqueue_worker(worker)

        self.switch_page(2)
        self.statusBar().showMessage(f"Download started: {title}")

    # ------------------------------------------------------------------
    # Concurrency queue (honours settings["max_concurrent_downloads"])
    # ------------------------------------------------------------------
    def with_save_dir(self, opts: dict) -> dict:
        """Force the output template to live under the saved download dir."""
        opts = dict(opts)
        tmpl = opts.get("outtmpl") or "%(title)s.%(ext)s"
        if isinstance(tmpl, dict):
            tmpl = tmpl.get("default") or "%(title)s.%(ext)s"

        if os.path.isabs(tmpl):
            rel = os.path.relpath(tmpl, self.save_dir)
            if rel.startswith(".."):
                # Built against a stale directory: keep the templated tail
                # (e.g. "%(playlist_title)s/%(title)s.%(ext)s").
                parts = []
                head, tail = os.path.split(tmpl)
                parts.append(tail)
                while "%(" in os.path.basename(head):
                    head, tail = os.path.split(head)
                    parts.append(tail)
                rel = os.path.join(*reversed(parts))
            tmpl = rel

        opts["outtmpl"] = os.path.join(self.save_dir, tmpl)
        opts.setdefault("paths", {})["home"] = self.save_dir
        return opts

    def enqueue_worker(self, worker: DownloadWorker):
        self.workers.append(worker)
        worker.done.connect(lambda *_: self.on_worker_done())
        self.queue.append(worker)
        self.pump_queue()

    def pump_queue(self):
        """Start queued workers while under the concurrency limit."""
        limit = max(1, int(getattr(self, "max_concurrent", 3)))
        while self.queue and self.active_count() < limit:
            worker = self.queue.pop(0)
            if worker.is_cancelled:
                continue
            worker.start()
        self.update_active_count()

    def on_worker_done(self):
        self.pump_queue()
        self.update_active_count()

    def active_count(self) -> int:
        return sum(1 for w in self.workers if w.isRunning())

    def update_active_count(self):
        active = self.active_count()
        queued = len(self.queue)
        color = "#3fb950" if active else "#6b7280"
        text = f"● {active} active"
        if queued:
            text += f"  ·  {queued} queued"
        self.status_dot.setText(text)
        self.status_dot.setStyleSheet(f"color: {color};")

    def closeEvent(self, event):
        self.queue.clear()
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
    app.setOrganizationName(SETTINGS_ORG)
    app.setApplicationName(SETTINGS_APP)

    # Application icon (taskbar / system tray). Use the .ico so Windows picks
    # the correct native resolution for the taskbar and title bar.
    app_icon_path = get_resource_path("icon.ico")
    if os.path.exists(app_icon_path):
        app.setWindowIcon(QIcon(app_icon_path))

    # Honour the persisted theme before the first paint.
    saved_theme = load_settings()["theme"]
    qdarktheme.setup_theme(saved_theme,
                           additional_qss=build_qss(saved_theme == "dark"))
    win = MainWindow()
    win.apply_theme()  # sync icons/tooltips with the saved theme

    # Window icon (top-left of the main window).
    if os.path.exists(app_icon_path):
        win.setWindowIcon(QIcon(app_icon_path))

    # yt-dlp ships as the bundled Python package, not a standalone binary.
    # Only ffmpeg/ffprobe need to exist next to the app.
    exe = ".exe" if os.name == "nt" else ""
    missing = [
        name for name in (f"ffmpeg{exe}", f"ffprobe{exe}")
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
