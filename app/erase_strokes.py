"""그림 위 글자: 색 대신 모양(가는 획)으로 글자를 찾아 지우기 (app.erase에서 나눔)."""

from __future__ import annotations

import cv2
import numpy as np

from app.erase_fill import _disk, luminance, normalized_blur


# =====================================================================
# 그림 배경 위 글자 (예외 처리)
# =====================================================================
# 글자가 그림(반투명 패널 아래로 비치는 일러스트 등) 위에 있으면 '상자 밖에 드문 색 = 글자'로 고르는 방식이
# 빗나간다: 글자색(어두운 회색)이 그림에도 많아 글자를 못 찾거나, 그림 조각·아이콘을 글자로 보고 뭉갠다.
# 이때는 색이 아니라 모양으로 찾는다: 주변보다 뚜렷이 어두운(또는 밝은) 가는 획이 한 줄로 늘어선 것이 글자다.

PICTURE_BG_WARNING = "배경이 그림이라 지운 자리가 티 날 수 있음"
PICTURE_STROKE_MIN = 75.0  # 가는 획이 둘레보다 이만큼(밝기) 어둡거나 밝아야 글자 획 (패널 아래 그림 선은 40~70, 글자는 130~180)
PICTURE_STROKE_LOW = 45.0  # 찾은 글자 줄의 띠 안에서는 이만큼만 달라도 글자 획 (작고 흐린 글자의 끊긴 획)
PICTURE_EDGE_INSIDE = 0.4  # 상자 가장자리에 걸친 덩어리: 이만큼은 상자 안에 있어야 글자 후보
PICTURE_KERNEL = (0.14, 9, 25)  # 획을 가르는 창 크기: 영역 높이 대비, 최소·최대 px
PICTURE_COMP_MAX = 0.45  # 영역 높이의 이만큼보다 큰 덩어리는 그림·테두리
PICTURE_GLYPH_MAX = 2.2  # 글자 높이의 이 배수보다 높은 덩어리는 아이콘·그림 (그 안의 조각도 함께 뺀다)
PICTURE_ICON_MAX = 6.0  # 글자 높이의 이 배수보다 넓은 덩어리도 그림. 이 크기까지의 큰 덩어리는 아이콘으로 보고 그 안의 조각을 뺀다
PICTURE_RUN_MIN_W = 1.6 # 줄 조각의 폭이 높이의 이 배수 이상이어야 글자 줄 (두 글자 낱말까지)
PICTURE_MIN_INK = 3.0  # 찾은 글자 줄의 폭 합계가 글자 크기의 이 배수는 돼야 믿는다
PICTURE_COVER_MIN = 0.6  # 기존 방식이 이 획의 이만큼도 못 덮었으면 (글자를 놓쳤으면) 획 방식으로 바꾼다
PICTURE_STRAY_MAX = 0.25 # 기존 방식이 획 둘레 밖을 이만큼(획 마스크 넓이 대비) 넘게 지우면 (그림을 뭉개면) 바꾼다
PICTURE_UNNARROW = 1.25  # 영역 전체에서 찾은 획이 좁힌 글자 상자 안의 이 배수를 넘으면 좁히지 않는다
PICTURE_FULL_RECHECK = 0.9  # 좁힌 글자 상자가 영역의 이만큼 이상이면 영역 전체로 다시 찾지 않는다 (거의 같은 결과)
PICTURE_GROW_PX = 3 # 획 둘레 번짐·흰 테(글로우)까지 지울 폭
PICTURE_SIGMA = 4.0  # 채울 때 둘레 픽셀을 평균하는 폭(px)


