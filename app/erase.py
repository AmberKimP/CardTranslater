"""글자 지우기 (PRD 5.4, ClearScan 경로).

쪽마다 다음 순서로 처리한다.
  1. 원본 모양(텍스트 레이어 + 배경 이미지)을 배경 이미지 픽셀 격자에 맞춰 렌더링하고,
     태그 라벨(둥근 사각형)의 위치·크기·모서리 반경·색을 잰다.
  2. 영역과 겹치는 텍스트 객체를 레드랙션한다 (채우기 없음, 이미지·벡터 도형 유지).
     영역 안의 작은 인라인 이미지(ClearScan이 '!' 점 등을 그린 것)도 지운다.
  3. 배경 이미지의 영역 안을 정리한다: 영역 밖 픽셀로 배경(색·그라데이션)을 추정하고,
     배경과 다른 픽셀 + 텍스트 레이어 글자 bbox + 라벨 bbox를 마스크로 잡아 넓힌 뒤,
     고른 배경은 추정 배경 + 종이 결 잡음으로, 질감이 있는 곳은 OpenCV 인페인팅으로 채운다.
  4. 원본 배경 이미지 스트림은 그대로 두고, 정리한 영역만 무손실 패치 이미지로 덮는다(픽셀 격자 맞춤).
  5. 잰 라벨을 같은 모양의 벡터 둥근 사각형으로 다시 그린다 (글자 없음).

이미지 처리 함수(라벨 찾기, 반경 맞추기, 배경 추정, 정리)는 numpy 배열만 다루는 순수 함수다.
"""

from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass, field

import cv2
import numpy as np

from app.colors import hex_to_rgb01  # noqa: F401 - 예전처럼 app.erase.hex_to_rgb01로도 쓴다
from app.regions import (
    Box,
    Region,
    RegionTemplate,
    area,
    clip_horizontally,
    expand,
    intersect,
    overlap_ratio,
    pt_to_px,
    px_box_outer,
    px_to_pt,
    ratio_to_pt,
    text_only,
    union,
)

from app.erase_fill import (  # noqa: F401 - 나눈 모듈의 이름도 예전처럼 app.erase.이름으로 쓴다
    CleanParams, CleanResult, _deviation, _disk, clean_area, dark_ink, estimate_background, fill_background,
    fill_box, luminance, non_paper_mask, normalized_blur, pushpull_fill, smooth_background, tall_ink_mask,
    text_ink_box, transfer_texture,
)
from app.erase_labels import (  # noqa: F401 - 나눈 모듈의 이름도 예전처럼 app.erase.이름으로 쓴다
    LABEL_MAX_ASPECT, LabelPx, _keep_largest, _labels_from_mask, _overlap_small, find_tag_labels,
    fit_corner_radius, label_color_candidates, label_like_mask,
)
from app.erase_strokes import (  # noqa: F401 - 나눈 모듈의 이름도 예전처럼 app.erase.이름으로 쓴다
    PICTURE_BG_WARNING, PICTURE_COMP_MAX, PICTURE_COVER_MIN, PICTURE_EDGE_INSIDE, PICTURE_FULL_RECHECK,
    PICTURE_GLYPH_MAX, PICTURE_GROW_PX, PICTURE_HALO_DIFF, PICTURE_HALO_MAX, PICTURE_HALO_STEP,
    PICTURE_ICON_MAX, PICTURE_KERNEL, PICTURE_MIN_INK, PICTURE_RUN_MIN_W, PICTURE_SIGMA, PICTURE_STRAY_MAX,
    PICTURE_STROKE_LOW, PICTURE_STROKE_MIN, PICTURE_UNNARROW, _box_area, _hat_strokes, _stroke_lines,
    fill_picture, grow_halo, picture_text_mask, prefer_picture_mask,
)

SPAN_ASSIGN_MIN = 0.30  # span 넓이 중 이 비율 이상이 영역에 들어오면 그 영역의 글자로 보고 지운다
SPAN_PARTIAL_WARN = 0.95  # 이보다 적게 들어오면 '영역에 일부만 걸침' 경고
TAG_SEARCH_PAD = 0.25  # 라벨 찾을 때 태그 영역 높이 대비 위아래 여유
TAG_MIN_H = 0.60  # 라벨 최소 높이 (태그 영역 높이 대비). 실측 라벨 ≈0.72, 17쪽 연기 조각 0.50
TAG_MAX_H = 1.0  # 라벨 최대 높이. 14·28쪽 연기 덩어리 ≈1.1
IMAGE_TEXT_PAD_PT = 1.2  # 이미지에 박힌 글자: 잉크 상자 둘레 여유 (번짐·그림자)
MAX_BG_ROTATION_DEG = 5.0  # 이보다 많이 돌아간 배경 이미지는 지원하지 않음
PATCH_INSET_PT = 0.002  # 패치 사각형을 안쪽으로 넣는 양 (grid-fit 바깥 번짐 방지)


# =====================================================================
# 순수 함수: 색·라벨
# =====================================================================

def rgb_to_hex(rgb) -> str:
    r, g, b = (int(round(float(v))) for v in rgb[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


def int_color_to_hex(c: int) -> str:
    return f"#{c & 0xFFFFFF:06x}"


# =====================================================================
# PDF 쪽 처리
# =====================================================================

@dataclass
class BackgroundImage:
    xref: int
    bbox: Box  # pt (page.rect 좌표). 회전된 이미지면 네 모서리를 감싸는 사각형
    size: tuple[int, int]  # px (w, h)
    # 이미지 단위 사각형 -> 페이지 좌표(pt, 위가 0) 행렬 (a, b, c, d, e, f).
    # 단위 (s, t)의 t는 위에서부터. 없으면 bbox에 축 정렬로 놓인 것으로 본다.
    transform: tuple | None = None

    @property
    def rotated(self) -> bool:
        t = self.transform
        return t is not None and (abs(t[1]) > 1e-6 or abs(t[2]) > 1e-6)

    @property
    def angle_deg(self) -> float:
        t = self.transform
        return 0.0 if t is None else math.degrees(math.atan2(t[1], t[0]))

    @property
    def dpi(self) -> tuple[float, float]:
        if self.rotated:
            a, b, c, d = self.transform[:4]
            return self.size[0] / math.hypot(a, b) * 72.0, self.size[1] / math.hypot(c, d) * 72.0
        return (
            self.size[0] / (self.bbox[2] - self.bbox[0]) * 72.0,
            self.size[1] / (self.bbox[3] - self.bbox[1]) * 72.0,
        )

    @property
    def pt_per_px(self) -> float:
        return 72.0 / self.dpi[0]

    def px_point_to_pt(self, u: float, v: float) -> tuple[float, float]:
        a, b, c, d, e, f = self.transform
        s, t = u / self.size[0], v / self.size[1]
        return s * a + t * c + e, s * b + t * d + f

    def pt_point_to_px(self, x: float, y: float) -> tuple[float, float]:
        a, b, c, d, e, f = self.transform
        det = a * d - b * c
        x, y = x - e, y - f
        s, t = (x * d - y * c) / det, (y * a - x * b) / det
        return s * self.size[0], t * self.size[1]

    def pt_to_px(self, b: Box) -> Box:
        """pt 사각형 -> px. 회전된 이미지면 네 모서리를 옮겨 감싸는 사각형."""
        if not self.rotated:
            return pt_to_px(b, self.bbox, self.size)
        pts = [self.pt_point_to_px(x, y) for x in (b[0], b[2]) for y in (b[1], b[3])]
        return (min(q[0] for q in pts), min(q[1] for q in pts), max(q[0] for q in pts), max(q[1] for q in pts))

    def px_to_pt(self, b: Box) -> Box:
        if not self.rotated:
            return px_to_pt(b, self.bbox, self.size)
        pts = [self.px_point_to_pt(u, v) for u in (b[0], b[2]) for v in (b[1], b[3])]
        return (min(q[0] for q in pts), min(q[1] for q in pts), max(q[0] for q in pts), max(q[1] for q in pts))


NO_BACKGROUND_MSG = "배경 이미지를 찾지 못했습니다 (순수 벡터 페이지는 아직 지원하지 않음)"


def find_background_image_or_none(page) -> BackgroundImage | None:
    """find_background_image와 같되, 쪽을 덮는 배경 이미지가 없으면(벡터 인쇄 PDF) None."""
    try:
        return find_background_image(page)
    except RuntimeError as e:
        if str(e) != NO_BACKGROUND_MSG:
            raise
        return None


def find_background_image(page) -> BackgroundImage:
    """페이지 대부분을 덮는 가장 큰 이미지(xref 있는 것)."""
    prect = tuple(page.rect)
    best = None
    for info in page.get_image_info(xrefs=True):
        xref = info.get("xref", 0)
        bbox = tuple(info["bbox"])
        if xref <= 0:
            continue
        cover = area(intersect(bbox, prect)) / area(prect)
        if cover < 0.9:
            continue
        a, b, c, d, _, _ = info["transform"]
        # Acrobat이 기울어진 스캔을 바로 세우려고 이미지를 조금 돌려 놓은 경우(1~2도)는 지원한다.
        if a <= 0 or d <= 0 or a * d - b * c <= 0 or abs(math.degrees(math.atan2(b, a))) > MAX_BG_ROTATION_DEG:
            raise NotImplementedError(f"크게 회전·반전된 배경 이미지는 아직 지원하지 않습니다: {info['transform']}")
        if best is None or cover > best[0]:
            best = (cover, BackgroundImage(xref, bbox, (info["width"], info["height"]), tuple(info["transform"])))
    if best is None:
        raise RuntimeError(NO_BACKGROUND_MSG)
    return best[1]


def load_image_rgb(doc, xref: int) -> np.ndarray:
    import pymupdf

    pix = pymupdf.Pixmap(doc, xref)
    if pix.alpha or pix.colorspace is None or pix.colorspace.n != 3:
        raise NotImplementedError(
            f"RGB가 아닌 배경 이미지(cs={pix.colorspace}, alpha={pix.alpha})는 아직 지원하지 않습니다 (CMYK 보존 규칙)"
        )
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n).copy()


def render_on_image_grid(page, bg: BackgroundImage, base: np.ndarray) -> tuple[np.ndarray, list[str]]:
    """페이지 전체 모양(텍스트 포함)을 배경 이미지 픽셀 격자에 맞춰 렌더링한다.

    페이지 밖으로 나간 이미지 픽셀은 base(원래 이미지) 값을 쓴다.
    """
    import pymupdf

    if bg.rotated:
        return _render_on_rotated_grid(page, bg, base)
    warnings = []
    sx = bg.size[0] / (bg.bbox[2] - bg.bbox[0])
    sy = bg.size[1] / (bg.bbox[3] - bg.bbox[1])
    pix = page.get_pixmap(matrix=pymupdf.Matrix(sx, sy), alpha=False)
    R = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[..., :3]
    ox = (bg.bbox[0] - page.rect.x0) * sx - pix.x
    oy = (bg.bbox[1] - page.rect.y0) * sy - pix.y
    if abs(ox - round(ox)) > 0.1 or abs(oy - round(oy)) > 0.1:
        warnings.append(f"렌더링과 이미지 격자가 정수 픽셀로 맞지 않음 (ox={ox:.2f}, oy={oy:.2f})")
    ox, oy = int(round(ox)), int(round(oy))
    out = base.copy()
    H, W = base.shape[:2]
    # 이미지 (u, v) -> 렌더 (u+ox, v+oy)
    u0, v0 = max(0, -ox), max(0, -oy)
    u1, v1 = min(W, R.shape[1] - ox), min(H, R.shape[0] - oy)
    out[v0:v1, u0:u1] = R[v0 + oy : v1 + oy, u0 + ox : u1 + ox]
    return out, warnings


def _render_on_rotated_grid(page, bg: BackgroundImage, base: np.ndarray) -> tuple[np.ndarray, list[str]]:
    """회전된 배경 이미지: 페이지를 2배 해상도로 렌더링한 뒤 이미지 격자로 되돌려 샘플링한다.

    정수 격자로 딱 맞출 수 없으므로 배경까지 한 번 다시 샘플링된다. 렌더 - 원본 차이로 글자를
    찾을 때 경계에 생기는 작은 차이는 span 상자 안으로만 쓰이므로 괜찮다.
    """
    import pymupdf

    k = 2.0 * bg.dpi[0] / 72.0
    pix = page.get_pixmap(matrix=pymupdf.Matrix(k, k), alpha=False)
    R = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[..., :3]
    a, b, c, d, e, f = bg.transform
    W, H = bg.size
    x0, y0 = page.rect.x0, page.rect.y0
    # 이미지 픽셀 중심 (u+.5, v+.5) -> 페이지 pt -> 렌더 픽셀 좌표
    m = np.array([
        [k * a / W, k * c / H, 0.0],
        [k * b / W, k * d / H, 0.0],
    ])
    m[0, 2] = k * (0.5 * a / W + 0.5 * c / H + e - x0) - 0.5
    m[1, 2] = k * (0.5 * b / W + 0.5 * d / H + f - y0) - 0.5
    warped = cv2.warpAffine(np.ascontiguousarray(R), m, (W, H), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                            borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))
    inside = cv2.warpAffine(np.full(R.shape[:2], 255, np.uint8), m, (W, H),
                            flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP, borderValue=0) > 0
    out = base.copy()
    out[inside] = warped[inside]
    return out, [f"배경 이미지가 {bg.angle_deg:.2f}도 회전되어 있어 다시 샘플링해 비교함"]


