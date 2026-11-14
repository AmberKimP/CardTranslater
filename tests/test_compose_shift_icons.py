"""그림 카드 한글 넣기: 짧아진 한글 글 덩어리 옮기기, 줄 앞 아이콘, 줄 수가 바뀐 번역, 그림을 잰 글자 크기,
사전 그림으로 아이콘 자리 다시 잡기, 그림 배경 글자 극성 고르기 (1.21.3)."""

from __future__ import annotations

import numpy as np
import pymupdf
import pytest

from app import erase_picture, icons, typeset_compose as tc
from app.typeset_compose import compose_layout_region, sentence_blocks


def _ln(x0, y, x1, gh=4.32, align="left", src=None):
    d = {"bbox_pt": [x0, y, x1, y + gh], "glyph_h_pt": gh, "color": "#505050", "stroke_color": None,
         "stroke_pt": 0.0, "weight": 0.2, "bold": False, "align": align, "accents": []}
    if src is not None:
        d["_src"] = src
    return d


def _page(w=180, h=80):
    doc = pymupdf.open()
    return doc, doc.new_page(width=w, height=h)


# ---------------------------------------------------------------- 1. 짧아진 한글을 원문 글 칸 가운데 쪽으로
def test_block_center_shift_half_of_free_space_with_limits():
    assert tc.block_center_shift(160.0, [120.0, 100.0], room=50.0, gh=4.0) == pytest.approx(20.0)
    assert tc.block_center_shift(160.0, [120.0], room=5.0, gh=4.0) == pytest.approx(5.0)  # 줄 자리 끝까지만
    assert tc.block_center_shift(160.0, [159.5], room=50.0, gh=4.0) == 0.0  # 거의 꽉 참: 옮기지 않음
    assert tc.block_center_shift(160.0, [170.0], room=50.0, gh=4.0) == 0.0  # 한글이 더 길다


def test_short_korean_lines_move_together_toward_column_center():
    """원문 두 줄이 칸(20~160)을 꽉 채운 왼쪽 정렬, 한글은 짧다: 두 줄이 같은 만큼 오른쪽으로 옮겨져 시작점이 같고,
    가장 긴 줄의 가운데가 원문 글 칸 가운데(90) 근처."""
    lay = {"lines": [_ln(20, 20, 160), _ln(20, 30, 158)], "dividers_pt": [], "icons": []}
    _doc, page = _page()
    rec = compose_layout_region(page, (10, 10, 170, 70), "짧은 한글 줄이다.\n더 짧은 줄.", {}, lay)
    b = rec["line_boxes_pt"]
    assert b[0][0] == pytest.approx(b[1][0])
    assert b[0][0] > 25
    widest = max(b, key=lambda r: r[2] - r[0])
    assert (widest[0] + widest[2]) / 2 == pytest.approx(90, abs=1.0)


def test_explicit_left_alignment_and_block_center_off_do_not_move():
    lay = {"lines": [_ln(20, 20, 160), _ln(20, 30, 158)], "dividers_pt": [], "icons": []}
    for style in ({"align": "left"}, {"block_center": False}):
        _doc, page = _page()
        rec = compose_layout_region(page, (10, 10, 170, 70), "짧은 한글 줄이다.\n더 짧은 줄.", style, lay)
        assert rec["line_boxes_pt"][0][0] == pytest.approx(20, abs=0.3)


def test_no_shift_when_a_line_is_pinned_after_unerased_picture():
    """둘째 줄이 들여 쓰여(줄 앞에 지우지 않은 그림) 그 자리에서 시작하면 글 덩어리를 옮기지 않는다."""
    lay = {"lines": [_ln(20, 20, 160), _ln(32, 30, 158), _ln(20, 40, 150)], "dividers_pt": [], "icons": []}
    _doc, page = _page()
    rec = compose_layout_region(page, (10, 10, 170, 70), "첫 줄은\n둘째 줄\n셋째 줄이다.", {}, lay)
    x0s = [b[0] for b in rec["line_boxes_pt"]]
    assert x0s[0] == pytest.approx(20, abs=0.3) and x0s[2] == pytest.approx(20, abs=0.3)
    assert x0s[1] == pytest.approx(32 + tc.PIN_GAP * 4.32, abs=0.3)


