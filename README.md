<div align="center">

# Universal Downloader+ 🚀

**Monorepo for a cross-platform video & playlist downloader: a PyQt6 desktop app and an Expo/React Native mobile app, backed by yt-dlp + FFmpeg.**

</div>

## 📦 Repo layout

```text
Universal-Downloader-Plus/
├── .github/workflows/
│   ├── desktop-release.yml      # desktop-v* tags → .exe / .msi / .dmg / .deb
│   └── android-release.yml      # android-v* tags → .apk / .aab
├── desktop/                     # PyQt6 desktop app (PyInstaller packaging)
│   ├── src/universal_downloader/
│   ├── ffmpeg/                  # local binaries (git-ignored except README.md)
│   ├── installer.iss            # Inno Setup  (.exe)
│   ├── installer.wxs            # WiX v3      (.msi)
│   ├── pyproject.toml
│   └── requirements.txt
├── android/                     # Expo / React Native mobile app
│   ├── App.js
│   ├── app.json
│   ├── package.json
│   ├── eas.json
│   └── server/                  # FastAPI + yt-dlp backend for the app
│       ├── server.py
│       └── requirements.txt
└── .gitignore
```

## 🖥️ Desktop app — `desktop/`

PyQt6 + Qt WebEngine + yt-dlp downloader with an embedded Chromium browser,
persistent logins, playlist queueing, pause/resume/cancel, and ID3 tagging.

- Docs & local setup: **[`desktop/README.md`](desktop/README.md)**
- Builds: tag `desktop-v*` → Windows `.exe` (Inno + WiX + portable), macOS `.dmg`, Linux `.deb`
- Bundled: `ffmpeg`, `ffprobe`, **`ffplay`** (Windows/macOS/Linux), no separate install required

## 📱 Mobile app — `android/`

Expo / React Native app that talks to the bundled FastAPI server
(`android/server/server.py`) for video extraction and downloads.

- App: `cd android && npm install && npx expo start`
- Server: `cd android/server && pip install -r requirements.txt && uvicorn server:app --host 0.0.0.0 --port 8000`
- Set your server address in `android/app.json` → `extra.serverBaseUrl`
- Builds: tag `android-v*` → `.apk` (direct install) + `.aab` (Play Store). Requires `EXPO_TOKEN` for
  EAS local builds; without it the workflow falls back to a plain Gradle build.

## 🚀 Releasing

| Product | Tag | Artifacts |
|---|---|---|
| Desktop | `desktop-v1.0.0` | Setup.exe · msi · dmg · deb (+ sha256) |
| Android | `android-v1.0.0` | app-release.apk · app-release.aab |

## 📄 License

MIT — see [`LICENSE`](desktop/LICENSE).
