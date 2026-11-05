"""한글 넣기 단계(8단계): 지운 PDF(cleared/) + 지우기 메타 + 번역 -> 한글 카드 PDF(translated/).

    python -m app.cli compose --project projects/sample [--excel 번역_검수.xlsx] [--pages 1-3]

지우기는 다시 하지 않는다. 번역은 엑셀 ‘번역’ 열(--excel) 또는 samples.json에서 온다.

- 지운 PDF는 메모리로 읽어 사본에 그린다 (지운 PDF·원본 PDF 모두 수정하지 않음).
- 원문 잉크 높이(F28)·세로 위치·왼쪽 시작점은 **원본 PDF**의 같은 쪽·같은 영역을 렌더링해 잰다.
- 태그: 메타의 tag_labels(지우기 단계가 빈 라벨로 그려 둠) 위에 글자만 넣는다.
  한글이 길어 라벨을 늘려야 할 때만, 예전 라벨 그림(content stream)을 지우고 그 영역 라벨 전체를 다시 그린다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pymupdf

from app import report
from app.icons import style_for_compose as icon_style_for
from app.project import ensure_not_source, load_project, parse_page_spec, sha256_file
from app.typeset import ROOT, TextStyle, TypesetResult, finalize, typeset_region, typeset_tags
from app.typeset_ink import measure_source_ink

_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)"
_TOKEN = re.compile(rf"{_NUM}|[A-Za-z*'\"]+")


# ---------------------------------------------------------------- 라벨 그림 찾기
def _path_bbox_pdf(stream: bytes) -> tuple[float, float, float, float] | None:
    """채우기 경로만 있는 content stream의 경로 bbox (PDF 좌표). 그 밖의 것이 있으면 None."""
    s = stream.decode("latin1")
    if any(op in s for op in (" Do", "BT", " cm", " Tf")):
        return None
    toks = _TOKEN.findall(s)
    nums: list[float] = []
    xs: list[float] = []
    ys: list[float] = []
    filled = False
    for t in toks:
        if re.fullmatch(_NUM, t):
            nums.append(float(t))
            continue
        if t in ("m", "l") and len(nums) >= 2:
            xs.append(nums[-2]); ys.append(nums[-1])
        elif t == "c" and len(nums) >= 6:
            xs += nums[-6::2]; ys += nums[-5::2]
        elif t == "re" and len(nums) >= 4:
            x, y, w, h = nums[-4:]
            xs += [x, x + w]; ys += [y, y + h]
        elif t in ("f", "F", "f*", "B", "b"):
            filled = True
        nums = []
    if not xs or not filled:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def find_label_streams(page: pymupdf.Page, label_bboxes: list, min_iou: float = 0.8) -> dict[int, int]:
    """라벨 bbox(page 좌표)마다 그 라벨만 그린 content stream xref를 찾는다. {라벨 index: xref}"""
    doc = page.parent
    found: dict[int, int] = {}
    for xref in page.get_contents():
        bb = _path_bbox_pdf(doc.xref_stream(xref) or b"")
        if bb is None:
            continue
        r = pymupdf.Rect(bb) * page.transformation_matrix
        for i, lb in enumerate(label_bboxes):
            L = pymupdf.Rect(lb)
            inter = (r & L)
            if inter.is_empty:
                continue
            iou = inter.get_area() / (r.get_area() + L.get_area() - inter.get_area())
            if iou >= min_iou and i not in found:
                found[i] = xref
    return found


# ---------------------------------------------------------------- 쪽 합성
def _clip_x1(rect: pymupdf.Rect, work: pymupdf.Rect | None) -> pymupdf.Rect:
    r = pymupdf.Rect(rect)
    if work is not None:
        r.x1 = min(r.x1, work.x1)
    return r


def _rot_rect(r, pivot) -> pymupdf.Rect:
    px, py = pivot
    r = pymupdf.Rect(r)
    return pymupdf.Rect(2 * px - r.x1, 2 * py - r.y1, 2 * px - r.x0, 2 * py - r.y0)


def safe_name(name: str, idx: int) -> str:
    """파일 이름용 영역 이름 (ASCII)."""
    return SAFE.get(name) or f"r{idx}"


STATUS_NO_TRANSLATION = "번역 없음 (원문만 지워짐)"
STATUS_MISSING_LINE = "읽지 못한 줄 있음 (지운 줄이 원문보다 많음)"


def compose_page(page: pymupdf.Page, src_page: pymupdf.Page, meta: dict, card: dict | None,
                 base_dir: Path = ROOT, styles: dict | None = None,
                 region_erase: dict | None = None) -> dict:
    """지운 페이지 사본(page)에 한 장의 카드 번역을 그린다. 칸별 기록 dict를 돌려준다.

    styles: {영역 이름: style}. 지금 regions.json의 서식(글꼴·자간 등). 지우기를 다시 돌리지 않고
    서식만 바꿔 합성할 수 있게, 있으면 지우기 때 저장한 style_defaults보다 우선한다.
    """
    card = card or {}
    out: dict = {}
    for reg in meta["regions"]:
        name = reg["name"]
        style_d = (styles or {}).get(name) or reg.get("style_defaults") or {}
        entry = card.get(name) or {}
        rect = pymupdf.Rect(reg["rect_pt"])
        work = pymupdf.Rect(reg["work_rect_pt"]) if reg.get("work_rect_pt") else None
        tl = reg.get("text_layer") or {}
        rec: dict = {"rect_pt": [round(v, 3) for v in rect], "warnings": []}

        if reg.get("kind") == "tag_pills":
            labels = [{"bbox_pt": l["bbox_pt"], "color": l["color"], "radius_pt": l.get("radius_pt")}
                      for l in (reg.get("tag_labels") or [])]
            texts = entry.get("ko") or []
            if isinstance(texts, str):
                texts = [texts]
            # 원문 라벨 안 흰 글자 잉크 높이 (원본 PDF)
            hs = []
            for lab in labels:
                lr = pymupdf.Rect(lab["bbox_pt"])
                inset = lab.get("radius_pt") or lr.height / 2
                ink = measure_source_ink(src_page, (lr.x0 + inset, lr.y0 + 0.5, lr.x1 - inset, lr.y1 - 0.5),
                                         polarity="light")
                lab["source_ink"] = ink.to_dict()
                if ink.glyph_height:
                    hs.append(ink.glyph_height)
            ink_h = float(np.median(hs)) if hs else None
            st = TextStyle.from_region_style(style_d, base_dir, size_pt=tl.get("size_pt") or 8.35,
                                             ink_height_pt=ink_h, valign="middle",
                                             fixed_size_pt=entry.get("fixed_size_pt"))
            if ink_h is None:
                rec["warnings"].append("라벨 안 원문 잉크를 못 잼 -> 텍스트 레이어 크기(pt) 사용")
            if not labels:
                rec.update(status=["라벨 없음"], results=[])
                out[name] = rec
                continue
            # 1차: 그리지 않고 계산 -> 라벨이 늘어나는지 본다
            dry = typeset_tags(page, labels, texts, st, draw=False)
            grown = any(abs(r.label_rect[2] - l["bbox_pt"][2]) > 0.01 or abs(r.label_rect[0] - l["bbox_pt"][0]) > 0.01
                        for r, l in zip(dry, labels) if r.label_rect)
            redraw = False
            if grown:
                streams = find_label_streams(page, [l["bbox_pt"] for l in labels])
                if len(streams) == len(labels):
                    for xref in streams.values():
                        page.parent.update_stream(xref, b" ")
                    redraw = True
                else:
                    rec["warnings"].append(f"라벨 그림을 {len(streams)}/{len(labels)}개만 찾음 -> 새 라벨을 덮어 그림")
                    redraw = True
            res = typeset_tags(page, labels, texts, st, draw_label=redraw)
            rec.update(
                source_glyph_height_pt=ink_h, labels=labels, label_redrawn=redraw,
                status=sorted({s for r in res for s in r.status}), results=[r.to_dict() for r in res],
            )
            out[name] = rec
            continue

        # ---- 이름·본문
        text = entry.get("ko")
        layout = reg.get("text_layout") or {}
        if layout.get("lines") and text and int(style_d.get("rotate", 0)) % 360 in (0, 180):
            # 그림 카드: 지우기 때 잰 원문 줄 자리·색·테두리·굵기·정렬·구분선을 따른다
            rec.update(source_ink={"layout_lines": len(layout["lines"])}, draw_rect_pt=[round(v, 3) for v in rect])
            r_ = compose_layout_region(page, rect, text, style_d, layout, base_dir, entry.get("source"))
            if r_ is not None:
                rec["warnings"] += r_.pop("warnings")
                rec.update(r_)
                out[name] = rec
                continue
            rec["warnings"].append("줄 모양이 믿을 만하지 않아 영역 전체에 넣음")
        if not text and (reg.get("erase") or {}).get("text_mask"):
            # 원문은 지웠는데 번역이 없다: 빈 카드가 되므로 크게 알린다
            rec.update(status=[STATUS_NO_TRANSLATION], lines=[], line_boxes_pt=[], overflow_text="", errors=[],
                       drawn=False, draw_rect_pt=[round(v, 3) for v in rect])
            out[name] = rec
            continue
        rot = int(style_d.get("rotate", 0)) % 360
        mrect = _clip_x1(rect | work if work else rect, work) & src_page.rect
        keep_pt = (region_erase or {}).get(name, {}).get("keep_ink_taller_than_pt")
        ink = measure_source_ink(src_page, mrect, polarity="dark", rotate=rot, drop_taller_than_pt=keep_pt)
        draw_rect = _clip_x1(rect, work)
        pivot = None
        if rot:
            # 돌린 틀 좌표: 잴 때 쓴 사각형(mrect)의 중심 둘레로 180도. 그릴 사각형도 틀로 옮긴다.
            pivot = ((mrect.x0 + mrect.x1) / 2, (mrect.y0 + mrect.y1) / 2)
            draw_rect = _rot_rect(draw_rect, pivot)
        if ink.bbox and style_d.get("align", "left") == "left":
            draw_rect.x0 = max(draw_rect.x0, ink.bbox[0])
        top = ink.lines[0][0] if ink.lines else None
        if top is not None and top < draw_rect.y0:
            draw_rect.y0 = top
        st = TextStyle.from_region_style(
            style_d, base_dir, size_pt=tl.get("size_pt"), ink_height_pt=ink.glyph_height,
            line_pitch_pt=ink.line_pitch, fixed_size_pt=entry.get("fixed_size_pt"),
            pivot=pivot,
        )
        if ink.glyph_height is None:
            rec["warnings"].append("원문 잉크를 못 잼 -> 텍스트 레이어 크기(pt) 사용")
        res = typeset_region(page, draw_rect, text, st, source_text=entry.get("source"), top_anchor=top)
        rd = res.to_dict()
        if rot:  # 보고서에는 페이지 좌표로 적는다 (틀 좌표는 frame_ 접두사)
            rec["rotate"] = rot
            rec["frame_draw_rect_pt"] = [round(v, 3) for v in draw_rect]
            rec["frame_line_boxes_pt"] = rd["line_boxes_pt"]
            rd["line_boxes_pt"] = [[round(v, 2) for v in _rot_rect(b, pivot)] for b in rd["line_boxes_pt"]]
            draw_rect = _rot_rect(draw_rect, pivot)
        rec.update(source_ink=ink.to_dict(), draw_rect_pt=[round(v, 3) for v in draw_rect], **rd)
        out[name] = rec
    return out


# ---------------------------------------------------------------- 그림 카드: 원문 줄 자리에 한 줄씩
SLOT_MIN_RATIO = 0.7
LAYOUT_ICON_SCALE = 1.3  # 줄 자리 배치에서 사전 기본 크기(1.1) 아이콘의 크기 (한글 글자 높이 대비)
_WORDY = re.compile(r"[가-힣A-Za-z]")  # 글자가 있는 줄 (기호·숫자·아이콘만 있는 줄은 크기를 정하지 않는다)  # 줄 자리에 넣을 때 원문 글자 높이의 70%까지 줄인다


def _rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _hex_rgb(c) -> str:
    c = [int(round(float(v))) for v in c]
    return "#%02x%02x%02x" % tuple(max(0, min(255, v)) for v in c)


def _merge_cuts(dividers: list) -> list[float]:
    """구분선(좌우 두 토막일 수 있음)의 세로 가운데 목록."""
    cuts: list[float] = []
    for y in sorted((d[1] + d[3]) / 2 for d in dividers):
        if not cuts or y - cuts[-1] > 2.0:
            cuts.append(y)
    return cuts


def layout_sections(layout: dict, rect) -> list[dict]:
    """원문 줄을 구분선으로 나눈 묶음: [{"lines": [...], "y0": 위, "y1": 아래}]."""
    rect = pymupdf.Rect(rect)
    cuts = _merge_cuts(layout.get("dividers_pt") or [])
    bounds = [rect.y0] + cuts + [rect.y1]
    out = []
    for k in range(len(bounds) - 1):
        lines = [ln for ln in layout["lines"] if bounds[k] <= (ln["bbox_pt"][1] + ln["bbox_pt"][3]) / 2 < bounds[k + 1]]
        if lines:
            out.append({"lines": lines, "y0": bounds[k], "y1": bounds[k + 1]})
    return out


def assign_lines(ko_lines: list[str], counts: list[int]) -> list[list[str]]:
    """번역 줄을 묶음에 나눈다. 수가 같으면 그대로, 다르면 아래 묶음부터 원문 줄 수만큼 주고 남는 것은 맨 위 묶음에."""
    if sum(counts) == len(ko_lines):
        out, i = [], 0
        for c in counts:
            out.append(ko_lines[i:i + c])
            i += c
        return out
    out = [[] for _ in counts]
    end = len(ko_lines)
    for k in range(len(counts) - 1, 0, -1):
        take = max(0, min(counts[k], end - k))  # 위 묶음마다 적어도 한 줄은 남긴다
        out[k] = ko_lines[end - take:end]
        end -= take
    out[0] = ko_lines[:end]
    return out


def _line_style(style_d: dict, base_dir, ln: dict, region_accents: list[str], pivot=None, **kw) -> TextStyle:
    """원문 줄 모양(색·테두리·굵기·정렬·다른 색 낱말)을 따른 서식. 영역 서식에 직접 적은 값이 먼저다."""
    st = TextStyle.from_region_style(style_d, base_dir, ink_height_pt=ln["glyph_h_pt"], **kw)
    if pivot is not None:
        st.rotate, st.pivot = 180, pivot
    if "color" not in style_d:
        st.color = ln["color"]
    if "align" not in style_d:
        st.align = ln.get("align", "left")
    st.bold = bool(style_d.get("bold", ln.get("bold", False)))
    if ln.get("stroke_color") and not style_d.get("stroke_color") and ln.get("stroke_pt"):
        st.stroke_color = ln["stroke_color"]
        st.stroke_width = ln["stroke_pt"] / max(st.start_size(), 1e-3)
    accents = [a["color"] for a in ln.get("accents") or []]
    accents += [c for c in region_accents if c not in accents]
    tags = {f"c{i + 1}": c for i, c in enumerate(accents)}
    st.tag_colors = {**tags, **(style_d.get("tag_colors") or {})}
    st.min_ratio = min(st.min_ratio, SLOT_MIN_RATIO)
    # 그림 카드의 글 속 아이콘은 보통 글자보다 크다: 사전의 기본 크기(1.1)를 그대로 둔 아이콘은 1.3으로
    st.icon_scale = max(st.icon_scale, LAYOUT_ICON_SCALE)
    st.icon_scales = {k: (LAYOUT_ICON_SCALE if abs(v - 1.1) < 1e-6 else v) for k, v in (st.icon_scales or {}).items()}
    return st


def compose_layout_region(page: pymupdf.Page, rect, text: str, style_d: dict, layout: dict,
                          base_dir: Path = ROOT, source_text: str | None = None) -> dict:
    """원문 줄 모양(지우기 때 잰 text_layout)대로 한글을 넣는다.

    - 원문 줄 자리마다 번역 한 줄: 같은 높이·색·테두리·굵기·정렬 (줄 수가 같을 때)
    - 구분선(-◆-) 위아래는 따로: 번역 줄을 묶음별로 나눠 선을 넘지 않는다
    - 묶음 안 줄 수가 원문과 다르면 그 묶음 안에서 줄바꿈해 맞춘다
    """
    rect = pymupdf.Rect(rect)
    # 잡음 줄(테두리 조각 등 아주 낮은 줄)은 빼고, 남는 줄이 없으면 None (영역 전체 배치로)
    good = [ln for ln in layout["lines"] if ln["glyph_h_pt"] >= 2.5]
    if good:
        gmax = max(ln["glyph_h_pt"] for ln in good)
        good = [ln for ln in good if ln["glyph_h_pt"] >= 0.3 * gmax]
    if not good:
        return None
    layout = {**layout, "lines": good}
    pivot = None
    if int(style_d.get("rotate", 0)) % 360 == 180:
        # 거꾸로 인쇄된 영역: 줄 모양을 ‘돌린 틀’ 좌표로 옮겨 배치하고, 그릴 때만 돌린다 (typeset의 rotate·pivot)
        pivot = ((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
        sx, sy = rect.x0 + rect.x1, rect.y0 + rect.y1

        def flip(b):
            return [sx - b[2], sy - b[3], sx - b[0], sy - b[1]]

        flipped = []
        for ln in layout["lines"]:
            f = {**ln, "bbox_pt": flip(ln["bbox_pt"]),
                 **({"cy_pt": sy - ln["cy_pt"]} if ln.get("cy_pt") is not None else {}),
                 "align": {"left": "right", "right": "left"}.get(ln.get("align", "left"), ln.get("align", "left")),
                 "accents": [{**a, "x_pt": sorted([[sx - v, sx - u] for u, v in a.get("x_pt", [])])}
                             for a in ln.get("accents") or []]}
            flipped.append(f)
        flipped.sort(key=lambda ln: ln["bbox_pt"][1])
        layout = {**layout, "lines": flipped, "dividers_pt": [flip(d) for d in layout.get("dividers_pt") or []]}
    lines_all = layout["lines"]
    secs = layout_sections(layout, rect)
    ko_lines = [t.strip() for t in (text or "").split("\n") if t.strip()]
    parts = assign_lines(ko_lines, [len(s["lines"]) for s in secs])
    region_accents: list[str] = []
    for ln in lines_all:
        for a in ln.get("accents") or []:
            if a["color"] not in region_accents:
                region_accents.append(a["color"])
    # 같은 서식 줄(색이 가깝고 글자 높이가 비슷)은 한 묶음: 색은 하나로, 크기는 가장 작게 맞은 줄에 맞춘다
    sec_of = {id(ln): k for k, sec in enumerate(secs) for ln in sec["lines"]}
    groups: list[list[dict]] = []
    for ln in lines_all:
        c = np.array(_rgb(ln["color"]))
        for g in groups:
            g0 = g[0]
            gh_med = float(np.median([x["glyph_h_pt"] for x in g]))
            if (sec_of.get(id(g0)) == sec_of.get(id(ln))
                    and np.abs(np.array(_rgb(g0["color"])) - c).max() < 70
                    and bool(g0.get("stroke_color")) == bool(ln.get("stroke_color"))
                    and abs(ln["glyph_h_pt"] - gh_med) <= 0.25 * max(ln["glyph_h_pt"], gh_med)):
                g.append(ln)
                break
        else:
            groups.append([ln])
    gid = {}
    for k, g in enumerate(groups):
        col = _hex_rgb(np.median([_rgb(x["color"]) for x in g], axis=0))
        gh = float(np.median([x["glyph_h_pt"] for x in g]))
        for x in g:
            gid[id(x)] = (k, col, gh)
    lines_all = [{**ln, "color": gid[id(ln)][1], "glyph_h_pt": gid[id(ln)][2], "_g": gid[id(ln)][0]} for ln in lines_all]
    layout = {**layout, "lines": lines_all}
    secs = layout_sections(layout, rect)
    parts = assign_lines(ko_lines, [len(s["lines"]) for s in secs])
    right = max(ln["bbox_pt"][2] for ln in lines_all)
    left = min(ln["bbox_pt"][0] for ln in lines_all)
    results: list[tuple[TypesetResult, str]] = []
    slots = []  # (줄 모양, 번역, 사각형, 서식, 위)
    for sec, kos in zip(secs, parts):
        if not kos:
            continue
        src = sec["lines"]
        if len(kos) == len(src):
            for ln, ko in zip(src, kos):
                if ln.get("keep"):  # 아이콘·숫자만 있는 줄: 원문 그대로 남겼다 (지우지도 그리지도 않음)
                    continue
                b = ln["bbox_pt"]
                gh = ln["glyph_h_pt"]
                st = _line_style(style_d, base_dir, ln, region_accents, pivot)
                pad = 0.5 * gh
                cy = ln.get("cy_pt") if ln.get("cy_pt") is not None else (b[1] + b[3]) / 2
                top = cy - gh / 2
                if st.align == "center":
                    cx = (b[0] + b[2]) / 2
                    hw = max(min(cx - rect.x0, rect.x1 - cx), (b[2] - b[0]) / 2)
                    hw = min(hw, max(cx - left, right - cx) + pad)
                    r = pymupdf.Rect(cx - hw, top, cx + hw, top + 1.6 * gh)
                elif st.align == "right":
                    r = pymupdf.Rect(max(rect.x0, left - pad), top, b[2], top + 1.6 * gh)
                else:
                    r = pymupdf.Rect(b[0], top, min(rect.x1, right + pad), top + 1.6 * gh)
                slots.append((ln, ko, r, st, top))
        else:  # 줄 수가 다르면 묶음 안에서 줄바꿈
            ln0 = dict(src[0])
            ln0["glyph_h_pt"] = float(np.median([ln["glyph_h_pt"] for ln in src]))
            aligns = [ln.get("align", "left") for ln in src]
            ln0["align"] = max(set(aligns), key=aligns.count)
            pitch = (float(np.median([src[i + 1]["bbox_pt"][1] - src[i]["bbox_pt"][1] for i in range(len(src) - 1)]))
                     if len(src) > 1 else None)
            st = _line_style(style_d, base_dir, ln0, region_accents, pivot, line_pitch_pt=pitch)
            gh = ln0["glyph_h_pt"]
            top = (src[0]["bbox_pt"][1] + src[0]["bbox_pt"][3]) / 2 - gh / 2
            bottom = sec["y1"] - (0.3 * gh if sec["y1"] < rect.y1 else 0)
            x0 = min(ln["bbox_pt"][0] for ln in src) if st.align == "left" else max(rect.x0, left - 0.5 * gh)
            r = pymupdf.Rect(x0, top, min(rect.x1, right + 0.5 * gh), max(bottom, top + 1.6 * gh))
            results.append((typeset_region(page, r, "\n".join(kos), st, top_anchor=top), "paragraph"))
    # 1차: 그리지 않고 줄마다 맞는 크기 -> 같은 묶음은 가장 작은 크기로 그린다
    fit_size: dict[int, float] = {}
    own: dict[int, float] = {}
    for i, (ln, ko, r, st, top) in enumerate(slots):
        d = typeset_region(page, r, ko, st, top_anchor=top, draw=False)
        if d.size:
            own[i] = d.size
            if _WORDY.search(re.sub(r"\[\[[^\]]*\]\]|</?(?:b|i|c\d+)>", "", ko)):
                fit_size[ln["_g"]] = min(fit_size.get(ln["_g"], d.size), d.size)
    for i, (ln, ko, r, st, top) in enumerate(slots):
        size = fit_size.get(ln["_g"])
        if size and i in own and size > own[i] + 1e-6:
            # 기호·아이콘 줄처럼 원문 높이가 낮게 재진 줄: 묶음 크기로 넣어 보고, 안 들어가면 이 줄만 작게
            st.fixed_size_pt = size
            trial = typeset_region(page, r, ko, st, top_anchor=top, draw=False)
            st.fixed_size_pt = None
            if trial.overflow_text:
                size = own[i]
        elif i in own and not size:
            size = own[i]
        if size:
            start = st.start_size()
            st.fixed_size_pt = size
            res = typeset_region(page, r, ko, st, top_anchor=top)
            res.start_size = start
            if size < start - 1e-6 and "글자 축소" not in res.status:
                res.status = ["글자 축소"] + res.status
        else:
            res = typeset_region(page, r, ko, st, top_anchor=top)
        results.append((res, "slot"))
    status = sorted({s for r, _ in results for s in r.status})
    # 지운 줄보다 읽은(원문) 줄이 적다: 추출이 줄을 빠뜨렸으면 그 줄은 지워지기만 하고 다시 넣지 못한다
    n_src = len([t for t in (source_text or "").split("\n") if t.strip()]) if source_text else None
    if n_src is not None and n_src < len(lines_all):
        status = sorted(set(status) | {STATUS_MISSING_LINE})
    sizes = [r.size for r, _ in results if r.size]
    errors =[e for r, _ in results for e in r.errors]
    if ko_lines and len(ko_lines) != len(lines_all):
        errors_note = f"번역 {len(ko_lines)}줄, 원문 {len(lines_all)}줄: 묶음 안에서 줄바꿈"
    else:
        errors_note = ""
    return {
        "layout": "lines", "sections": len(secs), "line_modes": [m for _, m in results],
        "status": status, "size_pt": round(float(np.median(sizes)), 3) if sizes else None,
        "start_size_pt": round(float(np.median([r.start_size for r, _ in results if r.start_size])), 3) if sizes else None,
        "min_size_pt": round(min(r.min_size for r, _ in results if r.min_size), 3) if sizes else None,
        "tracking": round(min((r.tracking for r, _ in results), default=0.0), 3),
        "lines": [t for r, _ in results for t in r.lines],
        "line_boxes_pt": [[round(v, 2) for v in b] for r, _ in results for b in r.line_boxes],
        "overflow_text": "\n".join(r.overflow_text for r, _ in results if r.overflow_text),
        "errors": errors, "label_rect_pt": None, "drawn": True, "rotate": 180 if pivot else 0,
        "warnings": [errors_note] if errors_note else [],
    }


# ---------------------------------------------------------------- 실행
def _render(page: pymupdf.Page, dpi: int, clip=None) -> np.ndarray:
    pix = page.get_pixmap(dpi=dpi, clip=clip, alpha=False, colorspace=pymupdf.csRGB)
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, 3).copy()


SAFE = {"태그": "tag", "이름": "name", "본문": "body",
        "위_이름": "top_name", "위_설명": "top_flavor", "위_효과": "top_effect",
        "아래_이름": "bot_name", "아래_설명": "bot_flavor", "아래_효과": "bot_effect"}


def erased_pdf_path(erase_run: Path) -> Path:
    """지우기 결과 폴더의 PDF (프로젝트 cleared/는 cleared.pdf, 테스트 실행은 erased.pdf)."""
    for name in ("cleared.pdf", "erased.pdf"):
        if (Path(erase_run) / name).exists():
            return Path(erase_run) / name
    raise FileNotFoundError(f"지운 PDF가 없습니다: {erase_run} (먼저 erase를 실행하세요)")


def run_compose(project_dir: Path, erase_run: Path, pages_spec: str | None, run_dir: Path,
                cards: dict | None = None, translation_source: str = "samples.json",
                out_name: str = "composed.pdf", progress=None, cancel=None) -> tuple[Path, int]:
    """run_dir(새로 만들 빈 폴더)에 결과를 쓴다. cards가 없으면 samples.json을 쓴다.

    progress: app.service.ProgressEvent를 받는 콜백, cancel: app.service.CancelToken (둘 다 선택).
    """
    from app.service import ProgressEvent

    def _p(stage, done, total, msg, page=None):
        if progress:
            progress(ProgressEvent(stage, done, total, msg, page))

    project = load_project(Path(project_dir))
    src_path = project.source_pdf
    erase_run = Path(erase_run).resolve()
    erased_path = erased_pdf_path(erase_run)
    sha_src_before = sha256_file(src_path)
    sha_erased_before = sha256_file(erased_path)
    cards = project.samples if cards is None else cards

    run_dir = Path(run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    ensure_not_source(run_dir / "x", src_path)

    src_doc = pymupdf.open(stream=src_path.read_bytes(), filetype="pdf")
    er_doc = pymupdf.open(stream=erased_path.read_bytes(), filetype="pdf")
    if pages_spec:
        pages = parse_page_spec(pages_spec, src_doc.page_count)
    else:
        run_info = json.loads((erase_run / "run.json").read_text(encoding="utf-8"))
        pages = list(run_info["pages"])

    metas = {}
    for pno in pages:
        f = erase_run / "meta" / f"page-{pno:03d}.json"
        if not f.exists():
            raise FileNotFoundError(f"지우기 메타가 없습니다: {f}")
        metas[pno] = json.loads(f.read_text(encoding="utf-8"))

    styles = {r.name: r.style for r in project.template.regions}
    icon_style = icon_style_for(project_dir)  # 게임 아이콘 사전 (영역 서식에 따로 적은 값이 우선)
    if icon_style:
        styles = {n: {**icon_style, **(s or {})} for n, s in styles.items()}
    region_erase = {r.name: r.erase for r in project.template.regions}

    out_doc = pymupdf.open()
    for pno in pages:
        idx = metas[pno]["output_page_index"]
        out_doc.insert_pdf(er_doc, from_page=idx, to_page=idx)

    page_recs = []
    for i, pno in enumerate(pages):
        if cancel:
            cancel.check()
        _p("compose", i, len(pages), f"{pno}쪽 한글 넣는 중", pno)
        rec = compose_page(out_doc[i], src_doc[pno - 1], metas[pno], cards.get(pno), ROOT, styles, region_erase)
        page_recs.append({"page": pno, "regions": rec})

    finalize(out_doc)
    out_pdf = run_dir / out_name
    ensure_not_source(out_pdf, src_path)
    out_doc.save(out_pdf, garbage=3, deflate=True)
    out_doc.close()

    # ---- PNG·비교
    chk = pymupdf.open(out_pdf)
    cdir = run_dir / "compare"
    for i, pno in enumerate(pages):
        if cancel:
            cancel.check()
        _p("render", i, len(pages), f"{pno}쪽 미리보기·비교 이미지", pno)
        cp, sp = chk[i], src_doc[pno - 1]
        cp.get_pixmap(dpi=300, alpha=False).save(str(run_dir / f"p{pno:03d}.png"))
        report.save(report.side_by_side(_render(sp, 300), _render(cp, 300)), cdir / f"p{pno:03d}_card.png")
        for ri, reg in enumerate(metas[pno]["regions"]):
            sn = safe_name(reg["name"], ri)
            r = pymupdf.Rect(reg["rect_pt"])
            for lab in reg.get("tag_labels") or []:
                r |= pymupdf.Rect(lab["bbox_pt"])
            res_rect = page_recs[i]["regions"].get(reg["name"], {})
            for rr in res_rect.get("line_boxes_pt") or []:
                r |= pymupdf.Rect(rr)
            for t in res_rect.get("results") or []:
                if t.get("label_rect_pt"):
                    r |= pymupdf.Rect(t["label_rect_pt"])
            clip = pymupdf.Rect(r.x0 - 3, r.y0 - 3, r.x1 + 3, r.y1 + 3) & sp.rect
            img = report.stacked([(f"ORIGINAL p{pno} {sn} 600dpi", _render(sp, 600, clip)),
                                  ("COMPOSED", _render(cp, 600, clip))], scale=1)
            report.save(img, cdir / f"p{pno:03d}_{sn}.png")
        page_recs[i]["extracted_text"] = cp.get_text()
    chk.close()

    # ---- 칸별 상태 요약
    summary: dict[str, list[str]] = {}
    for pr in page_recs:
        for rname, rec in pr["regions"].items():
            key = f"{src_path.name}#p{pr['page']}#{rname}"
            for s in rec.get("status") or []:
                summary.setdefault(s, []).append(key)

    sha_src_after = sha256_file(src_path)
    sha_erased_after = sha256_file(erased_path)
    rep = {
        "command": "compose",
        "project": str(project.root),
        "erase_run": str(erase_run),
        "translation_source": translation_source,
        "pages": pages,
        "source_pdf": str(src_path),
        "source_sha256_before": sha_src_before,
        "source_sha256_after": sha_src_after,
        "source_unchanged": sha_src_before == sha_src_after,
        "erased_sha256_before": sha_erased_before,
        "erased_sha256_after": sha_erased_after,
        "erased_unchanged": sha_erased_before == sha_erased_after,
        "output_pdf": str(out_pdf),
        "size_rule": "원문 글자 높이 기준: 시작크기 = 원문 잉크 높이 / 글꼴 보정 배율, min = 시작크기 x 0.8 (한글 글자 높이가 원문의 80% 아래로 가지 않음). 잉크 높이를 모르면 min = max(원문pt x 0.8, min(6pt, 원문pt))",
        "status_summary": summary,
        "pages_detail": page_recs,
    }
    (run_dir / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "source.sha256").write_text(
        f"before {sha_src_before}  {src_path.name}\nafter  {sha_src_after}  {src_path.name}\n", encoding="utf-8")
    ok = rep["source_unchanged"] and rep["erased_unchanged"]
    return run_dir, (0 if ok else 2)
