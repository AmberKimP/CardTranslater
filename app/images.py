"""이미지(JPG·PNG 등) 카드를 PDF로 바꾼다 (PRD F27). 화면 라이브러리를 쓰지 않는다.

이미지로 들어온 카드는 **카드 한 장 = PDF 한 쪽**으로 바꿔 그룹에 넣는다. 그러면 영역 지정·추출·지우기·
한글 넣기가 PDF와 같은 흐름을 그대로 쓴다(글자 레이어 없는 스캔 PDF와 같은 경로).

- 이미지 한 장에 카드 한 장: 그대로 한 쪽.
- 이미지 한 장에 가로 X장 × 세로 Y장: 카드 사이의 빈 틈(배경)을 찾아 자르고, 틈이 안 보이면 같은 간격으로 나눈다.
  바깥 여백은 잘라 낸다.
- 수동으로 영역 지정: 사람이 이미지 위에 그린 카드 상자(이미지 px) 그대로 자른다 (manual_cut).
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
    junctions: bool = False  # 틈이 안 보여 네 카드가 만나는 점으로 잘랐다 (붙은 배열)

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


PDF_SHEET_DPI = 300  # 카드 여러 장이 한 쪽에 있는 PDF를 그림으로 바꿀 때 (쪽이 스캔 그림 한 장이면 그 해상도)
PDF_SHEET_MAX_DPI = 600


def _page_scan_dpi(page) -> float | None:
    """쪽을 거의 덮는 그림 한 장(스캔)의 해상도 (가로 기준). 없으면 None."""
    best = None
    area = page.rect.width * page.rect.height
    for info in page.get_image_info():
        x0, y0, x1, y1 = info["bbox"]
        cover = max(0.0, x1 - x0) * max(0.0, y1 - y0) / area if area else 0
        if cover >= 0.85 and x1 > x0 and info.get("width"):
            best = max(best or 0.0, info["width"] * 72.0 / (x1 - x0))
    return best


def pdf_to_images(pdf: Path | str, out_dir: Path | str) -> list[Path]:
    """PDF 쪽마다 PNG 한 장 (<PDF 이름>_p001.png …). 한 쪽에 카드가 여러 장인 PDF를 이미지 시트처럼 자르려고 쓴다.
    쪽이 스캔 그림 한 장이면 그 해상도로(최대 PDF_SHEET_MAX_DPI), 아니면 PDF_SHEET_DPI로 그린다. 해상도는 PNG에 적어 둔다
    (카드 쪽 크기가 원래 PDF와 같게)."""
    import pymupdf

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = []
    with pymupdf.open(pdf) as doc:
        for i, page in enumerate(doc):
            dpi = _page_scan_dpi(page) or PDF_SHEET_DPI
            dpi = int(round(min(max(dpi, 72), PDF_SHEET_MAX_DPI)))
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            pix.set_dpi(dpi, dpi)
            p = out_dir / f"{Path(pdf).stem}_p{i + 1:03d}.png"
            pix.save(str(p))
            out.append(p)
    if not out:
        raise ValueError(f"{Path(pdf).name}: 쪽이 없는 PDF입니다")
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
    # 만나는 점을 카드 안의 흰 그림(옷·레이스)에서 잘못 잡으면 경계가 크게 밀려 이웃 카드 크기가 어긋난다
    # (공주와 기사 시트: 폭 374·346·400). 같은 크기 카드 배열이므로 옮긴 결과 이웃 칸이 칸 크기에서
    # CELL_SIZE_TOL 넘게 벗어나는 경계는 옮기지 않는다 (같은 간격 자리로 되돌림)
    nom_x = [xs_[0]] + [round((xs_[2 * c - 1] + xs_[2 * c]) / 2) for c in range(1, cols)] + [xs_[-1]]
    nom_y = [ys_[0]] + [round((ys_[2 * r - 1] + ys_[2 * r]) / 2) for r in range(1, rows)] + [ys_[-1]]
    moved -= _revert_odd_cuts(vx, nom_x, cw)
    moved -= _revert_odd_cuts(hy, nom_y, ch)
    return vx, hy, moved, missing


CELL_SIZE_TOL = 0.04  # 경계를 옮긴 뒤 이웃 칸 크기가 칸 크기에서 이만큼 넘게 벗어나면 옮기지 않는다


def _revert_odd_cuts(lines: list[list[int]], nominal: list[int], cell: float) -> int:
    """lines[줄][경계]: 옮긴 경계 때문에 이웃 칸 크기가 cell에서 CELL_SIZE_TOL 넘게 벗어나면 그 경계를 nominal로
    되돌린다 (줄마다, 바깥 끝은 그대로). 되돌린 경계 수."""
    n = 0
    tol = CELL_SIZE_TOL * cell
    for line in lines:
        for _ in range(len(line)):  # 하나를 되돌리면 이웃 칸 크기가 바뀌므로 바뀌지 않을 때까지
            bad = [i for i in range(1, len(line) - 1) if line[i] != nominal[i]
                   and (abs((line[i] - line[i - 1]) - cell) > tol or abs((line[i + 1] - line[i]) - cell) > tol)]
            if not bad:
                break
            i = max(bad, key=lambda j: abs(line[j] - nominal[j]))  # 가장 많이 옮긴 것부터
            line[i] = nominal[i]
            n += 1
    return n


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


def _apply_equal_size(rgb: np.ndarray, cut: SheetCut) -> None:
    """cut.boxes를 같은 크기로 맞춘다 (맞췄으면 크기 경고를 빼고 알린다)."""
    if len(cut.boxes) < 2:
        return
    H, W = rgb.shape[:2]
    # 그림 끝에 걸려 잘린 카드는 원래 작다: 크기가 고른지 볼 때 뺀다
    inner = [b for b in cut.boxes if b[0] > 0 and b[1] > 0 and b[2] < W and b[3] < H] or cut.boxes
    ws = [b[2] - b[0] for b in inner]
    hs = [b[3] - b[1] for b in inner]
    spread = max((max(ws) - min(ws)) / max(1.0, float(np.median(ws))), (max(hs) - min(hs)) / max(1.0, float(np.median(hs))))
    if cut.gutters_found and spread <= EQUAL_SIZE_SPREAD:  # 틈이 보이고 크기도 고르다: 그대로
        return
    # 틈이 안 보여 네 카드가 만나는 점으로 자른 경계는 실제 카드 경계다: 경계 자리는 조금만 옮긴다.
    # (붙은 카드는 끝 선이 거의 없어, 넓게 찾으면 카드 안 글상자 테두리처럼 긴 밝은·어두운 선을 끝 선으로 잡는다 — 공주와 기사 시트)
    boxes, size = _fit_equal_size(rgb, cut.boxes, touching=cut.junctions)
    if size is None:
        return
    cut.boxes = boxes
    cut.warnings = [x for x in cut.warnings if not x.startswith(("크기가 다른 카드", "카드 위치에 맞춰", "카드 사이 경계를"))]
    cut.warnings.append(f"카드 크기가 같다고 보고 카드 끝 선에 맞춰 잘랐습니다 (카드 {size[0]}×{size[1]}px)")


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
        _apply_equal_size(rgb, cut)
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
    _apply_equal_size(rgb, cut)
    notes = ["카드가 아닌 부분(자·종이 등)은 빼고 카드가 있는 곳만 잘랐습니다"]
    if len(kept) < cols * rows:
        notes.append(f"카드 {len(kept)}장을 찾았습니다 ({cols}×{rows} 자리 중 빈 자리는 뺌)")
    cut.warnings[:0] = notes
    return cut


EQUAL_PITCH_RANGE = (0.9, 1.1)  # 카드 크기(반복 간격) 찾기: 처음 자른 칸 크기(중앙값)의 이 범위에서
EQUAL_PITCH_TRUST = 0.04  # 반복 간격이 처음 자른 칸 크기(중앙값)와 이만큼 넘게 다르면 믿지 않고 중앙값을 카드 크기로
EQUAL_PHASE_SHIFT = 0.06  # 카드 경계 자리 찾기: 처음 자른 경계 둘레 카드 크기의 ±6%
#   (카드 안에도 끝 선처럼 보이는 가로줄이 있다: 제목 아래 띠 등, 카드 위에서 약 26%. 범위를 넓히면 그쪽을 잡는다)
EQUAL_JUNCTION_SHIFT = 0.02  # 틈 없이 붙은 배열(만나는 점으로 자름)에서 경계 자리를 찾는 범위 (카드 크기의 ±2%)
EQUAL_CARD_SHIFT = 0.02  # 카드마다 제 끝 선이 뚜렷하면 이만큼(카드 크기 대비)까지 따로 옮긴다 (손으로 놓아 조금씩 어긋남)
EQUAL_EDGE_MIN = 0.3  # 카드 하나의 끝 선이 그 변 길이의 이만큼 이상 이어져야 그 카드만 따로 옮긴다
EQUAL_SIZE_SPREAD = 0.02  # 처음 자른 카드 크기가 이만큼 넘게 들쭉날쭉하면 (틈을 잘못 찾음) 같은 크기로 다시 맞춘다


def _edge_maps(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """카드 끝 선 지도 (세로 선, 가로 선): 가는 그림자(어두움) 바로 옆에 밝은 선(카드 종이 단면)."""
    import cv2

    lum = cv2.cvtColor(np.ascontiguousarray(rgb[..., :3]), cv2.COLOR_RGB2GRAY)
    bright, dark = (lum >= 235).astype(np.uint8), (lum <= 120).astype(np.uint8)
    ev = cv2.dilate(bright, np.ones((1, 7), np.uint8)) & cv2.dilate(dark, np.ones((1, 7), np.uint8))
    eh = cv2.dilate(bright, np.ones((7, 1), np.uint8)) & cv2.dilate(dark, np.ones((7, 1), np.uint8))
    return ev, eh


def _pitch(lum: np.ndarray, axis: int, size0: float) -> int | None:
    """카드가 한 줄로 반복되는 간격 (px): 밝기 변화량 윤곽의 자기상관이 가장 큰 간격 (size0의 EQUAL_PITCH_RANGE 안).
    같은 카드 틀(제목 띠·숫자 원·테두리)이 카드마다 같은 자리에 있어 간격이 또렷하다. 못 찾으면 None."""
    # axis=0: 가로 방향 반복 (열마다 가로 밝기 변화량), axis=1: 세로 방향 반복
    g = np.abs(np.diff(lum, axis=1)).mean(axis=0) if axis == 0 else np.abs(np.diff(lum, axis=0)).mean(axis=1)
    lo, hi = int(size0 * EQUAL_PITCH_RANGE[0]), int(size0 * EQUAL_PITCH_RANGE[1])
    if hi >= len(g) or lo < 4:
        return None
    p = g - g.mean()
    f = np.fft.rfft(p, 2 * len(p))
    ac = np.fft.irfft(f * np.conj(f))[:len(p)]
    k = lo + int(np.argmax(ac[lo:hi + 1]))
    return k if ac[k] > 0 else None


def _phase(prof: np.ndarray, pitch: int, phase0: float, r: int) -> float:
    """카드 경계 자리 (pitch로 나눈 나머지): phase0 둘레 ±r에서 모든 경계 자리의 끝 선 세기 합이 가장 큰 곳.
    끝 선이 거의 없으면 phase0."""
    n = len(prof)
    best, best_v = phase0, 0.0
    for d in range(-r, r + 1):
        ph = phase0 + d
        idx = np.arange(int(round(ph)) % pitch, n, pitch)
        v = float(prof[idx].sum()) if len(idx) else 0.0
        if v > best_v:
            best, best_v = ph, v
    return best if best_v >= 0.3 else phase0


def _fit_equal_size(rgb: np.ndarray, boxes: list[tuple[int, int, int, int]], touching: bool = False
                    ) -> tuple[list[tuple[int, int, int, int]], tuple[int, int] | None]:
    """카드가 있는 곳(+찾는 범위 여유)만 잘라 맞춘다 (큰 스캔에서 메모리·시간을 줄인다). _fit_equal_size_in 참고."""
    if len(boxes) < 2:
        return boxes, None
    H, W = rgb.shape[:2]
    w0 = float(np.median([b[2] - b[0] for b in boxes]))
    h0 = float(np.median([b[3] - b[1] for b in boxes]))
    mx, my = int(w0 * (EQUAL_PHASE_SHIFT + 2 * EQUAL_CARD_SHIFT)) + 8, int(h0 * (EQUAL_PHASE_SHIFT + 2 * EQUAL_CARD_SHIFT)) + 8
    cx0, cy0 = max(0, min(b[0] for b in boxes) - mx), max(0, min(b[1] for b in boxes) - my)
    cx1, cy1 = min(W, max(b[2] for b in boxes) + mx), min(H, max(b[3] for b in boxes) + my)
    sub = rgb[cy0:cy1, cx0:cx1]
    local = [(x0 - cx0, y0 - cy0, x1 - cx0, y1 - cy0) for x0, y0, x1, y1 in boxes]
    out, size = _fit_equal_size_in(sub, local, touching)
    if size is None:
        return boxes, None
    return [(x0 + cx0, y0 + cy0, x1 + cx0, y1 + cy0) for x0, y0, x1, y1 in out], size


def _fit_equal_size_in(rgb: np.ndarray, boxes: list[tuple[int, int, int, int]], touching: bool = False
                       ) -> tuple[list[tuple[int, int, int, int]], tuple[int, int] | None]:
    """한 시트의 카드는 크기가 같다 (붙어 있는 배열): 카드 크기와 경계 자리를 시트 전체에서 정해 모든 카드를 같은 크기로.

    1. 카드 크기 = 카드 틀이 반복되는 간격 (_pitch). 처음 자른 칸 크기 ±10% 안에서.
    2. 경계 자리 = 처음 자른 안쪽 경계 둘레 ±EQUAL_PHASE_SHIFT에서 시트 전체 끝 선이 가장 잘 맞는 곳 (_phase).
    3. 카드마다 제 끝 선이 뚜렷하면 ±EQUAL_CARD_SHIFT 안에서 따로 맞춘다.
    touching: 틈 없이 붙은 배열 (처음 경계 = 네 카드가 만나는 점). 붙은 카드는 끝 선이 거의 없어 넓게 찾으면 카드 안
    글상자 테두리처럼 긴 밝은·어두운 선을 끝 선으로 잡는다 (공주와 기사 시트): 경계 자리는 ±EQUAL_JUNCTION_SHIFT만
    옮기고, 카드 크기는 반복 간격(믿을 만할 때)을 넘지 않는다 (넘으면 옆 카드와 겹친다).
    반환: (상자, (가로, 세로) 또는 None = 맞추지 않음)."""
    import cv2

    if len(boxes) < 2:
        return boxes, None
    H, W = rgb.shape[:2]
    lum = cv2.cvtColor(np.ascontiguousarray(rgb[..., :3]), cv2.COLOR_RGB2GRAY).astype(np.float32)
    w0 = float(np.median([b[2] - b[0] for b in boxes]))
    h0 = float(np.median([b[3] - b[1] for b in boxes]))
    # 카드가 있는 곳만 본다 (둘레의 자·모눈종이는 짧은 간격으로 반복돼 카드 간격을 가린다)
    ux0, uy0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    ux1, uy1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
    pw, ph = _pitch(lum[uy0:uy1, ux0:ux1], 0, w0), _pitch(lum[uy0:uy1, ux0:ux1], 1, h0)
    # 카드마다 그림이 많이 달라 반복 간격이 흐리면 엉뚱한 간격이 나온다: 처음 자른 크기(중앙값)와 많이 다르면 그 중앙값을 쓴다
    trust_w = pw is not None and abs(pw / w0 - 1) <= EQUAL_PITCH_TRUST
    trust_h = ph is not None and abs(ph / h0 - 1) <= EQUAL_PITCH_TRUST
    if not trust_w:
        pw = int(round(w0))
    if not trust_h:
        ph = int(round(h0))
    ev, eh = _edge_maps(rgb)
    ex = np.zeros(W, np.float32)
    ey = np.zeros(H, np.float32)
    ex[ux0:ux1] = ev[uy0:uy1, ux0:ux1].mean(axis=0)  # 카드 영역의 끝 선 윤곽
    ey[uy0:uy1] = eh[uy0:uy1, ux0:ux1].mean(axis=1)

    def phase0(starts: list[int], pitch: int) -> float:
        # 처음 자른 경계(첫 줄은 그림 끝·여백이라 뺌)의 pitch 나머지: 원 위의 평균
        inner = [v for v in starts if v > min(starts) + 0.5 * pitch] or starts
        ang = np.array(inner, np.float64) * (2 * np.pi / pitch)
        m = np.arctan2(np.sin(ang).mean(), np.cos(ang).mean())
        return float((m % (2 * np.pi)) * pitch / (2 * np.pi))

    phase_shift = EQUAL_JUNCTION_SHIFT if touching else EQUAL_PHASE_SHIFT
    fx = _phase(ex, pw, phase0([b[0] for b in boxes], pw), int(pw * phase_shift) + 2)
    fy = _phase(ey, ph, phase0([b[1] for b in boxes], ph), int(ph * phase_shift) + 2)
    cvx = np.cumsum(ev, axis=0, dtype=np.int32)
    chy = np.cumsum(eh, axis=1, dtype=np.int32)

    def own(cum: np.ndarray, lo: int, hi: int, at: int, vertical: bool) -> float:
        if hi - lo < 8:
            return 0.0
        n = cum.shape[1] if vertical else cum.shape[0]
        if not 0 <= at < n:
            return 0.0
        if vertical:
            return float(cum[hi - 1, at] - (cum[lo - 1, at] if lo > 0 else 0)) / (hi - lo)
        return float(cum[at, hi - 1] - (cum[at, lo - 1] if lo > 0 else 0)) / (hi - lo)

    sx, sy = int(pw * EQUAL_CARD_SHIFT) + 1, int(ph * EQUAL_CARD_SHIFT) + 1
    starts = [(int(round(fx + round((x0 - fx) / pw) * pw)), int(round(fy + round((y0 - fy) / ph) * ph)))
              for x0, y0, _x1, _y1 in boxes]
    bands = [((max(0, int(ny + 0.1 * ph)), min(H, int(ny + 0.9 * ph))), (max(0, int(nx + 0.1 * pw)), min(W, int(nx + 0.9 * pw))))
             for nx, ny in starts]

    def best_shift(cum, lo, hi, at, n, r, vertical):
        """이 카드를 크기 n으로 ±r 안에서 옮겨 볼 때 (옮길 양, 두 끝 선 중 약한 쪽 세기, 센 쪽 세기)."""
        cand = [(min(own(cum, lo, hi, at + d, vertical), own(cum, lo, hi, at + n + d, vertical)),
                 max(own(cum, lo, hi, at + d, vertical), own(cum, lo, hi, at + n + d, vertical)), -abs(d), d)
                for d in range(-r, r + 1)]
        both, one, _neg, d = max(cand)
        return d, both, one

    def size_for(pitch, cum, r, vertical, cap):
        """카드 크기: 줄끼리 조금씩 어긋나면 반복 간격(pitch)이 카드 크기와 다르다. pitch ±EQUAL_CARD_SHIFT에서
        카드마다 두 끝 선이 다 맞는 크기 (약한 쪽 끝 선 세기 합이 가장 큰 것, 같으면 pitch에 가까운 것)."""
        best_n, best_v = pitch, -1.0
        for n in sorted(range(pitch - r, (pitch if cap else pitch + r) + 1), key=lambda v: abs(v - pitch)):
            v = 0.0
            for (nx, ny), ((ya, yb), (xa, xb)) in zip(starts, bands):
                at, (lo, hi) = (nx, (ya, yb)) if vertical else (ny, (xa, xb))
                v += best_shift(cum, lo, hi, at, n, r, vertical)[1]
            if v > best_v + 1e-6:
                best_n, best_v = n, v
        return best_n

    cw = size_for(pw, cvx, sx, True, touching and trust_w)
    chh = size_for(ph, chy, sy, False, touching and trust_h)
    out = []
    for (nx, ny), ((ya, yb), (xa, xb)) in zip(starts, bands):
        # 이 카드의 끝 선이 뚜렷하면 조금 옮긴다
        d, _both, one = best_shift(cvx, ya, yb, nx, cw, sx, True)
        if one >= EQUAL_EDGE_MIN:
            nx += d
        d, _both, one = best_shift(chy, xa, xb, ny, chh, sy, False)
        if one >= EQUAL_EDGE_MIN:
            ny += d
        out.append((max(0, nx), max(0, ny), min(W, nx + cw), min(H, ny + chh)))
    return out, (cw, chh)


BOUND_OPEN_FRAC = 0.004  # 바깥 여백 찾기: 이 굵기(그림 긴 변 대비)보다 가는 선·작은 점(스캔 잡음)은 지우고 본다
BOUND_MIN_COVER = 0.05  # 내용이 가장 많은 행·열의 이만큼 이상 덮인 행·열만 카드로 본다 (여백의 세로줄·점은 아주 조금 덮는다)


def _content_bounds(mask: np.ndarray) -> tuple[int, int, int, int]:
    """내용 마스크에서 카드 배열이 있는 곳 (x0, y0, x1, y1). 여백에 있는 가는 스캔 줄·작은 점은 뺀다.

    1) 가는 선·점을 지운 마스크(열림)에서 덮인 비율이 충분한 행·열의 처음~끝을 찾고
    2) 그 끝에서 바깥쪽으로, 원래 마스크로도 충분히 덮인 행·열이 이어지면 넓힌다
       (열림으로 지워진 카드의 가는 테두리 선은 카드 너비만큼 덮으므로 다시 들어온다).
    못 찾으면 모든 내용 픽셀을 감싸는 상자."""
    import cv2

    h, w = mask.shape
    ys, xs = np.nonzero(mask)
    full = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    k = max(3, int(round(BOUND_OPEN_FRAC * max(h, w))))
    opened = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN, np.ones((k, k), np.uint8)).astype(bool)
    if not opened.any():
        opened = mask

    def span(m: np.ndarray, axis: int, lo: int, hi: int) -> tuple[int, int] | None:
        """axis=1: 행 [lo, hi) 열 안에서, axis=0: 열. 충분히 덮인 처음~끝 [s, e)."""
        sub = m[:, lo:hi] if axis == 1 else m[lo:hi, :]
        p = sub.mean(axis=axis)
        if not p.size or p.max() <= 0:
            return None
        on = np.nonzero(p >= BOUND_MIN_COVER * p.max())[0]
        return int(on[0]), int(on[-1]) + 1

    # 행 먼저(세로 잡음 줄은 행마다 몇 px뿐), 그 행 안에서 열, 그 열 안에서 행을 다시
    yr = span(opened, 1, 0, w)
    if yr is None:
        return full
    xr = span(opened, 0, *yr)
    if xr is None:
        return full
    yr = span(opened, 1, *xr) or yr
    x0, x1 = xr
    y0, y1 = yr
    # 원래 마스크로 바깥쪽 넓히기 (카드 끝의 가는 테두리 선)
    rowp = mask[:, x0:x1].mean(axis=1)
    colp = mask[y0:y1, :].mean(axis=0)
    rthr = BOUND_MIN_COVER * max(float(rowp[y0:y1].max()), 1e-6)
    cthr = BOUND_MIN_COVER * max(float(colp[x0:x1].max()), 1e-6)
    while y0 > 0 and rowp[y0 - 1] >= rthr:
        y0 -= 1
    while y1 < h and rowp[y1] >= rthr:
        y1 += 1
    while x0 > 0 and colp[x0 - 1] >= cthr:
        x0 -= 1
    while x1 < w and colp[x1] >= cthr:
        x1 += 1
    return x0, y0, x1, y1


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
    else:  # 바깥 여백은 잘라 낸다 (여백의 스캔 줄·점은 카드로 보지 않는다)
        x0, y0, x1, y1 = _content_bounds(mask)
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
        cut.junctions = True
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


MIN_BOX_PX = 8  # 이보다 작은 수동 상자는 버린다 (잘못 누른 것)


def reading_order(boxes: list) -> list[tuple[int, int, int, int]]:
    """카드 상자를 읽는 순서로: 위 줄부터, 줄 안에서는 왼쪽부터 (배열 자르기와 같은 순서 = PDF 쪽 순서).
    세로 가운데가 상자 높이(중앙값)의 절반 안이면 같은 줄로 본다."""
    bs = [tuple(int(round(v)) for v in b) for b in boxes]
    if not bs:
        return []
    hs = sorted(b[3] - b[1] for b in bs)
    tol = max(1, hs[len(hs) // 2] / 2)
    rows: list[list] = []
    for b in sorted(bs, key=lambda b: (b[1] + b[3]) / 2):
        cy = (b[1] + b[3]) / 2
        if rows and abs(cy - rows[-1][0]) <= tol:
            rows[-1][1].append(b)
        else:
            rows.append([cy, [b]])
    return [b for _cy, row in rows for b in sorted(row, key=lambda b: b[0])]


def manual_cut(path: Path | str, boxes: list, frame: int = 0, im=None) -> SheetCut:
    """사람이 그린 카드 상자(이미지 px, 회전 정보 반영한 그림 기준)로 자른다. 이미지 밖은 잘라 내고, 너무 작은 상자는 버린다.
    im: 이미 읽은 그 쪽 그림 (여러 쪽 TIFF를 쪽마다 다시 읽지 않게). 상자가 빈 목록이면 그 그림에서는 카드를 만들지 않는다."""
    im = im if im is not None else load_images(path)[frame]
    w, h = im.size
    cut = SheetCut(str(path), (w, h), [], image_dpi(path), frame=frame)
    out = []
    for b in boxes:
        x0, y0, x1, y1 = (int(round(v)) for v in b)
        x0, x1 = sorted((max(0, min(w, x0)), max(0, min(w, x1))))
        y0, y1 = sorted((max(0, min(h, y0)), max(0, min(h, y1))))
        if x1 - x0 >= MIN_BOX_PX and y1 - y0 >= MIN_BOX_PX:
            out.append((x0, y0, x1, y1))
    cut.boxes = reading_order(out)
    if len(cut.boxes) < len(boxes):
        cut.warnings.append(f"이미지 밖이거나 너무 작은 상자 {len(boxes) - len(cut.boxes)}개는 뺐습니다")
    return cut


def display_png(path: Path | str, max_px: int = 1600) -> tuple[bytes, tuple[int, int], float]:
    """수동 영역 지정 화면에 보여 줄 그림: (PNG, 원본 크기(w, h), 축소 배율). 화면 좌표 / 배율 = 원본 px."""
    import io

    im = load_images(path)[0]
    s = min(1.0, max_px / max(im.size))
    shown = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s)))) if s < 1.0 else im
    buf = io.BytesIO()
    shown.save(buf, format="PNG")
    return buf.getvalue(), im.size, s


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


def images_to_pdf(paths: list[Path | str], out_pdf: Path | str, cols: int = 1, rows: int = 1,
                  boxes: dict | None = None) -> ImagePdfResult:
    """이미지 여러 장 -> 카드 한 장이 한 쪽인 PDF. 이미지 순서대로, 한 이미지 안에서는 왼쪽 위부터 가로 먼저.
    boxes: 수동으로 영역 지정 {이미지 경로: [(x0, y0, x1, y1), …]} (있으면 cols·rows 대신 그 상자대로 자른다.
    경로는 / 와 \\ 를 가리지 않는다. 목록에 없는 이미지는 통째로 한 장, 빈 목록이면 그 이미지에서는 카드를 만들지 않는다.
    여러 쪽 TIFF는 모든 쪽에 같은 상자를 쓴다 (쪽 크기가 첫 쪽과 다르면 알린다))."""
    import io

    import pymupdf

    out_pdf = Path(out_pdf)
    doc = pymupdf.open()
    sheets, warnings = [], []
    norm = {str(Path(k)): v for k, v in (boxes or {}).items()}  # 파일 고르기 창은 C:/x/a.png, Path는 C:\\x\\a.png
    for p in paths:
        frames = load_images(p)
        for fi, im in enumerate(frames):
            if boxes is not None:
                own = norm.get(str(Path(p)))
                if own is None:
                    cut = cut_sheet(p, 1, 1, frame=fi)
                else:
                    cut = manual_cut(p, own, frame=fi, im=im)
                    if fi and im.size != frames[0].size:
                        cut.warnings.append(f"{fi + 1}쪽은 첫 쪽과 크기가 달라 그린 자리가 맞지 않을 수 있습니다")
            else:
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
