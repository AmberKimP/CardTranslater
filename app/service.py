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


def _included(file_dir: Path, page_count: int) -> list[int]:
    """쪽을 따로 주지 않았을 때 처리할 쪽: 파일 설정(file.json)에서 뺀 쪽을 제외한 전부."""
    from app.workspace import file_settings

    excl = set(file_settings(file_dir).get("exclude_pages", []))
    return [p for p in range(1, page_count + 1) if p not in excl]


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


# ---------------------------------------------------------------- 3단계: 글자 추출 (Claude 비전)
@dataclass
class ExtractResult:
    state: str  # "done" | "paused" | "waiting_batch" | "blocked"
    engine: str = "claude-code"
    equiv_usd: float = 0.0  # Claude Code 요금제로 읽은 분량의 API 정가 환산 (청구되지 않음)
    done_pages: list[int] = field(default_factory=list)
    remaining_pages: list[int] = field(default_factory=list)
    failed: dict = field(default_factory=dict)  # 쪽 -> 이유
    spent_usd: float = 0.0
    reason: str = ""  # 멈춘 이유
    retry_after: str | None = None  # 이 시각 뒤에 다시 실행하면 된다
    batch_id: str | None = None
    model: str = ""  # 쓴 모델 ID (1차)
    output_dir: Path | None = None  # 결과 폴더 (프로젝트 extract/ 또는 시험 폴더)
    mode: str = "double-check"
    recheck_cells: int = 0  # 두 번째 모델로 다시 읽은 칸
    recheck_agreed: int = 0  # 같게 읽어 '확인 필요'를 지운 칸
    recheck_disagreed: int = 0  # 다르게 읽어 '확인 필요'로 남긴 칸


EXTRACT_MODES = {  # 모드 -> (1차 모델, 다시 확인 모델 또는 None)
    "double-check": ("sonnet", "opus"),  # 기본: Sonnet으로 읽고 불확실한 칸만 Opus로 다시
    "opus": ("opus", None),  # Opus만
}


def extract(project_dir: Path | str, pages: str | None = None, *, mode: str = "double-check",
            model: str | None = None, engine: str = "claude-code", batch: bool = False,
            budget_usd: float | None = None, retry_failed: bool = False, test: bool = False,
            out_root: Path | str = "test_runs", client=None,
            progress: ProgressFn | None = None, cancel: CancelToken | None = None) -> ExtractResult:
    """영역 원문을 Claude로 읽어 extract/에 쪽마다 저장한다. 끊겨도 다시 부르면 이어 간다.

    mode: "double-check"(기본) - Sonnet으로 전부 읽고 '확인 필요' 칸만 Opus로 다시 읽어 비교한다.
          "opus" - Opus만으로 읽는다.
    model을 주면(시험·비교용) mode 대신 그 모델 하나로만 읽는다.

    API 한도(속도·크레딧)나 예산에 닿으면 state="paused"로 돌아온다(예외 아님).
    batch=True면 일괄 처리(50% 할인)로 보내고 state="waiting_batch"로 돌아온다. 나중에 다시 부르면
    결과를 받아 오고 남은 카드를 보낸다.
    engine: "claude-code"(기본, 로그인된 Claude Code 요금제로 `claude -p`, API 키 불필요)
            | "api"(Anthropic API, .env의 ANTHROPIC_API_KEY 또는 `ant auth login`, 사용료 별도).
            일괄 처리(batch)와 예산(budget_usd)은 api 엔진에서만 쓴다.
    test=True면 프로젝트 extract/ 대신 test_runs/extract-<프로젝트>-<모드>/ 에 쓴다(비교용, 다시 부르면 이어 감).
    """
    import pymupdf

    from app.extract import MODELS, make_client, resolve_model, run_extract, run_recheck

    project = load_project(Path(project_dir))
    if model is None and mode not in EXTRACT_MODES:
        return ExtractResult("blocked", mode=mode, reason=f"모르는 모드: {mode} (double-check, opus 중 하나)")
    first, check = (model, None) if model else EXTRACT_MODES[mode]
    try:
        model_id = resolve_model(first)
        check_id = resolve_model(check) if check else None
    except ValueError as e:
        return ExtractResult("blocked", reason=str(e))
    alias = next((k for k, v in MODELS.items() if v == model_id), model_id) if model else mode
    out_dir = (Path(out_root).resolve() / f"extract-{project.root.name}-{alias}") if test else None
    with pymupdf.open(project.source_pdf) as d:
        page_list = parse_page_spec(pages, d.page_count) if pages else _included(project.root, d.page_count)
    if engine not in ("claude-code", "api"):
        return ExtractResult("blocked", engine=engine, reason=f"모르는 엔진: {engine} (claude-code, api 중 하나)")
    if engine == "claude-code" and batch:
        return ExtractResult("blocked", engine=engine,
                             reason="일괄 처리(--batch)는 API 전용입니다. --engine api와 함께 쓰세요")
    if client is None:
        try:
            if engine == "claude-code":
                from app.extract_cc import ClaudeCodeClient

                client = ClaudeCodeClient()
            else:
                client = make_client()
        except Exception as e:  # noqa: BLE001  (키·claude 명령이 없을 때 등)
            return ExtractResult("blocked", engine=engine, reason=f"{engine} 엔진을 쓸 수 없습니다: {e}")
    r = run_extract(project, page_list, client, batch=batch, budget_usd=budget_usd, retry_failed=retry_failed,
                    model=model_id, out_dir=out_dir, progress=progress or _noop, cancel=cancel)
    res = ExtractResult(r.state, r.engine, r.equiv_usd, r.done_pages, r.remaining_pages, r.failed, r.spent_usd,
                        r.reason, r.retry_after, r.batch_id, r.model, r.out_dir, mode if not model else "single")
    if check_id and r.state == "done":
        # 두 번째 확인: 1차가 끝난 카드의 '확인 필요' 칸만. 끊기면 다시 불렀을 때 남은 칸부터.
        rc = run_recheck(project, page_list, client, base=r.out_dir, check_model=check_id,
                         progress=progress or _noop, cancel=cancel)
        res.recheck_cells, res.recheck_agreed, res.recheck_disagreed = rc.checked_cells, rc.agreed, rc.disagreed
        res.spent_usd = round(res.spent_usd + rc.spent_usd, 4)
        res.equiv_usd = round(res.equiv_usd + rc.equiv_usd, 4)
        if rc.state != "done":
            res.state, res.reason, res.retry_after = rc.state, f"두 번째 확인 중: {rc.reason}", rc.retry_after
    return res


