"""
Universal Downloader+ mobile backend.

FastAPI + yt-dlp extraction/worker used by the Expo app. Runs on the same
LAN as the phone (or on a VPS):

    pip install -r requirements.txt
    uvicorn server:app --host 0.0.0.0 --port 8000

Endpoints:
    GET  /health                      -> {"status": "ok"}
    GET  /info?url=...                -> title + streamable formats
    POST /download {url, format_id}   -> queues a yt-dlp download
    GET  /files/<name>                -> serves finished downloads

Intentionally stateless: downloads land in ./downloads and are served from
there. No auth - do NOT expose this to the public internet as-is.
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

APP = FastAPI(title="Universal Downloader+ server", version="1.0.0")

APP.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR = Path(__file__).resolve().parent / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

FFMPEG_DIR = Path(__file__).resolve().parent / "ffmpeg"
FFMPEG_DIR.mkdir(exist_ok=True)

_JOBS: dict[str, dict] = {}


def _sanitize(info: dict) -> dict:
    """Reduce the huge yt-dlp extractor dict to what the app renders."""
    formats = []
    for f in info.get("formats", []):
        format_id = f.get("format_id")
        if not format_id:
            continue
        formats.append(
            {
                "format_id": format_id,
                "ext": f.get("ext"),
                "resolution": f.get("resolution") or None,
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "vcodec": f.get("vcodec"),
                "acodec": f.get("acodec"),
            }
        )
    return {
        "title": info.get("title") or info.get("id"),
        "uploader": info.get("uploader"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "formats": formats,
    }


class DownloadRequest(BaseModel):
    url: str
    format_id: str | None = None


@APP.get("/health")
def health() -> dict:
    return {"status": "ok"}


@APP.get("/info")
def info(url: str) -> dict:
    if not url:
        raise HTTPException(400, "missing url")
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ffmpeg_location": str(FFMPEG_DIR),
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return _sanitize(ydl.extract_info(url, download=False))
    except Exception as exc:  # yt-dlp raises a rich exception tree
        raise HTTPException(422, str(exc)) from exc


@APP.post("/download")
def download(req: DownloadRequest) -> dict:
    job_id = uuid.uuid4().hex[:8]
    out_dir = DOWNLOAD_DIR / job_id
    out_dir.mkdir()

    ydl_opts = {
        "format": req.format_id or "bestvideo*+bestaudio/best",
        "outtmpl": str(out_dir / "%(title)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": str(FFMPEG_DIR),
        "merge_output_format": "mp4",
        "postprocessors": [
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ],
    }

    def _run() -> None:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([req.url])
            files = [p.name for p in out_dir.iterdir() if p.is_file()]
            _JOBS[job_id] = {"status": "done", "files": files}
        except Exception as exc:
            _JOBS[job_id] = {"status": "error", "error": str(exc)}

    threading.Thread(target=_run, daemon=True).start()
    _JOBS[job_id] = {"status": "running", "files": []}
    return {"job_id": job_id, "status": "running"}


@APP.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return job


APP.mount("/files", StaticFiles(directory=DOWNLOAD_DIR), name="files")
