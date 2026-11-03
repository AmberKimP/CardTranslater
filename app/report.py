"""원본 | 결과 비교 이미지 (visual-qa 검사용)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

SEP = 12
SEP_COLOR = (255, 0, 255)


def _label(img: Image.Image, text: str) -> Image.Image:
    bar = Image.new("RGB", (img.width, 18), (30, 30, 30))
    ImageDraw.Draw(bar).text((4, 3), text, fill=(255, 255, 255))
    out = Image.new("RGB", (img.width, img.height + bar.height), (0, 0, 0))
    out.paste(bar, (0, 0))
    out.paste(img, (0, bar.height))
    return out


def side_by_side(left: np.ndarray, right: np.ndarray, titles=("ORIGINAL", "RESULT")) -> Image.Image:
    a = _label(Image.fromarray(left), titles[0])
    b = _label(Image.fromarray(right), titles[1])
    out = Image.new("RGB", (a.width + SEP + b.width, max(a.height, b.height)), SEP_COLOR)
    out.paste(a, (0, 0))
    out.paste(b, (a.width + SEP, 0))
    return out


def stacked(rows: list[tuple[str, np.ndarray]], scale: int = 2) -> Image.Image:
    ims = []
    for title, arr in rows:
        im = Image.fromarray(arr)
        if scale != 1:
            im = im.resize((im.width * scale, im.height * scale), Image.NEAREST)
        ims.append(_label(im, title))
    W = max(i.width for i in ims)
    H = sum(i.height for i in ims) + SEP * (len(ims) - 1)
    out = Image.new("RGB", (W, H), SEP_COLOR)
    y = 0
    for im in ims:
        out.paste(im, (0, y))
        y += im.height + SEP
    return out


def mask_overlay(base: np.ndarray, flat: np.ndarray, inpaint: np.ndarray) -> np.ndarray:
    """정리한 픽셀 표시: 추정 배경 채움 = 빨강, 인페인팅 = 파랑."""
    out = base.astype(np.float32).copy()
    out[flat] = out[flat] * 0.45 + np.array([255, 0, 0]) * 0.55
    out[inpaint] = out[inpaint] * 0.45 + np.array([0, 80, 255]) * 0.55
    return np.clip(out, 0, 255).astype(np.uint8)


def save(img: Image.Image, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path
