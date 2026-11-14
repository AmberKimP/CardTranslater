"""카드 보기 ‘이 칸 원본 그대로’ — ‘문단마다 다른 글꼴’ 영역은 아이콘이 있는 문단만 원본 그대로 (지우기·번역·한글 넣기 안 함),
다른 문단은 한글화한다. ‘한 칸 모두 같은 글꼴’ 영역은 예전처럼 칸 전체."""

from __future__ import annotations

import json

import numpy as np
import pymupdf
import pytest

from app import erase_text, extract, font_plan, icons, service
from app import typeset_compose as tc
from tests.test_batch_1170 import _confirm_all
from tests.test_icons import _add_run_partner, _project

SRC = "いずれかを選択し実行\n[[?]][[?]]"


def _cell(fd, page=1):
    p = fd / "extract" / "pages" / f"page-{page:03d}.json"
    return json.loads(p.read_text(encoding="utf-8"))["regions"]["본문"]


def _set_src(fd, text, page=1):
    for p in (fd / "extract" / "pages" / f"page-{page:03d}.json", fd / "extract" / f"page-{page:03d}.json"):
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            d["regions"]["본문"]["source"] = text
            p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def sentence_mode(monkeypatch):
    real = font_plan.region_font_modes
    monkeypatch.setattr(font_plan, "region_font_modes", lambda fd: {**real(fd), "본문": "sentence"})


def _kept_project(tmp_path, name):
    """1쪽 본문: 글 줄 하나 + 아이콘 줄 하나 ([[?]] 두 자리)."""
    info, fd = _project(tmp_path, name, pages=1)
    host, extra = _add_run_partner(info, fd)
    _set_src(fd, SRC)
    return info, fd, host, extra


# ---------------------------------------------------------------- 문단 고르기
def test_icon_line_is_its_own_paragraph():
    """문장 끝 없이 이어진 아이콘 줄도 앞 글 줄과 한 문단이 되지 않는다 (글 줄은 번역한다)."""
    assert extract.keep_paragraph_lines(SRC) == [1]
    assert extract.keep_paragraph_lines("効果を得る。\nコストを払い[[coin]]を\n得る。\n次へ進む") == [1, 2]
    assert extract.keep_paragraph_lines("アイコンなし") == []


def test_keep_state_partial_broken_whole():
    cell = {"source": SRC, extract.KEEP_PARAGRAPHS: {"lines": [1], "n": 2}}
    assert extract.keep_state(cell) == ("partial", [1])
    assert extract.translate_source(cell) == "いずれかを選択し実行"
    assert not extract.keep_whole(cell)
    # 확정으로 [[?]]가 빠져 아이콘 줄이 비어도 줄 번호는 맞는다
    assert extract.keep_state({**cell, "source": "いずれかを選択し実行\n"}) == ("partial", [1])
    # 원문 확인에서 줄 수가 바뀜: 맞출 수 없어 칸 전체를 원본 그대로
    broken = {**cell, "source": "いずれかを選択し実行"}
    assert extract.keep_state(broken)[0] == "broken" and extract.keep_whole(broken)
    # 남길 줄이 글 줄 전부: 칸 전체와 같다
    assert extract.keep_state({"source": "[[a]]\n[[b]]", extract.KEEP_PARAGRAPHS: {"lines": [0, 1], "n": 2}})[0] == "whole"


