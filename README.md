# CardTranslater
Translate text in cards

보드게임 카드 PDF의 외국어(영어·일본어)를 추출해 엑셀로 검수하고, 서식과 아이콘을 유지한 채 한글로 합성하는 로컬 앱. 자세한 내용은 [docs/PRD.md](docs/PRD.md).

## Project Structure

```
CardTranslater/
├── .claude/agents/   # Claude Code subagent definitions
│   ├── pdf-engine.md          # PDF 영역·글자 지우기·출력
│   ├── extract-translate.md   # Claude 원문 읽기·용어집·번역·아이콘 사전·검수 엑셀
│   ├── korean-typesetter.md   # 한글·아이콘 배치, 서식 유지
│   └── visual-qa.md           # 결과 이미지 검수 (읽기 전용)
├── app/                # 처리 엔진 (Python)
│   ├── regions.py             # 비율 ↔ pt ↔ px 좌표
│   ├── erase.py               # 레드랙션 + 배경 정리 패치 + 태그 라벨
│   ├── typeset*.py            # 한글 줄바꿈·크기 맞추기·글꼴 보정·그리기
│   ├── typeset_compose.py     # 지운 PDF 위에 합성
│   ├── service.py             # 작업 흐름 (모든 화면이 이것만 부름)
│   ├── cli.py                 # 명령줄 진입점
│   └── qt/                    # PySide6 화면 (python -m app.qt)
├── tests/              # pytest
├── fonts/catalog.json  # 한글 글꼴 목록 (OFL)
├── scripts/fetch_fonts.py
├── docs/
│   └── PRD.md
├── requirements.txt
├── .gitignore
└── README.md
```

`projects/`(카드 원본과 `cleared/`·`translated/` 결과), `test_runs/`, 글꼴 파일은 Git에 올리지 않는다.

