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
    # 아이콘 자리에 줄이 잡혔으면(검정 선 아이콘): 그 줄은 빼 두고 지우지 않는다, 글 줄끼리 맞춘다
    tm2 = erase_text.TextMask(np.ones((200, 240), bool), mask.copy(), False, 1.0)
    lay2 = {"lines": [_ln(30, 45, 15), _ln(80, 110, 15), _ln(150, 165, 15)], "dividers": []}
    out2 = erase_text.match_source_lines(tm2, lay2, "LABEL\n[[sword]][[sword]]\nTEXT", icon_rows=[(78, 112)])
    assert [ln["bbox"][1] for ln in out2["lines"]] == [30, 150]
    assert not tm2.mask[85:105, 20:200].any()  # 아이콘 줄은 지우지 않음


def test_match_source_lines_icon_pieces_do_not_shift_lines():
    """8쪽: 카드 번호 / 라벨 / 아이콘 조각 3줄 / 글 3줄. 아이콘 조각이 줄 수를 늘려 카드 번호가 라벨 자리를
    차지하던 문제: 아이콘 조각을 먼저 빼면 카드 번호는 원문에 없는 줄로 남고 라벨·글이 맞는다."""
    mask = np.ones((900, 1240), bool)
    tm = erase_text.TextMask(mask, mask.copy(), False, 1.0)

    def ln(x0, y0, x1, y1, gh):
        return {"bbox": (x0, y0, x1, y1), "glyph_h": gh, "color": "#303030", "align": "left"}
    lay = {"lines": [ln(1068, 95, 1148, 123, 24), ln(378, 229, 815, 274, 40), ln(300, 391, 512, 429, 38),
                     ln(310, 452, 425, 474, 20), ln(298, 492, 361, 508, 16), ln(73, 630, 1115, 687, 50),
                     ln(40, 720, 1138, 777, 50), ln(275, 815, 846, 866, 50)], "dividers": []}
    src = "プレイ時効果\n[[coin_crate_arrow]] [[tower_down]]\nこのカード\nその拠点\nすべて"
    out = erase_text.match_source_lines(tm, lay, src, icon_rows=[(358, 547), (353, 592)])
    assert [l_["bbox"][1] for l_ in out["lines"]] == [229, 630, 720, 815]
    assert not tm.mask[95:123, 1068:1148].any()  # 카드 번호는 지우지 않음


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


def test_set_partner_merges_number_and_icon_read_as_one_token():
    """원문이 ‘3 + 검’을 아이콘 하나로 읽었으면, 고른 칸(3)에 바짝 붙은 큰 칸(검)까지 한 그림으로."""
    from app.icons import _set_partner

    seq = list("そうした場合") + ["■"] + list("を得る")
    cells = [(i * 60, 100, i * 60 + 50, 150) for i in range(6)]
    cells += [(380, 92, 418, 160), (422, 80, 520, 178), (540, 100, 590, 150), (600, 100, 650, 150), (660, 100, 710, 150)]
    box = _set_partner(cells[6], cells, seq, 6, 55.0, 90.0)
    assert box[0] == 380 and box[2] == 520 and box[1] < 92 and box[3] > 160
    # 원문에서 바로 뒤가 다른 아이콘이면 따로 읽은 것: 합치지 않는다
    seq2 = list("そうした場合") + ["■", "■"] + list("得る")
    assert _set_partner(cells[6], cells, seq2, 6, 55.0, 90.0) == cells[6]
    # 뒤 칸이 보통 글자 크기면 합치지 않는다
    small = cells[:7] + [(422, 100, 470, 150)] + cells[8:]
    assert _set_partner(cells[6], small, seq, 6, 55.0, 90.0) == cells[6]


def test_loose_bands_keep_short_title_strip_on_icon_only_card():
    """아이콘만 있는 카드: 제목 띠(아이콘보다 낮음)는 보통 기준에선 빠지지만 느슨한 기준에선 남는다."""
    from app.icons import _bands_from

    comps = [(542, 20, 66, 20, 487),  # 장식 (실제 카드에서 뽑은 덩어리)
             (1046, 52, 19, 30, 302), (1077, 52, 17, 30, 314), (1095, 52, 16, 30, 268), (1111, 52, 17, 30, 329),  # 카드 번호
             (335, 171, 481, 88, 36331),  # 제목 띠
             (630, 309, 196, 226, 26434), (343, 327, 196, 191, 24474), (396, 358, 91, 130, 6876),
             (464, 445, 58, 58, 1613),  # 아이콘 줄
             (474, 612, 223, 237, 34134)]  # 아이콘 줄
    assert len(_bands_from(comps)) == 2
    assert len(_bands_from(comps, min_rel_h=0.3)) == 3


def test_icon_review_puts_issues_first_then_appearance_order(tmp_path, monkeypatch):
    from app import icons, service
    from tests.test_icons import _project as icon_project

    monkeypatch.setattr(icons, "SAME_SHAPE", 1.01)  # 쪽마다 따로 묶이게
    info, _fd = icon_project(tmp_path, "1차", pages=3)
    service.find_icons(info.dir, suggest=False)
    work = info.game_dir / "work" / "icons"
    res = icons.load_scan(work)
    names = list(res.groups)
    assert len(names) == 3
    for g in names:
        res.groups[g].update(suggest="coin", meaning="코인", ko="금화")
    res.groups[names[2]]["ko"] = ""  # 마지막에 나온 것만 이슈
    icons._save_scan(work, res)
    groups = service.icon_review(info.dir)["groups"]
    assert [g["group"] for g in groups] == [names[2], names[0], names[1]]
    assert groups[0]["issue"] == "한국어 이름 없음" and not groups[1]["issue"]


def test_sentence_blocks_align_multiline_sentence_to_its_outer_edges():
    """여러 줄 문장은 문장 전체의 가장 왼쪽·오른쪽이 기준. 가운데 맞춘 원문이면 가운데, 서식이 다른 줄은 따로."""
    from app.typeset_compose import sentence_blocks

    def ln(x0, x1, gh=6.2, g=0):
        return {"bbox_pt": [x0, 0, x1, 6], "glyph_h_pt": gh, "_g": g}
    num = ln(146.8, 156.4, gh=3.4, g=1)  # 카드 번호
    a, b, c = ln(23.6, 155.3), ln(37.3, 141.5), ln(40.6, 130.9)
    blocks = sentence_blocks([num, a, b, c], ["플레이 시 효과", "이 카드 위에 있는", "칩을 1장 놓아도 된다", "그렇게 한 경우 얻는다"])
    assert id(num) not in blocks and id(c) not in blocks  # 한 줄 문장·다른 서식은 그대로
    assert blocks[id(a)] is blocks[id(b)]
    assert blocks[id(a)]["align"] == "center" and blocks[id(a)]["x0"] == 23.6 and blocks[id(a)]["x1"] == 155.3
    left = sentence_blocks([ln(20, 150), ln(20, 90)], ["첫 줄", "끝."])
    assert next(iter(left.values()))["align"] == "left"
