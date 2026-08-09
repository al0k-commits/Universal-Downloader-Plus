; ---------------------------------------------------------------------------
;  Inno Setup 6 script for Universal Downloader+
;
;  Compiled by .github/workflows/release.yml roughly like:
;
;      iscc.exe /Qp ^
;        /DMyAppVersion=1.0.0 ^
;        /DMyAppFullVersion=v1.0.0 ^
;        /DSourceDir=dist ^
;        /DAppArch=x64 ^
;        /O"artifacts" ^
;        /F"UniversalDownloaderPlus-v1.0.0-windows-x64-Setup" ^
;        installer.iss
;
;  Requires Inno Setup 6.3 or newer: "x64compatible" and native arm64 support
;  were introduced in 6.3. The workflow installs it if the runner lacks it.
; ---------------------------------------------------------------------------

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif

; Human-facing version (may carry a -rc1 / -dev.abc1234 suffix).
#ifndef MyAppFullVersion
  #define MyAppFullVersion MyAppVersion
#endif

; Root of the PyInstaller output that is being packaged.
#ifndef SourceDir
  #define SourceDir "dist"
#endif

; "x64" or "arm64" - decides both the allowed architectures and the AppId.
#ifndef AppArch
  #define AppArch "x64"
#endif

#define MyAppName        "Universal Downloader+"
#define MyAppShortName   "UniversalDownloaderPlus"
#define MyAppExeName     "UniversalDownloaderPlus.exe"
#define MyAppPublisher   "Alok"
#define MyAppURL         "https://github.com/al0k-commits/Universal-Downloader-Plus"
#define MyAppSupportURL  "https://github.com/al0k-commits/Universal-Downloader-Plus/issues"

; AppId is per-architecture, mirroring the per-arch UpgradeCode in
; installer.wxs: an x64 install and an arm64 install are separate products,
; never a broken in-place upgrade of one another.
#if AppArch == "arm64"
  #define MyAppId "{8D1F4C6A-3E55-4B21-9A0F-5C7B1E9D4A22}"
#else
  #define MyAppId "{2B7A9E14-6D38-4F0C-8E63-91A4C5D7B033}"
#endif

[Setup]
AppId={{#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppFullVersion}
VersionInfoVersion={#MyAppVersion}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoProductName={#MyAppName}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Setup
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppSupportURL}
AppUpdatesURL={#MyAppURL}/releases

DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=no
AllowNoIcons=yes
LicenseFile=LICENSE

; Per-machine install into Program Files, matching the MSI.
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog

#if AppArch == "arm64"
ArchitecturesAllowed=arm64
ArchitecturesInstallIn64BitMode=arm64
#else
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
#endif

; /O and /F on the ISCC command line override these; the defaults only exist
; so the script still compiles when opened by hand in the Inno Setup IDE.
OutputDir=artifacts
OutputBaseFilename={#MyAppShortName}-{#MyAppFullVersion}-windows-{#AppArch}-Setup

Compression=lzma2/ultra64
SolidCompression=yes
LZMANumBlockThreads=2
WizardStyle=modern
SetupIconFile=src\universal_downloader\resources\icon.ico
UninstallDisplayName={#MyAppName} {#MyAppFullVersion}
UninstallDisplayIcon={app}\{#MyAppExeName}

; Offer to close a running instance instead of failing on a locked file.
CloseApplications=yes
CloseApplicationsFilter=*.exe
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The application itself.
Source: "{#SourceDir}\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

; FFMPEG_DIR resolves to <exe dir>\ffmpeg when the app is frozen, so the
; helper binaries must land in a sibling folder - exactly like the portable
; build and the MSI layout.
Source: "{#SourceDir}\ffmpeg\*"; DestDir: "{app}\ffmpeg"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Comment: "Download video and audio from the web"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; PyInstaller/Qt scratch files that may appear inside the install folder.
Type: filesandordirs; Name: "{app}\ffmpeg"
Type: dirifempty; Name: "{app}"
