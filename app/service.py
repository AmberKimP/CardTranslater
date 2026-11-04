"""화면과 상관없는 작업 흐름 (명령줄·웹·PySide6가 모두 이것을 부른다).

규칙 (PRD 6장 '화면과 엔진 나누기'):
- 이 모듈과 app/의 다른 모듈은 화면 라이브러리(PySide6, FastAPI 등)를 import하지 않고, print하지 않는다.
- 긴 작업은 progress 콜백으로 진행률을 알리고, cancel 토큰을 쪽마다 확인한다.
  콜백은 작업 스레드에서 불린다. 화면 쪽이 자기 스레드로 넘긴다
  (PySide6: Signal.emit, 웹: 큐/SSE). 콜백 안에서 화면을 직접 건드리지 않는다.
- 결과는 dataclass로 돌려준다 (경로, 경고, 상태). 화면은 이것만 보고 그린다.
- 사용자에게 물어야 하는 것(엑셀 확인 항목 등)은 예외가 아니라 결과 상태로 돌려준다.
  화면은 물어본 뒤 accept_issues=True로 다시 부른다.

    erase(project_dir, pages="1-30")                  -> EraseResult   (7단계, cleared/)
    export_excel(project_dir)                          -> Path
    compose(project_dir, excel=..., accept_issues=...) -> ComposeResult (8단계, translated/)
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from app import report
from app.erase import erase_page, find_background_image, load_image_rgb, render_on_image_grid
from app.project import ensure_not_source, load_project, parse_page_spec, sha256_file
from app.typeset_compose import safe_name

SCHEMA = "cardtranslater.erase-page/2"  # 2: 원본 이미지 유지 + patches


# ---------------------------------------------------------------- 진행률·취소
@dataclass
class ProgressEvent:
    stage: str  # "erase" | "verify" | "compose" | "render" | "done"
    done: int  # 끝난 수
    total: int
    message: str = ""
    page: int | None = None


ProgressFn = Callable[[ProgressEvent], None]


class Cancelled(Exception):
    """사용자가 취소했다. 작업 폴더는 지우고, 최종 폴더(cleared/·translated/)는 그대로 둔다."""


class CancelToken:
    """다른 스레드(화면)에서 cancel()하면 작업 스레드가 다음 확인 지점에서 멈춘다."""

    def __init__(self) -> None:
        self._ev = threading.Event()

    def cancel(self) -> None:
        self._ev.set()

    @property
    def cancelled(self) -> bool:
        return self._ev.is_set()

    def check(self) -> None:
        if self._ev.is_set():
            raise Cancelled()


def _noop(_: ProgressEvent) -> None:
    pass


# ---------------------------------------------------------------- 결과
@dataclass
class EraseResult:
    ok: bool  # 원본이 바뀌지 않았고 결과를 최종 폴더에 넣었다
    output_dir: Path  # ok면 최종 폴더, 아니면 결과를 남겨 둔 작업 폴더
    pages: list[int]
    warnings: dict[int, list[str]] = field(default_factory=dict)  # 쪽 -> 경고
    error: str | None = None


@dataclass
class ComposeResult:
    state: str  # "done" | "needs_confirmation" | "blocked" | "failed"
    output_dir: Path | None = None
    blocking: list[str] = field(default_factory=list)  # 이것이 있으면 진행할 수 없다
    issues: list[str] = field(default_factory=list)  # 사용자 확인이 필요한 항목
    status_summary: dict[str, list[str]] = field(default_factory=dict)  # 글자 축소·넘침 등 -> 칸 key
    rows: int = 0
    error: str | None = None


# ---------------------------------------------------------------- 폴더
def _output_dirs(project_root: Path, folder: str, kind: str, test: bool, out_root: Path) -> tuple[Path, Path]:
    """(최종 폴더, 작업 폴더). 프로젝트 모드는 숨은 작업 폴더에 다 쓴 뒤 _commit으로 바꿔 넣는다."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if test:
        d = Path(out_root).resolve() / f"{stamp}-{kind}"
        return d, d
    return project_root / folder, project_root / f".{folder}-{stamp}"


def _commit(tmp: Path, final: Path) -> None:
    """다 쓴 작업 폴더를 최종 폴더로 바꿔 넣는다 (예전 결과는 지움)."""
    if tmp == final:
        return
    if final.exists():
        shutil.rmtree(final)
    tmp.rename(final)


def _discard(tmp: Path, final: Path) -> None:
    if tmp != final and tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)