def _hat_strokes(lum: np.ndarray, k: int, polarity: int) -> np.ndarray:
    """둘레보다 어두운(polarity=-1) 또는 밝은(+1) 가는 획의 세기 (창 k보다 넓은 면은 0에 가깝다)."""
    op = cv2.MORPH_BLACKHAT if polarity < 0 else cv2.MORPH_TOPHAT
    return cv2.morphologyEx(lum, op, _disk(max(1, k // 2)))


def _stroke_lines(hat: np.ndarray, box: tuple[int, int, int, int]) -> tuple[np.ndarray, float, float, np.ndarray]:
    """획 세기(hat)에서 글자 줄을 이루는 덩어리만 고른다. (글자 마스크, 글자 높이 px, 줄 폭 합계 px, 지워도 되는 곳).

    - 상자 밖으로 이어지는 덩어리(그림 선·테두리)와 너무 큰 덩어리(아이콘 외곽선)는 뺀다. 큰 덩어리 안의 조각도 뺀다.
    - 남은 덩어리를 가로로 이어 붙여 '줄 조각'을 만들고, 글자 높이쯤이며 가로로 긴 조각만 글자로 본다.
    - 줄을 찾은 뒤 그 줄 띠 안에서만 더 약한 획(작고 흐린 글자의 끊긴 획)을 더한다.
    - 영역을 빠듯하게 그려 상자 가장자리에 걸친 글자 크기 덩어리는 글자로 받고, 그만큼 상자 밖도 지울 수 있게 한다.
    """
    x0, y0, x1, y1 = box
    H, W = hat.shape
    bh = max(1, y1 - y0)
    empty = np.zeros((H, W), bool)
    inbox = empty.copy()
    inbox[max(0, y0):y1, max(0, x0):x1] = True
    strokes = hat > PICTURE_STROKE_MIN
    n, lab, st, _ = cv2.connectedComponentsWithStats(strokes.astype(np.uint8), connectivity=8)
    inside_n = np.bincount(lab[inbox], minlength=n)
    ok, big, edge = [], [], set()
    for i in range(1, n):
        x, y, w, h, a = (int(v) for v in st[i])
        if inside_n[i] < PICTURE_EDGE_INSIDE * a or max(w, h) > PICTURE_COMP_MAX * bh or a < 3:
            continue
        if inside_n[i] < 0.97 * a:
            edge.add(i)
        ok.append(i)
    # 글자 크기는 덩어리 높이로 잰다: 작은 글자는 옆 글자와 붙어 한 덩어리가 되므로 폭은 믿을 수 없다
    sizes = [int(st[i, 3]) for i in ok if st[i, 4] >= 8 and i not in edge]
    if len(sizes) < 3:
        return empty, 0.0, 0.0, inbox
    glyph = float(np.percentile(sizes, 75))
    for i in list(ok):
        x, y, w, h = (int(v) for v in st[i, :4])
        if i in edge:  # 가장자리에 걸친 것은 글자 하나 크기일 때만
            if h > 1.5 * glyph or w > 2.0 * glyph:
                ok.remove(i)
            continue
        if h > PICTURE_GLYPH_MAX * glyph or w > PICTURE_ICON_MAX * glyph:
            if max(w, h) <= PICTURE_ICON_MAX * glyph:  # 아이콘 외곽선 크기만 (길게 뻗은 그림 선의 상자는 글자까지 덮는다)
                big.append((x, y, x + w, y + h))
            ok.remove(i)

    # 아이콘은 외곽선이 끊겨 글자 크기 조각으로 보이기도 한다: 더 약한 기준으로 이으면 한 덩어리(글자보다 큼)가 된다.
    # 글자는 둘레의 흰 테 때문에 약한 기준으로도 서로 붙지 않는다
    wn, _wlab, wst, _ = cv2.connectedComponentsWithStats((hat > PICTURE_STROKE_LOW).astype(np.uint8), connectivity=8)
    for i in range(1, wn):
        x, y, w, h = (int(v) for v in wst[i, :4])
        if h > PICTURE_GLYPH_MAX * glyph and max(w, h) <= PICTURE_ICON_MAX * glyph:
            big.append((x, y, x + w, y + h))

    def in_big(i) -> bool:
        cx, cy = st[i, 0] + st[i, 2] / 2, st[i, 1] + st[i, 3] / 2
        return any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in big)

    ok = [i for i in ok if not in_big(i)]
    if not ok:
        return empty, glyph, 0.0, inbox
    cand = np.isin(lab, ok)
    gx = int(max(3, 1.2 * glyph)) | 1
    runs = cv2.dilate(cand.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_RECT, (gx, 1)))
    rn, rlab, rst, _ = cv2.connectedComponentsWithStats(runs, connectivity=8)
    keep = empty.copy()
    bands = empty.copy()
    allowed = inbox.copy()
    total_w = 0.0
    g2 = int(round(2 * glyph))
    for r in range(1, rn):
        w, h = int(rst[r, 2]) - (gx - 1), int(rst[r, 3])
        if not (0.5 * glyph <= h <= PICTURE_GLYPH_MAX * glyph and w >= PICTURE_RUN_MIN_W * h):
            continue
        sel = (rlab == r) & cand
        members = np.unique(lab[sel])
        if len(members) < 2:  # 긴 선 하나(구분선·그림 선)는 글자 줄이 아니다
            continue
        if all(int(m) in edge for m in members):  # 가장자리에 걸친 조각뿐인 줄은 그림
            continue
        keep |= sel
        total_w += w
        rx, ry = int(rst[r, 0]) + gx // 2, int(rst[r, 1])
        bands[max(0, ry - 2):ry + h + 2, max(0, rx - g2):rx + w + g2] = True
        for m in members:
            if int(m) in edge:
                ex, ey, ew, eh = (int(v) for v in st[m, :4])
                p_ = PICTURE_GROW_PX
                allowed[max(0, ey - p_):ey + eh + p_, max(0, ex - p_):ex + ew + p_] = True
    if not keep.any():
        return empty, glyph, 0.0, inbox
    # 줄 띠 안의 약한 획: 덩어리가 띠 안에 온전히 들어 있을 때만 (띠 밖으로 이어지면 그림 선)
    low = (hat > PICTURE_STROKE_LOW) & ~strokes
    ln, llab, lst, _ = cv2.connectedComponentsWithStats(low.astype(np.uint8), connectivity=8)
    in_band = np.bincount(llab[bands & allowed], minlength=ln)
    add = [i for i in range(1, ln) if lst[i, 4] >= 2 and in_band[i] == lst[i, 4] and lst[i, 3] <= PICTURE_GLYPH_MAX * glyph]
    if add:
        keep |= np.isin(llab, add)
    return keep, glyph, total_w, allowed


def picture_text_mask(img: np.ndarray, box: tuple[int, int, int, int], protect: np.ndarray | None = None,
                      texture: float = 0.0):
    """그림 배경 위 글자를 획 모양으로 찾는다. 믿을 만한 글자 줄이 없으면 None.

    img: H×W×3 uint8 (상자 둘레에 여유가 있는 자른 그림), box: (x0, y0, x1, y1). app.erase_text.TextMask를 돌려준다
    (mask = 획 + 둘레 PICTURE_GROW_PX, core = 획). 어두운 글자와 밝은 글자 중 줄이 더 길게 잡히는 쪽을 고른다.
    texture: 기록할 배경 결 값 (기존 방식이 잰 값을 그대로 넘긴다: 여기서 다시 재지 않는다).
    """
    from app import erase_text

    x0, y0, x1, y1 = box
    H, W = img.shape[:2]
    bh = max(1, y1 - y0)
    lum = luminance(img)
    frac, kmin, kmax = PICTURE_KERNEL
    k = int(min(kmax, max(kmin, frac * bh))) | 1
    best = None
    for pol in (-1, 1):
        core, glyph, total_w, allowed = _stroke_lines(_hat_strokes(lum, k, pol), box)
        if glyph and total_w >= PICTURE_MIN_INK * glyph and (best is None or total_w > best[2]):
            best = (core, glyph, total_w, allowed)
    if best is None:
        return None
    core, glyph, _w, allowed = best
    mask = cv2.dilate(core.astype(np.uint8), _disk(PICTURE_GROW_PX)).astype(bool) & allowed
    if protect is not None:
        mask &= ~protect
    return erase_text.TextMask(mask, core & allowed, True, texture, glyph,
                               ["그림 배경: 글자를 획 모양으로 찾음"], [])


def _box_area(b) -> int:
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def prefer_picture_mask(tm, alt) -> bool:
    """그림 배경에서 기존 방식(tm) 대신 획 방식(alt)을 쓸 것인가.
    기존 방식이 글자 획을 많이 놓쳤거나(남은 글자), 획 둘레 밖을 넓게 지우려 할 때(그림 뭉갬)만 바꾼다."""
    if alt is None or not alt.core.any():
        return False
    covered = float((tm.mask & alt.core).sum()) / float(alt.core.sum())
    near = cv2.dilate(alt.core.astype(np.uint8), _disk(PICTURE_GROW_PX + 3)).astype(bool)
    stray = float((tm.mask & ~near).sum()) / max(1.0, float(alt.mask.sum()))
    return covered < PICTURE_COVER_MIN or stray > PICTURE_STRAY_MAX


PICTURE_HALO_STEP = 2  # 글자 둘레 그림자를 재는 띠 폭(px)
PICTURE_HALO_MAX = 10  # 그림자까지 넓힐 최대 폭(px)
PICTURE_HALO_DIFF = 12.0  # 이웃한 두 띠의 밝기(중앙값) 차이가 이보다 크면 아직 그림자 안


def grow_halo(img: np.ndarray, mask: np.ndarray, protect: np.ndarray | None = None) -> np.ndarray:
    """글자 둘레의 흐린 그림자·흰 테까지 마스크를 넓힌다 (낱말 덩어리마다).

    아이콘 이름표처럼 글자에 넓고 흐린 그림자가 있으면, 획만 지우고 채울 때 남은 그림자 색이 끌려와 검은 얼룩이 된다.
    마스크 바깥으로 PICTURE_HALO_STEP씩 띠를 넓혀 가며 바로 다음 띠와 밝기가 비슷해지는 곳(그림 배경)에서 멈춘다.
    그림자가 없는 글자(반투명 패널 위 본문)는 첫 띠에서 멈추므로 그대로다.
    """
    if not mask.any():
        return mask
    lum = luminance(img).astype(np.float32)
    m8 = mask.astype(np.uint8)
    d = cv2.distanceTransform(1 - m8, cv2.DIST_L2, 3)
    n, lab = cv2.connectedComponents(cv2.dilate(m8, np.ones((7, 7), np.uint8)))
    out = mask.copy()
    step, max_px = PICTURE_HALO_STEP, PICTURE_HALO_MAX
    for i in range(1, n):
        zone = cv2.dilate((lab == i).astype(np.uint8), _disk(max_px + step)).astype(bool)
        r = 0
        while r < max_px:
            a = zone & (d > r) & (d <= r + step)
            b = zone & (d > r + step) & (d <= r + 2 * step)
            if a.sum() < 10 or b.sum() < 10:
                break
            if abs(float(np.median(lum[a])) - float(np.median(lum[b]))) <= PICTURE_HALO_DIFF:
                break
            r += step
        if r:
            out |= zone & (d <= r)
    if protect is not None:
        out &= ~protect | mask
    return out


def fill_picture(img: np.ndarray, mask: np.ndarray, seed: int = 0) -> np.ndarray:
    """그림 배경의 글자 자리(mask)를 채운다. mask 밖 픽셀은 그대로 둔다.

    TELEA 인페인팅만 쓰면 빽빽한 글자 줄에서 줄무늬 얼룩이 생긴다. 그래서 인페인팅 결과(그림의 선·색 경계를 이어 줌)를
    안쪽에서 한 번 다듬고(줄무늬 제거), 둘레의 고운 결(인쇄 망점·JPEG 잡음)만큼 잡음을 얹어 매끈한 얼룩으로 보이지 않게 한다.
    """
    if not mask.any():
        return img.copy()
    m8 = mask.astype(np.uint8)
    bgr = cv2.inpaint(cv2.cvtColor(img, cv2.COLOR_RGB2BGR), m8 * 255, 3, cv2.INPAINT_TELEA)
    base = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB).astype(np.float32)
    smooth = cv2.GaussianBlur(base, (0, 0), PICTURE_SIGMA / 2)
    # 구멍 가장자리에서는 인페인팅 값(경계와 이어짐), 안쪽으로 갈수록 다듬은 값
    dist = cv2.distanceTransform(m8, cv2.DIST_L2, 3)
    a = np.clip(dist / 2.0, 0.0, 1.0)[..., None]
    fill = (1 - a) * base + a * smooth
    known = ~cv2.dilate(m8, _disk(1)).astype(bool)
    I = img.astype(np.float32)
    hp = I - normalized_blur(I, known, 1.5)
    ring = cv2.dilate(m8, _disk(8)).astype(bool) & known
    if ring.sum() >= 30:
        amp = float(np.clip(np.median(np.abs(luminance(hp)[ring])) * 1.4826, 0.0, 6.0))
        if amp > 0.3:
            rng = np.random.default_rng(seed)
            g = cv2.GaussianBlur(rng.standard_normal(mask.shape).astype(np.float32), (0, 0), 0.6)
            fill = fill + (g * amp / max(float(g.std()), 1e-6))[..., None]
    out = img.copy()
    out[mask] = np.clip(np.rint(fill[mask]), 0, 255).astype(np.uint8)
    return out
