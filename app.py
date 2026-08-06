"""
Universal Downloader - 4K Video Downloader+ inspired GUI.

Workflow: paste URL -> Analyze Link -> pick quality -> Download.
Supports pause/resume and cancel via progress-hook flag checks.

Requires ffmpeg.exe / ffprobe.exe next to this script.
"""

import io
import os
import sys
import time
import threading
import tkinter.filedialog as filedialog

import requests
import customtkinter as ctk
from PIL import Image
import yt_dlp


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


def get_base_dir() -> str:
    """Directory containing this script (or the frozen exe)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


class DownloadCancelled(Exception):
    """Raised inside the progress hook to abort a running download."""


class DownloaderApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Universal Downloader")
        self.geometry("680x640")
        self.minsize(680, 640)

        self.base_dir = get_base_dir()
        self.download_dir = os.path.join(os.path.expanduser("~"), "Downloads")

        # State
        self.video_info = None          # metadata from extract_info
        self.quality_map = {}           # dropdown label -> yt-dlp format string
        self.is_downloading = False
        self.is_paused = False
        self.is_cancelled = False

        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        # ---- Top bar: URL + Analyze ----
        self.top_frame = ctk.CTkFrame(self, corner_radius=12)
        self.top_frame.pack(fill="x", padx=16, pady=(16, 8))

        self.url_entry = ctk.CTkEntry(
            self.top_frame,
            placeholder_text="Paste video or playlist URL here...",
            height=40,
            corner_radius=8,
        )
        self.url_entry.pack(
            side="left", fill="x", expand=True, padx=(12, 8), pady=12
        )

        self.analyze_button = ctk.CTkButton(
            self.top_frame,
            text="Analyze Link",
            width=120,
            height=40,
            corner_radius=8,
            command=self.start_analyze,
        )
        self.analyze_button.pack(side="left", padx=(0, 12), pady=12)

        # ---- Middle section: thumbnail + title + quality (hidden) ----
        self.info_frame = ctk.CTkFrame(self, corner_radius=12)
        # not packed yet - shown after analysis

        self.thumb_label = ctk.CTkLabel(self.info_frame, text="")
        self.thumb_label.pack(pady=(16, 8))

        self.video_title_label = ctk.CTkLabel(
            self.info_frame,
            text="",
            font=ctk.CTkFont(size=15, weight="bold"),
            wraplength=600,
        )
        self.video_title_label.pack(pady=(0, 10), padx=16)

        self.quality_row = ctk.CTkFrame(self.info_frame, fg_color="transparent")
        self.quality_row.pack(pady=(0, 16))

        ctk.CTkLabel(self.quality_row, text="Quality:").pack(
            side="left", padx=(0, 8)
        )
        self.quality_menu = ctk.CTkOptionMenu(
            self.quality_row,
            values=["-"],
            width=220,
            corner_radius=8,
            command=self.on_quality_selected,
        )
        self.quality_menu.pack(side="left")

        # ---- Destination row ----
        self.dest_frame = ctk.CTkFrame(self, corner_radius=12)
        self.dest_frame.pack(fill="x", padx=16, pady=8)

        self.dest_entry = ctk.CTkEntry(self.dest_frame, height=34, corner_radius=8)
        self.dest_entry.insert(0, self.download_dir)
        self.dest_entry.configure(state="disabled")
        self.dest_entry.pack(
            side="left", fill="x", expand=True, padx=(12, 8), pady=10
        )

        self.browse_button = ctk.CTkButton(
            self.dest_frame,
            text="Browse",
            width=100,
            corner_radius=8,
            command=self.browse_folder,
        )
        self.browse_button.pack(side="left", padx=(0, 12), pady=10)

        # ---- Action area: Download ----
        self.download_button = ctk.CTkButton(
            self,
            text="Download",
            height=46,
            corner_radius=10,
            font=ctk.CTkFont(size=17, weight="bold"),
            state="disabled",
            command=self.start_download,
        )
        self.download_button.pack(fill="x", padx=16, pady=8)

        # ---- Bottom area: progress + status + controls ----
        self.bottom_frame = ctk.CTkFrame(self, corner_radius=12)
        self.bottom_frame.pack(fill="x", padx=16, pady=(8, 16))

        self.progress_bar = ctk.CTkProgressBar(self.bottom_frame, corner_radius=8)
        self.progress_bar.set(0)
        self.progress_bar.pack(fill="x", padx=16, pady=(16, 8))

        # Control buttons directly below the progress bar
        self.controls_row = ctk.CTkFrame(self.bottom_frame, fg_color="transparent")
        self.controls_row.pack(pady=(0, 8))

        self.pause_button = ctk.CTkButton(
            self.controls_row,
            text="Pause",
            width=130,
            height=36,
            corner_radius=8,
            state="disabled",
            command=self.toggle_pause,
        )
        self.pause_button.pack(side="left", padx=8)

        self.cancel_button = ctk.CTkButton(
            self.controls_row,
            text="Cancel",
            width=130,
            height=36,
            corner_radius=8,
            fg_color="#8b2e2e",
            hover_color="#a83c3c",
            state="disabled",
            command=self.cancel_download,
        )
        self.cancel_button.pack(side="left", padx=8)

        self.status_label = ctk.CTkLabel(
            self.bottom_frame,
            text="Ready. Paste a URL and click Analyze Link.",
            font=ctk.CTkFont(size=13),
            wraplength=600,
        )
        self.status_label.pack(pady=(0, 16))

    # ------------------------------------------------------------------
    # Thread-safe helpers
    # ------------------------------------------------------------------
    def ui(self, func, *args, **kwargs):
        """Run a GUI mutation on the main thread."""
        self.after(0, lambda: func(*args, **kwargs))

    def set_status(self, text: str, error: bool = False):
        color = "#ff5555" if error else "#a0a0a0"
        self.status_label.configure(text=text, text_color=color)

    # ------------------------------------------------------------------
    # Analyze link
    # ------------------------------------------------------------------
    def start_analyze(self):
        url = self.url_entry.get().strip()
        if not url:
            self.set_status("Please enter a URL first.", error=True)
            return

        self.analyze_button.configure(state="disabled", text="Analyzing...")
        self.download_button.configure(state="disabled")
        self.set_status("Fetching video information...")

        threading.Thread(
            target=self.analyze_worker, args=(url,), daemon=True
        ).start()

    def analyze_worker(self, url: str):
        try:
            ydl_opts = {
                "skip_download": True,
                "quiet": True,
                "no_warnings": True,
                "noplaylist": False,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)

            # Playlist: use the first entry for preview, keep playlist title
            display_info = info
            if info.get("_type") == "playlist" and info.get("entries"):
                entries = [e for e in info["entries"] if e]
                display_info = entries[0] if entries else info

            self.video_info = info

            title = info.get("title") or display_info.get("title", "Unknown title")
            if info.get("_type") == "playlist":
                count = len(info.get("entries") or [])
                title = f"[Playlist - {count} videos] {title}"

            # Thumbnail
            thumb_image = None
            thumb_url = display_info.get("thumbnail")
            if thumb_url:
                try:
                    resp = requests.get(thumb_url, timeout=10)
                    resp.raise_for_status()
                    pil_img = Image.open(io.BytesIO(resp.content))
                    pil_img.thumbnail((360, 220))
                    thumb_image = ctk.CTkImage(
                        light_image=pil_img,
                        dark_image=pil_img,
                        size=pil_img.size,
                    )
                except Exception:
                    thumb_image = None  # thumbnail is optional

            qualities = self.build_quality_options(display_info)

            self.ui(self.show_analysis_result, title, thumb_image, qualities)

        except yt_dlp.utils.DownloadError as e:
            msg = str(e).replace("ERROR:", "").strip()
            self.ui(self.analysis_failed, f"Analyze failed: {msg}")
        except Exception as e:
            self.ui(self.analysis_failed, f"Analyze failed: {e}")

    def build_quality_options(self, info: dict) -> dict:
        """Map dropdown labels to yt-dlp format strings."""
        heights = set()
        for f in info.get("formats") or []:
            h = f.get("height")
            if h and f.get("vcodec") not in (None, "none"):
                heights.add(h)

        options = {"Best Video": "bestvideo+bestaudio/best"}
        for h in sorted(heights, reverse=True):
            options[f"{h}p"] = (
                f"bestvideo[height<={h}]+bestaudio/best[height<={h}]"
            )
        options["Audio Only (MP3)"] = "bestaudio/best"
        return options

    def show_analysis_result(self, title, thumb_image, qualities):
        self.quality_map = qualities

        self.video_title_label.configure(text=title)
        if thumb_image is not None:
            self.thumb_label.configure(image=thumb_image, text="")
        else:
            self.thumb_label.configure(image=None, text="(no thumbnail)")

        labels = list(qualities.keys())
        self.quality_menu.configure(values=labels)
        self.quality_menu.set(labels[0])

        self.info_frame.pack(fill="x", padx=16, pady=8, after=self.top_frame)

        self.analyze_button.configure(state="normal", text="Analyze Link")
        self.download_button.configure(state="normal")
        self.set_status("Ready to download. Select a quality and click Download.")

    def analysis_failed(self, message: str):
        self.analyze_button.configure(state="normal", text="Analyze Link")
        self.set_status(message, error=True)

    def on_quality_selected(self, _choice: str):
        if self.video_info and not self.is_downloading:
            self.download_button.configure(state="normal")

    # ------------------------------------------------------------------
    # Folder browse
    # ------------------------------------------------------------------
    def browse_folder(self):
        folder = filedialog.askdirectory(initialdir=self.download_dir)
        if folder:
            self.download_dir = folder
            self.dest_entry.configure(state="normal")
            self.dest_entry.delete(0, "end")
            self.dest_entry.insert(0, folder)
            self.dest_entry.configure(state="disabled")

    # ------------------------------------------------------------------
    # Download / pause / cancel
    # ------------------------------------------------------------------
    def start_download(self):
        if self.is_downloading or not self.video_info:
            return

        url = self.url_entry.get().strip()
        self.is_downloading = True
        self.is_paused = False
        self.is_cancelled = False

        self.download_button.configure(state="disabled", text="Downloading...")
        self.analyze_button.configure(state="disabled")
        self.pause_button.configure(state="normal", text="Pause")
        self.cancel_button.configure(state="normal")
        self.progress_bar.set(0)
        self.set_status("Starting download...")

        threading.Thread(
            target=self.download_worker, args=(url,), daemon=True
        ).start()

    def toggle_pause(self):
        if not self.is_downloading:
            return
        self.is_paused = not self.is_paused
        if self.is_paused:
            self.pause_button.configure(text="Resume")
            self.set_status("Paused. Click Resume to continue.")
        else:
            self.pause_button.configure(text="Pause")
            self.set_status("Resuming download...")

    def cancel_download(self):
        if not self.is_downloading:
            return
        self.is_cancelled = True
        self.is_paused = False  # release the pause loop so hook can see cancel
        self.set_status("Cancelling...")

    def progress_hook(self, d: dict):
        """Runs inside the download thread. Handles pause + cancel."""
        # Cancel check FIRST, then pause loop (cancel must break out of pause)
        if self.is_cancelled:
            raise DownloadCancelled("Download cancelled by user")

        while self.is_paused:
            time.sleep(0.5)
            if self.is_cancelled:
                raise DownloadCancelled("Download cancelled by user")

        status = d.get("status")
        if status == "downloading":
            downloaded = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            fraction = (downloaded / total) if total else 0.0

            speed = d.get("speed")
            speed_str = f" | {speed / 1024 / 1024:.2f} MB/s" if speed else ""
            eta = d.get("eta")
            eta_str = f" | ETA: {eta}s" if eta else ""

            info = d.get("info_dict") or {}
            idx = info.get("playlist_index")
            count = info.get("n_entries")
            playlist_str = f" [{idx}/{count}]" if idx and count else ""

            self.ui(self.progress_bar.set, min(1.0, fraction))
            self.ui(
                self.set_status,
                f"Downloading{playlist_str}: "
                f"{fraction * 100:.1f}%{speed_str}{eta_str}",
            )
        elif status == "finished":
            self.ui(self.progress_bar.set, 1.0)
            self.ui(self.set_status, "Processing with ffmpeg...")

    def build_ydl_opts(self) -> dict:
        label = self.quality_menu.get()
        fmt = self.quality_map.get(label, "bestvideo+bestaudio/best")
        is_playlist = (self.video_info or {}).get("_type") == "playlist"

        outtmpl = os.path.join(self.download_dir, "%(title)s.%(ext)s")
        if is_playlist:
            outtmpl = os.path.join(
                self.download_dir,
                "%(playlist_title)s",
                "%(playlist_index)s - %(title)s.%(ext)s",
            )

        ydl_opts = {
            "format": fmt,
            "outtmpl": outtmpl,
            "ffmpeg_location": self.base_dir,
            "progress_hooks": [self.progress_hook],
            "quiet": True,
            "no_warnings": True,
            "noplaylist": not is_playlist,
        }

        if label.startswith("Audio Only"):
            ydl_opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ]
        else:
            ydl_opts["merge_output_format"] = "mp4"

        return ydl_opts

    def download_worker(self, url: str):
        try:
            with yt_dlp.YoutubeDL(self.build_ydl_opts()) as ydl:
                ydl.download([url])
            self.ui(self.download_finished, True, "✅ Download complete!")
        except DownloadCancelled:
            self.ui(self.download_finished, False, "Download cancelled by user.")
        except yt_dlp.utils.DownloadError as e:
            # yt-dlp wraps hook exceptions; detect our cancel inside it
            if "cancelled by user" in str(e).lower():
                self.ui(self.download_finished, False, "Download cancelled by user.")
            else:
                msg = str(e).replace("ERROR:", "").strip()
                self.ui(self.download_finished, False, f"Download error: {msg}")
        except Exception as e:
            self.ui(self.download_finished, False, f"Unexpected error: {e}")

    def download_finished(self, success: bool, message: str):
        self.is_downloading = False
        self.is_paused = False
        self.is_cancelled = False

        self.download_button.configure(state="normal", text="Download")
        self.analyze_button.configure(state="normal")
        self.pause_button.configure(state="disabled", text="Pause")
        self.cancel_button.configure(state="disabled")

        if success:
            self.progress_bar.set(1.0)
        else:
            self.progress_bar.set(0)
        self.set_status(message, error=not success)


def main():
    app = DownloaderApp()
    if not os.path.exists(os.path.join(get_base_dir(), "ffmpeg.exe")):
        app.set_status(
            "Warning: ffmpeg.exe not found next to script. "
            "Audio extraction and video merging will fail.",
            error=True,
        )
    app.mainloop()


if __name__ == "__main__":
    main()
