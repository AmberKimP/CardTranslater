"""번역 검수 엑셀 왕복(app.excel_io) 테스트. 작은 가짜 프로젝트를 만들어 쓴다."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pymupdf = pytest.importorskip("pymupdf")
openpyxl = pytest.importorskip("openpyxl")

from app.excel_io import export_translation, import_translation, make_key  # noqa: E402
from app.project import load_project  # noqa: E402


def _make_project(root: Path, name: str = "demo") -> Path:
    p = root / name
    (p / "source").mkdir(parents=True)
    doc = pymupdf.open()
    for i in range(2):
        pg = doc.new_page(width=250, height=180)
        pg.insert_text((30, 140), f"card {i + 1}", fontsize=8)
    doc.save(p / "source" / "cards.pdf")
    (p / "regions.json").write_text(json.dumps({
        "source_language": "ja",
        "regions": [
            {"name": "태그", "kind": "tag_pills", "rect": {"x": 0.1, "y": 0.6, "w": 0.4, "h": 0.06}},
            {"name": "본문", "kind": "text", "rect": {"x": 0.1, "y": 0.75, "w": 0.6, "h": 0.1}},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    (p / "samples.json").write_text(json.dumps({"cards": [
        {"page": 1, "태그": {"source": ["竜", "炎"], "ko": ["용", "불꽃"]}, "본문": {"source": "一陣の熱風", "ko": "한 줄기 열풍"}},
        {"page": 2, "태그": {"source": ["毒"], "ko": ["독"]}, "본문": {"source": "骨", "ko": "뼈"}},
    ]}, ensure_ascii=False), encoding="utf-8")
    return p


def _edit(xlsx: Path, fn) -> None:
    wb = openpyxl.load_workbook(xlsx)
    ws = wb["번역"]
    head = [c.value for c in ws[1]]
    rows = {r[head.index("key")].value: {h: r[i] for i, h in enumerate(head)} for r in ws.iter_rows(min_row=2)}
    fn(rows, wb)
    wb.save(xlsx)


def test_round_trip_uses_translation_column(tmp_path):
    proj = load_project(_make_project(tmp_path))
    x = export_translation(proj, tmp_path / "t.xlsx", [1, 2])
    res = import_translation(proj, x)
    assert not res.blocking and not res.issues and res.rows == 4
    assert res.cards[1]["태그"]["ko"] == ["용", "불꽃"]  # 태그는 셀 안 줄바꿈 -> 목록
    assert res.cards[2]["본문"]["ko"] == "뼈"

    k_body = make_key("cards.pdf", 1, "본문")
    k_tag = make_key("cards.pdf", 2, "태그")

    def change(rows, wb):
        rows[k_body]["번역"].value = "새 번역\n둘째 줄"
        rows[k_body]["글자 크기"].value = 9
        rows[k_body]["초벌 번역"].value = "무시되어야 함"
        rows[k_tag]["번역"].value = "-"

    _edit(x, change)
    res = import_translation(proj, x)
    assert not res.blocking and not res.issues
    assert res.cards[1]["본문"]["ko"] == "새 번역\n둘째 줄"
    assert res.cards[1]["본문"]["fixed_size_pt"] == 9.0
    assert res.cards[2]["태그"]["ko"] == "-"  # 지우기만


def test_import_reports_empty_missing_and_source_edits(tmp_path):
    proj = load_project(_make_project(tmp_path))
    x = export_translation(proj, tmp_path / "t.xlsx", [1, 2])
    k1 = make_key("cards.pdf", 1, "본문")
    k2 = make_key("cards.pdf", 2, "본문")

    def change(rows, wb):
        rows[k1]["번역"].value = None
        rows[k1]["원문"].value = "一陣の熱風!"
        wb["번역"].delete_rows(rows[k2]["key"].row)

    _edit(x, change)
    res = import_translation(proj, x)
    assert not res.blocking
    text = "\n".join(res.issues)
    assert "번역이 비어 있음" in text and k1 in text
    assert "원문을 고친 행" in text
    assert "엑셀에 없는 행" in text and k2 in text
    assert res.cards[1]["본문"]["ko"] is None


def test_import_stops_for_other_project(tmp_path):
    a = load_project(_make_project(tmp_path, "a"))
    b = load_project(_make_project(tmp_path, "b"))
    x = export_translation(a, tmp_path / "a.xlsx", [1])
    res = import_translation(b, x)
    assert res.blocking and "다른 프로젝트" in res.blocking[0]
