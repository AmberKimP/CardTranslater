"""번역 검수에서 사람이 ‘확인함’으로 표시한 칸 (파일 폴더 file.json의 "review_checked").

확인할 때 쓰던 번역을 함께 둔다. 그 뒤 번역이 바뀌면(엑셀에서 고침·다시 번역·결과 보기에서 고침) 저절로
‘확인 안 함’이 된다 (확인한 글과 지금 글이 달라서).
"""

from __future__ import annotations

from pathlib import Path

from app.card_edits import norm

KEY = "review_checked"


def load(file_dir: Path | str) -> dict:
    """{쪽(int): {영역: 확인할 때의 번역(norm)}}."""
    from app.workspace import file_settings

    raw = file_settings(Path(file_dir)).get(KEY) or {}
    return {int(p): dict(regs) for p, regs in raw.items() if regs}


def mark(file_dir: Path | str, page: int, region: str, ko) -> None:
    """확인함으로 표시 (ko: 확인한 번역). ko가 None이면 확인 취소."""
    from app import workspace

    fd = Path(file_dir)
    data = workspace.file_settings(fd)
    marks = data.setdefault(KEY, {})
    card = marks.setdefault(str(int(page)), {})
    if ko is None:
        card.pop(region, None)
    else:
        card[region] = norm(ko)
    if not card:
        marks.pop(str(int(page)), None)
    if not marks:
        data.pop(KEY, None)
    workspace._write(fd / "file.json", data)


def state(marks: dict, page: int, region: str, ko) -> tuple[bool, bool]:
    """(확인함, 확인한 뒤 바뀜)."""
    m = (marks.get(int(page)) or {}).get(region)
    if m is None:
        return False, False
    return (m == norm(ko)), (m != norm(ko))