def learn_tag_colors(project_dir: Path | str, save: bool = False) -> tuple[dict, list[str]]:
    """글자를 아는 카드(samples.json 원문, 그다음 확인 필요가 아닌 추출 결과)에서 태그 '라벨 색 -> 글자'
    사전을 배운다. save=True면 regions.json의 태그 영역 extract.tag_colors에 쓴다."""
    import pymupdf

    from app.extract import learn_tag_colors as _learn
    from app.extract import load_extracted

    project = load_project(Path(project_dir))
    known: dict = {}
    for pno, regs in load_extracted(project.root).items():
        for name, r in regs.items():
            if not r.get("uncertain") and isinstance(r.get("source"), list):
                known.setdefault(pno, {})[name] = r["source"]
    for pno, card in project.samples.items():  # 사람이 옮긴 것이 우선
        for name, r in card.items():
            if isinstance(r, dict) and isinstance(r.get("source"), list):
                known.setdefault(pno, {})[name] = r["source"]
    with pymupdf.open(stream=project.source_pdf.read_bytes(), filetype="pdf") as d:
        vocab, warns = _learn(project, d, known)
    if save and vocab:
        path = project.regions_path or project.root / "regions.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for r in data["regions"]:
            if r["name"] in vocab:
                r.setdefault("extract", {})["tag_colors"] = vocab[r["name"]]
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    return vocab, warns


def evaluate_extract(project_dir: Path | str, from_dir: Path | str | None = None) -> dict:
    """자동 추출 결과(기본: 프로젝트 extract/, 또는 시험 폴더)를 samples.json(손으로 옮긴 정답)과 비교한다."""
    from app.extract import evaluate, load_extracted

    project = load_project(Path(project_dir))
    return evaluate(load_extracted(project.root, Path(from_dir) if from_dir else None), project.samples)


# ---------------------------------------------------------------- 게임·프로젝트·그룹 (PRD 2장)
def list_games(root: Path | None = None) -> list[dict]:
    from app import workspace

    return workspace.list_games(root or workspace.DEFAULT_ROOT)


def new_project(name: str, game: str, source_language: str = "ja", root: Path | None = None):
    """(ProjectInfo, 새 게임인지). 게임 이름이 처음이면 게임을 만든다."""
    from app import workspace

    return workspace.create_project(name, game, source_language, root or workspace.DEFAULT_ROOT)


def list_groups(project_dir: Path | str) -> list[dict]:
    from app import workspace

    return workspace.list_groups(workspace.load_project_info(project_dir))


def add_files(project_dir: Path | str, files: list, group: str, *, new_group: bool = False,
              regions_from: Path | str | None = None) -> dict:
    """파일을 그룹에 넣는다. 화면은 먼저 list_groups로 기존 그룹을 보여 주고 ‘기존 그룹 / 새 그룹’을 고르게 한다."""
    from app import workspace

    return workspace.add_files(workspace.load_project_info(project_dir), files, group, new_group=new_group,
                               regions_from=regions_from)


@dataclass
class ProjectExtractResult:
    state: str  # done | paused | blocked | waiting_batch
    files: list[dict] = field(default_factory=list)  # 파일마다 ExtractResult 요약
    reason: str = ""
    retry_after: str | None = None


def extract_project(project_dir: Path | str, group: str | None = None, *, mode: str = "double-check",
                    engine: str = "claude-code", client=None, progress: ProgressFn | None = None,
                    cancel: CancelToken | None = None) -> ProjectExtractResult:
    """그룹(없으면 모든 그룹)의 모든 파일에서 텍스트를 추출한다. 멈추면 다시 불러 이어 간다."""
    from app import workspace

    info = workspace.load_project_info(project_dir)
    if group and group not in [g["name"] for g in workspace.list_groups(info)]:
        return ProjectExtractResult("blocked", reason=f"그룹이 없습니다: {group}")
    out = ProjectExtractResult("done")
    for fd in workspace.file_dirs(info, group):
        if not ((fd / "regions.json").exists() or (fd.parent.parent / "regions.json").exists()):
            out.files.append({"file": workspace.file_key(fd), "state": "blocked", "reason": "영역 템플릿 없음"})
            out.state, out.reason = "blocked", f"{workspace.file_key(fd)}: 영역 지정이 필요합니다"
            return out
        r = extract(fd, mode=mode, engine=engine, client=client, progress=progress, cancel=cancel)
        out.files.append({"file": workspace.file_key(fd), "state": r.state, "done": len(r.done_pages),
                          "remaining": len(r.remaining_pages), "failed": len(r.failed),
                          "recheck": (r.recheck_agreed, r.recheck_disagreed)})
        if r.state != "done":
            out.state, out.reason, out.retry_after = r.state, f"{workspace.file_key(fd)}: {r.reason}", r.retry_after
            return out
    return out


# ---------------------------------------------------------------- 아이콘 찾기·사전 (PRD 5.6)
@dataclass
class IconScanResult:
    state: str  # done | icons_review | blocked
    review_xlsx: Path | None = None
    auto: dict = field(default_factory=dict)  # 사전과 같아 바로 바꾼 아이콘 {이름: 횟수}
    groups: int = 0  # 이름을 정해야 할 묶음
    occurrences: int = 0
    not_found: int = 0  # 위치를 못 찾은 [[?]]
    reason: str = ""


ICON_SUGGEST_MODEL = "claude-sonnet-5-5"


