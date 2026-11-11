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


def test_outlined_white_name_found_even_when_white_panels_surround_band():
    """Dungeon 이름 띠: 띠 위아래에 흰 패널이 있어도(흰색이 상자 밖에 흔함) 흰 글자 + 검은 테두리를 글자로 찾는다."""
    band = _glass(h=90, w=420)
    img = np.full((170, 420, 3), (245, 240, 236), np.uint8)  # 위아래 흰 패널
    img[40:130] = band
    white = np.zeros(img.shape[:2], np.uint8)
    cv2.putText(white, "NAME", (110, 105), cv2.FONT_HERSHEY_SIMPLEX, 1.5, 255, 6)
    outline = cv2.dilate(white, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11)))
    img[outline > 0] = (40, 38, 42)
    img[white > 0] = (240, 238, 236)
    box = (20, 44, 400, 126)
    tm = erase_text.text_mask(img, box)
    assert tm.busy and tm.mask[white > 0].mean() > 0.9
    lay = erase_text.text_layout(img, tm, box)
    assert len(lay["lines"]) == 1 and lay["lines"][0]["stroke_color"]


def test_single_line_section_below_divider_is_centered():
    img = _paper(h=220)
    cv2.putText(img, "LEFT LINE ONE", (30, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (40, 40, 40), 3, cv2.LINE_AA)
    cv2.putText(img, "LINE TWO", (30, 95), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (40, 40, 40), 3, cv2.LINE_AA)
    cv2.line(img, (40, 140), (320, 140), (40, 40, 40), 3)
    (tw, _), _ = cv2.getTextSize("flavor", cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)
    cv2.putText(img, "flavor", (180 - tw // 2, 185), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (40, 40, 40), 2, cv2.LINE_AA)
    box = (20, 10, 340, 210)
    lay = erase_text.text_layout(img, erase_text.text_mask(img, box), box)
    assert [ln["align"] for ln in lay["lines"]] == ["left", "left", "center"]
    assert all("cy" in ln for ln in lay["lines"])


def test_compose_layout_falls_back_on_noise_lines_and_sizes_sections_separately():
    doc = pymupdf.open()
    page = doc.new_page(width=180, height=110)
    noise = {"lines": [{"bbox_pt": [20, 20, 150, 22], "glyph_h_pt": 1.5, "color": "#ffff00", "stroke_color": None,
                        "stroke_pt": 0, "weight": 0.1, "bold": False, "align": "left", "accents": []}], "dividers_pt": []}
    assert compose_layout_region(page, (10, 10, 170, 100), "이름", {}, noise) is None
    lay = _layout()
    lay["lines"][3]["glyph_h_pt"] = 5.0  # 구분선 아래 문구는 작은 글자
    rec = compose_layout_region(page, (10, 10, 170, 100), "첫 줄\n둘째 줄\n+2\n아래 문구", {}, lay)
    hs = [b[3] - b[1] for b in rec["line_boxes_pt"]]
    assert hs[3] < 0.8 * hs[0]  # 아래 묶음은 따로 작게


def test_compose_page_flags_erased_region_without_translation(tmp_path):
    from app.typeset_compose import STATUS_NO_TRANSLATION, compose_page

    doc = pymupdf.open()
    page = doc.new_page(width=180, height=110)
    src = doc.new_page(width=180, height=110)
    meta = {"regions": [{"name": "본문", "kind": "text", "rect_pt": [10, 10, 170, 100],
                         "erase": {"text_mask": {"used": "pattern"}}, "text_layout": _layout()}]}
    out = compose_page(page, src, meta, {"본문": {"ko": ""}})
    assert out["본문"]["status"] == [STATUS_NO_TRANSLATION]


def test_compose_layout_flags_lines_erased_but_not_read():
    from app.typeset_compose import STATUS_MISSING_LINE

    doc = pymupdf.open()
    page = doc.new_page(width=180, height=110)
    rec = compose_layout_region(page, (10, 10, 170, 100), "첫 줄\n둘째 줄\n아래 문구", {}, _layout(),
                                source_text="一行目\n二行目\n下の文")  # 원문 3줄, 지운 줄 4줄 (coin +2 줄을 못 읽음)
    assert STATUS_MISSING_LINE in rec["status"]


def test_label_pill_text_is_text_and_icon_only_and_extra_lines_are_kept():
    """빨간 라벨 띠 안 흰 글자는 지울 글자(띠는 남김). 원문에 맞춰: 작은 카드 번호 줄·아이콘만 있는 줄은 남긴다."""
    img = _paper(h=260)
    cv2.putText(img, "A-001", (290, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (40, 40, 40), 1, cv2.LINE_AA)  # 카드 번호
    cv2.rectangle(img, (110, 40), (250, 76), (98, 90, 187), -1)  # 빨간 라벨 띠 (RGB)
    cv2.putText(img, "LABEL", (128, 69), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (250, 250, 250), 2, cv2.LINE_AA)
    cv2.putText(img, "4", (120, 140), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (60, 80, 140), 4, cv2.LINE_AA)
    cv2.rectangle(img, (160, 100), (210, 145), (90, 60, 40), -1)  # 아이콘
    cv2.putText(img, "TEXT LINE", (60, 200), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (40, 40, 40), 3, cv2.LINE_AA)
    box = (20, 10, 340, 250)
    tm = erase_text.text_mask(img, box)
    white = (img.min(axis=2) > 240)
    white[:, :110] = white[:, 251:] = False
    white[:40] = white[77:] = False
    assert tm.mask[white].mean() > 0.8  # 라벨 안 흰 글자는 지운다
    assert not tm.dividers  # 두꺼운 라벨은 구분선이 아니다
    lay = erase_text.text_layout(img, tm, box)
    lay = erase_text.match_source_lines(tm, lay, "LABEL\n4[[coin]]\nTEXT LINE")
    assert len(lay["lines"]) == 3 and lay["lines"][1].get("keep")
    assert not tm.mask[12:26, 285:340].any()  # 원문에 없는 카드 번호는 남김
    assert not tm.mask[110:142, 115:145].any()  # 아이콘만 있는 줄의 숫자도 남김
    assert erase_text.is_icon_line("4[[coin]] [[tile]]") and not erase_text.is_icon_line("迷宮[[x]]")


def test_small_protect_box_does_not_clip_region_width():
    """영역 높이 일부만 덮는 작은 보호 사각형(카드 번호)은 영역을 자르지 않는다. 세로 띠 보호 영역은 자른다."""
    from app.regions import clip_horizontally

    region = (18.0, 36.0, 162.0, 68.0)
    assert clip_horizontally(region, [(132.0, 64.0, 166.0, 72.0)]) == region  # 아래쪽 4pt만 걸침
    assert clip_horizontally(region, [(140.0, 0.0, 180.0, 250.0)])[2] == 140.0  # 오른쪽 세로 띠


def test_ocr_exclude_area_is_painted_out_of_extraction_crop():
    import io

    from PIL import Image

    from app.extract import region_crop_png
    from app.regions import RatioRect, Region, _protect

    doc = pymupdf.open()
    page = doc.new_page(width=200, height=100)
    page.draw_rect(pymupdf.Rect(150, 10, 190, 30), color=None, fill=(0, 0, 0))  # 카드 번호 자리 (검정)
    reg = Region("제목", RatioRect(0.0, 0.0, 1.0, 0.5))
    ex = _protect({"name": "번호", "rect": {"x": 0.7, "y": 0.05, "w": 0.3, "h": 0.3}, "ocr_exclude": True})
    assert ex.extract.get("ocr_exclude")
    plain = np.array(Image.open(io.BytesIO(region_crop_png(page, reg))).convert("L"))
    masked = np.array(Image.open(io.BytesIO(region_crop_png(page, reg, exclude=[ex]))).convert("L"))
    h, w = plain.shape
    box = (slice(int(h * 0.3), int(h * 0.5)), slice(int(w * 0.8), int(w * 0.9)))
    assert plain[box].mean() < 50 and masked[box].mean() > 200  # 읽기 제외 자리는 바탕색으로 덮임


def test_region_align_modes_for_line_slots():
    """영역 정렬 설정: 왼쪽(글 칸 왼쪽 끝) · 가운데(영역 가운데) · 오른쪽(글 칸 오른쪽 끝) · 원래 위치 그대로."""
    lay = _layout()
    lay["lines"][1]["bbox_pt"] = [40, 32, 110, 40]  # 둘째 줄은 들여 쓴 줄
    rect = (10, 10, 170, 100)

    def boxes(align):
        doc = pymupdf.open()
        page = doc.new_page(width=180, height=110)
        return compose_layout_region(page, rect, "첫 줄\n둘째\n+2\n아래", {"align": align} if align else {}, lay)["line_boxes_pt"]

    left, orig, center, right = boxes("left"), boxes("original"), boxes("center"), boxes("right")
    assert abs(left[1][0] - 20) < 1  # 글 칸 왼쪽 끝(가장 왼쪽 원문 줄 20)
    assert abs(orig[1][0] - 40) < 1  # 그 줄의 원문 시작점
    assert all(abs((b[0] + b[2]) / 2 - 90) < 1.5 for b in center)  # 영역 가운데
    assert all(abs(b[2] - 150) < 1.5 for b in right[:2])  # 글 칸 오른쪽 끝(150)


def _plain_lines(*ys, bold=False):
    return {"lines": [{"bbox_pt": [20.0, y, 150.0, y + 8], "glyph_h_pt": 8.0, "color": "#303030", "stroke_color": None,
                       "stroke_pt": 0.0, "weight": 0.2, "bold": bold, "align": "left", "accents": []} for y in ys],
            "dividers_pt": []}


def test_compose_layout_joins_two_line_sentence_into_one_line():
    """원문 두 줄이 한 문장이고 한글이 한 줄에 들어가면 한 줄로 (첫 줄 자리에서, 다음 문장은 바로 아래 줄로)."""
    doc = pymupdf.open()
    page = doc.new_page(width=180, height=110)
    rec = compose_layout_region(page, (10, 10, 170, 100), "카드를\n한 장 뽑는다.\n버린다.", {}, _plain_lines(20, 32, 44))
    assert rec["line_modes"] == ["joined"] and rec["lines"] == ["카드를 한 장 뽑는다.", "버린다."]
    b = rec["line_boxes_pt"]
    assert abs((b[0][1] + b[0][3]) / 2 - 24) < 2.5 and abs((b[1][1] + b[1][3]) / 2 - 36) < 2.5
    assert "두 줄 문장을 한 줄로" in rec["warnings"][0] and not rec["overflow_text"]


def test_compose_layout_keeps_lines_when_not_two_line_sentence_or_too_long():
    doc = pymupdf.open()
    lay = _plain_lines(20, 32, 44)
    # 세 줄 문장: 그대로
    rec = compose_layout_region(doc.new_page(width=180, height=110), (10, 10, 170, 100),
                                "카드를\n한 장\n뽑는다.", {}, lay)
    assert rec["line_modes"] == ["slot"] * 3
    # 두 줄 문장이지만 이으면 한 줄 폭을 넘음: 그대로
    long = "아주 길고 긴 한국어 번역 문장이 여기에\n이어지는 두 번째 줄도 길게 쓴다.\n끝."
    rec = compose_layout_region(doc.new_page(width=180, height=110), (10, 10, 170, 100), long, {}, lay)
    assert rec["line_modes"] == ["slot"] * 3
    # 굵기가 다른 줄이 섞인 묶음: 줄 모양을 지키려 그대로
    mixed = _plain_lines(20, 32, 44)
    mixed["lines"][1]["bold"] = True
    rec = compose_layout_region(doc.new_page(width=180, height=110), (10, 10, 170, 100),
                                "카드를\n한 장 뽑는다.\n버린다.", {}, mixed)
    assert rec["line_modes"] == ["slot"] * 3


def test_overflow_in_line_slots_falls_back_to_whole_region_without_dropping_text():
    """원문 줄 자리(아주 좁은 줄)에 다 들어가지 않으면 버리지 않고 영역 전체에 줄여 넣는다 (엘프 3쪽 제목처럼)."""
    from app.typeset_compose import STATUS_WHOLE_REGION, fit_whole_region

    lay = {"lines": [{"bbox_pt": [150.0, 20.0, 152.0, 30.0], "glyph_h_pt": 10.0, "color": "#202020",
                      "stroke_color": None, "stroke_pt": 0.0, "weight": 0.2, "bold": False, "align": "left",
                      "accents": []}], "dividers_pt": []}
    text = "작은 불길이 엘프의 숲을 태운다"
    doc = pymupdf.open()
    page = doc.new_page(width=180, height=110)
    dry = compose_layout_region(page, (10, 10, 170, 60), text, {}, lay, draw=False)
    assert dry["overflow_text"]  # 줄 자리에는 다 안 들어간다
    assert not page.get_text().strip()  # 시험은 그리지 않는다
    rec = fit_whole_region(page, (10, 10, 170, 60), text, {}, lay)
    assert rec is not None and not rec["overflow_text"] and STATUS_WHOLE_REGION in rec["status"]
    assert "".join(page.get_text().split()) == "".join(text.split())  # 한 글자도 빠지지 않는다


def test_result_pages_report_dropped_text(tmp_path):
    """결과 보기: 넣지 못한 글자(overflow_text)를 칸마다 돌려준다."""
    import json

    from app import service

    (tmp_path / "compare").mkdir()
    (tmp_path / "report.json").write_text(json.dumps({"pages_detail": [{"page": 3, "regions": {
        "제목": {"status": ["넘침"], "overflow_text": "작\n은\n불"}, "본문": {"status": []}}}]}), encoding="utf-8")
    p = service.result_pages(tmp_path)[0]
    assert p["regions"][0]["overflow"] == "작 은 불" and p["regions"][1]["overflow"] == ""


def test_color_tag_around_whole_slot_uses_line_color_without_mismatch():
    """원문 한 줄(회색 인용문) 전체를 <c1>로 읽은 경우: 그 줄 색으로 그리고 ‘서식 불일치’로 보지 않는다."""
    import pymupdf

    from app.typeset import TextStyle, typeset_region
    from app.typeset_layout import STATUS_TAG_MISMATCH

    doc = pymupdf.open()
    page = doc.new_page(width=200, height=100)
    st = TextStyle(font_file="fonts/notosanskr/NotoSansKR-Regular.ttf", size_pt=8, color="#969994")
    res = typeset_region(page, pymupdf.Rect(10, 10, 190, 40), "<c1>인용문 한 줄</c1>", st)
    assert STATUS_TAG_MISMATCH not in res.status
    part = typeset_region(page, pymupdf.Rect(10, 50, 190, 90), "본문 <c1>강조</c1>",
                          TextStyle(font_file="fonts/notosanskr/NotoSansKR-Regular.ttf", size_pt=8))
    assert STATUS_TAG_MISMATCH in part.status  # 일부만 감싼 색 태그는 색을 몰라 그대로 알린다