# ---------------------------------------------------------------- 6쪽: 머리 줄·양쪽 맞춤 줄
def test_colon_line_is_its_own_sentence_and_full_lines_are_left():
    """‘…일 때:’ 머리 줄은 다음 줄과 한 문장으로 정렬하지 않는다. 칸을 꽉 채운 두 줄은 줄 가운데가 우연히 더 고르게
    맞아도 가운데 정렬로 보지 않는다 (가운데로 가면 줄 앞에 남은 아이콘과 글이 떨어진다)."""
    head = _ln(85.4, 0, 123.8, align="left", src="[[x]]のとき：")
    a = _ln(37.2, 10, 155.0, align="center", src="を得る。")
    b = _ln(30.2, 20, 158.6, align="left", src="なら，")
    blocks = sentence_blocks([head, a, b], ["일 때:", "을 얻는다. 해방되어 있다면", "얻어도 된다."])
    assert id(head) not in blocks
    assert blocks[id(a)]["align"] == "left" and blocks[id(a)] is blocks[id(b)]
    # 가장 왼쪽 줄 둘레에 모인 시작점: 들여 쓴 둘째 줄(37.2)은 섞지 않는다
    assert blocks[id(a)]["x0"] == pytest.approx(30.2)


def test_polluted_line_start_follows_region_margin():
    """마지막 줄 상자에 카드 왼쪽 그림 조각이 붙어 16pt부터 시작: 다른 줄이 여백(49pt)에 있으면 여백에 맞춘다
    (묶음이 둘로 나뉘어도 여백은 영역 전체 줄로 잰다)."""
    lines = [_ln(49.4, 0, 156), _ln(50.4, 10, 152), _ln(49.0, 20, 159), _ln(16.1, 30, 158.6)]
    blocks = sentence_blocks(lines[2:], ["한도가", "증가한다."], all_lines=lines)
    assert blocks[id(lines[3])]["x0"] == pytest.approx(49.2)  # 여백 = 왼쪽 정렬 줄 시작점의 가운데 값
    assert tc.text_left_edge(lines) == pytest.approx(49.0)


# ---------------------------------------------------------------- 줄 앞 아이콘
def test_with_edge_icons_extends_line_box_to_leading_icon():
    lines = [_ln(41.3, 188.4, 165.1), _ln(19.0, 197.8, 116.4)]
    out = tc.with_edge_icons(lines, [{"name": "a", "bbox_pt": [18.0, 186.5, 41.0, 195.6]}])
    assert out[0]["bbox_pt"][0] == pytest.approx(18.0) and out[0]["_lead_icon"]
    assert out[1]["bbox_pt"][0] == pytest.approx(19.0)
    assert lines[0]["bbox_pt"][0] == pytest.approx(41.3)  # 원래 목록은 그대로
    far = tc.with_edge_icons(lines, [{"name": "a", "bbox_pt": [90.0, 186.5, 100.0, 195.6]}])
    assert far[0]["bbox_pt"][0] == pytest.approx(41.3)  # 줄 가운데 아이콘은 줄 앞이 아니다


def _icon_pages(erased: bool):
    """원본 쪽에는 줄 앞(20~36pt)에 검은 아이콘, 지운 쪽은 erased면 그 자리를 흰색으로."""
    sdoc, edoc = pymupdf.open(), pymupdf.open()
    sp, ep = sdoc.new_page(width=180, height=80), edoc.new_page(width=180, height=80)
    for p in (sp, ep):
        p.draw_rect(pymupdf.Rect(20, 20, 36, 26), color=(0, 0, 0), fill=(0, 0, 0))
        p.draw_rect(pymupdf.Rect(24, 21, 28, 25), color=(1, 1, 1), fill=(1, 1, 1))
    if erased:
        ep.draw_rect(pymupdf.Rect(18, 18, 38, 28), color=(1, 1, 1), fill=(1, 1, 1))
    return (sdoc, sp), (edoc, ep)


