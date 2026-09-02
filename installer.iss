; Sand 资格领取器 安装包脚本（Inno Setup 6）
; 版本与源 exe 名由 build.bat 通过 /DAppVer /DExeName 传入；单独跑 ISCC 时用下面默认值。
#ifndef AppVer
#define AppVer "1.1.8"
#endif
#ifndef ExeName
#define ExeName "SandClaimer-" + AppVer + ".exe"
#endif

[Setup]
AppName=Sand 资格领取器
AppVersion={#AppVer}
AppPublisher=SandClaimer
DefaultDirName={autopf}\SandClaimer
DefaultGroupName=Sand 资格领取器
DisableProgramGroupPage=yes
OutputDir=installer
OutputBaseFilename=SandClaimer-Setup-{#AppVer}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\SandClaimer.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin

[Languages]
Name: "cn"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; 分发的 onefile 名带版本号，但装到本机统一叫 SandClaimer.exe，快捷方式跨版本不失效。
Source: "nuitka-out\{#ExeName}"; DestDir: "{app}"; DestName: "SandClaimer.exe"; Flags: ignoreversion

[Icons]
Name: "{group}\Sand 资格领取器"; Filename: "{app}\SandClaimer.exe"
Name: "{group}\{cm:UninstallProgram,Sand 资格领取器}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Sand 资格领取器"; Filename: "{app}\SandClaimer.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\SandClaimer.exe"; Description: "{cm:LaunchProgram,Sand 资格领取器}"; Flags: nowait postinstall skipifsilent
