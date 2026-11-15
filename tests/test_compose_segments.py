"""그림 아래 짧은 이름 줄(‘工房 / 馬車 / 商館’): 번역 조각을 원문 잉크 덩어리마다 가운데에 넣는다 (공주1 1쪽)."""

from __future__ import annotations

import pymupdf
import pytest

from app import typeset_compose as tc
from app.typeset_compose import compose_layout_region, segment_layout, split_segments

SRC = "ずれかを2つまで選択し実行\n工房 / 馬車 / 商館"
KO = "다음 중 최대 2개를 선택하여 실행\n공방 / 마차 / 상관"
CLUSTERS = [(20.0, 38.0), (75.0, 95.0), (130.0, 150.0)]  # 이름 잉크 덩어리 (가운데 29, 85, 140)
SLASHES = [(55.5, 58.0), (111.5, 114.0)]  # 사이 ‘/’ 자리


def _ln(x0, y, x1, gh):
    return {"bbox_pt": [x0, y, x1, y + gh], "glyph_h_pt": gh, "color": "#505050", "stroke_color": None,
            "stroke_pt": 0.0, "weight": 0.2, "bold": False, "align": "left", "accents": []}


def _layout():
    return {"lines": [_ln(40, 20, 140, 4.32), _ln(20, 40, 150, 6.0)], "dividers_pt": [], "icons": []}


def _pages(clusters=CLUSTERS, slash_text=False):
    """원본 쪽: 위 줄 글자·아래 이름 덩어리(검은 사각형), 사이 ‘/’ 그림(두 쪽 모두). 지운 쪽: 글자 자리만 비었다.
    slash_text면 ‘/’도 원본에만 있는 글자 잉크 (지워짐)."""
    sdoc, edoc = pymupdf.open(), pymupdf.open()
    sp, ep = sdoc.new_page(width=180, height=80), edoc.new_page(width=180, height=80)
    sp.draw_rect(pymupdf.Rect(40, 20, 140, 24.32), color=(0, 0, 0), fill=(0, 0, 0))
    for x0, x1 in clusters:
        sp.draw_rect(pymupdf.Rect(x0, 40, x1, 46), color=(0, 0, 0), fill=(0, 0, 0))
    for x0, x1 in SLASHES:
        if slash_text:
            sp.draw_rect(pymupdf.Rect(x0, 40, x1, 46), color=(0, 0, 0), fill=(0, 0, 0))
        else:  # 지우지 않는 큰 ‘/’ 그림 (이름 줄보다 위아래로 길다)
            for p in (sp, ep):
                p.draw_line((x1 + 2, 28), (x0 - 2, 52), color=(0.2, 0.2, 0.2), width=1.5)
    return (sdoc, sp), (edoc, ep)


def _centers(boxes):
    return [(b[0] + b[2]) / 2 for b in boxes]


# ---------------------------------------------------------------- 순수 함수
def test_split_segments():
    assert split_segments("工房 / 馬車 / 商館") == ["工房", "馬車", "商館"]
    assert split_segments("공방／마차") == ["공방", "마차"]
    assert split_segments("<b>굵게</b> 글") is None  # 닫는 태그의 / 는 나누지 않는다
    assert split_segments("<b>공방</b> / 마차") == ["<b>공방</b>", "마차"]
    assert split_segments("공방 / ") is None and split_segments("") is None and split_segments(None) is None
    assert split_segments("한 줄 글") is None


def test_segment_layout_graphic_slashes_centers_on_clusters():
    seg = segment_layout("工房 / 馬車 / 商館", "공방 / 마차 / 상관", CLUSTERS, 6.0, 10.0, 170.0)
    assert [t for t, _a, _b in seg] == ["공방", "마차", "상관"]
    assert [(a + b) / 2 for _t, a, b in seg] == pytest.approx([29.0, 85.0, 140.0])
    # 자리는 이웃 덩어리와 사이 빈 곳의 SEG_MARGIN까지만 (사이 ‘/’ 그림을 덮지 않게)
    assert seg[0][2] <= 38.0 + tc.SEG_MARGIN * 37.0 + 1e-6