@pytest.mark.parametrize("erased, expect", [(False, True), (True, False)])
def test_kept_lead_icon_compares_source_and_erased(erased, expect):
    (_s, sp), (_e, ep) = _icon_pages(erased)
    ln = _ln(40.0, 20.5, 160.0, src="[[fleur]]の山札を見て")
    assert tc.kept_lead_icon(ep, sp, ln, "[[fleur]]의 덱을 보고", [], margin=19.0) is expect
    # 한글 줄이 아이콘으로 시작하지 않거나, 줄이 여백에 붙어 있으면 보지 않는다
    assert tc.kept_lead_icon(ep, sp, ln, "덱을 보고", [], margin=19.0) is False
    assert tc.kept_lead_icon(ep, sp, ln, "[[fleur]]의 덱을", [], margin=39.0) is False


def test_compose_does_not_redraw_kept_leading_icon():
    """원문 줄 앞 아이콘을 지우지 못했으면 한글 줄 앞 아이콘을 다시 그리지 않고 원문 첫 글자 자리에서 시작한다."""
    (_s, sp), (_e, ep) = _icon_pages(False)
    lay = {"lines": [_ln(40.0, 20.5, 160.0), _ln(19.0, 30.5, 120.0)], "dividers_pt": [], "icons": []}
    rec = compose_layout_region(ep, (10, 10, 170, 70), "[[fleur]]의 덱을 보고\n추가한다.", {}, lay,
                                source_text="[[fleur]]の山札を見て\n加える。", src_page=sp)
    assert rec["lines"][0] == "의 덱을 보고"
    assert rec["line_boxes_pt"][0][0] == pytest.approx(40.0 + tc.PIN_GAP * 4.32, abs=0.3)


# ---------------------------------------------------------------- 8쪽: 번역 줄 수를 바꿔 고친 카드
def test_gap_cuts_finds_wide_gap_between_lines():
    lines = [_ln(50, 183.8, 160), _ln(50, 191.8, 156), _ln(50, 200.2, 152), _ln(49, 212.9, 159), _ln(49, 221.8, 158)]
    cuts = tc.gap_cuts(lines)
    assert len(cuts) == 1 and 204.5 < cuts[0] < 212.9
    assert tc.gap_cuts(lines[:2]) == []


def test_fewer_translation_lines_keep_lower_paragraph_slots():
    """원문 5줄(셋째·넷째 줄 사이가 넓음), 번역 4줄: 문장 끝에서 나눠 아래 두 줄은 원문 자리에, 위 두 줄은 위 묶음
    안에서. 왼쪽 시작점은 혼자 왼쪽으로 튀어나온 줄(16pt)이 아니라 여백(49pt)."""
    lay = {"lines": [_ln(50.2, 20, 162, align="right"), _ln(49.4, 28, 156), _ln(50.4, 36.4, 152),
                     _ln(49.0, 49.1, 159), _ln(16.1, 58.0, 158.6)], "dividers_pt": [], "icons": []}
    _doc, page = _page(h=90)
    ko = "게임에서 사용하지 않은 카드 중\n1장을 선택하고, 비용을 모두 경감하여 플레이할 수 있다.\n카드의 한도가 늘어난다.\n수만큼 증가한다."
    rec = compose_layout_region(page, (10, 10, 170, 80), ko, {}, lay)
    assert rec["line_modes"] == ["paragraph", "slot", "slot"]
    boxes = rec["line_boxes_pt"]
    assert min(b[0] for b in boxes) > 45
    assert boxes[-1][1] == pytest.approx(58.0, abs=1.0)  # 마지막 줄은 원문 마지막 줄 높이에
    assert boxes[-2][1] == pytest.approx(49.1, abs=1.0)