def find_icons(project_dir: Path | str, *, engine: str = "claude-code", client=None,
               suggest: bool = True) -> IconScanResult:
    """원문의 이름 없는 아이콘 [[?]]를 찾아 사전과 같은 것은 바로 바꾸고, 나머지는 ‘아이콘_검증.xlsx’로 만든다."""
    from app import icons, workspace
    from app.extract import Paused, make_client
    from app.extract_cc import CardError, ClaudeCodeClient, NotAvailable

    info = workspace.load_project_info(project_dir)
    if not icons.pending_count(info):
        return IconScanResult("done")
    work = info.dir / "work" / "icons"
    res = icons.scan(info, work)
    out = IconScanResult("done", auto=res.auto)
    if not res.occurrences:
        return out
    note = ""
    if suggest and any(g.get("rep") for g in res.groups.values()):
        try:
            if client is None:
                client = ClaudeCodeClient() if engine == "claude-code" else make_client()
            icons.suggest_names(res, client, ICON_SUGGEST_MODEL, work / "suggest")
            icons._save_scan(work, res)
        except (Paused, CardError, NotAvailable, Exception) as e:  # noqa: BLE001 - 제안 없이도 검증은 할 수 있다
            note = f"이름 제안을 받지 못함: {getattr(e, 'reason', e)}"
    xlsx = icons.export_review(info, res, info.dir / "아이콘_검증.xlsx")
    return IconScanResult("icons_review", xlsx, res.auto, len(res.groups), len(res.occurrences),
                          sum(1 for o in res.occurrences if not o.png), note)


@dataclass
class IconImportResult:
    state: str  # confirmed | blocked
    added: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    replaced: int = 0
    removed: int = 0
    blocking: list[str] = field(default_factory=list)


def import_icons(project_dir: Path | str, xlsx: Path | str) -> IconImportResult:
    """아이콘 검증 엑셀을 가져와 게임 아이콘 사전에 넣고 원문의 [[?]]를 [[이름]]으로 바꾼다."""
    from app import icons, workspace

    info = workspace.load_project_info(project_dir)
    work = info.dir / "work" / "icons"
    if not (work / "scan.json").exists():
        return IconImportResult("blocked", blocking=["아이콘 찾기 결과가 없습니다 (find-icons를 먼저 하세요)"])
    r = icons.import_review(info, Path(xlsx), work)
    if r.blocking:
        return IconImportResult("blocked", blocking=r.blocking)
    return IconImportResult("confirmed", r.added, r.reused, r.replaced, r.removed)


# ---------------------------------------------------------------- 추출 완료 -> 용어집 검증 -> 확정 (PRD 5.3 가)
@dataclass
class GlossaryReviewResult:
    state: str  # review_ready | needs_confirmation | blocked | paused
    review_xlsx: Path | None = None
    new_terms: int = 0
    confirmed_terms: int = 0
    issues: list[str] = field(default_factory=list)
    reason: str = ""
    retry_after: str | None = None
    equiv_usd: float = 0.0
    spent_usd: float = 0.0


GLOSSARY_MODEL = "claude-opus-5-5"  # 용어 후보는 한 번만 묻고 전체 번역의 기준이 되므로 Opus


def finish_extraction(project_dir: Path | str, *, accept_issues: bool = False, engine: str = "claude-code",
                      client=None) -> GlossaryReviewResult:
    """‘추출 완료’: 모든 그룹·파일의 원문을 모아 용어 후보를 뽑고 용어집 검증 엑셀을 만든다."""
    from app import glossary, workspace
    from app.extract import Paused, make_client
    from app.extract_cc import CardError, ClaudeCodeClient, NotAvailable

    info = workspace.load_project_info(project_dir)
    status = glossary.extraction_status(info)
    if not status:
        return GlossaryReviewResult("blocked", reason="프로젝트에 파일이 없습니다 (add-files로 넣으세요)")
    issues = []
    for s in status:
        if not s["has_regions"]:
            issues.append(f"{s['file']}: 영역 템플릿이 없어 추출하지 못함")
        elif s["extracted"] < s["pages"] - len(s["failed"]):
            issues.append(f"{s['file']}: {s['pages']}장 중 {s['extracted']}장만 읽음")
        if s["failed"]:
            issues.append(f"{s['file']}: 실패한 카드 {len(s['failed'])}장 (용어 후보에서 빠짐)")
        if s["no_verdict"]:
            issues.append(f"{s['file']}: ‘결론 못 냄’ {s['no_verdict']}칸 (원문이 틀렸을 수 있음)")
    if issues and not accept_issues:
        return GlossaryReviewResult("needs_confirmation", issues=issues)
    # 이름 없는 아이콘이 남아 있으면 용어집으로 넘어가지 않는다 (PRD 2장 3단계 뒤 안전장치)
    ic = find_icons(info.dir, engine=engine, client=client)
    if ic.state == "icons_review":
        return GlossaryReviewResult("icons_review", review_xlsx=ic.review_xlsx, issues=issues,
                                    reason=f"이름을 정할 아이콘 묶음 {ic.groups}개 ({ic.occurrences}곳)"
                                           + (f", 위치 못 찾음 {ic.not_found}곳" if ic.not_found else "")
                                           + (f". {ic.reason}" if ic.reason else ""))
    sources = glossary.collect_sources(info)
    if not sources:
        return GlossaryReviewResult("blocked", issues=issues, reason="추출한 원문이 없습니다")
    if client is None:
        try:
            client = ClaudeCodeClient() if engine == "claude-code" else make_client()
        except Exception as e:  # noqa: BLE001
            return GlossaryReviewResult("blocked", issues=issues, reason=f"{engine} 엔진을 쓸 수 없습니다: {e}")
    try:
        terms, api_usd, equiv = glossary.find_candidates(info, sources, client, GLOSSARY_MODEL,
                                                         info.dir / "work" / "glossary")
    except Paused as p:
        from app.extract import _iso_after

        return GlossaryReviewResult("paused", issues=issues, reason=p.reason, retry_after=_iso_after(p.retry_after_s))
    except (CardError, NotAvailable) as e:
        return GlossaryReviewResult("blocked", issues=issues, reason=str(e))
    out = glossary.export_review(info, terms, info.dir / "용어집_검증.xlsx")
    workspace.set_status(info, "glossary_review")
    return GlossaryReviewResult("review_ready", out, sum(not t.confirmed for t in terms),
                                sum(t.confirmed for t in terms), issues, equiv_usd=equiv, spent_usd=api_usd)


