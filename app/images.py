"""이미지(JPG·PNG 등) 카드를 PDF로 바꾼다 (PRD F27). 화면 라이브러리를 쓰지 않는다.

이미지로 들어온 카드는 **카드 한 장 = PDF 한 쪽**으로 바꿔 그룹에 넣는다. 그러면 영역 지정·추출·지우기·
한글 넣기가 PDF와 같은 흐름을 그대로 쓴다(글자 레이어 없는 스캔 PDF와 같은 경로).

- 이미지 한 장에 카드 한 장: 그대로 한 쪽.
- 이미지 한 장에 가로 X장 × 세로 Y장: 카드 사이의 빈 틈(배경)을 찾아 자르고, 틈이 안 보이면 같은 간격으로 나눈다.
  바깥 여백은 잘라 낸다.
- 쪽 크기는 이미지 DPI(없으면 300dpi로 가정)로 정한다. 사진 회전 정보(EXIF)를 반영한다.
- 원본 이미지는 읽기만 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
DEFAULT_DPI = 300


def is_image(path: Path | str) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTS


@dataclass
class SheetCut:
    """이미지 한 장을 자른 결과."""

    source: str
    size: tuple[int, int]  # (가로 px, 세로 px)
    boxes: list[tuple[int, int, int, int]]  # 카드마다 (x0, y0, x1, y1) px, 왼쪽 위부터 가로 먼저
    dpi: float = DEFAULT_DPI
    gutters_found: bool = True  # False면 틈을 못 찾아 같은 간격으로 나눔
    warnings: list[str] = field(default_factory=list)
    frame: int = 0  # 여러 쪽 TIFF의 몇 번째 쪽

    def layout(self) -> dict:
        """file.json에 남길 배치 (결과를 원래 시트로 다시 합칠 때 쓴다)."""
        return {"file": Path(self.source).name, "frame": self.frame, "size": list(self.size), "dpi": self.dpi,
                "boxes": [list(b) for b in self.boxes]}


def load_images(path: Path | str) -> list:
    """이미지 파일 -> PIL 이미지 목록 (여러 쪽 TIFF는 쪽마다). 회전 정보(EXIF)를 반영하고 RGB로."""
    from PIL import Image, ImageOps

    out = []
    with Image.open(path) as im:
        n = getattr(im, "n_frames", 1)
        for i in range(n):
            im.seek(i)
            fr = ImageOps.exif_transpose(im.copy())
            if fr.mode not in ("RGB", "L"):
                bg = Image.new("RGB", fr.size, (255, 255, 255))
                fr = fr.convert("RGBA")
                bg.paste(fr, mask=fr.split()[-1])
                fr = bg
            out.append(fr.convert("RGB"))
    return out


def image_dpi(path: Path | str) -> float:
    from PIL import Image

    with Image.open(path) as im:
        d = im.info.get("dpi")
    try:
        v = float(d[0]) if d else 0.0
    except (TypeError, ValueError, IndexError):
        v = 0.0
    return v if 72 <= v <= 2400 else DEFAULT_DPI  # 72 미만·비정상 값은 사진 기본값일 때가 많다


def _content_mask(rgb: np.ndarray) -> np.ndarray:
    """배경(가장자리 색)과 다른 픽셀."""
    border = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]]).astype(np.float32)
    bg = np.median(border, axis=0)
    dist_border = np.abs(border - bg).max(axis=1)
    # 바탕의 잡음(스캔·JPG)보다 확실히 다르면 카드. 흰 테두리 카드(255)와 회색 바탕(232)처럼 차이가 작아도 가른다
    thr = float(np.clip(np.percentile(dist_border, 98) * 1.5, 12, 40))
    return np.abs(rgb.astype(np.float32) - bg).max(axis=2) > thr


def _cut_positions(profile: np.ndarray, n: int, lo: int, hi: int) -> tuple[list[tuple[int, int]], bool]:
    """내용 비율(profile) 위에서 n칸으로 나눌 n-1개의 틈 [(틈 시작, 틈 끝)]. 같은 간격 근처(±20%)의 가장 빈 곳.
    빈 곳이 뚜렷하지 않으면 같은 간격의 한 점 (두 번째 값 False)."""
    if n <= 1:
        return [], True
    span = hi - lo
    step = span / n
    cuts, found = [], True
    win = max(2, int(step * 0.2))
    for k in range(1, n):
        c = int(lo + step * k)
        a, b = max(lo + 1, c - win), min(hi - 1, c + win)
        seg = profile[a:b]
        if seg.size == 0:
            cuts.append((c, c))
            found = False
            continue
        m = float(seg.min())
        if m > 0.02:  # 카드 사이에 빈 틈이 없다 (붙어 있는 배열)
            cuts.append((c, c))
            found = False
            continue
        idx = np.nonzero(seg <= m + 0.005)[0]
        mid = int(a + (idx[0] + idx[-1]) / 2)
        g0 = g1 = mid  # 가운데에서 양쪽으로 빈 줄이 이어지는 데까지 = 틈
        while g0 - 1 > lo and profile[g0 - 1] <= m + 0.005:
            g0 -= 1
        while g1 + 1 < hi and profile[g1 + 1] <= m + 0.005:
            g1 += 1
        cuts.append((g0, g1 + 1))
    return cuts, found


JUNCTION_SEARCH = 0.06  # 네 카드가 만나는 점을 찾는 범위: 칸 크기의 ±6%
SNAP_SEARCH = 0.017  # 만나는 점으로 정한 경계 둘레에서 카드 끝 선(그림자+밝은 선)을 찾는 범위: 칸 크기의 ±1.7%
SNAP_MIN_SCORE = 0.25  # 카드 끝 선이 그 경계 길이의 이만큼 이상 이어져야 그쪽으로 옮긴다


def _junction(rgb: np.ndarray, cx: int, cy: int, rx: int, ry: int) -> tuple[float, float] | None:
    """(cx, cy) 근처에서 네 카드의 둥근 귀 사이로 보이는 바탕(스캐너 덮개: 회색·무채색) 덩이의 가운데.
    카드에 인쇄된 선·장식은 이렇게 넓은 무채색 덩이를 만들지 않는다. 못 찾으면 None."""
    import cv2

    H, W = rgb.shape[:2]
    x0, x1, y0, y1 = max(0, cx - rx), min(W, cx + rx), max(0, cy - ry), min(H, cy + ry)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    f = cv2.GaussianBlur(rgb[y0:y1, x0:x1, :3].astype(np.float32), (0, 0), 2.0)  # 스캔 잡음에 덩이가 조각나지 않게
    mx, mn = f.max(axis=2), f.min(axis=2)
    lum = f.mean(axis=2)
    sat_w = (mx - mn) / np.maximum(mx, 1.0)
    paper_rgb = np.median(f.reshape(-1, 3), axis=0)  # 창 대부분은 카드 종이
    paper = float(np.median(lum))
    # 바탕: 무채색이고 카드 종이와 색이 다르다 (밝은 카드엔 회색 바탕이 어둡게, 붉은 카드엔 밝게 보인다)
    dist = np.abs(f - paper_rgb).max(axis=2)
    chroma = mx - mn  # 어두운 회색은 채도 비율이 흔들리므로 색 폭(절대값)으로도 본다
    gray = (sat_w < 0.09) | (chroma < 12)
    bg = gray & (dist > 30) & (lum > 15)
    k = max(3, int(min(rx, ry) * 0.06)) | 1
    bg = cv2.morphologyEx(bg.astype(np.uint8), cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    n, lab, st, cen = cv2.connectedComponentsWithStats(bg, connectivity=8)
    min_area = (0.12 * min(rx, ry)) ** 2
    WW, HH = x1 - x0, y1 - y0
    best, best_d, cross = None, None, False
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in st[i])
        if area < min_area:
            continue
        span_x, span_y = x == 0 and x + w >= WW, y == 0 and y + h >= HH
        if span_x and span_y:  # 카드 사이 틈이 넓어 바탕이 십자로 창을 가로지른다: 두 틈이 만나는 곳
            if best is None or not cross:
                best, best_d, cross = i, 0.0, True
            continue
        if cross:
            continue
        sides = (x == 0) + (y == 0) + (x + w >= WW) + (y + h >= HH)
        if sides >= 2 and not (span_x or span_y) and area > 0.15 * WW * HH:  # 창 구석을 넓게 덮는 덩이: 빈 자리의 바탕
            continue
        if span_x or span_y:  # 한 방향으로만 가로지르면: 별(넓다)에 굵은 틈 팔이 붙은 것만. 가는 띠는 아니다
            if (h if span_x else w) < 0.25 * (HH if span_x else WW):
                continue
        # 네 귀 사이 별 모양: 납작하지 않고 상자를 다 채우지 않는다 (카드 안 크림색 띠·흰 칸은 길거나 꽉 찬 사각형)
        elif max(w, h) > 2.5 * min(w, h) or area > 0.75 * w * h:
            continue
        d = float(np.hypot(cen[i][0] - (cx - x0), cen[i][1] - (cy - y0)))
        if best_d is None or d < best_d:
            best, best_d = i, d
    if best is None:
        return None
    # 덩이의 가운데는 귀 모양·카드 어긋남 때문에 치우친다. 경계는 별 밖으로 이어지는 카드 사이 틈(무채색 선)으로 정한다
    # (별 안에 보이는 스캐너 덮개의 격자선은 카드에 가려 별 밖으로는 이어지지 않는다)
    x, y, w, h, _a = (int(v) for v in st[best])
    gap = ((sat_w < 0.15) | (chroma < 18)) & (dist > 30)
    if cross:  # 십자: 가운데 근처에서 틈이 가장 길게 가로지르는 행·열
        rr = max(4, int(0.5 * min(rx, ry)))
        rows = np.convolve(gap.sum(axis=1).astype(np.float32), np.ones(3) / 3, mode="same")
        cols = np.convolve(gap.sum(axis=0).astype(np.float32), np.ones(3) / 3, mode="same")
        ccx, ccy = cx - x0, cy - y0
        xa, xb = max(0, ccx - rr), min(WW, ccx + rr + 1)
        ya, yb = max(0, ccy - rr), min(HH, ccy + rr + 1)
        return x0 + float(xa + int(np.argmax(cols[xa:xb]))), y0 + float(ya + int(np.argmax(rows[ya:yb])))
    outside_x = np.ones(gap.shape[1], bool)
    outside_x[x:x + w] = False
    outside_y = np.ones(gap.shape[0], bool)
    outside_y[y:y + h] = False
    rows = gap[:, outside_x].sum(axis=1).astype(np.float32) if outside_x.any() else gap.sum(axis=1).astype(np.float32)
    cols = gap[outside_y, :].sum(axis=0).astype(np.float32) if outside_y.any() else gap.sum(axis=0).astype(np.float32)
    rows = np.convolve(rows, np.ones(3) / 3, mode="same")
    cols = np.convolve(cols, np.ones(3) / 3, mode="same")
    # 별 가운데 근처에서만 찾는다 (틈이 굵으면 덩이가 한쪽 팔을 따라 창 끝까지 이어진다)
    rr = max(4, int(0.3 * min(w, h)))
    cx_, cy_ = int(cen[best][0]), int(cen[best][1])
    xa, xb = max(0, cx_ - rr), min(len(cols), cx_ + rr + 1)
    ya, yb = max(0, cy_ - rr), min(len(rows), cy_ + rr + 1)
    gx = xa + int(np.argmax(cols[xa:xb])) if xb > xa and cols[xa:xb].max() > 0 else cx_
    gy = ya + int(np.argmax(rows[ya:yb])) if yb > ya and rows[ya:yb].max() > 0 else cy_
    return x0 + float(gx), y0 + float(gy)


def _refine_cuts(rgb: np.ndarray, xs_: list[int], ys_: list[int], cols: int, rows: int
                 ) -> tuple[list[list[int]], list[list[int]], int, int]:
    """카드가 반듯하게 놓이지 않은 배열: 네 카드가 만나는 점(둥근 귀 사이 바탕)을 찾아 그 점을 지나게 자른다.

    세로 경계(열 c와 c+1 사이)는 행마다 그 위·아래 만나는 점의 x로, 가로 경계는 열마다 그 왼쪽·오른쪽 점의 y로.
    시트 가장자리라 점이 하나뿐이면 그 점으로, 하나도 없으면 같은 간격 그대로.
    반환: (vx[행][경계], hy[열][경계], 고친 경계 수, 찾지 못한 경계 수). 경계 0·마지막은 바깥 끝."""
    import cv2

    H, W = rgb.shape[:2]
    cw = (xs_[-1] - xs_[0]) / cols
    ch = (ys_[-1] - ys_[0]) / rows
    rx, ry = int(cw * JUNCTION_SEARCH) + 4, int(ch * JUNCTION_SEARCH) + 4
    # 카드 끝 선: 가는 그림자(어두움) 바로 옆에 밝은 선. 만나는 점으로 정한 경계 가까이에서만 본다
    # (넓게 찾으면 카드에 인쇄된 테두리·장식 선을 잡는다)
    lum = cv2.cvtColor(np.ascontiguousarray(rgb[..., :3]), cv2.COLOR_RGB2GRAY)
    bright, dark = (lum >= 235).astype(np.uint8), (lum <= 120).astype(np.uint8)
    edge_v = cv2.dilate(bright, np.ones((1, 7), np.uint8)) & cv2.dilate(dark, np.ones((1, 7), np.uint8))
    edge_h = cv2.dilate(bright, np.ones((7, 1), np.uint8)) & cv2.dilate(dark, np.ones((7, 1), np.uint8))

    def snap(profile: np.ndarray, lo: int, v: int) -> int:
        if profile.size and float(profile.max()) >= SNAP_MIN_SCORE:
            return lo + int(np.argmax(profile))
        return v
    # 안쪽 만나는 점 (c, r): c=1..cols-1, r=1..rows-1
    J = {}
    for c in range(1, cols):
        for r in range(1, rows):
            J[(c, r)] = _junction(rgb, round((xs_[2 * c - 1] + xs_[2 * c]) / 2), round((ys_[2 * r - 1] + ys_[2 * r]) / 2), rx, ry)
    moved = missing = 0
    vx = []
    for r in range(rows):
        row = [xs_[0]]
        for c in range(1, cols):
            nom = round((xs_[2 * c - 1] + xs_[2 * c]) / 2)
            pts = [J[(c, rr)][0] for rr in (r, r + 1) if (c, rr) in J and J[(c, rr)] is not None]
            v = round(sum(pts) / len(pts)) if pts else nom
            if pts:
                ya, yb = ys_[2 * r], ys_[2 * r + 1]
                band = slice(int(ya + (yb - ya) * 0.1), int(yb - (yb - ya) * 0.1))  # 둥근 귀 빼고
                w_ = int(cw * SNAP_SEARCH) + 2
                lo = max(0, v - w_)
                v = snap(edge_v[band, lo:min(W, v + w_ + 1)].mean(axis=0), lo, v)
            moved += bool(pts) and v != nom
            missing += not pts
            row.append(v)
        row.append(xs_[-1])
        vx.append(row)
    hy = []
    for c in range(cols):
        col = [ys_[0]]
        for r in range(1, rows):
            nom = round((ys_[2 * r - 1] + ys_[2 * r]) / 2)
            pts = [J[(cc, r)][1] for cc in (c, c + 1) if (cc, r) in J and J[(cc, r)] is not None]
            v = round(sum(pts) / len(pts)) if pts else nom
            if pts:
                xa, xb = xs_[2 * c], xs_[2 * c + 1]
                band = slice(int(xa + (xb - xa) * 0.1), int(xb - (xb - xa) * 0.1))
                w_ = int(ch * SNAP_SEARCH) + 2
                lo = max(0, v - w_)
                v = snap(edge_h[lo:min(H, v + w_ + 1), band].mean(axis=1), lo, v)
            moved += bool(pts) and v != nom
            missing += not pts
            col.append(v)
        col.append(ys_[-1])
        hy.append(col)
    return vx, hy, moved, missing


CARD_AREA_MIN_FRAC = 0.25  # 카드 영역: 카드가 가장 많은 열·행의 이만큼 이상 카드로 덮인 열·행
EXTRA_MIN_INK = 0.01  # 카드 밖에 이만큼 이상 어두운 선·글자(자 눈금·모눈)가 있어야 카드가 아닌 것으로 보고 뺀다
CELL_MIN_FILL = 0.5  # 칸이 이만큼 이상 카드로 덮여야 카드가 있다고 본다 (아니면 빈 칸: 쪽을 만들지 않는다)


def _card_mask_small(rgb: np.ndarray, width: int = 1000) -> tuple[np.ndarray, float]:
    """줄인 그림에서 카드(종이·인쇄)가 있는 곳. 무채색 바탕(흰 종이·모눈종이·자·스캐너 덮개)과 가는 선은 뺀다.
    (마스크, 줄인 비율)."""
    import cv2

    h, w = rgb.shape[:2]
    k = min(1.0, width / w)
    small = cv2.resize(np.ascontiguousarray(rgb[..., :3]), (max(1, int(w * k)), max(1, int(h * k))),
                       interpolation=cv2.INTER_AREA)
    f = small.astype(np.float32)
    mx, mn = f.max(axis=2), f.min(axis=2)
    sat = (mx - mn) / np.maximum(mx, 1.0)
    lum = f.mean(axis=2)
    bg = (sat < 0.07) & ((lum > 120) | (lum < 70))  # 무채색: 흰·회색 바탕, 검은 눈금·글자
    m = (~bg).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7)))  # 자 눈금·모눈
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9)))
    n, lab, st, _ = cv2.connectedComponentsWithStats(1 - m, connectivity=4)  # 카드 안 흰 칸·검은 글자 메우기
    H, W = m.shape
    for i in range(1, n):
        x, y, ww, hh, _a = st[i]
        if x > 0 and y > 0 and x + ww < W and y + hh < H:
            m[lab == i] = 1
    return m.astype(bool), k


def _largest_run(on: np.ndarray, bridge: int = 0) -> tuple[int, int] | None:
    """켜진 칸이 가장 길게 이어진 구간 [s, e). bridge 이하로 짧게 끊긴 곳(카드 사이 틈)은 이어 본다."""
    on = np.asarray(on, bool).copy()
    if bridge > 0 and on.any():
        idx = np.nonzero(on)[0]
        for a_, b_ in zip(idx[:-1], idx[1:]):
            if 1 < b_ - a_ <= bridge + 1:
                on[a_:b_] = True
    best, s = None, None
    for i, v in enumerate(list(on) + [False]):
        if v and s is None:
            s = i
        elif not v and s is not None:
            if best is None or i - s > best[1] - best[0]:
                best = (s, i)
            s = None
    return best


def _card_area(rgb: np.ndarray) -> tuple[tuple[int, int, int, int] | None, np.ndarray, float]:
    """카드만 있는 곳 (x0, y0, x1, y1). 스캔에 자·모눈종이·빈 종이가 같이 찍힌 경우 그것을 뺀다.
    그림 거의 전부가 카드면 None. (영역, 줄인 카드 마스크, 줄인 비율)."""
    m, k = _card_mask_small(rgb)
    if not m.any():
        return None, m, k
    colf, rowf = m.mean(axis=0), m.mean(axis=1)
    H, W = m.shape
    xr = _largest_run(colf >= CARD_AREA_MIN_FRAC * colf.max(), bridge=int(0.05 * W))
    yr = _largest_run(rowf >= CARD_AREA_MIN_FRAC * rowf.max(), bridge=int(0.05 * H))
    if xr is None or yr is None:
        return None, m, k
    H, W = m.shape
    if (xr[1] - xr[0]) >= 0.97 * W and (yr[1] - yr[0]) >= 0.97 * H:
        return None, m, k
    # 뺄 곳이 그냥 여백(고른 바탕)이면 예전 방식 그대로. 자·모눈종이처럼 가는 검은 선·숫자가 있을 때만 뺀다
    import cv2

    h, w = rgb.shape[:2]
    small = cv2.resize(np.ascontiguousarray(rgb[..., :3]), (W, H), interpolation=cv2.INTER_AREA)
    f = small.astype(np.float32)
    lum = f.mean(axis=2)
    sat = (f.max(axis=2) - f.min(axis=2)) / np.maximum(f.max(axis=2), 1.0)
    out = np.ones((H, W), bool)
    out[yr[0]:yr[1], xr[0]:xr[1]] = False
    if not out.any() or m[out].sum() > 0.05 * m.sum():  # 카드 같은 것이 영역 밖에도 많으면 영역을 잘못 잡은 것
        return None, m, k
    if float(((lum[out] < 120) & (sat[out] < 0.12)).mean()) < EXTRA_MIN_INK:  # 무채색 눈금·선·숫자가 없으면 여백
        return None, m, k
    area = (int(xr[0] / k), int(yr[0] / k), min(w, int(round(xr[1] / k))), min(h, int(round(yr[1] / k))))
    return area, m, k


def _grid_in_area(rgb: np.ndarray, cols: int, rows: int) -> tuple[int, int]:
    """카드 영역에 실제로 몇 열·몇 행이 있는가 (덜 채운 시트: 예 3×3 자리에 2줄만).
    줄 수를 바꿔 가며 그 자리에 네 카드가 만나는 점(둥근 귀 사이 바탕)이 가장 잘 보이는 수를 고른다."""
    h, w = rgb.shape[:2]
    best, best_key = (cols, rows), None
    for nc in range(cols, 0, -1):
        for nr in range(rows, 0, -1):
            pts = [(round(w * c / nc), round(h * r / nr)) for c in range(1, nc) for r in range(1, nr)]
            if not pts:
                continue
            rx, ry = int(w / nc * JUNCTION_SEARCH) + 4, int(h / nr * JUNCTION_SEARCH) + 4
            found = sum(_junction(rgb, x, y, rx, ry) is not None for x, y in pts)
            # 만나는 점이 반 이상 보이는 배열 중 가장 큰 것 (정한 배열이 먼저: 우연히 맞는 작은 배열에 지지 않게)
            key = (found / len(pts) >= 0.5, nc * nr, found / len(pts))
            if best_key is None or key > best_key:
                best, best_key = (nc, nr), key
    if best_key is None or not best_key[0]:  # 만나는 점이 거의 안 보이면(틈 있는 배열 등) 정한 수 그대로
        return cols, rows
    return best


def cut_sheet(path: Path | str, cols: int = 1, rows: int = 1, frame: int = 0) -> SheetCut:
    """이미지 한 장(frame: 여러 쪽 TIFF의 몇 번째)을 cols×rows 카드로 자른다.
    스캔에 카드 말고 다른 것(자·모눈종이·빈 종이)이 있으면 카드가 있는 곳만 쓰고, 카드가 없는 칸은 뺀다."""
    im = load_images(path)[frame]
    rgb = np.asarray(im)
    h, w = rgb.shape[:2]
    cut = SheetCut(str(path), (w, h), [], image_dpi(path), frame=frame)
    if cols < 1 or rows < 1:
        raise ValueError("가로·세로 장수는 1 이상")
    area, cmask, k = _card_area(rgb)
    if area is None:
        _cut_grid(rgb, cols, rows, cut)
        return cut
    # 카드 둘레에 바탕을 조금 남긴다 (틈 찾기는 그림 가장자리 색을 바탕으로 본다)
    pad = int(0.006 * max(w, h))
    ax0, ay0 = max(0, area[0] - pad), max(0, area[1] - pad)
    ax1, ay1 = min(w, area[2] + pad), min(h, area[3] + pad)
    sub = rgb[ay0:ay1, ax0:ax1]
    nc, nr = _grid_in_area(sub, cols, rows) if cols * rows > 1 else (1, 1)
    _cut_grid(sub, nc, nr, cut)
    # 둘레에 남긴 바탕은 다시 뺀다 (카드 영역 밖으로 나가지 않게)
    boxes = [(max(area[0], x0 + ax0), max(area[1], y0 + ay0), min(area[2], x1 + ax0), min(area[3], y1 + ay0))
             for x0, y0, x1, y1 in cut.boxes]
    kept = []
    for b in boxes:  # 카드가 없는 칸(덜 채운 시트)은 쪽을 만들지 않는다
        sb = cmask[int(b[1] * k):max(int(b[1] * k) + 1, int(b[3] * k)), int(b[0] * k):max(int(b[0] * k) + 1, int(b[2] * k))]
        if sb.size and float(sb.mean()) >= CELL_MIN_FILL:
            kept.append(b)
    cut.boxes = kept
    # 크기 검사는 남은 카드로 다시 (빈 자리 칸은 크기가 달라도 상관없다)
    cut.warnings = [x for x in cut.warnings if not x.startswith("크기가 다른 카드")]
    if kept:
        mw = np.median([b[2] - b[0] for b in kept])
        mh = np.median([b[3] - b[1] for b in kept])
        odd = [i + 1 for i, b in enumerate(kept)
               if abs((b[2] - b[0]) - mw) > mw * 0.08 or abs((b[3] - b[1]) - mh) > mh * 0.08]
        if odd:
            cut.warnings.append(f"크기가 다른 카드가 있습니다: {', '.join(map(str, odd))}번째 (잘못 잘렸을 수 있음)")
    notes = ["카드가 아닌 부분(자·종이 등)은 빼고 카드가 있는 곳만 잘랐습니다"]
    if len(kept) < cols * rows:
        notes.append(f"카드 {len(kept)}장을 찾았습니다 ({cols}×{rows} 자리 중 빈 자리는 뺌)")
    cut.warnings[:0] = notes
    return cut


def _cut_grid(rgb: np.ndarray, cols: int, rows: int, cut: SheetCut) -> None:
    """rgb(카드만 있는 그림)를 cols×rows로 잘라 cut.boxes·warnings에 더한다 (rgb 좌표)."""
    h, w = rgb.shape[:2]
    if cols == 1 and rows == 1:
        cut.boxes = [(0, 0, w, h)]
        return
    mask = _content_mask(rgb)
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:  # 이미지 전체가 한 색으로 이어짐: 카드끼리 틈 없이 붙어 있다
        x0, y0, x1, y1 = 0, 0, w, h
    else:  # 바깥 여백은 잘라 낸다
        x0, x1, y0, y1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
    colp = mask[y0:y1].mean(axis=0)
    rowp = mask[:, x0:x1].mean(axis=1)
    if len(xs) == 0:  # 틈이 없으면 같은 간격
        xc = [(round(x0 + (x1 - x0) * k / cols),) * 2 for k in range(1, cols)]
        yc = [(round(y0 + (y1 - y0) * k / rows),) * 2 for k in range(1, rows)]
        fx = fy = False
    else:
        xc, fx = _cut_positions(colp, cols, x0, x1)
        yc, fy = _cut_positions(rowp, rows, y0, y1)
    cut.gutters_found = fx and fy and len(xs) > 0
    # 칸 경계 = 바깥 여백 끝과 틈의 시작·끝 (배열 전체에서 공통).
    # 칸마다 그림이 있는 부분만 남기면 가장자리가 흰 카드(바탕과 비슷)가 들쭉날쭉 작게 잘린다.
    xs_ = [x0] + [v for g in xc for v in g] + [x1]
    ys_ = [y0] + [v for g in yc for v in g] + [y1]
    if cut.gutters_found or rows * cols == 1 or min(rows, cols) == 1:
        # 틈(바탕)이 보이는 배열은 공통 틈으로 자른다 (같은 열·행의 카드는 같은 크기)
        if not cut.gutters_found:
            cut.warnings.append("카드 사이 틈을 찾지 못한 곳은 같은 간격으로 나눴습니다 (미리 보기로 확인하세요)")
        for r in range(rows):
            for c in range(cols):
                cut.boxes.append((xs_[2 * c], ys_[2 * r], xs_[2 * c + 1], ys_[2 * r + 1]))
    else:
        # 틈이 안 보이는 배열(카드가 붙었거나 틈이 아주 좁음): 카드가 반듯하게 놓이지 않아 줄·열마다 경계가
        # 조금씩 다르다. 네 카드가 만나는 점을 찾아 이웃 카드 사이마다 따로 자른다
        vx, hy, moved, missing = _refine_cuts(rgb, xs_, ys_, cols, rows)
        if missing:
            cut.warnings.append(f"카드 사이 경계를 찾지 못한 {missing}곳은 같은 간격으로 나눴습니다 (미리 보기로 확인하세요)")
        if moved:
            cut.warnings.append(f"카드 위치에 맞춰 자르는 자리를 {moved}곳 고쳤습니다")
        for r in range(rows):
            for c in range(cols):
                cut.boxes.append((vx[r][c], hy[c][r], vx[r][c + 1], hy[c][r + 1]))
    sizes = [(b[2] - b[0], b[3] - b[1]) for b in cut.boxes]
    mw, mh = np.median([s[0] for s in sizes]), np.median([s[1] for s in sizes])
    odd = [i + 1 for i, (sw, sh) in enumerate(sizes) if abs(sw - mw) > mw * 0.08 or abs(sh - mh) > mh * 0.08]
    if odd:
        cut.warnings.append(f"크기가 다른 카드가 있습니다: {', '.join(map(str, odd))}번째 (빈 칸이거나 잘못 잘렸을 수 있음)")


def preview_png(path: Path | str, cols: int = 1, rows: int = 1, max_px: int = 900) -> tuple[bytes, SheetCut]:
    """자를 자리를 빨간 상자와 번호로 표시한 미리 보기 PNG."""
    import io

    from PIL import ImageDraw

    cut = cut_sheet(path, cols, rows)
    im = load_images(path)[0]
    s = min(1.0, max_px / max(im.size))
    if s < 1.0:
        im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))))
    d = ImageDraw.Draw(im)
    lw = max(2, int(3 * s / 0.5)) if s < 1 else 3
    for i, (x0, y0, x1, y1) in enumerate(cut.boxes, start=1):
        box = [x0 * s, y0 * s, x1 * s - 1, y1 * s - 1]
        d.rectangle(box, outline=(220, 30, 30), width=lw)
        d.text((box[0] + 6, box[1] + 4), str(i), fill=(220, 30, 30))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue(), cut


@dataclass
class ImagePdfResult:
    pdf: Path
    pages: int
    sheets: list[SheetCut]
    warnings: list[str] = field(default_factory=list)


def images_to_pdf(paths: list[Path | str], out_pdf: Path | str, cols: int = 1, rows: int = 1) -> ImagePdfResult:
    """이미지 여러 장 -> 카드 한 장이 한 쪽인 PDF. 이미지 순서대로, 한 이미지 안에서는 왼쪽 위부터 가로 먼저."""
    import io

    import pymupdf

    out_pdf = Path(out_pdf)
    doc = pymupdf.open()
    sheets, warnings = [], []
    for p in paths:
        frames = load_images(p)
        for fi, im in enumerate(frames):
            cut = cut_sheet(p, cols, rows, frame=fi)
            sheets.append(cut)
            warnings += [f"{Path(p).name}: {w}" for w in cut.warnings]
            jpeg_src = Path(p).suffix.lower() in (".jpg", ".jpeg")
            for (x0, y0, x1, y1) in cut.boxes:
                card = im.crop((x0, y0, x1, y1))
                buf = io.BytesIO()
                if jpeg_src:  # 사진(JPG)은 JPG로, 나머지는 손실 없는 PNG로
                    card.save(buf, format="JPEG", quality=95, subsampling=0)
                else:
                    card.save(buf, format="PNG", optimize=True)
                wpt, hpt = card.width * 72.0 / cut.dpi, card.height * 72.0 / cut.dpi
                page = doc.new_page(width=wpt, height=hpt)
                page.insert_image(page.rect, stream=buf.getvalue())
    if doc.page_count == 0:
        doc.close()
        raise ValueError("넣을 이미지가 없습니다")
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out_pdf, garbage=3, deflate=True)
    n = doc.page_count
    doc.close()
    return ImagePdfResult(out_pdf, n, sheets, warnings)


def sheet_output_name(layout: dict, frames_in_file: int = 1) -> str:
    """합친 시트 이미지 파일 이름: 원본 이름 그대로 (JPG는 JPG, 나머지는 PNG). 여러 쪽 TIFF는 _쪽번호."""
    p = Path(layout["file"])
    ext = ".jpg" if p.suffix.lower() in (".jpg", ".jpeg") else ".png"
    tail = f"_{layout.get('frame', 0) + 1}" if frames_in_file > 1 else ""
    return f"{p.stem}{tail}{ext}"


def compose_sheets(layouts: list[dict], images_dir: Path | str, cards_pdf: Path | str, page_numbers: list[int],
                   out_dir: Path | str, extra: list[tuple[Path, list[int]]] | None = None) -> list[Path]:
    """한글을 넣은 카드(쪽)를 원본 시트 이미지의 같은 자리에 다시 붙여, 원래 크기·배열의 이미지로 저장한다.

    layouts: 시트마다 {"file", "frame", "size", "dpi", "boxes"} (이미지 순서·카드 순서 = PDF 쪽 순서).
    cards_pdf의 i번째 쪽이 원래 PDF의 page_numbers[i]쪽이다. 그 안에 없는 쪽(뺀 쪽)은 원본 그대로 둔다.
    카드 사이 틈과 바깥 여백도 원본 그대로다.
    extra: [(다른 결과 PDF, 쪽 번호들)] — 같은 시트의 카드를 다른 그룹에서 넣은 결과도 함께 붙인다
    (cards_pdf에 있는 쪽이 먼저)."""
    import pymupdf
    from PIL import Image

    images_dir, out_dir = Path(images_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    frames: dict[str, int] = {}
    for lay in layouts:
        frames[lay["file"]] = frames.get(lay["file"], 0) + 1
    docs = []
    where: dict[int, tuple] = {}
    for pdf, pages in reversed([(Path(cards_pdf), page_numbers)] + list(extra or [])):
        d_ = pymupdf.open(pdf)
        docs.append(d_)
        where.update({p: (d_, i) for i, p in enumerate(pages)})
    outs, page_no = [], 0
    try:
        for lay in layouts:
            sheet = load_images(images_dir / lay["file"])[lay.get("frame", 0)].copy()
            if list(sheet.size) != list(lay["size"]):
                raise ValueError(f"{lay['file']}: 원본 이미지 크기가 넣을 때와 다릅니다 {sheet.size} ≠ {tuple(lay['size'])}")
            for (x0, y0, x1, y1) in lay["boxes"]:
                page_no += 1
                hit = where.get(page_no)
                if hit is None:
                    continue
                bw, bh = x1 - x0, y1 - y0
                pg = hit[0][hit[1]]
                mat = pymupdf.Matrix(bw / pg.rect.width, bh / pg.rect.height)
                pix = pg.get_pixmap(matrix=mat, alpha=False)
                card = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                if card.size != (bw, bh):  # 반올림으로 1px 차이
                    card = card.resize((bw, bh), Image.Resampling.LANCZOS)
                sheet.paste(card, (x0, y0))
            out = out_dir / sheet_output_name(lay, frames[lay["file"]])
            dpi = (float(lay.get("dpi") or DEFAULT_DPI),) * 2
            if out.suffix == ".jpg":
                sheet.save(out, format="JPEG", quality=95, subsampling=0, dpi=dpi)
            else:
                sheet.save(out, format="PNG", optimize=True, dpi=dpi)
            outs.append(out)
    finally:
        for d_ in docs:
            d_.close()
    return outs
