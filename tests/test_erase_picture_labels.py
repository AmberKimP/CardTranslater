"""그림마다 이름표가 붙은 칸 (app.erase_picture): '工房 / 馬車 / 商館'처럼 그림 셋 아래에 짧은 이름표가 있고,
이름표 글자 밑에 어두운 그림자가 받쳐 있으며, 그 바로 위 그림 속에 글자처럼 줄지어 선 조각(문·바퀴)이 있다.

예전에는 그림자가 이름표 줄에 묶여 줄이 너무 높다고 빠지고, 그림 속 문 줄이 둘째 글 줄로 잡혀 그림을 지웠다
(공주 1쪽). 합성 그림으로 본다.
"""

from __future__ import annotations

import cv2
import numpy as np

from app import erase_picture

W, H = 520, 240
BOX = (20, 20, 500, 220)
SOURCE = "ずれかを2つまで選択し実行する\n工房 / 馬車 / 商館"
SH_CY, SH_RY, FRAG = 188, 10, (192, 212)  # 예전 방식이면 문 줄을 둘째 줄로 잡고 이름표는 남기던 모양


def _art(seed: int = 0) -> np.ndarray:
    """연한 그림 배경: 그라데이션 + 비스듬한 밝은 띠 + 스캔 잡음."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    img = np.empty((H, W, 3), np.float32)
    img[..., 0] = 215 + 15 * np.sin(xx / 23.0) + 8 * np.cos(yy / 9.0)
    img[..., 1] = 205 + 14 * np.sin((xx + yy) / 17.0)
    img[..., 2] = 225 + 10 * np.cos(xx / 31.0)
    img[np.abs((xx - 2.0 * yy) % 90 - 45) < 9] += 20
    img += rng.normal(0, 3.0, img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


def _glyphs(ink: np.ndarray, x0: int, y0: int, n: int, size: int) -> None:
    """글자 비슷한 덩어리(ㅁ·ㅏ·ㅗ 모양) n개를 ink에 그린다."""
    for k in range(n):
        x = x0 + k * (size + 3)
        kind = k % 3
        if kind == 0:
            cv2.rectangle(ink, (x, y0), (x + size - 2, y0 + size - 1), 1, 2)
        elif kind == 1:
            cv2.line(ink, (x + 3, y0), (x + 3, y0 + size - 1), 1, 2)
            cv2.line(ink, (x + 3, y0 + size // 2), (x + size - 2, y0 + size // 2), 1, 2)
        else:
            cv2.line(ink, (x, y0 + size - 2), (x + size - 2, y0 + size - 2), 1, 2)
            cv2.line(ink, (x + size // 2 - 1, y0 + 1), (x + size // 2 - 1, y0 + size - 2), 1, 2)


def _card(sh_cy: int = SH_CY, sh_ry: int = SH_RY, frag: tuple = FRAG):
    """(그림, 문장 글자, 이름표 글자, 그림 속 문 조각, 그림 전체) 마스크.
    sh_cy·sh_ry: 이름표 밑 그림자의 세로 가운데·반높이, frag: 첫 이름표 왼쪽 아래 그림 조각 (y0, y1) (옆 문장(紋章) 끝)."""
    img = _art()
    sentence = np.zeros((H, W), np.uint8)
    _glyphs(sentence, 60, 30, 18, 15)
    picture = np.zeros((H, W), np.uint8)
    doors = np.zeros((H, W), np.uint8)
    for k, x in enumerate((50, 200, 350)):
        cv2.rectangle(picture, (x, 70), (x + 120, 158), 1, 2)  # 그림 테두리 (글자보다 훨씬 큼)
        cv2.line(picture, (x + 6, 84), (x + 114, 84), 1, 2)  # 지붕선
        if k > 0:  # 둘째·셋째 그림의 문 (글자 크기의 '∩' 넷, 아래 테두리와는 떨어져 있다)
            for j in range(4):
                dx = x + 30 + j * 18
                cv2.ellipse(doors, (dx + 6, 128), (6, 6), 0, 180, 360, 1, 3)
                cv2.line(doors, (dx, 128), (dx, 146), 1, 3)
                cv2.line(doors, (dx + 12, 128), (dx + 12, 146), 1, 3)
    for x in (180, 330):  # 그림 사이의 굵은 '/' 그림
        cv2.line(picture, (x + 14, 80), (x, 175), 1, 5)
    labels = np.zeros((H, W), np.uint8)
    shadow = np.zeros((H, W), np.uint8)
    for x in (50, 200, 350):
        lx = x + 40
        _glyphs(labels, lx, 168, 2, 18)
        cv2.ellipse(shadow, (lx + 19, sh_cy), (30, sh_ry), 0, 0, 360, 1, -1)  # 이름표 밑을 받친 어두운 그림자
    if frag:  # 그림자에 이어져 같은 줄로 묶이는 옆 그림 조각 (줄을 더 높게 만든다)
        cv2.rectangle(picture, (90 - 26, frag[0]), (90 - 14, frag[1]), 1, 3)
    labels_b, shadow_b = labels.astype(bool), shadow.astype(bool)
    glow = cv2.dilate(labels, np.ones((5, 5), np.uint8)).astype(bool) & ~labels_b
    img[shadow_b] = (90, 80, 85)
    img[glow] = (245, 245, 245)
    img[labels_b] = (50, 45, 48)
    sb = sentence.astype(bool)
    sg = cv2.dilate(sentence, np.ones((5, 5), np.uint8)).astype(bool) & ~sb
    img[sg] = (0.3 * img[sg] + 0.7 * 255).astype(np.uint8)
    img[sb] = (50, 45, 48)
    img[picture.astype(bool)] = (60, 50, 45)
    img[doors.astype(bool)] = (70, 55, 45)
    return img, sb, labels_b, doors.astype(bool), (picture | doors).astype(bool)


def _region(img):
    import app.erase_text as et

    tm = et.TextMask(np.zeros((H, W), bool), np.zeros((H, W), bool), True, 10.0, 15.0, [], [])
    return erase_picture.region_text_mask(img, BOX, None, tm, SOURCE, True)


def test_label_row_with_shadow_is_text():
    """이름표 밑 그림자가 줄을 높게 만들어도 이름표 줄은 글자 줄이다 (그림자는 줄에 딸린 그림자로 붙는다)."""
    img, _sentence, labels, _doors, _pic = _card()
    pic = erase_picture.picture_text_mask(img, BOX, expected_rows=2)
    assert pic is not None
    label_rows = [r for r in pic.rows if r["box"][1] >= 160]
    assert label_rows, [r["box"] for r in pic.rows]
    assert (pic.mask & labels).sum() >= 0.9 * labels.sum()


def test_list_line_keeps_picture_row_untouched():
    """원문 둘째 줄이 '/' 목록(조각 셋)이면 묶음이 셋인 이름표 줄을 맞추고, 그림 속 문 줄은 지우지 않는다."""
    img, sentence, labels, doors, picture = _card()
    pic, tm = _region(img)
    assert pic is not None
    assert len(pic.rows) == 2
    assert pic.rows[0]["box"][3] < 60 and pic.rows[1]["box"][1] >= 160
    assert (tm.mask & sentence).sum() >= 0.95 * sentence.sum()
    assert (tm.mask & labels).sum() >= 0.9 * labels.sum()
    assert not (tm.mask & doors).any()
    assert (tm.mask & picture).sum() <= 0.01 * picture.sum()


def test_list_segments_and_clusters():
    assert erase_picture._list_segments("工房 / 馬車 / 商館") == 3
    assert erase_picture._list_segments("工房／馬車") == 2
    assert erase_picture._list_segments("選択し実行") == 1
    assert erase_picture._list_segments("[[coin]] / 工房") == 1
    core = np.zeros((40, 300), bool)
    core[10:25, 10:40] = True
    core[10:25, 120:150] = True
    core[10:25, 250:280] = True
    core[30:31, 200:202] = True  # 티끌은 세지 않는다
    assert erase_picture._row_clusters({"core": core}, 15.0) == 3


def test_no_list_line_leaves_rows_alone():
    """원문에 '/' 목록 줄이 없으면 keep_list_rows는 아무것도 바꾸지 않는다."""
    img, *_ = _card()
    pic = erase_picture.picture_text_mask(img, BOX, expected_rows=2)
    assert erase_picture.keep_list_rows(pic, ["選択し実行する", "工房 馬車 商館"]) is pic
