; CardTranslater 설치 파일 (Inno Setup 6)
;   1) .venv/Scripts/python packaging/build.py        -> dist/CardTranslater/
;   2) .venv/Scripts/python packaging/make_installer.py  -> dist/CardTranslater-Setup-<버전>-<날짜>.exe
; 관리자 권한 없이 사용자 폴더(%LOCALAPPDATA%\Programs)에 설치한다. 데이터(프로젝트·게임)는 문서\CardTranslater에 따로
; 있으므로 지우기(제거)해도 남는다.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef SetupName
  #define SetupName AppVersion
#endif

[Setup]
AppId={{6B7C2E1A-4F3D-4C8B-9E21-CA4D7A1B5E30}
AppName=CardTranslater
AppVersion={#AppVersion}
AppPublisher=CardTranslater
DefaultDirName={localappdata}\Programs\CardTranslater
DefaultGroupName=CardTranslater
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=CardTranslater-Setup-{#SetupName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\CardTranslater.exe
CloseApplications=yes

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면에 바로가기 만들기"; GroupDescription: "바로가기:"

[Files]
Source: "..\dist\CardTranslater\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\CardTranslater"; Filename: "{app}\CardTranslater.exe"
Name: "{group}\CardTranslater 제거"; Filename: "{uninstallexe}"
Name: "{autodesktop}\CardTranslater"; Filename: "{app}\CardTranslater.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\CardTranslater.exe"; Description: "CardTranslater 실행"; Flags: nowait postinstall skipifsilent
