; Inno Setup script. Built by packaging\build.py, which passes /DAppVersion=x.y.z
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{8F3C2A51-6B7E-4C1D-9A2F-3E5B7D9C1A40}
AppName=Converter
AppVersion={#AppVersion}
AppVerName=Converter {#AppVersion}
AppPublisher=sttsspjy
AppPublisherURL=https://github.com/sttsspjy/converter
AppSupportURL=https://github.com/sttsspjy/converter/issues
AppUpdatesURL=https://github.com/sttsspjy/converter/releases
; Per-user install: no admin prompt, and updates install over the top.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\Converter
DisableProgramGroupPage=yes
DisableDirPage=yes
OutputDir=..\dist
OutputBaseFilename=Converter-Setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\Converter.exe
UninstallDisplayName=Converter
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
AppMutex=ConverterAppMutex
CloseApplications=yes

[Tasks]
Name: "sendto"; Description: "Add Converter to the right-click ""Send to"" menu"
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[InstallDelete]
; Drop the previous version's libraries before copying the new ones.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\Converter\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Converter"; Filename: "{app}\Converter.exe"
Name: "{usersendto}\Converter"; Filename: "{app}\Converter.exe"; Tasks: sendto
Name: "{autodesktop}\Converter"; Filename: "{app}\Converter.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Converter.exe"; Description: "Open Converter"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Downloaded ffmpeg/opusenc, logs and the WebView cache. Settings in %APPDATA% are kept.
Type: filesandordirs; Name: "{localappdata}\Converter"

[Code]
function WebView2Installed(): Boolean;
var
  v: String;
begin
  Result :=
    RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', v) or
    RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', v) or
    RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', v);
  if Result then Result := (v <> '') and (v <> '0.0.0.0');
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if not WebView2Installed() then
    MsgBox('Converter needs the Microsoft Edge WebView2 Runtime, which this PC doesn''t seem to have.' + #13#10 + #13#10 +
           'It comes with Windows 11. On Windows 10, get it from' + #13#10 +
           'https://developer.microsoft.com/microsoft-edge/webview2/' + #13#10 + #13#10 +
           'Setup will continue; install the runtime before opening Converter.', mbInformation, MB_OK);
end;
