# DESIGN.md — CardTranslater 화면 디자인 (Apple HIG 참고, Windows 데스크톱용)

이 프로그램은 **Windows 데스크톱 앱(PySide6/Qt 위젯)** 이고, 표·카드 그림·편집 칸을 한 창에 빽빽하게 보는 작업 도구다.
Apple HIG 기반 가이드(모바일·웹용)를 검토해 **그대로 쓰는 것 / 데스크톱에 맞게 바꿔 쓰는 것 / 쓰지 않는 것**으로 나눴다.
값은 모두 `app/qt/theme.py`(토큰)와 `app/qt/dialogs.py`(공용 위젯)에 있다. 새 화면은 여기 값을 쓰고, 색을 코드에 직접 적지 않는다.

## 1. 원칙
- **Clarity (명확성)** — 그대로. 글은 어느 크기에서도 읽혀야 하고, 장식보다 내용(카드 그림·원문·번역)이 먼저다.
- **Deference (존중)** — 그대로. 화면 틀(바탕·테두리·버튼)은 옅게, 카드 그림과 글이 눈에 띄게.
- **Depth (깊이감)** — 바꿔서. 반투명 블러·움직임 대신 **바탕(회색) ↔ 내용 칸(흰색, 둥근 모서리, 옅은 테두리)** 두 층으로 나눈다.

## 2. 색 (라이트 모드)
| 토큰 (`theme.py`) | 값 | 가이드 | 쓰는 곳 |
|---|---|---|---|
| `BG` | `#F2F2F7` | Secondary Background | 창 바탕 |
| `CARD` | `#FFFFFF` | Primary/Tertiary Background | 표·입력 칸·카드·묶음 상자 |
| `TEXT` | `#1D1D1F` | Primary Label (≈ 검정 85%) | 제목·본문 |
| `TEXT_SUB` | `#6E6E73` | Secondary Label | 보조 설명·머리글 |
| `DISABLED` | `#AEAEB2` | Tertiary Label | 비활성·자리 표시 글 |
| `LINE` / `LINE_SOFT` | `#D1D1D6` / `#E5E5EA` | Separator | 입력 칸 / 표·카드 테두리 |
| `ACCENT` | `#0071E3` | System Blue **(바꿈)** | 주 버튼·선택·링크 |
| `ACCENT_SOFT` | `#E5F0FD` | Blue 12% tint | 고른 줄·보조(tinted) 강조 |
| `DANGER` | `#D70015` | System Red **(바꿈)** | 지우기·되돌릴 수 없는 동작 |

- **System Blue `#007AFF` → `#0071E3`**: 흰 바탕의 작은 글·흰 글씨 버튼에서 `#007AFF`는 대비가 4.0:1이라 기준(4.5:1)에 못 미친다.
  `#0071E3`(Apple 웹의 버튼 파랑)은 약 4.7:1. 빨강도 같은 이유로 `#FF3B30` 대신 `#D70015`.
- **상태 색은 그대로 둔다**: 확인할 것 노랑 `#FFE066`·주황 `#F4B183`, 확인됨 연두 `#D9EAD3`, 사람이 고침 하늘 `#CFE2F3`.
  검수 엑셀과 같은 약속이라 바꾸면 화면과 엑셀이 달라진다.
- **다크 모드: 지금은 안 함.** 화면 곳곳의 상태 색·카드 그림 비교가 밝은 바탕을 전제로 하고, 다크 토큰을 쓰려면 화면 전체를
  다시 검사해야 한다. 토큰은 한 곳(`theme.py`)에 모았으므로 나중에 다크 값을 더할 수 있다.

## 3. 글자 (데스크톱 크기로 바꿈)
가이드의 SF Pro 크기(본문 17px)는 휴대폰 기준이라 표가 많은 이 창에는 너무 크다. 같은 비율로 줄여 쓴다.