def page_spans(page) -> list[dict]:
    spans = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for line in b["lines"]:
            for s in line["spans"]:
                if not s["text"].strip():
                    continue
                spans.append(
                    {
                        "bbox": tuple(round(v, 3) for v in s["bbox"]),
                        "size": round(float(s["size"]), 3),
                        "color": int_color_to_hex(s["color"]),
                        "font": s["font"],
                        "chars": len(s["text"]),
                        "origin": tuple(round(v, 3) for v in s["origin"]),
                    }
                )
    return spans


_INLINE_RE = re.compile(
    rb"q\s+([-+\d.]+)\s+([-+\d.]+)\s+([-+\d.]+)\s+([-+\d.]+)\s+([-+\d.]+)\s+([-+\d.]+)\s+cm\s+"
    rb"(?:(?:[-+\d.]+\s+){1,4}(?:rg|RG|g|G|k|K|sc|SC|scn|SCN)\s+|/[^\s/]+\s+gs\s+)*"  # 색·그래픽 상태
    rb"BI\b.*?\bID\b.*?\bEI\s+Q",
    re.S,
)


def remove_inline_images(page, rects: list[Box]) -> list[Box]:
    """content stream의 `q <cm> BI … EI Q` 인라인 이미지 중 중심이 rects 안인 것을 지운다.

    지운 이미지의 bbox(pt) 목록을 돌려준다. 내용 스트림이 여러 개면 손대지 않는다.
    """
    import pymupdf

    xrefs = page.get_contents()
    if len(xrefs) != 1:
        return []
    doc = page.parent
    data = doc.xref_stream(xrefs[0])
    removed: list[Box] = []
    tm = page.transformation_matrix

    def repl(m: re.Match) -> bytes:
        a, b, c, d, e, f = (float(v) for v in m.groups())
        r = pymupdf.Rect(e, f, e + a, f + d) * tm
        r.normalize()
        cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
        if any(x0 <= cx <= x1 and y0 <= cy <= y1 for x0, y0, x1, y1 in rects) and r.width < 20 and r.height < 20:
            removed.append(tuple(r))
            return b""
        return m.group(0)

    new = _INLINE_RE.sub(repl, data)
    if removed:
        doc.update_stream(xrefs[0], new)
    return removed


def _dominant(spans: list[dict], key: str):
    tally: dict = {}
    for s in spans:
        tally[s[key]] = tally.get(s[key], 0) + s["chars"]
    return max(tally.items(), key=lambda kv: kv[1])[0] if tally else None


def _r(b, nd=3):
    return [round(float(v), nd) for v in b]


@dataclass
class PageErase:
    meta: dict
    before: np.ndarray  # 원래 모양 (이미지 격자)
    clean: CleanResult | None
    crop_px: tuple[int, int, int, int]
    region_px: dict  # 영역 이름 -> 작업 bbox (px, 정수)
    warnings: list[str]
    cleaned: np.ndarray | None = None  # 패치를 반영한 배경 이미지 (확인용, PDF에는 패치만 들어감)


def patch_rect_pt(box_px: tuple[int, int, int, int], bg: BackgroundImage) -> Box:
    """정수 픽셀 범위 -> pt 사각형 (격자에 정확히 맞음; 회전된 배경이면 감싸는 사각형)."""
    return bg.px_to_pt(tuple(float(v) for v in box_px))  # type: ignore[arg-type]


def patch_matrix_pdf(page, box_px: tuple[int, int, int, int], bg: BackgroundImage):
    """회전된 배경 위 패치의 PDF cm 행렬 (패치 단위 사각형 -> PDF 사용자 좌표).

    배경 이미지와 같은 행렬에서 원점·크기만 픽셀 범위로 옮긴다 -> 배경 픽셀 격자와 정확히 겹친다.
    """
    import pymupdf

    W, H = bg.size
    u0, v0, u1, v1 = box_px
    T = pymupdf.Matrix(*bg.transform)  # (s, t 위에서부터) -> 페이지(위가 0)
    sub = pymupdf.Matrix((u1 - u0) / W, 0, 0, (v1 - v0) / H, u0 / W, v0 / H)
    flip = pymupdf.Matrix(1, 0, 0, -1, 0, 1)  # PDF 이미지 공간은 t가 아래에서부터
    return flip * sub * T * ~page.transformation_matrix


def _set_image_cm(page, xref: int, mat) -> None:
    """insert_image가 쓴 `q … cm /이름 Do Q`의 cm을 mat으로 바꾼다."""
    doc = page.parent
    name = next(img[7] for img in page.get_images(full=True) if img[0] == xref)
    pat = re.compile(rb"((?:[-+\d.eE]+\s+){6})cm(\s*/" + re.escape(name.encode()) + rb"\s+Do)")
    new_cm = " ".join(f"{v:.6f}" for v in (mat.a, mat.b, mat.c, mat.d, mat.e, mat.f)).encode() + b" "
    for cx in page.get_contents():
        data = doc.xref_stream(cx)
        new, n = pat.subn(lambda m: new_cm + b"cm" + m.group(2), data)
        if n:
            if n != 1:
                raise RuntimeError(f"패치 이미지 {name}이(가) 내용 스트림에 {n}번 나옴")
            doc.update_stream(cx, new)
            return
    raise RuntimeError(f"패치 이미지 {name}의 cm을 찾지 못함")


def place_patch(page, pixels: np.ndarray, box_px: tuple[int, int, int, int], bg: BackgroundImage) -> dict:
    """정리한 픽셀 조각을 무손실(Flate) 이미지로 배경 위에 그린다."""
    import pymupdf

    h, w = pixels.shape[:2]
    if (box_px[2] - box_px[0], box_px[3] - box_px[1]) != (w, h):
        raise ValueError("패치 크기와 픽셀 범위가 다릅니다")
    pix = pymupdf.Pixmap(pymupdf.csRGB, w, h, np.ascontiguousarray(pixels).tobytes(), 0)
    rect = patch_rect_pt(box_px, bg)
    if bg.rotated:
        xref = page.insert_image(pymupdf.Rect(rect), pixmap=pix, overlay=True, keep_proportion=False)
        _set_image_cm(page, xref, patch_matrix_pdf(page, box_px, bg))
        kind, val = page.parent.xref_get_key(bg.xref, "ColorSpace")
        page.parent.xref_set_key(xref, "ColorSpace", val if kind in ("name", "xref") else "/DeviceRGB")
        return {"bbox_pt": _r(rect, 4), "bbox_px": list(box_px), "size_px": [w, h], "xref": xref,
                "rotated_deg": round(bg.angle_deg, 3)}
    # MuPDF는 축 정렬 이미지를 픽셀 경계 바깥쪽으로 맞춰(grid-fit) 그린다. 소수점 오차로
    # 모서리가 경계를 아주 조금 넘으면 바깥 1px 줄까지 덮으므로, 아주 조금(0.002pt ≈ 0.008px) 안쪽으로 넣는다.
    eps = PATCH_INSET_PT
    draw = pymupdf.Rect(rect[0] + eps, rect[1] + eps, rect[2] - eps, rect[3] - eps)
    xref = page.insert_image(draw, pixmap=pix, overlay=True, keep_proportion=False)
    # MuPDF는 RGB 픽스맵을 ICCBased(sRGB)로 넣는다. 원본 배경은 DeviceRGB라 Acrobat·인쇄의
    # 색 관리에서 경계에 미세한 색 차이가 날 수 있으므로 원본과 같은 DeviceRGB로 맞춘다.
    kind, val = page.parent.xref_get_key(bg.xref, "ColorSpace")
    page.parent.xref_set_key(xref, "ColorSpace", val if kind in ("name", "xref") else "/DeviceRGB")
    return {"bbox_pt": _r(rect, 4), "bbox_px": list(box_px), "size_px": [w, h], "xref": xref}


