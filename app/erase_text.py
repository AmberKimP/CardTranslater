"""글자 모양만 지우기 (텍스트 레이어가 없는 그림 카드). PRD 5.4.

영역 상자 전체를 덮지 않고 **글자 픽셀만** 찾아, 그 픽셀만 주변에 맞춰 채운다.

글자 찾기 (``text_mask``)
  1. 큰 중앙값 흐림(글자 획보다 넓은 창)을 배경으로 보고, 배경과 크게 다른 픽셀을 밝은 쪽·어두운 쪽으로 나눠
     덩어리로 묶는다. 흰 글자 + 검은 테두리 + 스테인드글라스 납선이 한 덩어리로 붙지 않게 하려는 것이다.
  2. 글자가 아닌 덩어리는 뺀다: 상자 밖으로 이어지는 것(그림·선), 상자만큼 큰 것(테두리), 긴 가로선(구분선),
     글자 크기의 3배를 넘는 것(테두리 곡선).
  3. 상자 배경이 고르면(종이·무늬) 남은 덩어리 중 주변이 바쁜 것(상자에 걸친 그림 조각)을 빼되, 주요 글자색과 같은
     것(획이 빽빽한 한자)은 되살린다. 배경이 그림이면 덩어리를 색으로 묶어 ‘가장 넓고 상자 밖에 드문 색’ 묶음만 글자로 본다.
  4. 글자에 붙은 테두리·그림자(배경과 조금 다른 픽셀)를 몇 px 넓혀 포함하고, 글자 속 닫힌 빈칸(코인 아이콘 속 등)을 채운다.

글자 상자로 좁히기 (``label_panel``)
  영역이 밝고 고른 글자 상자(흰 띠·흰 박스)보다 크게 잡혀 그림·테두리까지 걸치면, 글자 찾기·지우기·줄 재기를
  그 상자 안으로만 한다 (영역 위쪽의 그림·띠 밖 테두리를 글자로 읽지 않게). 상자가 영역 대부분이거나, 밝은 상자가
  아니거나(흰 글자 + 어두운 띠), 상자 모양이 아니면 그대로 둔다.

채우기 (``fill_text``) — 영역 설정 ``erase.mode``(카드별로 바꿀 수 있음)
  auto      배경을 보고 고름: 단색 -> solid, 무늬 -> pattern, 그림 -> inpaint (그림이면 ‘방식 확인’ 표시)
  solid     글자 자리를 주변 색(매끄러운 배경 추정)으로 덮음
  pattern   글자 자리를 주변 무늬로 이어 채움 (복원, 좁은 반경)
  rect_avg  글자 줄마다 사각형으로 지우고 사각형 둘레의 평균색으로 채움
  inpaint   글자 뒤 그림을 주변에서 유추해 채움 (복원, 넓은 반경)
numpy 배열만 다룬다(PDF·화면을 모름).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import re
import cv2
import numpy as np

MODES = ("auto", "solid", "pattern", "rect_avg", "inpaint")
MODE_LABELS = {
    "auto": "자동 (배경 보고 고름)",
    "solid": "단색: 주변 색으로 덮기",
    "pattern": "무늬: 주변 무늬로 채우기",
    "rect_avg": "그림: 사각형 + 평균색",
    "inpaint": "그림: 뒤 그림 유추해 채우기",
    "picture": "복잡한 그림 배경: 획 모양으로 찾아 그림 결 따라 채우기",  # app.erase_picture. fill_text는 이 방식을 모른다: erase_page가 따로 채운다
}
STRONG = 60.0  # 배경과 이만큼(RGB 최대 채널) 다르면 글자 후보
WEAK = 30.0  # 글자에 붙은 테두리·그림자로 넓힐 때의 기준
GAP_MIN = 1.2  # 원문과 맞춘 줄 안에서 잉크가 이만큼(글자 높이 배) 넘게 비면 놓친 글자일 수 있다 (연한 회색 글자)
GAP_DIFF = 20.0  # 그 빈 곳에서 배경과 이만큼 다르면 글자로 본다 (줄 안이라 STRONG보다 낮게)
GAP_COLOR_TOL = 45.0  # 그리고 그 줄 글자색과 이만큼 안이어야 한다
TRIM_KEEP = 0.8  # 끝 조각을 빼면 줄 폭이 원문 길이(글자 수 × 글자 높이)의 이만큼보다 짧아지면 빼지 않는다
COLOR_TOL = 45.0  # 같은 색으로 묶는 거리
BOLD_WEIGHT = 0.13  # 획 두께 / 글자 높이가 이 이상이면 굵은 글자


def _disk(r: int) -> np.ndarray:
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))


def _lum(a: np.ndarray) -> np.ndarray:
    return a @ np.array([0.299, 0.587, 0.114], np.float32)


PANEL_NARROW = True  # label_panel을 쓴다 (비교·되돌리기용 스위치)
PANEL_MIN_LUM = 170.0  # 이보다 밝은 상자만 (흰·크림색 글자 상자)
PANEL_COLOR_TOL = 40.0  # 상자 색과 이만큼 안이면 상자 바탕
PANEL_SHRINK = 0.10  # 상자가 영역보다 이만큼(넓이 비율) 넘게 작을 때만 좁힌다
PANEL_FILL = 0.85  # 가로·세로줄의 이만큼이 상자색이면 상자 몸통 (글자 자리는 구멍으로 메워 센다)
PANEL_EDGE_ROW = 0.4  # 몸통 위·아래로 이만큼 상자색인 줄(가장자리에 붙은 글자 줄, 굵은 글자 줄은 0.45쯤)까지 넓힌다 (그림 줄은 0~0.3)
PANEL_OUTSIDE_MAX = 0.35  # 빼는 부분에서 상자색이 이보다 많으면 좁히지 않는다 (흰 바탕 카드)
PANEL_MIN_AREA = 0.30  # 상자가 영역의 이만큼은 돼야 한다


def label_panel(img: np.ndarray, box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """영역 box 안의 밝고 고른 글자 상자. 영역이 상자보다 눈에 띄게 크면 상자 사각형을, 아니면 box 그대로."""
    if not PANEL_NARROW:
        return box
    x0, y0, x1, y1 = (int(v) for v in box)
    H, W = img.shape[:2]
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    sub = img[y0:y1, x0:x1].astype(np.float32)
    h, w = sub.shape[:2]
    if h < 12 or w < 12:
        return box
    lum = _lum(sub)
    light = lum >= np.percentile(lum, 55)
    paper = np.median(sub[light], axis=0)
    if float(_lum(paper[None, :])[0]) < PANEL_MIN_LUM:
        return box  # 어두운 띠(흰 글자) 등: 그대로
    near = (np.abs(sub - paper).max(axis=2) < PANEL_COLOR_TOL).astype(np.uint8)
    k = max(3, int(min(h, w) * 0.08)) | 1  # 글자 사이 틈을 잇는다 (상자 밖 그림과는 잇지 않을 만큼)
    near = cv2.morphologyEx(near, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    n, lab, st, _c = cv2.connectedComponentsWithStats(near, connectivity=4)
    if n <= 1:
        return box
    i = 1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))
    comp = (lab == i).astype(np.uint8)
    # 글자 자리(구멍)를 메운다: 덩어리 바깥에서 채워 나가 닿지 않는 곳이 구멍
    pad = np.pad(1 - comp, 1, constant_values=1)
    ff = pad.copy()
    cv2.floodFill(ff, np.zeros((ff.shape[0] + 2, ff.shape[1] + 2), np.uint8), (0, 0), 2)
    filled = comp | (ff[1:-1, 1:-1] == 1).astype(np.uint8)
    # 상자 몸통: 가로줄의 대부분(PANEL_FILL)이 상자인 줄들의 가장 긴 연속 구간, 그 안에서 같은 방법으로 세로줄
    # (상자에 붙은 밝은 그림 조각·띠 밖 테두리는 줄의 일부만 차지하므로 빠진다)
    def longest(good: np.ndarray) -> tuple[int, int] | None:
        best, start = None, None
        for j, g in enumerate(list(good) + [False]):
            if g and start is None:
                start = j
            elif not g and start is not None:
                if best is None or j - start > best[1] - best[0]:
                    best = (start, j)
                start = None
        return best
    rows = longest(filled.mean(axis=1) >= PANEL_FILL)
    if rows is None:
        return box
    cols = longest(filled[rows[0]:rows[1]].mean(axis=0) >= PANEL_FILL)
    if cols is None:
        return box
    px0, py0, px1, py1 = cols[0], rows[0], cols[1], rows[1]
    # 상자 위·아래 끝에 붙은 글자 줄은 구멍이 닫히지 않아 몸통에서 빠질 수 있다: 상자색이 절반 넘는 줄까지 넓힌다
    near_raw = np.abs(sub - paper).max(axis=2) < PANEL_COLOR_TOL
    row_paper = np.convolve(near_raw[:, px0:px1].mean(axis=1), np.ones(3) / 3, mode="same")  # 3줄 평균 (굵은 글자 한 줄에서 멈추지 않게)
    ny0, ny1 = py0, py1
    while ny0 > 0 and row_paper[ny0 - 1] >= PANEL_EDGE_ROW:
        ny0 -= 1
    while ny1 < h and row_paper[ny1] >= PANEL_EDGE_ROW:
        ny1 += 1
    need = max(3, int(0.15 * (py1 - py0)))  # 글자 줄 하나쯤 늘 때만 (띠 가장자리 번짐 한두 줄은 넣지 않는다)
    if py0 - ny0 >= need:
        py0 = ny0
    if ny1 - py1 >= need:
        py1 = ny1
    if (px1 - px0) * (py1 - py0) < PANEL_MIN_AREA * h * w:
        return box
    if (px1 - px0) * (py1 - py0) > (1 - PANEL_SHRINK) * h * w:
        return box  # 영역이 이미 상자에 맞다
    # 안전장치: 빼는 부분(영역 - 상자)이 대부분 상자색이면(흰 바탕 카드 등) 상자를 잘못 고른 것일 수 있다: 그대로
    out = np.ones((h, w), bool)
    out[py0:py1, px0:px1] = False
    if float(near_raw[out].mean()) > PANEL_OUTSIDE_MAX:
        return box
    m = 0  # 여유를 두면 띠 가장자리(테두리)를 글자 줄로 읽는다 (몸통은 이미 글자 줄을 다 품는다)
    return (x0 + max(0, px0 - m), y0 + max(0, py0 - m), x0 + min(w, px1 + m), y0 + min(h, py1 + m))


def paint_outside(img: np.ndarray, box: tuple[int, int, int, int]) -> np.ndarray:
    """box(글자 상자) 밖을 상자 바탕색으로 칠한 사본: 글자 찾기의 바탕 추정(넓은 창)에 상자 밖 테두리·그림이 섞이지
    않게 한다 (얇은 띠 위 글자가 띠 가장자리와 한 덩어리로 붙어 ‘상자 밖 그림’으로 빠지는 것을 막는다)."""
    x0, y0, x1, y1 = (int(v) for v in box)
    inside = img[y0:y1, x0:x1].astype(np.float32)
    lum = _lum(inside)
    paper = np.median(inside[lum >= np.percentile(lum, 55)], axis=0)
    out = np.empty_like(img)
    out[...] = paper.astype(img.dtype)
    out[y0:y1, x0:x1] = img[y0:y1, x0:x1]
    return out


def _local_std(lum: np.ndarray, k: int = 15) -> np.ndarray:
    m1 = cv2.blur(lum, (k, k))
    m2 = cv2.blur(lum * lum, (k, k))
    return np.sqrt(np.maximum(m2 - m1 * m1, 0))


@dataclass
class TextMask:
    mask: np.ndarray  # 지울 픽셀 (글자 + 테두리 + 닫힌 빈칸)
    core: np.ndarray  # 글자로 고른 덩어리 (넓히기 전)
    busy: bool  # 상자 배경이 그림(바쁨)인가
    texture: float  # 상자 배경의 국소 표준편차 중앙값
    glyph_px: float = 0.0  # 글자 크기 추정 (px)
    notes: list[str] = field(default_factory=list)
    dividers: list[tuple[int, int, int, int]] = field(default_factory=list)  # 남긴 가로 구분선 (x0,y0,x1,y1)


def _outlined(I: np.ndarray, c) -> bool:
    """덩어리 둘레(2~4px 고리)가 고른 한 색이고 덩어리 색과 크게 다르면 테두리 두른 글자 획."""
    comp = c[0]
    ring = cv2.dilate(comp.astype(np.uint8), _disk(4)).astype(bool) & ~cv2.dilate(comp.astype(np.uint8), _disk(1)).astype(bool)
    if ring.sum() < 20:
        return False
    rp = I[ring]
    med = np.median(rp, axis=0)
    coherent = float((np.abs(rp - med).max(axis=1) < COLOR_TOL).mean())
    if coherent < 0.6 or float(np.abs(med - c[1]).max()) < 100:
        return False
    # 글자 테두리는 흰 글자 + 어두운 테두리(또는 그 반대)다. 납선에 둘러싸인 색유리 조각은 빼다
    col = np.asarray(c[1])
    light_on_dark = col.min() >= 190 and med.max() <= 110
    dark_on_light = col.max() <= 80 and med.min() >= 170
    return bool(light_on_dark or dark_on_light)


def text_mask(img: np.ndarray, box: tuple[int, int, int, int], protect: np.ndarray | None = None,
              size_box: tuple[int, int, int, int] | None = None) -> TextMask:
    """img(H×W×3 uint8, 상자 둘레에 여유가 있는 자른 그림) 안 box(x0,y0,x1,y1)의 글자 픽셀.
    size_box: 크기 기준(너무 큰 덩어리·배경 창 크기)에 쓸 상자. 영역을 글자 상자(label_panel)로 좁혀 찾을 때 원래 영역을
    넘긴다 (좁힌 상자 높이로 재면 그림에 붙은 큰 글자를 ‘큰 그림’으로 잘못 뺀다)."""
    x0, y0, x1, y1 = box
    H, W = img.shape[:2]
    sb = size_box or box
    bw, bh = max(1, sb[2] - sb[0]), max(1, sb[3] - sb[1])
    I = img.astype(np.float32)
    k = int(max(31, min(81, bh * 0.6))) | 1  # 글자 획보다 넓은 창
    med = np.dstack([cv2.medianBlur(np.ascontiguousarray(img[..., c]), k) for c in range(3)]).astype(np.float32)
    D = np.abs(I - med).max(axis=2)
    # 색 라벨 띠(가로로 길고 속이 찬 덩어리) 안은 띠 색을 배경으로 본다: 넓은 창의 중앙값은 종이색이라
    # 띠 위 흰 글자가 배경과 다르게 보이지 않는다
    for pol in (1, -1):
        sl = ((D > STRONG) & (np.sign(_lum(I) - _lum(med)) == pol)).astype(np.uint8)
        n_, lab_, st_, _c = cv2.connectedComponentsWithStats(sl, connectivity=8)
        for i in range(1, n_):
            x, y, w, h, a_ = (int(v) for v in st_[i])
            if w > 0.25 * bw and w > 3 * h and h < 0.2 * bh and a_ >= 0.45 * w * h and h >= 12:
                comp = lab_ == i
                fill = np.median(I[comp], axis=0)
                inside = np.zeros((H, W), bool)
                inside[y:y + h, x:x + w] = True
                med[inside] = fill
    D = np.abs(I - med).max(axis=2)
    lum, lmed = _lum(I), _lum(med)
    inbox = np.zeros((H, W), bool)
    inbox[max(0, y0):y1, max(0, x0):x1] = True
    strong_all = D > STRONG
    near = cv2.dilate(strong_all.astype(np.uint8), _disk(4)).astype(bool)
    sel = inbox & ~near
    texture = float(np.median(_local_std(lum)[sel])) if sel.sum() > 50 else 99.0
    busy = texture >= 8.0

    cands = []  # (덩어리, 평균색, 넓이, 큰 변)
    edge_glyphs: set[int] = set()  # 위·아래 가장자리에 걸쳐 글자로 받아들인 덩어리 (id)
    lines = []  # 가로 구분선의 (세로 중심, 두께): 선 사이의 장식(◆)도 남긴다
    dividers = []
    for pol in (1, -1):
        strong = (strong_all & (np.sign(lum - lmed) == pol)).astype(np.uint8)
        n, lab, st, _ = cv2.connectedComponentsWithStats(strong, connectivity=8)
        for i in range(1, n):
            x, y, w, h, a = (int(v) for v in st[i])
            if a < 6:
                continue
            if not busy and w > 0.25 * bw and w > 4 * h and h < 0.15 * bh and a >= 0.45 * w * h:  # 종이 위 가로 구분선 (그림 위의 긴 'ー'는 글자)
                lines.append((y + h / 2, h, (x, y, x + w, y + h)))
                continue
            if w > 0.6 * bw or h > 0.8 * bh:  # 테두리·큰 그림
                continue
            comp = lab == i
            out_frac = (comp & ~inbox).sum() / a
            if out_frac > 0.03:  # 상자 밖으로 이어지는 그림·선
                # 영역을 글자에 조금 빠듯하게 그려 위·아래 가장자리에 걸친 글자 조각: 대부분 안에 있고 글자 크기이면
                # 글자로 보고 상자를 그만큼 넓힌다 (좌우로 걸친 것·큰 것은 그림·테두리로 두고 그대로 뺀다)
                edge = (y < y0 <= y + h or y < y1 <= y + h) and x >= x0 and x + w <= x1
                if not (edge and out_frac <= 0.6 and h <= 0.5 * bh and w <= 0.2 * bw):
                    continue
                inbox[max(0, y - 2):min(H, y + h + 2), max(0, x - 2):min(W, x + w + 2)] = True
                edge_glyphs.add(id(comp))
            cands.append((comp, I[comp].mean(axis=0), a, max(w, h)))
    # 가로로 긴 덩어리 중 글자보다 두꺼운 것은 구분선이 아니라 색 라벨(예: 빨간 ‘プレイ時効果’ 띠): 라벨은 남기고
    # 안의 글자는 글자로 본다. 진짜 구분선은 글자 획 굵기쯤으로 가늘다
    sizes0 = [c[3] for c in cands if c[2] >= 30]
    glyph0 = float(np.percentile(sizes0, 75)) if sizes0 else 0.0
    lines = [ln for ln in lines if not glyph0 or ln[1] <= max(4.0, 0.35 * glyph0)]
    for _cy, _h, (lx0, ly0, lx1, ly1) in lines:
        if x0 <= (lx0 + lx1) / 2 <= x1 and y0 <= (ly0 + ly1) / 2 <= y1:
            dividers.append((lx0, ly0, lx1, ly1))

    def on_line(c):
        ys, xs = np.nonzero(c[0])
        cy, ch = (ys.min() + ys.max()) / 2, ys.max() - ys.min() + 1
        return any(abs(cy - ly) <= max(3.0, lh) and ch <= 4 * lh + 6 for ly, lh, _b in lines)

    cands = [c for c in cands if not on_line(c)]  # 구분선 위의 작은 장식
    sizes = [c[3] for c in cands if c[2] >= 30]
    glyph = float(np.percentile(sizes, 75)) if sizes else 0.0
    if glyph:
        cands = [c for c in cands if c[3] <= 3.0 * glyph]  # 테두리 곡선·큰 그림

    keep = np.zeros((H, W), bool)
    notes: list[str] = []
    if not busy:
        lum2 = np.where(near, lmed, lum)  # 글자 근처는 배경값으로 바꿔 놓고 바쁜 곳을 잰다
        busy_map = _local_std(lum2) > 2.5 * texture + 8
        busy_map = cv2.dilate(busy_map.astype(np.uint8), _disk(10)).astype(bool)
        def on_label(c) -> bool:
            """색 라벨 위 글자: 둘레가 한 색으로 고르고(종이색 아님) 글자와 대비가 크다."""
            ring = cv2.dilate(c[0].astype(np.uint8), _disk(5)).astype(bool) & ~cv2.dilate(
                c[0].astype(np.uint8), _disk(1)).astype(bool)
            if ring.sum() < 15:
                return False
            rp = I[ring]
            med = np.median(rp, axis=0)
            if paper0 is not None and float(np.abs(med - paper0).max()) < 40:
                return False  # 둘레가 종이색이면 라벨이 아니다 (종이 위 글자는 원래 규칙대로)
            if not (float((np.abs(rp - med).max(axis=1) < 30).mean()) >= 0.75
                    and float(np.abs(med - c[1]).max()) >= 90):
                return False
            # 라벨은 가로 띠다: 글자 바로 위·아래 줄을 글자 폭의 다섯 배쯤 넓게 보면 대부분 띠 색이다.
            # 동전·배지 위 숫자 ‘5’처럼 작은 둥근 그림 위의 것은 곧 다른 색이 나와 글자가 아니다
            ys, xs = np.nonzero(c[0])
            d = int(max(xs.max() - xs.min(), ys.max() - ys.min()) + 1)
            a_, b_ = max(0, int(xs.min()) - 2 * d), min(W, int(xs.max()) + 1 + 2 * d)
            strips = [I[max(0, ys.min() - 4):max(0, ys.min() - 1), a_:b_], I[ys.max() + 2:min(H, ys.max() + 5), a_:b_]]
            px = np.concatenate([st_.reshape(-1, 3) for st_ in strips])
            return len(px) > 0 and float((np.abs(px - med).max(axis=1) < 30).mean()) >= 0.6

        paper0 = np.median(I[sel], axis=0) if sel.sum() > 50 else None
        kept, dropped = [], []
        for c in cands:
            if on_label(c):
                kept.append((c, False))
                continue
            ys, xs = np.nonzero(c[0])
            # 위·아래 가장자리에 걸친 글자로 이미 받아들인 것은 그쪽 가장자리에 닿아도 그림 조각으로 보지 않는다
            # 위·아래에만 닿은 글자 크기 덩어리는 "tb": 영역을 빠듯하게 그려 글자 밑동이 닿은 것일 수 있다
            touch = "lr" if (xs.min() <= x0 + 2 or xs.max() >= x1 - 3) else (
                "tb" if id(c[0]) not in edge_glyphs and (ys.min() <= y0 + 2 or ys.max() >= y1 - 3) else "")
            ov = (c[0] & busy_map).sum() / c[2]
            (dropped if ov > 0.3 or (touch and ov > 0.05) else kept).append((c, touch))
        # 글자보다 길고 속이 빈 가는 곡선(패널 테두리 조각)은 글자가 아니다
        def thin_edge(c, _t) -> bool:
            ys, xs = np.nonzero(c[0])
            w, h = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
            if not (bool(glyph) and max(w, h) > 1.5 * glyph and c[2] < 0.25 * w * h):
                return False
            # 큰 글자의 가는 획(‘魔’의 广)은 같은 색 글자 조각을 품고, 가는 글자(‘ド’의 ト)는 같은 줄에 비슷한 높이의
            # 같은 색 글자가 바로 옆에 있다: 테두리 조각이 아니다 (윗첨자(루비)가 있으면 글자 크기가 작게 잡혀 이렇게 걸린다)
            bx0, by0, bx1, by1 = xs.min(), ys.min(), xs.max(), ys.max()
            inner = 0
            beside = False
            for d in pieces:
                if d is c or np.abs(d[1] - c[1]).max() >= 35:
                    continue
                dy, dx = np.nonzero(d[0])
                cx_, cy_ = (dx.min() + dx.max()) / 2, (dy.min() + dy.max()) / 2
                if bx0 <= cx_ <= bx1 and by0 <= cy_ <= by1 and d[2] >= 0.2 * c[2]:
                    inner += d[2]
                dh = dy.max() - dy.min() + 1
                ov = min(by1, dy.max()) - max(by0, dy.min()) + 1
                gap = max(dx.min() - bx1, bx0 - dx.max())
                if dh >= 0.7 * h and ov >= 0.6 * min(h, dh) and gap <= max(w, h):
                    beside = True
            if not _t and beside and max(w, h) <= 2.5 * glyph:
                return False
            return inner < 0.3 * c[2]

        pieces = [c for c, _t in kept] + [c for c, _t in dropped]
        kept = [c for c, t in kept if not thin_edge(c, t)]
        dropped = [(c, t) for c, t in dropped if not thin_edge(c, t)]
        # 종이 위 글자는 둘레가 종이색이다. 둘레가 그림(유리 조각·테두리)인 조각은 빼다
        paper = np.median(I[sel], axis=0) if sel.sum() > 50 else None
        free = ~cv2.dilate(strong_all.astype(np.uint8), _disk(2)).astype(bool) & inbox

        def on_paper(c) -> bool:
            if paper is None:
                return True
            ring = cv2.dilate(c[0].astype(np.uint8), _disk(6)).astype(bool) & free
            if ring.sum() < 15:
                return True
            if float((np.abs(I[ring] - paper).max(axis=1) < 45).mean()) >= 0.5:
                return True
            return on_label(c)  # 색 라벨 위 글자 (유리 조각은 둘레가 여러 색이라 빠진다)

        kept = [c for c in kept if on_paper(c)]
        dropped = [(c, t) for c, t in dropped if on_paper(c)]
        if kept:  # 획이 빽빽한 글자는 주변이 바빠 보인다: 주요 글자색이고 가장자리에 닿지 않으면 되살린다
            tot = sum(c[2] for c in kept)
            main = []
            for c in sorted(kept, key=lambda c: -c[2]):
                if any(np.abs(m - c[1]).max() < COLOR_TOL for m in main):
                    continue
                if sum(d[2] for d in kept if np.abs(d[1] - c[1]).max() < COLOR_TOL) / tot >= 0.2:
                    main.append(c[1])
            # 위·아래에만 닿은 것도 글자 크기면 되살린다 (빽빽한 글자 ‘殻城’의 밑동이 빠듯한 영역 끝에 닿은 경우)
            kept += [c for c, t in dropped if (not t or (t == "tb" and glyph and c[3] <= 2.0 * glyph))
                     and any(np.abs(m - c[1]).max() < 35 for m in main)]
        for c in kept:
            keep |= c[0]
    else:
        ring_out = cv2.dilate(inbox.astype(np.uint8), _disk(14)).astype(bool) & ~inbox
        out_cols = I[ring_out]
        groups: list[dict] = []
        for c in sorted(cands, key=lambda c: -c[2]):
            for g in groups:
                if np.abs(g["color"] - c[1]).max() < COLOR_TOL:
                    g["items"].append(c)
                    g["area"] += c[2]
                    break
            else:
                groups.append({"color": c[1], "items": [c], "area": c[2]})
        best, best_score = None, 0.0
        for g in groups:
            g["rare"] = float((np.abs(out_cols - g["color"]).max(axis=1) < COLOR_TOL).mean()) if len(out_cols) else 0.0
            g["outlined"] = sum(c[2] for c in g["items"] if _outlined(I, c)) / max(1, g["area"])
            score = g["area"] * max(0.0, 1 - g["rare"] * 8)
            if score > best_score:
                best, best_score = g, score
        # 테두리 두른 글자(흰 글자 + 검은 외곽선 등): 둘레가 고르고 글자와 대비가 큰 색 묶음이 있으면 그것이 글자다.
        # 상자 밖에 같은 색(예: 띠 위아래 흰 패널)이 많아도 놓치지 않게 ‘드묾’보다 먼저 본다.
        lined = [g for g in groups if g["outlined"] >= 0.5 and g["area"] >= 0.3 * max(gg["area"] for gg in groups)]
        if lined:
            best = max(lined, key=lambda g: g["area"] * g["outlined"])
        if not lined:
            # 상자 바로 밖에 글자와 같은 색(띠 테두리선·카드 검은 테두리)이 많으면 ‘드묾’으로는 못 고른다 (못 고르거나
            # 작은 장식 조각을 고른다). 그때는 한 줄 글자처럼 늘어선 색 묶음을 글자로
            row = _text_row_group(groups, (x0, y0, x1, y1))
            if row is not None and row is not best and (best is None or best["area"] < 0.3 * row["area"]):
                best = row
                notes.append("상자 밖에 글자색이 많아 한 줄로 늘어선 색을 글자로 봄")
        for g in groups:
            if g is best or g["rare"] < 0.005:
                for c in g["items"]:
                    keep |= c[0]
        if best is None:
            notes.append("그림 배경에서 글자색을 찾지 못함")

    grown = keep.copy()
    weak = D > WEAK
    for _ in range(4):  # 글자에 붙은 테두리·그림자
        grown |= cv2.dilate(grown.astype(np.uint8), _disk(2)).astype(bool) & weak & inbox
    inv = (~grown).astype(np.uint8)  # 닫힌 빈칸 채우기 (글자 속, 코인 속)
    n, lab, st, _ = cv2.connectedComponentsWithStats(inv, connectivity=4)
    lim = (0.5 * bh) ** 2 if busy else (0.25 * bh) ** 2
    for i in range(1, n):
        x, y, w, h, a = (int(v) for v in st[i])
        if x == 0 or y == 0 or x + w >= W or y + h >= H:
            continue
        if a <= lim and w < 0.3 * bw:
            grown |= lab == i
    # 번짐: 종이 1px, 그림 배경은 흰 채움·테두리 잔상이 남지 않게 3px
    grown = cv2.dilate(grown.astype(np.uint8), _disk(3 if busy else 1)).astype(bool) & inbox
    if protect is not None:
        grown &= ~protect
    return TextMask(grown, keep, busy, texture, glyph, notes, dividers)


def _text_row_group(groups: list[dict], box: tuple[int, int, int, int]) -> dict | None:
    """색 묶음 중 한 줄 글자처럼 늘어선 가장 큰 것 (없으면 None).
    조각 3개 이상, 조각들의 세로 범위가 보통 조각 높이의 2.5배 이내(한 줄), 가로 폭이 세로의 2배 이상,
    상자 왼쪽·오른쪽 끝에 닿는 조각이 없다 (그림·테두리 조각은 흩어져 있거나 끝에 걸친다. 위·아래는 영역을
    빠듯하게 그리면 글자가 걸칠 수 있어 본다)."""
    x0, y0, x1, y1 = box
    for g in sorted(groups, key=lambda g: -g["area"]):
        items = [c for c in g["items"] if c[2] >= 20]
        if len(items) < 3:
            continue
        boxes = []
        for c in items:
            ys, xs = np.nonzero(c[0])
            boxes.append((xs.min(), ys.min(), xs.max(), ys.max()))
        if any(b[0] <= x0 + 2 or b[2] >= x1 - 3 for b in boxes):
            continue
        hs = sorted(b[3] - b[1] + 1 for b in boxes)
        h_med = hs[len(hs) // 2]
        uy0, uy1 = min(b[1] for b in boxes), max(b[3] for b in boxes)
        ux0, ux1 = min(b[0] for b in boxes), max(b[2] for b in boxes)
        if uy1 - uy0 + 1 <= 2.5 * h_med and ux1 - ux0 >= 2 * (uy1 - uy0 + 1):
            return g
    return None


def background_kind(img: np.ndarray, tm: TextMask) -> str:
    """글자 주변 배경: solid(단색) | pattern(무늬) | image(그림)."""
    if tm.busy:
        return "image"
    ring = cv2.dilate(tm.mask.astype(np.uint8), _disk(8)).astype(bool) & ~tm.mask
    if ring.sum() < 20:
        return "solid"
    # 글자를 뺀 주변만으로 고주파(무늬) 세기를 잰다 (완만한 색 변화는 단색으로 본다)
    lum = _lum(img.astype(np.float32))
    known = (~tm.mask).astype(np.float32)
    smooth = cv2.GaussianBlur(lum * known, (0, 0), 4) / np.maximum(cv2.GaussianBlur(known, (0, 0), 4), 1e-4)
    s = float(np.std((lum - smooth)[ring]))
    return "solid" if s < 2.0 else "pattern"


def _rects(mask: np.ndarray, glyph: float) -> list[tuple[int, int, int, int]]:
    """글자 줄마다 사각형 (가로로 이어 붙여 묶음)."""
    gx = int(max(5, glyph * 0.8)) | 1
    joined = cv2.dilate(mask.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_RECT, (gx, 3)))
    n, _lab, st, _ = cv2.connectedComponentsWithStats(joined, connectivity=8)
    return [(int(st[i, 0]), int(st[i, 1]), int(st[i, 0] + st[i, 2]), int(st[i, 1] + st[i, 3])) for i in range(1, n)]


def fill_text(img: np.ndarray, tm: TextMask, mode: str = "auto",
              box: tuple[int, int, int, int] | None = None) -> tuple[np.ndarray, str, np.ndarray]:
    """(채운 그림, 실제로 쓴 방식, 바뀐 픽셀). 바뀌는 픽셀은 글자 자리(rect_avg면 줄 사각형)뿐이다."""
    if mode not in MODES:
        mode = "auto"
    kind = background_kind(img, tm)
    used = mode if mode != "auto" else {"solid": "solid", "pattern": "pattern", "image": "inpaint"}[kind]
    out = img.copy()
    m = tm.mask
    if not m.any():
        return out, used, m
    if used == "rect_avg":
        changed = np.zeros(m.shape, bool)
        H, W = m.shape
        bx0, by0, bx1, by1 = box or (0, 0, W, H)
        for x0, y0, x1, y1 in _rects(m, tm.glyph_px):
            x0, y0, x1, y1 = max(bx0, x0 - 1), max(by0, y0 - 1), min(bx1, x1 + 1), min(by1, y1 + 1)
            r = np.zeros(m.shape, bool)
            r[max(0, y0 - 3):min(H, y1 + 3), max(0, x0 - 3):min(W, x1 + 3)] = True
            r[y0:y1, x0:x1] = False
            r &= ~m
            col = img[r].mean(axis=0) if r.sum() else img[y0:y1, x0:x1].reshape(-1, 3).mean(axis=0)
            out[y0:y1, x0:x1] = np.clip(np.rint(col), 0, 255).astype(np.uint8)
            changed[y0:y1, x0:x1] = True
        return out, used, changed
    if used == "solid":
        known = ~cv2.dilate(m.astype(np.uint8), _disk(1)).astype(bool)
        f = img.astype(np.float32)
        w = cv2.GaussianBlur(known.astype(np.float32), (0, 0), 6)
        bgs = cv2.GaussianBlur(f * known[..., None], (0, 0), 6) / np.maximum(w, 1e-4)[..., None]
        for s in (12, 24, 48):  # 큰 글자 속처럼 주변이 먼 곳은 더 넓게
            hole = w < 0.02
            if not hole.any():
                break
            w2 = cv2.GaussianBlur(known.astype(np.float32), (0, 0), s)
            b2 = cv2.GaussianBlur(f * known[..., None], (0, 0), s) / np.maximum(w2, 1e-4)[..., None]
            bgs = np.where(hole[..., None], b2, bgs)
            w = np.where(hole, w2, w)
        out[m] = np.clip(np.rint(bgs[m]), 0, 255).astype(np.uint8)
        return out, used, m
    radius = 3 if used == "pattern" else 5
    bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    bgr = cv2.inpaint(bgr, m.astype(np.uint8) * 255, radius, cv2.INPAINT_TELEA)
    out = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    out[~m] = img[~m]
    return out, used, m


# ---------------------------------------------------------------- 원문 줄 모양 (한글 넣기용)
def _hex(c) -> str:
    c = np.clip(np.rint(np.asarray(c, np.float32)), 0, 255).astype(int)
    return "#%02x%02x%02x" % tuple(c)


def _bands(rows: np.ndarray, gap: int) -> list[list[int]]:
    out: list[list[int]] = []
    for i in np.nonzero(rows)[0]:
        if out and i - out[-1][1] <= gap + 1:
            out[-1][1] = int(i)
        else:
            out.append([int(i), int(i)])
    return out


def _merge_thin_bands(bands: list[list[int]], glyph: float, gap: int) -> list[list[int]]:
    """얇은 띠(글자 높이의 45% 미만)를 gap 안의 가까운 이웃 띠에 붙인다. 두꺼운 띠끼리는 붙이지 않는다."""
    out = [list(b) for b in bands]
    thin = lambda b: b[1] - b[0] + 1 < 0.45 * glyph  # noqa: E731
    changed = True
    while changed and len(out) > 1:
        changed = False
        for i, b in enumerate(out):
            if not thin(b):
                continue
            prev_d = b[0] - out[i - 1][1] - 1 if i > 0 else None
            next_d = out[i + 1][0] - b[1] - 1 if i + 1 < len(out) else None
            cands = [(d, j) for d, j in ((prev_d, i - 1), (next_d, i + 1)) if d is not None and d <= gap + 1]
            if not cands:
                continue
            _d, j = min(cands)
            lo, hi = min(i, j), max(i, j)
            out[lo] = [out[lo][0], out[hi][1]]
            del out[hi]
            changed = True
            break
    return out


def _seg_height(m: np.ndarray) -> float | None:
    """빈 세로줄로 나뉜 조각의 높이 중 큰 쪽(75%) = 대표 글자 높이 (작은 ー・、 를 피함)."""
    rows_all = np.nonzero(m.any(1))[0]
    if not len(rows_all):
        return None
    bh = rows_all[-1] - rows_all[0] + 1
    cols = m.any(0)
    hs, i, n = [], 0, len(cols)
    while i < n:
        if cols[i]:
            j = i
            while j < n and cols[j]:
                j += 1
            r = np.nonzero(m[:, i:j].any(1))[0]
            if j - i >= 0.25 * bh:
                hs.append(r[-1] - r[0] + 1)
            i = j
        else:
            i += 1
    return float(np.percentile(hs, 75)) if hs else float(bh)


def text_layout(img: np.ndarray, tm: TextMask, box: tuple[int, int, int, int]) -> dict:
    """지운 글자의 줄 모양: 줄마다 위치·대표 글자 높이·글자색·테두리색·굵기·정렬, 다른 색 낱말, 구분선.

    좌표는 img 픽셀. 한글 넣기가 원문 줄 자리·서식을 그대로 따르게 지우기 때 함께 기록한다.
    """
    x0, y0, x1, y1 = box
    bw = max(1, x1 - x0)
    I = img.astype(np.float32)
    inbox = np.zeros(tm.core.shape, bool)
    inbox[max(0, y0):y1, max(0, x0):x1] = True
    core = tm.core & inbox
    divs = [list(map(int, d)) for d in tm.dividers]
    if not core.any():
        return {"lines": [], "dividers": divs}
    glyph = tm.glyph_px or 10.0
    rows = core.sum(1)
    on = rows >= max(1, 0.04 * rows.max())  # 작은 티끌이 줄을 잇지 않게
    # 줄 사이가 2~3px뿐인 빽빽한 글(줄 간격 좁은 본문)도 줄마다 나눈다: 빈 행에서 모두 나눈 뒤,
    # 글자 높이의 45%보다 얇은 조각(탁점·점·위로 튀어나온 획)만 가까운 이웃 줄에 붙인다
    bands = _merge_thin_bands(_bands(on, 0), glyph, max(2, int(glyph * 0.12)))
    hmax = max(b - a + 1 for a, b in bands)
    bands = [b for b in bands if b[1] - b[0] + 1 >= 0.25 * hmax]
    bg_px = I[inbox & ~tm.mask]
    bg = np.median(bg_px, axis=0) if len(bg_px) > 20 else None
    lines = []
    for a, b in bands:
        sub = np.zeros_like(core)
        sub[a:b + 1] = core[a:b + 1]
        ys, xs = np.nonzero(sub)
        # 글자색: 덩어리마다 색을 재서, 가장 넓은 색 = 줄 기본색, 그와 다른 색 = 강조 낱말
        n, lab, st, _ = cv2.connectedComponentsWithStats(sub.astype(np.uint8), connectivity=8)
        comps = []
        for i in range(1, n):
            cm = lab == i
            e = cv2.erode(cm.astype(np.uint8), _disk(1)).astype(bool)
            col = np.median(I[e] if e.sum() >= 4 else I[cm], axis=0)
            comps.append((col, int(st[i, 4]), int(st[i, 0]), int(st[i, 0] + st[i, 2])))
        groups: list[dict] = []
        for col, area, cx0, cx1 in sorted(comps, key=lambda c: -c[1]):
            for g in groups:
                if np.abs(g["color"] - col).max() < COLOR_TOL:
                    g["area"] += area
                    g["x"].append((cx0, cx1))
                    break
            else:
                groups.append({"color": col, "area": area, "x": [(cx0, cx1)]})
        groups.sort(key=lambda g: -g["area"])
        tot = sum(g["area"] for g in groups) or 1
        main = groups[0]["color"]
        accents = [g for g in groups[1:] if g["area"] / tot >= 0.04 and np.abs(g["color"] - main).max() >= 80
                   and (bg is None or np.abs(g["color"] - bg).max() >= 60)]  # 코인 속 흰 곳 같은 배경색은 빼다
        accents.sort(key=lambda g: min(x for x, _ in g["x"]))
        gh = _seg_height(sub[a:b + 1]) or float(b - a + 1)
        # 테두리: 글자 바로 바깥 고리가 고른 색이고 글자색·배경색과 많이 다르면 외곽선이 있는 글자
        stroke, stroke_px = None, 0.0
        r = max(2, int(round(gh * 0.1)))
        ring = cv2.dilate(sub.astype(np.uint8), _disk(r)).astype(bool) & ~sub & inbox
        if ring.sum() >= 30:
            rp = I[ring]
            rmed = np.median(rp, axis=0)
            coherent = float((np.abs(rp - rmed).max(axis=1) < COLOR_TOL).mean())
            far_fill = np.abs(rmed - main).max() >= 90
            far_bg = bg is None or tm.busy or np.abs(rmed - bg).max() >= 70
            if coherent >= 0.55 and far_fill and far_bg:
                stroke = rmed
                near = (np.abs(I - rmed).max(axis=2) < COLOR_TOL) & ~sub
                dist = cv2.distanceTransform((~sub).astype(np.uint8), cv2.DIST_L2, 3)
                d = dist[near & (dist <= 4 * r) & inbox]
                stroke_px = float(np.median(d)) if len(d) else float(r)
        # 굵기: 획 두께(2 x 넓이 / 둘레) / 글자 높이
        cnts, _ = cv2.findContours(sub.astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
        per = sum(cv2.arcLength(c, True) for c in cnts) or 1.0
        weight = 2.0 * float(sub.sum()) / per / max(gh, 1.0)
        # 줄 폭: 글자에서 멀리 떨어진 작은 티끌(테두리 옆 점)은 빼고 잰다
        big = [(c[2], c[3]) for c in comps if c[1] >= 0.05 * gh * gh]
        lx0, lx1 = (min(u for u, _ in big), max(v for _, v in big)) if big else (int(xs.min()), int(xs.max()) + 1)
        for _col, area, u, v in comps:
            if (u >= lx1 and u - lx1 <= gh) or (v <= lx0 and lx0 - v <= gh):
                lx0, lx1 = min(lx0, u), max(lx1, v)
        lines.append({
            "bbox": [int(lx0), int(a), int(lx1), int(b) + 1],
            "glyph_h": round(gh, 2),
            "color": _hex(main),
            "accents": [{"color": _hex(g["color"]), "x": [[int(u), int(v)] for u, v in sorted(g["x"])]}
                        for g in accents],
            "stroke_color": _hex(stroke) if stroke is not None else None,
            "stroke_px": round(stroke_px, 2),
            "weight": round(weight, 3),
            "bold": weight >= BOLD_WEIGHT,
            # 줄의 세로 가운데: 잉크가 몰린 곳(행 10~90%) 기준. 「 」·ー 같은 튀어나온 획에 끌리지 않게
            "cy": round(float(np.mean(np.percentile(ys, [10, 90]))) + 0.5, 2),
        })
    # 정렬: 구분선으로 나뉜 묶음마다 본다. 줄 가운데가 상자 가운데에 가깝고, 묶음의 다른 줄보다 들어가 있거나
    # 묶음에 줄이 하나뿐이면 가운데 정렬
    cx = (x0 + x1) / 2
    cuts = sorted((d[1] + d[3]) / 2 for d in divs)

    def section(ln):
        return sum((ln["bbox"][1] + ln["bbox"][3]) / 2 > c for c in cuts)

    for ln in lines:
        left, _t, right, _b = ln["bbox"]
        mid = (left + right) / 2
        same = [o for o in lines if section(o) == section(ln)]
        lmin = min(o["bbox"][0] for o in same)
        if abs(mid - cx) <= 0.06 * bw and (left - lmin > 0.04 * bw or len(same) == 1):
            ln["align"] = "center"
        elif len(same) == 1 and min(left - x0, x1 - right) > 0.1 * bw:
            # 묶음에 줄이 하나이고 양옆이 다 비었다: 영역을 글자보다 한쪽으로 넓게 그렸어도 그 줄은 가운데 정렬
            # (한글 넣기가 원문 줄의 가운데에 맞춘다. 왼쪽 정렬로 보면 한글 길이만큼 한쪽으로 쏠린다)
            ln["align"] = "center"
        elif right >= x1 - 0.04 * bw and left - lmin > 0.15 * bw:
            ln["align"] = "right"
        else:
            ln["align"] = "left"
    _center_widest(lines, x0, x1, cuts)
    return {"lines": lines, "dividers": divs, "box_x": [x0, x1], "cuts": cuts}


def _center_widest(lines: list[dict], x0: float, x1: float, cuts: list) -> None:
    """가운데 정렬 묶음의 가장 긴 줄: 다른 줄보다 들어가 있지 않아 ‘left’로 잡힌다. 그 줄도 상자 가운데에 있고
    묶음의 다른 줄이 모두 가운데 정렬이면 가운데 정렬 (한글이 짧아져도 왼쪽으로 쏠리지 않게)."""
    cx, bw = (x0 + x1) / 2, max(1.0, x1 - x0)

    def section(ln):
        return sum((ln["bbox"][1] + ln["bbox"][3]) / 2 > c for c in cuts)

    for ln in lines:
        if ln.get("align") != "left":
            continue
        others = [o for o in lines if o is not ln and section(o) == section(ln) and not o.get("keep")]
        mid = (ln["bbox"][0] + ln["bbox"][2]) / 2
        if others and all(o.get("align") == "center" for o in others) and abs(mid - cx) <= 0.06 * bw:
            ln["align"] = "center"


_WORD = __import__("re").compile(r"[ぁ-んァ-ヶ一-龯々A-Za-zＡ-Ｚａ-ｚ가-힣]")
_TOKEN = __import__("re").compile(r"\[\[[^\[\]]*\]\]|</?(?:b|i|c\d+)>")


def is_icon_line(text: str) -> bool:
    """아이콘·숫자·기호만 있는 줄 (예: ‘4[[coin]] [[tile]]’). 번역해도 같으므로 원문 그대로 둔다."""
    return not _WORD.search(_TOKEN.sub("", text))


def _ink_segments(row_cols: np.ndarray) -> list[tuple[int, int]]:
    """잉크가 있는 열(bool)을 틈으로 나눈 덩이 [(시작, 끝)]."""
    xs = np.nonzero(row_cols)[0]
    if not len(xs):
        return []
    segs, start = [], xs[0]
    for a, b in zip(xs[:-1], xs[1:]):
        if b - a > 1:
            segs.append((int(start), int(a + 1)))
            start = b
    segs.append((int(start), int(xs[-1] + 1)))
    return segs


def fill_line_gaps(tm: "TextMask", img: np.ndarray, ln: dict, allowed: np.ndarray | None = None) -> int:
    """원문과 맞춘 줄 안에 넓게 빈 곳(GAP_MIN 글자 넘게)이 있으면, 그 자리에서만 배경과 조금(GAP_DIFF) 다른 픽셀을
    지울 글자로 더한다. 그림 위 연한 회색 글자는 STRONG 기준으로는 일부만 잡혀, 줄 중간 글자가 지워지지 않고
    남던 것 (예: 회색 인용문 앞부분). allowed: 지워도 되는 곳 (보호 영역·글자 상자 밖은 False). 더한 픽셀 수."""
    x0, y0, x1, y1 = (int(v) for v in ln["bbox"])
    gh = float(ln.get("glyph_h") or 0)
    H, W = tm.mask.shape
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    if gh <= 0 or x1 - x0 < 3 * gh or y1 <= y0:
        return 0
    segs = _ink_segments(tm.mask[y0:y1, x0:x1].any(axis=0))
    gaps = [(a[1], b[0]) for a, b in zip(segs[:-1], segs[1:]) if b[0] - a[1] > GAP_MIN * gh]
    if not gaps:
        return 0
    pad = int(max(2, 0.3 * gh))
    by0, by1 = max(0, y0 - pad), min(H, y1 + pad)
    band = img[by0:by1, max(0, x0 - int(2 * gh)):min(W, x1 + int(2 * gh))].astype(np.float32)
    bx0 = max(0, x0 - int(2 * gh))
    k = int(max(15, min(81, 3 * gh))) | 1  # 글자보다 넓은 창의 중앙값 = 배경
    med = np.dstack([cv2.medianBlur(np.ascontiguousarray(band[..., c]).astype(np.uint8), k)
                     for c in range(3)]).astype(np.float32)
    cand = np.abs(band - med).max(axis=2) > GAP_DIFF
    ink = ln.get("color")
    if not ink:
        return 0
    col = np.array([int(ink[i:i + 2], 16) for i in (1, 3, 5)], np.float32)
    cand &= np.abs(band - col).max(axis=2) < GAP_COLOR_TOL  # 그 줄 글자색에 가까운 것만 (바쁜 배경의 티끌·테두리 색 빼기)
    added = 0
    for a, b in gaps:
        ga, gb = x0 + a - bx0, x0 + b - bx0
        sub = cand[:, ga:gb]
        if sub.sum() < 0.02 * sub.size:
            continue  # 정말 빈 곳 (낱말 사이 넓은 띄어쓰기 등)
        sub = cv2.dilate(sub.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
        if allowed is not None:
            sub &= allowed[by0:by1, x0 + a:x0 + b]
        region = tm.mask[by0:by1, x0 + a:x0 + b]
        added += int((sub & ~region).sum())
        region |= sub
    return added


def _trim_stray_end(tm: "TextMask", ln: dict, text: str) -> bool:
    """줄 맨 앞·맨 뒤에 넓은 틈을 두고 떨어진 작은 조각(글자 하나 크기)이 원문에 없으면 줄에서 뺀다.
    예: 제목 영역 왼쪽 위 배지 안의 숫자 ‘5’가 ‘緊急クエスト’ 줄에 붙어 같이 지워지던 것.
    원문 그 끝이 숫자·기호면 진짜 글자일 수 있어 그대로 둔다. 줄 상자(ln["bbox"])를 고친다."""
    from app.icons import TOKEN_RE, _line_units

    x0, y0, x1, y1 = ln["bbox"]
    gh = float(ln.get("glyph_h") or 0)
    plain = TOKEN_RE.sub("■", text).strip()
    if gh <= 0 or len(plain) < 2 or x1 - x0 < 4 * gh:
        return False
    cols = tm.mask[max(0, y0):y1, max(0, x0):x1].any(axis=0)
    xs = np.nonzero(cols)[0]
    if len(xs) < 2:
        return False
    # 잉크가 있는 열을 틈으로 나눈 덩이들
    segs, start = [], xs[0]
    for a, b in zip(xs[:-1], xs[1:]):
        if b - a > 1:
            segs.append((start, a + 1))
            start = b
    segs.append((start, xs[-1] + 1))
    if len(segs) < 2:
        return False
    changed = False
    for end in ("left", "right"):
        if len(segs) < 2:
            break
        s, nxt = (segs[0], segs[1]) if end == "left" else (segs[-1], segs[-2])
        gap = (nxt[0] - s[1]) if end == "left" else (s[0] - nxt[1])
        ch = plain[0] if end == "left" else plain[-1]
        rest = (segs[-1][1] - nxt[0]) if end == "left" else (nxt[1] - segs[0][0])
        need = TRIM_KEEP * _line_units(text) * gh  # 원문 글자가 차지할 폭 (어림)
        if (gap >= 2.5 * gh and s[1] - s[0] <= 1.6 * gh and rest >= need
                and not re.match(r"[0-9０-９+＋\-−×xX■]", ch)):
            a, b = x0 + int(s[0]), x0 + int(s[1])
            tm.mask[max(0, y0 - int(gh)):y1 + int(gh), max(0, a - 2):b + 2] = False
            segs = segs[1:] if end == "left" else segs[:-1]
            changed = True
    if changed:
        ln["bbox"] = [x0 + int(segs[0][0]), y0, x0 + int(segs[-1][1]), y1]
    return changed


def _extra_lines(lines: list[dict], src: list[str], box_y: tuple | None = None,
                 glyph_px: float | None = None) -> list[dict]:
    """찾은 줄이 원문보다 많을 때 원문에 없는 줄을 고른다.
    줄 폭이 원문 줄 길이(글자 수)와 가장 잘 맞게 남는 줄을 뺀다 (원문이 빠뜨린 숫자 타일·카드 번호 등).
    폭이 같게 맞으면 상자 위·아래 끝에 붙은 줄(띠 안으로 들어온 그림 조각)을 먼저 뺀다. 그다음은 영역의 대표 글자
    크기(glyph_px)를 알면 그와 가장 다른 줄부터 (주사위 속 큰 숫자·작은 카드 번호), 모르면 글자가 작은 줄부터 뺀다.
    (원문이 한 줄이면 폭 맞춤은 늘 같아서, 작은 줄부터 빼면 진짜 글 줄 대신 주사위 숫자 줄이 남았다: 2쪽)"""
    from itertools import combinations

    from app.icons import TOKEN_RE, _line_units

    n_drop = len(lines) - len(src)
    if n_drop <= 0:
        return []

    def smallest() -> list[dict]:
        out: list[dict] = []
        while len(lines) - len(out) > len(src):
            out.append(min((ln for ln in lines if ln not in out), key=lambda ln: ln["glyph_h"]))
        return out

    if n_drop > 3 or len(lines) > 14:
        return smallest()

    def at_edge(ln) -> bool:  # 상자 위·아래 끝에 닿은 줄: 글자 줄은 보통 상자 가장자리에 붙지 않는다
        return box_y is not None and (ln["bbox"][1] <= box_y[0] + 1 or ln["bbox"][3] >= box_y[1] - 1)
    widths = [ln["bbox"][2] - ln["bbox"][0] for ln in lines]
    units = [_line_units(t) for t in src]
    icon_only = [bool(TOKEN_RE.findall(t)) and not TOKEN_RE.sub("", t).strip() for t in src]
    best, best_key = None, None
    for drop in combinations(range(len(lines)), n_drop):
        keep = [i for i in range(len(lines)) if i not in drop]
        txt = [(i, j) for i, j in zip(keep, range(len(src))) if not icon_only[j]] or list(zip(keep, range(len(src))))
        r = sum(widths[i] for i, _j in txt) / max(sum(units[j] for _i, j in txt), 1e-6)
        err = sum(abs(widths[i] - units[j] * r) for i, j in txt) / max(sum(widths[i] for i, _j in txt), 1)
        size_key = (-sum(abs(lines[i]["glyph_h"] - glyph_px) for i in drop) if glyph_px
                    else sum(lines[i]["glyph_h"] for i in drop))
        key = (round(err, 2), sum(1 for i in keep if at_edge(lines[i])), size_key)
        if best_key is None or key < best_key:
            best, best_key = drop, key
    if best is None or best_key[0] >= 0.35:
        return smallest()
    return [lines[i] for i in best]


def match_source_lines(tm: TextMask, lay: dict, source: str, icon_rows: list | None = None,
                       img: np.ndarray | None = None, allowed_px: np.ndarray | None = None,
                       keep_idx: list[int] | None = None, keep_rows: list | None = None,
                       _info: dict | None = None) -> dict:
    """찾은 줄을 읽은 원문 줄에 맞춘다. tm.mask를 고친다(남길 줄은 지우지 않음).

    - 원문보다 줄이 많으면 가장 작은 글자 줄(카드 번호 ‘A-001’ 같은 것)부터 원문에 없는 줄로 보고 남긴다
    - 아이콘·숫자만 있는 원문 줄은 그 자리를 지우지 않고 keep=True로 표시 (한글 넣기도 그리지 않는다)
    - keep_idx: 원본 그대로 남길 원문 줄 번호 (빈 줄 뺀 기준. 아이콘 문단만 원본 그대로인 칸), keep_rows: 그 칸 아이콘
      자리의 세로 범위 (sub px). 그 줄도 keep=True로 남긴다. 남길 줄을 확실히 찾지 못하면 영역을 하나도 지우지 않고
      keep_failed=True (_match_keeping)
    _info: 주면 원문 줄과 맞췄는지 적는다 ({"matched": bool})
    """
    if keep_idx is not None:
        return _match_keeping(tm, lay, source, keep_idx, keep_rows or [], icon_rows, img, allowed_px)
    src = [t for t in source.split("\n") if t.strip()]
    lines = list(lay.get("lines") or [])
    if not src or not lines:
        return lay
    src_text = [t for t in src if not is_icon_line(t)]
    if len(src_text) < len(src):
        # 색 그림 아이콘만 있는 줄(예: 큰 칼 두 개)은 글자로 잡히지 않는다: 그 줄은 그대로 두고 글 줄끼리 맞춘다.
        # 아이콘 자리(icon_rows: 아이콘만 있는 줄의 아이콘 세로 범위)를 알면, 그 높이에 잡힌 줄이 없을 때만.
        def on_icon(ln) -> bool:
            y0, y1 = ln["bbox"][1], ln["bbox"][3]
            return any(min(y1, b) - max(y0, a) > 0.5 * max(1, y1 - y0) for a, b in icon_rows or [])

        if icon_rows is not None:
            # 아이콘 자리에 잡힌 줄은 아이콘 조각이다: 지우지 않고 빼 둔 뒤, 남은 줄을 글 줄끼리 맞춘다
            # (그대로 두면 아이콘 조각이 줄 수를 늘려 카드 번호가 제목 자리를 차지하는 식으로 한 칸씩 밀린다)
            on = [ln for ln in lines if on_icon(ln)]
            if on:
                H_, W_ = tm.mask.shape
                for ln in on:
                    x0, y0, x1, y1 = ln["bbox"]
                    pad = int(max(3, 0.35 * ln["glyph_h"]))
                    tm.mask[max(0, y0 - pad):min(H_, y1 + pad), max(0, x0 - pad):min(W_, x1 + pad)] = False
                lines = [ln for ln in lines if not on_icon(ln)]
                if not lines:
                    return {**lay, "lines": []}
            src = src_text
        elif len(lines) == len(src_text):
            src = src_text
    extra = _extra_lines(lines, src, lay.get("box_y"), tm.glyph_px or None)
    kept = [ln for ln in lines if ln not in extra]
    restore = list(extra)
    if _info is not None:
        _info["matched"] = len(kept) == len(src)
    if len(kept) == len(src):
        for ln, t in zip(kept, src):
            if is_icon_line(t):
                ln["keep"] = True
                restore.append(ln)
    H, W = tm.mask.shape
    if len(kept) == len(src):
        trimmed = 0
        for ln, t in zip(kept, src):
            if not ln.get("keep") and _trim_stray_end(tm, ln, t):
                trimmed += 1
        # 줄 끝 조각을 정리한 뒤에(빼낸 배지 숫자와 줄 사이 틈을 채우지 않게) 줄 안의 넓은 빈 곳에서 연한 글자를 다시 찾는다
        if img is not None:
            refound = sum(fill_line_gaps(tm, img, ln, allowed=allowed_px) for ln in kept if not ln.get("keep"))
            if refound:
                tm.notes.append("줄 안에서 연한 글자를 더 찾아 지움")
        if trimmed:
            tm.notes.append(f"줄 끝에 떨어진 원문에 없는 조각 {trimmed}개는 지우지 않음")
        # 원문 줄과 맞으면: 지울 줄(아이콘만 있는 줄 빼고) 둘레 밖의 지울 픽셀은 원문에 없는 것(작은 카드 번호 등)이라 남긴다
        allowed = np.zeros_like(tm.mask)
        for ln in kept:
            if ln.get("keep"):
                continue
            x0, y0, x1, y1 = ln["bbox"]
            pad = int(max(4, 0.5 * ln["glyph_h"]))
            allowed[max(0, y0 - pad):min(H, y1 + pad), max(0, x0 - pad):min(W, x1 + pad)] = True
        tm.mask &= allowed
    for ln in restore:
        x0, y0, x1, y1 = ln["bbox"]
        pad = int(max(3, 0.35 * ln["glyph_h"]))
        tm.mask[max(0, y0 - pad):min(H, y1 + pad), max(0, x0 - pad):min(W, x1 + pad)] = False
    if lay.get("box_x"):  # 원문에 없는 줄·아이콘 조각을 뺀 뒤 정렬을 다시 본다
        _center_widest(kept, lay["box_x"][0], lay["box_x"][1], lay.get("cuts") or [])
    out = dict(lay)
    out["lines"] = kept
    if extra:
        tm.notes.append(f"원문에 없는 줄 {len(extra)}개는 지우지 않음")
    return out


def _match_keeping(tm: TextMask, lay: dict, source: str, keep_idx: list[int], keep_rows: list,
                   icon_rows: list | None, img: np.ndarray | None, allowed_px: np.ndarray | None) -> dict:
    """아이콘 문단만 원본 그대로인 칸: 남길 원문 줄(keep_idx)이 찾은 줄 중 어느 것인지 정해 keep=True로 남기고, 나머지 줄만
    원문의 나머지 줄에 맞춰 지운다. 남길 줄은 이렇게 찾는다 (확실할 때만):
    1) 카드 보기의 아이콘 자리(keep_rows) 높이에 잡힌 줄이 남길 줄 수와 같으면 그 줄들
    2) 남길 줄이 아이콘 줄뿐이고 그 자리에 잡힌 줄이 없으면 (색 그림 아이콘은 글자로 잡히지 않는다) 남길 줄 없음
       (아이콘 자리는 지우기가 지킨다)
    3) 찾은 줄 수가 원문 줄 수와 같으면 순서대로 (아이콘 자리에 잡힌 줄이 남길 줄이 아니면 맞지 않음)
    어느 것도 아니거나 나머지 줄을 맞추지 못하면 영역을 하나도 지우지 않고 keep_failed=True
    (아이콘 줄을 글 줄로 잘못 맞춰 지우는 것보다 칸 전체를 원본 그대로 두는 것이 안전하다)."""
    src_all = [t for t in source.split("\n") if t.strip()]
    lines = list(lay.get("lines") or [])
    keep_set = {i for i in keep_idx if 0 <= i < len(src_all)}

    def fail(why: str) -> dict:
        tm.mask[:] = False
        tm.core[:] = False
        tm.notes.append(f"원본 그대로 둘 문단의 줄을 찾지 못해 이 영역은 지우지 않음 ({why})")
        return {**lay, "lines": [], "keep_failed": True}

    rest_src = [t for i, t in enumerate(src_all) if i not in keep_set]
    if not lines:
        return fail("글자 줄을 찾지 못함")
    if not rest_src:
        return fail("남길 줄 밖에 원문이 없음")

    def on_keep(ln) -> bool:
        y0, y1 = ln["bbox"][1], ln["bbox"][3]
        return any(min(y1, b) - max(y0, a) > 0.5 * max(1, y1 - y0) for a, b in keep_rows)

    on = {id(ln) for ln in lines if on_keep(ln)}
    kept_src = [src_all[i] for i in sorted(keep_set)]
    if keep_rows and len(on) == len(keep_set):
        kept_lines = [ln for ln in lines if id(ln) in on]
    elif keep_rows and not on and all(is_icon_line(t) for t in kept_src):
        kept_lines = []
    elif len(lines) == len(src_all) and all(i in keep_set for i, ln in enumerate(lines) if id(ln) in on):
        kept_lines = [lines[i] for i in sorted(keep_set)]
    else:
        return fail(f"찾은 줄 {len(lines)}개, 원문 줄 {len(src_all)}개")
    ids = {id(ln) for ln in kept_lines}
    rest_lines = [ln for ln in lines if id(ln) not in ids]
    if not rest_lines:
        return fail("번역할 줄을 찾지 못함")
    info: dict = {}
    rest_rows = icon_rows if any(is_icon_line(t) for t in rest_src) else None
    out = match_source_lines(tm, {**lay, "lines": rest_lines}, "\n".join(rest_src), icon_rows=rest_rows, img=img,
                             allowed_px=allowed_px, _info=info)
    if not info.get("matched"):
        return fail(f"번역할 줄 {len(rest_lines)}개, 원문 {len(rest_src)}줄")
    H, W = tm.mask.shape
    for ln in kept_lines:
        ln["keep"] = True
        ln["keep_para"] = True  # 원본 그대로 둔 문단의 줄 (남은 글의 아이콘·숫자만 있는 줄 keep과 가른다: 합성이 이 줄만 뺀다)
        x0, y0, x1, y1 = ln["bbox"]
        pad = int(max(3, 0.35 * ln["glyph_h"]))
        tm.mask[max(0, y0 - pad):min(H, y1 + pad), max(0, x0 - pad):min(W, x1 + pad)] = False
    out = dict(out)
    out["lines"] = sorted(list(out["lines"]) + kept_lines, key=lambda ln: ln["bbox"][1])
    tm.notes.append(f"원본 그대로 둘 문단 줄 {len(kept_src)}개는 지우지 않음")
    return out
