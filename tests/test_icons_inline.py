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


def test_match_line_uses_icon_height_for_icon_only_lines():
    """칼 셋만 있는 줄(큰 아이콘)과 카드 번호 줄: 아이콘을 글자 한 칸 폭으로 치면 아이콘 줄을 빼 버렸다.
    아이콘만 있는 줄은 그 줄 높이만 한 아이콘이 늘어선 폭으로 재서, 남는 카드 번호 줄을 뺀다."""
    from app.icons import _match_line

    lines = ["プレイ時効果", "[[?]][[?]][[?]]", "自身のプレイ済の表向きのイベントカードを", "１枚選び、それを裏向きにしてもよい"]
    bands = [(8, 39), (114, 202), (276, 454), (515, 567), (591, 643)]

    def row(x0, x1, y0, y1, n):
        step = (x1 - x0) / n
        return [(int(x0 + i * step), y0, int(step * 0.8), y1 - y0, int(step * 0.8 * (y1 - y0) * 0.4)) for i in range(n)]
    comps = (row(1048, 1124, 10, 38, 5) + row(338, 818, 116, 200, 1) + row(283, 871, 277, 453, 3)
             + row(36, 1112, 517, 565, 20) + row(126, 1034, 593, 641, 17))
    assert _match_line(lines, 1, bands, comps) == 2  # 칼 줄
    assert _match_line(lines, 0, bands, comps) == 1  # 라벨
    assert _match_line(lines, 3, bands, comps) == 4


def test_extra_lines_dropped_by_width_not_glyph_size():
    """원문이 숫자 타일 줄(‘2’)을 빠뜨림: 찾은 줄 4개(라벨·타일·글·글) vs 원문 3줄. 글자가 작은 라벨을 빼면
    ‘プレイ時効果’가 타일 자리에 간다. 줄 폭으로 맞춰 좁은 타일 줄을 뺀다."""
    def ln(x0, x1, gh):
        return {"bbox": (x0, 0, x1, 10), "glyph_h": gh}
    label, tile, l2, l3 = ln(333, 813, 40), ln(502, 642, 70), ln(200, 940, 50), ln(390, 705, 60)
    extra = erase_text._extra_lines([label, tile, l2, l3], ["プレイ時効果", "人物[[person]]タグが２個以上なら", "[[sword_2]]を得る"])
    assert extra == [tile]
    # 카드 번호처럼 폭으로도 작은 줄은 그대로 빠진다
    num = ln(1040, 1125, 24)
    assert erase_text._extra_lines([num, label, l2, l3], ["プレイ時効果", "人物[[person]]タグが２個以上なら", "[[sword_2]]を得る"]) == [num]


def test_set_partner_does_not_take_printed_number_or_plus():
    """원문이 ‘[[coin]]+2’처럼 아이콘 바로 옆에 숫자·기호를 적었으면 옆 칸은 그 글자: 합치지 않는다."""
    from app.icons import _set_partner

    seq = list("それをした場合") + ["■", "+", "2"]
    cells = [(i * 60, 100, i * 60 + 50, 150) for i in range(7)] + [(430, 80, 520, 170), (525, 80, 600, 170), (610, 100, 650, 150)]
    assert _set_partner(cells[7], cells, seq, 7, 55.0, 90.0) == cells[7]


# ---------------------------------------------------------------- 아이콘 옆 숫자: 확정할 때만 빼고, 숫자가 다르면 다른 아이콘
def _num_project(root, name, digits):
    """쪽마다 'AB {숫자}● CD' 카드 (숫자 ""이면 숫자 없음). 원문은 'AB 2[[?]]CD'처럼 읽혔다고 둔다."""
    import json

    from app import workspace
    from tests.test_icons import _regions

    pdf = root / "src" / name / "cards.pdf"
    doc = pymupdf.open()
    for dg in digits:
        pg = doc.new_page(width=250, height=180)
        pg.insert_text((40, 120), "AB", fontsize=12)
        if dg:
            pg.insert_text((56, 120), dg, fontsize=12)
        pg.draw_circle((70, 116), 4.5, color=(0.85, 0.1, 0.1), fill=(0.85, 0.1, 0.1))
        pg.insert_text((80, 120), "CD", fontsize=12)
    pdf.parent.mkdir(parents=True)
    doc.save(pdf)
    info, _ = workspace.create_project(name, "G", root=root)
    workspace.add_files(info, [pdf], "카드", new_group=True, regions_from=_regions(root / f"{name}_regions.json"))
    fd = workspace.file_dirs(info)[0]
    d = fd / "extract" / "pages"
    d.mkdir(parents=True)
    for p, dg in enumerate(digits, start=1):
        (d / f"page-{p:03d}.json").write_text(json.dumps({"page": p, "model": "m", "regions": {
            "본문": {"source": f"AB {dg}[[?]]CD" if dg else "AB[[?]]CD", "uncertain": False, "note": ""}}}),
            encoding="utf-8")
    return info, fd


