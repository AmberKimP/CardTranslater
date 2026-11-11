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


def test_tight_lines_are_split_and_thin_bits_join_their_line():
    """줄 사이가 2~3px뿐인 본문도 줄마다 나눈다 (엘프 A 5쪽: 세 줄이 한 줄로 잡혀 한글이 25pt로 넘침).
    탁점·점처럼 얇은 조각은 가까운 줄에 붙는다."""
    bands = [[89, 120], [123, 153], [157, 184]]
    assert erase_text._merge_thin_bands(bands, 25.0, 3) == bands  # 두꺼운 줄끼리는 붙이지 않는다
    with_dots = [[60, 64], [66, 95], [99, 128]]  # 위 탁점(5px) + 두 줄
    assert erase_text._merge_thin_bands(with_dots, 25.0, 3) == [[60, 95], [99, 128]]
    far = [[10, 14], [40, 70]]  # 멀리 떨어진 얇은 조각은 그대로 (뒤에서 걸러진다)
    assert erase_text._merge_thin_bands(far, 25.0, 3) == far


def test_text_row_group_found_when_frame_shares_text_color():
    """상자 바로 밖(띠 테두리·카드 검은 테두리)에 글자색이 많아도, 한 줄로 늘어선 색 묶음은 글자다 (엘프 A 제목)."""
    def comp(x, y, w, h):
        m = np.zeros((120, 520), bool)
        m[y:y + h, x:x + w] = True
        return (m, np.array([30.0, 25, 20]), int(m.sum()), max(w, h))

    text = {"color": np.array([30.0, 25, 20]), "items": [comp(150 + 50 * i, 45, 40, 42) for i in range(5)],
            "area": 5 * 40 * 42}
    art = {"color": np.array([200.0, 180, 150]), "items": [comp(30, 20, 30, 80), comp(300, 10, 20, 90)], "area": 4200}
    box = (24, 24, 500, 110)
    assert erase_text._text_row_group([art, text], box) is text
    edge = {**text, "items": text["items"] + [comp(24, 50, 20, 30)]}  # 왼쪽 끝에 걸친 조각이 있으면 아니다
    assert erase_text._text_row_group([edge], box) is None


def test_single_inset_line_is_centered():
    """묶음에 줄이 하나이고 양옆이 비면 가운데 정렬 (영역을 한쪽으로 넓게 그려도: 엘프 A 제목)."""
    img = _paper(120, 520)
    _text(img, (120, 80), (40, 40, 40), 1.6, 5, "TITLE")
    box = (20, 20, 500, 110)
    tm = erase_text.text_mask(img, box)
    lay = erase_text.text_layout(img, tm, box)
    assert len(lay["lines"]) == 1 and lay["lines"][0]["align"] == "center"


# ---------------------------------------------------------------- 글자 상자로 좁히기 (엘프: 영역이 그림·테두리까지 걸침)
def _card_with_box(h=200, w=400, box_top=120, art=(60, 30, 20), paper=(235, 232, 225)):
    img = np.zeros((h, w, 3), np.uint8)
    img[:] = art
    rng = np.random.default_rng(3)
    img[:box_top] = np.clip(np.array(art) + rng.integers(-25, 25, (box_top, w, 3)), 0, 255)  # 위쪽 그림
    img[box_top:h - 10, 10:w - 10] = paper  # 밝은 글자 상자
    cv2.putText(img, "TEXT LINE", (40, box_top + 45), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (20, 20, 20), 3, cv2.LINE_AA)
    return img


def test_label_panel_narrows_region_to_light_box_only_when_region_overhangs():
    from app import erase_text as et

    img = _card_with_box()
    region = (5, 40, 395, 195)  # 위쪽 그림까지 걸친 영역
    x0, y0, x1, y1 = et.label_panel(img, region)
    assert abs(y0 - 120) <= 2 and y1 <= 195 and x0 >= 5  # 상자 위끝으로 좁힌다
    tight = (10, 120, 390, 190)
    assert et.label_panel(img, tight) == tight  # 이미 상자에 맞으면 그대로
    dark = _card_with_box(paper=(40, 40, 60))
    assert et.label_panel(dark, region) == region  # 어두운 띠(흰 글자)는 그대로
    white_bg = _card_with_box(art=(240, 238, 232))
    assert et.label_panel(white_bg, region) == region  # 흰 바탕 카드: 빼는 부분도 상자색이라 그대로
    et.PANEL_NARROW = False
    try:
        assert et.label_panel(img, region) == region  # 스위치로 끌 수 있다
    finally:
        et.PANEL_NARROW = True