def test_segment_layout_text_slashes_are_drawn_on_their_clusters():
    cl = sorted(CLUSTERS + SLASHES)
    seg = segment_layout("工房 / 馬車 / 商館", "공방 / 마차 / 상관", cl, 6.0, 10.0, 170.0)
    assert [t for t, _a, _b in seg] == ["공방", "/", "마차", "/", "상관"]
    assert [(a + b) / 2 for _t, a, b in seg] == pytest.approx([29.0, 56.75, 85.0, 112.75, 140.0])


@pytest.mark.parametrize("src, ko, clusters", [
    ("工房 / 馬車 / 商館", "공방 / 마차", CLUSTERS),  # 번역 조각 수가 다르다
    ("工房 / 馬車 / 商館", "공방, 마차, 상관", CLUSTERS),  # 번역에 ‘/’가 없다
    ("工房 / 馬車 / 商館", "공방 / 마차 / 상관", CLUSTERS[:2]),  # 덩어리 수가 다르다
    ("工房 / 馬車 / 商館", "공방 / 마차 / 상관", [(20.0, 150.0)]),  # 한 덩어리 (보통 줄)
    ("工房 / 馬車 / 商館", "공방 / 마차 / 상관", [(20.0, 38.0), (75.0, 76.0), (130.0, 150.0)]),  # 너무 좁은 이름
    ("工房 / 馬車 / 商館", "<b>공방 / 마차</b> / 상관", CLUSTERS),  # 조각 안 태그 짝이 깨진다
    ("工房 / 馬車", "공방 / 마차", [(20.0, 38.0), (50.0, 70.0), (75.0, 95.0)]),  # 가운데가 ‘/’로 보기엔 넓다
])
def test_segment_layout_falls_back(src, ko, clusters):
    assert segment_layout(src, ko, clusters, 6.0, 10.0, 170.0) is None


# ---------------------------------------------------------------- 지운 잉크 덩어리
def test_erased_clusters_ignores_unerased_slash_picture():
    (_s, sp), (_e, ep) = _pages()
    cl = tc.erased_clusters(ep, sp, [20, 40, 150, 46], 6.0)
    assert len(cl) == 3
    for (u, v), (a, b) in zip(cl, CLUSTERS):
        assert u == pytest.approx(a, abs=1.0) and v == pytest.approx(b, abs=1.0)  # 사각형 테두리 두께만큼
    (_s, sp), (_e, ep) = _pages(slash_text=True)
    assert len(tc.erased_clusters(ep, sp, [20, 40, 150, 46], 6.0)) == 5


# 공주1 1쪽 실제 지우기: 이름 둘레 흰 테두리까지 지우고, 사이 큰 ‘/’ 그림의 아래 끝(이름 줄 높이)도 함께 지웠다.
# 이름과 ‘/’ 끝 사이가 글자 높이보다 좁아 덩어리가 하나로 이어져 보통 줄로 넣었다
TIGHT = [(20.0, 38.0), (52.0, 72.0), (86.0, 104.0)]  # 이름 (가운데 29, 62, 95)
TIGHT_SLASH_X = [45.0, 79.0]  # ‘/’ 그림 가운데


def _tight_pages():
    sdoc, edoc = pymupdf.open(), pymupdf.open()
    sp, ep = sdoc.new_page(width=180, height=80), edoc.new_page(width=180, height=80)
    for p in (sp, ep):
        p.draw_rect(p.rect, color=None, fill=(0.6, 0.6, 0.6))  # 회색 그림 배경
        for cx in TIGHT_SLASH_X:  # 위아래로 긴 ‘/’ 그림 (이름 줄 아래 끝까지)
            p.draw_line((cx + 3, 27), (cx - 1, 46), color=(0.1, 0.1, 0.1), width=2.5)
    sp.draw_rect(pymupdf.Rect(40, 20, 140, 24.32), color=None, fill=(0, 0, 0))  # 위 문장 줄 (지워짐)
    for x0, x1 in TIGHT:
        sp.draw_rect(pymupdf.Rect(x0 - 1.5, 38.5, x1 + 1.5, 47.5), color=None, fill=(1, 1, 1))  # 흰 테두리
        sp.draw_rect(pymupdf.Rect(x0, 40, x1, 46), color=None, fill=(0, 0, 0))
    for cx in TIGHT_SLASH_X:  # 지우기가 이름 줄 높이의 ‘/’ 끝도 지웠다
        ep.draw_rect(pymupdf.Rect(cx - 4, 39.5, cx + 3, 47.5), color=None, fill=(0.6, 0.6, 0.6))
    return (sdoc, sp), (edoc, ep)


