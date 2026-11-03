"""잉크 높이 측정 (PRD 5.7 F28 크기 보정).

같은 pt라도 글꼴마다 실제 글자 높이가 다르므로, 한글 시작 크기를 pt가 아니라
원문의 실제 글자 높이(잉크 높이)에 맞춘다.

- ``measure_source_ink``: 원문 영역을 렌더링해 줄(잉크 띠)과 대표 글자 높이를 잰다.
  대표 높이 = 줄마다 '글자 덩어리'(빈 세로줄로 나뉜 조각, 폭이 줄 높이의 25% 이상)
  높이의 75번째 백분위 -> 줄들의 중앙값. 작은 가나·문장 부호·탁점에 덜 흔들리고
  한자·가나 몸통 높이를 대표한다.
- ``font_ink_ratio``: 한글 글꼴로 대표 글자(기본 '한','가','을')를 한 자씩 그려
  잉크 높이를 재고, 평균 / 글자 크기(pt) = 보정 배율(잉크 높이/pt)을 돌려준다.
- ``start_size_for_ink``: 원문 대표 높이 / 보정 배율 = 그 글꼴의 시작 크기(pt).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
import pymupdf

DEFAULT_KO_SAMPLE = "한가을"


@dataclass
class SourceInk:
    bbox: tuple[float, float, float, float] | None  # 잉크 전체 bbox (pt)
    lines: list[tuple[float, float]] = field(default_factory=list)  # 줄별 (위, 아래) pt
    glyph_height: float | None = None  # 대표 글자 높이 (pt)
    line_pitch: float | None = None  # 줄 간격 (위끝 기준, pt)
    line_x: list[tuple[float, float]] = field(default_factory=list)  # 줄별 (왼, 오른) pt

    def to_dict(self) -> dict:
        r = lambda v: None if v is None else round(float(v), 3)  # noqa: E731
        return {
            "bbox_pt": None if self.bbox is None else [r(v) for v in self.bbox],
            "lines_pt": [[r(a), r(b)] for a, b in self.lines],
            "line_x_pt": [[r(a), r(b)] for a, b in self.line_x],
            "glyph_height_pt": r(self.glyph_height),
            "line_pitch_pt": r(self.line_pitch),
        }


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def ink_mask(page: pymupdf.Page, rect, *, polarity: str = "dark", color: str | None = None,
             dpi: int = 1200, tol: float = 70.0, dark_max: int = 110, light_min: int = 205):
    """영역을 렌더링해 글자 잉크 마스크(bool 2차원 배열)를 만든다.

    polarity: "dark"(어두운 글자) | "light"(밝은 글자, 예: 색 라벨 위 흰 글자) | "color"(color에 가까운 픽셀)
    """
    rect = pymupdf.Rect(rect) & page.rect
    if polarity == "color":
        pix = page.get_pixmap(dpi=dpi, clip=rect, colorspace=pymupdf.csRGB, alpha=False)
        a = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, 3).astype(np.int32)
        ref = np.array(_hex_to_rgb(color or "#000000"))
        mask = np.sqrt(((a - ref) ** 2).sum(-1)) < tol
    else:
        pix = page.get_pixmap(dpi=dpi, clip=rect, colorspace=pymupdf.csGRAY, alpha=False)
        a = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w)
        mask = a < dark_max if polarity == "dark" else a > light_min
    return mask, rect, 72.0 / dpi


def _bands(rows: np.ndarray, min_frac: float = 0.02, gap: int = 2) -> list[tuple[int, int]]:
    if rows.max() == 0:
        return []
    on = rows >= max(1, min_frac * rows.max())
    bands: list[list[int]] = []
    for i in np.where(on)[0]:
        if bands and i - bands[-1][1] <= gap + 1:
            bands[-1][1] = i
        else:
            bands.append([i, i])
    if not bands:
        return []
    hmax = max(b - a + 1 for a, b in bands)
    return [(a, b) for a, b in bands if (b - a + 1) >= 0.3 * hmax]


def _glyph_segments(m: np.ndarray) -> list[tuple[int, int]]:
    """빈 세로줄로 나뉜 조각들의 (폭, 높이) px."""
    cols = m.any(0)
    out = []
    n = len(cols)
    i = 0
    while i < n:
        if cols[i]:
            j = i
            while j < n and cols[j]:
                j += 1
            r = np.where(m[:, i:j].any(1))[0]
            out.append((j - i, int(r[-1] - r[0] + 1)))
            i = j
        else:
            i += 1
    return out


def representative_height_px(m: np.ndarray, pct: float = 75.0, min_w_ratio: float = 0.25) -> float | None:
    rows = np.where(m.any(1))[0]
    if len(rows) == 0:
        return None
    bh = rows[-1] - rows[0] + 1
    hs = [h for w, h in _glyph_segments(m) if w >= min_w_ratio * bh]
    if not hs:
        return float(bh)
    return float(np.percentile(hs, pct))


def measure_source_ink(page: pymupdf.Page, rect, *, polarity: str = "dark", color: str | None = None,
                       dpi: int = 1200, **mask_kw) -> SourceInk:
    """원문 영역의 잉크 줄과 대표 글자 높이를 잰다 (좌표는 pt, page 좌표계)."""
    mask, r, s = ink_mask(page, rect, polarity=polarity, color=color, dpi=dpi, **mask_kw)
    if not mask.any():
        return SourceInk(None)
    bands = _bands(mask.sum(1))
    lines, line_x, heights = [], [], []
    for a, b in bands:
        sub = mask[a:b + 1]
        cols = np.where(sub.any(0))[0]
        lines.append((r.y0 + a * s, r.y0 + (b + 1) * s))
        line_x.append((r.x0 + cols[0] * s, r.x0 + (cols[-1] + 1) * s))
        h = representative_height_px(sub)
        if h:
            heights.append(h * s)
    ys, xs = np.where(mask)
    bbox = (r.x0 + xs.min() * s, r.y0 + ys.min() * s, r.x0 + (xs.max() + 1) * s, r.y0 + (ys.max() + 1) * s)
    pitch = None
    if len(lines) >= 2:
        pitch = float(np.median([lines[i + 1][0] - lines[i][0] for i in range(len(lines) - 1)]))
    return SourceInk(bbox, lines, float(np.median(heights)) if heights else None, pitch, line_x)


@lru_cache(maxsize=256)
def _glyph_ink(font_file: str, ch: str, size: float = 200.0, dpi: int = 144) -> tuple[float, float] | None:
    """한 글자를 그려 (기준선 위 잉크 높이, 기준선 아래 깊이)를 1pt 기준으로 돌려준다."""
    doc = pymupdf.open()
    page = doc.new_page(width=size * 2, height=size * 2)
    page.insert_font(fontname="K", fontfile=font_file)
    base = size * 1.4
    page.insert_text((size * 0.5, base), ch, fontname="K", fontsize=size)
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
    a = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w)
    rows = np.where((a < 128).any(1))[0]
    doc.close()
    if len(rows) == 0:
        return None
    s = 72.0 / dpi
    top = base - rows[0] * s
    bottom = (rows[-1] + 1) * s - base
    return float(top / size), float(bottom / size)


def font_ink_ratio(font_file: str | Path, sample: str = DEFAULT_KO_SAMPLE) -> float:
    """보정 배율 = 대표 한글 글자의 평균 잉크 높이 / 글자 크기(pt)."""
    hs = []
    for ch in sample:
        g = _glyph_ink(str(font_file), ch)
        if g:
            hs.append(g[0] + g[1])
    if not hs:
        raise ValueError(f"{font_file}: 대표 글자 {sample!r}를 그릴 수 없음")
    return float(np.mean(hs))


def start_size_for_ink(source_glyph_height_pt: float, font_file: str | Path,
                       sample: str = DEFAULT_KO_SAMPLE) -> float:
    return source_glyph_height_pt / font_ink_ratio(font_file, sample)
