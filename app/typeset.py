"""한글 배치 (PRD 5.5, 5.6 합성, 5.7).

지운 PDF 페이지의 영역(rect, pt)에 번역 한글을 벡터 텍스트로 그린다.

공개 함수
    typeset_region(page, rect, text, style, *, source_text=None, top_anchor=None, draw=True)
        -> TypesetResult
    typeset_tag(page, label_rect, text, style, *, label_color=None, radius=None,
                draw_label=True, grow="right", draw=True) -> TypesetResult
    typeset_tags(page, labels, texts, style, **kw) -> list[TypesetResult]
    TextStyle.from_region_style(style_dict, base_dir=..., **overrides) -> TextStyle
    finalize(doc)  # 저장 직전에 호출: 쓴 글자만 남기도록 글꼴 서브셋

줄바꿈·금칙·태그·크기 맞추기의 순수 계산은 app.typeset_layout,
F28 잉크 높이 측정은 app.typeset_ink에 있다.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import pymupdf

from app.typeset_ink import DEFAULT_KO_SAMPLE, _glyph_ink, font_ink_ratio, start_size_for_ink
from app.typeset_layout import (
    STATUS_EMPTY,
    STATUS_ERASE_ONLY,
    STATUS_ICON_ERROR,
    STATUS_MISSING_GLYPH,
    STATUS_TAG_MISMATCH,
    Atom,
    FitParams,
    FitResult,
    Fmt,
    check_tags,
    fit_text,
    icon_names,
    parse_markup,
    to_atoms,
)

_ANY_ICON = re.compile(r"\[\[([^\[\]]*)\]\]")
_ICON_NAME = re.compile(r"^[a-z0-9_]+$")

__all__ = [
    "TextStyle", "TypesetResult", "FontMeasurer", "typeset_region", "typeset_tag", "typeset_tags",
    "finalize", "font_ink_ratio", "start_size_for_ink",
]

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- 스타일
def hex_to_rgb01(color: str) -> tuple[float, float, float]:
    c = color.lstrip("#")
    return tuple(int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


@dataclass
class TextStyle:
    font_file: str
    color: str = "#000000"
    align: str = "left"  # left | center | right
    valign: str = "top"  # top | middle | bottom (top_anchor가 있으면 무시)
    size_pt: float | None = None  # 원문 크기 (텍스트 레이어)
    ink_height_pt: float | None = None  # 원문 대표 잉크 높이 (F28). 있으면 이것으로 시작 크기를 정함
    ink_sample: str = DEFAULT_KO_SAMPLE
    fixed_size_pt: float | None = None  # 엑셀 I열(글자 크기)
    line_pitch_pt: float | None = None  # 원문 줄 간격 (시작 크기 기준)
    min_ratio: float = 0.8
    floor_pt: float = 6.0  # floor_basis="pt"일 때만 쓴다
    floor_basis: str = "ink"  # ink: 원문 글자 높이의 min_ratio까지 (잉크 높이를 모를 때는 pt 규칙) | pt
    tracking: float = 0.0  # 영역 기본 자간 (글자 크기 대비, 예: -0.06). 맞추기는 여기서 -max_tracking까지 더 좁힌다
    max_tracking: float = 0.05
    italic: bool = False  # 영역 기본 서식이 기울임
    bold: bool = False  # 영역(줄) 기본 서식이 굵게 (원문 줄 모양에서)
    fallback_file: str | None = None  # 주 글꼴에 없는 글자(♥, … 등)를 그릴 글꼴. 굵게는 이웃 굵기 파일을 찾는다
    bold_file: str | None = None  # <b>용 글꼴. 없으면 이웃 굵기 파일 -> 없으면 가짜 굵게(획 덧그리기)
    fake_bold_width: float = 0.05  # 가짜 굵게 획 두께 (글자 크기 대비, 외곽선 경로에서만 사용)
    italic_angle: float = 12.0
    tag_colors: dict = field(default_factory=dict)  # {"c1": "#aa0000"}
    stroke_color: str | None = None  # 외곽선 (원문에 있을 때만)
    stroke_width: float = 0.0  # 글자 크기 대비
    icons_dir: str | None = None
    icon_scale: float = 1.1  # 글자(한글 잉크) 높이 대비
    icon_scales: dict = field(default_factory=dict)  # 아이콘 사전의 아이콘별 표시 크기 {이름: 배율}
    # 180이면 글자를 pivot(pt) 둘레로 180도 돌려 그린다 (카드 위 반쪽처럼 거꾸로 인쇄된 영역).
    # 배치 계산은 돌린 틀(frame) 좌표에서 하고, 그릴 때만 돌린다.
    rotate: int = 0
    pivot: tuple | None = None
    respect_manual: bool = True

    @classmethod
    def from_region_style(cls, style: dict, base_dir: str | Path = ROOT, **overrides) -> "TextStyle":
        """regions.json의 style(또는 추출 JSON의 style_defaults)에서 만든다."""
        font = style.get("font") or {}
        ff = font.get("file") or "fonts/notosanskr/NotoSansKR-Regular.ttf"
        p = Path(ff)
        if not p.is_absolute():
            p = Path(base_dir) / p
        kw = dict(
            font_file=str(p),
            color=style.get("color", "#000000"),
            align=style.get("align", "left"),
            italic=bool(style.get("italic", False)),
        )
        fb = font.get("fallback_file", "fonts/notosanskr/NotoSansKR-Bold.ttf")
        if fb:
            fp = Path(fb)
            kw["fallback_file"] = str(fp if fp.is_absolute() else Path(base_dir) / fp)
        if font.get("bold_file"):
            bp = Path(font["bold_file"])
            kw["bold_file"] = str(bp if bp.is_absolute() else Path(base_dir) / bp)
        for k in ("tag_colors", "size_pt", "line_pitch_pt", "fixed_size_pt", "min_ratio", "floor_pt",
                  "floor_basis", "tracking", "rotate", "icons_dir", "icon_scales"):
            if k in style:
                kw[k] = style[k]
        kw.update(overrides)
        return cls(**kw)

    def floor_for_fit(self) -> float | None:
        """FitParams.floor_pt: 원문 글자 높이 기준이고 잉크 높이를 알면 None(pt 하한 없음)."""
        if self.floor_basis == "ink" and self.ink_height_pt:
            return None
        return self.floor_pt

    def start_size(self) -> float:
        """시작 크기: 고정 크기 > 잉크 높이 보정(F28) > 원문 pt."""
        if self.fixed_size_pt:
            return float(self.fixed_size_pt)
        if self.ink_height_pt:
            return start_size_for_ink(self.ink_height_pt, self.font_file, self.ink_sample)
        if self.size_pt:
            return float(self.size_pt)
        raise ValueError("size_pt, ink_height_pt, fixed_size_pt 중 하나는 있어야 합니다")


# ---------------------------------------------------------------- 글꼴 메트릭
@lru_cache(maxsize=64)
def _font(path: str) -> pymupdf.Font:
    return pymupdf.Font(fontfile=path)


@lru_cache(maxsize=64)
def _bold_variant(path: str) -> str | None:
    p = Path(path)
    for a, b in (("Regular", "Bold"), ("Bold", "Black")):
        if a in p.name:
            q = p.with_name(p.name.replace(a, b))
            if q.exists():
                return str(q)
    return None


class FontMeasurer:
    """실제 글꼴 메트릭으로 폭·잉크 높이를 잰다 (app.typeset_layout.Measurer 구현)."""

    def __init__(self, style: TextStyle):
        self.style = style

    def file_for(self, fmt: Fmt) -> tuple[str, bool]:
        """(글꼴 파일, 가짜 굵게 여부)"""
        if fmt.bold or self.style.bold:
            bf = self.style.bold_file or _bold_variant(self.style.font_file)
            if bf:
                return bf, False
            return self.style.font_file, True
        return self.style.font_file, False

    def file_for_char(self, ch: str, fmt: Fmt) -> tuple[str, bool]:
        """글자 하나를 그릴 (글꼴 파일, 가짜 굵게). 주 글꼴에 없으면 대체 글꼴을 쓴다."""
        ff, fake = self.file_for(fmt)
        fb = self.style.fallback_file
        if fb and not _font(ff).has_glyph(ord(ch)) and Path(fb).exists():
            if fmt.bold or self.style.bold:
                bv = _bold_variant(fb)
                if bv:
                    return bv, False
            if _font(fb).has_glyph(ord(ch)):
                return fb, fake
        return ff, fake

    def hangul_ink(self) -> tuple[float, float]:
        g = _glyph_ink(self.style.font_file, "한") or (0.8, 0.0)
        return g

    def icon_size(self, name: str, size: float) -> tuple[float, float]:
        top, bottom = self.hangul_ink()
        h = self.style.icon_scales.get(name, self.style.icon_scale) * (top + bottom) * size
        aspect = 1.0
        path = self.icon_path(name)
        if path and path.exists():
            try:
                pm = pymupdf.Pixmap(str(path))
                aspect = pm.width / pm.height
            except Exception:  # noqa: BLE001
                pass
        return h * aspect, h

    def icon_path(self, name: str) -> Path | None:
        if not self.style.icons_dir:
            return None
        return Path(self.style.icons_dir) / f"{name}.png"

    def advance(self, atom: Atom, size: float) -> float:
        if atom.kind == "icon":
            return self.icon_size(atom.text, size)[0]
        ch = " " if atom.kind == "space" else atom.text
        ff, _ = self.file_for_char(ch, atom.fmt)
        return _font(ff).glyph_advance(ord(ch)) * size

    def ink_extent(self, atoms: list[Atom]) -> tuple[float, float]:
        top = bottom = 0.0
        chars = {(self.file_for_char(a.text, a.fmt)[0], a.text) for a in atoms if a.kind == "char"}
        if not chars:
            chars = {(self.style.font_file, "한")}
        for ff, ch in chars:
            g = _glyph_ink(ff, ch)
            if g:
                top, bottom = max(top, g[0]), max(bottom, g[1])
        if any(a.kind == "icon" for a in atoms):
            ht, hb = self.hangul_ink()
            mid = (ht - hb) / 2
            scale = max([self.style.icon_scales.get(a.text, self.style.icon_scale)
                         for a in atoms if a.kind == "icon"])
            half = scale * (ht + hb) / 2
            top, bottom = max(top, mid + half), max(bottom, half - mid)
        return top, bottom

    def missing_glyphs(self, atoms: list[Atom]) -> list[str]:
        out = []
        for a in atoms:
            if a.kind == "char":
                ff, _ = self.file_for_char(a.text, a.fmt)
                if not _font(ff).has_glyph(ord(a.text)) and a.text not in out:
                    out.append(a.text)
        return out


# ---------------------------------------------------------------- 결과
@dataclass
class TypesetResult:
    status: list[str]
    size: float | None = None  # 최종 글자 크기 (pt)
    start_size: float | None = None
    min_size: float | None = None
    tracking: float = 0.0  # 글자 크기 대비 (0 ~ -0.05)
    lines: list[str] = field(default_factory=list)
    line_boxes: list[tuple[float, float, float, float]] = field(default_factory=list)  # 줄별 잉크 영역 (pt)
    overflow_text: str = ""
    errors: list[str] = field(default_factory=list)
    label_rect: tuple[float, float, float, float] | None = None  # 태그 모드: 최종 라벨 (늘었을 수 있음)
    drawn: bool = False

    def to_dict(self) -> dict:
        r = lambda v: None if v is None else round(v, 3)  # noqa: E731
        return {
            "status": self.status, "size_pt": r(self.size), "start_size_pt": r(self.start_size),
            "min_size_pt": r(self.min_size), "tracking": round(self.tracking, 3), "lines": self.lines,
            "line_boxes_pt": [[round(v, 2) for v in b] for b in self.line_boxes],
            "overflow_text": self.overflow_text, "errors": self.errors,
            "label_rect_pt": None if self.label_rect is None else [round(v, 2) for v in self.label_rect],
            "drawn": self.drawn,
        }


# ---------------------------------------------------------------- 그리기
def _font_name(page: pymupdf.Page, font_file: str) -> str:
    name = "KT" + hashlib.md5(font_file.encode("utf-8")).hexdigest()[:8]
    cache = page.parent.__dict__.setdefault("_kt_fonts", set())
    key = (page.number, name)
    if key not in cache:
        page.insert_font(fontname=name, fontfile=font_file)
        cache.add(key)
    return name


def _rotation(style: TextStyle):
    """TextWriter·insert_text의 morph (180도 회전) 또는 None."""
    if not style.rotate:
        return None
    if style.rotate % 360 != 180:
        raise NotImplementedError(f"회전은 180도만 지원합니다: {style.rotate}")
    if style.pivot is None:
        raise ValueError("rotate=180이면 pivot(돌릴 중심 pt)이 있어야 합니다")
    return pymupdf.Point(*style.pivot), pymupdf.Matrix(-1, 0, 0, -1, 0, 0)


def _rotate_rect(r, pivot) -> pymupdf.Rect:
    """사각형을 pivot 둘레로 180도 돌린 사각형."""
    px, py = pivot
    r = pymupdf.Rect(r)
    return pymupdf.Rect(2 * px - r.x1, 2 * py - r.y1, 2 * px - r.x0, 2 * py - r.y0)


def _draw_line(page, atoms: list[Atom], x: float, baseline: float, size: float, tracking: float,
               style: TextStyle, m: FontMeasurer) -> None:
    """한 줄을 그린다. 글자는 글자마다 위치를 정해(자간) TextWriter로 벡터 텍스트로 넣는다.

    - 기울임: 기준선을 고정점으로 한 shear 행렬 (PDF 좌표, 위로 갈수록 오른쪽으로 tan(12도)).
      (insert_text의 morph는 cm을 BT 안에 넣어 무시되므로 TextWriter를 쓴다.)
    - 가짜 굵게: 같은 색 획 덧그리기(render_mode 2). 획 두께는 PyMuPDF 고정값 0.05 x 글자 크기.
    - 외곽선(stroke_color)은 insert_text(border_width)로 그린다 (이때 기울임은 지원 안 함).
    """
    t_shear = math.tan(math.radians(style.italic_angle))
    rot = _rotation(style)
    cur = None  # (key, TextWriter, x0): 같은 서식이 이어지는 동안 한 번에 쓴다 (내용 순서 = 읽는 순서)

    def _flush(c) -> None:
        if c is None:
            return
        (color, fake_bold, italic), tw, x0 = c
        if italic and rot:
            raise NotImplementedError("기울임 + 180도 회전 영역은 아직 지원하지 않습니다")
        morph = (pymupdf.Point(x0, baseline), pymupdf.Matrix(1, 0, t_shear, 1, 0, 0)) if italic else rot
        tw.write_text(page, color=hex_to_rgb01(color), render_mode=2 if fake_bold else 0, morph=morph)

    for i, a in enumerate(atoms):
        adv = m.advance(a, size)
        if a.kind == "space" and i > 0 and atoms[i - 1].kind == "char":
            x += adv + tracking * size  # 공백은 앞 글자와 함께 그렸다 (텍스트 추출에 공백이 남도록)
            continue
        italic = style.italic or a.fmt.italic
        if a.kind == "icon":
            w, h = m.icon_size(a.text, size)
            ht, hb = m.hangul_ink()
            cy = baseline - (ht - hb) / 2 * size
            p = m.icon_path(a.text)
            if p and p.exists():
                r_icon = pymupdf.Rect(x, cy - h / 2, x + w, cy + h / 2)
                if rot:
                    r_icon = _rotate_rect(r_icon, style.pivot)
                page.insert_image(r_icon, filename=str(p), rotate=180 if rot else 0)
        elif a.kind == "char":
            ff, fake_bold = m.file_for_char(a.text, a.fmt)
            color = style.color
            if a.fmt.color_tag:
                color = style.tag_colors.get(a.fmt.color_tag, style.color)
            s = a.text
            if i + 1 < len(atoms) and atoms[i + 1].kind == "space":
                s += " "
            if style.stroke_color and style.stroke_width:
                # 외곽선은 글자 바깥으로: 두꺼운 획(외곽선 색)을 먼저 그리고 그 위에 글자를 채운다
                fb_w = style.fake_bold_width if fake_bold else 0
                base: dict = dict(fontname=_font_name(page, ff), fontsize=size)
                if rot:
                    base["morph"] = rot
                sc = hex_to_rgb01(style.stroke_color)
                page.insert_text(pymupdf.Point(x, baseline), s, render_mode=2, color=sc, fill=sc,
                                 border_width=2 * style.stroke_width + fb_w, **base)
                if fake_bold:
                    page.insert_text(pymupdf.Point(x, baseline), s, render_mode=2, color=hex_to_rgb01(color),
                                     fill=hex_to_rgb01(color), border_width=fb_w, **base)
                else:
                    page.insert_text(pymupdf.Point(x, baseline), s, render_mode=0, color=hex_to_rgb01(color), **base)
            else:
                key = (color, fake_bold, italic)
                if cur is None or cur[0] != key:
                    _flush(cur)
                    cur = (key, pymupdf.TextWriter(page.rect), x)
                cur[1].append(pymupdf.Point(x, baseline), s, font=_font(ff), fontsize=size)
        x += adv + tracking * size
    _flush(cur)


def _prepare(text, style: TextStyle, source_text, m: FontMeasurer):
    """공통 전처리. (atoms, status, errors) 또는 조기 종료용 TypesetResult."""
    if text is None or text.strip() == "":
        return TypesetResult([STATUS_EMPTY])
    if text.strip() == "-":
        return TypesetResult([STATUS_ERASE_ONLY])
    status: list[str] = []
    errors = check_tags(text, source_text)
    spans, _ = parse_markup(text)
    atoms = to_atoms(spans)
    unknown_colors = sorted({a.fmt.color_tag for a in atoms if a.fmt.color_tag and a.fmt.color_tag not in style.tag_colors})
    if unknown_colors:
        errors.append("색이 정해지지 않은 태그 " + ", ".join(unknown_colors))
    if errors:
        status.append(STATUS_TAG_MISMATCH)
    unnamed = [t for t in _ANY_ICON.findall(text) if not _ICON_NAME.match(t)]
    if unnamed:  # 이름이 정해지지 않은 아이콘([[?]])을 글자로 찍지 않는다
        return TypesetResult(status + [STATUS_ICON_ERROR],
                             errors=errors + [f"이름 없는 아이콘: [[{t}]] (아이콘 검증을 먼저 하세요)" for t in unnamed])
    missing_icons = [n for n in icon_names(text) if not (m.icon_path(n) and m.icon_path(n).exists())]
    if missing_icons:
        return TypesetResult(status + [STATUS_ICON_ERROR],
                             errors=errors + [f"아이콘 사전에 없음: [{n}]" for n in missing_icons])
    missing = m.missing_glyphs(atoms)
    if missing:
        status.append(STATUS_MISSING_GLYPH)
        errors.append("글꼴에 없는 글자: " + "".join(missing))
    return atoms, status, errors


def typeset_region(page: pymupdf.Page, rect, text: str | None, style: TextStyle, *,
                   source_text: str | None = None, top_anchor: float | None = None,
                   draw: bool = True) -> TypesetResult:
    """영역 rect(pt) 안에 text를 맞춰 그린다.

    - rect: 글자가 들어갈 상자. 왼쪽 정렬이면 rect.x0이 첫 글자 시작점이다.
    - top_anchor: 첫 줄 잉크 위끝을 맞출 y(pt). 원문 첫 줄 위끝을 넘기면 세로 위치가 원문과 같아진다.
      없으면 style.valign으로 rect 안에 놓는다.
    - source_text: 원문(태그 종류·개수 비교용, 선택).
    - draw=False면 계산만 한다 (미리보기·크기 통일 계산용).
    """
    rect = pymupdf.Rect(rect)
    m = FontMeasurer(style)
    prep = _prepare(text, style, source_text, m)
    if isinstance(prep, TypesetResult):
        return prep
    atoms, status, errors = prep
    start = style.start_size()
    box_h = rect.height if top_anchor is None else rect.y1 - top_anchor
    params = FitParams(
        box_w=rect.width, box_h=box_h, start_size=start, fixed_size=style.fixed_size_pt,
        min_ratio=style.min_ratio, floor_pt=style.floor_for_fit(), max_tracking=style.max_tracking,
        base_tracking=style.tracking,
        line_pitch=style.line_pitch_pt, respect_manual=style.respect_manual,
    )
    fit: FitResult = fit_text(atoms, params, m)
    size, t = fit.size, fit.tracking
    pitch = params.pitch(size)
    block_h = fit.block_height(pitch)
    if top_anchor is not None:
        top = top_anchor
    elif style.valign == "middle":
        top = rect.y0 + (rect.height - block_h) / 2
    elif style.valign == "bottom":
        top = rect.y1 - block_h
    else:
        top = rect.y0
    baseline0 = top + fit.ink_top * size
    boxes = []
    for i, ln in enumerate(fit.lines):
        bl = baseline0 + i * pitch
        if style.align == "center":
            x = rect.x0 + (rect.width - ln.width) / 2
        elif style.align == "right":
            x = rect.x1 - ln.width
        else:
            x = rect.x0
        boxes.append((float(x), float(bl - fit.ink_top * size), float(x + ln.width), float(bl + fit.ink_bottom * size)))
        if draw:
            _draw_line(page, ln.atoms, x, bl, size, t, style, m)
    return TypesetResult(
        status=status + fit.status, size=size, start_size=fit.start_size, min_size=fit.min_size,
        tracking=t, lines=fit.line_texts, line_boxes=boxes, overflow_text=fit.overflow_text,
        errors=errors, drawn=draw,
    )


def typeset_tag(page: pymupdf.Page, label_rect, text: str | None, style: TextStyle, *,
                label_color: str | None = None, radius: float | None = None, draw_label: bool = True,
                min_pad: float | None = None, grow: str = "right", draw: bool = True) -> TypesetResult:
    """태그: 색 라벨 rect 안 가운데에 한 줄로 그린다.

    글자를 줄이지 않는다. 한글이 라벨보다 길면 라벨 폭을 늘린다(grow: right | center).
    늘어난 라벨은 result.label_rect로 돌려준다 (다음 라벨을 밀 때 쓴다).
    draw_label=True면 label_color·radius로 라벨(둥근 사각형)도 다시 그린다.
    """
    lr = pymupdf.Rect(label_rect)
    m = FontMeasurer(style)
    prep = _prepare(text, style, None, m)
    if isinstance(prep, TypesetResult):
        prep.label_rect = tuple(lr)
        return prep
    atoms, status, errors = prep
    atoms = [a if a.kind != "newline" else Atom("space", " ", a.fmt) for a in atoms]
    size = style.start_size()
    width = sum(m.advance(a, size) for a in atoms)
    top, bottom = m.ink_extent(atoms)
    pad = min_pad if min_pad is not None else 0.45 * size
    need = width + 2 * pad
    if need > lr.width:
        extra = need - lr.width
        if grow == "center":
            lr = pymupdf.Rect(lr.x0 - extra / 2, lr.y0, lr.x1 + extra / 2, lr.y1)
        else:
            lr = pymupdf.Rect(lr.x0, lr.y0, lr.x1 + extra, lr.y1)
    x = lr.x0 + (lr.width - width) / 2
    cy = (lr.y0 + lr.y1) / 2
    baseline = cy + (top - bottom) / 2 * size
    if draw:
        if draw_label and label_color:
            r = radius if radius is not None else lr.height / 2
            rr = (min(0.5, r / lr.width), min(0.5, r / lr.height))
            page.draw_rect(lr, color=None, fill=hex_to_rgb01(label_color), radius=rr, width=0)
        _draw_line(page, atoms, x, baseline, size, 0.0, style, m)
    return TypesetResult(
        status=status, size=size, start_size=size, min_size=size, tracking=0.0,
        lines=["".join(a.display for a in atoms)],
        line_boxes=[(float(x), float(baseline - top * size), float(x + width), float(baseline + bottom * size))],
        errors=errors, label_rect=tuple(lr), drawn=draw,
    )


def typeset_tags(page: pymupdf.Page, labels: list[dict], texts: list[str], style: TextStyle,
                 **kw) -> list[TypesetResult]:
    """여러 태그. labels: [{"bbox_pt": [x0,y0,x1,y1], "color": "#...", "radius_pt": r}, ...] (왼쪽부터).

    라벨이 늘어나면 오른쪽 라벨들을 같은 만큼 밀어 라벨 사이 간격을 유지한다.
    라벨 수와 번역 수가 다르면 짧은 쪽에 맞추고 errors에 남긴다.
    """
    labels = sorted(labels, key=lambda d: d["bbox_pt"][0])
    out: list[TypesetResult] = []
    shift = 0.0
    for i, (lab, text) in enumerate(zip(labels, texts)):
        x0, y0, x1, y1 = lab["bbox_pt"]
        rect = (x0 + shift, y0, x1 + shift, y1)
        res = typeset_tag(page, rect, text, style, label_color=lab.get("color"),
                          radius=lab.get("radius_pt"), **kw)
        if res.label_rect:
            shift += (res.label_rect[2] - rect[2])
        out.append(res)
    if len(labels) != len(texts) and out:
        out[-1].errors.append(f"라벨 {len(labels)}개, 번역 {len(texts)}개로 수가 다름")
    return out


def finalize(doc: pymupdf.Document) -> None:
    """저장 직전에 호출한다. 쓴 글자만 남기도록 내장 글꼴을 서브셋한다."""
    doc.subset_fonts()
