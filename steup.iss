[Setup]
AppName=Acoustic Trigger
AppVersion=1.1
AppPublisher=Rocket49
DefaultDirName={localappdata}\Acoustic Trigger
DefaultGroupName=Acoustic Trigger
PrivilegesRequired=lowest
OutputDir=installer_output
OutputBaseFilename=AcousticTrigger_Setup_v4.0
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
Source: "dist\AcousticTrigger\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "trigger_backend.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "tap_model.pkl"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Acoustic Trigger"; Filename: "{app}\AcousticTrigger.exe"
Name: "{group}\Uninstall Acoustic Trigger"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Acoustic Trigger"; Filename: "{app}\AcousticTrigger.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\AcousticTrigger.exe"; Description: "Launch Acoustic Trigger now"; Flags: nowait postinstall skipifsilent