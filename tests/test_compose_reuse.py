"""바뀐 카드만 다시 합성 (F19): 같은 프로젝트에 번역을 다시 넣으면 재료가 바뀐 쪽만 새로 넣고 나머지는 예전 쪽을 쓴다."""

from __future__ import annotations

import json

import pymupdf
import pytest

from app import service
from tests.test_service import GAEGU, make_project

pytestmark = pytest.mark.skipif(not GAEGU.exists(), reason="fonts/ 없음 (scripts/fetch_fonts.py)")


def _report(proj):
    return json.loads((proj / "translated" / "report.json").read_text(encoding="utf-8"))


def _pixmaps(pdf):
    with pymupdf.open(pdf) as d:
        return [pg.get_pixmap(dpi=72, alpha=False).samples for pg in d]


def _set_ko(proj, page, ko):
    p = proj / "samples.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    for c in data["cards"]:
        if c["page"] == page:
            c["이름"]["ko"] = ko
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def composed(tmp_path):
    proj = make_project(tmp_path, pages=3)
    assert service.erase(proj, "1-3").ok
    first = service.compose(proj)
    assert first.state == "done" and first.reused_pages == [] and first.composed_pages == [1, 2, 3]
    return proj


def test_same_inputs_reuse_every_page_and_render_identically(composed):
    proj = composed
    before = _pixmaps(proj / "translated" / "translated.pdf")
    sigs = [r["compose_sig"] for r in _report(proj)["pages_detail"]]
    r = service.compose(proj)
    assert r.state == "done" and r.reused_pages == [1, 2, 3] and r.composed_pages == []
    rep = _report(proj)
    assert rep["reused_pages"] == [1, 2, 3] and rep["composed_pages"] == []
    assert [x["compose_sig"] for x in rep["pages_detail"]] == sigs
    assert all(x.get("reused") for x in rep["pages_detail"])
    assert rep["source_unchanged"] and rep["erased_unchanged"]
    assert _pixmaps(proj / "translated" / "translated.pdf") == before
    for p in (1, 2, 3):  # 쪽 그림·비교 이미지도 옮겨 담는다
        assert (proj / "translated" / f"p{p:03d}.png").exists()
        assert list((proj / "translated" / "compare").glob(f"p{p:03d}_*.png"))
    assert not [d for d in proj.iterdir() if d.name.startswith(".")]


def test_changed_translation_recomposes_only_that_page_in_order(composed):
    proj = composed
    before = _pixmaps(proj / "translated" / "translated.pdf")
    _set_ko(proj, 2, "반가워요")
    r = service.compose(proj)
    assert r.reused_pages == [1, 3] and r.composed_pages == [2]
    rep = _report(proj)
    assert rep["pages"] == [1, 2, 3] and [x["page"] for x in rep["pages_detail"]] == [1, 2, 3]
    assert [bool(x.get("reused")) for x in rep["pages_detail"]] == [True, False, True]
    after = _pixmaps(proj / "translated" / "translated.pdf")
    assert after[0] == before[0] and after[2] == before[2] and after[1] != before[1]
    assert "반가워요" in rep["pages_detail"][1]["extracted_text"]
    # 다시 한 번: 이번에는 모두 그대로
    r2 = service.compose(proj)
    assert r2.reused_pages == [1, 2, 3]
    assert _pixmaps(proj / "translated" / "translated.pdf") == after


def test_region_style_change_recomposes_all(composed):
    proj = composed
    p = proj / "regions.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["regions"][0]["style"]["color"] = "#aa0000"
    p.write_text(json.dumps(data), encoding="utf-8")
    r = service.compose(proj)
    assert r.reused_pages == [] and r.composed_pages == [1, 2, 3]


def test_app_version_change_recomposes_all(composed, monkeypatch):
    monkeypatch.setattr("app.version.VERSION", "999.0.0")
    r = service.compose(composed)
    assert r.reused_pages == [] and r.composed_pages == [1, 2, 3]


