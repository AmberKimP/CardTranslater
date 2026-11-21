"""② 원문 확인: 사람이 잡은 아이콘 상자가 옆 숫자까지 감쌌으면 그 숫자는 아이콘 그림이다 (확정할 때 원문에서 뺀다)."""

from __future__ import annotations

import cv2
import numpy as np

from app import icons
from app.icons import Occurrence


def _card():
    """종이 위에 숫자 ‘2’와 그 오른쪽의 네모 아이콘 (P7 ‘2 [[prod2coins]]’ 모양)."""
    img = np.full((200, 400, 3), 235, np.uint8)
    cv2.putText(img, "2", (95, 140), cv2.FONT_HERSHEY_SIMPLEX, 3.0, (90, 60, 30), 9)
    cv2.rectangle(img, (190, 50), (300, 160), (70, 40, 20), -1)
    cv2.circle(img, (245, 105), 35, (60, 170, 200), -1)
    return img


def test_box_around_digit_and_icon_counts_the_digit():
    img = _card()
    assert icons.number_in_box(img, (90, 45, 305, 165), "before", 1)


def test_box_around_icon_only_leaves_the_digit():
    img = _card()
    assert not icons.number_in_box(img, (185, 45, 305, 165), "before", 1)


def test_manual_occurrence_number_rules():
    o = Occurrence("o1", "f", "k", 1, "본문", 0, "")
    o.manual, o.number = True, {"text": "2", "side": "before"}
    assert not o.num_in_image and o.num == ""  # 손으로 잡았고 숫자를 감싸지 않음: 숫자는 원문에 남는다
    o.number["boxed"] = True
    assert o.num_in_image and o.num == "2"
    o.manual, o.number = False, {"text": "3", "side": "after"}
    assert o.num_in_image and o.num == "3"


def test_boxed_number_finds_the_token_digit():
    from app.icons_place import _boxed_number

    img = _card()
    o = Occurrence("o1", "f", "k", 1, "본문", 0, "", ti=1)
    text = "プレイ時効果[[heart]]\n2 [[?]]"
    assert _boxed_number(img, text, o, (90, 45, 305, 165)) == {"text": "2", "side": "before", "boxed": True}
    assert _boxed_number(img, text, o, (185, 45, 305, 165)) is None
    assert _boxed_number(img, "[[?]]만", o, (90, 45, 305, 165)) is None  # 옆 숫자가 없음


def test_boxed_flag_survives_scan_save_and_load(tmp_path):
    o = Occurrence("o1", "f", "k", 1, "본문", 0, "", manual=True, number={"text": "2", "side": "before", "boxed": True})
    icons._save_scan(tmp_path, icons.ScanResult(occurrences=[o]))
    back = icons.load_scan(tmp_path).occurrences[0]
    assert back.number == {"text": "2", "side": "before", "boxed": True} and back.num_in_image


def test_box_over_the_gap_only_does_not_count_the_digit():
    img = _card()  # 숫자 x≈95..160, 아이콘 190..300: 숫자 끝만 조금 감쌈
    assert not icons.number_in_box(img, (150, 45, 305, 165), "before", 1)


def test_order_conflicts_between_stacked_icons():
    from app.icons import ScanResult
    from app.icons_place import order_conflicts

    a = Occurrence("o1", "f", "k", 5, "본문2", 0, "", line=2, order=1, group="g1", box=[10, 60, 40, 90])  # 아래
    b = Occurrence("o2", "f", "k", 5, "본문2", 1, "", line=3, order=1, group="g2", box=[10, 10, 40, 40])  # 위
    res = ScanResult(occurrences=[a, b], groups={"g1": {}, "g2": {}})
    assert order_conflicts(res, a) == ["o2"] and order_conflicts(res, b) == ["o1"]
    b.box, a.box = [10, 60, 40, 90], [10, 10, 40, 40]
    assert order_conflicts(res, a) == []


def test_erase_icon_boxes_skips_cells_out_of_text_order():
    import pymupdf

    from app.regions import RatioRect
    from app.service._translate_erase import erase_icon_boxes

    class R:
        name, style = "본문2", {}
        rect = RatioRect(0.0, 0.0, 1.0, 1.0)
    src = "常時効果\nA[[gear]]B\n[[coin]]C"
    good = {"본문2": {"source": src, "icon_boxes": [{"name": "gear", "k": 0, "box": [10, 10, 40, 40]},
                                                   {"name": "coin", "k": 0, "box": [10, 60, 40, 90]}]}}
    out = erase_icon_boxes(good, pymupdf.Rect(0, 0, 200, 300), [R])
    assert [(n, k) for n, k, _b in out["본문2"]] == [("gear", 0), ("coin", 0)]
    bad = {"본문2": {"source": src, "icon_boxes": [{"name": "gear", "k": 0, "box": [10, 60, 40, 90]},
                                                  {"name": "coin", "k": 0, "box": [10, 10, 40, 40]}]}}
    assert erase_icon_boxes(bad, pymupdf.Rect(0, 0, 200, 300), [R]) == {}
    bad["본문2"]["source"] = "生活[[house]]\n" + src  # 엇갈리지 않은 house는 쓴다 (8쪽)
    bad["본문2"]["icon_boxes"].insert(0, {"name": "house", "k": 0, "box": [10, 0, 30, 5]})
    assert [n for n, _k, _b in erase_icon_boxes(bad, pymupdf.Rect(0, 0, 200, 300), [R])["본문2"]] == ["house"]


def test_moving_a_box_far_away_drops_the_old_name_suggestion(tmp_path):
    from app import icons, service
    from tests.test_icons import _project

    info, fd = _project(tmp_path, "mv", pages=1)
    service.find_icons(info.dir, suggest=False)
    work = info.game_dir / "work" / "icons"
    res = icons.load_scan(work)
    o = res.occurrences[0]
    res.groups[o.group].update(suggest="coin_3", meaning="코인", ko="코인")
    icons._save_scan(work, res)
    service.set_icon_box(info.dir, o.id, o.box)  # 같은 자리: 제안은 그대로
    gi = icons.load_scan(work).groups[icons.load_scan(work).occurrences[0].group]
    assert gi.get("suggest") == "coin_3"
    x0, y0, x1, y1 = o.box
    w = x1 - x0
    service.set_icon_box(info.dir, o.id, [x0 + 2 * w, y0, x1 + 2 * w, y1])  # 전혀 다른 곳
    res2 = icons.load_scan(work)
    gi = res2.groups[res2.occurrences[0].group]
    assert not gi.get("suggest") and not gi.get("meaning")