def _tag_labels_in(rect_pt: Box, protect_pt: list, bg: BackgroundImage, before: np.ndarray):
    """태그 영역(위아래 여유 포함)에서 라벨을 찾아 (LabelPx, px 상자, pt 상자)를 왼쪽부터 돌려준다."""
    size = (before.shape[1], before.shape[0])
    h = rect_pt[3] - rect_pt[1]
    search_pt = clip_horizontally(expand(rect_pt, 0, TAG_SEARCH_PAD * h), protect_pt)
    sx0, sy0, sx1, sy1 = px_box_outer(bg.pt_to_px(search_pt), size)
    h_px = h / bg.pt_per_px  # 회전된 배경에서도 감싸는 상자가 아니라 실제 높이
    out = []
    for lab in find_tag_labels(before[sy0:sy1, sx0:sx1], min_h=TAG_MIN_H * h_px, max_h=TAG_MAX_H * h_px):
        bpx = (lab.bbox[0] + sx0, lab.bbox[1] + sy0, lab.bbox[2] + sx0, lab.bbox[3] + sy0)
        out.append((lab, bpx, bg.px_to_pt(bpx)))
    return out


def detect_tag_labels(page, template: RegionTemplate, region: Region) -> list[dict]:
    """원본 쪽에서 태그 라벨의 색·위치를 잰다 (지우지 않음). [{"color": "#rrggbb", "bbox_pt": [...]}, ...]"""
    bg = find_background_image(page)
    raw = load_image_rgb(page.parent, bg.xref)
    before, _ = render_on_image_grid(page, bg, raw)
    prect: Box = tuple(page.rect)
    protect_pt = [ratio_to_pt(p.rect, prect) for p in template.protect]
    rect_pt = ratio_to_pt(region.rect, prect)
    return [{"color": rgb_to_hex(lab.color), "bbox_pt": _r(bpt)}
            for lab, _bpx, bpt in _tag_labels_in(rect_pt, protect_pt, bg, before)]


def uses_text_mask(reg: Region, has_text_layer: bool) -> bool:
    """글자 모양만 지우는 방식(app.erase_text)을 쓸 영역인가.
    텍스트 레이어가 없는 글자 영역(그림 카드) 또는 지우기 방식(erase.mode)을 정한 영역. 예전 설정
    (keep_ink_taller_than_pt)을 쓰는 영역과 텍스트 레이어가 있는 영역은 지금까지의 방식을 그대로 쓴다."""
    er = reg.erase or {}
    if reg.kind != "text":
        return False
    if er.get("mode"):
        return True
    return not has_text_layer and not er.get("keep_ink_taller_than_pt")


INLINE_ICON_SCALE_RANGE = (0.8, 3.0)  # 글 속 아이콘 크기(그 줄 글자 높이 대비)로 믿을 범위


def _find_icons(sub: np.ndarray, lb, src, reg) -> list:
    """원문 아이콘의 자리 [(이름, sub 안 px 상자, 아이콘만 있는 줄인가)]. 못 찾은 아이콘은 뺀다."""
    if not isinstance(src, str) or "[[" not in src:
        return []
    from app import icons

    rot = int(((reg.style if reg is not None else None) or {}).get("rotate", 0) or 0) % 360
    img = sub[lb[1]:lb[3], lb[0]:lb[2]]
    if rot == 180:
        img = np.ascontiguousarray(img[::-1, ::-1])
    out = []
    try:
        found = icons.icon_boxes(img, src)
    except Exception:  # noqa: BLE001 - 아이콘을 못 찾아도 글자 지우기는 한다
        return []
    for nm, (x0, y0, x1, y1), only in found:
        if rot == 180:
            x0, y0, x1, y1 = img.shape[1] - x1, img.shape[0] - y1, img.shape[1] - x0, img.shape[0] - y0
        out.append((nm, (x0 + lb[0], y0 + lb[1], x1 + lb[0], y1 + lb[1]), only))
    return out


SET_ICON_ASPECT = 2.0  # 사전 그림의 가로/세로가 이 이상이면 아이콘 여럿을 옆으로 늘어놓은 세트 그림으로 본다


def inline_icon_box_ok(box, lim_px: float, template=None) -> bool:
    """글 속 아이콘 상자가 글 속 아이콘으로 믿을 만한 크기인가 (lim_px보다 크면 아이콘 찾기가 그림에 속은 것).
    가로세로 모두 lim_px까지. 다만 사전 그림이 세트 그림(가로/세로 SET_ICON_ASPECT 이상: 문·광석·궁전, 네잎 문양 3개)이면
    폭은 lim_px × 그 비까지 봐 준다 (가로로 긴 세트 아이콘을 버리지 않는다). 아이콘 하나짜리(왕관·도시 1.3~1.9)는
    예전대로: 이름표가 붙은 큰 아이콘(공주 6쪽 도시+社交)을 지우면 이름표만 남는다."""
    w, h = box[2] - box[0], box[3] - box[1]
    aspect = 1.0
    shape = getattr(template, "shape", None)
    if shape is not None and len(shape) >= 2 and shape[0] > 0:
        a = float(shape[1]) / float(shape[0])
        if a >= SET_ICON_ASPECT:
            aspect = a
    return h <= lim_px and w <= lim_px * aspect


def _inline_icons(found: list, lay: dict, tm) -> list:
    """글 줄 안의 아이콘 자리를 tm.mask에 더한다 (그 자리를 지운다).
    [(이름, sub 안 px 상자, 그 줄 글자 높이 대비 크기 | None)]. 아이콘만 있는 줄과 원본 그대로 남긴 줄(keep)의
    아이콘은 그대로 둔다."""
    H, W = tm.mask.shape
    kept = [ln for ln in lay.get("lines") or [] if ln.get("keep")]
    out = []
    for nm, bx, only in found:
        if only:
            continue
        cy0 = (bx[1] + bx[3]) / 2
        if any(ln["bbox"][1] <= cy0 <= ln["bbox"][3] for ln in kept):
            continue  # 원본 그대로 남긴 줄(아이콘 문단 등) 안의 아이콘: 줄과 함께 남긴다
        tm.mask[max(0, bx[1] - 2):min(H, bx[3] + 2), max(0, bx[0] - 2):min(W, bx[2] + 2)] = True
        cy = (bx[1] + bx[3]) / 2
        ln = min((ln for ln in lay.get("lines") or [] if ln["bbox"][1] - ln["glyph_h"] <= cy <= ln["bbox"][3] + ln["glyph_h"]),
                 key=lambda ln: abs((ln["bbox"][1] + ln["bbox"][3]) / 2 - cy), default=None)
        sc = None
        if ln is not None and ln.get("glyph_h"):
            v = (bx[3] - bx[1]) / ln["glyph_h"]
            if INLINE_ICON_SCALE_RANGE[0] <= v <= INLINE_ICON_SCALE_RANGE[1]:
                sc = round(v, 3)
        out.append((nm, bx, sc))
    return out


_CANCEL = threading.local()  # 이 스레드의 작업 취소 확인 (쪽 안에서도 영역마다 멈출 수 있게)


def set_cancel_check(fn) -> None:
    """이 스레드에서 erase_page가 영역마다 부를 취소 확인 함수 (CancelToken.check, None이면 끔).
    쪽 하나를 지우는 데 30초 넘게 걸려, 쪽 사이에서만 확인하면 초기화·삭제가 그만큼 기다렸다."""
    _CANCEL.check = fn


def _cancel_point() -> None:
    fn = getattr(_CANCEL, "check", None)
    if fn is not None:
        fn()


