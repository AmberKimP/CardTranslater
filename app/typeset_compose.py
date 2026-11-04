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
from app.project import ensure_not_source, load_project, parse_page_spec, sha256_file
from app.typeset import ROOT, TextStyle, finalize, typeset_region, typeset_tags
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


def compose_page(page: pymupdf.Page, src_page: pymupdf.Page, meta: dict, card: dict | None,
                 base_dir: Path = ROOT, styles: dict | None = None) -> dict:
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
        mrect = _clip_x1(rect | work if work else rect, work)
        ink = measure_source_ink(src_page, mrect, polarity="dark")
        draw_rect = _clip_x1(rect, work)
        if ink.bbox and style_d.get("align", "left") == "left":
            draw_rect.x0 = max(draw_rect.x0, ink.bbox[0])
        top = ink.lines[0][0] if ink.lines else None
        if top is not None and top < draw_rect.y0:
            draw_rect.y0 = top
        st = TextStyle.from_region_style(
            style_d, base_dir, size_pt=tl.get("size_pt"), ink_height_pt=ink.glyph_height,
            line_pitch_pt=ink.line_pitch, fixed_size_pt=entry.get("fixed_size_pt"),
        )
        if ink.glyph_height is None:
            rec["warnings"].append("원문 잉크를 못 잼 -> 텍스트 레이어 크기(pt) 사용")
        res = typeset_region(page, draw_rect, text, st, source_text=entry.get("source"), top_anchor=top)
        rec.update(source_ink=ink.to_dict(), draw_rect_pt=[round(v, 3) for v in draw_rect], **res.to_dict())
        out[name] = rec
    return out


# ---------------------------------------------------------------- 실행
def _render(page: pymupdf.Page, dpi: int, clip=None) -> np.ndarray:
    pix = page.get_pixmap(dpi=dpi, clip=clip, alpha=False, colorspace=pymupdf.csRGB)
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, 3).copy()


SAFE = {"태그": "tag", "이름": "name", "본문": "body"}


def erased_pdf_path(erase_run: Path) -> Path:
    """지우기 결과 폴더의 PDF (프로젝트 cleared/는 cleared.pdf, 테스트 실행은 erased.pdf)."""
    for name in ("cleared.pdf", "erased.pdf"):
        if (Path(erase_run) / name).exists():
            return Path(erase_run) / name
    raise FileNotFoundError(f"지운 PDF가 없습니다: {erase_run} (먼저 erase를 실행하세요)")


def run_compose(project_dir: Path, erase_run: Path, pages_spec: str | None, run_dir: Path,
                cards: dict | None = None, translation_source: str = "samples.json",
                out_name: str = "composed.pdf") -> tuple[Path, int]:
    """run_dir(새로 만들 빈 폴더)에 결과를 쓴다. cards가 없으면 samples.json을 쓴다."""
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

    out_doc = pymupdf.open()
    for pno in pages:
        idx = metas[pno]["output_page_index"]
        out_doc.insert_pdf(er_doc, from_page=idx, to_page=idx)

    page_recs = []
    for i, pno in enumerate(pages):
        print(f"[compose] page {pno} ...", flush=True)
        rec = compose_page(out_doc[i], src_doc[pno - 1], metas[pno], cards.get(pno), ROOT, styles)
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
        cp, sp = chk[i], src_doc[pno - 1]
        cp.get_pixmap(dpi=300, alpha=False).save(str(run_dir / f"p{pno:03d}.png"))
        report.save(report.side_by_side(_render(sp, 300), _render(cp, 300)), cdir / f"p{pno:03d}_card.png")
        for reg in metas[pno]["regions"]:
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
            img = report.stacked([(f"ORIGINAL p{pno} {SAFE.get(reg['name'], 'r')} 600dpi", _render(sp, 600, clip)),
                                  ("COMPOSED", _render(cp, 600, clip))], scale=1)
            report.save(img, cdir / f"p{pno:03d}_{SAFE.get(reg['name'], 'r')}.png")
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
