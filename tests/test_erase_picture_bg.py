"""그림 배경 위 글자 지우기 (예외 처리): 획 모양으로 찾기, 그림자까지 넓히기, 채우기."""

import cv2
import numpy as np

from app import erase


def _picture(h=120, w=360, seed=1):
    """결이 있는 그림 배경 (밝은 편, 큰 무늬 + 잔결)."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    base = 190 + 25 * np.sin(xx / 23.0) * np.cos(yy / 17.0)
    noise = cv2.GaussianBlur(rng.normal(0, 8, (h, w)).astype(np.float32), (0, 0), 1.5)
    g = np.clip(base + noise, 0, 255).astype(np.uint8)
    return np.dstack([g, g, np.clip(g.astype(int) + 10, 0, 255).astype(np.uint8)])


def _write(img, text, org, scale=0.9, color=(30, 30, 30), thick=2):
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, thick, cv2.LINE_AA)


def test_picture_text_mask_finds_dark_text_line():
    img = _picture()
    _write(img, "ABCDEFGH KLMN", (30, 70))
    box = (10, 30, 350, 100)
    tm = erase.picture_text_mask(img, box)
    assert tm is not None
    ys, xs = np.nonzero(tm.core)
    # 글자 줄 자리(y 46~71, x 30~244)에서 찾는다: 첫 글자부터 끝 글자까지
    assert 45 <= np.median(ys) <= 75
    assert xs.min() < 50 and xs.max() > 220
    assert tm.mask.sum() >= tm.core.sum()


def test_picture_text_mask_none_without_text():
    assert erase.picture_text_mask(_picture(), (10, 30, 350, 100)) is None


def test_fill_picture_keeps_pixels_outside_mask():
    img = _picture()
    _write(img, "ABCDEFGH", (30, 70))
    mask = np.zeros(img.shape[:2], bool)
    mask[45:80, 25:200] = True
    out = erase.fill_picture(img, mask, seed=3)
    assert np.array_equal(out[~mask], img[~mask])
    # 채운 자리는 둘레 그림 밝기에 가깝다 (검은 글자가 남지 않는다)
    assert erase.luminance(out)[mask].mean() > 150


def test_grow_halo_extends_over_soft_shadow_only():
    h, w = 120, 300
    img = np.full((h, w, 3), 210, np.uint8)
    # 왼쪽 글자: 넓고 흐린 어두운 그림자 위. 오른쪽 글자: 그림자 없음
    shadow = np.zeros((h, w), np.float32)
    shadow[50:70, 20:120] = 1.0
    shadow = cv2.GaussianBlur(shadow, (0, 0), 4)
    img = np.clip(img.astype(np.float32) - 110 * shadow[..., None], 0, 255).astype(np.uint8)
    mask = np.zeros((h, w), bool)
    mask[55:65, 30:110] = True   # 그림자 안의 글자
    mask[55:65, 180:260] = True  # 그림자 없는 글자
    out = erase.grow_halo(img, mask)
    assert out[mask].all()
    left_grown = (out & ~mask)[:, :150].sum()
    right_grown = (out & ~mask)[:, 150:].sum()
    assert left_grown > 100
    assert right_grown == 0


def test_grow_halo_respects_protect():
    h, w = 100, 200
    img = np.full((h, w, 3), 210, np.uint8)
    shadow = np.zeros((h, w), np.float32)
    shadow[40:60, 40:160] = 1.0
    img = np.clip(img.astype(np.float32) - 110 * cv2.GaussianBlur(shadow, (0, 0), 4)[..., None], 0, 255).astype(np.uint8)
    mask = np.zeros((h, w), bool)
    mask[45:55, 50:150] = True
    protect = np.zeros((h, w), bool)
    protect[:45, :] = True  # 위쪽(아이콘)은 건드리지 않는다
    out = erase.grow_halo(img, mask, protect)
    assert not (out & protect).any()
    assert (out & ~mask).any()


def test_prefer_picture_mask():
    from app import erase_text

    shape = (60, 200)
    core = np.zeros(shape, bool)
    core[25:35, 20:180] = True
    alt = erase_text.TextMask(core.copy(), core.copy(), True, 10.0, 10.0, [], [])
    good = erase_text.TextMask(core.copy(), core.copy(), True, 10.0, 10.0, [], [])
    missed = erase_text.TextMask(np.zeros(shape, bool), np.zeros(shape, bool), True, 10.0, 10.0, [], [])
    smear = np.ones(shape, bool)
    stray = erase_text.TextMask(smear, smear, True, 10.0, 10.0, [], [])
    assert not erase.prefer_picture_mask(good, alt)  # 기존 방식이 잘 맞으면 그대로
    assert erase.prefer_picture_mask(missed, alt)  # 글자를 놓쳤으면 바꾼다
    assert erase.prefer_picture_mask(stray, alt)  # 그림을 넓게 지우면 바꾼다
    assert not erase.prefer_picture_mask(good, None)