# ---------------------------------------------------------------- 1쪽: 그림을 글자로 잰 크기
def test_typical_glyph_heights_from_other_cards():
    def meta(*hs):
        return {"regions": [{"name": "본문", "text_layout": {"lines": [{"glyph_h_pt": h} for h in hs]}}]}
    typ = tc.typical_glyph_heights([meta(4.32, 4.32), meta(4.08, 4.32), meta(2.16, 1.2), meta()])
    assert typ == {"본문": pytest.approx(np.median([4.32, 4.2]))}  # 쪽마다 가운데 값의 가운데 값 (잡음 줄 쪽은 뺌)
    assert tc.typical_glyph_heights([meta(4.3)]) == {}  # 한 장뿐이면 모른다


def test_whole_region_fallback_uses_typical_height_when_ink_is_a_picture():
    """줄 모양을 못 쓴 카드에서 원문 잉크로 잰 ‘글자 높이’가 큰 그림이면 다른 카드의 글자 높이로 넣는다."""
    sdoc, edoc = pymupdf.open(), pymupdf.open()
    sp, ep = sdoc.new_page(width=180, height=100), edoc.new_page(width=180, height=100)
    sp.draw_rect(pymupdf.Rect(20, 30, 70, 80), color=(0, 0, 0), fill=(0.1, 0.1, 0.1))  # 큰 그림
    meta = {"regions": [{"name": "본문", "kind": "text", "rect_pt": [10, 10, 170, 90], "text_layer": {}}],
            tc.TYPICAL_KEY: {"본문": 4.3}}
    rec = tc.compose_page(ep, sp, meta, {"본문": {"ko": "다음 중 최대 2개를 선택하여 실행"}})["본문"]
    assert rec["size_pt"] < 8
    assert any("그림으로 봄" in w for w in rec["warnings"])


# ---------------------------------------------------------------- 지우기: 사전 그림으로 아이콘 자리 다시 잡기
def _tpl():
    t = np.full((20, 40, 4), 255, np.uint8)
    t[..., 3] = 255
    t[2:18, 2:12, :3] = (200, 30, 30)
    t[4:16, 15:25, :3] = (30, 30, 200)
    t[2:18, 28:38, :3] = (30, 160, 30)
    return t


def _band_img():
    img = np.full((120, 300, 3), 240, np.uint8)
    img[60:80, 10:50] = _tpl()[..., :3]  # 둘째 줄 앞에 진짜 아이콘
    img[5:25, 200:240] = (120, 120, 120)  # 위쪽 그림 조각
    return img


def test_match_in_band_finds_template_in_line_band():
    hits = icons.match_in_band(_band_img(), _tpl(), (62, 78), 16, n=1)
    assert len(hits) == 1
    x0, y0, x1, y1 = hits[0][1]
    assert abs(x0 - 10) <= 3 and abs(y0 - 60) <= 3 and abs(x1 - 50) <= 4


def test_refine_icon_boxes_relocates_wrong_box_and_drops_out_of_band():
    img = _band_img()
    text = "一行目の文\n[[fleur]]の山札を見て\n[[gone]]を得る"
    found = [("fleur", (200, 5, 240, 25), False), ("gone", (200, 5, 240, 25), False)]
    bands = [(30, 46, 16), (62, 78, 16), (95, 111, 16)]
    out, notes = icons.refine_icon_boxes(img, text, found, bands, {"fleur": _tpl()})
    by = {n: b for n, b, _o in out}
    assert abs(by["fleur"][0] - 10) <= 3 and abs(by["fleur"][1] - 60) <= 3
    assert "gone" not in by  # 사전 그림도 없고 찾은 자리가 줄 밖: 지우지 않는다
    assert notes
    # 줄을 원문에 못 맞췄으면 그대로
    assert icons.refine_icon_boxes(img, text, found, None, {"fleur": _tpl()})[0] == found


