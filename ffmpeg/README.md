# ffmpeg / yt-dlp binaries

This folder holds the third-party executables the app needs at runtime.

Place these files here (they are git-ignored and must NOT be committed):

- `ffmpeg.exe`     — required for muxing video+audio and format conversion
- `ffprobe.exe`    — required by yt-dlp for stream probing/merging
- `yt-dlp.exe`     — optional standalone binary (the app normally uses the
                     `yt-dlp` Python package, but keeping the exe here keeps
                     everything in one place)

## Where to get them

- ffmpeg / ffprobe: https://www.gyan.dev/ffmpeg/builds/ (or https://ffmpeg.org)
- yt-dlp:          https://github.com/yt-dlp/yt-dlp#release-files

On a frozen (PyInstaller) build, the same three files should sit next to the
generated executable instead.
