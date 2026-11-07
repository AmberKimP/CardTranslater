"""글 속 아이콘 지우기·원본 크기로 다시 넣기, 아이콘만 있는 줄, 영역 가장자리에 걸친 글자, 아이콘에 붙은 숫자."""

from __future__ import annotations

import cv2
import numpy as np
import pymupdf

from app import erase_text, icons
from app.typeset_compose import compose_layout_region, measured_icon_scales
from tests.test_layout_compose import _paper


# ---------------------------------------------------------------- 아이콘에 붙은 숫자
def test_number_near_icon_token():
    def near(t):
        i = t.index("[[?]]")
        return icons.number_near(t, i, i + 5)

    assert near("2[[?]] [[x]]") == ("before", 0, 1)
    assert near("3 [[?]]") == ("before", 0, 1)
    assert near("は2[[?]]を得る") == ("before", 1, 2)
    assert near("１０[[?]]") == ("before", 0, 2)
    assert near("[[?]]+1") == ("after", 5, 7)
    assert near("[[?]]1枚") is None  # 단위가 붙은 수는 아이콘 숫자가 아니다
    assert near("4以下なら[[?]]を得る") is None
    assert near("[[?]] 2 [[x]]") is None  # 다음 아이콘 앞의 수는 그 아이콘 것


def test_strip_spans_removes_number_and_tidies_spaces():
    t = "効果\n3 [[?]]\nは2[[?]]を得る"
    assert icons.strip_spans(t, [(3, 4), (12, 13)]) == "効果\n[[?]]\nは[[?]]を得る"


def test_absorb_number_takes_digit_next_to_icon_only():
    img = np.full((80, 220, 3), 235, np.uint8)
    cv2.rectangle(img, (100, 20), (140, 60), (90, 60, 40), -1)  # 아이콘
    cv2.putText(img, "2", (72, 56), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (120, 80, 40), 3, cv2.LINE_AA)  # 붙은 숫자
    cv2.putText(img, "A", (20, 56), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (40, 40, 40), 3, cv2.LINE_AA)  # 떨어진 글자
    box = icons.absorb_number(img, (100, 20, 141, 61), "before", 1)
    assert box is not None and 66 <= box[0] <= 76 and box[2] == 141  # 숫자까지, 옆 글자는 넣지 않음
    assert icons.absorb_number(img, (100, 20, 141, 61), "after", 1) is None  # 오른쪽엔 숫자가 없다


# ---------------------------------------------------------------- 아이콘만 있는 줄 (색 그림이라 글자로 안 잡힘)
def _ln(y0, y1, gh):
    return {"bbox": (20, y0, 200, y1), "glyph_h": gh, "color": "#303030", "align": "left"}


def test_match_source_lines_skips_icon_only_line_not_found_as_text():
    """원문 3줄(라벨 / 아이콘만 / 글)과 찾은 줄 3개(카드 번호 / 라벨 / 글): 아이콘 자리에 줄이 없으면
    글 줄끼리 맞추고, 원문에 없는 카드 번호 줄은 남긴다."""
    mask = np.ones((200, 240), bool)
    tm = erase_text.TextMask(mask, mask.copy(), False, 1.0)
    lay = {"lines": [_ln(5, 12, 7), _ln(30, 45, 15), _ln(150, 165, 15)], "dividers": []}
    out = erase_text.match_source_lines(tm, lay, "LABEL\n[[sword]][[sword]]\nTEXT", icon_rows=[(70, 120)])
    assert [ln["bbox"][1] for ln in out["lines"]] == [30, 150]
    assert not any(ln.get("keep") for ln in out["lines"])
    assert not tm.mask[5:12, 20:200].any()  # 카드 번호는 지우지 않음
    # 아이콘 자리에 줄이 잡혔으면(검정 선 아이콘) 예전처럼 그 줄을 남김 표시
    tm2 = erase_text.TextMask(np.ones((200, 240), bool), mask.copy(), False, 1.0)
    lay2 = {"lines": [_ln(30, 45, 15), _ln(80, 110, 15), _ln(150, 165, 15)], "dividers": []}
    out2 = erase_text.match_source_lines(tm2, lay2, "LABEL\n[[sword]][[sword]]\nTEXT", icon_rows=[(78, 112)])
    assert [bool(ln.get("keep")) for ln in out2["lines"]] == [False, True, False]


# ---------------------------------------------------------------- 영역 가장자리에 걸친 글자
def test_text_mask_takes_glyphs_crossing_top_edge():
    img = _paper(h=160, w=360)
    cv2.putText(img, "TITLE", (90, 52), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (40, 40, 40), 3, cv2.LINE_AA)  # 위 줄
    cv2.putText(img, "NAME LINE", (60, 110), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (40, 40, 40), 4, cv2.LINE_AA)
    box = (20, 40, 340, 140)  # 위 줄 글자의 윗부분이 상자 밖
    tm = erase_text.text_mask(img, box)
    above = img[28:40, 90:200].min(axis=2) < 120  # 상자 밖으로 나온 글자 픽셀
    assert above.any() and tm.mask[28:40, 90:200][above].mean() > 0.8


# ---------------------------------------------------------------- 한글 넣기: 아이콘만 있는 줄·원본 아이콘 크기
def _lay_pt(icons_=None):
    def ln(y, gh=8.0):
        return {"bbox_pt": [20.0, y, 150.0, y + gh], "glyph_h_pt": gh, "color": "#303030", "stroke_color": None,
                "stroke_pt": 0.0, "weight": 0.2, "bold": False, "align": "left", "accents": []}

    return {"lines": [ln(20), ln(70)], "dividers_pt": [], "icons": icons_ or []}


def test_measured_icon_scales_median_per_name():
    lay = {"icons": [{"name": "book", "scale": 1.8}, {"name": "book", "scale": 2.0}, {"name": "x", "scale": None}]}
    assert measured_icon_scales(lay) == {"book": 1.9}


def test_compose_skips_icon_only_lines_and_uses_measured_icon_size(tmp_path):
    from PIL import Image

    Image.new("RGBA", (40, 40), (20, 120, 40, 255)).save(tmp_path / "book.png")
    style = {"icons_dir": str(tmp_path), "icon_scales": {"book": 1.1}}
    text = "첫 줄 글\n[[sword]] [[sword]]\n문명[[book]]이 4 이하"
    heights = []
    for scale in (None, 2.0):
        doc = pymupdf.open()
        page = doc.new_page(width=180, height=110)
        lay = _lay_pt([{"name": "book", "bbox_pt": [0, 0, 1, 1], "scale": scale}] if scale else None)
        rec = compose_layout_region(page, (10, 10, 170, 100), text, style, lay)
        assert rec["line_modes"] == ["slot", "slot"]  # 아이콘만 있는 줄은 그리지 않고 두 줄 자리에 맞춤
        assert "sword" not in " ".join(rec["lines"])
        imgs = [i for i in page.get_image_info() if i["bbox"][1] > 60]
        assert imgs, "책 아이콘이 둘째 줄 자리에 들어가야 한다"
        heights.append(imgs[0]["bbox"][3] - imgs[0]["bbox"][1])
    assert heights[1] > heights[0] * 1.3  # 잰 크기(글자 높이의 2배)로 더 크게
