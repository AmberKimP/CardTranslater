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

`projects/`(카드 원본), `test_runs/`, `output/`, 글꼴 파일은 Git에 올리지 않는다.

## 실행

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python scripts/fetch_fonts.py
.venv/Scripts/python -m app.cli erase   --project projects/sample --pages 1-3
.venv/Scripts/python -m app.cli compose --project projects/sample --erase-run test_runs/<시각>-erase --pages 1-3
.venv/Scripts/python -m pytest -q
```

## Status

1단계(PDF 핵심 실험): ClearScan PDF 1~3쪽의 지우기·한글 합성 완료, 시각 검수 합격(경미 3건).