def test_thin_title_bar_text_found_after_painting_outside():
    """얇은 흰 띠 위아래가 검은 테두리: 상자 밖을 상자색으로 칠한 사본에서 찾으면 글자가 띠 가장자리와 붙지 않는다."""
    from app import erase_text as et

    img = np.full((110, 600, 3), 25, np.uint8)  # 검은 테두리
    img[30:80, 20:580] = (232, 230, 226)  # 얇은 흰 띠
    cv2.putText(img, "TITLE", (220, 70), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (15, 15, 15), 4, cv2.LINE_AA)
    region = (15, 15, 585, 95)
    panel = et.label_panel(img, region)
    assert panel != region and abs(panel[1] - 30) <= 2 and abs(panel[3] - 80) <= 2
    look = et.paint_outside(img, panel)
    tm = et.text_mask(look, panel, None, size_box=region)
    lay = et.text_layout(look, tm, panel)
    assert len(lay["lines"]) == 1 and 200 <= lay["lines"][0]["bbox"][0] <= 230


def test_extra_line_at_box_edge_is_dropped_first():
    """원문 한 줄인데 줄이 둘: 상자 아래 끝에 붙은 줄(띠 안으로 들어온 그림 조각)을 뺀다 (글자가 더 커 보여도)."""
    from app.erase_text import _extra_lines

    title = {"bbox": [386, 46, 474, 89], "glyph_h": 11.8}
    art = {"bbox": [394, 88, 475, 103], "glyph_h": 14.0}
    assert _extra_lines([title, art], ["延焼"], (35, 103)) == [art]
    assert _extra_lines([title, art], ["延焼"]) == [title]  # 상자를 모르면 예전처럼 작은 줄부터


def _tm(mask):
    return erase_text.TextMask(mask=mask, core=mask.copy(), busy=False, texture=0.0)


def test_trim_stray_end_keeps_piece_the_source_needs():
    """줄 앞 조각이 떨어져 있어도, 빼면 줄이 원문 길이보다 많이 짧아지면 원문 글자다 (연한 글자가 덜 잡힌 줄)."""
    m = np.zeros((40, 400), bool)
    m[10:30, 10:30] = True  # 왼쪽 조각 (글자 하나)
    m[10:30, 90:300] = True  # 나머지 줄
    ln = {"bbox": [10, 10, 300, 30], "glyph_h": 20}
    assert not erase_text._trim_stray_end(_tm(m), dict(ln), "自由民と奴隷、見ただけでその違いが分かるかね")
    # 원문이 짧으면(배지 숫자 같은 원문에 없는 조각) 예전처럼 뺀다
    ln2 = dict(ln)
    assert erase_text._trim_stray_end(_tm(m.copy()), ln2, "緊急クエスト")
    assert ln2["bbox"][0] == 90


def test_fill_line_gaps_finds_faint_glyphs_of_line_color_only():
    img = np.full((40, 400, 3), 200, np.uint8)
    img[12:28, 40:200:6] = (130, 130, 130)  # 연한 회색 글자 (마스크가 놓친 것)
    img[12:28, 220:224] = (230, 200, 40)  # 다른 색(노란 테두리 조각): 더하지 않는다
    m = np.zeros((40, 400), bool)
    m[12:28, 10:30] = True
    m[12:28, 300:380] = True
    tm = _tm(m)
    ln = {"bbox": [10, 12, 380, 28], "glyph_h": 16, "color": "#828282"}
    assert erase_text.fill_line_gaps(tm, img, ln) > 0
    assert tm.mask[12:28, 40:200].any() and not tm.mask[12:28, 221:223].any()


def test_gap_fill_runs_after_trim_and_respects_allowed_area():
    """배지 숫자(원문에 없는 조각)를 뺀 틈은 채우지 않고, 지워도 되는 곳(allowed) 밖은 더하지 않는다."""
    img = np.full((40, 600, 3), 200, np.uint8)
    img[12:28, 40:100:6] = (60, 60, 60)  # 배지와 제목 사이 그림 (글자색과 같은 진한 색)
    m = np.zeros((40, 600), bool)
    m[12:28, 10:28] = True  # 배지 숫자
    m[12:28, 150:420] = True  # 제목
    tm = _tm(m)
    lay = {"lines": [{"bbox": [10, 12, 420, 28], "glyph_h": 16, "color": "#3c3c3c"}]}
    erase_text.match_source_lines(tm, lay, "緊急クエスト", img=img, allowed_px=np.ones_like(m))
    assert not tm.mask[12:28, 40:100].any()  # 뺀 조각과 줄 사이 그림은 지우지 않는다
    img2 = np.full((40, 400, 3), 200, np.uint8)
    img2[12:28, 40:200:6] = (130, 130, 130)
    m2 = np.zeros((40, 400), bool)
    m2[12:28, 10:30] = True
    m2[12:28, 300:380] = True
    ok = np.ones_like(m2)
    ok[:, 100:] = False
    tm2 = _tm(m2)
    erase_text.fill_line_gaps(tm2, img2, {"bbox": [10, 12, 380, 28], "glyph_h": 16, "color": "#828282"}, allowed=ok)
    assert tm2.mask[12:28, 40:100].any() and not tm2.mask[12:28, 100:200].any()
