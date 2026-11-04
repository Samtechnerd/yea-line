; Yea-Line installer (Inno Setup 6). Per-user install: no administrator rights needed.
; Build the app first (packaging\build.ps1 does both steps).
#define AppName "Yea-Line"
#define AppVersion "1.0.0"
#define AppExe "Yea-Line.exe"

[Setup]
AppId={{7B3C9E52-4A1D-4F6B-9C25-5E8A1D2F6B07}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Yea-Line
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\installer-out
OutputBaseFilename=Yea-Line-Setup
SetupIconFile=..\app\yealine.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start {#AppName} when I sign in to Windows"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "..\dist\Yea-Line\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: startup

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName} now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "taskkill.exe"; Parameters: "/F /IM {#AppExe}"; Flags: runhidden; RunOnceId: "StopYeaLine"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  // Only ever offered when a person is uninstalling interactively; a silent uninstall keeps all data.
  if (CurUninstallStep = usPostUninstall) and (not UninstallSilent()) then
    if MsgBox('Also delete your Yea-Line settings and call history?' + #13#10 +
              '(Your saved SIP and Home Assistant passwords stay in Windows Credential Manager.)',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(ExpandConstant('{userappdata}\YeaLine'), True, True, True);
end;