@dataclass
class GlossaryConfirmResult:
    state: str  # confirmed | needs_confirmation | blocked
    version: int = 0
    terms: int = 0
    changed: list[str] = field(default_factory=list)  # 번역이 바뀐 확정 용어 (그 용어가 든 번역은 다시 해야 함)
    blocking: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


def import_glossary(project_dir: Path | str, xlsx: Path | str, *, accept_issues: bool = False) -> GlossaryConfirmResult:
    """검증한 용어집 엑셀을 검사하고, 문제가 없거나 확인하면 게임 용어집으로 확정한다."""
    from app import glossary, workspace

    info = workspace.load_project_info(project_dir)
    res = glossary.import_review(info, Path(xlsx))
    if res.blocking:
        return GlossaryConfirmResult("blocked", blocking=res.blocking, issues=res.issues)
    if not res.terms:
        return GlossaryConfirmResult("blocked", blocking=["확정할 용어가 없습니다 (번역 열이 모두 비어 있음)"])
    if res.issues and not accept_issues:
        return GlossaryConfirmResult("needs_confirmation", terms=len(res.terms), issues=res.issues)
    c = glossary.confirm(info, res.terms)
    workspace.set_status(info, "glossary_confirmed", glossary_version=c["version"])
    return GlossaryConfirmResult("confirmed", c["version"], c["terms"], c["changed"], issues=res.issues)