def _cell(fd, page):
    import json
    return json.loads((fd / "extract" / "pages" / f"page-{page:03d}.json").read_text(encoding="utf-8"))["regions"]["본문"]


def _blind_shape(monkeypatch):
    """실제 버그처럼 모양 비교가 숫자를 보지 못하는 경우 (상자+2·상자+5가 같은 모양으로 나옴)."""
    monkeypatch.setattr(icons, "same_icon", lambda a, b: 1.0)


def test_apply_icons_strips_number_and_restore_puts_it_back():
    t = "AB 2 [[?]] [[?]]\n[[?]]+1"
    out, nums, n = icons.apply_icons(t, {0: "crate", 1: "book", 2: ""}, {0: {"text": "2", "side": "before"},
                                                                      2: {"text": "+1", "side": "after"}})
    assert out == "AB [[crate]] [[book]]\n+1" and n == 1  # ‘아이콘 아님’(빼기)은 숫자를 남긴다
    assert nums == [{"index": 0, "name": "crate", "text": "2", "side": "before", "sep": " "}]
    # 사람이 그사이 숫자를 고쳤으면 빼지 않는다
    out2, nums2, n2 = icons.apply_icons("3[[?]]", {0: "crate"}, {0: {"text": "2", "side": "before"}})
    assert out2 == "3[[crate]]" and nums2 == [] and n2 == 0
    back, rest, k = icons.restore_numbers(out, "crate", nums)
    assert back == "AB 2 [[?]] [[book]]\n+1" and rest == [] and k == 1
    # 앞의 [[?]]가 ‘아이콘 아님’으로 빠지면 이미 적어 둔 번호를 당긴다
    out3, nums3, _ = icons.apply_icons("[[?]] x [[coin]]", {0: ""}, None,
                                       [{"index": 1, "name": "coin", "text": "5", "side": "before", "sep": ""}])
    assert out3 == " x [[coin]]"
    assert nums3 == [{"index": 0, "name": "coin", "text": "5", "side": "before", "sep": ""}]
    assert icons.restore_numbers(out3, "coin", nums3)[0] == " x 5[[?]]"


def test_scan_keeps_number_until_confirm_and_splits_groups_by_number(tmp_path, monkeypatch):
    from app import service

    _blind_shape(monkeypatch)
    info, fd = _num_project(tmp_path, "1차", ["2", "5", "5", "2"])
    service.find_icons(info.dir, suggest=False)
    assert [_cell(fd, p)["source"] for p in (1, 2)] == ["AB 2[[?]]CD", "AB 5[[?]]CD"]  # 찾을 때는 원문 그대로
    res = icons.load_scan(info.game_dir / "work" / "icons")
    assert {o.page: o.number["text"] for o in res.occurrences} == {1: "2", 2: "5", 3: "5", 4: "2"}
    by_num = {}
    for o in res.occurrences:
        by_num.setdefault(o.number["text"], set()).add(o.group)
    assert len(by_num["2"]) == len(by_num["5"]) == 1 and by_num["2"] != by_num["5"]  # 숫자가 다르면 다른 묶음
    g2, g5 = next(iter(by_num["2"])), next(iter(by_num["5"]))
    # 4쪽은 손으로 다시 잡는다 -> 숫자는 원문에 남기고, 숫자 없는 묶음으로 옮긴다
    o4 = next(o for o in res.occurrences if o.page == 4)
    r = service.set_icon_box(info.dir, o4.id, o4.box)
    assert r["group"] not in (g2, g5) and r["new_group"]
    out = service.confirm_icons(info.dir, [{"group": g2, "name": "crate_2", "ko": "상자 2"},
                                           {"group": g5, "not_icon": True},
                                           {"group": r["group"], "name": "crate", "ko": "상자"}])
    assert out.state == "confirmed", out.blocking
    c1 = _cell(fd, 1)
    assert c1["source"] == "AB [[crate_2]]CD"  # 이름으로 정한 보통 상자: 숫자를 아이콘에 넣고 원문에서 뺀다
    assert c1["icon_numbers"] == [{"index": 0, "name": "crate_2", "text": "2", "side": "before", "sep": ""}]
    assert _cell(fd, 2)["source"] == "AB 5CD"  # 아이콘 아님: 숫자는 남는다
    assert _cell(fd, 4)["source"] == "AB 2[[crate]]CD" and "icon_numbers" not in _cell(fd, 4)  # 손으로 잡은 자리
    d = icons.load_dictionary(info)["icons"]
    assert d["crate_2"]["number"] == "2" and d["crate"]["number"] == ""

    # 사전 ‘다시 정하기’: [[crate_2]] -> [[?]]와 함께 숫자 2도 제자리에
    assert service.update_icon_dictionary(info.dir, [{"name": "crate_2", "reset": True}])["reset"] == {"crate_2": 1}
    c1 = _cell(fd, 1)
    assert c1["source"] == "AB 2[[?]]CD" and "icon_numbers" not in c1