def test_erased_clusters_drop_cut_picture_ends_and_halo_tails():
    (_s, sp), (_e, ep) = _tight_pages()
    cl = tc.erased_clusters(ep, sp, [20, 40, 104, 46], 6.0)
    assert len(cl) == 3
    assert [(u + v) / 2 for u, v in cl] == pytest.approx([29.0, 62.0, 95.0], abs=0.6)


def test_compose_tight_labels_with_cut_slash_ends():
    (_s, sp), (_e, ep) = _tight_pages()
    lay = {"lines": [_ln(40, 20, 140, 4.32), _ln(20, 40, 104, 6.0)], "dividers_pt": [], "icons": []}
    rec = compose_layout_region(ep, (10, 10, 170, 70), KO, {}, lay, source_text=SRC, src_page=sp)
    assert rec["lines"][1:] == ["공방", "마차", "상관"]  # 잘려 지워진 ‘/’ 그림 끝을 글자 ‘/’로 그리지 않는다
    assert _centers(rec["line_boxes_pt"][1:]) == pytest.approx([29.0, 62.0, 95.0], abs=0.6)


# ---------------------------------------------------------------- 한글 넣기
def test_compose_puts_each_label_under_its_picture():
    (_s, sp), (_e, ep) = _pages()
    rec = compose_layout_region(ep, (10, 10, 170, 70), KO, {}, _layout(), source_text=SRC, src_page=sp)
    assert rec["lines"] == ["다음 중 최대 2개를 선택하여 실행", "공방", "마차", "상관"]  # ‘/’ 그림은 그대로, 글자로 다시 그리지 않음
    assert _centers(rec["line_boxes_pt"][1:]) == pytest.approx([29.0, 85.0, 140.0], abs=0.6)
    assert rec["line_modes"].count("segment") == 3
    assert not rec["overflow_text"]
    # 이름 줄 높이는 원문 줄 자리 그대로
    for b in rec["line_boxes_pt"][1:]:
        assert 38.0 < (b[1] + b[3]) / 2 < 48.0


def test_compose_draws_text_slashes_on_their_place():
    (_s, sp), (_e, ep) = _pages(slash_text=True)
    rec = compose_layout_region(ep, (10, 10, 170, 70), KO, {}, _layout(), source_text=SRC, src_page=sp)
    assert rec["lines"][1:] == ["공방", "/", "마차", "/", "상관"]
    assert _centers(rec["line_boxes_pt"][1:]) == pytest.approx([29.0, 56.75, 85.0, 112.75, 140.0], abs=0.6)


@pytest.mark.parametrize("ko, clusters", [
    ("다음 중 최대 2개를 선택하여 실행\n공방 / 마차", CLUSTERS),  # 번역 조각 수가 다름
    (KO, CLUSTERS[:2]),  # 지운 덩어리 수가 다름
    (KO, [(20.0, 150.0)]),  # 보통 줄
])
def test_compose_falls_back_to_normal_line_exactly(ko, clusters):
    """맞지 않으면 오늘과 똑같이 (원본 쪽 없이 넣은 결과와 같다)."""
    (_s, sp), (_e, ep) = _pages(clusters)
    rec = compose_layout_region(ep, (10, 10, 170, 70), ko, {}, _layout(), source_text=SRC, src_page=sp)
    _d = pymupdf.open()
    plain = _d.new_page(width=180, height=80)
    ref = compose_layout_region(plain, (10, 10, 170, 70), ko, {}, _layout(), source_text=SRC)
    assert rec["lines"] == ref["lines"]
    assert rec["line_boxes_pt"] == ref["line_boxes_pt"]
    assert "segment" not in rec["line_modes"]


def test_explicit_alignment_keeps_normal_line():
    (_s, sp), (_e, ep) = _pages()
    rec = compose_layout_region(ep, (10, 10, 170, 70), KO, {"align": "center"}, _layout(), source_text=SRC, src_page=sp)
    assert rec["lines"][1] == "공방 / 마차 / 상관"