# ---------------------------------------------------------------- 지우기: 그림 배경 글자 극성
def test_picture_polarity_prefers_row_count_close_to_source(monkeypatch):
    """밝은 쪽(건물 창문 줄 7개)이 점수는 높아도 원문 글 줄이 2개면 줄 수가 가까운 어두운 쪽을 고른다."""
    H, W = 40, 60

    def fake(img, box, polarity, p):
        core = np.zeros((H, W), bool)
        core[10:14, 10:30] = True
        rows = [{"box": (10, 10 + 4 * i, 30, 13 + 4 * i), "core": core, "under_icon": False}
                for i in range(2 if polarity < 0 else 7)]
        return {"T": np.zeros((H, W), np.float32), "core": core, "guard": np.zeros((H, W), bool), "hg": 15.0,
                "rows": rows, "score": 2300.0 if polarity < 0 else 4200.0, "level": 100.0}

    monkeypatch.setattr(erase_picture, "_detect_polarity", fake)
    img = np.full((H, W, 3), 200, np.uint8)
    assert erase_picture.picture_text_mask(img, (0, 0, W, H), expected_rows=2).polarity == -1
    assert erase_picture.picture_text_mask(img, (0, 0, W, H)).polarity == 1  # 원문을 모르면 예전대로


# ---------------------------------------------------------------- 리뷰 고침 (1.21.3)
def test_refine_icon_boxes_skips_icon_only_line_entries():
    """아이콘만 있는 줄의 자리는 글 줄 몫으로 나누지 않는다: 글 줄의 잘못 잡은 자리(x=150)가 사전 그림 자리로 옮겨지고,
    아이콘 줄 자리는 그대로 (아이콘 줄 자리가 글 줄 자리로 바뀌어 글 속 아이콘이 둘 되던 것)."""
    img = np.full((120, 300, 3), 240, np.uint8)
    img[10:30, 10:50] = _tpl()[..., :3]  # 아이콘만 있는 줄
    img[60:80, 100:140] = _tpl()[..., :3]  # 글 줄 속 아이콘
    text = "[[a]]\n文字[[a]]です"
    found = [("a", (10, 10, 50, 30), True), ("a", (150, 60, 190, 80), False)]
    bands = [(12, 28, 16), (62, 78, 16)]
    out, _notes = icons.refine_icon_boxes(img, text, found, bands, {"a": _tpl()})
    assert out[0] == ("a", (10, 10, 50, 30), True)
    assert out[1][2] is False and abs(out[1][1][0] - 100) <= 3 and abs(out[1][1][1] - 60) <= 3


def test_refine_icon_boxes_far_hit_needs_higher_score(monkeypatch):
    """줄 배치 자리와 거의 겹치지 않는 자리는 BAND_MATCH_FAR 이상 닮아야 옮긴다 (무늬 있는 그림에 속지 않게)."""
    text = "一行目\n[[a]]の文"
    found = [("a", (10, 60, 30, 78), False)]
    bands = [(30, 46, 16), (62, 78, 16)]
    img = np.full((120, 300, 3), 240, np.uint8)
    monkeypatch.setattr(icons, "match_in_band", lambda *a, **k: [(0.65, (200, 60, 220, 78))])
    out, _ = icons.refine_icon_boxes(img, text, found, bands, {"a": _tpl()})
    assert out == found
    monkeypatch.setattr(icons, "match_in_band", lambda *a, **k: [(0.9, (200, 60, 220, 78))])
    out, _ = icons.refine_icon_boxes(img, text, found, bands, {"a": _tpl()})
    assert out[0][1] == (200, 60, 220, 78)
    monkeypatch.setattr(icons, "match_in_band", lambda *a, **k: [(0.65, (12, 60, 32, 78))])  # 가까우면 그대로 믿음
    out, _ = icons.refine_icon_boxes(img, text, found, bands, {"a": _tpl()})
    assert out[0][1] == (12, 60, 32, 78)


