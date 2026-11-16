"""벡터 인쇄 PDF(배경 스캔 이미지가 없는 쪽)의 글자 지우기.

디자인 툴에서 바로 나온 인쇄용 PDF는 쪽 전체를 덮는 스캔 이미지가 없다. 그림은 벡터 도형·작은 이미지 여러 장이고
글자는 진짜 텍스트 레이어(글꼴 + span)다. 그래서 이미지를 고치지 않고 텍스트 레이어의 글자만 지운다:

1. 영역에 든 span을 레드랙션한다 (글자만 지움, 이미지·벡터 도형은 그대로, 채우기 없음). 아래 그림이 그대로 남는다.
2. 지우기 전·후를 같은 해상도(VECTOR_DPI)로 렌더링해 차이(= 지운 글자 픽셀)로 줄 모양(text_layout)을 잰다.
   한글 넣기가 그림 카드와 똑같이 원문 줄 자리·크기·색을 따른다. 글자색은 span 색(정확한 값)으로 바꾼다.
3. 영역에 span이 없으면(글자가 윤곽선 도형이거나 그림에 박힘) 쪽을 그림으로 렌더링한 임시 쪽에서 그림 카드 방식
   (app.erase.erase_page)으로 지우고, 바뀐 조각(패치)만 원래 쪽 위에 무손실 이미지로 덮는다.

배경 이미지가 없으므로 메타의 background_image는 렌더링 격자를 적는다 ("virtual": true).
app.erase.erase_page가 배경 이미지를 찾지 못한 쪽에서만 부른다: 스캔·이미지 PDF는 예전 그대로다.
"""

from __future__ import annotations

import copy
import re

import cv2
import numpy as np

from app.regions import Box, RegionTemplate, overlap_ratio, px_box_outer, ratio_to_pt, union

VECTOR_DPI = 300.0  # 렌더링 격자 (비교 그림·줄 모양 재기·그림 방식 대체 경로)
GLYPH_DIFF = 10  # 지우기 전·후 렌더 차이가 이보다 크면 글자 픽셀
BG_CHANGE_FLAG = 0.02  # 글자 둘레 밖 픽셀이 이 비율 넘게 바뀌면 '배경 변화 큼'
PATCH_INSET_PT = 0.002  # app.erase.PATCH_INSET_PT와 같은 이유 (grid-fit 바깥 번짐 방지)
EFFECT_PAD_PT = 8.0  # 글자 효과(그림자·광선) 폼을 찾을 때 지운 글자 상자 둘레 여유 (흐림 반경)
EFFECT_INSIDE = 0.85  # 효과 폼 상자 중 이 비율 이상이 글자 상자(+여유) 안이어야 지운다
EFFECT_MAX_GROW = 1.6  # 효과 폼 상자가 글자 상자(+여유)보다 이만큼 넘게 넓으면 그림으로 보고 남긴다


def render_page(page, dpi: float = VECTOR_DPI) -> np.ndarray:
    """쪽 전체를 RGB 배열로 렌더링한다 (쪽 왼쪽 위 = 픽셀 (0, 0))."""
    import pymupdf

    k = dpi / 72.0
    pix = page.get_pixmap(matrix=pymupdf.Matrix(k, k), clip=page.rect, alpha=False)
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[..., :3].copy()


def virtual_background(page, size: tuple[int, int]):
    """렌더링 격자를 배경 이미지처럼 다루는 가짜 BackgroundImage (xref 0, 쪽 전체)."""
    from app.erase import BackgroundImage

    x0, y0, x1, y1 = tuple(page.rect)
    return BackgroundImage(0, (x0, y0, x1, y1), size, (x1 - x0, 0.0, 0.0, y1 - y0, x0, y0))


def has_vector_text(page) -> bool:
    """쪽에 지울 수 있는 텍스트 레이어가 있는가 (공백 아닌 span)."""
    from app.erase import page_spans

    return bool(page_spans(page))


