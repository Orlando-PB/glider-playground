; Inno Setup script for the Glider Playground Windows desktop app.
;
; Wraps the PyInstaller one-directory bundle (dist\GliderPlayground) into a
; single Setup.exe. Compiled in CI with:
;
;   iscc /DAppVersion=%GP_VERSION% packaging\installer.iss
;
; Why an installer instead of a zip: files extracted from a downloaded zip all
; carry Windows' "Mark of the Web", so SmartScreen blocks not just the .exe but
; the DLLs/data beside it — clicking "Run anyway" on the exe isn't enough.
; Files written by an installer don't get that mark, so the user only has to
; clear SmartScreen once (on Setup.exe itself) and the app then runs cleanly.
; It also gives us Start-menu/desktop shortcuts and a proper uninstaller.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName "Glider Playground"
#define AppExeName "GliderPlayground.exe"
#define AppPublisher "National Oceanography Centre"
#define AppId "{B5B0C7E2-3A4D-4E9F-9C21-9D7E1A6F0C42}"

[Setup]
AppId={{#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL=https://github.com/Orlando-PB/glider-playground
DefaultDirName={autopf}\Glider Playground
DefaultGroupName=Glider Playground
DisableProgramGroupPage=yes
; Install per-user so an unsigned build doesn't trigger a UAC admin prompt on
; top of the SmartScreen warning. {autopf} resolves to %LocalAppData%\Programs
; under this mode.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=installer
OutputBaseFilename=GliderPlayground-Windows-x64-Setup
SetupIconFile=app_icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; Recursively bundle the entire PyInstaller one-dir output.
Source: "..\dist\GliderPlayground\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
{ A stable AppId means re-running Setup.exe already upgrades in place. But the
  PyInstaller bundle's set of DLLs/data files changes between versions, and
  Inno only overwrites files present in the *new* build — orphans from the old
  one would linger. So before installing we silently run the previous version's
  uninstaller, guaranteeing a clean replace. The uninstall registry key lives
  under HKCU for a per-user install and HKLM for a per-machine one; check both. }

function GetUninstallString(): String;
var
  Key, S: String;
begin
  Key := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#AppId}_is1';
  S := '';
  if not RegQueryStringValue(HKCU, Key, 'UninstallString', S) then
    RegQueryStringValue(HKLM, Key, 'UninstallString', S);
  Result := RemoveQuotes(S);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  UnInstaller: String;
  ResultCode: Integer;
begin
  if CurStep = ssInstall then
  begin
    UnInstaller := GetUninstallString();
    if UnInstaller <> '' then
    begin
      { /VERYSILENT /NORESTART = no UI; the extra flags suppress the
        "are you sure" and final "removed" message boxes. }
      Exec(UnInstaller,
        '/VERYSILENT /NORESTART /SUPPRESSMSGBOXES',
        '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    end;
  end;
end;
