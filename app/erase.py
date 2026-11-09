"""글자 지우기 (PRD 5.4, ClearScan 경로).

쪽마다 다음 순서로 처리한다.
  1. 원본 모양(텍스트 레이어 + 배경 이미지)을 배경 이미지 픽셀 격자에 맞춰 렌더링하고,
     태그 라벨(둥근 사각형)의 위치·크기·모서리 반경·색을 잰다.
  2. 영역과 겹치는 텍스트 객체를 레드랙션한다 (채우기 없음, 이미지·벡터 도형 유지).
     영역 안의 작은 인라인 이미지(ClearScan이 '!' 점 등을 그린 것)도 지운다.
  3. 배경 이미지의 영역 안을 정리한다: 영역 밖 픽셀로 배경(색·그라데이션)을 추정하고,
     배경과 다른 픽셀 + 텍스트 레이어 글자 bbox + 라벨 bbox를 마스크로 잡아 넓힌 뒤,
     고른 배경은 추정 배경 + 종이 결 잡음으로, 질감이 있는 곳은 OpenCV 인페인팅으로 채운다.
  4. 원본 배경 이미지 스트림은 그대로 두고, 정리한 영역만 무손실 패치 이미지로 덮는다(픽셀 격자 맞춤).
  5. 잰 라벨을 같은 모양의 벡터 둥근 사각형으로 다시 그린다 (글자 없음).

이미지 처리 함수(라벨 찾기, 반경 맞추기, 배경 추정, 정리)는 numpy 배열만 다루는 순수 함수다.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import cv2
import numpy as np

from app.regions import (
    Box,
    Region,
    RegionTemplate,
    area,
    clip_horizontally,
    expand,
    intersect,
    overlap_ratio,
    pt_to_px,
    px_box_outer,
    px_to_pt,
    ratio_to_pt,
    union,
)

SPAN_ASSIGN_MIN = 0.30  # span 넓이 중 이 비율 이상이 영역에 들어오면 그 영역의 글자로 보고 지운다
SPAN_PARTIAL_WARN = 0.95  # 이보다 적게 들어오면 '영역에 일부만 걸침' 경고
TAG_SEARCH_PAD = 0.25  # 라벨 찾을 때 태그 영역 높이 대비 위아래 여유
LABEL_MAX_ASPECT = 5.5  # 라벨 폭 / 높이 상한 (실측 ≈4.3)
TAG_MIN_H = 0.60  # 라벨 최소 높이 (태그 영역 높이 대비). 실측 라벨 ≈0.72, 17쪽 연기 조각 0.50
TAG_MAX_H = 1.0  # 라벨 최대 높이. 14·28쪽 연기 덩어리 ≈1.1
IMAGE_TEXT_PAD_PT = 1.2  # 이미지에 박힌 글자: 잉크 상자 둘레 여유 (번짐·그림자)
MAX_BG_ROTATION_DEG = 5.0  # 이보다 많이 돌아간 배경 이미지는 지원하지 않음
PATCH_INSET_PT = 0.002  # 패치 사각형을 안쪽으로 넣는 양 (grid-fit 바깥 번짐 방지)


# =====================================================================
# 순수 함수: 색·라벨
# =====================================================================

def rgb_to_hex(rgb) -> str:
    r, g, b = (int(round(float(v))) for v in rgb[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


def hex_to_rgb01(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def int_color_to_hex(c: int) -> str:
    return f"#{c & 0xFFFFFF:06x}"


def label_like_mask(rgb: np.ndarray) -> np.ndarray:
    """라벨 바탕일 수 있는 픽셀: 어둡거나(검정·짙은 초록 라벨), 채도가 있는 중간 밝기(색 라벨).

    종이(밝음)·흰 라벨 글자·회색 연기(채도 낮고 V 0.6 이상)는 빠진다.
    실측 (1~3쪽 스캔): 검정 라벨 ≈ (64,58,70) V0.27, 초록 ≈ (85,105,85) V0.41 S0.19,
    파랑 ≈ (32,61,121) S0.74, 보라 ≈ (143,78,158) S0.51, 연기 ≈ (150,159,166) V0.65 S0.10.
    """
    f = rgb.astype(np.float32) / 255.0
    mx = f.max(axis=2)
    mn = f.min(axis=2)
    sat = (mx - mn) / np.maximum(mx, 1e-6)
    return (mx < 0.45) | ((sat > 0.25) & (mx < 0.85) & (mx > 0.15))


def fit_corner_radius(mask: np.ndarray, steps: int = 80) -> float:
    """bbox에 딱 맞게 자른 채워진 라벨 마스크(h×w, bool)에서 모서리 반경(px)을 맞춘다.

    각 줄의 왼쪽·오른쪽 빈 칸 수를 원호 모양 기대값과 비교해 오차가 가장 작은 반경을 고른다.
    반경은 0 ~ min(h, w)/2 범위다.
    """
    m = np.asarray(mask, dtype=bool)
    h, w = m.shape
    if h == 0 or w == 0:
        return 0.0
    left = np.empty(h, np.float64)
    right = np.empty(h, np.float64)
    for y in range(h):
        xs = np.flatnonzero(m[y])
        if xs.size == 0:
            left[y] = right[y] = w / 2.0
        else:
            left[y] = xs[0]
            right[y] = w - 1 - xs[-1]
    yc = np.arange(h) + 0.5
    d = np.minimum(yc, h - yc)  # 가장 가까운 위/아래 가장자리까지 거리
    rmax = min(h, w) / 2.0
    best_r, best_err = 0.0, math.inf
    for r in np.linspace(0.0, rmax, steps):
        t = np.clip(r - d, 0.0, None)  # 원 중심에서 위아래 방향 거리
        e = np.where(d < r, r - np.sqrt(np.clip(r * r - t * t, 0.0, None)), 0.0)
        err = float(np.mean((left - e) ** 2 + (right - e) ** 2))
        if err < best_err - 1e-9:
            best_r, best_err = float(r), err
    return best_r


@dataclass
class LabelPx:
    bbox: tuple[float, float, float, float]  # px, (x0, y0, x1, y1), x1·y1 미포함
    radius: float  # px
    color: tuple[int, int, int]
    coverage: float  # 라벨색 픽셀 / 껍질 넓이
    mask: np.ndarray = field(repr=False)  # bbox 크기, 채워진 모양


def label_color_candidates(rgb: np.ndarray, min_count: int, max_colors: int = 12,
                           merge_dist: float = 24.0) -> list[np.ndarray]:
    """종이·흰 글자를 뺀 픽셀에서 자주 나오는 색(16단계 양자화 칸의 평균색)을 많은 순으로."""
    f = rgb.astype(np.float32) / 255.0
    mx, mn = f.max(axis=2), f.min(axis=2)
    sat = (mx - mn) / np.maximum(mx, 1e-6)
    base = ~((mx > 0.80) & (sat < 0.20))
    px = rgb[base].astype(np.int32)
    if px.size == 0:
        return []
    q = (px // 16) @ np.array([256, 16, 1])
    keys, inv, counts = np.unique(q, return_inverse=True, return_counts=True)
    order = np.argsort(-counts)
    out: list[np.ndarray] = []
    for k in order:
        if counts[k] < min_count or len(out) >= max_colors:
            break
        c = px[inv == k].mean(axis=0)
        if all(np.linalg.norm(c - o) > merge_dist for o in out):
            out.append(c)
    return out


def find_tag_labels(
    rgb: np.ndarray,
    min_h: float,
    max_h: float,
    min_aspect: float = 1.2,
    min_fill: float = 0.80,
    min_coverage: float = 0.30,  # 3쪽 "モンスター" 라벨은 흰 글자가 많아 0.40
    max_color_dist: float = 40.0,
    seg_color_dist: float = 22.0,  # 회색 라벨과 회색 연기의 색 거리 ≈31 (15쪽)
) -> list[LabelPx]:
    """잘라낸 이미지(rgb)에서 둥근 사각형 색 라벨을 찾는다. 왼쪽부터 정렬해 돌려준다.

    라벨 바탕은 한 가지 색이므로, 자주 나오는 색마다 그 색에 가까운 픽셀만으로 마스크를 만들어
    모양을 검사한다. 하나의 '색 있는 픽셀' 마스크를 쓰면 라벨에 닿은 푸른 연기가 라벨과 한 덩어리가
    되어 모양 검사에서 떨어지고(19·24쪽), 회색 라벨(人間)은 채도가 낮아 아예 빠진다.
    예전 방식(label_like_mask)도 함께 돌린다.

    두 번째로, 가장 확실한 라벨(기준 라벨)로 라벨 줄의 세로 범위를 알아낸 뒤 그 줄 안에서만 다시 찾는다.
    라벨 위에 닿은 연기가 잘려 나가 회색 연기 위의 회색 라벨(15쪽)도 떨어져 나온다.
    끝으로 기준 라벨과 높이·위끝이 맞고 앞 라벨과 틈이 있는 것만 남긴다 (한 줄의 라벨은 같은 높이).
    """
    H, W = rgb.shape[:2]
    min_count = int(0.15 * min_h * min_h * min_aspect)
    masks = []
    for c in label_color_candidates(rgb, min_count):
        d = np.linalg.norm(rgb.astype(np.float32) - c.astype(np.float32), axis=2)
        masks.append(d < seg_color_dist)
    masks.append(label_like_mask(rgb))
    args = (min_h, max_h, min_aspect, min_fill, min_coverage, max_color_dist)

    cands: list[LabelPx] = []
    for m in masks:
        cands += _labels_from_mask(rgb, m, *args)
    found = _keep_largest(cands)
    if not found:
        return []

    anchor = max(found, key=lambda l: l.coverage * (l.bbox[2] - l.bbox[0]))
    ay0, ay1 = anchor.bbox[1], anchor.bbox[3]
    ah = ay1 - ay0
    b0, b1 = max(0, int(ay0) - 3), min(H, int(math.ceil(ay1)) + 3)
    for m in masks:
        mm = np.zeros_like(m)
        mm[b0:b1] = m[b0:b1]
        # 줄 안에서는 위아래 끝에 닿아도 된다 (줄 밖은 이미 잘렸다). 연기만 있는 조각은 높이가 모자라 빠진다
        cands += _labels_from_mask(rgb, mm, *args, bounds=(0, -1, W, H + 1))
    aligned = [l for l in cands
               if abs((l.bbox[3] - l.bbox[1]) - ah) <= 0.15 * ah and abs(l.bbox[1] - ay0) <= 0.25 * ah]
    out: list[LabelPx] = []
    for l in _keep_largest(aligned):  # 왼쪽부터. 앞 라벨에 틈 없이 붙은 것은 연기로 본다
        if out and l.bbox[0] - out[-1].bbox[2] < 0.15 * ah:
            continue
        out.append(l)
    return out


def _keep_largest(cands: list[LabelPx]) -> list[LabelPx]:
    """겹치는 후보는 넓은 것(조각이 아니라 라벨 전체)을 남긴다."""
    found: list[LabelPx] = []
    for lab in sorted(cands, key=lambda l: -(l.bbox[2] - l.bbox[0]) * (l.bbox[3] - l.bbox[1])):
        if not any(_overlap_small(lab.bbox, o.bbox) > 0.3 for o in found):
            found.append(lab)
    found.sort(key=lambda l: l.bbox[0])
    return found


def _overlap_small(a, b) -> float:
    """겹친 넓이 / 둘 중 작은 상자 넓이."""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return ix * iy / small if small > 0 else 0.0


def _labels_from_mask(rgb, mask, min_h, max_h, min_aspect, min_fill, min_coverage, max_color_dist,
                      bounds: tuple[int, int, int, int] | None = None) -> list[LabelPx]:
    """마스크 하나에서 둥근 사각형 라벨을 찾는다.

    흰 글자가 라벨 가장자리에 닿으면 라벨 픽셀이 여러 조각으로 갈라지므로,
    세로 범위가 겹치고 가로 틈이 좁으며 색이 같은 조각을 한 라벨로 묶은 뒤
    볼록 껍질(convex hull)로 모양을 복원한다.
      fill     = 껍질 넓이 / bbox 넓이 (둥근 사각형이면 0.85 이상)
      coverage = 라벨색 픽셀 / 껍질 넓이 (흰 글자가 많을수록 낮음)
    """
    m = np.asarray(mask).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    comps = []
    for k in range(1, n):
        x, y, w, h, a = stats[k]
        if a < 20:
            continue
        color = np.median(rgb[lab == k], axis=0)
        comps.append({"ids": [k], "box": [x, y, x + w, y + h], "color": color})
    comps.sort(key=lambda c: c["box"][0])

    def v_overlap(a, b) -> float:
        ov = min(a[3], b[3]) - max(a[1], b[1])
        return ov / max(1, min(a[3] - a[1], b[3] - b[1]))

    groups: list[dict] = []
    for c in comps:
        merged = False
        for g in groups:
            gb, cb = g["box"], c["box"]
            gh = max(gb[3] - gb[1], cb[3] - cb[1])
            gap = max(cb[0] - gb[2], gb[0] - cb[2])
            # 라벨 안 흰 글자(炎 한 글자 ≈ 0.6h, 人間 두 글자 ≈ 1.25h)로 갈라진 조각을 잇는다. 이웃 라벨과 합쳐지지 않게
            # 합친 폭이 라벨 한 개 폭(≈4.3h)을 넘지 않을 때만.
            if (
                gap <= 1.5 * gh
                and max(gb[2], cb[2]) - min(gb[0], cb[0]) <= LABEL_MAX_ASPECT * gh
                and v_overlap(gb, cb) >= 0.7
                and float(np.linalg.norm(g["color"] - c["color"])) <= max_color_dist
                and max(gb[3], cb[3]) - min(gb[1], cb[1]) <= max_h
            ):
                g["ids"] += c["ids"]
                g["box"] = [min(gb[0], cb[0]), min(gb[1], cb[1]), max(gb[2], cb[2]), max(gb[3], cb[3])]
                merged = True
                break
        if not merged:
            groups.append({"ids": list(c["ids"]), "box": list(c["box"]), "color": c["color"]})

    out: list[LabelPx] = []
    for g in groups:
        x0, y0, x1, y1 = g["box"]
        w, h = x1 - x0, y1 - y0
        if not (min_h <= h <= max_h) or w < min_aspect * h or w > LABEL_MAX_ASPECT * h:
            continue
        # 라벨은 찾는 범위(영역 위아래 여유 포함) 안에 통째로 들어 있다. 가장자리에 닿으면 연기·그림이다.
        bx0, by0, bx1, by1 = bounds or (0, 0, m.shape[1], m.shape[0])
        if x0 <= bx0 or y0 <= by0 or x1 >= bx1 or y1 >= by1:
            continue
        pix = np.isin(lab, g["ids"])
        pts = cv2.findNonZero(pix.astype(np.uint8))
        hull = cv2.convexHull(pts)
        filled = np.zeros(m.shape, np.uint8)
        cv2.fillConvexPoly(filled, hull, 1)
        sub = filled[y0:y1, x0:x1].astype(bool)
        fill = float(sub.mean())
        coverage = float(pix.sum()) / max(1, int(sub.sum()))
        if fill < min_fill or coverage < min_coverage:
            continue
        inner = cv2.erode(filled, np.ones((5, 5), np.uint8)).astype(bool) & pix
        if inner.sum() < 10:
            inner = pix
        color = tuple(int(v) for v in np.median(rgb[inner], axis=0))
        out.append(
            LabelPx(
                bbox=(float(x0), float(y0), float(x1), float(y1)),
                radius=fit_corner_radius(sub),
                color=color,  # type: ignore[arg-type]
                coverage=round(coverage, 3),
                mask=sub,
            )
        )
    out.sort(key=lambda l: l.bbox[0])
    return out


# =====================================================================
# 순수 함수: 배경 추정과 정리
# =====================================================================

def pushpull_fill(img: np.ndarray, known: np.ndarray) -> np.ndarray:
    """알려진 픽셀(known=True)로 나머지를 매끄럽게 메운다 (push-pull 피라미드 보간).

    img: H×W×C float32, known: H×W bool 또는 0~1 무게(float). 무게 1인 픽셀 값은 그대로 유지된다.
    """
    I = img.astype(np.float32)
    if I.ndim == 2:
        I = I[..., None]
    w = known.astype(np.float32)
    if w.sum() == 0:
        raise ValueError("알려진 픽셀이 없습니다")
    levels = []
    cI, cw = I * w[..., None], w
    while True:
        levels.append((cI, cw))
        h, wd = cw.shape
        if h <= 2 or wd <= 2:
            break
        size = ((wd + 1) // 2, (h + 1) // 2)
        cI = cv2.resize(cI, size, interpolation=cv2.INTER_AREA)
        if cI.ndim == 2:
            cI = cI[..., None]
        cw = cv2.resize(cw, size, interpolation=cv2.INTER_AREA)
    cI, cw = levels[-1]
    mean = (I * w[..., None]).sum(axis=(0, 1)) / float(w.sum())
    est = np.where(cw[..., None] > 1e-6, cI / np.maximum(cw, 1e-6)[..., None], mean)
    for cI, cw in reversed(levels[:-1]):
        up = cv2.resize(est, (cw.shape[1], cw.shape[0]), interpolation=cv2.INTER_LINEAR)
        if up.ndim == 2:
            up = up[..., None]
        col = cI / np.maximum(cw, 1e-6)[..., None]
        a = np.clip(cw, 0.0, 1.0)[..., None]
        est = a * col + (1.0 - a) * up
    return est.astype(np.float32)


def normalized_blur(img: np.ndarray, known: np.ndarray, sigma: float) -> np.ndarray:
    """알려진 픽셀만으로 가우시안 평균 (모르는 픽셀은 무게 0)."""
    w = known.astype(np.float32)
    num = cv2.GaussianBlur(img.astype(np.float32) * w[..., None], (0, 0), sigma)
    den = cv2.GaussianBlur(w, (0, 0), sigma)
    return num / np.maximum(den, 1e-6)[..., None]


def luminance(img: np.ndarray) -> np.ndarray:
    f = img.astype(np.float32)
    return 0.299 * f[..., 0] + 0.587 * f[..., 1] + 0.114 * f[..., 2]


def estimate_background(img: np.ndarray, known: np.ndarray, smooth: float = 2.0) -> np.ndarray:
    """알려진 픽셀로 배경(색·그라데이션)을 추정한 H×W×3 float 배열."""
    pre = normalized_blur(img, known, smooth)
    pre[~known] = 0
    return pushpull_fill(pre, known)


def smooth_background(img: np.ndarray, known: np.ndarray, sigma: float) -> np.ndarray:
    """알려진 픽셀의 넓은 가우시안 평균 (모르는 픽셀이 넓게 모인 곳은 push-pull로 메움)."""
    w = known.astype(np.float32)
    num = cv2.GaussianBlur(img.astype(np.float32) * w[..., None], (0, 0), sigma)
    den = cv2.GaussianBlur(w, (0, 0), sigma)
    valid = den > 0.15
    pre = num / np.maximum(den, 1e-6)[..., None]
    pre[~valid] = 0
    return pushpull_fill(pre, valid)


def fill_background(img: np.ndarray, known: np.ndarray, hole: np.ndarray, p: "CleanParams") -> np.ndarray:
    """구멍(hole)을 채울 배경.

    카드 배경은 주로 위→아래로 변한다(그림이 종이색으로 흐려지는 그라데이션). 그래서
    1) 같은 줄의 알려진 픽셀을 가로로 넓게 평균한 값(가로 σ 큼, 세로 σ 작음)을 기본으로 하고,
    2) 구멍 가장자리의 실제 값과의 차이(잔차)를 안쪽으로 이어 붙이되 거리에 따라 줄여
       경계에서 이음매가 생기지 않게 한다.
    """
    I = img.astype(np.float32)
    w = known.astype(np.float32)
    num = cv2.GaussianBlur(I * w[..., None], (0, 0), sigmaX=p.row_sigma_x, sigmaY=p.row_sigma_y)
    den = cv2.GaussianBlur(w, (0, 0), sigmaX=p.row_sigma_x, sigmaY=p.row_sigma_y)
    # 같은 줄에 알려진 픽셀이 적은 곳(큰 구멍)일수록 위아래에서 보간한 값에 기대도록
    # 무게를 부드럽게 준다. 딱 잘라 나누면 구멍 끝에서 세로 경계가 생긴다(2쪽 제목 오른쪽 끝).
    conf = np.clip(den / p.row_conf, 0.0, 1.0)
    base = num / np.maximum(den, 1e-6)[..., None]
    base[conf <= 0] = 0
    base = pushpull_fill(base, conf)
    resid = I - base
    resid = normalized_blur(resid, known, 1.5)
    resid[~known] = 0
    resid = pushpull_fill(resid, known)
    dist = cv2.distanceTransform(hole.astype(np.uint8), cv2.DIST_L2, 3)
    out = base + resid * np.exp(-dist / p.edge_decay_px)[..., None]
    return cv2.GaussianBlur(out, (0, 0), 1.0)


def transfer_texture(
    hp: np.ndarray, src: np.ndarray, box: tuple[int, int, int, int], rng: np.random.Generator,
    block: int = 8, rows: int = 24,
) -> np.ndarray | None:
    """box(y0, y1, x0, x1)를 덮을 질감(H×W×3)을 src(깨끗한 픽셀)의 고주파 hp에서 블록째 빌려 온다.

    블록마다 같은 높이(±rows px)의 깨끗한 블록 하나를 무작위로 고른다. 블록 격자는 이미지
    원점 기준 block px 간격이라 JPEG 8×8 블록 결이 그대로 이어진다. 후보가 없으면 None.
    """
    H, W = src.shape
    by0, by1, bx0, bx1 = box
    # 블록 전체가 깨끗한 위치 (적분 영상으로 빠르게)
    ii = cv2.integral(src.astype(np.uint8))
    nby, nbx = H // block, W // block
    if nby == 0 or nbx == 0:
        return None
    ys = np.arange(nby) * block
    xs = np.arange(nbx) * block
    full = (ii[ys[:, None] + block, xs[None, :] + block] - ii[ys[:, None], xs[None, :] + block]
            - ii[ys[:, None] + block, xs[None, :]] + ii[ys[:, None], xs[None, :]]) == block * block
    if not full.any():
        return None
    cand_r, cand_c = np.nonzero(full)
    out = np.zeros((by1 - by0, bx1 - bx0, 3), np.float32)
    gy0, gx0 = (by0 // block) * block, (bx0 // block) * block
    for ty in range(gy0, by1, block):
        sel = np.abs(cand_r * block - ty) <= rows
        if not sel.any():
            sel = np.abs(cand_r * block - ty) == np.abs(cand_r * block - ty).min()
        rr, cc = cand_r[sel], cand_c[sel]
        for tx in range(gx0, bx1, block):
            k = rng.integers(len(rr))
            sy, sx = rr[k] * block, cc[k] * block
            tile = hp[sy:sy + block, sx:sx + block]
            # 대상 칸과 겹치는 부분만 복사
            oy0, oy1 = max(ty, by0), min(ty + block, by1)
            ox0, ox1 = max(tx, bx0), min(tx + block, bx1)
            out[oy0 - by0:oy1 - by0, ox0 - bx0:ox1 - bx0] = tile[oy0 - ty:oy1 - ty, ox0 - tx:ox1 - tx]
    return out


def _deviation(img: np.ndarray, bg: np.ndarray) -> np.ndarray:
    d = np.linalg.norm(img.astype(np.float32) - bg, axis=2)
    return cv2.blur(d, (3, 3))


def _disk(r: int) -> np.ndarray:
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))


@dataclass
class CleanParams:
    min_threshold: float = 14.0  # 배경과 이 거리(RGB) 이상 다르면 글자·흔적으로 본다
    noise_k: float = 4.0  # 임계값 = max(min_threshold, noise_k × 종이 결 잡음)
    dilate_px: int = 3  # 마스크 넓히기 (테두리·그림자)
    smooth_sigma: float = 6.0  # 이상치 걷어내기용 배경 추정의 흐림 정도(px)
    outside_threshold: float = 80.0  # 영역 밖 기준 배경과 이만큼 다르면 무조건 글자로 본다
    row_sigma_x: float = 30.0  # 채울 배경: 같은 줄 가로 평균 폭(px)
    row_sigma_y: float = 5.0  # 채울 배경: 세로 평균 폭(px)
    row_conf: float = 0.5  # 가로 평균의 무게: 알려진 픽셀 비율이 이 값 이상이면 그대로 믿는다
    edge_decay_px: float = 12.0  # 가장자리 잔차를 안쪽으로 이어 붙이는 거리(px)
    texture_sigma: float = 9.0  # 주변 잔차 표준편차가 이보다 크면 '질감' → 인페인팅
    inpaint_radius: int = 5
    strong_threshold: float = 50.0  # '이미지에 박힌 글자' 판정용 강한 차이
    texture_sigma_hp: float = 3.0  # 빌려 올 질감(고주파)을 가르는 흐림 정도(px)
    texture_block_px: int = 8  # 질감을 빌려 오는 블록 크기 (JPEG 8×8 블록과 같게)
    texture_rows_px: int = 24  # 질감을 빌려 올 때 위아래로 찾는 범위(px). 배경은 세로로 변한다
    feather_px: float = 3.0  # 마스크 가장자리에서 원본과 섞는 폭(px)


@dataclass
class CleanResult:
    image: np.ndarray  # 정리한 이미지 (uint8)
    mask: np.ndarray  # 바꾼 픽셀 (bool)
    flat_mask: np.ndarray  # 추정 배경으로 채운 픽셀
    inpaint_mask: np.ndarray  # 인페인팅한 픽셀
    strong: np.ndarray  # 배경과 크게 다른 픽셀 (정리 전)
    threshold: float
    noise_sigma: float


def clean_area(
    img: np.ndarray,
    work: np.ndarray,
    forced: np.ndarray,
    protect: np.ndarray,
    params: CleanParams | None = None,
    seed: int = 0,
) -> CleanResult:
    """work 영역 안의 글자·흔적을 지운다.

    img: H×W×3 uint8. work: 정리 대상 영역. forced: 무조건 지울 픽셀(글자 bbox, 라벨).
    protect: 절대 바꾸지 않고 배경 추정에도 쓰지 않는 픽셀.
    """
    p = params or CleanParams()
    I = img.astype(np.float32)
    work = work & ~protect
    forced = forced & work

    # 1차: 영역 밖만 보고 배경 추정
    known0 = ~work & ~protect
    ring0 = cv2.dilate(work.astype(np.uint8), _disk(12)).astype(bool) & known0
    lum = luminance(I)
    lum_s = cv2.GaussianBlur(lum, (0, 0), 2.0)
    resid = (lum - lum_s)[ring0]
    noise = float(np.median(np.abs(resid)) * 1.4826) if resid.size else 3.0
    thr = max(p.min_threshold, p.noise_k * noise)

    # 영역 안의 깨끗한 종이 픽셀도 배경 추정에 쓴다 (영역 밖만 쓰면 큰 영역 가운데가
    # 위쪽 그림 색으로 끌려가 회색 사각형이 생긴다). 강한 차이부터 점점 약한 차이까지
    # 이상치를 걷어내며 매끄러운 배경을 다시 추정한다. 마스크는 늘어나기만 한다.
    dil = _disk(p.dilate_px)
    unknown = cv2.dilate(forced.astype(np.uint8), dil).astype(bool) & ~protect
    work_dil = cv2.dilate(work.astype(np.uint8), dil).astype(bool) & ~protect
    # 0단계: 영역 밖 픽셀만으로 추정한 배경과 아주 크게 다른 픽셀(이미지에 박힌 굵은 글자 등).
    # 굵은 획 안쪽은 지역 추정만으로는 '배경'처럼 보이므로 이 단계가 필요하다(2쪽 제목).
    B0 = cv2.GaussianBlur(estimate_background(I, known0), (0, 0), 3.0)
    strong0 = (_deviation(I, B0) > p.outside_threshold) & work
    unknown |= cv2.dilate(strong0.astype(np.uint8), dil).astype(bool) & work_dil
    for t in (max(35.0, 2 * thr), thr, thr):
        known = ~unknown & ~protect
        B = smooth_background(I, known, p.smooth_sigma)
        dev = (_deviation(I, B) > t) & work
        unknown |= cv2.dilate(dev.astype(np.uint8), dil).astype(bool) & work_dil
    M2 = unknown & work_dil

    # 채울 배경: 마스크 밖 픽셀 값을 경계로 그대로 이어받도록 보간
    known2 = ~M2 & ~protect
    B3 = fill_background(I, known2, M2, p)
    strong = (_deviation(I, B3) > p.strong_threshold) & work

    out = I.copy()
    flat_mask = np.zeros(M2.shape, bool)
    inpaint_mask = np.zeros(M2.shape, bool)
    rng = np.random.default_rng(seed)
    # 질감: 주변 깨끗한 픽셀의 고주파 성분(종이 결 + JPEG 블록 결)을 블록째 빌려 온다.
    # 가우시안 잡음만 얹으면 매끈한 면과 원본 JPEG 질감 사이에 경계가 보인다.
    hp = I - normalized_blur(I, known2, p.texture_sigma_hp)
    lim = max(12.0, 3.0 * noise)
    hp = np.clip(hp, -lim, lim)
    tex_src = cv2.erode(known2.astype(np.uint8), _disk(1)).astype(bool) & (_deviation(I, B3) < thr)
    n, labels = cv2.connectedComponents(M2.astype(np.uint8), connectivity=8)
    lumB = luminance(B3)
    for k in range(1, n):
        comp = labels == k
        ring = cv2.dilate(comp.astype(np.uint8), _disk(6)).astype(bool) & known2
        sig = float(np.std((lum - lumB)[ring])) if ring.sum() >= 10 else 0.0
        if sig > p.texture_sigma:
            inpaint_mask |= comp
        else:
            ys, xs = np.nonzero(comp)
            y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
            g = transfer_texture(hp, tex_src, (y0, y1, x0, x1), rng, p.texture_block_px, p.texture_rows_px)
            if g is None:  # 빌려 올 질감이 없으면 종이 결 흉내 잡음
                amp = float(np.clip(sig, 1.0, 6.0))
                g1 = cv2.GaussianBlur(rng.standard_normal((y1 - y0, x1 - x0)).astype(np.float32), (0, 0), 0.7)
                g = (g1 * amp / max(float(g1.std()), 1e-6))[..., None].repeat(3, axis=2)
            sub = comp[y0:y1, x0:x1]
            patch = B3[y0:y1, x0:x1] + g
            region = out[y0:y1, x0:x1]
            region[sub] = patch[sub]
            flat_mask |= comp
    # 가장자리 부드럽게: 마스크 바깥쪽 몇 px는 원본(깨끗한 픽셀일 때만)과 섞어 칼로 자른 듯한
    # 경계(특히 그림 무늬가 영역 위쪽에서 수평으로 끊기는 것)를 줄인다.
    if p.feather_px > 0:
        dist = cv2.distanceTransform(M2.astype(np.uint8), cv2.DIST_L2, 3)
        alpha = np.clip(dist / p.feather_px, 0.0, 1.0)
        clean_orig = _deviation(I, B3) < thr
        alpha = np.where(clean_orig & flat_mask, alpha, 1.0)[..., None]
        out = np.where(M2[..., None], alpha * out + (1 - alpha) * I, out)
    out_u8 = np.clip(np.rint(out), 0, 255).astype(np.uint8)
    if inpaint_mask.any():
        bgr = cv2.cvtColor(out_u8, cv2.COLOR_RGB2BGR)
        bgr = cv2.inpaint(bgr, inpaint_mask.astype(np.uint8) * 255, p.inpaint_radius, cv2.INPAINT_TELEA)
        out_u8 = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    out_u8[protect] = img[protect]
    return CleanResult(out_u8, M2, flat_mask, inpaint_mask, strong, thr, noise)


def dark_ink(img: np.ndarray, dark_delta: float = 70.0, bg_px: int = 61) -> np.ndarray:
    """주변 종이보다 dark_delta 이상 어두운 픽셀 (회색 띠 214 vs 흰 종이 250 ≈ 36은 잉크가 아님; 글자 ≈ 160).

    배경은 bg_px 크기의 최대값 필터(가장 밝은 이웃)를 흐린 것이다. 중앙값 흐림은 굵은 획(+2 숫자)
    안쪽에서 배경까지 어두워져 획 가장자리만 잉크로 잡힌다.
    """
    # 종이 결 잡음(밝은 점)에 최대값 필터가 끌려가지 않도록 먼저 살짝 흐린다
    lum = cv2.GaussianBlur(luminance(img.astype(np.float32)), (0, 0), 1.2)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (bg_px, bg_px))
    bg = cv2.GaussianBlur(cv2.dilate(lum, k), (0, 0), bg_px / 6)
    return (bg - lum) > dark_delta


def text_ink_box(img: np.ndarray, box: tuple[int, int, int, int], max_h_px: float,
                 dark_delta: float = 70.0, keep_above: str | None = None,
                 above_gap_px: float = 1.0) -> tuple[tuple[int, int, int, int] | None, list]:
    """box 안 글자 잉크(세로 max_h_px 이하 덩어리)를 감싸는 상자와, 남길 덩어리 상자 목록 (img 좌표).

    keep_above="up"|"down": 글자 줄(가장 큰 글자들의 위끝)보다 완전히 위(down이면 아래, 180도 돌린 영역)에
    있는 덩어리(예: 이름 위 왕관 아이콘)는 글자가 아니라고 보고 상자에서 빼고 남길 목록에 넣는다.
    """
    x0, y0, x1, y1 = box
    ink = dark_ink(img, dark_delta)[y0:y1, x0:x1].astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    keep = [k for k in range(1, n) if st[k, cv2.CC_STAT_HEIGHT] <= max_h_px and st[k, cv2.CC_STAT_AREA] >= 4]
    if not keep:
        return None, []
    kept_out: list = []
    if keep_above in ("up", "down"):
        hmax = max(st[k, cv2.CC_STAT_HEIGHT] for k in keep)
        main = [k for k in keep if st[k, cv2.CC_STAT_HEIGHT] >= 0.6 * hmax]
        if keep_above == "up":
            line_edge = min(st[k, 1] for k in main)
            out = [k for k in keep if st[k, 1] + st[k, 3] <= line_edge - above_gap_px]
        else:
            line_edge = max(st[k, 1] + st[k, 3] for k in main)
            out = [k for k in keep if st[k, 1] >= line_edge + above_gap_px]
        kept_out = [(x0 + st[k, 0], y0 + st[k, 1], x0 + st[k, 0] + st[k, 2], y0 + st[k, 1] + st[k, 3]) for k in out]
        keep = [k for k in keep if k not in out]
    bx0 = min(st[k, 0] for k in keep)
    by0 = min(st[k, 1] for k in keep)
    bx1 = max(st[k, 0] + st[k, 2] for k in keep)
    by1 = max(st[k, 1] + st[k, 3] for k in keep)
    return (x0 + bx0, y0 + by0, x0 + bx1, y0 + by1), kept_out


def tall_ink_mask(img: np.ndarray, box: tuple[int, int, int, int], max_h_px: float,
                  dark_delta: float = 70.0, grow_px: int = 6) -> np.ndarray:
    """box에 걸친 잉크 덩어리 중 세로로 max_h_px보다 큰 것(그림·큰 숫자)의 마스크 (img 크기).

    잉크 = 주변 배경(큰 중앙값 흐림)보다 dark_delta 이상 어두운 픽셀. 덩어리는 img 전체에서 찾으므로
    영역 밖으로 이어지는 그림도 통째로 잡힌다. 테두리 번짐(안티에일리어싱 회색 테)까지 덮도록 grow_px 넓힌다.
    덜 넓히면 테가 배경으로 쓰여 옆 글자 자리에 회색 얼룩이 생긴다.
    """
    ink = dark_ink(img, dark_delta).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    x0, y0, x1, y1 = box
    out = np.zeros(ink.shape, bool)
    for k in range(1, n):
        x, y, w, h, _a = st[k]
        if h <= max_h_px:
            continue
        if x + w <= x0 or x >= x1 or y + h <= y0 or y >= y1:
            continue
        out |= lab == k
    if out.any() and grow_px:
        out = cv2.dilate(out.astype(np.uint8), _disk(grow_px)).astype(bool)
    return out


def non_paper_mask(img: np.ndarray, lum_delta: float = 25.0, min_sat: float = 0.12,
                   bg_px: int = 31, grow_px: int = 4) -> np.ndarray:
    """종이가 아닌 픽셀(그림, 색 번짐, 글자): 주변 종이보다 lum_delta 이상 어둡거나 채도가 min_sat 이상."""
    f = img.astype(np.float32) / 255.0
    mx, mn = f.max(axis=2), f.min(axis=2)
    sat = (mx - mn) / np.maximum(mx, 1e-6)
    m = dark_ink(img, lum_delta, bg_px) | (sat > min_sat)
    if grow_px:
        m = cv2.dilate(m.astype(np.uint8), _disk(grow_px)).astype(bool)
    return m


def fill_box(mask: np.ndarray, box: tuple[int, int, int, int], value: bool = True) -> None:
    x0, y0, x1, y1 = box
    mask[max(0, y0) : max(0, y1), max(0, x0) : max(0, x1)] = value


# =====================================================================
# PDF 쪽 처리
# =====================================================================

@dataclass
class BackgroundImage:
    xref: int
    bbox: Box  # pt (page.rect 좌표). 회전된 이미지면 네 모서리를 감싸는 사각형
    size: tuple[int, int]  # px (w, h)
    # 이미지 단위 사각형 -> 페이지 좌표(pt, 위가 0) 행렬 (a, b, c, d, e, f).
    # 단위 (s, t)의 t는 위에서부터. 없으면 bbox에 축 정렬로 놓인 것으로 본다.
    transform: tuple | None = None

    @property
    def rotated(self) -> bool:
        t = self.transform
        return t is not None and (abs(t[1]) > 1e-6 or abs(t[2]) > 1e-6)

    @property
    def angle_deg(self) -> float:
        t = self.transform
        return 0.0 if t is None else math.degrees(math.atan2(t[1], t[0]))

    @property
    def dpi(self) -> tuple[float, float]:
        if self.rotated:
            a, b, c, d = self.transform[:4]
            return self.size[0] / math.hypot(a, b) * 72.0, self.size[1] / math.hypot(c, d) * 72.0
        return (
            self.size[0] / (self.bbox[2] - self.bbox[0]) * 72.0,
            self.size[1] / (self.bbox[3] - self.bbox[1]) * 72.0,
        )

    @property
    def pt_per_px(self) -> float:
        return 72.0 / self.dpi[0]

    def px_point_to_pt(self, u: float, v: float) -> tuple[float, float]:
        a, b, c, d, e, f = self.transform
        s, t = u / self.size[0], v / self.size[1]
        return s * a + t * c + e, s * b + t * d + f

    def pt_point_to_px(self, x: float, y: float) -> tuple[float, float]:
        a, b, c, d, e, f = self.transform
        det = a * d - b * c
        x, y = x - e, y - f
        s, t = (x * d - y * c) / det, (y * a - x * b) / det
        return s * self.size[0], t * self.size[1]

    def pt_to_px(self, b: Box) -> Box:
        """pt 사각형 -> px. 회전된 이미지면 네 모서리를 옮겨 감싸는 사각형."""
        if not self.rotated:
            return pt_to_px(b, self.bbox, self.size)
        pts = [self.pt_point_to_px(x, y) for x in (b[0], b[2]) for y in (b[1], b[3])]
        return (min(q[0] for q in pts), min(q[1] for q in pts), max(q[0] for q in pts), max(q[1] for q in pts))

    def px_to_pt(self, b: Box) -> Box:
        if not self.rotated:
            return px_to_pt(b, self.bbox, self.size)
        pts = [self.px_point_to_pt(u, v) for u in (b[0], b[2]) for v in (b[1], b[3])]
        return (min(q[0] for q in pts), min(q[1] for q in pts), max(q[0] for q in pts), max(q[1] for q in pts))


def find_background_image(page) -> BackgroundImage:
    """페이지 대부분을 덮는 가장 큰 이미지(xref 있는 것)."""
    prect = tuple(page.rect)
    best = None
    for info in page.get_image_info(xrefs=True):
        xref = info.get("xref", 0)
        bbox = tuple(info["bbox"])
        if xref <= 0:
            continue
        cover = area(intersect(bbox, prect)) / area(prect)
        if cover < 0.9:
            continue
        a, b, c, d, _, _ = info["transform"]
        # Acrobat이 기울어진 스캔을 바로 세우려고 이미지를 조금 돌려 놓은 경우(1~2도)는 지원한다.
        if a <= 0 or d <= 0 or a * d - b * c <= 0 or abs(math.degrees(math.atan2(b, a))) > MAX_BG_ROTATION_DEG:
            raise NotImplementedError(f"크게 회전·반전된 배경 이미지는 아직 지원하지 않습니다: {info['transform']}")
        if best is None or cover > best[0]:
            best = (cover, BackgroundImage(xref, bbox, (info["width"], info["height"]), tuple(info["transform"])))
    if best is None:
        raise RuntimeError("배경 이미지를 찾지 못했습니다 (순수 벡터 페이지는 아직 지원하지 않음)")
    return best[1]


def load_image_rgb(doc, xref: int) -> np.ndarray:
    import pymupdf

    pix = pymupdf.Pixmap(doc, xref)
    if pix.alpha or pix.colorspace is None or pix.colorspace.n != 3:
        raise NotImplementedError(
            f"RGB가 아닌 배경 이미지(cs={pix.colorspace}, alpha={pix.alpha})는 아직 지원하지 않습니다 (CMYK 보존 규칙)"
        )
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n).copy()


def render_on_image_grid(page, bg: BackgroundImage, base: np.ndarray) -> tuple[np.ndarray, list[str]]:
    """페이지 전체 모양(텍스트 포함)을 배경 이미지 픽셀 격자에 맞춰 렌더링한다.

    페이지 밖으로 나간 이미지 픽셀은 base(원래 이미지) 값을 쓴다.
    """
    import pymupdf

    if bg.rotated:
        return _render_on_rotated_grid(page, bg, base)
    warnings = []
    sx = bg.size[0] / (bg.bbox[2] - bg.bbox[0])
    sy = bg.size[1] / (bg.bbox[3] - bg.bbox[1])
    pix = page.get_pixmap(matrix=pymupdf.Matrix(sx, sy), alpha=False)
    R = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[..., :3]
    ox = (bg.bbox[0] - page.rect.x0) * sx - pix.x
    oy = (bg.bbox[1] - page.rect.y0) * sy - pix.y
    if abs(ox - round(ox)) > 0.1 or abs(oy - round(oy)) > 0.1:
        warnings.append(f"렌더링과 이미지 격자가 정수 픽셀로 맞지 않음 (ox={ox:.2f}, oy={oy:.2f})")
    ox, oy = int(round(ox)), int(round(oy))
    out = base.copy()
    H, W = base.shape[:2]
    # 이미지 (u, v) -> 렌더 (u+ox, v+oy)
    u0, v0 = max(0, -ox), max(0, -oy)
    u1, v1 = min(W, R.shape[1] - ox), min(H, R.shape[0] - oy)
    out[v0:v1, u0:u1] = R[v0 + oy : v1 + oy, u0 + ox : u1 + ox]
    return out, warnings


def _render_on_rotated_grid(page, bg: BackgroundImage, base: np.ndarray) -> tuple[np.ndarray, list[str]]:
    """회전된 배경 이미지: 페이지를 2배 해상도로 렌더링한 뒤 이미지 격자로 되돌려 샘플링한다.

    정수 격자로 딱 맞출 수 없으므로 배경까지 한 번 다시 샘플링된다. 렌더 - 원본 차이로 글자를
    찾을 때 경계에 생기는 작은 차이는 span 상자 안으로만 쓰이므로 괜찮다.
    """
    import pymupdf

    k = 2.0 * bg.dpi[0] / 72.0
    pix = page.get_pixmap(matrix=pymupdf.Matrix(k, k), alpha=False)
    R = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[..., :3]
    a, b, c, d, e, f = bg.transform
    W, H = bg.size
    x0, y0 = page.rect.x0, page.rect.y0
    # 이미지 픽셀 중심 (u+.5, v+.5) -> 페이지 pt -> 렌더 픽셀 좌표
    m = np.array([
        [k * a / W, k * c / H, 0.0],
        [k * b / W, k * d / H, 0.0],
    ])
    m[0, 2] = k * (0.5 * a / W + 0.5 * c / H + e - x0) - 0.5
    m[1, 2] = k * (0.5 * b / W + 0.5 * d / H + f - y0) - 0.5
    warped = cv2.warpAffine(np.ascontiguousarray(R), m, (W, H), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                            borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))
    inside = cv2.warpAffine(np.full(R.shape[:2], 255, np.uint8), m, (W, H),
                            flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP, borderValue=0) > 0
    out = base.copy()
    out[inside] = warped[inside]
    return out, [f"배경 이미지가 {bg.angle_deg:.2f}도 회전되어 있어 다시 샘플링해 비교함"]


def page_spans(page) -> list[dict]:
    spans = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for line in b["lines"]:
            for s in line["spans"]:
                if not s["text"].strip():
                    continue
                spans.append(
                    {
                        "bbox": tuple(round(v, 3) for v in s["bbox"]),
                        "size": round(float(s["size"]), 3),
                        "color": int_color_to_hex(s["color"]),
                        "font": s["font"],
                        "chars": len(s["text"]),
                        "origin": tuple(round(v, 3) for v in s["origin"]),
                    }
                )
    return spans


_INLINE_RE = re.compile(
    rb"q\s+([-+\d.]+)\s+([-+\d.]+)\s+([-+\d.]+)\s+([-+\d.]+)\s+([-+\d.]+)\s+([-+\d.]+)\s+cm\s+"
    rb"(?:(?:[-+\d.]+\s+){1,4}(?:rg|RG|g|G|k|K|sc|SC|scn|SCN)\s+|/[^\s/]+\s+gs\s+)*"  # 색·그래픽 상태
    rb"BI\b.*?\bID\b.*?\bEI\s+Q",
    re.S,
)


def remove_inline_images(page, rects: list[Box]) -> list[Box]:
    """content stream의 `q <cm> BI … EI Q` 인라인 이미지 중 중심이 rects 안인 것을 지운다.

    지운 이미지의 bbox(pt) 목록을 돌려준다. 내용 스트림이 여러 개면 손대지 않는다.
    """
    import pymupdf

    xrefs = page.get_contents()
    if len(xrefs) != 1:
        return []
    doc = page.parent
    data = doc.xref_stream(xrefs[0])
    removed: list[Box] = []
    tm = page.transformation_matrix

    def repl(m: re.Match) -> bytes:
        a, b, c, d, e, f = (float(v) for v in m.groups())
        r = pymupdf.Rect(e, f, e + a, f + d) * tm
        r.normalize()
        cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
        if any(x0 <= cx <= x1 and y0 <= cy <= y1 for x0, y0, x1, y1 in rects) and r.width < 20 and r.height < 20:
            removed.append(tuple(r))
            return b""
        return m.group(0)

    new = _INLINE_RE.sub(repl, data)
    if removed:
        doc.update_stream(xrefs[0], new)
    return removed


def _dominant(spans: list[dict], key: str):
    tally: dict = {}
    for s in spans:
        tally[s[key]] = tally.get(s[key], 0) + s["chars"]
    return max(tally.items(), key=lambda kv: kv[1])[0] if tally else None


def _r(b, nd=3):
    return [round(float(v), nd) for v in b]


@dataclass
class PageErase:
    meta: dict
    before: np.ndarray  # 원래 모양 (이미지 격자)
    clean: CleanResult | None
    crop_px: tuple[int, int, int, int]
    region_px: dict  # 영역 이름 -> 작업 bbox (px, 정수)
    warnings: list[str]
    cleaned: np.ndarray | None = None  # 패치를 반영한 배경 이미지 (확인용, PDF에는 패치만 들어감)


def patch_rect_pt(box_px: tuple[int, int, int, int], bg: BackgroundImage) -> Box:
    """정수 픽셀 범위 -> pt 사각형 (격자에 정확히 맞음; 회전된 배경이면 감싸는 사각형)."""
    return bg.px_to_pt(tuple(float(v) for v in box_px))  # type: ignore[arg-type]


def patch_matrix_pdf(page, box_px: tuple[int, int, int, int], bg: BackgroundImage):
    """회전된 배경 위 패치의 PDF cm 행렬 (패치 단위 사각형 -> PDF 사용자 좌표).

    배경 이미지와 같은 행렬에서 원점·크기만 픽셀 범위로 옮긴다 -> 배경 픽셀 격자와 정확히 겹친다.
    """
    import pymupdf

    W, H = bg.size
    u0, v0, u1, v1 = box_px
    T = pymupdf.Matrix(*bg.transform)  # (s, t 위에서부터) -> 페이지(위가 0)
    sub = pymupdf.Matrix((u1 - u0) / W, 0, 0, (v1 - v0) / H, u0 / W, v0 / H)
    flip = pymupdf.Matrix(1, 0, 0, -1, 0, 1)  # PDF 이미지 공간은 t가 아래에서부터
    return flip * sub * T * ~page.transformation_matrix


def _set_image_cm(page, xref: int, mat) -> None:
    """insert_image가 쓴 `q … cm /이름 Do Q`의 cm을 mat으로 바꾼다."""
    doc = page.parent
    name = next(img[7] for img in page.get_images(full=True) if img[0] == xref)
    pat = re.compile(rb"((?:[-+\d.eE]+\s+){6})cm(\s*/" + re.escape(name.encode()) + rb"\s+Do)")
    new_cm = " ".join(f"{v:.6f}" for v in (mat.a, mat.b, mat.c, mat.d, mat.e, mat.f)).encode() + b" "
    for cx in page.get_contents():
        data = doc.xref_stream(cx)
        new, n = pat.subn(lambda m: new_cm + b"cm" + m.group(2), data)
        if n:
            if n != 1:
                raise RuntimeError(f"패치 이미지 {name}이(가) 내용 스트림에 {n}번 나옴")
            doc.update_stream(cx, new)
            return
    raise RuntimeError(f"패치 이미지 {name}의 cm을 찾지 못함")


def place_patch(page, pixels: np.ndarray, box_px: tuple[int, int, int, int], bg: BackgroundImage) -> dict:
    """정리한 픽셀 조각을 무손실(Flate) 이미지로 배경 위에 그린다."""
    import pymupdf

    h, w = pixels.shape[:2]
    if (box_px[2] - box_px[0], box_px[3] - box_px[1]) != (w, h):
        raise ValueError("패치 크기와 픽셀 범위가 다릅니다")
    pix = pymupdf.Pixmap(pymupdf.csRGB, w, h, np.ascontiguousarray(pixels).tobytes(), 0)
    rect = patch_rect_pt(box_px, bg)
    if bg.rotated:
        xref = page.insert_image(pymupdf.Rect(rect), pixmap=pix, overlay=True, keep_proportion=False)
        _set_image_cm(page, xref, patch_matrix_pdf(page, box_px, bg))
        kind, val = page.parent.xref_get_key(bg.xref, "ColorSpace")
        page.parent.xref_set_key(xref, "ColorSpace", val if kind in ("name", "xref") else "/DeviceRGB")
        return {"bbox_pt": _r(rect, 4), "bbox_px": list(box_px), "size_px": [w, h], "xref": xref,
                "rotated_deg": round(bg.angle_deg, 3)}
    # MuPDF는 축 정렬 이미지를 픽셀 경계 바깥쪽으로 맞춰(grid-fit) 그린다. 소수점 오차로
    # 모서리가 경계를 아주 조금 넘으면 바깥 1px 줄까지 덮으므로, 아주 조금(0.002pt ≈ 0.008px) 안쪽으로 넣는다.
    eps = PATCH_INSET_PT
    draw = pymupdf.Rect(rect[0] + eps, rect[1] + eps, rect[2] - eps, rect[3] - eps)
    xref = page.insert_image(draw, pixmap=pix, overlay=True, keep_proportion=False)
    # MuPDF는 RGB 픽스맵을 ICCBased(sRGB)로 넣는다. 원본 배경은 DeviceRGB라 Acrobat·인쇄의
    # 색 관리에서 경계에 미세한 색 차이가 날 수 있으므로 원본과 같은 DeviceRGB로 맞춘다.
    kind, val = page.parent.xref_get_key(bg.xref, "ColorSpace")
    page.parent.xref_set_key(xref, "ColorSpace", val if kind in ("name", "xref") else "/DeviceRGB")
    return {"bbox_pt": _r(rect, 4), "bbox_px": list(box_px), "size_px": [w, h], "xref": xref}


def _tag_labels_in(rect_pt: Box, protect_pt: list, bg: BackgroundImage, before: np.ndarray):
    """태그 영역(위아래 여유 포함)에서 라벨을 찾아 (LabelPx, px 상자, pt 상자)를 왼쪽부터 돌려준다."""
    size = (before.shape[1], before.shape[0])
    h = rect_pt[3] - rect_pt[1]
    search_pt = clip_horizontally(expand(rect_pt, 0, TAG_SEARCH_PAD * h), protect_pt)
    sx0, sy0, sx1, sy1 = px_box_outer(bg.pt_to_px(search_pt), size)
    h_px = h / bg.pt_per_px  # 회전된 배경에서도 감싸는 상자가 아니라 실제 높이
    out = []
    for lab in find_tag_labels(before[sy0:sy1, sx0:sx1], min_h=TAG_MIN_H * h_px, max_h=TAG_MAX_H * h_px):
        bpx = (lab.bbox[0] + sx0, lab.bbox[1] + sy0, lab.bbox[2] + sx0, lab.bbox[3] + sy0)
        out.append((lab, bpx, bg.px_to_pt(bpx)))
    return out


def detect_tag_labels(page, template: RegionTemplate, region: Region) -> list[dict]:
    """원본 쪽에서 태그 라벨의 색·위치를 잰다 (지우지 않음). [{"color": "#rrggbb", "bbox_pt": [...]}, ...]"""
    bg = find_background_image(page)
    raw = load_image_rgb(page.parent, bg.xref)
    before, _ = render_on_image_grid(page, bg, raw)
    prect: Box = tuple(page.rect)
    protect_pt = [ratio_to_pt(p.rect, prect) for p in template.protect]
    rect_pt = ratio_to_pt(region.rect, prect)
    return [{"color": rgb_to_hex(lab.color), "bbox_pt": _r(bpt)}
            for lab, _bpx, bpt in _tag_labels_in(rect_pt, protect_pt, bg, before)]


def uses_text_mask(reg: Region, has_text_layer: bool) -> bool:
    """글자 모양만 지우는 방식(app.erase_text)을 쓸 영역인가.
    텍스트 레이어가 없는 글자 영역(그림 카드) 또는 지우기 방식(erase.mode)을 정한 영역. 예전 설정
    (keep_ink_taller_than_pt)을 쓰는 영역과 텍스트 레이어가 있는 영역은 지금까지의 방식을 그대로 쓴다."""
    er = reg.erase or {}
    if reg.kind != "text":
        return False
    if er.get("mode"):
        return True
    return not has_text_layer and not er.get("keep_ink_taller_than_pt")


INLINE_ICON_SCALE_RANGE = (0.8, 3.0)  # 글 속 아이콘 크기(그 줄 글자 높이 대비)로 믿을 범위


def _find_icons(sub: np.ndarray, lb, src, reg) -> list:
    """원문 아이콘의 자리 [(이름, sub 안 px 상자, 아이콘만 있는 줄인가)]. 못 찾은 아이콘은 뺀다."""
    if not isinstance(src, str) or "[[" not in src:
        return []
    from app import icons

    rot = int(((reg.style if reg is not None else None) or {}).get("rotate", 0) or 0) % 360
    img = sub[lb[1]:lb[3], lb[0]:lb[2]]
    if rot == 180:
        img = np.ascontiguousarray(img[::-1, ::-1])
    out = []
    try:
        found = icons.icon_boxes(img, src)
    except Exception:  # noqa: BLE001 - 아이콘을 못 찾아도 글자 지우기는 한다
        return []
    for nm, (x0, y0, x1, y1), only in found:
        if rot == 180:
            x0, y0, x1, y1 = img.shape[1] - x1, img.shape[0] - y1, img.shape[1] - x0, img.shape[0] - y0
        out.append((nm, (x0 + lb[0], y0 + lb[1], x1 + lb[0], y1 + lb[1]), only))
    return out


def _inline_icons(found: list, lay: dict, tm) -> list:
    """글 줄 안의 아이콘 자리를 tm.mask에 더한다 (그 자리를 지운다).
    [(이름, sub 안 px 상자, 그 줄 글자 높이 대비 크기 | None)]. 아이콘만 있는 줄의 아이콘은 그대로 둔다."""
    H, W = tm.mask.shape
    out = []
    for nm, bx, only in found:
        if only:
            continue
        tm.mask[max(0, bx[1] - 2):min(H, bx[3] + 2), max(0, bx[0] - 2):min(W, bx[2] + 2)] = True
        cy = (bx[1] + bx[3]) / 2
        ln = min((ln for ln in lay.get("lines") or [] if ln["bbox"][1] - ln["glyph_h"] <= cy <= ln["bbox"][3] + ln["glyph_h"]),
                 key=lambda ln: abs((ln["bbox"][1] + ln["bbox"][3]) / 2 - cy), default=None)
        sc = None
        if ln is not None and ln.get("glyph_h"):
            v = (bx[3] - bx[1]) / ln["glyph_h"]
            if INLINE_ICON_SCALE_RANGE[0] <= v <= INLINE_ICON_SCALE_RANGE[1]:
                sc = round(v, 3)
        out.append((nm, bx, sc))
    return out


def erase_page(page, template: RegionTemplate, sample: dict | None = None, seed: int = 0,
               params: CleanParams | None = None, modes: dict | None = None,
               sources: dict | None = None) -> PageErase:
    """page(결과 문서 쪽의 페이지 사본)에서 영역 글자를 지운다. page는 바뀐다.
    modes: 이 카드만 다르게 지울 영역 {영역 이름: 방식} (영역 설정 erase.mode보다 우선).
    sources: 읽은 원문 {영역 이름: 글}. 그림 카드에서 지울 줄을 원문 줄에 맞춘다."""
    import pymupdf

    warnings: list[str] = []
    doc = page.parent
    prect: Box = tuple(page.rect)
    bg = find_background_image(page)
    raw = load_image_rgb(doc, bg.xref)
    size = (raw.shape[1], raw.shape[0])
    before, w = render_on_image_grid(page, bg, raw)
    warnings += w

    def to_px(b: Box) -> Box:
        return bg.pt_to_px(b)

    def to_pt(b: Box) -> Box:
        return bg.px_to_pt(b)

    protect_pt = [ratio_to_pt(p.rect, prect) for p in template.protect]
    spans = page_spans(page)
    small_images = [
        tuple(i["bbox"]) for i in page.get_image_info(xrefs=True)
        if not (i.get("xref", 0) == bg.xref) and area(tuple(i["bbox"])) < 400
    ]

    region_metas = []
    keep_above_px: dict[str, list] = {}  # 영역별 남길 덩어리(왕관 등) px 상자
    redact_boxes: list[Box] = []
    work_pt: dict[str, Box] = {}
    forced_pt: list[Box] = []
    span_forced_pt: list[Box] = []
    used_spans: set[int] = set()
    mask_regions: dict[str, str] = {}  # 글자 모양만 지울 영역 -> 방식

    for reg in template.regions:
        rect_pt = ratio_to_pt(reg.rect, prect)
        assigned, partial_warn = [], []
        for i, s in enumerate(spans):
            r = overlap_ratio(s["bbox"], rect_pt)
            if r <= 0:
                continue
            if r >= SPAN_ASSIGN_MIN:
                if i in used_spans:
                    continue
                used_spans.add(i)
                assigned.append(s)
                if r < SPAN_PARTIAL_WARN:
                    partial_warn.append(f"span {_r(s['bbox'], 1)} 이 영역에 {r:.0%}만 걸침 (전부 지움)")
            else:
                partial_warn.append(f"span {_r(s['bbox'], 1)} 이 영역에 {r:.0%}만 걸쳐 지우지 않음")
        boxes = [rect_pt] + [s["bbox"] for s in assigned]
        # 쪽에 텍스트 레이어가 조금이라도 있으면(ClearScan) 예전 방식: 빠진 제목도 그 방식으로 맞춰 왔다
        if (modes or {}).get(reg.name) or uses_text_mask(reg, bool(assigned) or bool(spans)):
            mask_regions[reg.name] = (modes or {}).get(reg.name) or (reg.erase or {}).get("mode") or "auto"

        labels_meta = []
        if reg.kind == "tag_pills":
            for lab, bpx, bpt in _tag_labels_in(rect_pt, protect_pt, bg, before):
                scale_pt = bg.pt_per_px
                tl = [s for s in assigned if overlap_ratio(s["bbox"], bpt) > 0.3 or overlap_ratio(bpt, s["bbox"]) > 0.3]
                labels_meta.append(
                    {
                        "bbox_pt": _r(bpt),
                        "bbox_px": _r(bpx, 1),
                        "radius_pt": round(lab.radius * scale_pt, 3),
                        "radius_px": round(lab.radius, 2),
                        "color": rgb_to_hex(lab.color),
                        "color_text_layer": tl[0]["color"] if tl else None,
                        "in_text_layer": bool(tl),
                        "coverage": round(lab.coverage, 3),
                    }
                )
                boxes.append(bpt)
                forced_pt.append(expand(bpt, 0.5))
            expected = None
            if sample and reg.name in sample:
                src = sample[reg.name].get("source")
                expected = len(src) if isinstance(src, list) else None
            if expected is not None and expected != len(labels_meta):
                warnings.append(f"[{reg.name}] 찾은 라벨 {len(labels_meta)}개, samples.json 기대 {expected}개")

        inline_here = [b for b in small_images if overlap_ratio(b, rect_pt) > 0.5]
        boxes += inline_here
        image_text_box = None
        kt = (reg.erase or {}).get("keep_ink_taller_than_pt")
        if not assigned and reg.kind != "tag_pills" and kt:
            rpx = px_box_outer(to_px(rect_pt), size)
            ka = (reg.erase or {}).get("keep_ink_above_text")
            if ka:  # 180도 돌린 영역은 '글자 위'가 페이지에서 아래쪽
                ka = "down" if int((reg.style or {}).get("rotate", 0)) % 360 == 180 else "up"
            ib, kept_boxes = text_ink_box(raw, rpx, kt / bg.pt_per_px, keep_above=ka)
            if kept_boxes:
                keep_above_px[reg.name] = kept_boxes
            if ib is not None:
                pad = IMAGE_TEXT_PAD_PT
                image_text_box = expand(to_pt(tuple(float(v) for v in ib)), pad)
                image_text_box = intersect(image_text_box, rect_pt)
                boxes = [image_text_box]
        wpt = clip_horizontally(union(boxes), protect_pt)
        work_pt[reg.name] = wpt
        for s in assigned:
            redact_boxes.append(s["bbox"])
            span_forced_pt.append(expand(s["bbox"], 0.4))
        forced_pt += [expand(b, 0.5) for b in inline_here]

        region_metas.append(
            {
                "name": reg.name,
                "kind": reg.kind,
                "rect_ratio": reg.rect.to_dict(),
                "rect_pt": _r(rect_pt),
                "work_rect_pt": _r(wpt),
                "style_defaults": reg.style,
                "text_layer": {
                    "present": bool(assigned),
                    "spans": [
                        {"bbox_pt": _r(s["bbox"]), "size_pt": s["size"], "color": s["color"], "font": s["font"],
                         "origin_pt": _r(s["origin"]), "chars": s["chars"]}
                        for s in assigned
                    ],
                    "size_pt": _dominant(assigned, "size"),
                    "color": _dominant(assigned, "color"),
                    "union_bbox_pt": _r(union([s["bbox"] for s in assigned])) if assigned else None,
                },
                "tag_labels": labels_meta if reg.kind == "tag_pills" else None,
                "inline_images_pt": [_r(b) for b in inline_here],
                "image_text_box_pt": _r(image_text_box) if image_text_box else None,
                "warnings": partial_warn,
            }
        )

    # ---- 2. 레드랙션 (텍스트만)
    for b in redact_boxes:
        page.add_redact_annot(pymupdf.Rect(b), fill=False, cross_out=False)
    if redact_boxes:
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )
    removed_inline = remove_inline_images(page, [m_["rect_pt"] for m_ in region_metas])
    left_spans = [s for s in page_spans(page) if any(overlap_ratio(s["bbox"], m_["rect_pt"]) > 0 for m_ in region_metas)]
    if left_spans:
        warnings.append(f"레드랙션 뒤에도 영역에 걸친 글자 {len(left_spans)}개: {[_r(s['bbox'], 1) for s in left_spans]}")
    bg_after = find_background_image(page)
    if bg_after.xref != bg.xref:
        raise RuntimeError("레드랙션이 배경 이미지를 바꿨습니다")

    # ---- 3. 이미지 정리
    work_px = {k: px_box_outer(to_px(v), size) for k, v in work_pt.items()}
    crop = px_box_outer(expand(union([tuple(map(float, b)) for b in work_px.values()]), 40), size)
    # 자르는 시작점을 8px 격자에 맞춘다: 질감을 JPEG 8×8 블록 결 그대로 빌려 오기 위해
    crop = (crop[0] // 8 * 8, crop[1] // 8 * 8, crop[2], crop[3])
    cx0, cy0, cx1, cy1 = crop
    ch, cw = cy1 - cy0, cx1 - cx0

    def local(b: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        return (b[0] - cx0, b[1] - cy0, b[2] - cx0, b[3] - cy0)

    work_m = np.zeros((ch, cw), bool)
    for name, b in work_px.items():
        if name not in mask_regions:  # 글자 모양만 지울 영역은 아래에서 따로
            fill_box(work_m, local(b))
    # 무조건 지울 픽셀: 텍스트 레이어가 실제로 칠한 픽셀(렌더 - 이미지 차이; 글자 모양 그대로)
    # + 태그 라벨 bbox + 인라인 이미지. span bbox 전체를 칠하지 않는 이유: bbox 안의 깨끗한
    # 종이 픽셀이 배경 추정에 필요하다.
    glyph_m = np.abs(before[cy0:cy1, cx0:cx1].astype(np.int16) - raw[cy0:cy1, cx0:cx1].astype(np.int16)).max(axis=2) > 10
    span_box_m = np.zeros((ch, cw), bool)
    for b in span_forced_pt:
        fill_box(span_box_m, local(px_box_outer(to_px(b), size)))
    forced_m = glyph_m & span_box_m
    for b in forced_pt:
        fill_box(forced_m, local(px_box_outer(to_px(b), size)))
    span_m = np.zeros((ch, cw), bool)
    for b in span_forced_pt:
        fill_box(span_m, local(px_box_outer(to_px(b), size)))
    protect_m = np.zeros((ch, cw), bool)
    for b in protect_pt:
        fill_box(protect_m, local(px_box_outer(to_px(b), size)))
    # 영역 안의 큰 잉크 덩어리(+2 같은 큰 숫자, 영역에 걸친 그림)는 글자가 아니므로 지키기
    keep_meta = {}
    for reg in template.regions:
        kt = (reg.erase or {}).get("keep_ink_taller_than_pt")
        if not kt:
            continue
        kept = tall_ink_mask(raw[cy0:cy1, cx0:cx1], local(work_px[reg.name]), kt / bg.pt_per_px)
        protect_m |= kept
        keep_meta[reg.name] = int(kept.sum())
    for name, bxs in keep_above_px.items():  # 글자 줄 위의 작은 그림(왕관): 테두리까지 넉넉히 지킨다
        for b in bxs:
            fill_box(protect_m, local((b[0] - 3, b[1] - 3, b[2] + 3, b[3] + 3)))
    # 이미지에 박힌 글자 영역(텍스트 레이어 없음): 작업 범위 밖의 그림·색 번짐은 배경 추정에 쓰지 않는다.
    # 영역 밖이라 바뀌지도 않는다(protect). 쓰면 줄 방향 배경 평균에 그림 색이 끌려와 얼룩이 남는다.
    if any(m_.get("image_text_box_pt") for m_ in region_metas):
        protect_m |= non_paper_mask(raw[cy0:cy1, cx0:cx1]) & ~work_m

    if work_m.any():
        res = clean_area(raw[cy0:cy1, cx0:cx1], work_m, forced_m, protect_m, params, seed=seed)
    else:
        z = np.zeros((ch, cw), bool)
        res = CleanResult(raw[cy0:cy1, cx0:cx1].copy(), z, z.copy(), z.copy(), z.copy(), 0.0, 0.0)
    new_img = raw.copy()
    new_img[cy0:cy1, cx0:cx1] = res.image

    # ---- 3b. 글자 모양만 지우기 (그림 카드): 글자 픽셀만 찾아 그 자리만 채운다 (app.erase_text)
    from app import erase_text

    text_mask_meta: dict[str, dict] = {}
    layout_meta: dict[str, dict] = {}
    regs_by_name = {r.name: r for r in template.regions}
    for name, mode in mask_regions.items():
        b = work_px[name]
        mg = 24
        ox0, oy0 = max(0, b[0] - mg), max(0, b[1] - mg)
        ox1, oy1 = min(size[0], b[2] + mg), min(size[1], b[3] + mg)
        sub = new_img[oy0:oy1, ox0:ox1].copy()  # 채우기 전 원문 픽셀 (줄 모양을 잴 때도 쓴다)
        lb = (b[0] - ox0, b[1] - oy0, b[2] - ox0, b[3] - oy0)
        lb_region = lb
        core = erase_text.label_panel(sub, lb)  # 영역이 글자 상자보다 크면 상자 안에서만 (위쪽 그림·띠 밖 테두리 빼기)
        if core != lb_region:
            # 글자가 상자 끝에 닿아 ‘가장자리 조각’으로 빠지지 않게 몇 px 넉넉히 (넓힌 곳은 상자색으로 칠한 사본에서 찾는다)
            m_ = max(3, int(0.12 * (core[3] - core[1])))
            lb = (max(lb_region[0], core[0] - m_), max(lb_region[1], core[1] - m_),
                  min(lb_region[2], core[2] + m_), min(lb_region[3], core[3] + m_))
            area = lambda b_: max(1, b_[2] - b_[0]) * max(1, b_[3] - b_[1])  # noqa: E731
            if area(core) >= 0.97 * area(lb_region):  # 거의 영역 그대로: 좁히지 않는다
                lb, core = lb_region, lb_region
        else:
            lb = lb_region
        prot = protect_m[oy0 - cy0:oy1 - cy0, ox0 - cx0:ox1 - cx0]
        # 상자로 좁혔으면 상자 밖을 상자 색으로 칠한 사본에서 글자를 찾는다 (지우기는 원본 sub에)
        look = erase_text.paint_outside(sub, core) if lb != lb_region else sub
        tm = erase_text.text_mask(look, lb, prot, size_box=lb_region)
        if lb != lb_region:
            # 지울 픽셀은 상자 몸통 안으로만: 밖(띠 테두리·그림)까지 번지면 채울 때 그 색이 끌려와 얼룩이 생긴다
            inside = np.zeros(tm.mask.shape, bool)
            inside[core[1]:core[3], core[0]:core[2]] = True
            tm.mask &= inside
            tm.core &= inside
        lay = erase_text.text_layout(look, tm, lb)
        if lb != lb_region:  # 좁힌 상자: 상자 위·아래 끝에 붙은 줄(띠 안의 그림 조각)을 먼저 원문에 없는 줄로 본다
            lay["box_y"] = [core[1], core[3]]
        src = (sources or {}).get(name)
        found = _find_icons(sub, lb, src, regs_by_name.get(name))
        if isinstance(src, str) and src.strip():
            rows = [(b_[1], b_[3]) for _n, b_, only in found if only]
            lay = erase_text.match_source_lines(tm, lay, src, icon_rows=rows or None)
        # 글 줄 안의 아이콘(문장 속 책·숫자 타일 등)도 지운다: 한글 넣기가 그 자리에 아이콘을 다시 그린다
        icons_found = _inline_icons(found, lay, tm)
        filled, used, changed = erase_text.fill_text(sub, tm, mode, lb)
        new_img[oy0:oy1, ox0:ox1] = filled
        if changed.any():  # 영역 가장자리에 걸친 글자를 상자 밖까지 지웠으면 패치도 그만큼 넓힌다
            ys_, xs_ = np.nonzero(changed)
            work_px[name] = (min(b[0], int(xs_.min()) + ox0), min(b[1], int(ys_.min()) + oy0),
                             max(b[2], int(xs_.max()) + 1 + ox0), max(b[3], int(ys_.max()) + 1 + oy0))
        res.mask[oy0 - cy0:oy1 - cy0, ox0 - cx0:ox1 - cx0] |= changed
        res.image[oy0 - cy0:oy1 - cy0, ox0 - cx0:ox1 - cx0] = filled
        wa = max(1, (b[3] - b[1]) * (b[2] - b[0]))
        text_mask_meta[name] = {
            "mode": mode, "used": used, "background": erase_text.background_kind(sub, tm),
            "mask_ratio": round(float(changed.sum()) / wa, 4), "texture": round(tm.texture, 2),
            "needs_choice": mode == "auto" and tm.busy, "notes": tm.notes,
            # 상자 안에서 글자를 거의 못 찾음: 영역 위치가 카드와 안 맞을 수 있다 (예: 거꾸로 인쇄된 카드)
            "few_text": bool(float(tm.core[lb[1]:lb[3], lb[0]:lb[2]].mean()) < 0.01),
        }
        if lb != lb_region:
            text_mask_meta[name]["notes"] = list(tm.notes) + ["영역 안의 글자 상자에서만 찾음 (위쪽 그림·테두리 제외)"]
        # 원문 줄 모양 (한글 넣기가 줄 자리·글자색·테두리·굵기·정렬·구분선을 따른다). 좌표는 pt
        ppx = bg.pt_per_px

        def _pt(bx, _ox=ox0, _oy=oy0):
            q = to_pt((bx[0] + _ox, bx[1] + _oy, bx[2] + _ox, bx[3] + _oy))
            return [round(v, 3) for v in q]

        layout_meta[name] = {
            "lines": [{**{k: v for k, v in ln.items() if k not in ("bbox", "glyph_h", "stroke_px", "accents", "cy")},
                       "bbox_pt": _pt(ln["bbox"]), "glyph_h_pt": round(ln["glyph_h"] * ppx, 3),
                       "cy_pt": _pt((ln["bbox"][0], ln["cy"], ln["bbox"][2], ln["cy"]))[1],
                       "stroke_pt": round(ln["stroke_px"] * ppx, 3),
                       "accents": [{"color": a_["color"],
                                    "x_pt": [[_pt((u, ln["bbox"][1], v, ln["bbox"][3]))[0],
                                              _pt((u, ln["bbox"][1], v, ln["bbox"][3]))[2]] for u, v in a_["x"]]}
                                   for a_ in ln["accents"]]}
                      for ln in lay["lines"]],
            "dividers_pt": [_pt(d) for d in lay["dividers"]],
            # 영역을 글자 상자로 좁혔으면 그 상자 (한글 넣기가 영역 대신 이 안에 넣는다)
            **({"panel_pt": _pt(lb)} if lb != lb_region else {}),
            # 지운 글 속 아이콘: 이름, 자리, 그 줄 글자 높이 대비 크기 (한글 넣기가 원본 크기로 다시 그린다)
            "icons": [{"name": n_, "bbox_pt": _pt(bx), "scale": sc} for n_, bx, sc in icons_found],
        }

    # ---- 4. 패치 덮기: 원본 배경 이미지(JPEG 스트림)는 바이트 그대로 두고,
    # 영역(work_rect) 크기의 무손실 패치를 배경 이미지 픽셀 격자에 정확히 맞춰 위에 그린다.
    # 마스크는 작업 영역보다 dilate_px까지 넓어질 수 있으므로 그만큼(+2) 넓혀 자른다.
    p_ = params or CleanParams()
    patches = []
    for m_ in region_metas:
        x0, y0, x1, y1 = work_px[m_["name"]]
        pad = p_.dilate_px + 2
        box = (max(0, x0 - pad), max(0, y0 - pad), min(size[0], x1 + pad), min(size[1], y1 + pad))
        sub_new = new_img[box[1] : box[3], box[0] : box[2]]
        if np.array_equal(sub_new, raw[box[1] : box[3], box[0] : box[2]]):
            continue
        patch = place_patch(page, sub_new, box, bg)
        patch["region"] = m_["name"]
        patches.append(patch)

    # ---- 5. 라벨 다시 그리기
    for m_ in region_metas:
        for lab in m_["tag_labels"] or []:
            r = pymupdf.Rect(lab["bbox_pt"])
            rad = min(0.5, lab["radius_pt"] / max(1e-6, min(r.width, r.height)))
            page.draw_rect(r, color=None, fill=hex_to_rgb01(lab["color"]), width=0,
                           radius=rad if rad > 0.01 else None)

    # ---- 영역별 통계
    lum_before = luminance(raw[cy0:cy1, cx0:cx1])
    lum_after = luminance(res.image)
    changed = np.abs(lum_after - lum_before) > 25
    evidence = cv2.dilate((res.strong | forced_m).astype(np.uint8), _disk(4)).astype(bool)
    for m_ in region_metas:
        b = local(work_px[m_["name"]])
        sl = (slice(max(0, b[1]), b[3]), slice(max(0, b[0]), b[2]))
        wa = max(1, (b[3] - b[1]) * (b[2] - b[0]))
        flat_n = int(res.flat_mask[sl].sum())
        inp_n = int(res.inpaint_mask[sl].sum())
        bg_change = float((changed[sl] & ~evidence[sl]).sum()) / wa
        strong_outside_spans = float((res.strong[sl] & ~span_m[sl]).sum()) / wa
        img_text = strong_outside_spans > 0.005
        missing = (not m_["text_layer"]["present"] and img_text) or any(
            not l_["in_text_layer"] for l_ in (m_["tag_labels"] or [])
        )
        if flat_n and inp_n:
            method = "mixed"
        elif inp_n:
            method = "inpaint"
        elif flat_n:
            method = "flat_gradient"
        else:
            method = "none"
        flags = []
        if m_["name"] in text_mask_meta:
            bg_change = 0.0  # 글자 픽셀만 바꿨다 (바뀐 픽셀 = 지울 픽셀)
        if bg_change > 0.02:
            flags.append("배경 변화 큼")
        m_["work_rect_px"] = list(work_px[m_["name"]])
        if m_["name"] in keep_meta:
            m_["kept_large_ink_px"] = keep_meta[m_["name"]]
        if m_["name"] in keep_above_px:
            m_["kept_above_text_boxes_px"] = [list(map(int, b)) for b in keep_above_px[m_["name"]]]
        m_["missing_from_text_layer"] = bool(missing)
        m_["image_text_detected"] = bool(img_text)
        if m_["name"] in text_mask_meta:
            tmm = text_mask_meta[m_["name"]]
            method = f"text_mask:{tmm['used']}"
            m_["text_layout"] = layout_meta.get(m_["name"])
            if tmm["needs_choice"]:
                flags.append("그림 배경: 지우기 방식 확인")
            if tmm.get("few_text"):
                flags.append("글자를 거의 못 찾음: 영역 위치 확인")
        m_["erase"] = {
            "text_mask": text_mask_meta.get(m_["name"]),
            "redacted_spans": len(m_["text_layer"]["spans"]),
            "removed_inline_images": sum(
                1 for rb in removed_inline if overlap_ratio(rb, tuple(m_["rect_pt"])) > 0.5
            ),
            "method": method,
            "flat_px": flat_n,
            "inpaint_px": inp_n,
            "mask_ratio": round(float(res.mask[sl].sum()) / wa, 4),
            "background_change_ratio": round(bg_change, 4),
            "strong_px_outside_text_layer_ratio": round(strong_outside_spans, 4),
            "flags": flags,
        }

    meta = {
        "page_rect_pt": _r(prect),
        "mediabox": _r(page.mediabox),
        "cropbox": _r(page.cropbox),
        "background_image": {
            "xref": bg.xref,
            "bbox_pt": _r(bg.bbox),
            "size_px": list(size),
            "dpi": [round(v, 2) for v in bg.dpi],
            "stream_untouched": True,  # 원본 스트림을 바이트 그대로 둔다 (CLI가 해시로 확인)
        },
        "patches": patches,
        "clean": {"threshold": round(res.threshold, 2), "paper_noise_sigma": round(res.noise_sigma, 2),
                  "crop_px": list(crop)},
        "removed_inline_images_pt": [_r(b) for b in removed_inline],
        "regions": region_metas,
        "warnings": warnings,
    }
    return PageErase(meta, before, res, crop, work_px, warnings, new_img)
