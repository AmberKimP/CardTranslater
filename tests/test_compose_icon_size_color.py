"""그림 카드 한글 넣기 결함 (공주와기사 3·5쪽, 4쪽): 여러 아이콘을 합친 세트 아이콘이 원본보다 작게 그려짐,
지우기가 찾지 못한 아이콘 색이 <c1> 색으로 잡혀 빨간 강조 글이 파랗게 그려짐."""

from __future__ import annotations

import numpy as np
import pymupdf

from app.erase import inline_icon_box_ok
from app.typeset_compose import compose_layout_region, order_tag_accents, with_guessed_icon_scales

FONT = "fonts/notosanskr/NotoSansKR-Medium.ttf"


def _ln(y, x0=20.0, x1=150.0, gh=8.0, accents=None, color="#545358"):
    return {"bbox_pt": [x0, y, x1, y + gh], "glyph_h_pt": gh, "color": color, "stroke_color": None,
            "stroke_pt": 0.0, "weight": 0.2, "bold": False, "align": "left", "accents": accents or []}


def _char_colors(page) -> dict:
    colors = {}
    for b in page.get_text("rawdict")["blocks"]:
        for line in b.get("lines", []):
            for s in line["spans"]:
                for ch in s["chars"]:
                    colors.setdefault(ch["c"], s["color"])
    return colors


# ---------------------------------------------------------------- 세트 아이콘 크기 (지우기)


def test_wide_set_icon_box_kept_when_dictionary_picture_is_wide():
    # 3쪽 문·광석·궁전: 118×23px, 글자 높이 9px -> 한도 1.5 × 3 × 9 = 40.5px
    lim = 40.5
    wide_tpl = np.zeros((40, 200, 4), np.uint8)  # 가로세로 비 5
    assert inline_icon_box_ok((32, 27, 150, 50), lim, wide_tpl)
    assert not inline_icon_box_ok((32, 27, 150, 50), lim, None)  # 사전 그림을 모르면 예전처럼 폭으로도 막는다
    assert not inline_icon_box_ok((32, 27, 150, 50), lim, np.zeros((40, 40, 4), np.uint8))
    # 아이콘 하나짜리 조금 넓은 그림(도시 1.84)은 예전대로 폭으로 막는다 (6쪽: 이름표 붙은 큰 아이콘)
    assert not inline_icon_box_ok((50, 0, 116, 36), lim, np.zeros((88, 162, 4), np.uint8))
    assert inline_icon_box_ok((29, 34, 79, 54), lim, np.zeros((80, 201, 4), np.uint8))  # 5쪽 네잎 문양 3개 (2.51)
    # 높이가 한도를 넘으면 가로로 긴 그림이어도 버린다 (그림에 속은 큰 상자)
    assert not inline_icon_box_ok((0, 0, 150, 60), lim, wide_tpl)
    # 세로로 긴 사전 그림: 폭 한도를 줄이지 않는다
    assert inline_icon_box_ok((0, 0, 30, 30), lim, np.zeros((60, 20, 4), np.uint8))


# ---------------------------------------------------------------- 세트 아이콘 크기 (한글 넣기)


def test_unmeasured_icon_takes_median_of_measured_icons():
    fit = {"crown": 2.444, "rainbow_gem_4": 2.556}
    text = "[[gate_ore_palace]] 중 하나를 선택하고\n[[crown]]을 되돌린 수만큼 [[rainbow_gem_4]]을"
    out = with_guessed_icon_scales(fit, text, {"gate_ore_palace": 1.1, "crown": 1.1})
    assert abs(out["gate_ore_palace"] - 2.5) < 1e-9
    assert out["crown"] == 2.444 and out["rainbow_gem_4"] == 2.556  # 잰 값은 그대로
    # 사전에서 크기를 따로 정한 아이콘은 사전 크기를 따른다
    assert "gate_ore_palace" not in with_guessed_icon_scales(fit, text, {"gate_ore_palace": 2.0})
    # 잰 아이콘이 없으면 짐작하지 않는다
    assert with_guessed_icon_scales({}, text, {}) == {}
    assert with_guessed_icon_scales(fit, "아이콘 없음", {}) == fit


def _png(path, w, h, rgb):
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h), 1)
    pix.set_rect(pix.irect, (*rgb, 255))
    pix.save(str(path))