def test_dictionary_auto_match_needs_same_number(tmp_path, monkeypatch):
    import json

    from app import service

    _blind_shape(monkeypatch)
    info, fd = _num_project(tmp_path, "1차", ["5"])
    service.find_icons(info.dir, suggest=False)
    g = icons.load_scan(info.game_dir / "work" / "icons").occurrences[0].group
    assert service.confirm_icons(info.dir, [{"group": g, "name": "crate_5", "ko": "상자 5"}]).state == "confirmed"
    assert _cell(fd, 1)["source"] == "AB [[crate_5]]CD"

    # 다음 프로젝트: 5는 모델·사람 없이 바로 바꾸고 숫자를 뺀다, 2는 모양이 같아도 사람 확인으로
    info2, fd2 = _num_project(tmp_path, "2차", ["2", "5"])
    r = service.find_icons(info2.dir, suggest=False)
    assert r.auto == {"crate_5": 1} and r.state == "icons_review" and r.occurrences == 1
    assert _cell(fd2, 2)["source"] == "AB [[crate_5]]CD" and _cell(fd2, 2)["icon_numbers"][0]["text"] == "5"
    assert _cell(fd2, 1)["source"] == "AB 2[[?]]CD"

    # 예전 사전 항목(number 키 없음)은 어느 자리와도 자동으로 맞추지 않는다 (예전 그림에 숫자가 박혔을 수 있음).
    # 숫자 없는 자리는 ② 검토에 그 이름을 제안하고, 숫자 있는 자리는 제안하지 않는다
    dp = icons.dictionary_path(info)
    dic = json.loads(dp.read_text(encoding="utf-8"))
    dic["icons"]["crate_5"].pop("number")
    dp.write_text(json.dumps(dic, ensure_ascii=False), encoding="utf-8")
    info3, fd3 = _num_project(tmp_path, "3차", ["5", ""])
    r3 = service.find_icons(info3.dir, suggest=False)
    assert r3.auto == {} and r3.occurrences == 3  # 2차 1쪽(숫자 2)도 게임 단위로 다시 올라온다
    assert [_cell(fd3, p)["source"] for p in (1, 2)] == ["AB 5[[?]]CD", "AB[[?]]CD"]
    rev = {(g["where"][0]["file_dir"] == fd3, g["where"][0]["page"]): g
           for g in service.icon_review(info3.dir)["groups"]}
    assert rev[True, 2]["dict"] == "crate_5" and rev[True, 1]["dict"] == "" and rev[False, 1]["dict"] == ""
    other = {"group": rev[False, 1]["group"], "name": "crate_2", "ko": "상자 2"}
    rev = {k[1]: v for k, v in rev.items() if k[0]}
    # 숫자 있는 묶음을 예전 이름으로: 그림 속 숫자를 몰라 막는다 (사전·원문은 그대로)
    bad = service.confirm_icons(info3.dir, [other, {"group": rev[1]["group"], "name": "crate_5"},
                                            {"group": rev[2]["group"], "name": "crate_5"}])
    assert bad.state == "blocked" and any("crate_5_5" in b for b in bad.blocking), bad.blocking
    assert "number" not in icons.load_dictionary(info3)["icons"]["crate_5"]
    assert _cell(fd3, 1)["source"] == "AB 5[[?]]CD"
    # 숫자 없는 묶음만 예전 이름으로 확정: 사람이 그림을 보고 고른 것이므로 number ""로 적는다
    ok = service.confirm_icons(info3.dir, [other, {"group": rev[1]["group"], "name": "crate_5b", "ko": "상자 5"},
                                           {"group": rev[2]["group"], "name": "crate_5"}])
    assert ok.state == "confirmed", ok.blocking
    assert _cell(fd3, 2)["source"] == "AB[[crate_5]]CD"
    assert _cell(fd3, 1)["source"] == "AB [[crate_5b]]CD"
    d3 = icons.load_dictionary(info3)["icons"]
    assert d3["crate_5"]["number"] == "" and d3["crate_5b"]["number"] == "5"


