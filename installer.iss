; ============================================================
;  Inno Setup script for Mohini Print Worker
;  Builds MohiniPrintSetup.exe which installs the app and
;  creates a desktop shortcut automatically.
;
;  PREREQ: build the exe first (build_exe.bat) so
;          dist\MohiniPrintWorker.exe exists.
;  THEN:   install Inno Setup (https://jrsoftware.org/isdl.php),
;          open this .iss in it, and click Build > Compile.
;          Output: Output\MohiniPrintSetup.exe
; ============================================================

#define AppName "Mohini Print Worker"
#define AppVersion "1.0.0"
#define Publisher "GOBT"
#define ExeName "MohiniPrintWorker.exe"

[Setup]
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#Publisher}
; Install into Program Files\GOBT\Mohini Print Worker
DefaultDirName={autopf}\{#Publisher}\{#AppName}
DefaultGroupName={#Publisher}
DisableProgramGroupPage=yes
OutputBaseFilename=MohiniPrintSetup
Compression=lzma2
SolidCompression=yes
; Per-user install so no admin prompt is needed to install
PrivilegesRequired=lowest
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: checkedonce
Name: "startupicon"; Description: "Start automatically when Windows starts"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
; Grab the PyInstaller-built exe
Source: "dist\{#ExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; Start menu shortcut
Name: "{group}\{#AppName}"; Filename: "{app}\{#ExeName}"
; Desktop shortcut (created if the user ticks the task, ticked by default)
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#ExeName}"; Tasks: desktopicon
; Optional: launch on Windows startup
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#ExeName}"; Tasks: startupicon

[Run]
; Offer to launch the app right after install
Filename: "{app}\{#ExeName}"; Description: "Launch {#AppName} now"; Flags: nowait postinstall skipifsilent