def _boxes(page) -> dict:
    doc = page.parent
    out = {"mediabox": [round(v, 3) for v in page.mediabox], "cropbox": [round(v, 3) for v in page.cropbox]}
    for key in ("BleedBox", "TrimBox", "ArtBox"):
        out[key] = doc.xref_get_key(page.xref, key)[1]
    return out


# ---------------------------------------------------------------- 7단계: 지우기
def erase(project_dir: Path | str, pages: str, *, test: bool = False, out_root: Path | str = "test_runs",
          progress: ProgressFn | None = None, cancel: CancelToken | None = None) -> EraseResult:
    """영역 글자를 지워 cleared/ (test면 test_runs/<시각>-erase/)에 쓴다. 원본은 읽기만 한다."""
    progress = progress or _noop
    project = load_project(Path(project_dir))
    final_dir, run_dir = _output_dirs(project.root, "cleared", "erase", test, Path(out_root))
    run_dir.mkdir(parents=True, exist_ok=False)
    try:
        return _erase(project, pages, final_dir, run_dir, test, progress, cancel)
    except BaseException:
        _discard(run_dir, final_dir)
        raise


def _erase(project, pages_spec, final_dir, run_dir, test, progress, cancel) -> EraseResult:
    import pymupdf

    src_path = project.source_pdf
    sha_before = sha256_file(src_path)
    ensure_not_source(run_dir / "x", src_path)
    pdf_name = "erased.pdf" if test else "cleared.pdf"

    src_bytes = src_path.read_bytes()  # 원본은 메모리로만 읽는다
    src_doc = pymupdf.open(stream=src_bytes, filetype="pdf")
    pages = parse_page_spec(pages_spec, src_doc.page_count)
    n = len(pages)

    out_doc = pymupdf.open(stream=src_bytes, filetype="pdf")
    out_doc.select([p - 1 for p in pages])

    page_metas = []
    results = []
    for i, pno in enumerate(pages):
        if cancel:
            cancel.check()
        progress(ProgressEvent("erase", i, n, f"{pno}쪽 지우는 중", pno))
        r = erase_page(out_doc[i], project.template, project.samples.get(pno), seed=pno)
        page_metas.append({
            "schema": SCHEMA,
            "source_pdf": str(src_path),
            "source_page": pno,
            "output_pdf": pdf_name,
            "output_page_index": i,
            **r.meta,
        })
        results.append(r)

    out_pdf = run_dir / pdf_name
    ensure_not_source(out_pdf, src_path)
    out_doc.save(out_pdf, garbage=3, deflate=True)
    out_doc.close()

    # ---- 검증 + 비교 이미지
    check = pymupdf.open(out_pdf)
    try:
        box_checks = _verify(pymupdf, project, pages, page_metas, results, src_doc, check, run_dir, progress, cancel)
    finally:  # 취소·오류 때도 닫는다 (Windows는 열린 파일이 든 작업 폴더를 지우지 못한다)
        check.close()
        src_doc.close()
    return _finish_erase(project, pages, page_metas, box_checks, final_dir, run_dir, pdf_name, sha_before,
                         progress, cancel)


