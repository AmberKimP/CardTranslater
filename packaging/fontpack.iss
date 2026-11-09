; CardTranslater 글꼴 설치 파일 (Inno Setup 6) — 프로그램 설치 파일과 따로
;   .venv/Scripts/python packaging/make_font_pack.py  -> dist/CardTranslater-Fonts-<묶음 버전>.exe
; 프로그램에 들어 있는 기본 글꼴(본고딕)을 뺀 한글 글꼴을 %LOCALAPPDATA%\CardTranslater\fontpack\fonts 에 설치한다.
; 앱(app/font_files.py)이 그 폴더를 찾아 쓴다. 프로그램을 업데이트·제거해도 남고, 따로 제거할 수 있다.
; Windows 글꼴 폴더에는 설치하지 않는다 (이 앱만 쓴다).

#ifndef PackVersion
  #define PackVersion "1"
#endif

[Setup]
AppId={{B3E1F0C4-8D2A-4F67-9A15-2C7E9D4B8F21}
AppName=CardTranslater 글꼴
AppVersion={#PackVersion}
AppVerName=CardTranslater 글꼴 묶음 {#PackVersion}
AppPublisher=CardTranslater
DefaultDirName={localappdata}\CardTranslater\fontpack
DisableDirPage=yes
DisableProgramGroupPage=yes
CreateAppDir=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=CardTranslater-Fonts-{#PackVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
Uninstallable=yes
UninstallDisplayName=CardTranslater 글꼴 묶음

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[InstallDelete]
; 예전 묶음에만 있던 글꼴이 남지 않게 (같은 폴더를 새 묶음으로 바꾼다)
Type: filesandordirs; Name: "{app}\fonts"

[Files]
Source: "..\build\fontpack_stage\*"; DestDir: "{app}\fonts"; Flags: ignoreversion recursesubdirs createallsubdirs

[Messages]
FinishedLabel=글꼴을 설치했습니다. CardTranslater를 켜 두었다면 다시 켜면 새 글꼴을 쓸 수 있습니다.
