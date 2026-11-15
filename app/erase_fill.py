"""배경 추정과 정리: 지운 자리 채우기·무늬 옮기기·잉크 상자 (app.erase에서 나눔)."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


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
