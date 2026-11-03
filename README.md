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
├── docs/
│   └── PRD.md
├── .gitignore
└── README.md
```

## Status

1단계: PDF 핵심 실험(프로토타입) 준비 중.