# ---------------------------------------------------------------- 카드 보기: 켜기·끄기·확정
def test_sentence_mode_keeps_only_icon_paragraph(tmp_path, sentence_mode):
    info, fd, host, extra = _kept_project(tmp_path, "kp")
    r = service.keep_cell_original(info.dir, host, True)
    assert r["mode"] == "partial" and r["paragraphs"] == 1 and set(r["not_icon"]) == {host, extra}
    cell = _cell(fd)
    kp = cell[extract.KEEP_PARAGRAPHS]
    assert not cell.get("keep_original") and kp["lines"] == [1] and kp["n"] == 2
    # 카드 보기에서 찾은 아이콘 자리(원본 쪽 pt)를 함께 적어 둔다 (확정하면 찾기 결과가 지워진다)
    scan = icons.load_scan(info.game_dir / "work" / "icons")
    n_boxes = sum(1 for o in scan.occurrences if o.page == 1 and o.box)
    assert len(kp.get("boxes_pt") or []) == n_boxes >= 1
    b = kp["boxes_pt"][0]
    assert b[0] < b[2] and b[1] < b[3]
    assert service.cell_keep_mode(info.dir, host) == "partial" and service.cell_kept(info.dir, host)
    # 번역·용어집·검수에는 남은 문단만
    from app.excel_io import source_of
    from app.glossary import collect_sources
    from app.project import load_project

    srcs = [s.text for s in collect_sources(info)]
    assert srcs == ["いずれかを選択し実行"]
    assert source_of(load_project(fd), extract.load_extracted(fd), 1, "본문")[0] == "いずれかを選択し実行"
    # 원문 확인 화면용
    c = next(x for x in service.extracted_cells(fd) if x["page"] == 1)
    assert c["keep_mode"] == "partial" and c["keep_lines"] == [1] and not c["keep_original"]
    # 결과 보기용
    info2 = service.card_edit_info(fd, 1, "본문")
    assert info2["keep_partial"] == 1 and not info2["keep_original"]


def test_cell_mode_keeps_whole_cell_as_before(tmp_path):
    info, fd, host, extra = _kept_project(tmp_path, "kc")
    r = service.keep_cell_original(info.dir, host, True)
    assert r["mode"] == "whole"
    cell = _cell(fd)
    assert cell["keep_original"] and extract.KEEP_PARAGRAPHS not in cell
    assert service.cell_keep_mode(info.dir, host) == "whole"
    from app.glossary import collect_sources

    assert collect_sources(info) == []


def test_turning_off_restores_cell_and_slots(tmp_path, sentence_mode):
    info, fd, host, extra = _kept_project(tmp_path, "koff")
    service.keep_cell_original(info.dir, host, True)
    r = service.keep_cell_original(info.dir, host, False)
    assert r["mode"] == "" and set(r["restored"]) == {host, extra}
    cell = _cell(fd)
    assert extract.KEEP_PARAGRAPHS not in cell and not cell.get("keep_original")
    assert service.cell_keep_mode(info.dir, host) == ""
    states = {it["occ"]: it["state"] for it in service.icon_place(info.dir, host)["items"]}
    assert "not_icon" not in states.values()


def test_survives_icon_confirm_token_rewriting(tmp_path, sentence_mode):
    """확정하면 ‘아이콘 아님’이 된 [[?]]는 원문에서 빠지지만 줄바꿈은 남아 남길 줄이 그대로 맞는다.
    원본 그대로로 빠진 자리는 사람이 정한 것이라 칸이 ‘확인 필요’가 되지 않는다."""
    info, fd, host, extra = _kept_project(tmp_path, "kconf")
    service.keep_cell_original(info.dir, host, True)
    assert _confirm_all(info).state == "confirmed"
    cell = _cell(fd)
    assert "[[" not in cell["source"] and cell["source"].count("\n") == 1
    assert extract.keep_state(cell)[0] == "partial"
    assert extract.translate_source(cell) == "いずれかを選択し実行"
    assert not cell.get("uncertain") and not cell.get("icon_changed")
    assert service.icon_reread_pending([fd]) == []