def test_test_mode_and_page_spec_never_reuse(composed, tmp_path):
    r = service.compose(composed, test=True, out_root=tmp_path / "runs")
    assert r.state == "done" and r.reused_pages == [] and r.composed_pages == [1, 2, 3]
    r = service.compose(composed, pages="1-2")
    assert r.reused_pages == [] and r.composed_pages == [1, 2]


@pytest.mark.parametrize("damage", ["corrupt_report", "missing_report", "missing_pdf", "missing_png"])
def test_broken_old_result_falls_back_to_full_compose(composed, damage):
    proj = composed
    t = proj / "translated"
    if damage == "corrupt_report":
        (t / "report.json").write_text("{깨짐", encoding="utf-8")
    elif damage == "missing_report":
        (t / "report.json").unlink()
    elif damage == "missing_pdf":
        (t / "translated.pdf").unlink()
    else:
        (t / "p002.png").unlink()
    r = service.compose(proj)
    assert r.state == "done"
    if damage == "missing_png":
        assert r.reused_pages == [1, 3] and r.composed_pages == [2]
    else:
        assert r.reused_pages == [] and r.composed_pages == [1, 2, 3]
    assert len(_pixmaps(t / "translated.pdf")) == 3


def test_parallel_compose_of_changed_pages_keeps_order(composed, monkeypatch):
    proj = composed
    before = _pixmaps(proj / "translated" / "translated.pdf")
    monkeypatch.setenv("CARDTRANSLATER_WORKERS", "2")
    _set_ko(proj, 1, "하나")
    _set_ko(proj, 3, "셋")
    r = service.compose(proj)
    assert r.reused_pages == [2] and r.composed_pages == [1, 3]
    after = _pixmaps(proj / "translated" / "translated.pdf")
    assert after[1] == before[1] and after[0] != before[0] and after[2] != before[2]
    rep = _report(proj)
    assert "하나" in rep["pages_detail"][0]["extracted_text"] and "셋" in rep["pages_detail"][2]["extracted_text"]



def test_swapping_region_font_file_recomposes_all(composed, tmp_path):
    """영역 글꼴 파일 이름은 그대로 두고 내용만 바꿔도(다른 글꼴로 덮어씀) 다시 넣는다."""
    import shutil

    from tests.test_service import ROOT

    proj = composed
    font = tmp_path / "myfont" / "Mine-Regular.ttf"
    font.parent.mkdir()
    shutil.copyfile(GAEGU, font)
    p = proj / "regions.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["regions"][0]["style"]["font"]["file"] = str(font)
    p.write_text(json.dumps(data), encoding="utf-8")
    assert service.compose(proj).composed_pages == [1, 2, 3]
    assert service.compose(proj).reused_pages == [1, 2, 3]
    shutil.copyfile(ROOT / "fonts/notosanskr/NotoSansKR-Regular.ttf", font)
    r = service.compose(proj)
    assert r.reused_pages == [] and r.composed_pages == [1, 2, 3]
    # 이웃 굵기 파일(Mine-Bold.ttf)이 새로 생겨도 굵게가 바뀌므로 다시 넣는다
    shutil.copyfile(GAEGU, font.with_name("Mine-Bold.ttf"))
    assert service.compose(proj).composed_pages == [1, 2, 3]


def test_compose_sig_sees_icons_in_subfolders(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app import typeset_compose

    icons = tmp_path / "icons"
    (icons / "sub").mkdir(parents=True)
    (icons / "sub" / "gold.png").write_bytes(b"a")
    monkeypatch.setattr(typeset_compose, "icon_style_for", lambda _d: {"icons_dir": str(icons)})
    project = SimpleNamespace(template=SimpleNamespace(regions=[]))

    def sig():
        return typeset_compose._compose_sigs(tmp_path, project, [1], {1: {"regions": []}}, {}, "s", "e")[1]

    before = sig()
    (icons / "sub" / "gold.png").write_bytes(b"bb")  # 아래 폴더의 아이콘 그림만 바꿈
    assert sig() != before