def test_legacy_entry_is_never_auto_matched():
    assert not icons.number_allows(None, "") and not icons.number_allows(None, "2")
    assert icons.number_allows("", "") and icons.number_allows("2", "2") and not icons.number_allows("2", "")


def test_one_name_for_groups_with_different_numbers_is_blocked(tmp_path, monkeypatch):
    from app import service

    _blind_shape(monkeypatch)
    info, fd = _num_project(tmp_path, "1차", ["2", "5", ""])
    service.find_icons(info.dir, suggest=False)
    gs = {g["where"][0]["page"]: g["group"] for g in service.icon_review(info.dir)["groups"]}
    r = service.confirm_icons(info.dir, [{"group": gs[1], "name": "crate"}, {"group": gs[2], "name": "crate"},
                                         {"group": gs[3], "name": "plain"}])
    assert r.state == "blocked"
    assert r.blocking == ["수량이 다른 아이콘을 같은 이름 crate로 정할 수 없습니다 (2, 5): crate_2·crate_5처럼 나눠 주세요"]
    assert [_cell(fd, p)["source"] for p in (1, 2, 3)] == ["AB 2[[?]]CD", "AB 5[[?]]CD", "AB[[?]]CD"]
    assert icons.load_dictionary(info)["icons"] == {}
    # 숫자 없는 것과 섞어도 막는다
    r = service.confirm_icons(info.dir, [{"group": gs[1], "name": "crate_2"}, {"group": gs[2], "name": "crate_5"},
                                         {"group": gs[3], "name": "crate_2"}])
    assert r.state == "blocked" and "(숫자 없음, 2)" in r.blocking[0]
    # 나누면 된다
    ok = service.confirm_icons(info.dir, [{"group": gs[1], "name": "crate_2"}, {"group": gs[2], "name": "crate_5"},
                                          {"group": gs[3], "name": "crate"}])
    assert ok.state == "confirmed", ok.blocking
    assert [_cell(fd, p)["source"] for p in (1, 2, 3)] == ["AB [[crate_2]]CD", "AB [[crate_5]]CD", "AB[[crate]]CD"]

    # 다음 프로젝트: 사전 crate_5(숫자 5)를 숫자 2인 묶음에 쓰면 막는다
    info2, fd2 = _num_project(tmp_path, "2차", ["2"])
    monkeypatch.setattr(icons, "same_icon", lambda a, b: 0.0)  # 사전과 자동으로 맞추지 않게
    service.find_icons(info2.dir, suggest=False)
    monkeypatch.setattr(icons, "same_icon", lambda a, b: 1.0)
    g = service.icon_review(info2.dir)["groups"][0]["group"]
    r2 = service.confirm_icons(info2.dir, [{"group": g, "name": "crate_5"}])
    assert r2.state == "blocked" and r2.blocking == [
        "수량이 다른 아이콘을 같은 이름 crate_5로 정할 수 없습니다 (사전 그림: 5, 이 아이콘: 2): "
        "crate_2처럼 새 이름을 정해 주세요"]
    assert _cell(fd2, 1)["source"] == "AB 2[[?]]CD"


