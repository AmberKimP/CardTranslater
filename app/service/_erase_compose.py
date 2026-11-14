"""⑦ 글자 지우기·⑧ 한글 넣기의 바탕 흐름 (진행률·취소, 쪽 단위 지우기·검사, 합성, 엑셀 내보내기)."""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app import applog, report
from app.erase import erase_page, find_background_image, load_image_rgb, render_on_image_grid
from app.project import ensure_not_source, load_project, parse_page_spec, sha256_file
from app.typeset_compose import safe_name
from app.service import _base
from app.service import _translate_erase


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
    sheet_images: list[Path] = field(default_factory=list)  # 배열 이미지로 넣었으면 원래 배열로 다시 합친 이미지
    sheet_error: str | None = None  # 합치지 못한 이유 (한글 넣기 결과는 그대로 쓸 수 있다)
    reused_pages: list[int] = field(default_factory=list)  # 바뀌지 않아 예전 결과를 그대로 쓴 쪽 (F19)
    composed_pages: list[int] = field(default_factory=list)  # 새로 한글을 넣은 쪽


# ---------------------------------------------------------------- 폴더
def _output_dirs(project_root: Path, folder: str, kind: str, test: bool, out_root: Path) -> tuple[Path, Path]:
    """(최종 폴더, 작업 폴더). 프로젝트 모드는 숨은 작업 폴더에 다 쓴 뒤 _commit으로 바꿔 넣는다."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if test:
        d = Path(out_root).resolve() / f"{stamp}-{kind}"
        n = 2
        while d.exists():  # 같은 초에 여러 번 (그룹 시험 실행의 파일마다): 겹쳐 쓰지 않게
            d = Path(out_root).resolve() / f"{stamp}-{n}-{kind}"
            n += 1
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


def _included(file_dir: Path, page_count: int) -> list[int]:
    """쪽을 따로 주지 않았을 때 처리할 쪽: 파일 설정(file.json)에서 뺀 쪽을 제외한 전부
    (프로젝트가 시험 모드면 그중 앞 card_limit장)."""
    from app.workspace import file_settings

    from app.workspace import limit_pages

    excl = set(file_settings(file_dir).get("exclude_pages", []))
    return limit_pages(file_dir, [p for p in range(1, page_count + 1) if p not in excl])


def _boxes(page) -> dict:
    doc = page.parent
    out = {"mediabox": [round(v, 3) for v in page.mediabox], "cropbox": [round(v, 3) for v in page.cropbox]}
    for key in ("BleedBox", "TrimBox", "ArtBox"):
        out[key] = doc.xref_get_key(page.xref, key)[1]
    return out


# ---------------------------------------------------------------- 7단계: 지우기
def erase(project_dir: Path | str, pages: str, *, test: bool = False, out_root: Path | str = "test_runs",
          progress: _base.ProgressFn | None = None, cancel: _base.CancelToken | None = None,
          workers: int | None = None) -> EraseResult:
    """영역 글자를 지워 cleared/ (test면 test_runs/<시각>-erase/)에 쓴다. 원본은 읽기만 한다.
    workers: 동시에 돌릴 프로세스 수 (None이면 쪽 수·CPU에 맞춰, 1이면 나누지 않음: app.parallel)."""
    progress = progress or _base._noop
    project = load_project(Path(project_dir))
    final_dir, run_dir = _output_dirs(project.root, "cleared", "erase", test, Path(out_root))
    run_dir.mkdir(parents=True, exist_ok=False)
    try:
        return _erase(project, pages, final_dir, run_dir, test, progress, cancel, workers)
    except BaseException:
        _discard(run_dir, final_dir)
        raise


def _erase(project, pages_spec, final_dir, run_dir, test, progress, cancel, workers=None) -> EraseResult:
    import pymupdf

    from app import parallel

    src_path = project.source_pdf
    sha_before = sha256_file(src_path)
    ensure_not_source(run_dir / "x", src_path)
    pdf_name = "erased.pdf" if test else "cleared.pdf"
    with pymupdf.open(src_path) as d:
        pages = parse_page_spec(pages_spec, d.page_count)
    w = parallel.worker_count(len(pages), workers)
    t_start = time.monotonic()

    def serial():
        return _erase_chunk(project, pages, run_dir, pdf_name, progress, cancel)
    if w <= 1:
        page_metas, box_checks = serial()
    else:  # 작업 프로세스를 못 띄웠거나 합친 결과가 다르면 한 프로세스로 (결과는 같다)
        page_metas, box_checks = parallel.run_or_fallback(
            lambda: _erase_parallel(project, pages, run_dir, pdf_name, w, progress, cancel), serial, run_dir)
    res = _finish_erase(project, pages, page_metas, box_checks, final_dir, run_dir, pdf_name, sha_before,
                        progress, cancel)
    from app import runlog

    applog.safe(runlog.log_erase, project.root.name, page_metas, time.monotonic() - t_start, w)
    return res


def _erase_chunk(project, pages, run_dir, pdf_name, progress, cancel) -> tuple[list, list]:
    """pages를 지워 run_dir/pdf_name에 저장하고 확인·비교 이미지(run_dir/compare)를 만든다. (쪽 메타, 상자 확인)."""
    import pymupdf

    src_path = project.source_pdf
    src_bytes = src_path.read_bytes()  # 원본은 메모리로만 읽는다
    src_doc = pymupdf.open(stream=src_bytes, filetype="pdf")
    n = len(pages)

    out_doc = pymupdf.open(stream=src_bytes, filetype="pdf")
    out_doc.select([p - 1 for p in pages])

    from app.workspace import file_settings

    card_modes = file_settings(project.root).get("erase_modes", {})  # 카드별 지우기 방식 {쪽: {영역: 방식}}
    try:  # 읽은 원문: 지울 줄을 원문 줄에 맞춘다 (원문에 없는 작은 줄·아이콘만 있는 줄은 남긴다)
        from app.extract import load_extracted

        extracted = load_extracted(project.root)
    except Exception:  # noqa: BLE001 - 원문이 없어도 지우기는 한다
        extracted = {}
    from app import icons as _icons

    templates = _icons.templates_for(project.root)  # 글 속 아이콘 자리를 사전 그림으로 다시 확인한다
    page_metas = []
    results = []
    for i, pno in enumerate(pages):
        if cancel:
            cancel.check()
        progress(_base.ProgressEvent("erase", i, n, f"{pno}쪽 지우는 중", pno))
        srcs, kept = _translate_erase.erase_sources(extracted.get(pno))
        t_page = time.monotonic()
        tpl = project.template.for_page(pno)
        skip = _translate_erase.icon_only_regions(srcs, kept)
        keep_lines = _translate_erase.erase_keep_lines(extracted.get(pno))
        sig = _translate_erase.erase_sig(project, pno, srcs, card_modes.get(str(pno)), kept, keep_lines)
        whole: list[str] = []
        if keep_lines:
            # 아이콘 문단만 남길 칸은 글자 모양만 지우는 방식에서만 줄을 골라 남길 수 있다. 영역을 통째로 지우는 방식
            # (텍스트 레이어가 있는 쪽 등)이면 남길 수 없어 칸 전체를 원본 그대로 둔다 (아이콘을 지우는 것보다 안전)
            from app.erase import page_spans, uses_text_mask

            has_layer = bool(page_spans(out_doc[i]))
            modes_here = card_modes.get(str(pno)) or {}
            whole = [reg.name for reg in tpl.regions if reg.name in keep_lines
                     and not (modes_here.get(reg.name) or uses_text_mask(reg, has_layer))]
            skip = sorted(set(skip) | set(whole))
            keep_lines = {k: v for k, v in keep_lines.items() if k not in whole}
            srcs = {**srcs, **{k: v["source"] for k, v in keep_lines.items()}}  # 비게 된 남길 줄도 줄 수에 든다
        if skip:  # 아이콘(과 숫자·기호)만 있는 칸: 지우지도 한글을 넣지도 않고 원본 그대로 (메타에 없으면 한글 넣기도 건너뛴다)
            tpl.regions = [reg for reg in tpl.regions if reg.name not in skip]
        with applog.context(프로젝트=project.root.name, 쪽=pno):
            r = erase_page(out_doc[i], tpl, project.samples.get(pno), seed=pno,
                           modes=card_modes.get(str(pno)), sources=srcs, icon_templates=templates,
                           keep_lines=keep_lines)
        if skip:
            r.meta["icon_only_regions"] = skip
        if whole:  # 결과 보기에서 알린다 (아이콘 문단만 남기려 했는데 칸 전체가 원본 그대로)
            r.meta["keep_fallback_regions"] = whole
        page_metas.append({
            "timing": {"erase_s": round(time.monotonic() - t_page, 3)},
            "schema": _base.SCHEMA,
            "source_pdf": str(src_path),
            "source_page": pno,
            "output_pdf": pdf_name,
            "output_page_index": i,
            "erase_sig": sig,
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
    return page_metas, box_checks


def _erase_part(pages: list[int], k: int, project_root: str, run_dir: str) -> tuple[list, list]:
    """(작업 프로세스) 이어진 쪽 묶음 하나를 지워 run_dir/.part-k.pdf에 쓴다. 비교 이미지는 run_dir/compare에 바로.
    쪽마다 지우기(erase)·확인(verify) 단계를 알리고, 화면에서 취소하면 다음 쪽에서 멈춘다."""
    from app.parallel import tick_progress, worker_cancel

    project = load_project(Path(project_root))
    return _erase_chunk(project, pages, Path(run_dir), f".part-{k:03d}.pdf", tick_progress(), worker_cancel())


def _erase_parallel(project, pages, run_dir, pdf_name, workers, progress, cancel) -> tuple[list, list]:
    """쪽을 묶음으로 나눠 여러 프로세스로 지우고 한 PDF로 합친다 (쪽 순서·output_page_index를 맞춘다)."""
    import pymupdf

    from app import parallel

    n = len(pages)
    parts = parallel.split(pages, workers * parallel.PARTS_PER_WORKER)
    progress(_base.ProgressEvent("erase", 0, 2 * n, f"{n}쪽 지우는 중 (동시에 {workers}개)"))

    def on_tick(c: dict):  # 쪽마다 지우기 + 확인 두 단계
        e, v = min(c.get("erase", 0), n), min(c.get("verify", 0), n)
        msg = f"지우기 {e}/{n}쪽 · 확인 {v}/{n}쪽 (동시에 {workers}개)"
        progress(_base.ProgressEvent("erase", e + v, 2 * n, msg))
    res = parallel.run_parts(_erase_part, parts, [(str(project.root), str(run_dir))] * len(parts), workers,
                             on_tick, cancel)
    progress(_base.ProgressEvent("verify", n, n, "지운 쪽을 한 파일로 합치는 중"))
    page_metas, box_checks = [], []
    offset = 0
    for k, (metas, boxes) in enumerate(res):
        for m in metas:
            m["output_page_index"] += offset
            m["output_pdf"] = pdf_name  # 묶음 파일 이름 대신 합친 파일
        offset += len(parts[k])
        page_metas += metas
        box_checks += boxes
    out_pdf = run_dir / pdf_name
    ensure_not_source(out_pdf, project.source_pdf)
    parallel.merge_parts(run_dir, len(parts), out_pdf)  # 합친 쪽이 묶음 쪽과 똑같이 그려지는지도 확인
    src_doc = pymupdf.open(project.source_pdf)
    check = pymupdf.open(out_pdf)
    try:  # 합치면 PDF 안의 번호(xref)가 바뀐다: 배경·패치 스트림 정보를 합친 파일에서 다시 적는다
        for i, (pno, m) in enumerate(zip(pages, page_metas)):
            _stream_meta(src_doc, check, pno, i, m)
    finally:
        check.close()
        src_doc.close()
    return page_metas, box_checks


def _stream_meta(src_doc, check, pno: int, i: int, meta: dict):
    """원본 배경 이미지 스트림이 바이트 그대로인지와 패치 이미지의 xref·압축 방식을 meta에 적는다. (bg_o, 패치 정보)."""
    sp, op = src_doc[pno - 1], check[i]
    bg = find_background_image(sp)
    bg_o = find_background_image(op)
    s_raw = src_doc.xref_stream_raw(bg.xref)
    o_raw = check.xref_stream_raw(bg_o.xref)
    bgm = meta["background_image"]
    bgm["stream_sha256_source"] = hashlib.sha256(s_raw).hexdigest()
    bgm["stream_sha256_output"] = hashlib.sha256(o_raw).hexdigest()
    bgm["stream_untouched"] = s_raw == o_raw
    bgm["filter"] = check.xref_get_key(bg_o.xref, "Filter")[1]
    bgm["output_xref"] = bg_o.xref
    # 패치: 저장 뒤 xref·압축 방식 다시 확인 (garbage 정리로 번호가 바뀔 수 있음)
    infos = [x for x in op.get_image_info(xrefs=True) if x.get("xref", 0) not in (0, bg_o.xref)]
    for pt in meta["patches"]:
        match = min(infos, key=lambda x: sum(abs(a - b) for a, b in zip(x["bbox"], pt["bbox_pt"])))
        pt["xref"] = match["xref"]
        pt["filter"] = check.xref_get_key(match["xref"], "Filter")[1]
        pt["stream_bytes"] = len(check.xref_stream_raw(match["xref"]))


def _verify(pymupdf, project, pages, page_metas, results, src_doc, check, run_dir, progress, cancel) -> list:
    n = len(pages)
    box_checks = []
    for i, pno in enumerate(pages):
        if cancel:
            cancel.check()
        progress(_base.ProgressEvent("verify", i, n, f"{pno}쪽 확인·비교 이미지", pno))
        t_page = time.monotonic()
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

        _stream_meta(src_doc, check, pno, i, page_metas[i])  # 원본 배경 스트림 그대로인지, 패치 xref·압축
        outside = np.ones(raw.shape[:2], bool)
        for pt in page_metas[i]["patches"]:
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
        if res.clean is None:  # 지울 영역이 없는 쪽 (아이콘만 있는 칸만 있었음): 카드 비교 그림만
            page_metas[i]["compare_card_png"] = str(pdir / f"{tag}_card.png")
            page_metas[i].setdefault("timing", {})["verify_s"] = round(time.monotonic() - t_page, 3)
            continue
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
        page_metas[i].setdefault("timing", {})["verify_s"] = round(time.monotonic() - t_page, 3)
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
    progress(_base.ProgressEvent("done", n, n, "지우기 완료"))
    return EraseResult(True, final_dir, pages, warnings)


# ---------------------------------------------------------------- 번역 엑셀
def export_excel(project_dir: Path | str, out: Path | str | None = None, pages: str | None = None,
                 force: bool = False) -> Path:
    """번역 검수 엑셀을 만든다. 이미 있으면 검수 내용을 지키려고 FileExistsError.
    force로 다시 만들 때도 이전 엑셀에서 사람이 고친 번역·메모·글자 크기는 옮겨 담는다."""
    import pymupdf

    from app.excel_io import export_translation, read_human_edits

    project = load_project(Path(project_dir))
    out = Path(out) if out else project.root / "번역_검수.xlsx"
    if out.exists() and not force:
        raise FileExistsError(str(out))
    with pymupdf.open(project.source_pdf) as d:
        page_list = parse_page_spec(pages, d.page_count) if pages else _included(project.root, d.page_count)
    keep = read_human_edits(out) if out.exists() else {}
    return export_translation(project, out, page_list, keep=keep)


# ---------------------------------------------------------------- 8단계: 한글 넣기
def _reuse_dir(final_dir: Path, cleared_dir: Path) -> Path | None:
    """예전 translated/가 같은 지운 결과로 만든 것이면 그 폴더 (바뀐 카드만 다시 넣는다, F19), 아니면 None."""
    try:
        old = json.loads((final_dir / "report.json").read_text(encoding="utf-8"))
        same = Path(old["erase_run"]).resolve() == Path(cleared_dir).resolve()
    except Exception:  # noqa: BLE001 - 없음·깨짐: 전부 새로 넣는다
        return None
    return final_dir if same else None


def compose(project_dir: Path | str, *, excel: Path | str | None = None, cleared: Path | str | None = None,
            pages: str | None = None, accept_issues: bool = False, test: bool = False,
            out_root: Path | str = "test_runs", progress: _base.ProgressFn | None = None,
            cancel: _base.CancelToken | None = None) -> ComposeResult:
    """지운 카드(cleared/) 위에 한글을 넣어 translated/에 쓴다. 지우기는 다시 하지 않는다.

    엑셀에 확인 항목이 있으면 state="needs_confirmation"으로 돌아온다. 화면은 issues를 보여 주고
    사용자가 계속을 고르면 accept_issues=True로 다시 부른다.
    """
    from app.excel_io import import_translation
    from app.typeset_compose import run_compose

    progress = progress or _base._noop
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
    reuse = None if (test or pages) else _reuse_dir(final_dir, cleared_dir)  # 시험·쪽 지정은 늘 전부 새로
    try:
        run_dir, code = run_compose(project.root, cleared_dir, pages, run_dir, cards=cards,
                                    translation_source=source, out_name=out_name,
                                    progress=progress, cancel=cancel, reuse_dir=reuse)
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
    res = ComposeResult("done", final_dir, issues=issues, status_summary=summary, rows=rows,
                        reused_pages=rep.get("reused_pages", []), composed_pages=rep.get("composed_pages", []))
    if not test:
        _translate_erase._attach_sheet_images(project.root, final_dir, res, progress)
    progress(_base.ProgressEvent("done", 1, 1, "한글 넣기 완료"))
    return res