def _assign(spans: list[dict], rect_pt: Box, used: set[int]) -> tuple[list[dict], list[str]]:
    """영역에 든 span (app.erase.erase_page와 같은 기준)과 일부만 걸친 span 경고."""
    from app.erase import SPAN_ASSIGN_MIN, SPAN_PARTIAL_WARN, _r

    assigned, warn = [], []
    for i, s in enumerate(spans):
        r = overlap_ratio(s["bbox"], rect_pt)
        if r <= 0:
            continue
        if r >= SPAN_ASSIGN_MIN:
            if i in used:
                continue
            used.add(i)
            assigned.append(s)
            if r < SPAN_PARTIAL_WARN:
                warn.append(f"span {_r(s['bbox'], 1)} 이 영역에 {r:.0%}만 걸침 (전부 지움)")
        else:
            warn.append(f"span {_r(s['bbox'], 1)} 이 영역에 {r:.0%}만 걸쳐 지우지 않음")
    return assigned, warn


def _line_color(ln_pt: list[float], spans: list[dict]) -> str | None:
    """줄 상자와 세로로 가장 많이 겹치는 span들의 대표 색 (글자 수 기준)."""
    tally: dict[str, int] = {}
    for s in spans:
        b = s["bbox"]
        ov = min(b[3], ln_pt[3]) - max(b[1], ln_pt[1])
        if ov <= 0.4 * max(1e-6, min(b[3] - b[1], ln_pt[3] - ln_pt[1])):
            continue
        if min(b[2], ln_pt[2]) <= max(b[0], ln_pt[0]):
            continue
        tally[s["color"]] = tally.get(s["color"], 0) + s["chars"]
    return max(tally.items(), key=lambda kv: kv[1])[0] if tally else None


