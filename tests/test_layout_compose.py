"""그림 카드 한글 넣기: 원문 줄 모양(erase_text.text_layout) -> 줄 자리마다 한 줄, 구분선 위아래 따로."""

from __future__ import annotations

import cv2
import numpy as np
import pymupdf

from app import erase_text
from app.glossary import Source, local_candidates
from app.typeset_compose import assign_lines, compose_layout_region, layout_sections


def _paper(h=220, w=360):
    rng = np.random.default_rng(0)
    img = np.full((h, w, 3), (235, 225, 205), np.float32)
    img += cv2.GaussianBlur(rng.standard_normal((h, w)).astype(np.float32), (0, 0), 2)[..., None] * 3
    return np.clip(img, 0, 255).astype(np.uint8)


def test_text_layout_lines_colors_divider_and_alignment():
    img = _paper()
    cv2.putText(img, "RED LINE", (40, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (210, 40, 30), 4, cv2.LINE_AA)
    cv2.putText(img, "+2", (150, 110), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (40, 40, 40), 4, cv2.LINE_AA)
    cv2.line(img, (40, 140), (320, 140), (40, 40, 40), 3)  # 구분선
    cv2.putText(img, "flavor", (40, 190), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (40, 40, 40), 2, cv2.LINE_AA)
    box = (20, 10, 340, 210)
    tm = erase_text.text_mask(img, box)
    lay = erase_text.text_layout(img, tm, box)
    assert len(lay["lines"]) == 3
    assert len(lay["dividers"]) == 1 and 135 <= lay["dividers"][0][1] <= 141
    red, mid, flav = lay["lines"]
    r, g, b = (int(red["color"][i:i + 2], 16) for i in (1, 3, 5))
    assert r > 150 and g < 100 and b < 100  # 빨간 줄
    assert int(mid["color"][1:3], 16) < 100  # 검은 줄
    assert mid["align"] == "center" and red["align"] == "left"
    assert red["glyph_h"] > flav["glyph_h"]  # 큰 글자 / 작은 글자
    assert red["stroke_color"] is None  # 종이 위 글자에는 테두리가 없다


def _glass(h=120, w=420, seed=1):
    """스테인드글라스 비슷한 그림: 색 조각 + 검은 납선 (test_erase_text와 같은 것)."""
    rng = np.random.default_rng(seed)
    pts = rng.integers(0, [w, h], size=(40, 2))
    yy, xx = np.mgrid[0:h, 0:w]
    d = (xx[..., None] - pts[:, 0]) ** 2 + (yy[..., None] - pts[:, 1]) ** 2
    cell = d.argmin(axis=2)
    pal = np.array([(235, 150, 40), (230, 200, 60), (220, 90, 50), (240, 120, 30)], np.uint8)
    img = pal[cell % len(pal)].copy()
    edge = (np.abs(np.diff(cell, axis=0, prepend=cell[:1])) + np.abs(np.diff(cell, axis=1, prepend=cell[:, :1]))) > 0
    img[cv2.dilate(edge.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)] = (40, 35, 35)
    return img


def test_text_layout_outlined_white_text_has_stroke():
    img = _glass()
    white = np.zeros(img.shape[:2], np.uint8)
    (tw, _th), _ = cv2.getTextSize("NAME", cv2.FONT_HERSHEY_SIMPLEX, 1.5, 6)
    cv2.putText(white, "NAME", (210 - tw // 2, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.5, 255, 6)  # 상자 가운데
    outline = cv2.dilate(white, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11)))  # 5px 검은 테두리
    img[outline > 0] = (30, 30, 30)
    img[white > 0] = (245, 245, 245)
    box = (40, 20, 380, 100)
    tm = erase_text.text_mask(img, box)
    lay = erase_text.text_layout(img, tm, box)
    assert len(lay["lines"]) == 1
    ln = lay["lines"][0]
    assert int(ln["color"][1:3], 16) > 200  # 흰 글자
    assert ln["stroke_color"] and int(ln["stroke_color"][1:3], 16) < 90 and ln["stroke_px"] > 1
    assert ln["align"] == "center"


def test_assign_lines_keeps_bottom_sections():
    assert assign_lines(["a", "b", "c"], [2, 1]) == [["a", "b"], ["c"]]
    # 번역 줄이 하나 적으면 아래 묶음(구분선 아래 문구)은 그대로, 위 묶음이 줄어든다
    assert assign_lines(["a", "b", "c"], [3, 1]) == [["a", "b"], ["c"]]
    assert assign_lines(["a", "b", "c", "d", "e"], [3, 1]) == [["a", "b", "c", "d"], ["e"]]


def _layout():
    def ln(y, color="#d03020", align="left", x0=20.0, x1=150.0):
        return {"bbox_pt": [x0, y, x1, y + 8], "glyph_h_pt": 8.0, "color": color, "stroke_color": None,
                "stroke_pt": 0.0, "weight": 0.2, "bold": True, "align": align, "accents": []}

    return {"lines": [ln(20), ln(32), ln(44, "#303030", "center", 60, 110), ln(80, "#303030", x1=120)],
            "dividers_pt": [[20, 64, 80, 65.5], [90, 64, 150, 65.5]]}


def test_layout_sections_split_at_divider():
    secs = layout_sections(_layout(), (10, 10, 170, 100))
    assert [len(s["lines"]) for s in secs] == [3, 1]
    assert secs[0]["y1"] == secs[1]["y0"] and 64 < secs[0]["y1"] < 66


def test_compose_layout_region_puts_lines_in_slots_and_keeps_divider_clear():
    doc = pymupdf.open()
    page = doc.new_page(width=180, height=110)
    rec = compose_layout_region(page, (10, 10, 170, 100), "첫 줄 한글\n둘째 줄\n+2 보너스\n아래 문구", {}, _layout())
    assert rec["line_modes"] == ["slot"] * 4 and not rec["overflow_text"]
    boxes = rec["line_boxes_pt"]
    assert len(boxes) == 4
    for b, y in zip(boxes, (20, 32, 44, 80)):
        assert abs((b[1] + b[3]) / 2 - (y + 4)) < 2.5  # 원문 줄 자리
    assert boxes[2][3] < 64 and boxes[3][1] > 66  # 구분선을 넘지 않는다
    assert abs((boxes[2][0] + boxes[2][2]) / 2 - 85) < 3  # 가운데 정렬 줄
    words = page.get_text("words")
    assert any(w[4] == "아래" for w in words)
    # 줄 수가 다르면 구분선 위 묶음 안에서 줄바꿈하고 아래 문구는 아래에 둔다
    page2 = doc.new_page(width=180, height=110)
    rec2 = compose_layout_region(page2, (10, 10, 170, 100), "합친 한 줄 번역\n아래 문구", {}, _layout())
    assert rec2["line_modes"] == ["paragraph", "slot"]
    assert rec2["line_boxes_pt"][-1][1] > 66 and all(b[3] < 64 for b in rec2["line_boxes_pt"][:-1])


def test_bracketed_words_always_glossary_candidates():
    c = local_candidates([Source("k", "본문", "text", "この精鋭には「召喚石」「<c1>魔冠</c1>」\n[[coin]] [炎] ［人族］")])
    assert c["召喚石"] and c["魔冠"] and c["炎"] == "분류어·태그" and c["人族"] == "분류어·태그"
    assert not any("coin" in k for k in c)


def test_compose_layout_region_upside_down_region():
    """180도 영역: 줄 자리는 페이지에서 잰 것을 돌린 틀로 옮겨 쓰고, 글자는 거꾸로 그린다."""
    doc = pymupdf.open()
    page = doc.new_page(width=180, height=110)
    lay = _layout()
    rect = pymupdf.Rect(10, 10, 170, 100)
    sx, sy = rect.x0 + rect.x1, rect.y0 + rect.y1
    # 페이지에서 본 모습 = 돌린 것 (원래 줄 자리를 중심 둘레로 180도)
    page_lay = {"lines": [{**ln, "bbox_pt": [sx - ln["bbox_pt"][2], sy - ln["bbox_pt"][3], sx - ln["bbox_pt"][0],
                                             sy - ln["bbox_pt"][1]],
                           "align": {"left": "right"}.get(ln["align"], ln["align"])} for ln in lay["lines"]],
                "dividers_pt": [[sx - d[2], sy - d[3], sx - d[0], sy - d[1]] for d in lay["dividers_pt"]]}
    rec = compose_layout_region(page, rect, "첫 줄 한글\n둘째 줄\n+2 보너스\n아래 문구", {"rotate": 180}, page_lay)
    assert rec["rotate"] == 180 and rec["line_modes"] == ["slot"] * 4 and not rec["overflow_text"]
    for b, y in zip(rec["line_boxes_pt"], (20, 32, 44, 80)):  # 틀 좌표에서는 원래 줄 자리
        assert abs((b[1] + b[3]) / 2 - (y + 4)) < 2.5
    words = [w for w in page.get_text("words") if w[4] == "아래"]
    assert words and words[0][1] < 50  # 틀의 아래 줄 = 페이지 위쪽
    spans = [s for b in page.get_text("dict")["blocks"] for l in b.get("lines", []) for s in l["spans"]]
    assert any(abs(l_["dir"][0] + 1) < 1e-3 for b in page.get_text("dict")["blocks"] for l_ in b.get("lines", []))
    assert spans
