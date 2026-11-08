"""글자 모양만 지우기 (app.erase_text): 글자 픽셀만 고르고, 그 자리만 채운다."""

from __future__ import annotations

import cv2
import numpy as np

from app import erase_text


def _paper(h=160, w=360, seed=0, amp=3.0, base=(235, 225, 205)):
    rng = np.random.default_rng(seed)
    img = np.empty((h, w, 3), np.float32)
    img[:] = base
    img += cv2.GaussianBlur(rng.standard_normal((h, w)).astype(np.float32), (0, 0), 2)[..., None] * amp
    return np.clip(img, 0, 255).astype(np.uint8)


def _text(img, org, color, scale=1.4, thick=4, s="TEXT"):
    cv2.putText(img, s, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, thick, cv2.LINE_AA)


def test_paper_text_only_glyph_pixels_and_keeps_divider_and_art_at_edge():
    img = _paper()
    _text(img, (90, 80), (210, 40, 30))
    cv2.line(img, (40, 130), (320, 130), (40, 40, 40), 3)  # 가로 구분선: 남긴다
    img[:14, :] = (30, 30, 30)  # 상자 위쪽에 걸친 어두운 그림
    img[4:10, 100:140] = (240, 200, 40)
    box = (20, 8, 340, 150)
    tm = erase_text.text_mask(img, box)
    ink = np.abs(img.astype(int) - np.array([210, 40, 30])).max(axis=2) < 60
    assert not tm.busy
    assert tm.mask[ink].mean() > 0.95  # 글자 픽셀은 거의 다
    assert tm.mask[box[1]:box[3], box[0]:box[2]].mean() < 0.25  # 상자 전체를 덮지 않음
    assert not tm.mask[128:133, 60:300].any()  # 구분선은 남김
    assert not tm.mask[:14].any()  # 위쪽 그림 조각은 남김
    out, used, changed = erase_text.fill_text(img, tm, "auto", box)
    assert used in ("solid", "pattern")
    assert np.array_equal(out[~changed], img[~changed])  # 글자 자리 밖은 그대로
    left = np.abs(out.astype(int) - np.array([210, 40, 30])).max(axis=2) < 60
    assert left.sum() < 20  # 빨간 글자가 남지 않음


def _glass(h=120, w=420, seed=1):
    """스테인드글라스 비슷한 그림: 색 조각 + 검은 납선."""
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


def test_busy_picture_finds_white_outlined_text():
    img = _glass()
    _text(img, (110, 78), (30, 30, 30), 1.5, 12)  # 검은 테두리
    _text(img, (110, 78), (245, 245, 245), 1.5, 6)  # 흰 글자
    box = (40, 20, 380, 100)
    tm = erase_text.text_mask(img, box)
    white = (img.min(axis=2) > 230)
    white[:, :40] = white[:, 380:] = False
    assert tm.busy
    assert tm.mask[white].mean() > 0.9
    assert tm.mask[box[1]:box[3], box[0]:box[2]].mean() < 0.45
    out, used, changed = erase_text.fill_text(img, tm, "auto", box)
    assert used == "inpaint" and erase_text.background_kind(img, tm) == "image"
    assert (out.min(axis=2) > 230)[box[1]:box[3], box[0]:box[2]].sum() < 30  # 흰 글자가 지워짐
    assert np.array_equal(out[~changed], img[~changed])


def test_rect_avg_fills_line_rectangles_with_surrounding_mean():
    img = _glass()
    _text(img, (110, 78), (245, 245, 245), 1.5, 6)
    box = (40, 20, 380, 100)
    tm = erase_text.text_mask(img, box)
    out, used, changed = erase_text.fill_text(img, tm, "rect_avg", box)
    assert used == "rect_avg"
    ys, xs = np.nonzero(changed)
    assert box[0] <= xs.min() and xs.max() < box[2] and box[1] <= ys.min() and ys.max() < box[3]  # 상자 안
    rect = out[ys.min():ys.max() + 1, xs.min():xs.max() + 1].reshape(-1, 3)
    assert len(np.unique(rect, axis=0)) <= 3  # 줄 사각형 하나 = 한 색
    assert np.array_equal(out[~changed], img[~changed])


def test_solid_background_uses_surrounding_color():
    img = np.full((100, 300, 3), (40, 90, 160), np.uint8)
    _text(img, (60, 65), (250, 250, 250))
    box = (20, 10, 280, 90)
    tm = erase_text.text_mask(img, box)
    out, used, _ = erase_text.fill_text(img, tm, "auto", box)
    assert used == "solid"
    assert np.abs(out[box[1]:box[3], box[0]:box[2]].astype(int) - (40, 90, 160)).max() < 25


def test_unknown_mode_falls_back_to_auto():
    img = _paper()
    _text(img, (90, 80), (30, 30, 30))
    tm = erase_text.text_mask(img, (20, 8, 340, 150))
    _out, used, _ = erase_text.fill_text(img, tm, "bogus")
    assert used in ("solid", "pattern")