def _verify(pymupdf, project, pages, page_metas, results, src_doc, check, run_dir, progress, cancel) -> list:
    n = len(pages)
    box_checks = []
    for i, pno in enumerate(pages):
        if cancel:
            cancel.check()
        progress(ProgressEvent("verify", i, n, f"{pno}쪽 확인·비교 이미지", pno))
        sp, op = src_doc[pno - 1], check[i]
        sb, ob = _boxes(sp), _boxes(op)
        box_checks.append({"source_page": pno, "same": sb == ob, "source": sb, "output": ob})

        bg = find_background_image(sp)
        raw = load_image_rgb(src_doc, bg.xref)
        before, _ = render_on_image_grid(sp, bg, raw)
        bg_o = find_background_image(op)
        raw_o = load_image_rgb(check, bg_o.xref)
        after, _ = render_on_image_grid(op, bg_o, raw_o)
        if raw_o.shape != raw.shape:
            page_metas[i]["warnings"].append(f"이미지 크기가 바뀜 {raw.shape} -> {raw_o.shape}")

        # 원본 배경 JPEG 스트림이 바이트 그대로인지
        s_raw = src_doc.xref_stream_raw(bg.xref)
        o_raw = check.xref_stream_raw(bg_o.xref)
        bgm = page_metas[i]["background_image"]
        bgm["stream_sha256_source"] = hashlib.sha256(s_raw).hexdigest()
        bgm["stream_sha256_output"] = hashlib.sha256(o_raw).hexdigest()
        bgm["stream_untouched"] = s_raw == o_raw
        bgm["filter"] = check.xref_get_key(bg_o.xref, "Filter")[1]
        bgm["output_xref"] = bg_o.xref

        # 패치: 저장 뒤 xref·압축 방식 다시 확인 (garbage 정리로 번호가 바뀔 수 있음)
        infos = [x for x in op.get_image_info(xrefs=True) if x.get("xref", 0) not in (0, bg_o.xref)]
        outside = np.ones(raw.shape[:2], bool)
        for pt in page_metas[i]["patches"]:
            match = min(infos, key=lambda x: sum(abs(a - b) for a, b in zip(x["bbox"], pt["bbox_pt"])))
            pt["xref"] = match["xref"]
            pt["filter"] = check.xref_get_key(match["xref"], "Filter")[1]
            pt["stream_bytes"] = len(check.xref_stream_raw(match["xref"]))
            x0, y0, x1, y1 = pt["bbox_px"]
            outside[y0:y1, x0:x1] = False
        diff = np.abs(after.astype(int) - before.astype(int)).max(axis=2)
        page_metas[i]["verify"] = {
            "outside_patches_changed_px_at_image_grid": int((diff[outside] > 0).sum()),
            "outside_patches_max_diff": int(diff[outside].max()) if outside.any() else 0,
        }

        def one_page_size(d, idx):
            t = pymupdf.open()
            t.insert_pdf(d, from_page=idx, to_page=idx)
            size = len(t.tobytes(garbage=3, deflate=True))
            t.close()
            return size

        page_metas[i]["verify"]["single_page_pdf_bytes"] = {
            "source": one_page_size(src_doc, pno - 1),
            "output": one_page_size(check, i),
        }

        region_names = [m["name"] for m in page_metas[i]["regions"]]
        # 다른 해상도(600dpi)에서 패치 경계 확인용 확대
        for pt in page_metas[i]["patches"]:
            r = pymupdf.Rect(pt["bbox_pt"])
            clip = pymupdf.Rect(r.x0 - 4, r.y0 - 4, min(r.x0 + 60, r.x1 + 4), r.y1 + 4) & sp.rect
            a = sp.get_pixmap(dpi=600, clip=clip, alpha=False)
            b = op.get_pixmap(dpi=600, clip=clip, alpha=False)
            A = np.frombuffer(a.samples, np.uint8).reshape(a.h, a.w, 3)
            B = np.frombuffer(b.samples, np.uint8).reshape(b.h, b.w, 3)
            safe_r = safe_name(pt["region"], region_names.index(pt["region"]))
            pth = run_dir / "compare" / f"p{pno:03d}_seam600_{safe_r}.png"
            report.save(report.stacked([("ORIGINAL 600dpi (left edge of patch)", A), ("RESULT 600dpi", B)], scale=1), pth)
            pt["seam_png_600dpi"] = str(pth)

        pdir = run_dir / "compare"
        tag = f"p{pno:03d}"
        report.save(report.side_by_side(before, after), pdir / f"{tag}_card.png")
        res = results[i]
        cx0, cy0, cx1, cy1 = res.crop_px
        overlay_full = before.copy()
        overlay_full[cy0:cy1, cx0:cx1] = report.mask_overlay(
            before[cy0:cy1, cx0:cx1], res.clean.flat_mask, res.clean.inpaint_mask
        )
        for ri, m in enumerate(page_metas[i]["regions"]):
            x0, y0, x1, y1 = m["work_rect_px"]
            pad = 14
            x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
            x1, y1 = min(raw.shape[1], x1 + pad), min(raw.shape[0], y1 + pad)
            img = report.stacked(
                [
                    ("ORIGINAL (rendered)", before[y0:y1, x0:x1]),
                    ("RESULT (rendered, labels redrawn)", after[y0:y1, x0:x1]),
                    ("RESULT image layers only (original + patch, no vector)", res.cleaned[y0:y1, x0:x1]),
                    ("MASK red=flat/gradient fill, blue=inpaint", overlay_full[y0:y1, x0:x1]),
                ],
                scale=2,
            )
            path = report.save(img, pdir / f"{tag}_{safe_name(m['name'], ri)}.png")
            m["compare_png"] = str(path)
        page_metas[i]["compare_card_png"] = str(pdir / f"{tag}_card.png")
    return box_checks


