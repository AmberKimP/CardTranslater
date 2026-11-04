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

1단계(PDF 핵심 실험): ClearScan PDF 30쪽 전체(BATTLE 1~15, DUNGEON 16~30)의 지우기·한글 합성 완료. 회전된 배경 이미지(8·21쪽), 라벨 3개(9쪽), 회색 라벨, 글꼴에 없는 글자(♥·…) 대체 글꼴 처리 포함.