def _fill_label_lines(sub: np.ndarray, tm, lay: dict, allow: np.ndarray | None = None) -> None:
    """이름표 띠 위 글자 줄(테두리 색이 띠 색인 줄: ‘常時効果’·‘プレイ時効果’)은 줄 상자 안에서 띠 색보다 글자색에 가까운
    픽셀을 모두 지울 자리에 넣는다. 띠 위 흰 글자는 획이 조각나 몇 조각이 빠지면 잔상이 남았다 (1·3·5·7·9쪽).
    줄 상자는 글자 끝까지라 띠 양 끝의 ◆ 장식은 남는다. 원본 그대로 둘 줄(keep)은 건드리지 않는다."""
    def rgb(h):
        h = (h or "").lstrip("#")
        return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], np.float32) if len(h) == 6 else None

    H, W = tm.mask.shape
    I = sub.astype(np.float32)
    for ln in lay.get("lines") or []:
        fg, ring = rgb(ln.get("color")), rgb(ln.get("stroke_color"))
        if fg is None or ring is None or ln.get("keep") or np.abs(fg - ring).max() < 80:
            continue
        x0, y0, x1, y1 = (int(v) for v in ln["bbox"])
        m = max(2, int(0.15 * float(ln.get("glyph_h") or (y1 - y0))))
        ax0, ay0, ax1, ay1 = max(0, x0 - m), max(0, y0 - m), min(W, x1 + m), min(H, y1 + m)
        patch = I[ay0:ay1, ax0:ax1]
        # 띠 안쪽만: 띠 색에 가까운 픽셀을 닫고 구멍(글자)을 메운 곳. 줄 상자에 걸친 띠 밖 종이(흰 글자색에 더 가깝다)는
        # 빼야 띠 가장자리가 패이지 않는다
        gh = max(3, int(float(ln.get("glyph_h") or (y1 - y0))))
        band = (np.abs(patch - ring).max(axis=2) < 50).astype(np.uint8)
        k = max(3, gh // 3) | 1
        band = cv2.morphologyEx(band, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
        flood = np.pad(band, 1)
        cv2.floodFill(flood, None, (0, 0), 1)
        band = band.astype(bool) | (flood[1:-1, 1:-1] == 0)  # 띠 + 띠가 둘러싼 글자 구멍
        band = cv2.erode(band.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
        if band.mean() < 0.3:  # 띠가 거의 없다: 띠 위 글자가 아니다
            continue
        near_fg = (np.abs(patch - fg).max(axis=2) < np.abs(patch - ring).max(axis=2)) & band
        # 글자 둘레 번짐 1px만 (테두리 두께만큼 넓히면 지울 자리가 커져 채울 때 띠가 패였다)
        near_fg = cv2.dilate(near_fg.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool) & band
        # 글자 바로 둘레(2px)의 띠보다 어두운 테두리·그림자 (빨간 띠에 진한 점으로 남던 것)
        close = cv2.dilate(near_fg.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool) & band
        dark = (patch.mean(axis=2) < ring.mean() - 25) & close
        add = near_fg | dark
        if allow is not None:  # 보호 자리(지키는 그림·남길 아이콘)와 좁힌 상자 밖은 넣지 않는다
            add &= allow[ay0:ay1, ax0:ax1]
        tm.mask[ay0:ay1, ax0:ax1] |= add


def _confirmed_icons(found: list, confirmed: list, src: str, to_px, origin: tuple) -> list:
    """찾은 글 속 아이콘 자리 found [(이름, sub px 상자, 아이콘만 있는 줄인가)]에 사람이 확정한 자리를 넣는다.
    confirmed [(이름, 같은 이름의 몇째, 원본 쪽 pt 상자)]: 확정한 자리가 있는 이름은 찾은 자리를 버리고 확정한 자리를 쓴다
    (찾기가 못 찾았거나 엉뚱한 곳을 잡은 아이콘). ‘아이콘만 있는 줄’은 원문 줄로 정한다. 크기로 거르지 않는다."""
    from app.erase_text import is_icon_line
    from app.icons import TOKEN_RE

    only_of: dict[tuple, bool] = {}
    seen: dict[str, int] = {}
    for line in src.splitlines():
        for t in TOKEN_RE.finditer(line):
            k = seen.get(t.group(1), 0)
            seen[t.group(1)] = k + 1
            only_of[(t.group(1), k)] = is_icon_line(line)
    ox, oy = origin
    conf = {}
    for nm, k, bpt in confirmed:
        if (nm, k) not in only_of:
            continue  # 원문에 없는 아이콘 (원문이 바뀜)
        x0, y0, x1, y1 = to_px(tuple(bpt))
        conf[(nm, k)] = ((int(round(x0)) - ox, int(round(y0)) - oy, int(round(x1)) - ox, int(round(y1)) - oy),
                         only_of[(nm, k)])
    names = {nm for nm, _k in conf}
    # 확정한 자리는 4번째 값 True (부른 쪽이 지울 자리를 조금 넓힌 뒤 떼어 낸다)
    return [f_ for f_ in found if f_[0] not in names] + [(nm, bx, only, True) for (nm, _k), (bx, only) in conf.items()]


@dataclass
class _PageCtx:
    """erase_page 한 쪽의 공통 상태 (단계 함수들이 함께 쓴다)."""
    page: object
    template: RegionTemplate
    bg: BackgroundImage
    raw: np.ndarray  # 원본 배경 이미지 픽셀
    before: np.ndarray  # 원래 모양 (이미지 격자)
    size: tuple[int, int]
    prect: Box
    protect_pt: list
    warnings: list[str]
    # ---- 1. 영역 모으기 결과
    region_metas: list = field(default_factory=list)
    keep_above_px: dict = field(default_factory=dict)  # 영역별 남길 덩어리(왕관 등) px 상자
    redact_boxes: list = field(default_factory=list)
    work_pt: dict = field(default_factory=dict)
    forced_pt: list = field(default_factory=list)
    span_forced_pt: list = field(default_factory=list)
    mask_regions: dict = field(default_factory=dict)  # 글자 모양만 지울 영역 -> 방식

    def to_px(self, b: Box) -> Box:
        return self.bg.pt_to_px(b)

    def to_pt(self, b: Box) -> Box:
        return self.bg.px_to_pt(b)


@dataclass
class _CleanStage:
    """3. 이미지 정리 결과 (자른 범위, 마스크, 정리한 이미지)."""
    crop: tuple[int, int, int, int]
    work_px: dict
    res: CleanResult
    new_img: np.ndarray
    forced_m: np.ndarray
    span_m: np.ndarray
    protect_m: np.ndarray
    keep_meta: dict
    keep_px: dict

    def local(self, b: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        cx0, cy0 = self.crop[0], self.crop[1]
        return (b[0] - cx0, b[1] - cy0, b[2] - cx0, b[3] - cy0)


def erase_page(page, template: RegionTemplate, sample: dict | None = None, seed: int = 0,
               params: CleanParams | None = None, modes: dict | None = None,
               sources: dict | None = None, icon_templates: dict | None = None,
               keep_lines: dict | None = None, icon_boxes: dict | None = None) -> PageErase:
    """page(결과 문서 쪽의 페이지 사본)에서 영역 글자를 지운다. page는 바뀐다.
    modes: 이 카드만 다르게 지울 영역 {영역 이름: 방식} (영역 설정 erase.mode보다 우선).
    sources: 읽은 원문 {영역 이름: 글}. 그림 카드에서 지울 줄을 원문 줄에 맞춘다.
    keep_lines: {영역 이름: {"lines": 지우지 않을 원문 줄 번호(빈 줄 뺀 기준), "boxes_pt": 그 칸 아이콘 자리}} —
    아이콘 문단만 원본 그대로 둘 칸 (글자 모양만 지우는 영역에서만 쓴다. 아이콘 자리는 지키고, 남길 줄을 찾지 못하면 그
    영역은 하나도 지우지 않는다: erase_text.match_source_lines).
    icon_templates: 게임 아이콘 사전 그림 {이름: RGBA} (icons.templates_for). 글 속 아이콘 자리를 다시 확인한다.
    icon_boxes: ② 원문 확인에서 사람이 확정한 아이콘 자리 {영역 이름: [(이름, 같은 이름의 몇째, 원본 쪽 pt 상자)]}.
    찾기 결과보다 먼저 쓴다 (찾기가 그 아이콘을 못 찾아 지우지 않고 남기던 것: 4·6쪽 human, 8쪽 house)."""
    warnings: list[str] = []
    doc = page.parent
    prect: Box = tuple(page.rect)
    bg = find_background_image_or_none(page)
    if bg is None:  # 배경 스캔 이미지가 없는 벡터 인쇄 PDF: 텍스트 레이어 글자만 지운다 (app.erase_vector)
        from app.erase_vector import erase_vector_page

        return erase_vector_page(page, template, sample, seed=seed, params=params, modes=modes, sources=sources,
                                 icon_templates=icon_templates, keep_lines=keep_lines)
    raw = load_image_rgb(doc, bg.xref)
    size = (raw.shape[1], raw.shape[0])
    before, w = render_on_image_grid(page, bg, raw)
    warnings += w
    if not template.regions:  # 지울 영역이 없음 (아이콘만 있는 칸을 모두 뺀 쪽): 쪽을 그대로 둔다
        meta = {
            "page_rect_pt": _r(prect), "mediabox": _r(page.mediabox), "cropbox": _r(page.cropbox),
            "background_image": {"xref": bg.xref, "bbox_pt": _r(bg.bbox), "size_px": list(size),
                                 "dpi": [round(v, 2) for v in bg.dpi], "stream_untouched": True},
            "patches": [], "clean": None, "removed_inline_images_pt": [], "regions": [], "warnings": warnings,
        }
        return PageErase(meta, before, None, (0, 0, 0, 0), {}, warnings, None)

    protect_pt = [ratio_to_pt(p.rect, prect) for p in template.protect]
    ctx = _PageCtx(page, template, bg, raw, before, size, prect, protect_pt, warnings)
    # ---- 1. 영역마다 지울 글자·라벨·작업 범위 모으기
    _erase_collect_regions(ctx, sample, modes)
    # ---- 2. 레드랙션 (텍스트만)
    removed_inline = _erase_redact(ctx)
    # ---- 3. 이미지 정리
    cs = _erase_clean_image(ctx, params, seed, keep_lines)
    # ---- 3b. 글자 모양만 지우기 (그림 카드): 글자 픽셀만 찾아 그 자리만 채운다 (app.erase_text)
    text_mask_meta: dict[str, dict] = {}
    layout_meta: dict[str, dict] = {}
    regs_by_name = {r.name: r for r in template.regions}
    for name, mode in ctx.mask_regions.items():
        _cancel_point()
        _erase_text_region(ctx, cs, name, mode, regs_by_name, seed, sources, icon_templates, keep_lines,
                           icon_boxes, text_mask_meta, layout_meta)
    # ---- 4. 패치 덮기
    patches = _erase_place_patches(ctx, cs, params)
    # ---- 5. 라벨 다시 그리기
    _erase_redraw_labels(ctx)
    # ---- 영역별 통계
    _erase_region_stats(ctx, cs, removed_inline, text_mask_meta, layout_meta)

    res, crop = cs.res, cs.crop
    meta = {
        "page_rect_pt": _r(prect),
        "mediabox": _r(page.mediabox),
        "cropbox": _r(page.cropbox),
        "background_image": {
            "xref": bg.xref,
            "bbox_pt": _r(bg.bbox),
            "size_px": list(size),
            "dpi": [round(v, 2) for v in bg.dpi],
            "stream_untouched": True,  # 원본 스트림을 바이트 그대로 둔다 (CLI가 해시로 확인)
        },
        "patches": patches,
        "clean": {"threshold": round(res.threshold, 2), "paper_noise_sigma": round(res.noise_sigma, 2),
                  "crop_px": list(crop)},
        "removed_inline_images_pt": [_r(b) for b in removed_inline],
        "regions": ctx.region_metas,
        "warnings": warnings,
    }
    return PageErase(meta, before, res, crop, cs.work_px, warnings, cs.new_img)


def _erase_collect_regions(ctx: _PageCtx, sample: dict | None, modes: dict | None) -> None:
    """1. 영역마다 지울 span·태그 라벨·인라인 이미지·작업 범위를 모아 ctx(region_metas 등)에 넣는다."""
    page, bg = ctx.page, ctx.bg
    spans = page_spans(page)
    small_images = [
        tuple(i["bbox"]) for i in page.get_image_info(xrefs=True)
        if not (i.get("xref", 0) == bg.xref) and area(tuple(i["bbox"])) < 400
    ]
    used_spans: set[int] = set()
    for reg in ctx.template.regions:
        _erase_collect_region(ctx, reg, spans, used_spans, small_images, sample, modes)


def _erase_collect_region(ctx: _PageCtx, reg: Region, spans: list[dict], used_spans: set[int],
                          small_images: list, sample: dict | None, modes: dict | None) -> None:
    """영역 하나: 겹치는 span을 맡기고, 라벨·인라인 이미지·이미지 글자 상자로 작업 범위를 정해 메타를 쌓는다."""
    rect_pt = ratio_to_pt(reg.rect, ctx.prect)
    assigned, partial_warn = [], []
    for i, s in enumerate(spans):
        r = overlap_ratio(s["bbox"], rect_pt)
        if r <= 0:
            continue
        if r >= SPAN_ASSIGN_MIN:
            if i in used_spans:
                continue
            used_spans.add(i)
            assigned.append(s)
            if r < SPAN_PARTIAL_WARN:
                partial_warn.append(f"span {_r(s['bbox'], 1)} 이 영역에 {r:.0%}만 걸침 (전부 지움)")
        else:
            partial_warn.append(f"span {_r(s['bbox'], 1)} 이 영역에 {r:.0%}만 걸쳐 지우지 않음")
    boxes = [rect_pt] + [s["bbox"] for s in assigned]
    # 쪽에 텍스트 레이어가 조금이라도 있으면(ClearScan) 예전 방식: 빠진 제목도 그 방식으로 맞춰 왔다
    if (modes or {}).get(reg.name) or uses_text_mask(reg, bool(assigned) or bool(spans)):
        ctx.mask_regions[reg.name] = (modes or {}).get(reg.name) or (reg.erase or {}).get("mode") or "auto"

    labels_meta = []
    if reg.kind == "tag_pills":
        labels_meta = _erase_tag_labels(ctx, reg, rect_pt, assigned, boxes, sample)

    inline_here = [b for b in small_images if overlap_ratio(b, rect_pt) > 0.5]
    boxes += inline_here
    image_text_box = None
    kt = (reg.erase or {}).get("keep_ink_taller_than_pt")
    if not assigned and reg.kind != "tag_pills" and kt:
        image_text_box = _erase_image_text_box(ctx, reg, rect_pt, kt)
        if image_text_box is not None:
            boxes = [image_text_box]
    wpt = clip_horizontally(union(boxes), ctx.protect_pt)
    ctx.work_pt[reg.name] = wpt
    for s in assigned:
        ctx.redact_boxes.append(s["bbox"])
        ctx.span_forced_pt.append(expand(s["bbox"], 0.4))
    ctx.forced_pt += [expand(b, 0.5) for b in inline_here]

    ctx.region_metas.append(
        {
            "name": reg.name,
            "kind": reg.kind,
            "rect_ratio": reg.rect.to_dict(),
            "rect_pt": _r(rect_pt),
            "work_rect_pt": _r(wpt),
            "style_defaults": reg.style,
            "text_layer": {
                "present": bool(assigned),
                "spans": [
                    {"bbox_pt": _r(s["bbox"]), "size_pt": s["size"], "color": s["color"], "font": s["font"],
                     "origin_pt": _r(s["origin"]), "chars": s["chars"]}
                    for s in assigned
                ],
                "size_pt": _dominant(assigned, "size"),
                "color": _dominant(assigned, "color"),
                "union_bbox_pt": _r(union([s["bbox"] for s in assigned])) if assigned else None,
            },
            "tag_labels": labels_meta if reg.kind == "tag_pills" else None,
            "inline_images_pt": [_r(b) for b in inline_here],
            "image_text_box_pt": _r(image_text_box) if image_text_box else None,
            "warnings": partial_warn,
        }
    )


def _erase_tag_labels(ctx: _PageCtx, reg: Region, rect_pt: Box, assigned: list[dict], boxes: list,
                      sample: dict | None) -> list[dict]:
    """태그 영역의 라벨을 재서 메타 목록으로 돌려준다. 라벨 상자는 boxes(작업 범위)와 ctx.forced_pt에 더한다."""
    bg = ctx.bg
    labels_meta = []
    for lab, bpx, bpt in _tag_labels_in(rect_pt, ctx.protect_pt, bg, ctx.before):
        scale_pt = bg.pt_per_px
        tl = [s for s in assigned if overlap_ratio(s["bbox"], bpt) > 0.3 or overlap_ratio(bpt, s["bbox"]) > 0.3]
        labels_meta.append(
            {
                "bbox_pt": _r(bpt),
                "bbox_px": _r(bpx, 1),
                "radius_pt": round(lab.radius * scale_pt, 3),
                "radius_px": round(lab.radius, 2),
                "color": rgb_to_hex(lab.color),
                "color_text_layer": tl[0]["color"] if tl else None,
                "in_text_layer": bool(tl),
                "coverage": round(lab.coverage, 3),
            }
        )
        boxes.append(bpt)
        ctx.forced_pt.append(expand(bpt, 0.5))
    expected = None
    if sample and reg.name in sample:
        src = sample[reg.name].get("source")
        expected = len(src) if isinstance(src, list) else None
    if expected is not None and expected != len(labels_meta):
        ctx.warnings.append(f"[{reg.name}] 찾은 라벨 {len(labels_meta)}개, samples.json 기대 {expected}개")
    return labels_meta


def _erase_image_text_box(ctx: _PageCtx, reg: Region, rect_pt: Box, kt: float) -> Box | None:
    """이미지에 박힌 글자(텍스트 레이어 없음, keep_ink_taller_than_pt 설정)의 잉크 상자(pt, 영역 안). 없으면 None.
    글자 줄 위의 남길 덩어리(왕관 등)는 ctx.keep_above_px에 넣는다."""
    bg = ctx.bg
    rpx = px_box_outer(ctx.to_px(rect_pt), ctx.size)
    ka = (reg.erase or {}).get("keep_ink_above_text")
    if ka:  # 180도 돌린 영역은 '글자 위'가 페이지에서 아래쪽
        ka = "down" if int((reg.style or {}).get("rotate", 0)) % 360 == 180 else "up"
    ib, kept_boxes = text_ink_box(ctx.raw, rpx, kt / bg.pt_per_px, keep_above=ka)
    if kept_boxes:
        ctx.keep_above_px[reg.name] = kept_boxes
    if ib is None:
        return None
    pad = IMAGE_TEXT_PAD_PT
    image_text_box = expand(ctx.to_pt(tuple(float(v) for v in ib)), pad)
    return intersect(image_text_box, rect_pt)


def _erase_redact(ctx: _PageCtx) -> list[Box]:
    """2. 영역 글자를 레드랙션하고(텍스트만) 영역 안 인라인 이미지를 지운다. 지운 인라인 이미지 상자를 돌려준다."""
    import pymupdf

    page, bg, region_metas = ctx.page, ctx.bg, ctx.region_metas
    for b in ctx.redact_boxes:
        page.add_redact_annot(pymupdf.Rect(b), fill=False, cross_out=False)
    if ctx.redact_boxes:
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )
    removed_inline = remove_inline_images(page, [m_["rect_pt"] for m_ in region_metas])
    left_spans = [s for s in page_spans(page) if any(overlap_ratio(s["bbox"], m_["rect_pt"]) > 0 for m_ in region_metas)]
    if left_spans:
        ctx.warnings.append(f"레드랙션 뒤에도 영역에 걸친 글자 {len(left_spans)}개: {[_r(s['bbox'], 1) for s in left_spans]}")
    bg_after = find_background_image(page)
    if bg_after.xref != bg.xref:
        raise RuntimeError("레드랙션이 배경 이미지를 바꿨습니다")
    return removed_inline


def _erase_clean_image(ctx: _PageCtx, params: CleanParams | None, seed: int, keep_lines: dict | None) -> _CleanStage:
    """3. 배경 이미지의 작업 범위를 정리한다 (글자 모양만 지울 영역은 뺀다: _erase_text_region)."""
    raw, size, to_px = ctx.raw, ctx.size, ctx.to_px
    work_px = {k: px_box_outer(to_px(v), size) for k, v in ctx.work_pt.items()}
    crop = px_box_outer(expand(union([tuple(map(float, b)) for b in work_px.values()]), 40), size)
    # 자르는 시작점을 8px 격자에 맞춘다: 질감을 JPEG 8×8 블록 결 그대로 빌려 오기 위해
    crop = (crop[0] // 8 * 8, crop[1] // 8 * 8, crop[2], crop[3])
    cx0, cy0, cx1, cy1 = crop
    ch, cw = cy1 - cy0, cx1 - cx0

    def local(b: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        return (b[0] - cx0, b[1] - cy0, b[2] - cx0, b[3] - cy0)

    work_m = np.zeros((ch, cw), bool)
    for name, b in work_px.items():
        if name not in ctx.mask_regions:  # 글자 모양만 지울 영역은 아래에서 따로
            fill_box(work_m, local(b))
    # 무조건 지울 픽셀: 텍스트 레이어가 실제로 칠한 픽셀(렌더 - 이미지 차이; 글자 모양 그대로)
    # + 태그 라벨 bbox + 인라인 이미지. span bbox 전체를 칠하지 않는 이유: bbox 안의 깨끗한
    # 종이 픽셀이 배경 추정에 필요하다.
    glyph_m = np.abs(ctx.before[cy0:cy1, cx0:cx1].astype(np.int16) - raw[cy0:cy1, cx0:cx1].astype(np.int16)).max(axis=2) > 10
    span_box_m = np.zeros((ch, cw), bool)
    for b in ctx.span_forced_pt:
        fill_box(span_box_m, local(px_box_outer(to_px(b), size)))
    forced_m = glyph_m & span_box_m
    for b in ctx.forced_pt:
        fill_box(forced_m, local(px_box_outer(to_px(b), size)))
    span_m = np.zeros((ch, cw), bool)
    for b in ctx.span_forced_pt:
        fill_box(span_m, local(px_box_outer(to_px(b), size)))
    protect_m, keep_meta, keep_px = _erase_protect_mask(ctx, crop, work_px, work_m, keep_lines)

    if work_m.any():
        res = clean_area(raw[cy0:cy1, cx0:cx1], work_m, forced_m, protect_m, params, seed=seed)
    else:
        z = np.zeros((ch, cw), bool)
        res = CleanResult(raw[cy0:cy1, cx0:cx1].copy(), z, z.copy(), z.copy(), z.copy(), 0.0, 0.0)
    new_img = raw.copy()
    new_img[cy0:cy1, cx0:cx1] = res.image
    return _CleanStage(crop, work_px, res, new_img, forced_m, span_m, protect_m, keep_meta, keep_px)


def _erase_protect_mask(ctx: _PageCtx, crop: tuple[int, int, int, int], work_px: dict, work_m: np.ndarray,
                        keep_lines: dict | None):
    """지킬 픽셀 (자른 범위 좌표): 지키는 영역, 큰 잉크 덩어리, 글자 줄 위 그림, 남길 아이콘 자리.
    (protect_m, keep_meta {영역: 지킨 큰 잉크 px 수}, keep_px {영역: 원본 그대로 둘 아이콘 px 상자})"""
    raw, size, to_px, bg = ctx.raw, ctx.size, ctx.to_px, ctx.bg
    cx0, cy0, cx1, cy1 = crop
    ch, cw = cy1 - cy0, cx1 - cx0

    def local(b: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        return (b[0] - cx0, b[1] - cy0, b[2] - cx0, b[3] - cy0)

    protect_m = np.zeros((ch, cw), bool)
    for b in ctx.protect_pt:
        fill_box(protect_m, local(px_box_outer(to_px(b), size)))
    # 영역 안의 큰 잉크 덩어리(+2 같은 큰 숫자, 영역에 걸친 그림)는 글자가 아니므로 지키기
    keep_meta = {}
    for reg in ctx.template.regions:
        kt = (reg.erase or {}).get("keep_ink_taller_than_pt")
        if not kt:
            continue
        kept = tall_ink_mask(raw[cy0:cy1, cx0:cx1], local(work_px[reg.name]), kt / bg.pt_per_px)
        protect_m |= kept
        keep_meta[reg.name] = int(kept.sum())
    for name, bxs in ctx.keep_above_px.items():  # 글자 줄 위의 작은 그림(왕관): 테두리까지 넉넉히 지킨다
        for b in bxs:
            fill_box(protect_m, local((b[0] - 3, b[1] - 3, b[2] + 3, b[3] + 3)))
    keep_px: dict[str, list] = {}  # 아이콘 문단만 원본 그대로인 칸의 아이콘 자리 (배경 px): 지운다고 고른 픽셀이 있어도 지킨다
    for name, kl in (keep_lines or {}).items():
        keep_px[name] = [px_box_outer(to_px(tuple(map(float, b_))), size) for b_ in kl.get("boxes_pt") or []]
        for b in keep_px[name]:
            fill_box(protect_m, local((b[0] - 2, b[1] - 2, b[2] + 2, b[3] + 2)))
    # 이미지에 박힌 글자 영역(텍스트 레이어 없음): 작업 범위 밖의 그림·색 번짐은 배경 추정에 쓰지 않는다.
    # 영역 밖이라 바뀌지도 않는다(protect). 쓰면 줄 방향 배경 평균에 그림 색이 끌려와 얼룩이 남는다.
    if any(m_.get("image_text_box_pt") for m_ in ctx.region_metas):
        protect_m |= non_paper_mask(raw[cy0:cy1, cx0:cx1]) & ~work_m
    return protect_m, keep_meta, keep_px


@dataclass
class _TextRegion:
    """3b. 글자 모양만 지우는 영역 하나의 작업 상태 (좌표는 sub 안 px)."""
    name: str
    mode: str
    b: tuple  # 작업 범위 (배경 px, 넓히기 전)
    ox0: int
    oy0: int
    ox1: int
    oy1: int
    sub: np.ndarray  # 채우기 전 원문 픽셀 (줄 모양을 잴 때도 쓴다)
    lb: tuple  # 글자를 찾는 상자
    lb_region: tuple  # 영역 상자
    core: tuple  # 글자 상자 몸통
    prot: np.ndarray
    look: np.ndarray  # 글자를 찾는 그림 (좁혔으면 상자 밖을 상자 색으로 칠한 사본)
    tm: object  # erase_text.TextMask
    picture_bg: bool = False
    picture_strokes: bool = False
    pic: object = None  # ‘복잡한 그림 배경’ 방식으로 찾은 글자 (app.erase_picture)
    picture_tried: bool = False
    no_text: bool = False  # 읽은 원문에 글자가 없어 일부러 지우지 않는 칸
    inside: np.ndarray | None = None  # 좁힌 상자 몸통 (lb != lb_region일 때만)
    lay: dict = field(default_factory=dict)
    src: object = None
    found: list = field(default_factory=list)
    icons_found: list = field(default_factory=list)


def _erase_text_region(ctx: _PageCtx, cs: _CleanStage, name: str, mode: str, regs_by_name: dict, seed: int,
                       sources: dict | None, icon_templates: dict | None, keep_lines: dict | None,
                       icon_boxes: dict | None, text_mask_meta: dict, layout_meta: dict) -> None:
    """3b. 영역 하나의 글자 픽셀만 찾아 그 자리만 채운다 (app.erase_text). 결과는 cs와 두 메타 사전에 넣는다."""
    tr = _text_region_find(ctx, cs, name, mode, sources)
    _text_region_match(tr, regs_by_name, sources, keep_lines, cs.keep_px)
    _text_region_icons(ctx, tr, regs_by_name, icon_templates, icon_boxes, cs.keep_px)
    _text_region_fill(ctx, cs, tr, seed, text_mask_meta)
    layout_meta[name] = _text_region_layout(ctx, tr, keep_lines)


def _text_region_find(ctx: _PageCtx, cs: _CleanStage, name: str, mode: str, sources: dict | None) -> _TextRegion:
    """글자 상자로 좁히고 글자 픽셀(TextMask)·줄 배치를 찾는다. 그림 배경이면 그림 방식·획 방식으로 다시 찾는다."""
    from app import erase_text

    size = ctx.size
    cx0, cy0 = cs.crop[0], cs.crop[1]
    b = cs.work_px[name]
    mg = 24
    ox0, oy0 = max(0, b[0] - mg), max(0, b[1] - mg)
    ox1, oy1 = min(size[0], b[2] + mg), min(size[1], b[3] + mg)
    sub = cs.new_img[oy0:oy1, ox0:ox1].copy()  # 채우기 전 원문 픽셀 (줄 모양을 잴 때도 쓴다)
    lb = (b[0] - ox0, b[1] - oy0, b[2] - ox0, b[3] - oy0)
    lb_region = lb
    core = erase_text.label_panel(sub, lb)  # 영역이 글자 상자보다 크면 상자 안에서만 (위쪽 그림·띠 밖 테두리 빼기)
    if core != lb_region:
        # 글자가 상자 끝에 닿아 ‘가장자리 조각’으로 빠지지 않게 몇 px 넉넉히 (넓힌 곳은 상자색으로 칠한 사본에서 찾는다)
        m_ = max(3, int(0.12 * (core[3] - core[1])))
        lb = (max(lb_region[0], core[0] - m_), max(lb_region[1], core[1] - m_),
              min(lb_region[2], core[2] + m_), min(lb_region[3], core[3] + m_))
        px_area = lambda b_: max(1, b_[2] - b_[0]) * max(1, b_[3] - b_[1])  # noqa: E731 (이름이 area면 모듈의 area와 헷갈린다)
        if px_area(core) >= 0.97 * px_area(lb_region):  # 거의 영역 그대로: 좁히지 않는다
            lb, core = lb_region, lb_region
    else:
        lb = lb_region
    prot = cs.protect_m[oy0 - cy0:oy1 - cy0, ox0 - cx0:ox1 - cx0]
    # 상자로 좁혔으면 상자 밖을 상자 색으로 칠한 사본에서 글자를 찾는다 (지우기는 원본 sub에)
    look = erase_text.paint_outside(sub, core) if lb != lb_region else sub
    tm = erase_text.text_mask(look, lb, prot, size_box=lb_region)
    tr = _TextRegion(name, mode, b, ox0, oy0, ox1, oy1, sub, lb, lb_region, core, prot, look, tm)
    # 예외: 배경이 그림이면 색으로 고른 글자가 빗나가기 쉽다. 획 모양으로 다시 찾아, 기존 결과가 글자를 놓쳤거나
    # 그림을 넓게 지우려 할 때만 바꾼다 (단색·무늬 배경과 기존 방식이 잘 맞은 그림 배경은 그대로)
    tr.picture_bg = bool(tm.busy)
    tr.picture_tried = mode == "picture" or (mode == "auto" and tr.picture_bg)
    if tr.picture_tried:
        # ‘복잡한 그림 배경’ (사람이 골랐거나, 자동인데 배경이 그림): 따로 만든 방식으로 찾고 채운다.
        # 글자 줄을 못 찾으면 아래 기존 방식으로 (자동이면 획 방식, 사람이 골랐으면 색으로 고른 결과)
        from app import erase_picture

        tr.picture_bg = True
        src0 = (sources or {}).get(name)
        has_src = name in (sources or {})
        tr.pic, tr.tm = erase_picture.region_text_mask(sub, lb_region, prot, tr.tm, src0, has_src)
        tr.no_text = tr.pic is None and erase_picture.source_has_no_text(src0, has_src)
        if tr.pic is not None:
            tr.lb, tr.core, tr.look = lb_region, lb_region, sub
    if tr.picture_bg and tr.pic is None and mode == "auto" and not tr.no_text:
        alt = picture_text_mask(tr.look, tr.lb, prot, tr.tm.texture)
        if tr.lb != lb_region and _box_area(tr.lb) < PICTURE_FULL_RECHECK * _box_area(lb_region):
            # 그림 배경에서는 '글자 상자'로 좁힌 것이 빗나갈 수 있다 (밝은 그림 띠를 상자로 봄): 영역 전체에서
            # 글자 줄이 뚜렷이 더 잡히면 좁히지 않는다
            full = picture_text_mask(sub, lb_region, prot, tr.tm.texture)
            if full is not None and full.core.sum() > PICTURE_UNNARROW * (alt.core.sum() if alt is not None else 0):
                alt, tr.lb, tr.core, tr.look = full, lb_region, lb_region, sub
                tr.tm = erase_text.text_mask(tr.look, tr.lb, prot, size_box=lb_region)
        if prefer_picture_mask(tr.tm, alt):
            alt.texture = tr.tm.texture  # 좁힌 상자를 풀었으면 tm을 다시 쟀으므로 그 값으로
            tr.tm, tr.picture_strokes = alt, True
    tm, lb, core = tr.tm, tr.lb, tr.core
    if lb != lb_region:
        # 지울 픽셀은 상자 몸통 안으로만: 밖(띠 테두리·그림)까지 번지면 채울 때 그 색이 끌려와 얼룩이 생긴다
        inside = np.zeros(tm.mask.shape, bool)
        inside[core[1]:core[3], core[0]:core[2]] = True
        tm.mask &= inside
        tm.core &= inside
        tr.inside = inside
    lay = erase_text.text_layout(tr.look, tm, lb)
    if lb != lb_region:  # 좁힌 상자: 상자 위·아래 끝에 붙은 줄(띠 안의 그림 조각)을 먼저 원문에 없는 줄로 본다
        lay["box_y"] = [core[1], core[3]]
    lay["edge_y"] = [lb[1], lb[3]]  # 영역 위·아래 끝 (끝에 닿은 짧은 조각을 원문에 없는 줄로 먼저 뺀다)
    tr.lay = lay
    return tr


def _text_region_match(tr: _TextRegion, regs_by_name: dict, sources: dict | None, keep_lines: dict | None,
                       keep_px: dict) -> None:
    """찾은 글자 줄을 읽은 원문 줄에 맞추고, 이름표 띠 위 글자 줄을 마저 지울 자리에 넣는다."""
    from app import erase_text

    name, tm, lb, lb_region, prot = tr.name, tr.tm, tr.lb, tr.lb_region, tr.prot
    src = (sources or {}).get(name)
    if isinstance(src, str) and "[[?]]" in src and text_only(regs_by_name.get(name)):
        # ‘글자만’ 영역: 이름 없는 [[?]]가 남아 있어도(설정 전에 읽은 원문) 아이콘 자리를 찾거나 지키지 않는다.
        # 이름 붙은 [[이름]]은 사람이 정한 것이라 예전처럼 다룬다
        from app.extract import drop_unknown_icons

        src = drop_unknown_icons({"source": src})["source"]
    if tr.picture_bg and isinstance(src, str) and not src.strip():
        # 그림 배경인데 읽은 원문이 비어 있다(글자 없는 칸): 글자처럼 보이는 것은 그림 조각이므로 지우지 않는다
        tm.mask[:] = False
        tm.core[:] = False
        tm.notes.append("그림 배경: 읽은 원문이 없어 지우지 않음")
        tr.lay = {**tr.lay, "lines": []}
    found = _find_icons(tr.sub, lb, src, regs_by_name.get(name))
    if isinstance(src, str) and src.strip():
        rows = [(b_[1], b_[3]) for _n, b_, only in found if only]
        ok_px = ~prot if prot is not None else np.ones(tm.mask.shape, bool)
        if lb != lb_region:  # 좁힌 상자: 상자 몸통 안으로만 (위 mask &= inside와 같은 이유)
            ok_px = ok_px & tr.inside
        # 획으로 찾았으면 줄 안의 빈 곳을 색으로 다시 뒤지지 않는다 (img=None): 그림 조각·아이콘 선이 글자색과 비슷하다
        kl = (keep_lines or {}).get(name)
        tr.lay = erase_text.match_source_lines(
            tm, tr.lay, src, icon_rows=rows or None, img=None if tr.picture_strokes else tr.look, allowed_px=ok_px,
            keep_idx=kl["lines"] if kl is not None else None,
            keep_rows=[(b_[1] - tr.oy0, b_[3] - tr.oy0) for b_ in keep_px.get(name) or []])
    if tr.pic is None and not tr.picture_strokes and not tr.lay.get("keep_failed"):
        allow = ~prot if prot is not None else np.ones(tm.mask.shape, bool)
        if lb != lb_region:  # 좁힌 상자: 상자 몸통 안으로만 (위 mask &= inside와 같은 이유)
            core = tr.core
            core_m = np.zeros(tm.mask.shape, bool)
            core_m[core[1]:core[3], core[0]:core[2]] = True
            allow &= core_m
        _fill_label_lines(tr.sub, tm, tr.lay, allow)
    tr.src, tr.found = src, found


def _text_region_icons(ctx: _PageCtx, tr: _TextRegion, regs_by_name: dict, icon_templates: dict | None,
                       icon_boxes: dict | None, keep_px: dict) -> None:
    """글 속 아이콘 자리를 정해(사전 그림 확인, 사람이 확정한 자리, 남길 자리 빼기) 지울 자리에 넣는다."""
    name, tm, lay, src, found = tr.name, tr.tm, tr.lay, tr.src, tr.found
    ox0, oy0 = tr.ox0, tr.oy0
    # 줄 배치로 찾은 아이콘 자리를 사전 그림으로 다시 확인한다 (원문 줄에 맞춘 줄 띠 안에서): 그림 조각을 줄로 봐
    # 엉뚱한 곳을 지우고 진짜 아이콘은 남기던 것 (공주 5쪽 백합 문양, 8쪽 왼쪽 아래 문장)
    # (돌린 영역은 하지 않는다: 줄 띠는 돌리기 전 좌표인데 원문 줄 순서는 거꾸로라 맞는 자리를 띠 밖으로 버린다)
    reg_ = regs_by_name.get(name)
    rot_ = int(((reg_.style if reg_ is not None else None) or {}).get("rotate", 0) or 0) % 360
    if icon_templates and isinstance(src, str) and "[[" in src and rot_ == 0:
        bands = [(ln["bbox"][1], ln["bbox"][3], ln["glyph_h"]) for ln in lay.get("lines") or []]
        from app import icons as _icons

        found, icon_notes = _icons.refine_icon_boxes(tr.look, src, found, bands or None, icon_templates)
        tm.notes += icon_notes
    # 글 줄 안의 아이콘(문장 속 책·숫자 타일 등)도 지운다: 한글 넣기가 그 자리에 아이콘을 다시 그린다
    if tr.pic is not None:
        # 글 속 아이콘이라기엔 너무 큰 상자는 아이콘 찾기가 그림에 속은 것: 통째로 지우면 큰 아이콘·그림이 망가진다
        # 높이로 잰다: 여러 아이콘을 합친 세트 그림(문·광석·궁전 등)은 가로로 길어, 폭으로 재면 버려져 원본 크기를
        # 잃고 한글 넣기가 사전 기본 크기(작게)로 그렸다. 폭은 사전 그림의 가로세로 비만큼 넓게 봐 준다
        lim = INLINE_ICON_SCALE_RANGE[1] * tr.pic.glyph_px
        found = [f_ for f_ in found
                 if f_[2] or inline_icon_box_ok(f_[1], 1.5 * lim, (icon_templates or {}).get(f_[0]))]
    confirmed_px: set = set()
    if (icon_boxes or {}).get(name) and isinstance(src, str):  # 사람이 확정한 자리 (pt라 돌린 영역도 그대로)
        found = _confirmed_icons(found, icon_boxes[name], src, ctx.to_px, (ox0, oy0))
        confirmed_px = {tuple(f_[1]) for f_ in found if len(f_) > 3}
        found = [f_[:3] for f_ in found]
    if lay.get("keep_failed"):
        found, confirmed_px = [], set()  # 남길 문단을 찾지 못해 영역을 지우지 않는다: 글 속 아이콘 자리도 지우지 않는다
    icons_found = _inline_icons(found, lay, tm)
    if confirmed_px:
        # 사람이 잡은 상자는 아이콘에 딱 맞아 흐린 가장자리(그림자·번짐)가 밖에 남는다 (6쪽 human): 지울 자리만 조금 넓힌다.
        # 다시 그릴 크기(icons_found의 상자)는 그대로
        H_, W_ = tm.mask.shape
        for _n, bx, _sc in icons_found:
            if tuple(bx) not in confirmed_px:
                continue
            mx, my = max(3, int(0.06 * (bx[2] - bx[0]))), max(3, int(0.06 * (bx[3] - bx[1])))
            tm.mask[max(0, bx[1] - my):min(H_, bx[3] + my), max(0, bx[0] - mx):min(W_, bx[2] + mx)] = True
    if keep_px.get(name):  # 원본 그대로 둘 아이콘 자리는 글 속 아이콘으로 지우지도, 다시 그리지도 않는다
        kb = [(b_[0] - ox0, b_[1] - oy0, b_[2] - ox0, b_[3] - oy0) for b_ in keep_px[name]]
        for b_ in kb:
            tm.mask[max(0, b_[1] - 2):b_[3] + 2, max(0, b_[0] - 2):b_[2] + 2] = False
        icons_found = [f_ for f_ in icons_found
                       if not any(min(f_[1][2], b_[2]) > max(f_[1][0], b_[0]) and min(f_[1][3], b_[3]) > max(f_[1][1], b_[1])
                                  for b_ in kb)]
    tr.found, tr.icons_found = found, icons_found


def _text_region_fill(ctx: _PageCtx, cs: _CleanStage, tr: _TextRegion, seed: int, text_mask_meta: dict) -> None:
    """찾은 글자 자리를 채워 cs(new_img·res·work_px)에 반영하고 text_mask_meta[영역]을 쓴다."""
    from app import erase_text

    name, mode, tm, sub, prot = tr.name, tr.mode, tr.tm, tr.sub, tr.prot
    lb, lb_region, b = tr.lb, tr.lb_region, tr.b
    ox0, oy0, ox1, oy1 = tr.ox0, tr.oy0, tr.ox1, tr.oy1
    cx0, cy0 = cs.crop[0], cs.crop[1]
    if tr.pic is not None:
        from app import erase_picture

        filled, changed = erase_picture.fill_region(sub, tm, tr.pic, prot, seed=seed)
        used = "picture"
    elif tr.picture_strokes and mode == "auto":
        # 획만 좁게 잡은 마스크: 인페인팅 + 둘레 결로 채운다 (방식 이름은 그대로 inpaint)
        m_fill = grow_halo(sub, tm.mask, prot)
        if lb != lb_region:
            m_fill &= tr.inside
        filled, used, changed = fill_picture(sub, m_fill, seed=seed), "inpaint", m_fill
    else:
        filled, used, changed = erase_text.fill_text(sub, tm, mode, lb)
    cs.new_img[oy0:oy1, ox0:ox1] = filled
    if changed.any():  # 영역 가장자리에 걸친 글자를 상자 밖까지 지웠으면 패치도 그만큼 넓힌다
        ys_, xs_ = np.nonzero(changed)
        cs.work_px[name] = (min(b[0], int(xs_.min()) + ox0), min(b[1], int(ys_.min()) + oy0),
                            max(b[2], int(xs_.max()) + 1 + ox0), max(b[3], int(ys_.max()) + 1 + oy0))
    cs.res.mask[oy0 - cy0:oy1 - cy0, ox0 - cx0:ox1 - cx0] |= changed
    cs.res.image[oy0 - cy0:oy1 - cy0, ox0 - cx0:ox1 - cx0] = filled
    wa = max(1, (b[3] - b[1]) * (b[2] - b[0]))
    text_mask_meta[name] = {
        "mode": mode, "used": used, "background": erase_text.background_kind(sub, tm),
        "mask_ratio": round(float(changed.sum()) / wa, 4), "texture": round(tm.texture, 2),
        "needs_choice": mode == "auto" and tm.busy, "notes": tm.notes,
        # 상자 안에서 글자를 거의 못 찾음: 영역 위치가 카드와 안 맞을 수 있다 (예: 거꾸로 인쇄된 카드)
        "few_text": bool(float(tm.core[lb[1]:lb[3], lb[0]:lb[2]].mean()) < 0.01),
        # 배경이 그림인가, 글자를 획 모양으로 다시 찾았는가 (화면이 주의 표시를 할 때 쓴다)
        "picture_bg": tr.picture_bg, "picture_strokes": tr.picture_strokes,
    }
    if tr.picture_tried:
        from app import erase_picture

        text_mask_meta[name]["picture"] = erase_picture.picture_meta(tr.pic)
    if tr.picture_bg and changed.any():
        ctx.warnings.append(f"[{name}] {PICTURE_BG_WARNING}")
    if lb != lb_region:
        text_mask_meta[name]["notes"] = list(tm.notes) + ["영역 안의 글자 상자에서만 찾음 (위쪽 그림·테두리 제외)"]


def _text_region_layout(ctx: _PageCtx, tr: _TextRegion, keep_lines: dict | None) -> dict:
    """원문 줄 모양 (한글 넣기가 줄 자리·글자색·테두리·굵기·정렬·구분선을 따른다). 좌표는 pt."""
    name, lay, lb = tr.name, tr.lay, tr.lb
    ppx = ctx.bg.pt_per_px
    to_pt = ctx.to_pt

    def _pt(bx, _ox=tr.ox0, _oy=tr.oy0):
        q = to_pt((bx[0] + _ox, bx[1] + _oy, bx[2] + _ox, bx[3] + _oy))
        return [round(v, 3) for v in q]

    return {
        "lines": [{**{k: v for k, v in ln.items() if k not in ("bbox", "glyph_h", "stroke_px", "accents", "cy")},
                   "bbox_pt": _pt(ln["bbox"]), "glyph_h_pt": round(ln["glyph_h"] * ppx, 3),
                   "cy_pt": _pt((ln["bbox"][0], ln["cy"], ln["bbox"][2], ln["cy"]))[1],
                   "stroke_pt": round(ln["stroke_px"] * ppx, 3),
                   "accents": [{"color": a_["color"],
                                "x_pt": [[_pt((u, ln["bbox"][1], v, ln["bbox"][3]))[0],
                                          _pt((u, ln["bbox"][1], v, ln["bbox"][3]))[2]] for u, v in a_["x"]]}
                               for a_ in ln["accents"]]}
                  for ln in lay["lines"]],
        "dividers_pt": [_pt(d) for d in lay["dividers"]],
        # 영역을 글자 상자로 좁혔으면 그 상자 (한글 넣기가 영역 대신 이 안에 넣는다)
        **({"panel_pt": _pt(lb)} if lb != tr.lb_region else {}),
        # 지운 글 속 아이콘: 이름, 자리, 그 줄 글자 높이 대비 크기 (한글 넣기가 원본 크기로 다시 그린다)
        "icons": [{"name": n_, "bbox_pt": _pt(bx), "scale": sc} for n_, bx, sc in tr.icons_found],
        # 아이콘 문단만 원본 그대로 둔 칸: 남긴 줄은 keep. 남길 줄을 찾지 못했으면 keep_failed (영역을 지우지 않음,
        # 한글 넣기도 건너뛴다)
        **({"keep_paragraphs": True} if (keep_lines or {}).get(name) is not None else {}),
        **({"keep_failed": True} if lay.get("keep_failed") else {}),
    }


def _erase_place_patches(ctx: _PageCtx, cs: _CleanStage, params: CleanParams | None) -> list[dict]:
    """4. 패치 덮기: 원본 배경 이미지(JPEG 스트림)는 바이트 그대로 두고,
    영역(work_rect) 크기의 무손실 패치를 배경 이미지 픽셀 격자에 정확히 맞춰 위에 그린다.
    마스크는 작업 영역보다 dilate_px까지 넓어질 수 있으므로 그만큼(+2) 넓혀 자른다."""
    size = ctx.size
    p_ = params or CleanParams()
    patches = []
    for m_ in ctx.region_metas:
        x0, y0, x1, y1 = cs.work_px[m_["name"]]
        pad = p_.dilate_px + 2
        box = (max(0, x0 - pad), max(0, y0 - pad), min(size[0], x1 + pad), min(size[1], y1 + pad))
        sub_new = cs.new_img[box[1] : box[3], box[0] : box[2]]
        if np.array_equal(sub_new, ctx.raw[box[1] : box[3], box[0] : box[2]]):
            continue
        patch = place_patch(ctx.page, sub_new, box, ctx.bg)
        patch["region"] = m_["name"]
        patches.append(patch)
    return patches


def _erase_redraw_labels(ctx: _PageCtx) -> None:
    """5. 잰 태그 라벨을 같은 모양의 벡터 둥근 사각형으로 다시 그린다 (글자 없음)."""
    import pymupdf

    for m_ in ctx.region_metas:
        for lab in m_["tag_labels"] or []:
            r = pymupdf.Rect(lab["bbox_pt"])
            rad = min(0.5, lab["radius_pt"] / max(1e-6, min(r.width, r.height)))
            ctx.page.draw_rect(r, color=None, fill=hex_to_rgb01(lab["color"]), width=0,
                               radius=rad if rad > 0.01 else None)


def _erase_region_stats(ctx: _PageCtx, cs: _CleanStage, removed_inline: list[Box], text_mask_meta: dict,
                        layout_meta: dict) -> None:
    """영역별 통계(정리 방식, 바뀐 픽셀 비율, 확인 표시)를 region_metas에 쓴다."""
    res, crop = cs.res, cs.crop
    cx0, cy0, cx1, cy1 = crop
    lum_before = luminance(ctx.raw[cy0:cy1, cx0:cx1])
    lum_after = luminance(res.image)
    changed = np.abs(lum_after - lum_before) > 25
    evidence = cv2.dilate((res.strong | cs.forced_m).astype(np.uint8), _disk(4)).astype(bool)
    for m_ in ctx.region_metas:
        b = cs.local(cs.work_px[m_["name"]])
        sl = (slice(max(0, b[1]), b[3]), slice(max(0, b[0]), b[2]))
        wa = max(1, (b[3] - b[1]) * (b[2] - b[0]))
        flat_n = int(res.flat_mask[sl].sum())
        inp_n = int(res.inpaint_mask[sl].sum())
        bg_change = float((changed[sl] & ~evidence[sl]).sum()) / wa
        strong_outside_spans = float((res.strong[sl] & ~cs.span_m[sl]).sum()) / wa
        img_text = strong_outside_spans > 0.005
        missing = (not m_["text_layer"]["present"] and img_text) or any(
            not l_["in_text_layer"] for l_ in (m_["tag_labels"] or [])
        )
        if flat_n and inp_n:
            method = "mixed"
        elif inp_n:
            method = "inpaint"
        elif flat_n:
            method = "flat_gradient"
        else:
            method = "none"
        flags = []
        if m_["name"] in text_mask_meta:
            bg_change = 0.0  # 글자 픽셀만 바꿨다 (바뀐 픽셀 = 지울 픽셀)
        if bg_change > 0.02:
            flags.append("배경 변화 큼")
        m_["work_rect_px"] = list(cs.work_px[m_["name"]])
        if m_["name"] in cs.keep_meta:
            m_["kept_large_ink_px"] = cs.keep_meta[m_["name"]]
        if m_["name"] in ctx.keep_above_px:
            m_["kept_above_text_boxes_px"] = [list(map(int, b)) for b in ctx.keep_above_px[m_["name"]]]
        m_["missing_from_text_layer"] = bool(missing)
        m_["image_text_detected"] = bool(img_text)
        if m_["name"] in text_mask_meta:
            tmm = text_mask_meta[m_["name"]]
            method = f"text_mask:{tmm['used']}"
            m_["text_layout"] = layout_meta.get(m_["name"])
            if tmm["needs_choice"]:
                flags.append("그림 배경: 지우기 방식 확인")
            if tmm.get("picture_bg") and tmm["mask_ratio"] > 0:
                flags.append(PICTURE_BG_WARNING)
            if tmm.get("few_text"):
                flags.append("글자를 거의 못 찾음: 영역 위치 확인")
        m_["erase"] = {
            "text_mask": text_mask_meta.get(m_["name"]),
            "redacted_spans": len(m_["text_layer"]["spans"]),
            "removed_inline_images": sum(
                1 for rb in removed_inline if overlap_ratio(rb, tuple(m_["rect_pt"])) > 0.5
            ),
            "method": method,
            "flat_px": flat_n,
            "inpaint_px": inp_n,
            "mask_ratio": round(float(res.mask[sl].sum()) / wa, 4),
            "background_change_ratio": round(bg_change, 4),
            "strong_px_outside_text_layer_ratio": round(strong_outside_spans, 4),
            "flags": flags,
        }
