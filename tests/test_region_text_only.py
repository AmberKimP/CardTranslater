"""영역 설정 ‘아이콘: 글자만 (아이콘 없음)’ (style.icons == "none").

① 추출 요청에 아이콘 없음을 알리고 응답의 [[?]]를 빼며, ② 원문 확인은 그 칸을 찾지도 세지도 않고,
글자 지우기는 그 칸에서 아이콘 자리를 찾지 않는다. 기본(키 없음)은 예전과 같다."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pymupdf
import pytest

from app import extract, icons, service
from app.project import load_project
from app.regions import RatioRect, Region, RegionTemplate, text_only
from tests.test_fonts import qapp  # noqa: F401


def _rect(y: float) -> dict:
    return {"x": 0.1, "y": y, "w": 0.6, "h": 0.15}


def _setup(tmp_path, *, with_extract: bool = True):
    """영역 2개: ‘이름’(글자만), ‘본문’(기본). 1쪽 원문 두 칸 모두 [[?]]가 있다."""
    from tests.test_qt import _project

    info = _project(tmp_path)
    g = service.project_overview(info.dir)["groups"][0]
    assert service.save_region_template(g["dir"], {"source_language": "ja", "regions": [
        {"name": "이름", "kind": "text", "rect": _rect(0.1), "style": {"icons": "none"}},
        {"name": "본문", "kind": "text", "rect": _rect(0.5)}]}) == []
    fd = Path(g["files"][0]["dir"])
    if with_extract:
        d = fd / "extract" / "pages"
        d.mkdir(parents=True)
        (d / "page-001.json").write_text(json.dumps({"page": 1, "regions": {
            "이름": {"source": "勇者[[?]]", "uncertain": False, "note": ""},
            "본문": {"source": "[[?]]を得る", "uncertain": False, "note": ""}}}, ensure_ascii=False), encoding="utf-8")
    return info, g, fd


# ---------------------------------------------------------------- 저장 형식
def test_text_only_helper_and_validation():
    assert text_only({"kind": "text", "style": {"icons": "none"}})
    assert not text_only({"kind": "text"}) and not text_only({"kind": "text", "style": {}})
    assert not text_only({"kind": "tag_pills", "style": {"icons": "none"}})  # 글자 영역에만 뜻이 있다
    assert not text_only(None)
    assert text_only(Region("a", RatioRect(0, 0, 1, 1), style={"icons": "none"}))
    assert not text_only(Region("a", RatioRect(0, 0, 1, 1)))
    ok = {"regions": [{"name": "a", "kind": "text", "rect": _rect(0.1), "style": {"icons": "none"}}]}
    assert service.validate_region_template(ok) == []
    bad = {"regions": [{"name": "a", "kind": "text", "rect": _rect(0.1), "style": {"icons": "maybe"}}]}
    assert any("아이콘" in p for p in service.validate_region_template(bad))


def test_template_round_trip_and_per_card(tmp_path):
    _info, g, fd = _setup(tmp_path, with_extract=False)
    tpl = load_project(fd).template
    by = {r.name: r for r in tpl.regions_for(1)}
    assert text_only(by["이름"]) and not text_only(by["본문"])
    saved = json.loads((Path(g["dir"]) / "regions.json").read_text(encoding="utf-8"))
    assert "style" not in saved["regions"][1]  # 기본은 적지 않는다 (예전 파일과 같다)


def test_region_editor_icons_combo(qapp, tmp_path):  # noqa: F811
    from app.qt.region_editor import RegionEditor

    _info, g, _fd = _setup(tmp_path, with_extract=False)
    data = json.loads((Path(g["dir"]) / "regions.json").read_text(encoding="utf-8"))
    data["regions"].append({"name": "태그", "kind": "tag_pills", "rect": _rect(0.8)})
    (Path(g["dir"]) / "regions.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    ed = RegionEditor(Path(g["dir"]), [Path(f["dir"]) for f in g["files"]])
    items = {i.name: i for i in ed.items()}
    name, body, tag = items["이름"], items["본문"], items["태그"]
    name.setSelected(True)
    ed._to_form(name)
    assert ed.f_icons.currentData() == "none" and ed.f_icons.isEnabled()
    rows = [ed.list.item(i).text() for i in range(ed.list.count())]
    assert any("이름" in t and "글자만" in t for t in rows) and not any("본문" in t and "글자만" in t for t in rows)
    name.setSelected(False)
    body.setSelected(True)
    ed._to_form(body)
    assert ed.f_icons.currentData() == ""  # 기본: 섞여 있음
    ed.f_icons.setCurrentIndex(ed.f_icons.findData("none"))
    ed._from_form()
    assert body.data["style"]["icons"] == "none"
    ed.f_icons.setCurrentIndex(ed.f_icons.findData(""))
    ed._from_form()
    assert "icons" not in (body.data.get("style") or {})  # 기본은 적지 않는다
    body.setSelected(False)
    tag.setSelected(True)
    ed._to_form(tag)
    assert not ed.f_icons.isEnabled()  # 태그 라벨에는 없다


# ---------------------------------------------------------------- ① 추출
def test_extract_prompt_and_response(tmp_path):
    _info, _g, fd = _setup(tmp_path, with_extract=False)
    proj = load_project(fd)
    doc = pymupdf.open(next((fd / "source").glob("*.pdf")))
    try:
        params = extract.build_params(proj, 1, doc)
    finally:
        doc.close()
    texts = [c["text"] for c in params["messages"][0]["content"] if c["type"] == "text"]
    name_line = next(t for t in texts if t.startswith("Region name: 이름"))
    body_line = next(t for t in texts if t.startswith("Region name: 본문"))
    assert extract.NO_ICON_HINT in name_line and "[[?]]" in name_line
    assert body_line == "Region name: 본문 (kind: text). "  # 기본 영역의 요청은 예전과 같다
    assert extract.no_icon_names(proj, 1) == {"이름"}

    data = {"regions": [{"name": "이름", "text": "勇者 [[?]] 様\n[[?]]王", "uncertain": False, "note": ""},
                        {"name": "본문", "text": "[[?]]を得る", "uncertain": False, "note": ""}]}
    got = extract.regions_from_json(data, {"이름": "text", "본문": "text"}, {"이름"})
    assert got["이름"]["source"] == "勇者 様\n王" and "[[?]] 2개" in got["이름"]["note"]
    assert got["본문"] == {"source": "[[?]]を得る", "uncertain": False, "note": ""}  # 기본 영역은 그대로
    assert extract.regions_from_json(data, {"이름": "text", "본문": "text"}) == {
        "이름": {"source": "勇者 [[?]] 様\n[[?]]王", "uncertain": False, "note": ""},
        "본문": {"source": "[[?]]を得る", "uncertain": False, "note": ""}}  # 글자만 영역이 없으면 예전과 같다


def test_drop_unknown_icons_keeps_named_and_lists():
    r = extract.drop_unknown_icons({"source": "[[coin]] 1枚[[?]]", "uncertain": True, "note": "흐림"})
    assert r["source"] == "[[coin]] 1枚" and r["uncertain"] and r["note"].startswith("흐림; ")
    same = {"source": ["a", "b"], "note": ""}
    assert extract.drop_unknown_icons(same) is same
    plain = {"source": "abc", "note": ""}
    assert extract.drop_unknown_icons(plain) is plain


def test_claude_code_prompt_uses_hint():
    reg = Region("이름", RatioRect(0, 0, 1, 1), style={"icons": "none"})
    assert extract.region_hint(reg).endswith(extract.NO_ICON_HINT)
    assert extract.region_hint(Region("본문", RatioRect(0, 0, 1, 1))) == ""


# ---------------------------------------------------------------- ② 원문 확인
def test_icon_scan_skips_text_only(tmp_path, monkeypatch):
    info, _g, _fd = _setup(tmp_path)
    assert icons.pending_count(info) == 1 and icons.token_count(info) == 1  # ‘이름’ 칸의 [[?]]는 세지 않는다
    seen = []

    def spy(page, region):
        seen.append(region.name)
        return np.full((40, 200, 3), 255, np.uint8)
    monkeypatch.setattr(icons, "region_image", spy)
    res = icons.scan(info, tmp_path / "work")
    assert seen == ["본문"]
    assert {o.region for o in res.occurrences} <= {"본문"}


def test_icon_reread_pending_skips_text_only(tmp_path):
    info, _g, fd = _setup(tmp_path)
    f = fd / "extract" / "pages" / "page-001.json"
    rec = json.loads(f.read_text(encoding="utf-8"))
    for r in rec["regions"].values():
        r.update(icon_reread=True, icon_changed=True)
    f.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    assert [c["region"] for c in service.icon_reread_pending([fd])] == ["본문"]


# ---------------------------------------------------------------- 글자 지우기
@pytest.mark.parametrize("style, called", [({"icons": "none"}, False), ({}, True)])
def test_erase_skips_icon_detection(monkeypatch, style, called):
    from app import erase
    from tests.test_erase_picture_fable import BOX, H, W, _art, _glyph_row, _page

    img = _art(6)
    _glyph_row(img, 40, 40, 16)
    doc, page = _page(img)
    reg = Region(name="본문", kind="text", style=style, erase={"mode": "picture"},
                 rect=RatioRect(BOX[0] / W, BOX[1] / H, (BOX[2] - BOX[0]) / W, (BOX[3] - BOX[1]) / H))
    calls = []
    real = icons.icon_boxes
    monkeypatch.setattr(icons, "icon_boxes", lambda im, text: calls.append(text) or real(im, text))
    erase.erase_page(page, RegionTemplate(regions=[reg], protect=[]), seed=3,
                     sources={"본문": "가나다라마바사[[?]]아자차카타파하가나"})
    doc.close()
    assert bool(calls) is called


def test_erase_text_only_named_icon_still_found(monkeypatch):
    """이름 붙은 [[이름]]은 사람이 정한 것: 글자만 영역이어도 예전처럼 자리를 찾는다."""
    from app import erase
    from tests.test_erase_picture_fable import BOX, H, W, _art, _glyph_row, _page

    img = _art(6)
    _glyph_row(img, 40, 40, 16)
    doc, page = _page(img)
    reg = Region(name="본문", kind="text", style={"icons": "none"}, erase={"mode": "picture"},
                 rect=RatioRect(BOX[0] / W, BOX[1] / H, (BOX[2] - BOX[0]) / W, (BOX[3] - BOX[1]) / H))
    calls = []
    real = icons.icon_boxes
    monkeypatch.setattr(icons, "icon_boxes", lambda im, text: calls.append(text) or real(im, text))
    erase.erase_page(page, RegionTemplate(regions=[reg], protect=[]), seed=3,
                     sources={"본문": "가나다라마바사[[coin]]아자차카타[[?]]파하가나"})
    doc.close()
    assert calls == ["가나다라마바사[[coin]]아자차카타파하가나"]  # [[?]]만 빠졌다