| 스타일 | 가이드 | 이 앱 | 쓰는 곳 |
|---|---|---|---|
| Title | 22–34px Bold | 14–15pt Bold (`page_title`) | 창 맨 위 제목 |
| Headline | 17px Semibold | 11pt Bold (`section_title`) / 15px Semibold | 묶음 제목, 원문 확인의 칸 제목 |
| Body | 17px Regular | **10pt (≈13px)** (`BODY_PT`) | 본문·표·버튼 |
| Footnote | 13px | 기본 크기 회색 (`grey_note`, `subtitle`) | 안내문·단축키 |

- 글꼴: **Segoe UI + 맑은 고딕** (`FONT_FAMILIES`). Pretendard는 설치 파일에 UI 글꼴을 더 넣어야 해서 쓰지 않는다
  (카드에 넣는 한글 글꼴은 별도 글꼴 설치 파일 — 화면 글꼴과 섞지 않는다).
- 원문(일본어) 칸은 `source_font`(Yu Gothic UI → Meiryo)를 그대로 쓴다.
- 글자 사이 간격(letter-spacing)은 Qt 위젯에서 기본값 그대로 (작은 한글은 좁히면 읽기 어렵다).

## 4. 배치·간격·모서리
- **여백**: 창 가장자리 16–20px, 묶음 사이 8–12px, 칸 사이 4–8px (4의 배수).
- **누르는 곳 크기 (바꿈)**: 가이드의 44×44px는 손가락 기준. 마우스로 쓰는 데스크톱이므로 버튼 높이 약 32px
  (`min-height: 22px` + 위아래 여백), 표 안의 버튼·입력 칸은 줄 높이(40–48px)에 맞춘다.
- **모서리**: 버튼·입력 칸 8px (`RADIUS_CONTROL`), 카드·묶음 상자 12px (`RADIUS_CARD`), 표 8px, 말풍선 8px.
  가이드의 버튼 12px는 32px 높이 버튼에서 알약처럼 보여 8px로 줄였다.

## 5. 재질·그림자 — 쓰지 않음
- **반투명 블러(backdrop-filter)**: Qt 위젯 스타일시트에는 없다. 흉내 내려면 창마다 그림을 다시 그려야 해 느려진다.
- **떠 있는 그림자**: `QGraphicsDropShadowEffect`는 표·그림이 많은 창에서 다시 그리기가 느려진다. 대신 옅은 테두리
  (`LINE_SOFT`)와 흰 칸으로 층을 나눈다.

## 6. 구성 요소
- **버튼**
  - 주 버튼 (`make_primary`, `PRIMARY_QSS`): 파랑 채움 + 흰 굵은 글. 한 창에 하나, 오른쪽 끝(저장·확정·다음).
  - 보조 버튼: 흰 바탕 + 회색 테두리, 마우스를 올리면 파란 테두리.
  - 위험 버튼 (`make_danger`): 빨간 글.
  - 큰 다음 단계 버튼 (`NEXT_QSS`): 홈의 ‘다음 할 일’에만.
- **묶음 (Inset Grouped List에 해당)**: `QGroupBox`·`card()` — 회색 바탕 위 흰 카드, 12px 모서리.
- **안내 띠** (`hint_bar`): info(파랑)·todo(노랑)·done(연두) — 창 위쪽의 상태·할 일 안내.
- **창 (Modal/Sheet에 해당)**: 데스크톱 대화상자를 그대로 쓴다. 아래에서 올라오는 시트·손잡이(grabber)는 쓰지 않는다
  (Windows 사용자가 기대하는 모양이 아니고, 창을 옮기고 크기를 바꿀 수 있어야 한다).
- **큰 제목 접힘(Navigation Bar collapse)**: 쓰지 않음. 창 제목은 늘 같은 크기로 위에 둔다.
- **아이콘**: 지금은 글 버튼만 쓴다. 아이콘을 더한다면 Lucide(ISC)처럼 같은 굵기(1.5–2px)·둥근 끝의 선 아이콘으로,
  라이선스를 `CLAUDE.md` 규칙대로 확인한다. 콤보·숫자 칸 화살표·체크 표시는 이미 둥근 끝 선 그림(`theme._ARROWS`).