# ---------------------------------------------------------------- 5단계: 초벌 번역 (확정 용어집 기준)
TRANSLATE_MODELS = {"sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}


def translate_project(project_dir: Path | str, *, model: str = "sonnet", engine: str = "claude-code",
                      client=None, progress: ProgressFn | None = None, cancel: CancelToken | None = None):
    """확정 게임 용어집을 기준으로 프로젝트의 모든 그룹·파일 원문을 번역한다 (같은 원문은 한 번).
    용어집이 확정되기 전에는 막는다. 끊겨도 다시 부르면 남은 칸부터 이어 간다."""
    from app import glossary, workspace
    from app.extract import make_client
    from app.extract_cc import ClaudeCodeClient
    from app.translate import TranslateResult, run_translate

    info = workspace.load_project_info(project_dir)
    if info.status not in ("glossary_confirmed", "translating", "translated"):
        return TranslateResult("blocked", reason=f"용어집이 확정되지 않았습니다 (지금 상태: {info.status}). "
                                                 "finish-extraction -> import-glossary 를 먼저 하세요")
    terms = glossary.load_glossary(info).get("terms", [])
    sources = glossary.collect_sources(info)
    if not sources:
        return TranslateResult("blocked", reason="번역할 원문이 없습니다")
    if model not in TRANSLATE_MODELS:
        return TranslateResult("blocked", reason=f"모르는 모델: {model} (sonnet, opus)")
    if client is None:
        try:
            client = ClaudeCodeClient() if engine == "claude-code" else make_client()
        except Exception as e:  # noqa: BLE001
            return TranslateResult("blocked", reason=f"{engine} 엔진을 쓸 수 없습니다: {e}")
    workspace.set_status(info, "translating")
    from app.icons import load_dictionary

    r = run_translate(info.dir, sources, terms, client, TRANSLATE_MODELS[model], game=info.game,
                      work_dir=info.dir / "work" / "translate", progress=progress or _noop, cancel=cancel,
                      icons=load_dictionary(info)["icons"])
    if r.state == "done":
        workspace.set_status(info, "translated")
    return r


def export_project_excel(project_dir: Path | str, force: bool = False) -> list[Path]:
    """게임 프로젝트의 파일마다 번역 검수 엑셀을 만든다 (초벌 번역·용어 검사 포함)."""
    from app import workspace

    info = workspace.load_project_info(project_dir)
    out = []
    for fd in workspace.file_dirs(info):
        try:
            out.append(export_excel(fd, force=force))
        except FileExistsError:
            raise
    return out


# ---------------------------------------------------------------- 7·8단계를 프로젝트 단위로 (모든 그룹·파일)
def cards_from_translation(file_dir: Path) -> dict:
    """번역 검수 엑셀 없이 초벌 번역(프로젝트 번역 캐시)으로 한글 넣기용 카드를 만든다.
    {쪽: {영역: {"source", "ko"}}}. 번역이 없는 칸은 빠진다(그 칸은 지운 채로 남음)."""
    from app.excel_io import source_of
    from app.extract import load_extracted
    from app.project import load_project
    from app.translate import load_cache, lookup
    from app.workspace import included_pages, project_of

    info = project_of(file_dir)
    proj = load_project(file_dir)
    cache = load_cache(info.dir) if info else {}
    extracted = load_extracted(file_dir)
    cards: dict = {}
    for pno in included_pages(file_dir):
        for reg in proj.template.regions:
            src, _ = source_of(proj, extracted, pno, reg.name)
            tr = lookup(cache, src) if src else None
            if tr:
                cards.setdefault(pno, {"page": pno})[reg.name] = {"source": src, "ko": tr["ko"]}
    return cards


@dataclass
class ProjectStepResult:
    state: str  # done | blocked | needs_confirmation | failed
    files: list[dict] = field(default_factory=list)
    reason: str = ""


def erase_project(project_dir: Path | str, group: str | None = None, *, redo: bool = False,
                  progress: ProgressFn | None = None, cancel: CancelToken | None = None) -> ProjectStepResult:
    """7단계를 모든 그룹·파일에. 이미 지운 파일(cleared/)은 건너뛴다(redo=True면 다시)."""
    from app import workspace

    info = workspace.load_project_info(project_dir)
    out = ProjectStepResult("done")
    for fd in workspace.file_dirs(info, group):
        key = workspace.file_key(fd)
        if (fd / "cleared" / "cleared.pdf").exists() and not redo:
            out.files.append({"file": key, "state": "skipped", "reason": "이미 지움 (--redo로 다시)"})
            continue
        pages = workspace.included_pages(fd)
        if not pages:
            out.files.append({"file": key, "state": "skipped", "reason": "처리할 쪽 없음"})
            continue
        try:
            r = erase(fd, workspace.pages_spec(pages), progress=progress, cancel=cancel)
        except FileNotFoundError as e:  # 영역 템플릿 없음 등
            out.files.append({"file": key, "state": "blocked", "reason": str(e)})
            out.state, out.reason = "blocked", f"{key}: {e}"
            return out
        out.files.append({"file": key, "state": "done" if r.ok else "failed", "pages": len(r.pages),
                          "warnings": sum(len(w) for w in r.warnings.values()), "dir": str(r.output_dir)})
        if not r.ok:
            out.state, out.reason = "failed", f"{key}: {r.error}"
            return out
    return out


def compose_project(project_dir: Path | str, group: str | None = None, *, accept_issues: bool = False,
                    use_draft: bool = False, progress: ProgressFn | None = None,
                    cancel: CancelToken | None = None) -> ProjectStepResult:
    """8단계를 모든 그룹·파일에. 파일마다 검수한 번역_검수.xlsx가 있으면 그것, 없으면(또는 use_draft)
    초벌 번역으로 한글을 넣는다. 지우기는 다시 하지 않는다."""
    from app import workspace

    info = workspace.load_project_info(project_dir)
    out = ProjectStepResult("done")
    for fd in workspace.file_dirs(info, group):
        key = workspace.file_key(fd)
        if not (fd / "cleared").exists():
            out.files.append({"file": key, "state": "blocked", "reason": "지운 결과 없음 (erase-project 먼저)"})
            out.state, out.reason = "blocked", f"{key}: 지운 결과가 없습니다"
            continue
        xlsx = fd / "번역_검수.xlsx"
        if xlsx.exists() and not use_draft:
            r = compose(fd, excel=xlsx, accept_issues=accept_issues, progress=progress, cancel=cancel)
            src = "번역_검수.xlsx"
        else:
            r = _compose_with_cards(fd, cards_from_translation(fd), progress, cancel)
            src = "초벌 번역"
        rec = {"file": key, "state": r.state, "from": src, "issues": r.issues, "blocking": r.blocking,
               "summary": {k: len(v) for k, v in r.status_summary.items()},
               "dir": str(r.output_dir) if r.output_dir else None}
        out.files.append(rec)
        if r.state in ("blocked", "failed"):
            out.state, out.reason = r.state, f"{key}: {(r.blocking or [r.error or ''])[0]}"
        elif r.state == "needs_confirmation" and out.state == "done":
            out.state, out.reason = "needs_confirmation", f"{key}: 엑셀에 확인할 항목이 있습니다"
    return out


def _compose_with_cards(fd: Path, cards: dict, progress, cancel) -> ComposeResult:
    """번역 카드(dict)로 바로 한글 넣기 (엑셀 없이)."""
    from app.typeset_compose import run_compose

    project = load_project(fd)
    final_dir, run_dir = _output_dirs(project.root, "translated", "compose", False, Path("test_runs"))
    try:
        run_dir, code = run_compose(project.root, fd / "cleared", None, run_dir, cards=cards,
                                    translation_source="초벌 번역 (translate/cache.json)", out_name="translated.pdf",
                                    progress=progress or _noop, cancel=cancel)
        rep = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
        if code:
            return ComposeResult("failed", run_dir, status_summary=rep["status_summary"],
                                 error="원본 또는 지운 PDF 해시가 바뀌었습니다")
    except BaseException:
        _discard(run_dir, final_dir)
        raise
    _commit(run_dir, final_dir)
    return ComposeResult("done", final_dir, status_summary=rep["status_summary"])


# ================================================================ 화면용 (PySide6 등)
# 화면은 아래 함수와 위의 작업 함수만 부른다. 결과는 경로·상태를 담은 dict/dataclass로 준다.
REGION_KINDS = ("text", "tag_pills")
ALIGNS = ("left", "center", "right")


def list_projects(root: Path | None = None) -> list[dict]:
    """[{"name", "game", "status", "dir"}] (데이터 폴더의 프로젝트)."""
    from app import workspace

    return workspace.list_projects(Path(root) if root else workspace.DEFAULT_ROOT)


STATUS_LABEL = {
    "extracting": "추출 중", "glossary_review": "용어집 검증 중", "glossary_confirmed": "용어집 확정",
    "translating": "번역 중", "translated": "번역 완료",
}


def project_overview(project_dir: Path | str) -> dict:
    """프로젝트 현황: 그룹·파일별 쪽 수·뺀 쪽·추출·지우기·한글 넣기 상태와 다음에 할 일."""
    from app import workspace
    from app.extract import load_extracted

    info = workspace.load_project_info(project_dir)
    groups = []
    for g in workspace.list_groups(info):
        files = []
        for name in g["files"]:
            fd = g["dir"] / "files" / name
            n = workspace.page_count(fd)
            inc = workspace.included_pages(fd)
            done = set(load_extracted(fd)) & set(inc)
            files.append({
                "name": name, "key": workspace.file_key(fd), "dir": fd, "pages": n,
                "included": len(inc), "excluded": sorted(set(range(1, n + 1)) - set(inc)),
                "exclude_reason": workspace.file_settings(fd).get("exclude_reason", ""),
                "extracted": len(done), "erased": (fd / "cleared" / "cleared.pdf").exists(),
                "composed": (fd / "translated" / "translated.pdf").exists(),
                "review_xlsx": (fd / "번역_검수.xlsx") if (fd / "번역_검수.xlsx").exists() else None,
            })
        groups.append({"name": g["name"], "dir": g["dir"], "regions": g["regions"],
                       "has_regions": (g["dir"] / "regions.json").exists(), "files": files})
    return {"name": info.name, "game": info.game, "dir": info.dir, "status": info.status,
            "status_label": STATUS_LABEL.get(info.status, info.status),
            "glossary_version": info.extra.get("glossary_version"), "groups": groups,
            "next_step": _next_step(info, groups)}


def _next_step(info, groups: list[dict]) -> str:
    if not groups or not any(g["files"] for g in groups):
        return "파일을 넣으세요"
    no_regions = [g["name"] for g in groups if not g["has_regions"]]
    if no_regions:
        return f"영역을 지정하세요: {', '.join(no_regions)}"
    left = [f["key"] for g in groups for f in g["files"] if f["extracted"] < f["included"]]
    if info.status == "extracting":
        return f"글자를 추출하세요 ({len(left)}개 파일 남음)" if left else "‘추출 완료’를 보내 용어집을 만드세요"
    if info.status == "glossary_review":
        return "용어집 검증 엑셀을 고쳐서 가져오세요"
    if info.status in ("glossary_confirmed", "translating"):
        return "번역을 시작하세요"
    files = [f for g in groups for f in g["files"]]
    if not all(f["erased"] for f in files):
        return "글자를 지우세요"
    if not all(f["composed"] for f in files):
        return "한글을 넣으세요"
    return "결과를 확인하세요"


def set_pages(file_dir: Path | str, exclude: list[int], reason: str = "") -> dict:
    """파일에서 뺄 쪽을 정한다. {"excluded": [...], "included": [...]}."""
    from app import workspace

    n = workspace.page_count(Path(file_dir))
    bad = [p for p in exclude if not 1 <= int(p) <= n]
    if bad:
        raise ValueError(f"없는 쪽: {bad} (1~{n})")
    workspace.set_excluded_pages(Path(file_dir), exclude, reason)
    return {"excluded": sorted(set(exclude)), "included": workspace.included_pages(Path(file_dir))}


def page_count(file_dir: Path | str) -> int:
    from app import workspace

    return workspace.page_count(Path(file_dir))


def page_image(file_dir: Path | str, page: int, dpi: int = 110) -> tuple[bytes, tuple[float, float]]:
    """파일 폴더 원본의 한 쪽을 PNG로. (PNG, (너비 pt, 높이 pt))."""
    import pymupdf

    src = next((Path(file_dir) / "source").glob("*.pdf"))
    with pymupdf.open(src) as d:
        if not 1 <= page <= d.page_count:
            raise ValueError(f"없는 쪽: {page}")
        pg = d[page - 1]
        return pg.get_pixmap(dpi=dpi, alpha=False).tobytes("png"), (pg.rect.width, pg.rect.height)


def pdf_page_image(pdf: Path | str, page: int, dpi: int = 110) -> bytes:
    """임의 PDF의 한 쪽 PNG (결과 PDF 미리보기 등)."""
    import pymupdf

    with pymupdf.open(pdf) as d:
        return d[page - 1].get_pixmap(dpi=dpi, alpha=False).tobytes("png")


def font_choices() -> list[dict]:
    """고를 수 있는 한글 글꼴 [{"label", "family", "file"}] (fonts/에 실제로 있는 파일만)."""
    from app.typeset import ROOT

    cat = json.loads((ROOT / "fonts" / "catalog.json").read_text(encoding="utf-8"))
    out = []
    for fam in cat.get("families", []):
        d = ROOT / "fonts" / fam["dir"]
        for f in sorted(p for p in d.glob("*.[ot]tf") if "[" not in p.name) if d.exists() else []:  # 가변 글꼴 원본 제외
            weight = f.stem.split("-")[-1] if "-" in f.stem else ""
            family = fam["name"] + (f" {weight}" if weight and weight != "Regular" else "")
            out.append({"label": f"{family} — {fam.get('ko_name', '')}", "family": family,
                        "file": f"fonts/{fam['dir']}/{f.name}", "style": fam.get("style", "")})
    return out


def load_region_template(group_dir: Path | str) -> dict:
    """그룹의 regions.json 원본 dict (없으면 빈 템플릿). 편집기는 모르는 항목을 그대로 두고 저장한다."""
    p = Path(group_dir) / "regions.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"source_language": "ja", "reference_page": 1, "regions": [], "protect_rects": []}


def validate_region_template(data: dict) -> list[str]:
    from app.typeset import ROOT

    problems = []
    all_regions = list(data.get("regions", [])) + list(data.get("protect_rects", []))
    names = [str(r.get("name", "")).strip() for r in all_regions]
    if not data.get("regions"):
        problems.append("영역이 하나도 없습니다")
    if any(not n for n in names):
        problems.append("이름이 빈 영역이 있습니다")
    dup = sorted({n for n in names if n and names.count(n) > 1})
    if dup:
        problems.append(f"이름이 겹칩니다: {', '.join(dup)}")
    for r in all_regions:
        rc = r.get("rect") or {}
        try:
            x, y, w, h = (float(rc[k]) for k in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError):
            problems.append(f"{r.get('name')}: 좌표가 없습니다")
            continue
        if w <= 0 or h <= 0 or x < -0.001 or y < -0.001 or x + w > 1.001 or y + h > 1.001:
            problems.append(f"{r.get('name')}: 카드 밖으로 나갔거나 크기가 0입니다")
    for r in data.get("regions", []):
        if r.get("kind", "text") not in REGION_KINDS:
            problems.append(f"{r.get('name')}: 모르는 종류 {r.get('kind')}")
        st = r.get("style") or {}
        if int(st.get("rotate", 0) or 0) not in (0, 180):
            problems.append(f"{r.get('name')}: 회전은 0 또는 180만")
        if st.get("align", "left") not in ALIGNS:
            problems.append(f"{r.get('name')}: 정렬은 left/center/right")
        f = (st.get("font") or {}).get("file")
        if f and not (Path(f) if Path(f).is_absolute() else ROOT / f).exists():
            problems.append(f"{r.get('name')}: 글꼴 파일이 없습니다 ({f})")
    return problems


def save_region_template(group_dir: Path | str, data: dict) -> list[str]:
    """검사해서 문제가 없으면 regions.json에 저장하고 [] (이전 파일은 regions.json.bak). 문제가 있으면 목록."""
    problems = validate_region_template(data)
    if problems:
        return problems
    p = Path(group_dir) / "regions.json"
    if p.exists():
        shutil.copyfile(p, p.with_suffix(".json.bak"))
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return []


def engine_status() -> dict:
    """쓸 수 있는 엔진: {"claude-code": bool, "api": bool, "message"}. 키 값은 돌려주지 않는다."""
    import os

    cc = bool(shutil.which("claude") or shutil.which("claude.exe") or shutil.which("claude.cmd"))
    from app.settings import env_files

    api = bool(os.environ.get("ANTHROPIC_API_KEY")) or any(
        e.exists() and "ANTHROPIC_API_KEY" in e.read_text(encoding="utf-8", errors="ignore") for e in env_files())
    msg = ("Claude Code를 찾았습니다 (요금제로 실행)" if cc else
           "Claude Code가 없습니다. 설치·로그인하거나 API 키 방식을 쓰세요")
    return {"claude-code": cc, "api": api, "message": msg}


def get_settings():
    """앱 설정 (app.settings.Settings)."""
    from app import settings

    return settings.load()


def save_settings(data_dir: str | Path, engine: str = "claude-code") -> Path:
    from app import settings

    s = settings.load()
    s.data_dir, s.engine = str(data_dir), engine
    return settings.save(s)


def data_dir() -> Path:
    """games/·projects/가 들어가는 데이터 폴더."""
    from app import settings

    return settings.data_dir()


# ---------------------------------------------------------------- 화면용: 원문 확인 (2차)
def extracted_cells(file_dir: Path | str) -> list[dict]:
    """파일의 추출 결과를 칸마다: {"page", "region", "kind", "source", "label", "uncertain", "note",
    "readings", "edited"}. 뺀 쪽은 빠지고, 아직 읽지 않은 쪽은 source=None."""
    from app import workspace
    from app.extract import MODEL_LABEL, load_extracted, verification_label

    fd = Path(file_dir)
    proj = load_project(fd)
    ext = load_extracted(fd)
    out = []
    for pno in workspace.included_pages(fd):
        for reg in proj.template.regions:
            r = (ext.get(pno) or {}).get(reg.name)
            out.append({
                "page": pno, "region": reg.name, "kind": reg.kind,
                "source": None if r is None else r.get("source"),
                "label": "읽지 않음" if r is None else verification_label(r),
                "uncertain": bool(r and r.get("uncertain")),
                "note": (r or {}).get("note", ""),
                "readings": [{"model": MODEL_LABEL.get(x["model"], x["model"]), "source": x["source"]}
                             for x in (r or {}).get("readings") or []],
                "edited": bool(r and r.get("edited_by")),
            })
    return out


def region_crop(file_dir: Path | str, page: int, region: str) -> bytes:
    """원본에서 영역을 잘라 바로 세운 PNG (원문 확인 화면)."""
    import pymupdf

    from app.extract import region_crop_png

    fd = Path(file_dir)
    proj = load_project(fd)
    reg = next(r for r in proj.template.regions if r.name == region)
    with pymupdf.open(proj.source_pdf) as d:
        return region_crop_png(d[page - 1], reg, 1600)


def save_source(file_dir: Path | str, page: int, region: str, source) -> None:
    """사람이 고친 원문을 추출 결과에 저장한다 (확인 필요를 지우고, 모델이 읽은 값은 note에 남긴다).
    source: 글자 칸은 문자열, 태그 칸은 목록. 그 칸의 번역은 다음 번역 때 새 원문으로 다시 한다."""
    from app.extract import page_file

    fd = Path(file_dir)
    files = [p for p in (page_file(fd / "extract", page), fd / "extract" / f"page-{page:03d}.json") if p.exists()]
    if not files:  # 아직 읽지 않은 쪽: 손 입력으로 새로 만든다
        f = page_file(fd / "extract", page)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"page": page, "model": "사람", "regions": {}}, ensure_ascii=False), encoding="utf-8")
        files = [f]
    for f in files:
        rec = json.loads(f.read_text(encoding="utf-8"))
        r = rec.setdefault("regions", {}).setdefault(region, {"source": None, "uncertain": False, "note": ""})
        old = r.get("source")
        if old == source:
            continue
        r["source"] = source
        r["uncertain"] = False
        r["edited_by"] = "사람"
        r["note"] = (f"사람이 고침 (모델: {json.dumps(old, ensure_ascii=False)})" if old is not None
                     else "사람이 입력")
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(f)


