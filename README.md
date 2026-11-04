# CardTranslater
Translate text in cards

보드게임 카드 PDF의 외국어(영어·일본어)를 추출해 엑셀로 검수하고, 서식과 아이콘을 유지한 채 한글로 합성하는 로컬 앱. 자세한 내용은 [docs/PRD.md](docs/PRD.md).

## Project Structure

```
CardTranslater/
├── .claude/agents/   # Claude Code subagent definitions
│   ├── pdf-engine.md          # PDF 추출·글자 지우기·출력
│   ├── korean-typesetter.md   # 한글·아이콘 배치, 서식 유지
│   └── visual-qa.md           # 결과 이미지 검수 (읽기 전용)
├── app/                # 처리 엔진 (Python)
│   ├── regions.py             # 비율 ↔ pt ↔ px 좌표
│   ├── erase.py               # 레드랙션 + 배경 정리 패치 + 태그 라벨
│   ├── typeset*.py            # 한글 줄바꿈·크기 맞추기·글꼴 보정·그리기
│   ├── typeset_compose.py     # 지운 PDF 위에 합성
│   └── cli.py                 # 명령줄 진입점
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
# 0~8단계 전체: 게임·프로젝트·그룹 -> 추출 -> ‘추출 완료’ -> 용어집 검증·확정 -> 번역 -> 검수 -> 지우기 -> 한글 넣기 (projects/, games/ 는 git에 올리지 않음)
.venv/Scripts/python -m app.cli new-project --name "Dungeon 1차" --game Dungeon
.venv/Scripts/python -m app.cli add-files --project "projects/Dungeon 1차" --files Items.pdf   # 그룹 목록을 보여 주고 고르게 함
.venv/Scripts/python -m app.cli extract-project --project "projects/Dungeon 1차"
.venv/Scripts/python -m app.cli finish-extraction --project "projects/Dungeon 1차"           # -> 용어집_검증.xlsx
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

2단계 진행 중: 비전 글자 추출 구현(엔진: Claude Code 요금제 기본 / API 옵션, 모델 opus·sonnet·haiku, 이어 하기, API 일괄 처리·예산).