@pytest.mark.parametrize("rotate, calls", [(0, 1), (180, 0)])
def test_erase_skips_band_refinement_for_rotated_region(monkeypatch, rotate, calls):
    """거꾸로 인쇄된 영역은 사전 그림으로 아이콘 자리를 다시 잡지 않는다 (줄 띠와 원문 줄 순서가 반대라 맞는 자리를 버린다)."""
    from app import erase
    from app.regions import RatioRect, Region, RegionTemplate

    img = np.full((150, 360, 3), 245, np.uint8)
    img[40:52, 40:250] = 40
    img[62:74, 40:200] = 40
    doc = pymupdf.open()
    page = doc.new_page(width=360 * 0.48, height=150 * 0.48)
    page.insert_image(page.rect, pixmap=pymupdf.Pixmap(pymupdf.csRGB, 360, 150, img.tobytes(), 0))
    reg = Region(name="본문", kind="text", rect=RatioRect(20 / 360, 20 / 150, 320 / 360, 110 / 150),
                 style={"rotate": rotate} if rotate else {})
    seen = []
    real = icons.refine_icon_boxes
    monkeypatch.setattr(icons, "refine_icon_boxes", lambda *a, **k: (seen.append(1), real(*a, **k))[1])
    erase.erase_page(page, RegionTemplate(regions=[reg], protect=[]), seed=1,
                     sources={"본문": "ABCDEFGHIJKLMNOP[[a]]\nABCDEFGHIJ"}, icon_templates={"a": _tpl()})
    assert len(seen) == calls


def test_centered_similar_length_lines_are_not_forced_left():
    """길이가 비슷한(문장 폭의 JUSTIFIED_FULL배 넘는) 가운데 정렬 두 줄: 양쪽 끝이 모두 어긋나 있으면 양쪽 맞춤이 아니다."""
    a = _ln(30.0, 10, 150.0, align="center")
    b = _ln(36.0, 20, 144.0, align="center")
    blocks = sentence_blocks([a, b], ["가운데 정렬된 첫 줄", "둘째 줄이다."])
    assert blocks[id(a)]["align"] == "center"


def test_no_block_shift_when_region_has_kept_lines():
    """원본 그대로 남긴 줄(아이콘만 있는 줄·원본 그대로 둔 문단)이 있는 칸은 글 덩어리를 옮기지 않는다 (남긴 줄과 어긋난다)."""
    icon = _ln(20, 30, 40)
    icon["keep"] = True
    lay = {"lines": [_ln(20, 20, 160), icon, _ln(20, 40, 158)], "dividers_pt": [], "icons": []}
    _doc, page = _page()
    rec = compose_layout_region(page, (10, 10, 170, 70), "짧은 한글 줄이다.\n[[x]]\n더 짧은 줄.", {}, lay)
    assert rec["line_boxes_pt"][0][0] == pytest.approx(20, abs=0.3)
    lay2 = {"lines": [_ln(20, 20, 160), _ln(20, 30, 158)], "dividers_pt": [], "icons": [], "keep_paragraphs": True}
    _doc, page = _page()
    rec2 = compose_layout_region(page, (10, 10, 170, 70), "짧은 한글 줄이다.\n더 짧은 줄.", {}, lay2)
    assert rec2["line_boxes_pt"][0][0] == pytest.approx(20, abs=0.3)


def test_kept_lead_icon_false_when_same_icon_erased_elsewhere_in_line():
    """같은 이름의 아이콘을 이 줄 안 다른 자리에서 지웠다: 줄 앞 아이콘은 다시 그려야 한다."""
    (_s, sp), (_e, ep) = _icon_pages(False)
    ln = _ln(40.0, 20.5, 160.0, src="[[fleur]]の山札を見て")
    there = [{"name": "fleur", "bbox_pt": [100.0, 20.0, 110.0, 26.0]}]
    assert tc.kept_lead_icon(ep, sp, ln, "[[fleur]]의 덱을 보고", there, margin=19.0) is False
    other = [{"name": "coin", "bbox_pt": [100.0, 20.0, 110.0, 26.0]}]
    assert tc.kept_lead_icon(ep, sp, ln, "[[fleur]]의 덱을 보고", other, margin=19.0) is True