def test_erase_lines_and_signature(tmp_path, sentence_mode):
    """지우기에 넘길 남길 줄(빈 줄 뺀 번호)·서명: 켜고 끄면 서명이 바뀌어 다시 지운다. 칸 전체(또는 맞출 수 없음)는
    ‘번역 안 함’ 칸으로 넘긴다."""
    from app.service import _translate_erase as te

    regions = {"본문": {"source": "\nいずれかを選択し実行\n[[x]]",
                        extract.KEEP_PARAGRAPHS: {"lines": [2], "n": 3, "boxes_pt": [[1, 2, 3, 4]]}}}
    assert te.erase_keep_lines(regions) == {"본문": {"source": "\nいずれかを選択し実行\n[[x]]", "lines": [1],
                                                    "boxes_pt": [[1, 2, 3, 4]]}}
    # 확정으로 아이콘 줄이 비면 자리 표시로 채워 줄 수를 지킨다 (빠지면 아이콘 줄을 글 줄로 잘못 맞춘다)
    emptied = {"본문": {"source": "いずれかを選択し実行\n", extract.KEEP_PARAGRAPHS: {"lines": [1], "n": 2}}}
    assert te.erase_keep_lines(emptied)["본문"] == {"source": "いずれかを選択し実行\n" + te.KEEP_BLANK_LINE,
                                                  "lines": [1], "boxes_pt": []}
    srcs, kept = te.erase_sources(regions)
    assert kept == set()
    broken = {"본문": {"source": "いずれかを選択し実行", extract.KEEP_PARAGRAPHS: {"lines": [2], "n": 3}}}
    assert te.erase_sources(broken)[1] == {"본문"} and te.erase_keep_lines(broken) == {}
    info, fd, host, extra = _kept_project(tmp_path, "ksig")
    from app.project import load_project

    proj = load_project(fd)
    s0 = te.erase_sig(proj, 1, srcs, None, kept)
    assert te.erase_sig(proj, 1, srcs, None, kept, {}) == s0  # 남길 줄이 없으면 예전 서명 그대로
    assert te.erase_sig(proj, 1, srcs, None, kept, te.erase_keep_lines(regions)) != s0


# ---------------------------------------------------------------- 지우기: 남길 줄은 지우지 않음
def _tm(mask):
    return erase_text.TextMask(mask=mask, core=mask.copy(), busy=False, texture=0.0)


def test_match_source_lines_keeps_kept_paragraph_lines():
    m = np.zeros((60, 300), bool)
    m[10:24, 10:280] = True  # 글 줄
    m[34:48, 10:120] = True  # 아이콘 문단 줄 (글자로 잡힘)
    tm = _tm(m)
    lay = {"lines": [{"bbox": [10, 10, 280, 24], "glyph_h": 14, "color": "#202020"},
                     {"bbox": [10, 34, 120, 48], "glyph_h": 14, "color": "#202020"}]}
    out = erase_text.match_source_lines(tm, lay, "いずれかを選択し実行する\nコスト[[x]]", keep_idx=[1])
    assert [bool(ln.get("keep")) for ln in out["lines"]] == [False, True]
    assert tm.mask[10:24, 10:280].any() and not tm.mask[30:52, :].any()


def _line(y0, y1, x0=10, x1=280):
    return {"bbox": [x0, y0, x1, y1], "glyph_h": y1 - y0, "color": "#202020"}


def test_match_keeping_finds_kept_line_by_icon_box_among_extra_lines():
    """찾은 줄이 원문보다 많아도(카드 번호 등) 아이콘 자리 높이의 줄을 남길 줄로 정하고 나머지만 맞춘다."""
    m = np.zeros((90, 300), bool)
    for y0, y1 in ((10, 24), (34, 48), (60, 64)):
        m[y0:y1, 10:280] = True
    tm = _tm(m)
    lay = {"lines": [_line(10, 24), _line(34, 48, 10, 120), _line(60, 64, 200, 230)]}
    out = erase_text.match_source_lines(tm, lay, "いずれかを選択し実行する\nコスト[[x]]", keep_idx=[1],
                                        keep_rows=[(32, 50)])
    keep = [ln for ln in out["lines"] if ln.get("keep")]
    assert len(keep) == 1 and keep[0]["bbox"][1] == 34
    assert tm.mask[10:24, 10:280].any() and not tm.mask[30:52, :].any()


def test_match_keeping_with_undetected_color_icons_erases_text_only():
    """남길 줄이 아이콘 줄뿐이고 색 그림이라 줄로 잡히지 않았다: 글 줄만 맞춰 지운다."""
    m = np.zeros((60, 300), bool)
    m[10:24, 10:280] = True
    tm = _tm(m)
    out = erase_text.match_source_lines(tm, {"lines": [_line(10, 24)]}, "いずれかを選択し実行\n·", keep_idx=[1],
                                        keep_rows=[(30, 56)])
    assert not out.get("keep_failed") and tm.mask[10:24].any()


