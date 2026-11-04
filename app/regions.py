"""영역 좌표 변환: 비율(0~1) <-> PDF pt <-> 이미지 px.

좌표 변환 함수는 이 파일 한 곳에만 둔다 (에이전트 규칙 2).
모든 사각형은 (x0, y0, x1, y1) 튜플이며, pt는 PyMuPDF의 page.rect 좌표계
(왼쪽 위 원점, CropBox 기준)를 따른다. 이 모듈은 PyMuPDF에 의존하지 않는다.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class RatioRect:
    """카드 너비·높이 대비 비율 사각형."""

    x: float
    y: float
    w: float
    h: float

    @classmethod
    def from_dict(cls, d: dict) -> "RatioRect":
        r = cls(float(d["x"]), float(d["y"]), float(d["w"]), float(d["h"]))
        if r.w <= 0 or r.h <= 0:
            raise ValueError(f"영역 크기가 0 이하: {d}")
        return r

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}


@dataclass
class Region:
    name: str
    rect: RatioRect
    kind: str = "text"  # "text" | "tag_pills"
    style: dict = field(default_factory=dict)
    notes: str = ""
    erase: dict = field(default_factory=dict)  # 지우기 옵션 (예: keep_ink_taller_than_pt)
    extract: dict = field(default_factory=dict)  # 추출 옵션 (예: tag_colors 라벨 색 -> 글자 사전)


@dataclass
class RegionTemplate:
    regions: list[Region]
    protect: list[Region]  # 절대 건드리지 않는 영역
    source_language: str = ""
    reference_page: int = 1


def load_template(path: Path) -> RegionTemplate:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    regions = [
        Region(
            name=r["name"],
            rect=RatioRect.from_dict(r["rect"]),
            kind=r.get("kind", "text"),
            style=r.get("style", {}),
            notes=r.get("notes", ""),
            erase=r.get("erase", {}),
            extract=r.get("extract", {}),
        )
        for r in data["regions"]
    ]
    protect = [
        Region(name=p["name"], rect=RatioRect.from_dict(p["rect"]), kind="protect")
        for p in data.get("protect_rects", [])
    ]
    return RegionTemplate(
        regions=regions,
        protect=protect,
        source_language=data.get("source_language", ""),
        reference_page=int(data.get("reference_page", 1)),
    )


# ---------------------------------------------------------------- 변환

def ratio_to_pt(r: RatioRect, page_rect: Box) -> Box:
    """비율 사각형 -> 페이지 pt 사각형."""
    px0, py0, px1, py1 = page_rect
    W, H = px1 - px0, py1 - py0
    return (px0 + r.x * W, py0 + r.y * H, px0 + (r.x + r.w) * W, py0 + (r.y + r.h) * H)


def pt_to_ratio(b: Box, page_rect: Box) -> RatioRect:
    px0, py0, px1, py1 = page_rect
    W, H = px1 - px0, py1 - py0
    return RatioRect((b[0] - px0) / W, (b[1] - py0) / H, (b[2] - b[0]) / W, (b[3] - b[1]) / H)


def image_scale(image_bbox: Box, image_size: tuple[int, int]) -> tuple[float, float]:
    """이미지 bbox(pt)와 픽셀 크기(w, h)로 px/pt 배율을 구한다."""
    w, h = image_size
    return w / (image_bbox[2] - image_bbox[0]), h / (image_bbox[3] - image_bbox[1])


def pt_to_px(b: Box, image_bbox: Box, image_size: tuple[int, int]) -> Box:
    """pt 사각형 -> 이미지 픽셀 좌표(실수). 이미지가 회전·반전 없이 놓였다고 가정."""
    sx, sy = image_scale(image_bbox, image_size)
    return (
        (b[0] - image_bbox[0]) * sx,
        (b[1] - image_bbox[1]) * sy,
        (b[2] - image_bbox[0]) * sx,
        (b[3] - image_bbox[1]) * sy,
    )


def px_to_pt(b: Box, image_bbox: Box, image_size: tuple[int, int]) -> Box:
    sx, sy = image_scale(image_bbox, image_size)
    return (
        image_bbox[0] + b[0] / sx,
        image_bbox[1] + b[1] / sy,
        image_bbox[0] + b[2] / sx,
        image_bbox[1] + b[3] / sy,
    )


def px_box_outer(b: Box, size: tuple[int, int] | None = None) -> tuple[int, int, int, int]:
    """실수 px 사각형을 그 사각형을 모두 덮는 정수 픽셀 범위(x0, y0, x1, y1; x1·y1 미포함)로."""
    eps = 1e-6
    x0, y0 = math.floor(b[0] + eps), math.floor(b[1] + eps)
    x1, y1 = math.ceil(b[2] - eps), math.ceil(b[3] - eps)
    if size is not None:
        w, h = size
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(w, x1), min(h, y1)
    return x0, y0, x1, y1


# ---------------------------------------------------------------- 사각형 도우미

def area(b: Box) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def intersect(a: Box, b: Box) -> Box:
    return (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))


def union(boxes: Iterable[Box]) -> Box:
    bs = list(boxes)
    if not bs:
        raise ValueError("빈 목록")
    return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))


def overlap_ratio(inner: Box, outer: Box) -> float:
    """inner 넓이 중 outer와 겹치는 비율 (0~1)."""
    a = area(inner)
    return area(intersect(inner, outer)) / a if a > 0 else 0.0


def expand(b: Box, dx: float, dy: float | None = None) -> Box:
    dy = dx if dy is None else dy
    return (b[0] - dx, b[1] - dy, b[2] + dx, b[3] + dy)


def clip_horizontally(b: Box, protect: Iterable[Box]) -> Box:
    """b가 보호 사각형과 겹치면 겹치지 않는 쪽으로 좌우를 잘라낸다.

    보호 영역은 카드 왼쪽·오른쪽 띠(세로로 긴 사각형)를 가정한다.
    """
    x0, y0, x1, y1 = b
    for p in protect:
        if area(intersect((x0, y0, x1, y1), p)) <= 0:
            continue
        cx = (x0 + x1) / 2
        if p[0] <= x0 < p[2] and p[2] < x1:
            x0 = p[2]
        elif p[0] < x1 <= p[2] and x0 < p[0]:
            x1 = p[0]
        elif p[0] > cx:
            x1 = min(x1, p[0])
        else:
            x0 = max(x0, p[2])
    return (x0, y0, x1, y1)