## 실행

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python scripts/fetch_fonts.py
# 화면 (PySide6): 프로젝트·파일·그룹·뺄 쪽·영역 지정 + 추출·원문 확인·아이콘 이름·용어집·번역·검수 엑셀 + 지우기·한글 넣기·결과 보기·시험 실행
.venv/Scripts/python -m app.qt          # 시작할 때 로그인 방식(Claude 구독 / API 키) 고르기. --no-login 으로 건너뜀
# exe 빌드 -> dist/CardTranslater/CardTranslater.exe (폴더째 배포. 빌드 뒤 자가 점검 --selftest)
.venv/Scripts/python packaging/build.py
# 0~8단계 전체: 게임·프로젝트·그룹 -> 추출 -> ‘추출 완료’ -> 용어집 검증·확정 -> 번역 -> 검수 -> 지우기 -> 한글 넣기 (projects/, games/ 는 git에 올리지 않음)
.venv/Scripts/python -m app.cli new-project --name "Dungeon 1차" --game Dungeon
.venv/Scripts/python -m app.cli add-files --project "projects/Dungeon 1차" --files Items.pdf   # 그룹 목록을 보여 주고 고르게 함
.venv/Scripts/python -m app.cli extract-project --project "projects/Dungeon 1차"
.venv/Scripts/python -m app.cli extract-project --project "projects/Dungeon 1차" --limit 3   # 시험 모드: 파일마다 앞 3장만 (번역·검수·지우기·한글 넣기도 그 카드만, 0이면 끄고 남은 카드부터)
.venv/Scripts/python -m app.cli finish-extraction --project "projects/Dungeon 1차"           # -> 용어집_검증.xlsx
# (이름 없는 아이콘 [[?]]가 있으면 finish-extraction이 먼저 아이콘_검증.xlsx를 만든다)
.venv/Scripts/python -m app.cli find-icons --project "projects/Dungeon 1차"
.venv/Scripts/python -m app.cli import-icons --project "projects/Dungeon 1차" --excel "projects/Dungeon 1차/아이콘_검증.xlsx"
.venv/Scripts/python -m app.cli import-glossary --project "projects/Dungeon 1차" --excel "projects/Dungeon 1차/용어집_검증.xlsx"
.venv/Scripts/python -m app.cli translate --project "projects/Dungeon 1차"              # 5단계: 확정 용어집 기준 초벌 번역
.venv/Scripts/python -m app.cli export-excel --project "projects/Dungeon 1차"           # 파일마다 번역_검수.xlsx
.venv/Scripts/python -m app.cli erase-project --project "projects/Dungeon 1차"          # 7단계, 모든 파일
.venv/Scripts/python -m app.cli compose-project --project "projects/Dungeon 1차"        # 8단계, 검수 엑셀 또는 초벌 번역
.venv/Scripts/python -m app.cli set-pages --project "projects/Dungeon 1차" --file 아이템/Items --exclude 1   # 다른 형식의 쪽 빼기
# 3단계 글자 추출 (기본: 로그인된 Claude Code 요금제로 읽음, 키 불필요. --engine api: API 키·사용료). 멈추면 같은 명령으로 이어 감
.venv/Scripts/python -m app.cli extract --project projects/sample            # --mode double-check(기본: Sonnet + 불확실한 칸만 Opus) | opus, --engine api일 때 --batch(50% 할인)·--budget 2.0
.venv/Scripts/python -m app.cli extract --project projects/sample --test   # 시험 폴더(test_runs/extract-sample-double-check)
.venv/Scripts/python -m app.cli learn-tag-colors --project projects/sample --save       # 태그 라벨 색 -> 글자 사전
.venv/Scripts/python -m app.cli extract-eval --project projects/sample       # 손으로 옮긴 원문과 비교 (--from <시험 폴더>)
# 7단계 글자 지우기 -> projects/sample/cleared/ (한 번만)
.venv/Scripts/python -m app.cli erase --project projects/sample --pages 1-30
# 번역 검수 엑셀 -> projects/sample/번역_검수.xlsx ('번역' 열을 고친다)
.venv/Scripts/python -m app.cli export-excel --project projects/sample
# 8단계 한글 넣기 -> projects/sample/translated/ (지우기 없이 엑셀 번역만 다시 넣음. 고칠 때마다 반복)
.venv/Scripts/python -m app.cli compose --project projects/sample --excel projects/sample/번역_검수.xlsx
.venv/Scripts/python -m pytest -q
```

`--test`를 붙이면 프로젝트 폴더 대신 `test_runs/<시각>-erase|compose/`에 쓴다. 엑셀에 확인할 항목(빈 번역, 빠진 행, 원문 수정)이 있으면 목록을 보여 주고 멈춘다. 확인했으면 `--yes`를 붙여 다시 실행한다. 다른 프로젝트의 엑셀이면 항상 멈춘다.

## Status

1단계(PDF 핵심 실험) 완료, 인쇄 확인 통과(2026-11-04): ClearScan PDF 30쪽 전체(BATTLE 1~15, DUNGEON 16~30)의 지우기·한글 합성 완료. 회전된 배경 이미지(8·21쪽), 라벨 3개(9쪽), 회색 라벨, 글꼴에 없는 글자(♥·…) 대체 글꼴 처리 포함.

Items.pdf(텍스트 레이어 없는 스캔, 위아래 반쪽 카드) 2~16쪽도 지우기·한글 넣기 완료.

이미지 카드(2026-11-05): JPG·PNG 등을 넣으면 카드 한 장이 한 쪽인 PDF로 바꿔 넣음. 한 장에 한 장 / 가로 X × 세로 Y 배열(틈을 찾아 자름, 미리 보기). 명령줄: `add-files --files a.jpg b.jpg --cols 3 --rows 3 --name 시트`.

영역 지정 창(2026-11-04 추가): 카드마다 영역 지정(형식이 카드마다 다른 그룹), 이전 카드 영역 복사, ◀▶·←/→·마우스 휠로 쪽 넘기기, 처음 열 때 화면에 맞춤. 프로젝트·그룹·파일 삭제(.trash로 옮김).

화면(PySide6) 3차: ⑧ 글자 지우기(이미 지운 파일 건너뛰기/다시) ⑨ 한글 넣기(검수 엑셀 확인 항목은 묻고 계속) · 결과 보기(쪽마다 원본·결과 비교, 영역 600dpi 확대, 문제 있는 쪽만) · 시험 실행(몇 쪽만 시험 폴더에서, 프로젝트 결과는 그대로) · 마지막 프로젝트 이어 열기 · 영역 편집기에서 다른 그룹으로 이동·쓰는 글꼴을 목록 맨 위에 굵게 · exe 빌드(PyInstaller onedir).

화면(PySide6) 2차: ① 글자 추출(그룹·모드·엔진, 진행률·취소, 한도로 멈추면 그 시각에 자동 이어 하기) ② 원문 확인(결론 못 냄·읽지 않은 칸을 카드 이미지·두 모델 결과와 비교해 고침) ③ 아이콘 이름(엑셀 없이 확정) ④ 추출 완료→용어집 검증 ⑤ 용어집 가져오기 ⑥ 번역 ⑦ 검수 엑셀.

화면(PySide6) 1차: 프로젝트 목록·새 프로젝트, 파일 넣기(기존/새 그룹), 뺄 쪽, 영역 지정 편집기(그리기·옮기기·크기·회전·글꼴·지우기 옵션, 모르는 항목 보존), 설정(데이터 폴더·엔진). exe 빌드는 3차.

2단계 진행 중: 비전 글자 추출 구현(엔진: Claude Code 요금제 기본 / API 옵션, 모델 opus·sonnet·haiku, 이어 하기, API 일괄 처리·예산).