def test_match_keeping_fails_when_only_icon_row_was_detected():
    """공주1 2쪽: 숫자가 든 상자 아이콘 줄만 글자 줄로 잡히고 작은 글 줄은 못 찾음. 아이콘 줄을 글 줄로 맞춰 지우지 않는다."""
    m = np.zeros((80, 300), bool)
    m[30:70, 20:280] = True  # 아이콘 줄 (상자 속 숫자)
    tm = _tm(m)
    lay = {"lines": [_line(30, 70, 20, 280)]}
    out = erase_text.match_source_lines(tm, lay, "いずれかを選択し実行\n /", keep_idx=[1], keep_rows=[(28, 72)])
    assert out.get("keep_failed") and not tm.mask.any()
    tm2 = _tm(m.copy())  # 아이콘 자리를 모를 때도 (줄 수가 원문과 달라) 지우지 않는다
    out2 = erase_text.match_source_lines(tm2, {"lines": [_line(30, 70, 20, 280)]}, "いずれかを選択し実行\n /",
                                         keep_idx=[1])
    assert out2.get("keep_failed") and not tm2.mask.any()


def test_match_source_lines_erases_nothing_when_kept_lines_cannot_be_matched():
    m = np.zeros((60, 300), bool)
    m[10:24, 10:280] = True
    tm = _tm(m)
    lay = {"lines": [{"bbox": [10, 10, 280, 24], "glyph_h": 14, "color": "#202020"}]}
    out = erase_text.match_source_lines(tm, lay, "一行目のテキスト\n二行目[[x]]のテキスト\n三行目", keep_idx=[1])
    assert out.get("keep_failed") and not tm.mask.any() and out["lines"] == []


# ---------------------------------------------------------------- 한글 넣기: 남은 문단은 제 줄 자리에
def _ln(x0, y, x1, gh=4.32, keep=False):
    d = {"bbox_pt": [x0, y, x1, y + gh], "glyph_h_pt": gh, "color": "#505050", "stroke_color": None,
         "stroke_pt": 0.0, "weight": 0.2, "bold": False, "align": "left", "accents": []}
    if keep:  # 원본 그대로 둔 문단의 줄 (지우기가 keep과 keep_para를 함께 적는다)
        d["keep"] = d["keep_para"] = True
    return d


def _meta(lines, **flags):
    return {"regions": [{"name": "본문", "kind": "text", "rect_pt": [10, 10, 170, 70], "text_layer": {},
                         "text_layout": {"lines": lines, "dividers_pt": [], "icons": [], **flags}}]}


def _compose(meta, ko, source):
    sdoc, edoc = pymupdf.open(), pymupdf.open()
    sp, ep = sdoc.new_page(width=180, height=80), edoc.new_page(width=180, height=80)
    return tc.compose_page(ep, sp, meta, {"본문": {"ko": ko, "source": source}})["본문"]


def test_typeset_puts_remaining_paragraph_in_its_own_slot_below_kept():
    """아이콘 문단(위)은 남기고 글 문단(아래)만 번역: 한글은 위 자리로 올라가지 않고 원래 아래 줄 자리에."""
    meta = _meta([_ln(20, 20, 120, keep=True), _ln(20, 40, 160)], keep_paragraphs=True)
    rec = _compose(meta, "하나를 골라 실행한다", "いずれかを選択し実行")
    assert len(rec["line_boxes_pt"]) == 1
    assert rec["line_boxes_pt"][0][1] == pytest.approx(40, abs=1.5)


def test_typeset_remaining_paragraph_above_kept_and_no_whole_region_fallback():
    meta = _meta([_ln(20, 20, 160), _ln(20, 40, 120, keep=True)], keep_paragraphs=True)
    rec = _compose(meta, "하나를 골라 실행한다", "いずれかを選択し実行")
    assert rec["line_boxes_pt"][0][1] == pytest.approx(20, abs=1.5)
    assert all(b[3] < 40 for b in rec["line_boxes_pt"])  # 남긴 아이콘 줄 위에 그리지 않는다
    # 넣을 줄 자리가 없으면 영역 전체에 넣지 않는다 (남긴 문단을 덮지 않게)
    rec2 = _compose(_meta([_ln(20, 40, 120, keep=True)], keep_paragraphs=True), "번역", "原文")
    assert rec2["status"] == [tc.STATUS_KEEP_NO_SLOT] and not rec2["drawn"]


