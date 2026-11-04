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


def cut_sheet(path: Path | str, cols: int = 1, rows: int = 1, frame: int = 0) -> SheetCut:
    """이미지 한 장(frame: 여러 쪽 TIFF의 몇 번째)을 cols×rows 카드로 자른다."""
    im = load_images(path)[frame]
    rgb = np.asarray(im)
    h, w = rgb.shape[:2]
    cut = SheetCut(str(path), (w, h), [], image_dpi(path))
    if cols < 1 or rows < 1:
        raise ValueError("가로·세로 장수는 1 이상")
    if cols == 1 and rows == 1:
        cut.boxes = [(0, 0, w, h)]
        return cut
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
    if not cut.gutters_found:
        cut.warnings.append("카드 사이 틈을 찾지 못한 곳은 같은 간격으로 나눴습니다 (미리 보기로 확인하세요)")
    # 칸 경계 = 바깥 여백 끝과 틈의 시작·끝 (배열 전체에서 공통): 같은 열·행의 카드는 같은 크기로 잘린다.
    # 칸마다 그림이 있는 부분만 남기면 가장자리가 흰 카드(바탕과 비슷)가 들쭉날쭉 작게 잘린다.
    xs_ = [x0] + [v for g in xc for v in g] + [x1]
    ys_ = [y0] + [v for g in yc for v in g] + [y1]
    for r in range(rows):
        for c in range(cols):
            cut.boxes.append((xs_[2 * c], ys_[2 * r], xs_[2 * c + 1], ys_[2 * r + 1]))
    sizes = [(b[2] - b[0], b[3] - b[1]) for b in cut.boxes]
    mw, mh = np.median([s[0] for s in sizes]), np.median([s[1] for s in sizes])
    odd = [i + 1 for i, (sw, sh) in enumerate(sizes) if abs(sw - mw) > mw * 0.08 or abs(sh - mh) > mh * 0.08]
    if odd:
        cut.warnings.append(f"크기가 다른 카드가 있습니다: {', '.join(map(str, odd))}번째 (빈 칸이거나 잘못 잘렸을 수 있음)")
    return cut


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