def test_divider_ornament_kept_and_long_dash_on_picture_is_text():
    img = _paper(h=200)
    _text(img, (90, 70), (210, 40, 30))
    cv2.line(img, (40, 150), (160, 150), (40, 40, 40), 3)
    cv2.line(img, (200, 150), (320, 150), (40, 40, 40), 3)
    cv2.fillPoly(img, [np.array([[180, 144], [186, 150], [180, 156], [174, 150]])], (40, 40, 40))  # ◆
    tm = erase_text.text_mask(img, (20, 8, 340, 190))
    assert not tm.mask[143:158, 172:189].any()  # 구분선 가운데 장식은 남김
    g = _glass()
    cv2.line(g, (110, 60), (180, 60), (30, 30, 30), 14)  # 이름 띠의 긴 ‘ー’ (검은 테두리 + 흰 획)
    cv2.line(g, (110, 60), (180, 60), (245, 245, 245), 7)
    _text(g, (50, 78), (30, 30, 30), 1.5, 12, "A")  # 다른 글자도 있는 이름
    _text(g, (50, 78), (245, 245, 245), 1.5, 6, "A")
    _text(g, (255, 78), (30, 30, 30), 1.5, 12, "M")
    _text(g, (255, 78), (245, 245, 245), 1.5, 6, "M")
    tm = erase_text.text_mask(g, (40, 20, 380, 100))
    assert tm.busy and tm.mask[58:63, 112:178].mean() > 0.9


def test_digit_on_small_coin_is_not_label_text():
    """동전·배지 위 숫자(‘5’)는 색 라벨 위 글자가 아니다 (지우면 배지가 상하고 그 줄이 배지까지 넓게 잡혀
    가운데 정렬 제목이 왼쪽 정렬로 잡힌다: 이세길 A-097 제목)."""
    img = _paper(160, 420)
    cv2.rectangle(img, (4, 0), (96, 92), (150, 170, 165), -1)  # 영역 위로 걸친 회청색 배지
    cv2.circle(img, (60, 55), 24, (200, 165, 70), -1)  # 배지 위 금색 동전
    cv2.putText(img, "5", (51, 67), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (250, 248, 225), 3, cv2.LINE_AA)
    cv2.rectangle(img, (110, 95), (380, 140), (190, 60, 70), -1)  # 빨간 라벨 띠
    _text(img, (150, 130), (250, 250, 250), 1.1, 3, "LABEL")
    _text(img, (150, 70), (40, 40, 40), 1.2, 4, "TEXT")
    box = (2, 20, 410, 152)
    tm = erase_text.text_mask(img, box)
    five = np.zeros(img.shape[:2], bool)
    five[35:75, 40:80] = (img[35:75, 40:80].astype(int).min(axis=2) > 215)
    assert tm.mask[five].mean() < 0.1  # 동전 위 숫자는 남긴다
    dark = np.zeros(img.shape[:2], bool)
    dark[40:75, 140:300] = img[40:75, 140:300].astype(int).max(axis=2) < 90
    assert tm.mask[dark].mean() > 0.95  # 종이 위 글자는 지운다
    lay = erase_text.text_layout(img, tm, box)
    assert min(ln["bbox"][0] for ln in lay["lines"]) > 100  # 줄이 동전까지 넓어지지 않는다


def test_thin_glyph_stroke_next_to_text_is_erased_with_ruby_above():
    """윗첨자(루비)로 글자 크기가 작게 잡혀도, 글자 옆의 가는 획(‘ド’의 ト, ‘魔’의 广)은 테두리 조각이 아니라 글자다."""
    img = _paper(200, 460)
    _text(img, (120, 60), (40, 40, 40), 0.9, 2, "abcdefghijk")  # 루비 (본문 글자의 절반쯤)
    cv2.line(img, (100, 118), (100, 165), (40, 40, 40), 3)  # 가는 획: 세로 + 짧은 가로
    cv2.line(img, (100, 138), (122, 146), (40, 40, 40), 3)
    _text(img, (130, 165), (40, 40, 40), 1.9, 5, "TEXT")
    box = (20, 15, 440, 190)
    tm = erase_text.text_mask(img, box)
    assert tm.mask[120:163, 99:102].mean() > 0.9


def test_widest_line_of_centered_block_stays_centered():
    lines = [{"bbox": [40, 0, 60, 10], "align": "center"}, {"bbox": [10, 20, 90, 30], "align": "left"},
             {"bbox": [35, 40, 65, 50], "align": "center"}]
    erase_text._center_widest(lines, 0, 100, [])
    assert [ln["align"] for ln in lines] == ["center"] * 3
    left = [{"bbox": [10, 0, 60, 10], "align": "left"}, {"bbox": [10, 20, 90, 30], "align": "left"}]
    erase_text._center_widest(left, 0, 100, [])
    assert [ln["align"] for ln in left] == ["left", "left"]  # 왼쪽 정렬 글은 그대로
