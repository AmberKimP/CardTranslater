"""아이콘(과 숫자·기호)만 있는 칸은 글자 지우기·한글 넣기를 하지 않고 원본 그대로 둔다."""

from __future__ import annotations

import json

import pymupdf
import pytest

from app import service
from app.extract import extract_dir
from tests.test_service import GAEGU, make_project

pytestmark = pytest.mark.skipif(not GAEGU.exists(), reason="fonts/ 없음 (scripts/fetch_fonts.py)")


def test_icon_only_regions_rule():
    assert service.icon_only_regions({"a": "[[sword]][[sword]]", "b": "2[[coin]] [[book]]", "c": "[[?]]",
                                      "d": "Gain 2 [[coin]]", "e": "", "f": "5", "g": ["태그"], "h": None,
                                      "i": "<c1>2</c1>[[coin]]", "j": "金貨[[coin]]"}) == ["a", "b", "c", "i"]


def _write_sources(proj, by_page: dict):
    base = extract_dir(proj)
    (base / "pages").mkdir(parents=True, exist_ok=True)
    for pno, src in by_page.items():
        rec = {"page": pno, "model": "claude-sonnet-5-5", "regions": {"이름": {"source": src, "uncertain": False}}}
        (base / "pages" / f"page-{pno:03d}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")


def _clip_pix(pdf, page_index, rect):
    with pymupdf.open(pdf) as d:
        return d[page_index].get_pixmap(dpi=72, clip=rect, alpha=False).samples


def test_icon_only_cell_is_neither_erased_nor_composed(tmp_path):
    proj = make_project(tmp_path, pages=2)
    _write_sources(proj, {1: "HELLO WORLD", 2: "[[sword]][[sword]]"})
    assert service.erase(proj, "1-2").ok
    m1 = json.loads((proj / "cleared" / "meta" / "page-001.json").read_text(encoding="utf-8"))
    m2 = json.loads((proj / "cleared" / "meta" / "page-002.json").read_text(encoding="utf-8"))
    assert [r["name"] for r in m1["regions"]] == ["이름"] and "icon_only_regions" not in m1
    assert m2["regions"] == [] and m2["icon_only_regions"] == ["이름"]
    rect = pymupdf.Rect(m1["regions"][0]["rect_pt"])
    src = next((proj / "source").glob("*.pdf"))
    assert _clip_pix(proj / "cleared" / "cleared.pdf", 1, rect) == _clip_pix(src, 1, rect)  # 2쪽: 지우지 않음
    assert _clip_pix(proj / "cleared" / "cleared.pdf", 0, rect) != _clip_pix(src, 0, rect)  # 1쪽: 지움
    r = service.compose(proj)
    assert r.state == "done"
    assert _clip_pix(proj / "translated" / "translated.pdf", 1, rect) == _clip_pix(src, 1, rect)  # 한글도 넣지 않음
    # 원문을 고쳐 글자가 생기면 그 쪽은 다시 지울 대상이 된다 (아이콘만일 때와 서명이 다름)
    _write_sources(proj, {2: "Gain [[sword]]"})
    assert 2 in service.stale_erased_pages(proj)