def test_typeset_skips_region_when_erase_could_not_find_kept_lines():
    rec = _compose(_meta([], keep_paragraphs=True, keep_failed=True), "번역", "原文")
    assert rec["status"] == [tc.STATUS_KEEP_FALLBACK] and not rec["drawn"]


# ---------------------------------------------------------------- 원문 확인 화면
def test_source_review_shows_partial_keep(tmp_path, sentence_mode, monkeypatch):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from app.qt import source_review as sr

    QApplication.instance() or QApplication([])
    info, fd, host, extra = _kept_project(tmp_path, "ksr")
    service.keep_cell_original(info.dir, host, True)
    files = service.project_overview(info.dir)["groups"][0]["files"]
    dlg = sr.SourceReviewDialog(files, only_check=False)
    row = next(i for i, c in enumerate(dlg.cells) if c["page"] == 1)
    dlg.table.setCurrentCell(row, 0)
    dlg._show()
    assert "일부 문단 원본 그대로" in dlg.table.item(row, 2).text()
    assert "아이콘이 있는 문단 1개" in dlg.info.text()
    assert dlg.keep_btn.isChecked()
    dlg.close()


# ---------------------------------------------------------------- 그림 카드로 지우기·한글 넣기 (끝에서 끝까지)
def _raster_card(path):
    """글 줄 하나 + 아래 아이콘 줄(빨간 동그라미)이 그림으로만 있는 카드 (텍스트 레이어 없음)."""
    v = pymupdf.open()
    pg = v.new_page(width=250, height=180)
    pg.insert_text((40, 100), "ABCDEF GHIJKL", fontsize=12)
    pg.draw_circle((65, 125), 6, color=(0.85, 0.1, 0.1), fill=(0.85, 0.1, 0.1))
    pix = pg.get_pixmap(dpi=300)
    doc = pymupdf.open()
    page = doc.new_page(width=250, height=180)
    page.insert_image(page.rect, pixmap=pix, keep_proportion=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)


def test_raster_card_keeps_icon_line_and_typesets_text_line_in_place(tmp_path):
    from app import workspace
    from app.service import _erase_compose
    from app.service._translate_erase import _compose_with_cards

    pdf = tmp_path / "src" / "cards.pdf"
    _raster_card(pdf)
    regs = tmp_path / "regions.json"
    regs.write_text(json.dumps({"regions": [{"name": "본문", "kind": "text", "style": {"font_mode": "sentence"},
                                             "rect": {"x": 0.1, "y": 0.45, "w": 0.8, "h": 0.35}}]}), encoding="utf-8")
    info, _ = workspace.create_project("raster", "G", root=tmp_path)
    workspace.add_files(info, [pdf], "카드", new_group=True, regions_from=regs)
    fd = workspace.file_dirs(info)[0]
    (fd / "extract" / "pages").mkdir(parents=True)
    (fd / "extract" / "pages" / "page-001.json").write_text(json.dumps({"page": 1, "model": "m", "regions": {
        "본문": {"source": "ABCDEF GHIJKL\n[[?]]", "uncertain": False, "note": ""}}}), encoding="utf-8")
    assert extract.keep_paragraph_lines("ABCDEF GHIJKL\n[[?]]") == [1]
    service.set_keep_paragraphs(fd, 1, "본문", [1], [[58.0, 118.0, 72.0, 132.0]])
    r = _erase_compose.erase(fd, "1", workers=1)
    assert r.ok, r.error
    meta = json.loads((fd / "cleared" / "meta" / "page-001.json").read_text(encoding="utf-8"))
    tl = next(x for x in meta["regions"] if x["name"] == "본문")["text_layout"]
    assert tl.get("keep_paragraphs") and not tl.get("keep_failed")
    with pymupdf.open(fd / "cleared" / "cleared.pdf") as d:
        px = d[0].get_pixmap(dpi=100)
    img = np.frombuffer(px.samples, np.uint8).reshape(px.height, px.width, px.n)[:, :, :3]
    k = 100 / 72
    icon = img[int(121 * k):int(129 * k), int(61 * k):int(69 * k)]
    assert (icon[..., 0].astype(int) - icon[..., 2].astype(int)).mean() > 80  # 아이콘(빨강)은 그대로
    text = img[int(91 * k):int(100 * k), int(40 * k):int(130 * k)]
    assert text.min() > 150  # 글 줄은 지워졌다
    src = extract.translate_source(extract.load_extracted(fd)[1]["본문"])
    assert src == "ABCDEF GHIJKL"
    res = _compose_with_cards(fd, {1: {"page": 1, "본문": {"source": src, "ko": "가나다라 마바사"}}}, None, None)
    assert res.state == "done"
    rep = json.loads((fd / "translated" / "report.json").read_text(encoding="utf-8"))
    rec = next(p for p in rep["pages_detail"] if p["page"] == 1)["regions"]["본문"]
    boxes = rec["line_boxes_pt"]
    assert len(boxes) == 1 and boxes[0][3] < 110  # 한글은 글 줄 자리에 (아이콘 줄 자리로 내려가지 않음)