# ---------------------------------------------------------------- 화면용: 아이콘 이름 확인 (2차)
def icon_review(project_dir: Path | str) -> dict | None:
    """find_icons 뒤 이름을 정할 묶음: {"groups": [{"group", "image", "suggest", "meaning", "ko", "count",
    "examples"}], "known": [사전 이름]}. 찾기 결과가 없으면 None."""
    from app import icons, workspace

    info = workspace.load_project_info(project_dir)
    work = info.dir / "work" / "icons"
    if not (work / "scan.json").exists():
        return None
    res = icons.load_scan(work)
    by_id = {o.id: o for o in res.occurrences}
    groups = []
    for g, gi in res.groups.items():
        members = [o for o in res.occurrences if o.group == g]
        groups.append({"group": g, "image": by_id[gi["rep"]].png if gi.get("rep") else None,
                       "suggest": gi.get("suggest", ""), "meaning": gi.get("meaning", ""), "ko": gi.get("ko", ""),
                       "count": len(members),
                       "examples": [f"{o.file_key} p{o.page} {o.region} {o.line}줄: {o.context}" for o in members],
                       "reason": members[0].reason if members and not gi.get("rep") else ""})
    return {"groups": groups, "known": sorted(icons.load_dictionary(info)["icons"])}


def confirm_icons(project_dir: Path | str, rows: list[dict]) -> IconImportResult:
    """화면에서 정한 아이콘 이름을 사전에 넣고 원문을 바꾼다.
    rows: [{"group", "name", "meaning", "ko", "scale", "not_icon"}]."""
    from app import icons, workspace

    info = workspace.load_project_info(project_dir)
    work = info.dir / "work" / "icons"
    if not (work / "scan.json").exists():
        return IconImportResult("blocked", blocking=["아이콘 찾기 결과가 없습니다"])
    xl = [{"묶음": r["group"], "이름": r.get("name", ""), "뜻(원문)": r.get("meaning", ""),
           "한국어 이름": r.get("ko", ""), "처리": icons.NOT_ICON if r.get("not_icon") else "",
           "표시 크기": r.get("scale") or icons.DEFAULT_SCALE} for r in rows]
    r = icons.import_rows(info, xl, work)
    if r.blocking:
        return IconImportResult("blocked", blocking=r.blocking)
    return IconImportResult("confirmed", r.added, r.reused, r.replaced, r.removed)