def _finish_erase(project, pages, page_metas, box_checks, final_dir, run_dir, pdf_name, sha_before,
                  progress, cancel) -> EraseResult:
    src_path = project.source_pdf
    n = len(pages)
    sha_after = sha256_file(src_path)
    mdir = run_dir / "meta"
    mdir.mkdir()
    for meta in page_metas:
        (mdir / f"page-{meta['source_page']:03d}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    run = {
        "command": "erase",
        "project": str(project.root),
        "pages": pages,
        "source_pdf": str(src_path),
        "source_sha256_before": sha_before,
        "source_sha256_after": sha_after,
        "source_unchanged": sha_before == sha_after,
        "output_pdf": str(final_dir / pdf_name),
        "page_boxes": box_checks,
        "meta_files": [str(mdir / f"page-{p:03d}.json") for p in pages],
    }
    (run_dir / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "source.sha256").write_text(
        f"before {sha_before}  {src_path.name}\nafter  {sha_after}  {src_path.name}\n", encoding="utf-8"
    )
    warnings = {m["source_page"]: list(m["warnings"]) for m in page_metas if m["warnings"]}
    if sha_before != sha_after:
        return EraseResult(False, run_dir, pages, warnings, error="원본 PDF 해시가 바뀌었습니다")
    if cancel:
        cancel.check()  # 마지막 확인: 취소했으면 예전 cleared/를 바꾸지 않는다
    _commit(run_dir, final_dir)
    progress(ProgressEvent("done", n, n, "지우기 완료"))
    return EraseResult(True, final_dir, pages, warnings)


# ---------------------------------------------------------------- 번역 엑셀
def export_excel(project_dir: Path | str, out: Path | str | None = None, pages: str | None = None,
                 force: bool = False) -> Path:
    """번역 검수 엑셀을 만든다. 이미 있으면 검수 내용을 지키려고 FileExistsError (force로 덮어씀)."""
    import pymupdf

    from app.excel_io import export_translation

    project = load_project(Path(project_dir))
    out = Path(out) if out else project.root / "번역_검수.xlsx"
    if out.exists() and not force:
        raise FileExistsError(str(out))
    with pymupdf.open(project.source_pdf) as d:
        page_list = parse_page_spec(pages or f"1-{d.page_count}", d.page_count)
    return export_translation(project, out, page_list)


# ---------------------------------------------------------------- 8단계: 한글 넣기
def compose(project_dir: Path | str, *, excel: Path | str | None = None, cleared: Path | str | None = None,
            pages: str | None = None, accept_issues: bool = False, test: bool = False,
            out_root: Path | str = "test_runs", progress: ProgressFn | None = None,
            cancel: CancelToken | None = None) -> ComposeResult:
    """지운 카드(cleared/) 위에 한글을 넣어 translated/에 쓴다. 지우기는 다시 하지 않는다.

    엑셀에 확인 항목이 있으면 state="needs_confirmation"으로 돌아온다. 화면은 issues를 보여 주고
    사용자가 계속을 고르면 accept_issues=True로 다시 부른다.
    """
    from app.excel_io import import_translation
    from app.typeset_compose import run_compose

    progress = progress or _noop
    project = load_project(Path(project_dir))
    cleared_dir = Path(cleared) if cleared else project.root / "cleared"
    if not cleared_dir.exists():
        return ComposeResult("blocked", blocking=[f"지운 결과가 없습니다: {cleared_dir} (먼저 7단계 지우기)"])
    cards, source, rows, issues = None, "samples.json", 0, []
    if excel:
        imp = import_translation(project, Path(excel))
        rows, issues = imp.rows, list(imp.issues)
        if imp.blocking:
            return ComposeResult("blocked", blocking=list(imp.blocking), issues=issues, rows=rows)
        if issues and not accept_issues:
            return ComposeResult("needs_confirmation", issues=issues, rows=rows)
        cards, source = imp.cards, str(Path(excel).resolve())

    final_dir, run_dir = _output_dirs(project.root, "translated", "compose", test, Path(out_root))
    out_name = "composed.pdf" if test else "translated.pdf"
    try:
        run_dir, code = run_compose(project.root, cleared_dir, pages, run_dir, cards=cards,
                                    translation_source=source, out_name=out_name,
                                    progress=progress, cancel=cancel)
        rep = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
        summary = rep["status_summary"]
        if code:
            return ComposeResult("failed", run_dir, issues=issues, status_summary=summary, rows=rows,
                                 error="원본 또는 지운 PDF 해시가 바뀌었습니다")
        if cancel:
            cancel.check()  # 마지막 확인: 취소했으면 예전 translated/를 바꾸지 않는다
    except BaseException:
        _discard(run_dir, final_dir)
        raise
    _commit(run_dir, final_dir)
    progress(ProgressEvent("done", 1, 1, "한글 넣기 완료"))
    return ComposeResult("done", final_dir, issues=issues, status_summary=summary, rows=rows)
