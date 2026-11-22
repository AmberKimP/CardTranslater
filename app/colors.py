"""색 표기 바꾸기 (지우기·한글 넣기가 같이 쓴다)."""

from __future__ import annotations


def hex_to_rgb01(color: str) -> tuple[float, float, float]:
    """'#rrggbb' -> (r, g, b) 0~1 (PyMuPDF가 쓰는 색)."""
    c = color.lstrip("#")
    return tuple(int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]