# ---------------------------------------------------------------- 화면용: 결과 보기·테스트 실행 (3차)
def result_pages(run_dir: Path | str) -> list[dict]:
    """한글 넣기 결과 폴더(translated/ 또는 시험 폴더)의 쪽마다: {"page", "card_png", "status": [..],
    "regions": [{"name", "status", "png"}]}. 상태(넘침·글자 축소·아이콘 오류 등)가 있는 칸을 표시하는 데 쓴다."""
    from app.typeset_compose import safe_name

    d = Path(run_dir)
    rep_f = d / "report.json"
    if not rep_f.exists():
        return []
    rep = json.loads(rep_f.read_text(encoding="utf-8"))
    out = []
    for pd in rep.get("pages_detail", []):
        pno = int(pd["page"])
        regions, page_status = [], set()
        for idx, (name, rec) in enumerate(pd.get("regions", {}).items()):
            st = list(rec.get("status") or [])
            for r in rec.get("results") or []:  # 태그 라벨마다
                st += [s for s in r.get("status") or [] if s not in st]
            page_status.update(st)
            png = d / "compare" / f"p{pno:03d}_{safe_name(name, idx)}.png"
            regions.append({"name": name, "status": st, "png": png if png.exists() else None,
                            "errors": rec.get("errors") or []})
        card = d / "compare" / f"p{pno:03d}_card.png"
        out.append({"page": pno, "card_png": card if card.exists() else None, "status": sorted(page_status),
                    "regions": regions})
    return out