# ---------------------------------------------------------------- 리뷰 고침 (1.21.3)
def test_keep_state_broken_when_kept_rows_shift_with_same_line_count():
    """줄 수는 같은데 한 문단에 줄을 더하고 다른 문단에서 빼 남길 줄 번호가 다른 줄을 가리키면 broken (칸 전체 원본 그대로).
    지문은 아이콘 토큰·공백·숫자를 빼고 재므로 확정으로 [[?]]가 빠지거나 숫자를 아이콘에 넣어도 그대로 partial."""
    src = "効果を得る。\n次へ進む。\n2[[?]] [[?]]\n最後の文。"
    kp = {"lines": [2], "n": 4, "rows": [extract.keep_row_print("2[[?]] [[?]]")]}
    cell = {"source": src, extract.KEEP_PARAGRAPHS: kp}
    assert extract.keep_state(cell) == ("partial", [2])
    assert extract.keep_state({**cell, "source": "効果を得る。\n次へ進む。\n\n最後の文。"}) == ("partial", [2])
    moved = {**cell, "source": "効果を得る。\n次へ進む。\nさらに一行。\n2[[?]] [[?]]"}
    assert extract.keep_state(moved)[0] == "broken" and extract.keep_whole(moved)
    # 지문이 없는 예전 기록은 줄 수만 본다
    assert extract.keep_state({"source": moved["source"], extract.KEEP_PARAGRAPHS: {"lines": [2], "n": 4}})[0] == "partial"


def test_set_keep_paragraphs_stores_print_and_keeps_review_flags(tmp_path):
    """아이콘 문단만 원본 그대로: 남은 문단은 번역하므로 ‘확인 필요’·확인한 사람을 바꾸지 않는다. 남길 줄 지문을 적는다."""
    info, fd = _project(tmp_path, "kflag", pages=1)
    for p in (fd / "extract" / "pages" / "page-001.json", fd / "extract" / "page-001.json"):
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            d["regions"]["본문"].update(source=SRC, uncertain=True)
            d["regions"]["본문"].pop("checked_by", None)
            p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    service.set_keep_paragraphs(fd, 1, "본문", [1])
    cell = _cell(fd)
    assert cell["uncertain"] is True and "checked_by" not in cell
    assert cell[extract.KEEP_PARAGRAPHS]["rows"] == [""]  # 아이콘만 있는 줄: 토큰을 빼면 빈 글
    # 남길 줄이 글 줄 전부(칸 전체와 같음): 원문 확인에는 ‘원본 그대로’로 보인다
    service.set_keep_paragraphs(fd, 1, "본문", [0, 1])
    c = next(x for x in service.extracted_cells(fd) if x["page"] == 1)
    assert c["keep_mode"] == "whole" and c["keep_original"]


def test_keep_boxes_skip_slots_already_not_icon(tmp_path, sentence_mode):
    """이 칸을 원본 그대로로 정하기 전에 이미 ‘아이콘 아님’으로 정한 자리(글 줄의 잘못 찾은 자리일 수 있음)는
    남길 줄 자리(boxes_pt)에 넣지 않는다."""
    info, fd, host, extra = _kept_project(tmp_path, "kbox")
    service.set_icon_not_icon(info.dir, host)
    r = service.keep_cell_original(info.dir, extra, True)
    assert r["mode"] == "partial" and r["not_icon"] == [extra]
    assert not _cell(fd)[extract.KEEP_PARAGRAPHS].get("boxes_pt")


