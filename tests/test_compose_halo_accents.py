"""그림 카드 한글 넣기 결함 (공주와 기사 4쪽): 외곽선이 글자를 덮어 겹쳐 보임, 아이콘 색이 <c1> 색으로 잡힘,
두 줄 잇기가 다시 두 줄로 갈려 번역의 줄바꿈이 무너짐."""

from __future__ import annotations

import pymupdf

from app.typeset import TextStyle, typeset_region
from app.typeset_compose import compose_layout_region, stroke_width_ratio, text_accents

FONT = "fonts/notosanskr/NotoSansKR-Medium.ttf"


def _ln(y, x0=20.0, x1=150.0, gh=8.0, accents=None, stroke=None, stroke_pt=0.0):
    return {"bbox_pt": [x0, y, x1, y + gh], "glyph_h_pt": gh, "color": "#545358", "stroke_color": stroke,
            "stroke_pt": stroke_pt, "weight": 0.2, "bold": False, "align": "left", "accents": accents or []}


def _content(page) -> bytes:
    doc = page.parent
    return b"\n".join(doc.xref_stream(x) for x in page.get_contents())


# ---------------------------------------------------------------- 외곽선


def test_stroke_width_is_capped_by_glyph_height():
    # 옅은 빛 테두리를 배경까지 재어 글자 높이의 40%로 나온 값 (4쪽: stroke 1.771pt, 글자 높이 4.32pt)
    w = stroke_width_ratio(1.771, 4.32, 4.66)
    assert abs(w * 4.66 - 0.12 * 4.32) < 1e-6
    assert abs(stroke_width_ratio(0.3, 10.0, 10.0) - 0.03) < 1e-9  # 얇은 외곽선은 그대로
    assert abs(stroke_width_ratio(2.0, None, 10.0) - 0.2) < 1e-9  # 글자 높이를 모르면 그대로


def test_outline_drawn_under_all_fills_and_not_extracted_twice():
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=60)
    st = TextStyle(font_file=FONT, size_pt=8, color="#545358", stroke_color="#f6eff3", stroke_width=0.3)
    res = typeset_region(page, pymupdf.Rect(10, 10, 190, 40), "민심 보드에 있는", st)
    assert not res.overflow_text
    c = _content(page)
    halo = c.rfind(b".9647059 .9372549 .9529412 RG")  # 외곽선 색 (#f6eff3)
    fill = c.find(b".32941178 .3254902 .34509806 rg")  # 글자 색 (#545358)
    assert halo != -1 and fill != -1
    assert halo < fill  # 줄의 외곽선을 모두 그린 뒤에 글자를 채운다 (뒤 글자 외곽선이 앞 글자를 덮지 않게)
    # 외곽선 글자는 추출에서 빠진다: 한 번만 나온다
    assert "".join(page.get_text().split()) == "민심보드에있는"


def test_outline_layer_keeps_colors_on_render():
    """외곽선을 먼저 그려도 글자 가운데는 글자 색이다 (외곽선 색이 글자를 덮지 않음)."""
    doc = pymupdf.open()
    page = doc.new_page(width=120, height=60)
    st = TextStyle(font_file=FONT, size_pt=30, color="#000000", stroke_color="#ffffff", stroke_width=0.12)
    typeset_region(page, pymupdf.Rect(5, 5, 115, 55), "ㅡㅡ", st)
    pix = page.get_pixmap(dpi=72)
    # 두 글자 모두에 어두운 픽셀이 있다 (예전: 뒤 글자 외곽선이 앞 글자 오른쪽을 덮었다)
    w = pix.width
    dark_cols = {x for y in range(pix.height) for x in range(w) if pix.pixel(x, y)[0] < 60}
    assert dark_cols
    first, last = min(dark_cols), max(dark_cols)
    mid = (first + last) // 2
    left_dark = [x for x in dark_cols if x < mid]
    right_dark = [x for x in dark_cols if x > mid]
    assert len(left_dark) > 5 and len(right_dark) > 5


# ---------------------------------------------------------------- 아이콘 색은 <cN> 색이 아니다


def test_text_accents_drops_color_measured_inside_icon_box():
    ln = _ln(205.92, 16.8, 111.36, gh=4.8, accents=[
        {"color": "#6080b9", "x_pt": [[47.04, 52.8], [52.32, 53.28]]},  # 파란 구슬 아이콘 안
        {"color": "#b0616e", "x_pt": [[81.12, 82.56], [89.76, 95.04]]},  # 빨간 （최대 14개）
    ])
    icons = [{"name": "blue_orb", "bbox_pt": [46.08, 204.96, 60.48, 212.16], "scale": 1.765}]
    assert [a["color"] for a in text_accents(ln, icons)] == ["#b0616e"]
    assert len(text_accents(ln, [])) == 2  # 아이콘 정보가 없으면 그대로
    # 다른 줄(세로로 겹치지 않는) 아이콘은 상관없다
    far = [{"name": "x", "bbox_pt": [46.08, 100.0, 60.48, 110.0]}]
    assert len(text_accents(ln, far)) == 2


def test_c1_uses_red_text_color_not_icon_blue():
    doc = pymupdf.open()
    page = doc.new_page(width=180, height=80)
    acc = [{"color": "#2050e0", "x_pt": [[60.0, 66.0]]}, {"color": "#d02020", "x_pt": [[100.0, 140.0]]}]
    lay = {"lines": [_ln(30, accents=acc)], "dividers_pt": [],
           "icons": [{"name": "blue_orb", "bbox_pt": [58.0, 28.0, 68.0, 40.0], "scale": 1.3}]}
    src = "得る。<c1>（最大14個）</c1>"
    rec = compose_layout_region(page, (10, 10, 170, 70), "얻는다. <c1>(최대 14개)</c1>", {}, lay, source_text=src)
    assert not rec["errors"]
    colors = {}
    for b in page.get_text("rawdict")["blocks"]:
        for line in b.get("lines", []):
            for s in line["spans"]:
                for ch in s["chars"]:
                    colors.setdefault(ch["c"], s["color"])
    assert colors["최"] == 0xD02020  # 빨간 강조 글자는 빨강
    assert colors["얻"] == 0x545358


# ---------------------------------------------------------------- 두 줄 잇기


def test_join_two_rejected_when_joined_sentence_wraps_again():
    """이은 줄이 칸 폭을 넘어 다시 두 줄로 갈리면 번역의 줄바꿈을 그대로 쓴다 (넘침이 없어도)."""
    doc = pymupdf.open()
    page = doc.new_page(width=180, height=110)
    lay = {"lines": [_ln(20), _ln(32)], "dividers_pt": []}
    ko = "민심 보드에 있는 자신의 나무 큐브의\n두 배만큼 구슬을 얻는다."
    rec = compose_layout_region(page, (10, 10, 170, 100), ko, {}, lay)
    assert rec["line_modes"] == ["slot", "slot"]
    assert rec["lines"] == ["민심 보드에 있는 자신의 나무 큐브의", "두 배만큼 구슬을 얻는다."]
    assert not rec["warnings"]