@dataclass
class TestRunResult:
    state: str  # done | blocked
    run_dir: Path | None = None  # 시험 한글 넣기 폴더 (compare/, report.json)
    erase_dir: Path | None = None
    pages: list[int] = field(default_factory=list)
    status_summary: dict = field(default_factory=dict)
    reason: str = ""


def test_run(file_dir: Path | str, pages: str, *, out_root: Path | str | None = None,
             progress: ProgressFn | None = None, cancel: CancelToken | None = None) -> TestRunResult:
    """고른 쪽만 시험 폴더(데이터 폴더/test_runs)에서 지우기 -> 한글 넣기. 프로젝트의 cleared/·translated/는
    건드리지 않는다. 번역은 검수 엑셀이 있으면 그것, 없으면 초벌 번역."""
    from app.excel_io import import_translation
    from app.typeset_compose import run_compose

    fd = Path(file_dir)
    root = Path(out_root) if out_root else data_dir() / "test_runs"
    project = load_project(fd)
    import pymupdf

    with pymupdf.open(project.source_pdf) as d:
        page_list = parse_page_spec(pages, d.page_count)
    if not page_list:
        return TestRunResult("blocked", reason="쪽을 고르세요")
    er = erase(fd, pages, test=True, out_root=root, progress=progress, cancel=cancel)
    if not er.ok:
        return TestRunResult("blocked", erase_dir=er.output_dir, reason=er.error or "지우기 실패")
    xlsx = fd / "번역_검수.xlsx"
    if xlsx.exists():
        imp = import_translation(project, xlsx)
        cards, source = imp.cards, str(xlsx)
    else:
        cards, source = cards_from_translation(fd), "초벌 번역 (translate/cache.json)"
    _final, run_dir = _output_dirs(project.root, "translated", "compose", True, root)
    run_dir, _code = run_compose(project.root, er.output_dir, pages, run_dir, cards=cards, translation_source=source,
                                 out_name="composed.pdf", progress=progress or _noop, cancel=cancel)
    rep = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    return TestRunResult("done", run_dir, er.output_dir, page_list, rep.get("status_summary", {}))


def remember_project(project_dir: Path | str) -> None:
    """마지막으로 연 프로젝트 (다음에 앱을 켜면 이어서 연다)."""
    from app import settings

    s = settings.load()
    s.last_project = str(Path(project_dir).resolve())
    settings.save(s)


def last_project() -> Path | None:
    from app import settings

    p = settings.load().last_project
    return Path(p) if p and (Path(p) / "project.json").exists() else None


def self_check() -> dict:
    """설치(exe) 점검: 글꼴·라이브러리·한글 그리기가 되는지. {"ok": bool, ...}."""
    import pymupdf

    from app.typeset import ROOT, TextStyle, finalize, typeset_region

    from app import settings

    out: dict = {"root": str(ROOT), "data_dir": str(data_dir()), "frozen": settings.is_frozen()}
    fonts = font_choices()
    out["fonts"] = len(fonts)
    libs = {}
    for name in ("pymupdf", "cv2", "numpy", "openpyxl", "PIL", "anthropic", "fontTools"):
        try:
            m = __import__(name)
            libs[name] = getattr(m, "__version__", getattr(m, "VersionBind", "ok"))
        except Exception as e:  # noqa: BLE001
            libs[name] = f"없음: {e}"
    out["libs"] = libs
    try:
        doc = pymupdf.open()
        page = doc.new_page(width=200, height=100)
        ff = ROOT / (fonts[0]["file"] if fonts else "fonts/notosanskr/NotoSansKR-Regular.ttf")
        r = typeset_region(page, (10, 10, 190, 60), "한글 점검", TextStyle(font_file=str(ff), size_pt=10))
        finalize(doc)
        data = doc.tobytes()
        text = pymupdf.open(stream=data, filetype="pdf")[0].get_text()
        out["typeset"] = bool(r.drawn and "한글점검" in "".join(text.split()))
    except Exception as e:  # noqa: BLE001
        out["typeset"] = f"실패: {e}"
    out["engine"] = engine_status()
    out["ok"] = bool(out["fonts"]) and out["typeset"] is True and not any(
        str(v).startswith("없음") for v in libs.values())
    return out
