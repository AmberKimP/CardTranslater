import pytest

from app.project import parse_page_spec
from app.regions import (
    RatioRect,
    clip_horizontally,
    overlap_ratio,
    pt_to_px,
    pt_to_ratio,
    px_box_outer,
    px_to_pt,
    ratio_to_pt,
)

PAGE = (0.0, 0.0, 251.76, 177.36)
IMG_BBOX = (0.0, -0.24, 252.0, 177.84)  # Dungeon.pdf 1쪽 배경 이미지
IMG_SIZE = (1050, 742)


def test_ratio_pt_roundtrip():
    r = RatioRect(0.095, 0.688, 0.663, 0.118)
    b = ratio_to_pt(r, PAGE)
    assert b[0] == pytest.approx(0.095 * 251.76)
    assert b[3] == pytest.approx((0.688 + 0.118) * 177.36)
    back = pt_to_ratio(b, PAGE)
    for a, c in zip((back.x, back.y, back.w, back.h), (r.x, r.y, r.w, r.h)):
        assert a == pytest.approx(c)


def test_ratio_to_pt_with_offset_page():
    b = ratio_to_pt(RatioRect(0.5, 0.5, 0.5, 0.5), (10.0, 20.0, 110.0, 220.0))
    assert b == pytest.approx((60.0, 120.0, 110.0, 220.0))


def test_pt_px_roundtrip_and_300dpi():
    b = (25.92, 110.88, 60.48, 118.8)
    px = pt_to_px(b, IMG_BBOX, IMG_SIZE)
    # 1050 px / 252 pt = 300 dpi
    assert px[0] == pytest.approx(25.92 * 1050 / 252)
    assert px[1] == pytest.approx((110.88 + 0.24) * 742 / 178.08)
    assert px_to_pt(px, IMG_BBOX, IMG_SIZE) == pytest.approx(b)


def test_px_box_outer_covers_and_clamps():
    assert px_box_outer((1.2, 2.0, 3.0, 4.7)) == (1, 2, 3, 5)
    assert px_box_outer((-3.0, -1.5, 2000.0, 9.0), (100, 50)) == (0, 0, 100, 9)


def test_overlap_ratio():
    assert overlap_ratio((0, 0, 10, 10), (5, 0, 20, 10)) == pytest.approx(0.5)
    assert overlap_ratio((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0
    assert overlap_ratio((0, 0, 0, 10), (0, 0, 10, 10)) == 0.0


def test_clip_horizontally_against_side_strips():
    left = (0.0, 0.0, 22.0, 177.0)
    right = (188.0, 0.0, 252.0, 177.0)
    assert clip_horizontally((20.0, 10.0, 195.0, 20.0), [left, right]) == (22.0, 10.0, 188.0, 20.0)
    assert clip_horizontally((30.0, 10.0, 100.0, 20.0), [left, right]) == (30.0, 10.0, 100.0, 20.0)


def test_parse_page_spec():
    assert parse_page_spec("1-3", 30) == [1, 2, 3]
    assert parse_page_spec("1, 5, 12-14, 5", 30) == [1, 5, 12, 13, 14]
    with pytest.raises(ValueError):
        parse_page_spec("0", 30)
    with pytest.raises(ValueError):
        parse_page_spec("5-3", 30)
    with pytest.raises(ValueError):
        parse_page_spec("31", 30)
