import cv2
import numpy as np
import pytest

from app.erase import (
    CleanParams,
    clean_area,
    find_tag_labels,
    fit_corner_radius,
    hex_to_rgb01,
    label_like_mask,
    pushpull_fill,
    rgb_to_hex,
    transfer_texture,
)


def rounded_rect_mask(w: int, h: int, r: int) -> np.ndarray:
    """정확한 둥근 사각형 마스크 (픽셀 중심 기준)."""
    ys, xs = np.mgrid[0:h, 0:w] + 0.5
    cx = np.clip(xs, r, w - r)
    cy = np.clip(ys, r, h - r)
    return (xs - cx) ** 2 + (ys - cy) ** 2 <= r * r


@pytest.mark.parametrize("w,h,r", [(140, 33, 16), (120, 30, 8), (80, 30, 0), (60, 20, 5)])
def test_fit_corner_radius(w, h, r):
    got = fit_corner_radius(rounded_rect_mask(w, h, r))
    assert got == pytest.approx(r, abs=1.2)


def test_hex_roundtrip():
    assert rgb_to_hex((32, 64, 120)) == "#204078"
    assert hex_to_rgb01("#204078") == pytest.approx((32 / 255, 64 / 255, 120 / 255))


def test_label_like_mask_colors():
    px = np.array(
        [[
            [32, 61, 121],    # 파랑 라벨
            [143, 78, 158],   # 보라 라벨
            [64, 58, 70],     # 검정 라벨
            [85, 105, 85],    # 초록 라벨
            [235, 230, 232],  # 종이
            [150, 159, 166],  # 회색 연기
            [255, 255, 255],  # 흰 글자
        ]],
        np.uint8,
    )
    assert label_like_mask(px)[0].tolist() == [True, True, True, True, False, False, False]


def _paper(h, w, seed=0):
    rng = np.random.default_rng(seed)
    img = np.full((h, w, 3), (236, 231, 230), np.float32)
    img += rng.normal(0, 1.5, (h, w, 1))
    return np.clip(img, 0, 255).astype(np.uint8)


def test_find_tag_labels_with_white_text_cutting_edges():
    img = _paper(60, 340)
    for x0, color in ((10, (32, 64, 120)), (175, (85, 105, 85))):
        m = rounded_rect_mask(140, 33, 16)
        sub = img[12:45, x0 : x0 + 140]
        sub[m] = color
    # 라벨 1: 가운데 흰 글자(닫힘), 라벨 2: 위아래 끝까지 닿는 흰 띠 (조각으로 갈라짐)
    img[20:37, 70:90] = 255
    img[12:45, 220:224] = 255
    img[12:45, 260:263] = 255
    labels = find_tag_labels(img, min_h=20, max_h=60)
    assert len(labels) == 2
    a, b = labels
    assert a.bbox == pytest.approx((10, 12, 150, 45), abs=1)
    assert b.bbox == pytest.approx((175, 12, 315, 45), abs=1)
    assert a.radius == pytest.approx(16, abs=1.5)
    assert rgb_to_hex(a.color) == "#204078"
    assert np.abs(np.array(b.color) - (85, 105, 85)).max() <= 3


def test_find_tag_labels_ignores_thin_and_round_shapes():
    img = _paper(60, 200)
    img[50:56, 10:190] = (10, 10, 10)  # 이름 글자 윗부분 같은 얇은 띠
    cv2.circle(img, (100, 25), 15, (32, 64, 120), -1)  # 원 (가로세로 비 부족)
    assert find_tag_labels(img, min_h=20, max_h=60) == []


def test_pushpull_keeps_known_and_interpolates_gradient():
    h, w = 64, 64
    yy = np.mgrid[0:h, 0:w][0].astype(np.float32)
    img = np.repeat((100 + yy * 2)[..., None], 3, axis=2)
    known = np.ones((h, w), bool)
    known[20:44, 20:44] = False
    out = pushpull_fill(img * known[..., None], known)
    assert np.allclose(out[known], img[known])
    # 구멍 가운데도 위→아래 그라데이션을 대략 따른다
    assert abs(out[32, 32, 0] - img[32, 32, 0]) < 12
    assert out[24, 32, 0] < out[40, 32, 0]


def test_clean_area_removes_dark_glyphs_on_gradient_and_respects_protect():
    h, w = 120, 300
    yy = np.mgrid[0:h, 0:w][0].astype(np.float32)
    base = np.stack([200 + yy * 0.3] * 3, axis=2)
    rng = np.random.default_rng(1)
    clean = np.clip(base + rng.normal(0, 1.5, (h, w, 1)), 0, 255).astype(np.uint8)
    img = clean.copy()
    img[40:80, 60:75] = 10      # 굵은 획
    img[40:48, 60:200] = 10
    img[50:56, 150:160] = 215   # 약한 얼룩 (밝은 사각 블록)
    img[:, 270:] = (135, 106, 124)  # 보호할 띠 (숫자 칸 색)
    work = np.zeros((h, w), bool)
    work[30:90, 40:285] = True
    protect = np.zeros((h, w), bool)
    protect[:, 265:] = True
    res = clean_area(img, work, np.zeros_like(work), protect, CleanParams(), seed=0)
    diff = np.abs(res.image.astype(int) - clean.astype(int)).max(axis=2)
    assert diff[30:90, 40:265].mean() < 4
    assert diff[30:90, 40:265].max() < 25
    assert np.array_equal(res.image[:, 265:], img[:, 265:])
    # 영역 밖은 그대로
    assert np.array_equal(res.image[:25], img[:25])


def test_transfer_texture_copies_clean_blocks_on_grid():
    rng = np.random.default_rng(0)
    H, W = 64, 64
    hp = rng.normal(0, 3, (H, W, 3)).astype(np.float32)
    src = np.ones((H, W), bool)
    src[16:40, 16:48] = False  # 구멍
    g = transfer_texture(hp, src, (16, 40, 16, 48), rng, block=8, rows=24)
    assert g.shape == (24, 32, 3)
    # 블록마다 깨끗한 8x8 블록 하나를 그대로 복사한다
    tile = g[0:8, 0:8]
    found = any(np.array_equal(hp[y:y + 8, x:x + 8], tile)
                for y in range(0, H, 8) for x in range(0, W, 8) if src[y:y + 8, x:x + 8].all())
    assert found


def test_transfer_texture_none_without_clean_blocks():
    rng = np.random.default_rng(0)
    hp = np.zeros((16, 16, 3), np.float32)
    assert transfer_texture(hp, np.zeros((16, 16), bool), (0, 16, 0, 16), rng) is None