def test_save_source_drops_stale_icon_numbers(tmp_path, monkeypatch):
    from app import service

    _blind_shape(monkeypatch)
    info, fd = _num_project(tmp_path, "1차", ["2"])
    service.find_icons(info.dir, suggest=False)
    g = service.icon_review(info.dir)["groups"][0]["group"]
    assert service.confirm_icons(info.dir, [{"group": g, "name": "crate_2", "ko": "상자"}]).state == "confirmed"
    assert _cell(fd, 1)["icon_numbers"] and _cell(fd, 1)["icon_number_note"]
    # 고치지 않고 저장(확인)은 기록을 지킨다
    service.save_source(fd, 1, "본문", "AB [[crate_2]]CD")
    assert _cell(fd, 1)["icon_numbers"][0]["text"] == "2"
    # 원문을 바꾸면 예전 원문 기준 기록을 버린다 -> 다시 정하기가 숫자를 엉뚱한 곳에 되살리지 않는다
    service.save_source(fd, 1, "본문", "AB 22[[crate_2]]CD")
    c = _cell(fd, 1)
    assert "icon_numbers" not in c and "icon_number_note" not in c
    service.update_icon_dictionary(info.dir, [{"name": "crate_2", "reset": True}])
    assert _cell(fd, 1)["source"] == "AB 22[[?]]CD"


def test_scan_saves_number_without_offsets_and_dead_code_gone(tmp_path, monkeypatch):
    import json

    from app import service

    _blind_shape(monkeypatch)
    info, _fd = _num_project(tmp_path, "1차", ["2"])
    service.find_icons(info.dir, suggest=False)
    work = info.game_dir / "work" / "icons"
    saved = json.loads((work / "scan.json").read_text(encoding="utf-8"))["occurrences"][0]["number"]
    assert saved == {"text": "2", "side": "before"}
    assert not hasattr(icons, "apply_to_extract")
    # 1.12.0 scan.json(start·end 있음)도 읽힌다
    data = json.loads((work / "scan.json").read_text(encoding="utf-8"))
    data["occurrences"][0]["number"] = {"text": "2", "start": 3, "end": 4, "side": "before"}
    (work / "scan.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert icons.load_scan(work).occurrences[0].number == {"text": "2", "side": "before"}


def test_old_scan_json_without_number_still_loads(tmp_path):
    import json

    (tmp_path / "scan.json").write_text(json.dumps({"occurrences": [{
        "id": "o001", "file": "f", "file_key": "k", "page": 1, "region": "본문", "index": 0, "context": "AB[[?]]CD",
        "png": "", "reason": "x", "group": "위치 못 찾음 o001", "match": "", "score": 0.0, "line": 1, "order": 1,
        "box": [], "manual": False}], "groups": {"위치 못 찾음 o001": {"rep": "", "count": 1}}}), encoding="utf-8")
    o = icons.load_scan(tmp_path).occurrences[0]
    assert o.number == {} and o.num == ""


def test_one_occurrence_marked_not_icon_from_card_view(tmp_path, monkeypatch):
    """카드 보기 · 자리 잡기의 ‘이 자리는 아이콘 아님’: 그 자리만 ‘아이콘 아님’ 줄(혼자)로 옮기고, 같은 묶음의
    다른 자리는 그대로 아이콘으로 정한다."""
    from app import service

    _blind_shape(monkeypatch)
    info, fd = _num_project(tmp_path, "1차", ["2", "2", "2"])
    service.find_icons(info.dir, suggest=False)
    res = icons.load_scan(info.game_dir / "work" / "icons")
    (g,) = {o.group for o in res.occurrences}
    o3 = next(o for o in res.occurrences if o.page == 3)
    r = service.set_icon_not_icon(info.dir, o3.id)
    assert r["new_group"] and r["group"] != g
    rev = service.icon_review(info.dir)
    by = {x["group"]: x for x in rev["groups"]}
    assert by[r["group"]]["not_icon"] and by[r["group"]]["count"] == 1 and by[g]["count"] == 2
    assert not by[g]["not_icon"]
    out = service.confirm_icons(info.dir, [{"group": g, "name": "crate_2", "ko": "상자 2"},
                                           {"group": r["group"], "not_icon": True}])
    assert out.state == "confirmed", out.blocking
    assert _cell(fd, 1)["source"] == "AB [[crate_2]]CD"
    assert _cell(fd, 3)["source"] == "AB 2CD"  # 이 자리만 아이콘 아님 (숫자도 남음)
    # 혼자인 묶음에서 누르면 옮기지 않고 표시만
    info2, _fd2 = _num_project(tmp_path / "b", "2차", ["5"])
    service.find_icons(info2.dir, suggest=False)
    (o,) = icons.load_scan(info2.game_dir / "work" / "icons").occurrences
    r2 = service.set_icon_not_icon(info2.dir, o.id)
    assert not r2["new_group"] and service.icon_review(info2.dir)["groups"][0]["not_icon"]