## 7. 움직임 — 쓰지 않음
- 스프링 곡선·눌렀을 때 줄어드는 효과(scale 0.97)는 Qt 스타일시트로 할 수 없고, 작업 도구에는 득이 적다.
- 대신 **즉시 반응**: 마우스를 올리면 테두리·바탕 색이 바뀌고(`:hover`), 누르면 더 진해진다(`:pressed`).
  긴 작업은 진행 창으로 알린다 (`worker.run_with_dialog`).

## 새 화면을 만들 때
1. 색은 `theme.py` 토큰(또는 `dialogs.py` 도우미)만 쓴다. 상태 색은 2장의 네 가지만.
2. 주 버튼은 하나, 오른쪽 끝. 버튼 글이 잘리면 줄을 나눈다 (예: 원문 확인 — 위 줄 ‘이 칸’ 도구, 아래 줄 저장).
3. 내용은 흰 칸, 바탕은 회색. 여백은 4의 배수.
4. 바꾼 화면은 실제 창으로 캡처해 직접 보고 확인한다.
5. 새 창은 창 번호를 받는다 (아래 표).

## 창 번호·버튼 코드

창 제목 앞에 `[2B]`처럼 번호가 붙는다 (숫자 = 작업 단계, 0 = 단계 밖 창, 9 = 결과 보기). 기능을 물을 때 이 번호로 가리킨다.
**Ctrl+D**: 디버깅 모드 — 버튼 글자 대신 `2B-03` 같은 버튼 코드가 보이고, 누르면 코드만 복사된다 (동작 안 함). 다시 Ctrl+D로 끈다.
버튼 코드의 순번은 그 창에서 버튼을 만든 순서다 (줄마다 생기는 버튼은 줄 수에 따라 달라질 수 있다).

새 창을 만들면 `app/qt/ui_codes.py`의 `WINDOW_CODES`와 이 표에 함께 넣는다.

| 번호 | 창 | 클래스 |
| :--- | :--- | :--- |
| 0A | 홈 | `HomePage` |
| 0B | 프로젝트 화면 | `ProjectPage` |
| 0C | 새 프로젝트 | `NewProjectDialog` |
| 0D | 파일 넣기 | `AddFilesDialog` |
| 0E | 카드 자리 직접 그리기 | `CardBoxesDialog` |
| 0F | 영역 지정 | `RegionEditor` |
| 0G | 뺄 쪽 | `ExcludePagesDialog` |
| 0H | 설정 | `SettingsDialog` |
| 0I | 로그인 | `LoginDialog` |
| 0J | 릴리스 노트 | `ReleaseNotesDialog` |
| 0K | 라이선스 | `LicenseDialog` |
| 0Z | 알림·질문 | `QMessageBox` |
| 1A | 글자 추출 | `ExtractDialog` |
| 2A | 아이콘 정의 | `IconReviewDialog` |
| 2B | 카드 보기 · 아이콘 자리 잡기 | `IconPlaceDialog` |
| 2C | 아이콘 사전 | `IconDictionaryDialog` |
| 3A | 원문 확인 | `SourceReviewDialog` |
| 5A | 용어집 확정 | `GlossaryReviewDialog` |
| 5B | 바뀐 용어 반영 | `TermChangesDialog` |
| 6A | 번역 검수 | `TranslationReviewDialog` |
| 8A | 한글 글꼴 고르기 | `FontPlanDialog` |
| 8B | 글꼴 추가 | `AddFontDialog` |
| 9A | 결과 보기 | `ResultViewer` |
| 9B | 그룹 고르기 | `GroupPickDialog` |
| 9C | 시험 기록 | `TestHistoryDialog` |
| 9D | 두 시험 비교 | `TestCompareDialog` |