def test_unmeasured_wide_icon_drawn_at_card_icon_size(tmp_path):
    """한 칸에 잰 아이콘(왕관)이 있으면 재지 못한 세트 아이콘도 그 크기(글자 높이 대비)로 그린다."""
    _png(tmp_path / "set3.png", 150, 30, (200, 120, 40))
    _png(tmp_path / "crown.png", 30, 30, (230, 180, 40))
    doc = pymupdf.open()
    page = doc.new_page(width=220, height=90)
    lay = {"lines": [_ln(20, 20, 200, gh=5.0), _ln(40, 20, 200, gh=5.0)], "dividers_pt": [],
           "icons": [{"name": "crown", "bbox_pt": [60.0, 38.0, 72.0, 50.0], "scale": 2.4}]}
    style = {"icons_dir": str(tmp_path), "icon_scales": {"set3": 1.1, "crown": 1.1}}
    rec = compose_layout_region(page, (10, 10, 210, 80), "[[set3]] 중 하나\n[[crown]]을 되돌린다", style, lay)
    assert not rec["errors"]
    hs = sorted(round(i["bbox"][3] - i["bbox"][1], 2) for i in page.get_image_info())
    assert len(hs) == 2
    assert abs(hs[0] - hs[1]) < 0.05 * hs[1]  # 세트 아이콘이 왕관과 같은 높이 (예전: 사전 기본 1.3배로 절반쯤)


# ---------------------------------------------------------------- <c1> 색


def test_tag_accent_moved_first_when_unfound_icon_color_is_left_of_tag():
    # 4쪽 둘째 줄 (지우기 결과 그대로): 파란 토큰 아이콘(찾지 못함)의 색이 맨 앞, 빨간 （最大14個）가 뒤
    ln = _ln(204.0, 15.84, 110.4, gh=4.08, color="#4b4a4e", accents=[
        {"color": "#5c78b8", "x_pt": [[46.56, 51.36]]},
        {"color": "#af6371", "x_pt": [[80.16, 81.6], [88.32, 94.08], [95.52, 96.96], [98.88, 101.76], [108.96, 110.4]]},
        {"color": "#e66374", "x_pt": [[82.56, 87.36], [102.24, 107.52]]},
    ])
    src = "倍の数の[[blue_round_token]]を得る.<c1>(最大14個)</c1>"
    out = order_tag_accents([ln], [src])
    assert out[0]["accents"][0]["color"] == "#e66374"  # 태그 안을 덮는 빨강 중 짙은 쪽
    assert {a["color"] for a in out[0]["accents"]} == {"#5c78b8", "#af6371", "#e66374"}
    # 원문 줄을 모르거나 색 태그가 없으면 그대로
    assert order_tag_accents([ln], None)[0] is ln
    assert order_tag_accents([ln], ["倍の数の[[blue_round_token]]を得る."])[0] is ln


def test_tag_accent_order_kept_when_first_accent_already_matches():
    acc = [{"color": "#d02020", "x_pt": [[100.0, 140.0]]}, {"color": "#2050e0", "x_pt": [[30.0, 36.0]]}]
    ln = _ln(30, accents=acc)
    assert order_tag_accents([ln], ["得る。<c1>（最大14個）</c1>"])[0] is ln
    # swap_line_accents가 줄 색과 맞바꾼 줄(맨 앞 강조색에 자리가 없음)은 건드리지 않는다
    sw = _ln(30, accents=[{"color": "#e96071", "x_pt": []}, {"color": "#2050e0", "x_pt": [[100.0, 140.0]]}])
    assert order_tag_accents([sw], ["得る。<c1>（最大14個）</c1>"])[0] is sw


def test_c1_red_when_icon_was_not_found_by_eraser():
    doc = pymupdf.open()
    page = doc.new_page(width=180, height=80)
    acc = [{"color": "#2050e0", "x_pt": [[50.0, 56.0]]}, {"color": "#d02020", "x_pt": [[100.0, 140.0]]}]
    lay = {"lines": [_ln(30, accents=acc)], "dividers_pt": [], "icons": []}  # 아이콘 자리를 모름
    src = "倍の数の得る。<c1>（最大14個）</c1>"
    rec = compose_layout_region(page, (10, 10, 170, 70), "두 배만큼 얻는다. <c1>(최대 14개)</c1>", {}, lay, source_text=src)
    assert not rec["errors"]
    colors = _char_colors(page)
    assert colors["최"] == 0xD02020  # 예전: 파랑 (0x2050E0)
    assert colors["얻"] == 0x545358
