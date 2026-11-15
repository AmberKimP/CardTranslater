"""태그 라벨 찾기: 라벨 바탕 픽셀·모서리 둥글기·라벨 상자 (app.erase에서 나눔)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np


LABEL_MAX_ASPECT = 5.5  # 라벨 폭 / 높이 상한 (실측 ≈4.3)


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