def layout_from_diff(before: np.ndarray, glyph: np.ndarray, box_px: tuple[int, int, int, int], bg,
                     spans: list[dict]) -> dict:
    """지운 글자 픽셀(glyph)로 원문 줄 모양을 잰다 (app.erase_text.text_layout, 좌표는 pt).
    글자색은 span 색으로 바꾼다 (렌더 픽셀 중앙값보다 정확하다)."""
    from app import erase_text

    H, W = glyph.shape
    x0, y0, x1, y1 = box_px
    inbox = np.zeros((H, W), bool)
    inbox[max(0, y0):y1, max(0, x0):x1] = True
    core = glyph & inbox
    sizes = sorted(s["size"] for s in spans)
    gpx = (sizes[len(sizes) // 2] * 0.7 / bg.pt_per_px) if sizes else 10.0
    mask = cv2.dilate(core.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
    tm = erase_text.TextMask(mask=mask, core=core, busy=False, texture=0.0, glyph_px=float(gpx))
    lay = erase_text.text_layout(before, tm, box_px)
    ppx = bg.pt_per_px

    def _pt(bx):
        q = bg.px_to_pt(tuple(float(v) for v in bx))
        return [round(v, 3) for v in q]

    lines = []
    for ln in lay["lines"]:
        bpt = _pt(ln["bbox"])
        col = _line_color(bpt, spans) or ln.get("color")
        lines.append({
            **{k: v for k, v in ln.items() if k not in ("bbox", "glyph_h", "stroke_px", "accents", "cy", "color")},
            "color": col,
            "bbox_pt": bpt, "glyph_h_pt": round(ln["glyph_h"] * ppx, 3),
            "cy_pt": _pt((ln["bbox"][0], ln["cy"], ln["bbox"][2], ln["cy"]))[1],
            "stroke_pt": round(ln["stroke_px"] * ppx, 3),
            "accents": [{"color": a_["color"],
                         "x_pt": [[_pt((u, ln["bbox"][1], v, ln["bbox"][3]))[0],
                                   _pt((u, ln["bbox"][1], v, ln["bbox"][3]))[2]] for u, v in a_["x"]]}
                        for a_ in ln["accents"]],
        })
    return {"lines": lines, "dividers_pt": [_pt(d) for d in lay["dividers"]], "icons": [], "source": "text_layer"}


_EFFECT_DO = re.compile(rb"/(\w+)\s+gs\s+(?:[-\d.]+\s+TL\s*)?/(\w+)\s+Do")


def remove_text_effects(page, text_boxes: list[Box]) -> list[Box]:
    """지운 글자에 딸린 효과(InDesign 그림자·광선: 소프트 마스크를 쓴 폼 XObject)를 지운다.

    글자를 지워도 흐린 글자 모양 그림자가 남는다 (Darkest Curio). 쪽 내용에서 `/GS gs /Fm Do` 중 GS에 SMask가 있고
    폼 상자가 지운 글자 줄 상자(둘레 EFFECT_PAD_PT)에 거의 다 들고 그보다 크게 넓지 않은 것만 지운다 (그림은 남긴다).
    지운 폼 상자(pt) 목록."""
    import pymupdf

    if not text_boxes:
        return []
    doc = page.parent
    if len(page.get_contents()) != 1:
        page.clean_contents()
    xrefs = page.get_contents()
    if len(xrefs) != 1:
        return []
    zones = [(b[0] - EFFECT_PAD_PT, b[1] - EFFECT_PAD_PT, b[2] + EFFECT_PAD_PT, b[3] + EFFECT_PAD_PT) for b in text_boxes]
    data = doc.xref_stream(xrefs[0])
    removed: list[Box] = []

    def res_xref(kind: str, name: str) -> int:
        t, v = doc.xref_get_key(page.xref, f"Resources/{kind}/{name}")
        return int(v.split()[0]) if t == "xref" else 0

    def repl(m: re.Match) -> bytes:
        gs, fm = m.group(1).decode("latin-1"), m.group(2).decode("latin-1")
        gx, fx = res_xref("ExtGState", gs), res_xref("XObject", fm)
        if not gx or not fx or doc.xref_get_key(gx, "SMask")[0] != "xref":
            return m.group(0)
        if doc.xref_get_key(fx, "Subtype")[1] != "/Form":
            return m.group(0)
        t, bb = doc.xref_get_key(fx, "BBox")
        if t != "array":
            return m.group(0)
        r = pymupdf.Rect([float(v) for v in bb.strip("[] ").split()])
        mt, mv = doc.xref_get_key(fx, "Matrix")
        if mt == "array":
            r = r * pymupdf.Matrix(*[float(v) for v in mv.strip("[] ").split()])
        r = r * page.transformation_matrix
        r.normalize()
        box = tuple(r)
        for z in zones:
            if overlap_ratio(box, z) >= EFFECT_INSIDE and area_of(box) <= EFFECT_MAX_GROW * area_of(z):
                removed.append(box)
                return b"/" + m.group(1) + b" gs"
        return m.group(0)

    new = _EFFECT_DO.sub(repl, data)
    if removed:
        doc.update_stream(xrefs[0], new)
    return removed


def area_of(b: Box) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _insert_patch(page, pixels: np.ndarray, box_px: tuple[int, int, int, int], bg) -> dict:
    """그림 방식으로 지운 조각을 원래 쪽 위에 무손실 이미지로 덮는다 (렌더링 격자에 맞춤)."""
    import pymupdf

    h, w = pixels.shape[:2]
    pix = pymupdf.Pixmap(pymupdf.csRGB, w, h, np.ascontiguousarray(pixels).tobytes(), 0)
    rect = bg.px_to_pt(tuple(float(v) for v in box_px))
    eps = PATCH_INSET_PT
    xref = page.insert_image(pymupdf.Rect(rect[0] + eps, rect[1] + eps, rect[2] - eps, rect[3] - eps),
                             pixmap=pix, overlay=True, keep_proportion=False)
    return {"bbox_pt": [round(v, 4) for v in rect], "bbox_px": list(box_px), "size_px": [w, h], "xref": xref}


def _raster_fallback(page, template: RegionTemplate, regions: list, size: tuple[int, int], **kw):
    """span이 없는 영역(윤곽선 글자·그림에 박힌 글자): 쪽을 렌더링한 그림 한 장짜리 임시 쪽에서 그림 카드 방식으로
    지운다. (PageErase, 렌더 격자 기준 정리한 그림)."""
    import pymupdf

    from app.erase import erase_page

    k = VECTOR_DPI / 72.0
    pix = page.get_pixmap(matrix=pymupdf.Matrix(k, k), clip=page.rect, alpha=False)
    if (pix.w, pix.h) != size:
        raise RuntimeError(f"렌더 크기가 다름 {(pix.w, pix.h)} != {size}")
    tmp = pymupdf.open()
    try:
        tp = tmp.new_page(width=page.rect.width, height=page.rect.height)
        tp.insert_image(tp.rect, pixmap=pix, keep_proportion=False)
        sub = copy.copy(template)
        sub.regions = list(regions)
        res = erase_page(tp, sub, **kw)
    finally:
        tmp.close()
    return res


def erase_vector_page(page, template: RegionTemplate, sample: dict | None = None, seed: int = 0,
                      params=None, modes: dict | None = None, sources: dict | None = None,
                      icon_templates: dict | None = None, keep_lines: dict | None = None):
    """배경 이미지가 없는 벡터 쪽에서 영역 글자를 지운다 (app.erase.erase_page와 같은 PageErase·메타를 돌려준다)."""
    import pymupdf

    from app.erase import PageErase, _dominant, _r, page_spans

    warnings: list[str] = []
    prect: Box = tuple(page.rect)
    before = render_page(page)
    size = (before.shape[1], before.shape[0])
    bg = virtual_background(page, size)
    bg_meta = {"virtual": True, "kind": "vector", "bbox_pt": _r(prect), "size_px": list(size),
               "dpi": [round(v, 2) for v in bg.dpi], "stream_untouched": True}

    def base_meta(regions, patches):
        return {"page_rect_pt": _r(prect), "mediabox": _r(page.mediabox), "cropbox": _r(page.cropbox),
                "background_image": bg_meta, "patches": patches, "clean": None,
                "removed_inline_images_pt": [], "regions": regions, "warnings": warnings}

    if not template.regions:
        return PageErase(base_meta([], []), before, None, (0, 0, 0, 0), {}, warnings, before)

    spans = page_spans(page)
    used: set[int] = set()
    plan = []  # (영역, rect_pt, 맡은 span, 경고)
    for reg in template.regions:
        rect_pt = ratio_to_pt(reg.rect, prect)
        assigned, warn = _assign(spans, rect_pt, used)
        plan.append((reg, rect_pt, assigned, warn))

    # ---- 1. 레드랙션 (텍스트만: 이미지·벡터 도형은 그대로, 채우기 없음)
    redact = [s["bbox"] for _r_, _p, a, _w in plan for s in a]
    others = [s for i, s in enumerate(spans) if i not in used]
    for b in redact:
        page.add_redact_annot(pymupdf.Rect(b), fill=False, cross_out=False)
    if redact:
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_REMOVE)
    # 지운 글자에 딸린 그림자·광선 효과 (영역마다 지운 span을 감싼 상자 기준)
    after_text = render_page(page)  # 글자만 지운 모양: 줄 모양·배경 변화는 이것으로 잰다 (흐린 그림자는 글자가 아니다)
    effects = remove_text_effects(page, [union([s["bbox"] for s in a]) for _r_, _p, a, _w in plan if a])
    after = render_page(page) if effects else after_text
    left = page_spans(page)
    for _reg, rect_pt, a, _w in plan:
        if a:
            stay = [s for s in left if overlap_ratio(s["bbox"], rect_pt) >= 0.3]
            if stay:
                warnings.append(f"레드랙션 뒤에도 영역에 남은 글자 {len(stay)}개: {[_r(s['bbox'], 1) for s in stay]}")
    lost = len(others) - sum(1 for s in left if any(overlap_ratio(s["bbox"], o["bbox"]) > 0.5 for o in others))
    if redact and lost > 0:
        warnings.append(f"영역 밖 글자 {lost}개가 레드랙션에 함께 지워졌을 수 있음 (span 상자가 겹침)")

    glyph = np.abs(before.astype(np.int16) - after_text.astype(np.int16)).max(axis=2) > GLYPH_DIFF
    near = cv2.dilate(glyph.astype(np.uint8), np.ones((7, 7), np.uint8)).astype(bool)

    region_metas: list[dict] = []
    work_px: dict[str, tuple[int, int, int, int]] = {}
    raster_regs = [reg for reg, _p, a, _w in plan if not a]
    for reg, rect_pt, assigned, warn in plan:
        if not assigned:
            continue
        wpt = union([rect_pt] + [s["bbox"] for s in assigned])
        bpx = px_box_outer(bg.pt_to_px(wpt), size)
        work_px[reg.name] = bpx
        x0, y0, x1, y1 = bpx
        sl = (slice(y0, y1), slice(x0, x1))
        wa = max(1, (x1 - x0) * (y1 - y0))
        changed = np.abs(before[sl].astype(np.int16) - after_text[sl].astype(np.int16)).max(axis=2) > GLYPH_DIFF
        bg_change = float((changed & ~near[sl]).sum()) / wa
        flags = ["배경 변화 큼"] if bg_change > BG_CHANGE_FLAG else []
        fx = [_r(e) for e in effects if overlap_ratio(e, (wpt[0] - EFFECT_PAD_PT, wpt[1] - EFFECT_PAD_PT,
                                                           wpt[2] + EFFECT_PAD_PT, wpt[3] + EFFECT_PAD_PT)) > 0.5]
        lay = layout_from_diff(before, glyph, bpx, bg, assigned) if reg.kind == "text" else None
        region_metas.append({
            "name": reg.name, "kind": reg.kind, "rect_ratio": reg.rect.to_dict(), "rect_pt": _r(rect_pt),
            "work_rect_pt": _r(wpt), "style_defaults": reg.style,
            "text_layer": {
                "present": True,
                "spans": [{"bbox_pt": _r(s["bbox"]), "size_pt": s["size"], "color": s["color"], "font": s["font"],
                           "origin_pt": _r(s["origin"]), "chars": s["chars"]} for s in assigned],
                "size_pt": _dominant(assigned, "size"), "color": _dominant(assigned, "color"),
                "union_bbox_pt": _r(union([s["bbox"] for s in assigned])),
            },
            "tag_labels": [] if reg.kind == "tag_pills" else None,
            "inline_images_pt": [], "image_text_box_pt": None, "warnings": warn,
            "work_rect_px": list(bpx), "missing_from_text_layer": False, "image_text_detected": False,
            **({"text_layout": lay} if lay is not None else {}),
            "erase": {
                "text_mask": None, "redacted_spans": len(assigned), "removed_inline_images": 0,
                "method": "vector_redact", "removed_text_effects_pt": fx, "flat_px": 0, "inpaint_px": 0,
                "mask_ratio": round(float(changed.sum()) / wa, 4),
                "background_change_ratio": round(bg_change, 4),
                "strong_px_outside_text_layer_ratio": 0.0, "flags": flags,
            },
        })

    # ---- 2. span이 없는 영역: 렌더링한 그림에서 그림 카드 방식으로 지우고 패치만 덮는다
    patches: list[dict] = []
    raster_meta = None
    cleaned = after
    if raster_regs:
        warnings.append(f"텍스트 레이어에 글자가 없는 영역 {[r.name for r in raster_regs]}: "
                        f"쪽을 {VECTOR_DPI:.0f}dpi 그림으로 렌더링해 그림 방식으로 지움 (그 자리는 그림 패치)")
        res = _raster_fallback(page, template, raster_regs, size, sample=sample, seed=seed, params=params,
                               modes=modes, sources=sources, icon_templates=icon_templates, keep_lines=keep_lines)
        raster_meta = res.meta
        warnings += [f"(그림 방식) {w}" for w in res.warnings]
        if res.cleaned is not None:
            cleaned = res.cleaned
            for p in res.meta.get("patches") or []:
                x0, y0, x1, y1 = p["bbox_px"]
                patch = _insert_patch(page, res.cleaned[y0:y1, x0:x1], (x0, y0, x1, y1), bg)
                patch["region"] = p.get("region")
                patches.append(patch)
        for m_ in res.meta.get("regions") or []:
            m_.setdefault("erase", {})["method"] = "vector_raster:" + str((m_.get("erase") or {}).get("method"))
            work_px[m_["name"]] = tuple(m_.get("work_rect_px") or px_box_outer(bg.pt_to_px(m_["rect_pt"]), size))
            region_metas.append(m_)
    order = {reg.name: i for i, reg in enumerate(template.regions)}
    region_metas.sort(key=lambda m_: order.get(m_["name"], 99))

    meta = base_meta(region_metas, patches)
    meta["removed_text_effects_pt"] = [_r(e) for e in effects]
    meta["vector_page"] = {
        "text_layer_regions": [m_["name"] for m_ in region_metas if m_["erase"].get("method") == "vector_redact"],
        "raster_regions": [r.name for r in raster_regs],
        "fallback_clean": raster_meta.get("clean") if raster_meta else None,
    }
    return PageErase(meta, before, None, (0, 0, 0, 0), work_px, warnings, cleaned)
