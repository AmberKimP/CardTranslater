"""결과 보기에서 고친 카드별 번역 (파일 폴더 file.json의 "card_edits").

검수 엑셀은 그림이 들어 있어 이 앱이 다시 저장하면 그림이 빠진다. 그래서 고친 번역은 엑셀을 건드리지 않고 여기에
두었다가 한글 넣기(run_compose)·글꼴 미리 보기·검수 엑셀 다시 만들기에서 그 칸에 덮어 쓴다.

고칠 때의 번역(base)을 함께 둔다. 그 뒤에 검수 엑셀에서 같은 칸을 고쳤으면(지금 번역 ≠ base) 엑셀이 더 새 것이므로
여기 고친 것은 쓰지 않는다 (엑셀에서 고친 것을 몰래 덮지 않게).
"""

from __future__ import annotations

from pathlib import Path

KEY = "card_edits"


def norm(v) -> str:
    """번역 값 비교용 글: 목록(태그 칸)은 줄로 잇고, 앞뒤 빈칸은 뺀다."""
    if isinstance(v, list):
        return "\n".join(str(x).strip() for x in v)
    return str(v or "").strip()


def load(file_dir: Path | str) -> dict:
    """{쪽(int): {영역: {"ko": 고친 번역, "base": 고칠 때의 번역}}}."""
    from app.workspace import file_settings

    raw = file_settings(Path(file_dir)).get(KEY) or {}
    return {int(p): dict(regs) for p, regs in raw.items() if regs}


def set_text(file_dir: Path | str, page: int, region: str, ko, base) -> None:
    """그 칸의 번역을 ko로 고친다 (base: 지금 쓰는 번역). ko가 base와 같으면 고친 것을 지운다 (원래대로)."""
    from app import workspace

    fd = Path(file_dir)
    data = workspace.file_settings(fd)
    edits = data.setdefault(KEY, {})
    card = edits.setdefault(str(int(page)), {})
    if norm(ko) == norm(base):
        card.pop(region, None)
    else:
        card[region] = {"ko": ko, "base": base}
    if not card:
        edits.pop(str(int(page)), None)
    if not edits:
        data.pop(KEY, None)
    workspace._write(fd / "file.json", data)


def edit_for(edits: dict, page: int, region: str, current) -> dict | None:
    """지금 번역(current)에 쓸 고친 것 (없거나 엑셀이 그 뒤에 바뀌었으면 None)."""
    e = (edits.get(int(page)) or {}).get(region)
    if not e or norm(current) != norm(e.get("base")):
        return None
    return e


def apply(file_dir: Path | str, cards: dict, source_of=None) -> tuple[dict, list[str]]:
    """한글 넣기용 cards({쪽: {영역: {"ko", "source", …}}})에 고친 번역을 덮은 사본과 알림.
    source_of(page, region): 번역이 아예 없던 칸에 새로 넣을 때 쓸 원문 (없으면 원문 없이)."""
    edits = load(file_dir)
    out = {p: dict(v) if isinstance(v, dict) else v for p, v in (cards or {}).items()}
    notes = []
    for page, regs in edits.items():
        for region, e in regs.items():
            card = out.setdefault(page, {"page": page})
            entry = card.get(region) if isinstance(card.get(region), dict) else None
            cur = entry.get("ko") if entry else None
            if norm(cur) != norm(e.get("base")):
                notes.append(f"{page}쪽 {region}: 검수 엑셀에서 그 뒤에 고쳐 결과 보기에서 고친 번역은 쓰지 않음")
                continue
            new = dict(entry or {})
            if "source" not in new and source_of is not None:
                new["source"] = source_of(page, region)
            new["ko"] = e["ko"]
            card[region] = new
    return out, notes
