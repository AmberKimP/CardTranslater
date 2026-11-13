"""그림(일러스트) 배경 위 글자 지우기 (app.erase의 picture_text_mask · directional_fill · fill_picture).

합성 그림으로 본다: 비스듬한 색 띠가 지나가는 연한 그림 위에, 흰 번짐을 두른 어두운 글자 줄과
외곽선·색 몸통이 있는 아이콘을 그린다.
"""

from __future__ import annotations

import cv2
import numpy as np
import pymupdf
import pytest

from app import erase, erase_picture
from app.regions import RatioRect, Region, RegionTemplate

W, H = 360, 150
BOX = (20, 20, 340, 130)


def _art(seed: int = 0) -> np.ndarray:
    """연한 그림: 세로 그라데이션 + 비스듬한 밝은 띠 + 스캔 잡음 (국소 표준편차가 커서 '그림 배경'으로 잡힌다)."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    img = np.empty((H, W, 3), np.float32)
    img[..., 0] = 205 + 20 * np.sin(xx / 23.0) + 10 * np.cos(yy / 9.0)
    img[..., 1] = 200 + 18 * np.sin((xx + yy) / 17.0)
    img[..., 2] = 225 + 12 * np.cos(xx / 31.0)
    band = np.abs((xx - 2.0 * yy) % 90 - 45) < 9  # 비스듬한 띠 (기울기 2)
    img[band] += 28
    img += rng.normal(0, 3.0, img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


def _glyph_row(img: np.ndarray, x0: int, y0: int, n: int, size: int = 10, color=(45, 40, 42),
               glow: bool = True) -> np.ndarray:
    """글자 비슷한 덩어리(ㅁ·ㅏ·ㅗ 모양) n개를 한 줄로 그린다. 글자 픽셀 마스크를 돌려준다."""
    ink = np.zeros(img.shape[:2], np.uint8)
    for k in range(n):
        x = x0 + k * (size + 3)
        kind = k % 3
        if kind == 0:
            cv2.rectangle(ink, (x, y0), (x + size - 2, y0 + size - 1), 1, 1)
        elif kind == 1:
            cv2.line(ink, (x + 3, y0), (x + 3, y0 + size - 1), 1, 2)
            cv2.line(ink, (x + 3, y0 + size // 2), (x + size - 2, y0 + size // 2), 1, 1)
        else:
            cv2.line(ink, (x, y0 + size - 2), (x + size - 2, y0 + size - 2), 1, 2)
            cv2.line(ink, (x + size // 2 - 1, y0 + 1), (x + size // 2 - 1, y0 + size - 2), 1, 1)
    ink = ink.astype(bool)
    if glow:  # 흰 번짐 2px
        g = cv2.dilate(ink.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool) & ~ink
        img[g] = (0.3 * img[g] + 0.7 * 255).astype(np.uint8)
    img[ink] = color
    return ink


def _icon(img: np.ndarray, x: int, y: int, s: int = 34) -> np.ndarray:
    """아이콘: 어두운 외곽선 + 갈색 몸통 + 속 무늬(작은 네모 줄). 아이콘 픽셀 마스크."""
    m = np.zeros(img.shape[:2], np.uint8)
    cv2.rectangle(m, (x, y), (x + s, y + s), 1, -1)
    img[m.astype(bool)] = (150, 90, 60)
    cv2.rectangle(img, (x, y), (x + s, y + s), (40, 30, 30), 2)
    for k in range(5):  # 속 무늬: 글자 크기와 비슷한 간격의 어두운 네모들 (글자 줄로 읽히면 안 된다)
        cv2.rectangle(img, (x + 4 + 6 * k, y + 14), (x + 7 + 6 * k, y + 20), (40, 30, 30), -1)
    return cv2.dilate(m, np.ones((5, 5), np.uint8)).astype(bool)


def test_finds_text_rows_on_picture_and_covers_glow():
    img = _art()
    ink1 = _glyph_row(img, 40, 40, 16)
    ink2 = _glyph_row(img, 40, 62, 12)
    pic = erase_picture.picture_text_mask(img, BOX)
    assert pic is not None and pic.polarity == -1
    assert len(pic.rows) == 2
    assert 8 <= pic.glyph_px <= 12
    ink = ink1 | ink2
    assert (pic.mask & ink).sum() >= 0.99 * ink.sum()  # 글자는 모두
    glow = cv2.dilate(ink.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
    assert (pic.mask & glow).sum() >= 0.95 * glow.sum()  # 흰 번짐까지
    far = ~cv2.dilate(ink.astype(np.uint8), np.ones((15, 15), np.uint8)).astype(bool)
    assert not (pic.mask & far).any()  # 글자에서 먼 그림은 건드리지 않는다


def test_red_emphasis_text_is_text_too():
    """빨간 강조 글자: 밝기로는 흐리지만 가장 어두운 채널로 보면 검정 글자만큼 뚜렷하다."""
    img = _art(1)
    black = _glyph_row(img, 40, 40, 8)
    red = _glyph_row(img, 40 + 8 * 13, 40, 8, color=(225, 45, 55))
    pic = erase_picture.picture_text_mask(img, BOX)
    assert pic is not None and len(pic.rows) == 1
    assert (pic.mask & red).sum() >= 0.97 * red.sum()
    assert (pic.mask & black).sum() >= 0.99 * black.sum()


def test_icon_and_its_inner_pattern_are_kept():
    img = _art(2)
    ink = _glyph_row(img, 40, 30, 16)
    icon = _icon(img, 150, 60)
    pic = erase_picture.picture_text_mask(img, BOX)
    assert pic is not None
    assert len(pic.rows) == 1  # 아이콘 속 네모 줄은 글자 줄이 아니다
    assert not (pic.mask & icon).any()
    assert (pic.mask & ink).sum() >= 0.99 * ink.sum()
    out, changed = erase_picture.fill_picture(img, pic.mask, pic.guard, pic.glyph_px, avoid=pic.avoid)
    assert np.array_equal(out[icon], img[icon])  # 아이콘 픽셀은 그대로


def test_no_rows_when_only_icons():
    """글자 없이 배지 두 개만 있는 칸: 글자 줄을 찾지 않는다 (None)."""
    img = _art(3)
    _icon(img, 70, 50, 44)
    _icon(img, 220, 50, 44)
    assert erase_picture.picture_text_mask(img, BOX) is None


def test_label_under_icon_is_text_and_can_be_dropped():
    """아이콘 바로 아래 이름표: 글자 줄로 찾되 under_icon으로 표시하고, 원문 줄 수가 모자라면 그 줄부터 남긴다."""
    img = _art(4)
    main = _glyph_row(img, 40, 28, 16)
    _icon(img, 150, 48)
    label = _glyph_row(img, 150, 86, 3)  # 아이콘 바로 아래
    pic = erase_picture.picture_text_mask(img, BOX)
    assert pic is not None and len(pic.rows) == 2
    assert [r["under_icon"] for r in pic.rows] == [False, True]
    assert (pic.mask & label).sum() >= 0.9 * label.sum()
    kept = erase_picture.drop_label_rows(pic, 1)
    assert len(kept.rows) == 1
    assert not (kept.mask & label).any() and (kept.guard & label).sum() >= 0.9 * label.sum()
    assert (kept.mask & main).sum() >= 0.99 * main.sum()


def test_directional_fill_continues_diagonal_band():
    """가로 띠 모양 구멍을 가로지르는 비스듬한 경계가 계단 없이 이어진다 (세로 보간·일반 인페인팅보다 정확)."""
    yy, xx = np.mgrid[0:60, 0:200].astype(np.float32)
    img = np.where((xx - 2.0 * yy)[..., None] > 40, (230, 230, 245), (150, 160, 215)).astype(np.uint8)
    hole = np.zeros((60, 200), bool)
    hole[22:38, 10:190] = True
    out, failed = erase_picture.directional_fill(img, hole, ~hole)
    assert not failed.any()
    err = np.abs(out[hole] - img[hole].astype(np.float32)).mean()
    tel = cv2.inpaint(img, hole.astype(np.uint8) * 255, 5, cv2.INPAINT_TELEA)
    err_tel = np.abs(tel[hole].astype(np.float32) - img[hole]).mean()
    assert err < 3.0 and err < 0.5 * err_tel


def test_directional_fill_does_not_use_blocked_pixels():
    """재료가 아닌 픽셀(지킬 덩어리)의 색은 끌려오지 않는다: 위가 막힌 곳은 아래쪽 값으로만 채운다."""
    img = np.full((40, 60, 3), 200, np.uint8)
    img[:10] = (20, 20, 20)  # 위쪽 어두운 덩어리 (아이콘 외곽선)
    hole = np.zeros((40, 60), bool)
    hole[10:24, 5:55] = True
    src = ~hole
    src[:10] = False
    out, failed = erase_picture.directional_fill(img, hole, src)
    assert not failed.any()
    assert out[hole].min() > 190


def test_fill_picture_changes_only_mask_and_matches_surroundings():
    img = _art(5)
    clean = img.copy()
    _glyph_row(img, 40, 40, 16)
    _glyph_row(img, 40, 62, 12)
    pic = erase_picture.picture_text_mask(img, BOX)
    out, changed = erase_picture.fill_picture(img, pic.mask, pic.guard, pic.glyph_px, seed=1, avoid=pic.avoid)
    assert np.array_equal(out[~changed], img[~changed])
    assert not (changed & ~pic.mask).any()
    # 글자·번짐이 사라지고 원래 그림에 가깝다 (글자가 있을 때보다 훨씬)
    before = np.abs(img[changed].astype(np.float32) - clean[changed]).mean()
    after = np.abs(out[changed].astype(np.float32) - clean[changed]).mean()
    assert after < 9.0 and after < 0.35 * before
    # 결: 채운 자리가 매끈하게 뭉개지지 않고 주변과 비슷한 잡음이 있다
    lum = erase.luminance(out)
    hp = lum - cv2.GaussianBlur(lum, (0, 0), 1.2)
    inner = cv2.erode(changed.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
    assert 0.3 < hp[inner].std() < 6.0


# ---------------------------------------------------------------- erase_page (PDF 한 쪽): ‘복잡한 그림 배경’ 방식

def _page(img: np.ndarray):
    doc = pymupdf.open()
    h, w = img.shape[:2]
    page = doc.new_page(width=w * 0.48, height=h * 0.48)
    pix = pymupdf.Pixmap(pymupdf.csRGB, w, h, np.ascontiguousarray(img).tobytes(), 0)
    page.insert_image(page.rect, pixmap=pix)
    return doc, page


def _template(mode: str | None = "picture") -> RegionTemplate:
    reg = Region(name="본문", kind="text",
                 rect=RatioRect(BOX[0] / W, BOX[1] / H, (BOX[2] - BOX[0]) / W, (BOX[3] - BOX[1]) / H),
                 erase={"mode": mode} if mode else {})
    return RegionTemplate(regions=[reg], protect=[])


@pytest.fixture()
def picture_card():
    img = _art(6)
    ink = _glyph_row(img, 40, 40, 16) | _glyph_row(img, 40, 62, 12)
    icon = _icon(img, 250, 80, 30)
    return img, ink, icon


def test_erase_page_picture_mode_meta_and_pixels(picture_card):
    img, ink, icon = picture_card
    doc, page = _page(img)
    r = erase.erase_page(page, _template(), seed=3, sources={"본문": "가나다라마바사아자차카타파하가나\n가나다라마바사아자차카타"})
    m = r.meta["regions"][0]
    tmm = m["erase"]["text_mask"]
    assert tmm["mode"] == "picture" and tmm["used"] == "picture" and m["erase"]["method"] == "text_mask:picture"
    assert tmm["picture"]["detector"] == "rows" and tmm["picture"]["rows"] == 2 and tmm["picture"]["polarity"] == "dark"
    assert erase.PICTURE_BG_WARNING in m["erase"]["flags"]
    for key in ("mode", "used", "mask_ratio", "texture", "needs_choice", "notes", "few_text"):
        assert key in tmm  # 기존 메타 키 유지
    assert tmm["needs_choice"] is False  # 사람이 방식을 골랐다
    assert len(m["text_layout"]["lines"]) == 2
    out = r.cleaned
    assert np.array_equal(out[icon], img[icon])  # 아이콘은 그대로
    assert out[ink].min(axis=1).mean() > 170  # 글자 자리가 그림색으로 (어두운 글자색 ≈ 40)
    doc.close()


def test_erase_page_picture_mode_no_text_source_erases_nothing(picture_card):
    """읽은 원문에 글자가 없으면(비었거나 아이콘뿐) 아무것도 지우지 않는다."""
    img, _ink, _icon_m = picture_card
    for src in ("", "[[scroll_red]] [[scroll_blue]]"):
        doc, page = _page(img)
        r = erase.erase_page(page, _template(), seed=3, sources={"본문": src})
        assert np.array_equal(r.cleaned, img)
        assert r.meta["patches"] == []
        tmm = r.meta["regions"][0]["erase"]["text_mask"]
        assert tmm["mask_ratio"] == 0.0
        doc.close()


def test_auto_mode_uses_picture_module_on_picture_background(picture_card, monkeypatch):
    """‘자동’도 배경이 그림이면 복잡한 그림 배경 방식으로 지운다. 다른 방식(뒤 그림 유추 등)과 고른 배경은 쓰지 않는다."""
    img, ink, icon = picture_card
    src = {"본문": "가나다라마바사아자차카타파하가나"}
    for mode in (None, "auto"):
        doc, page = _page(img)
        r = erase.erase_page(page, _template(mode), seed=3, sources=src)
        tmm = r.meta["regions"][0]["erase"]["text_mask"]
        assert tmm["used"] == "picture" and tmm["picture"]["detector"] == "rows"
        assert np.array_equal(r.cleaned[icon], img[icon])
        doc.close()
    called = []
    real = erase_picture.region_text_mask
    monkeypatch.setattr(erase_picture, "region_text_mask", lambda *a, **k: called.append(1) or real(*a, **k))
    doc, page = _page(img)
    r = erase.erase_page(page, _template("inpaint"), seed=3, sources=src)
    assert "picture" not in r.meta["regions"][0]["erase"]["text_mask"]
    doc.close()
    flat = np.clip(np.full((H, W, 3), 244, np.uint8) + np.random.default_rng(0).normal(0, 1.2, (H, W, 3)), 0, 255)
    flat = flat.astype(np.uint8)
    _glyph_row(flat, 40, 40, 16, glow=False)
    doc, page = _page(flat)
    r = erase.erase_page(page, _template("auto"), seed=3, sources=src)
    tmm = r.meta["regions"][0]["erase"]["text_mask"]
    assert "picture" not in tmm and tmm["used"] == "solid"
    doc.close()
    assert called == []


def test_auto_mode_falls_back_when_picture_rows_not_found(picture_card, monkeypatch):
    """복잡한 그림 배경 방식이 글자 줄을 못 찾으면 ‘자동’은 기존 방식으로 지운다."""
    img, _ink, _icon_m = picture_card
    monkeypatch.setattr(erase_picture, "picture_text_mask", lambda *a, **k: None)
    doc, page = _page(img)
    r = erase.erase_page(page, _template("auto"), seed=3, sources={"본문": "가나다라마바사아자차카타파하가나"})
    tmm = r.meta["regions"][0]["erase"]["text_mask"]
    assert tmm["used"] != "picture" and tmm["picture"]["detector"] == "color" and tmm["mask_ratio"] > 0
    doc.close()


def test_picture_mode_listed_for_ui():
    from app import service

    assert dict(service.ERASE_MODES)["picture"].startswith("복잡한 그림 배경")
