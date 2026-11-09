"""글자 잉크 재기(_glyph_ink)를 빠른 방식(TextWriter)으로 바꿔도 예전 방식(insert_text)과 같은 값이어야 한다."""

from pathlib import Path

import numpy as np
import pymupdf
import pytest

from app import typeset_ink

FONTS = sorted((Path(__file__).resolve().parent.parent / "fonts").glob("*/*.[ot]tf"))


def _old_way(font_file: str, ch: str, size: float = 200.0, dpi: int = 144):
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
    return float((base - rows[0] * s) / size), float(((rows[-1] + 1) * s - base) / size)


@pytest.mark.skipif(not FONTS, reason="fonts/ 없음")
def test_glyph_ink_same_as_insert_text_including_missing_glyphs():
    for f in FONTS[:4]:
        for ch in "한글g,.「」♥★":  # 「」♥★: 글꼴에 없을 수 있는 글자 (예전 방식으로 잰다)
            assert typeset_ink._glyph_ink.__wrapped__(str(f), ch) == _old_way(str(f), ch), (f.name, ch)