def test_set_cell_keep_original_off_restores_slots(tmp_path, sentence_mode):
    """결과 보기·원문 확인에서 끄면 아이콘 문단만 원본 그대로도 풀리고, 원본 그대로 때문에 ‘아이콘 아님’이 된 자리도
    다시 아이콘 자리로 돌아온다 (카드 보기에서 끈 것과 같게)."""
    info, fd, host, extra = _kept_project(tmp_path, "koff2")
    service.keep_cell_original(info.dir, host, True)
    r = service.set_cell_keep_original(fd, 1, "본문", False)
    assert set(r["restored"]) == {host, extra}
    cell = _cell(fd)
    assert extract.KEEP_PARAGRAPHS not in cell and not cell.get("keep_original")
    states = {it["occ"]: it["state"] for it in service.icon_place(info.dir, host)["items"]}
    assert "not_icon" not in states.values()
    # 켜는 것은 칸 전체 (예전 set_keep_original과 같음)
    assert service.set_cell_keep_original(fd, 1, "본문", True) == {"restored": [], "left": []}
    assert _cell(fd)["keep_original"]


def test_match_keeping_marks_only_kept_paragraph_lines_keep_para():
    """남은 글의 아이콘만 있는 줄도 keep(지우지 않음)이지만, 한글 넣기가 빼는 것은 원본 그대로 둔 문단 줄(keep_para)뿐."""
    m = np.zeros((90, 300), bool)
    for y0, y1 in ((10, 24), (34, 48), (60, 74)):
        m[y0:y1, 10:280] = True
    tm = _tm(m)
    lay = {"lines": [_line(10, 24), _line(34, 48, 10, 60), _line(60, 74)]}
    out = erase_text.match_source_lines(tm, lay, "いずれかを選択し実行する\n[[x]]\nコスト[[y]]を払う", keep_idx=[2])
    flags = [(bool(ln.get("keep")), bool(ln.get("keep_para"))) for ln in out["lines"]]
    assert flags == [(False, False), (True, False), (True, True)]


def test_compose_keeps_remaining_icon_line_slot_with_partial_keep():
    """아이콘 문단만 원본 그대로인 칸에서 남은 글의 아이콘만 있는 줄은 줄 자리에 남는다 (빼면 줄 수가 번역과 어긋난다)."""
    icon_line = _ln(20, 30, 40)
    icon_line["keep"] = True  # 남은 글의 아이콘 줄 (keep_para 없음)
    meta = _meta([_ln(20, 20, 160), icon_line, _ln(20, 45, 120, keep=True)], keep_paragraphs=True)
    rec = _compose(meta, "하나를 골라 실행한다\n[[x]]", "いずれかを選択し実行\n[[x]]")
    assert rec["source_ink"]["layout_lines"] == 2
    assert rec["line_boxes_pt"][0][1] == pytest.approx(20, abs=1.5)
    assert all(b[3] < 45 for b in rec["line_boxes_pt"])


def test_source_review_turns_off_partial_keep_and_restores_slots(tmp_path, sentence_mode):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from app.qt import source_review as sr

    QApplication.instance() or QApplication([])
    info, fd, host, extra = _kept_project(tmp_path, "ksr2")
    service.keep_cell_original(info.dir, host, True)
    files = service.project_overview(info.dir)["groups"][0]["files"]
    dlg = sr.SourceReviewDialog(files, only_check=False)
    row = next(i for i, c in enumerate(dlg.cells) if c["page"] == 1)
    dlg.table.setCurrentCell(row, 0)
    dlg._show()
    dlg.keep_btn.click()
    assert extract.KEEP_PARAGRAPHS not in _cell(fd) and not _cell(fd).get("keep_original")
    states = {it["occ"]: it["state"] for it in service.icon_place(info.dir, host)["items"]}
    assert "not_icon" not in states.values()
    dlg.close()
