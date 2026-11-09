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

from app.version import VERSION  # noqa: F401 (화면이 버전을 service로 읽는다)
from app import report
from app.claude_setup import find_claude
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
    sheet_images: list[Path] = field(default_factory=list)  # 배열 이미지로 넣었으면 원래 배열로 다시 합친 이미지
    sheet_error: str | None = None  # 합치지 못한 이유 (한글 넣기 결과는 그대로 쓸 수 있다)


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

    from app.workspace import file_settings

    card_modes = file_settings(project.root).get("erase_modes", {})  # 카드별 지우기 방식 {쪽: {영역: 방식}}
    try:  # 읽은 원문: 지울 줄을 원문 줄에 맞춘다 (원문에 없는 작은 줄·아이콘만 있는 줄은 남긴다)
        from app.extract import load_extracted

        extracted = load_extracted(project.root)
    except Exception:  # noqa: BLE001 - 원문이 없어도 지우기는 한다
        extracted = {}
    page_metas = []
    results = []
    for i, pno in enumerate(pages):
        if cancel:
            cancel.check()
        progress(ProgressEvent("erase", i, n, f"{pno}쪽 지우는 중", pno))
        srcs = {k: v.get("source") for k, v in (extracted.get(pno) or {}).items()}
        r = erase_page(out_doc[i], project.template.for_page(pno), project.samples.get(pno), seed=pno,
                       modes=card_modes.get(str(pno)), sources=srcs)
        page_metas.append({
            "schema": SCHEMA,
            "source_pdf": str(src_path),
            "source_page": pno,
            "output_pdf": pdf_name,
            "output_page_index": i,
            "erase_sig": erase_sig(project, pno, srcs, card_modes.get(str(pno))),
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
    res = ComposeResult("done", final_dir, issues=issues, status_summary=summary, rows=rows)
    if not test:
        _attach_sheet_images(project.root, final_dir, res, progress)
    progress(ProgressEvent("done", 1, 1, "한글 넣기 완료"))
    return res


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


def scoped_file_dirs(info, group: str | None = None, files: list | None = None) -> list[Path]:
    """작업 범위의 파일 폴더: files(화면에서 고른 파일들)가 있으면 그것만, 없으면 그룹(없으면 전체)의 모든 파일."""
    from app import workspace

    all_ = workspace.file_dirs(info, group)
    if files is None:
        return all_
    want = {Path(f).resolve() for f in files}
    return [fd for fd in all_ if fd.resolve() in want]


def extract_project(project_dir: Path | str, group: str | None = None, *, mode: str = "double-check",
                    engine: str = "claude-code", client=None, progress: ProgressFn | None = None,
                    cancel: CancelToken | None = None, files: list | None = None) -> ProjectExtractResult:
    """그룹(없으면 모든 그룹)의 모든 파일에서 텍스트를 추출한다. files를 주면 그 파일만. 멈추면 다시 불러 이어 간다."""
    from app import workspace

    info = workspace.load_project_info(project_dir)
    if group and group not in [g["name"] for g in workspace.list_groups(info)]:
        return ProjectExtractResult("blocked", reason=f"그룹이 없습니다: {group}")
    out = ProjectExtractResult("done")
    for fd in scoped_file_dirs(info, group, files):
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
    work = info.game_dir / "work" / "icons"
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
    xlsx = icons.export_review(info, res, info.game_dir / "아이콘_검증.xlsx")  # 아이콘 검증은 게임 단위
    _retire_legacy(info, ["아이콘_검증.xlsx"])
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
    work = info.game_dir / "work" / "icons"
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
    status = glossary.extraction_status(info, game=True)  # 용어집 검증은 게임 단위 (모든 프로젝트)
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
    sources = glossary.collect_sources(info, game=True)
    if not sources:
        return GlossaryReviewResult("blocked", issues=issues, reason="추출한 원문이 없습니다")
    if client is None:
        try:
            client = ClaudeCodeClient() if engine == "claude-code" else make_client()
        except Exception as e:  # noqa: BLE001
            return GlossaryReviewResult("blocked", issues=issues, reason=f"{engine} 엔진을 쓸 수 없습니다: {e}")
    try:
        terms, api_usd, equiv = glossary.find_candidates(info, sources, client, GLOSSARY_MODEL,
                                                         info.game_dir / "work" / "glossary")
    except Paused as p:
        from app.extract import _iso_after

        return GlossaryReviewResult("paused", issues=issues, reason=p.reason, retry_after=_iso_after(p.retry_after_s))
    except (CardError, NotAvailable) as e:
        return GlossaryReviewResult("blocked", issues=issues, reason=str(e))
    out = glossary.export_review(info, terms, info.game_dir / "용어집_검증.xlsx")
    _retire_legacy(info, ["용어집_검증.xlsx"])
    for p_ in workspace.game_projects(info):  # 같은 게임의 프로젝트가 함께 용어집 검증 단계로
        if p_.dir == info.dir or p_.status in ("extracting", "glossary_review"):
            workspace.set_status(p_, "glossary_review")
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
    # 번역한 뒤 다시 가져와도 번역이 바뀐 용어가 없으면 번역 상태를 그대로 둔다 (바뀌면 다시 번역해야 함)
    # 용어집은 게임 단위: 같은 게임에서 용어집 검증 중이던 프로젝트도 함께 확정된다
    for p_ in workspace.game_projects(info):
        if p_.dir != info.dir and p_.status != "glossary_review":
            continue
        keep = p_.status in ("translating", "translated") and not c["changed"]
        workspace.set_status(p_, p_.status if keep else "glossary_confirmed", glossary_version=c["version"])
    return GlossaryConfirmResult("confirmed", c["version"], c["terms"], c["changed"], issues=res.issues)


# ---------------------------------------------------------------- 5단계: 초벌 번역 (확정 용어집 기준)
TRANSLATE_MODELS = {"sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}


def translate_project(project_dir: Path | str, *, model: str = "sonnet", engine: str = "claude-code",
                      client=None, progress: ProgressFn | None = None, cancel: CancelToken | None = None,
                      files: list | None = None):
    """확정 게임 용어집을 기준으로 프로젝트의 모든 그룹·파일 원문을 번역한다 (같은 원문은 한 번).
    files를 주면 그 파일의 원문만 (상태는 프로젝트 전체를 번역했을 때만 ‘번역 완료’가 된다).
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
    partial = False
    if files is not None:
        keys = {workspace.file_key(fd) for fd in scoped_file_dirs(info, None, files)}
        partial = keys != {workspace.file_key(fd) for fd in workspace.file_dirs(info)}
        sources = [s_ for s_ in sources if s_.key.split("#", 1)[0] in keys]
    if not sources:
        return TranslateResult("blocked", reason="번역할 원문이 없습니다")
    if model not in TRANSLATE_MODELS:
        return TranslateResult("blocked", reason=f"모르는 모델: {model} (sonnet, opus)")
    if client is None:
        try:
            client = ClaudeCodeClient() if engine == "claude-code" else make_client()
        except Exception as e:  # noqa: BLE001
            return TranslateResult("blocked", reason=f"{engine} 엔진을 쓸 수 없습니다: {e}")
    if not (partial and info.status == "translated"):
        workspace.set_status(info, "translating")
    from app.icons import load_dictionary

    r = run_translate(info.dir, sources, terms, client, TRANSLATE_MODELS[model], game=info.game,
                      work_dir=info.dir / "work" / "translate", progress=progress or _noop, cancel=cancel,
                      icons=load_dictionary(info)["icons"])
    if r.state == "done":
        if not partial or info.status == "translated":
            workspace.set_status(info, "translated")
        if (r.translated or r.from_glossary) and (info.game_dir / "번역_검수.xlsx").exists():
            # 검수 엑셀이 이미 있으면 새 번역으로 다시 만든다 (한글 넣기는 엑셀을 먼저 쓰므로, 옛 번역이 남지 않게).
            # 사람이 엑셀에서 고친 번역·메모·글자 크기는 옮겨 담는다.
            try:
                export_project_excel(info.dir, force=True)
            except OSError as e:
                r.excel_note = f"검수 엑셀을 새 번역으로 다시 만들지 못했습니다 (엑셀에서 열려 있나요?): {e}"
    elif partial and info.status == "translated":
        pass  # 이미 전체 번역을 마친 프로젝트는 그대로
    return r


def game_dir(project_dir: Path | str) -> Path:
    """프로젝트가 속한 게임 폴더 (용어집·아이콘 검증, 번역 검수 엑셀이 있는 곳)."""
    from app import workspace

    return workspace.load_project_info(project_dir).game_dir


def review_xlsx_for(file_dir: Path | str) -> Path | None:
    """파일의 번역 검수 엑셀: 게임 검수 엑셀에 이 파일이 있으면 그것, 없으면 예전 파일별 엑셀, 둘 다 없으면 None."""
    from app import workspace
    from app.excel_io import game_files

    fd = Path(file_dir)
    info = workspace.project_of(fd)
    if info is not None:
        gx = info.game_dir / "번역_검수.xlsx"
        if gx.exists():
            files = game_files(gx) or []
            pdf = next((fd / "source").glob("*.pdf"), None)
            names = workspace.group_names(fd.parent.parent)  # 이름을 바꾼 그룹은 예전 이름도
            if any(f["프로젝트"] == info.name and f["그룹"] in names and pdf and f["원본 파일"] == pdf.name
                   for f in files):
                return gx
    legacy = fd / "번역_검수.xlsx"
    return legacy if legacy.exists() else None


def export_project_excel(project_dir: Path | str, force: bool = False) -> list[Path]:
    """게임의 번역 검수 엑셀 하나(games/<게임>/번역_검수.xlsx)를 만든다: 같은 게임의 모든 프로젝트, 그룹마다 시트.
    이미 있으면 FileExistsError (force면 다시 만들되 사람이 고친 번역·메모·글자 크기는 옮겨 담는다)."""
    from app import workspace
    from app.excel_io import export_game, group_of, read_human_edits

    info = workspace.load_project_info(project_dir)
    out = info.game_dir / "번역_검수.xlsx"
    if out.exists() and not force:
        raise FileExistsError(str(out))
    keep = read_human_edits(out) if out.exists() else {}
    entries = []
    for p_ in workspace.game_projects(info):
        for fd in workspace.file_dirs(p_):
            try:
                proj = load_project(fd)
            except FileNotFoundError:
                continue  # 영역 템플릿이 없는 그룹
            pages = workspace.included_pages(fd)
            if pages:
                entries.append({"project_name": p_.name, "group": group_of(proj), "project": proj, "pages": pages,
                                "old_groups": sorted(workspace.group_names(fd.parent.parent) - {group_of(proj)})})
    path = export_game(info.game, entries, out, keep=keep)
    _retire_legacy(info, [], per_file=True)
    return [path]


def _retire_legacy(info, names: list[str], per_file: bool = False) -> list[Path]:
    """게임 폴더로 옮겨 간 검증·검수 엑셀의 예전 사본(프로젝트 폴더, 파일 폴더)을 휴지통(.trash)으로.
    폴더를 열었을 때 옛 엑셀을 고치는 일이 없게 한다. 열려 있어 못 옮기면 그대로 둔다."""
    from app import workspace

    moved = []
    for p_ in workspace.game_projects(info):
        olds = [p_.dir / n for n in names]
        if per_file:
            olds += [fd / "번역_검수.xlsx" for fd in workspace.file_dirs(p_)]
        for old in olds:
            if old.exists():
                try:
                    moved.append(_to_trash(old, workspace.data_root(p_), f"{p_.name}-{old.parent.name}-{old.name}"))
                except OSError:
                    pass
    return moved


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
        for reg in proj.template.regions_for(pno):
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
                  progress: ProgressFn | None = None, cancel: CancelToken | None = None,
                  files: list | None = None, stale: bool = True) -> ProjectStepResult:
    """7단계를 모든 그룹·파일에. 이미 지운 파일(cleared/)은 건너뛴다(redo=True면 다시).
    stale=True면 지운 뒤 원문·영역·카드별 지우기 방식이 바뀐 파일은 다시 지운다."""
    from app import workspace

    info = workspace.load_project_info(project_dir)
    out = ProjectStepResult("done")
    for fd in scoped_file_dirs(info, group, files):
        key = workspace.file_key(fd)
        pages = workspace.included_pages(fd)
        if ((fd / "cleared" / "cleared.pdf").exists() and not redo and set(pages) <= _cleared_pages(fd)
                and not (stale and stale_erased_pages(fd))):
            out.files.append({"file": key, "state": "skipped", "reason": "이미 지움 (다시 지우려면 ⑦ 글자 지우기의 ‘그래도 다시 지우기’)"})
            continue
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


def erase_sig(project, page: int, sources: dict, modes: dict | None) -> str:
    """지운 결과가 무엇으로 만들어졌나 (원문·그 쪽 영역 설정·카드별 지우기 방식). 바뀌면 다시 지워야 한다."""
    import hashlib

    regs = [{"name": r.name, "kind": r.kind, "rect": r.rect.to_dict(), "style": r.style, "erase": r.erase}
            for r in project.template.regions_for(int(page))]
    blob = json.dumps({"src": sources or {}, "regions": regs, "modes": modes or {}}, ensure_ascii=False,
                      sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def stale_erased_pages(file_dir: Path | str) -> list[int]:
    """지운 뒤 원문(③ 원문 확인·② 아이콘 정의)이나 영역·카드별 지우기 방식이 바뀌어 다시 지워야 하는 쪽.
    예전 버전으로 지운 쪽(기록 없음)은 모르니 넣지 않는다."""
    from app.extract import load_extracted
    from app.workspace import file_settings

    fd = Path(file_dir)
    meta_dir = fd / "cleared" / "meta"
    if not meta_dir.exists():
        return []
    try:
        project = load_project(fd)
        extracted = load_extracted(fd)
    except Exception:  # noqa: BLE001
        return []
    modes = file_settings(fd).get("erase_modes", {})
    out = []
    for f in sorted(meta_dir.glob("page-*.json")):
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sig = m.get("erase_sig")
        pno = int(m.get("source_page") or f.stem.split("-")[-1])
        if not sig:
            continue
        srcs = {k: v.get("source") for k, v in (extracted.get(pno) or {}).items()}
        try:
            now = erase_sig(project, pno, srcs, modes.get(str(pno)))
        except Exception:  # noqa: BLE001 - 그 쪽 영역이 없어짐 등
            continue
        if now != sig:
            out.append(pno)
    return out


def files_needing_erase(project_dir: Path | str, files: list | None = None) -> list[Path]:
    """지워야 하는 파일 (아직 안 지움·처리할 쪽이 늘어남·지운 뒤 원문이나 설정이 바뀜). 뒤에서 지울 차례."""
    from app import workspace

    info = workspace.load_project_info(project_dir)
    out = []
    for fd in scoped_file_dirs(info, None, files):
        pages = workspace.included_pages(fd)
        if not pages:
            continue
        try:
            load_project(fd)
        except FileNotFoundError:
            continue  # 영역이 아직 없는 그룹
        done = (fd / "cleared" / "cleared.pdf").exists() and set(pages) <= _cleared_pages(fd)
        if not done or stale_erased_pages(fd):
            out.append(fd)
    return out


def _cleared_pages(fd: Path) -> set[int]:
    """지운 결과(cleared/run.json)에 든 쪽. 시험 모드로 일부만 지웠다가 장수를 늘리면 다시 지운다."""
    f = fd / "cleared" / "run.json"
    try:
        return set(json.loads(f.read_text(encoding="utf-8")).get("pages", []))
    except (OSError, ValueError):
        return set()


def set_card_limit(project_dir: Path | str, n: int | None) -> int | None:
    """시험 모드: 파일마다 앞 n장까지만 추출하고, 번역·검수 엑셀·지우기·한글 넣기도 그 카드만 한다.
    None·0이면 끈다(전체). 늘리거나 끄고 다시 추출하면 남은 카드부터 이어 읽는다. 지금 값을 돌려준다."""
    from app import workspace

    info = workspace.load_project_info(project_dir)
    workspace.set_card_limit(info, n)
    return workspace.card_limit(info)


def compose_project(project_dir: Path | str, group: str | None = None, *, accept_issues: bool = False,
                    use_draft: bool = False, progress: ProgressFn | None = None,
                    cancel: CancelToken | None = None, files: list | None = None) -> ProjectStepResult:
    """8단계를 모든 그룹·파일에. 파일마다 검수한 번역_검수.xlsx가 있으면 그것, 없으면(또는 use_draft)
    초벌 번역으로 한글을 넣는다. 지우기는 다시 하지 않는다."""
    from app import workspace

    info = workspace.load_project_info(project_dir)
    out = ProjectStepResult("done")
    for fd in scoped_file_dirs(info, group, files):
        key = workspace.file_key(fd)
        if not (fd / "cleared").exists():
            out.files.append({"file": key, "state": "blocked", "reason": "지운 결과 없음 (먼저 ⑦ 글자 지우기)"})
            out.state, out.reason = "blocked", f"{key}: 지운 결과가 없습니다"
            continue
        xlsx = review_xlsx_for(fd)
        if xlsx and not use_draft:
            r = compose(fd, excel=xlsx, accept_issues=accept_issues, progress=progress, cancel=cancel)
            src = "번역_검수.xlsx"
        else:
            r = _compose_with_cards(fd, cards_from_translation(fd), progress, cancel)
            src = "초벌 번역"
        rec = {"file": key, "state": r.state, "from": src, "issues": r.issues, "blocking": r.blocking,
               "summary": {k: len(v) for k, v in r.status_summary.items()},
               "dir": str(r.output_dir) if r.output_dir else None,
               "sheet_images": [str(p) for p in r.sheet_images], "sheet_error": r.sheet_error}
        out.files.append(rec)
        if r.state in ("blocked", "failed"):
            out.state, out.reason = r.state, f"{key}: {(r.blocking or [r.error or ''])[0]}"
        elif r.state == "needs_confirmation" and out.state == "done":
            out.state, out.reason = "needs_confirmation", f"{key}: 엑셀에 확인할 항목이 있습니다"
    return out


def image_sheet_layouts(file_dir: Path | str) -> list[dict]:
    """이미지로 넣은 파일이면 시트마다 카드 자리 [{"file", "frame", "size", "dpi", "boxes"}], 아니면 [].
    예전에 넣어 배치가 남아 있지 않으면 원본 이미지 사본(source/images/)을 같은 가로×세로로 다시 잘라 구한다."""
    from app import images, workspace

    fd = Path(file_dir)
    fi = workspace.file_settings(fd).get("from_images")
    if not fi:
        return []
    if fi.get("sheets"):
        return fi["sheets"]
    out = []
    for name in fi.get("files", []):
        src = fd / "source" / "images" / name
        for frame in range(len(images.load_images(src))):
            out.append(images.cut_sheet(src, fi.get("cols", 1), fi.get("rows", 1), frame=frame).layout())
    return out


def sheet_family(file_dir: Path | str) -> tuple[Path | None, list[Path]]:
    """같은 이미지 시트에서 나온 파일 폴더들: (이미지로 넣은 원래 파일, [다른 그룹으로 옮긴 쪽의 파일들 포함 전부]).
    쪽을 다른 그룹으로 옮기면(workspace.move_pages) 그 그룹에 같은 이름의 파일이 생기고 file.json의
    pages_in_groups로 서로 이어진다. 이미지로 넣지 않은 파일이면 (None, [file_dir])."""
    from app import workspace

    fd = Path(file_dir)
    info = workspace.project_of(fd)
    members = [fd]
    if info is not None:
        todo = [fd]
        while todo:
            d = todo.pop()
            for g in (workspace.file_settings(d).get("pages_in_groups") or {}):
                try:
                    other = workspace.group_dir(info, g) / "files" / d.name
                except Exception:  # noqa: BLE001 - 지운 그룹
                    continue
                if other.exists() and all(other.resolve() != m.resolve() for m in members):
                    members.append(other)
                    todo.append(other)
    owner = next((m for m in members if workspace.file_settings(m).get("from_images")), None)
    return owner, members


def _attach_sheet_images(file_dir: Path, final_dir: Path, res, progress: ProgressFn,
                         pdf_name: str = "translated.pdf", test: bool = False) -> None:
    """배열 이미지로 넣은 파일이면 한글 넣기 결과를 원래 시트 크기·배열의 이미지로도 저장한다 (결과 폴더/images/).
    같은 시트의 카드를 다른 그룹에서도 넣었으면(쪽 옮기기) 그 결과도 함께 붙이고, 그 그룹의 결과 폴더에도 같은
    시트를 쓴다. 시험 실행(test)은 그 실행의 쪽만 붙이고 시험 폴더에만 쓴다."""
    import logging

    from app import images

    try:
        owner, members = sheet_family(file_dir)
        layouts = image_sheet_layouts(owner) if owner else []
        if not layouts:
            return
        progress(ProgressEvent("render", 0, 1, "원래 배열 이미지로 합치는 중"))
        rep = json.loads((final_dir / "report.json").read_text(encoding="utf-8"))
        extra, others = [], []
        if not test:
            for m in members:
                if m.resolve() == Path(file_dir).resolve():
                    continue
                t = m / "translated"
                if (t / "translated.pdf").exists() and (t / "report.json").exists():
                    extra.append((t / "translated.pdf", json.loads((t / "report.json").read_text(encoding="utf-8"))["pages"]))
                    others.append(t)
        outs = images.compose_sheets(layouts, owner / "source" / "images", final_dir / pdf_name, rep["pages"],
                                     final_dir / "images", extra=extra)
        for t in others:  # 다른 그룹의 결과에도 같은 시트를
            (t / "images").mkdir(exist_ok=True)
            for o in outs:
                shutil.copyfile(o, t / "images" / o.name)
        if hasattr(res, "sheet_images"):
            res.sheet_images = outs
    except Exception as e:  # noqa: BLE001 - 한글 넣기 결과(PDF)는 그대로 둔다
        logging.getLogger("cardtranslater.compose").exception("시트 이미지 합치기 실패: %s", file_dir)
        if hasattr(res, "sheet_error"):
            res.sheet_error = f"원래 배열 이미지로 합치지 못했습니다: {e}"


EDITED_DIR = "edited"  # 파일 폴더 안: 사람이 고친 한글 PDF와 그것으로 합친 배열 이미지 (한글 넣기를 다시 해도 남는다)
EDITED_PDF = "edited.pdf"


@dataclass
class EditedImportResult:
    pdf: Path
    sheet_images: list[Path]
    warnings: list[str] = field(default_factory=list)


def can_import_edited(file_dir: Path | str) -> bool:
    """고친 PDF를 넣어 원래 배열 이미지로 합칠 수 있는 파일인가: 배열 이미지로 넣었고 한글 넣기 결과가 있다."""
    fd = Path(file_dir)
    t = fd / "translated"
    if not ((t / "translated.pdf").exists() and (t / "report.json").exists()):
        return False
    try:
        owner, _members = sheet_family(fd)
        return bool(owner and image_sheet_layouts(owner))
    except Exception:  # noqa: BLE001
        return False


def edited_sheet_images(file_dir: Path | str) -> list[Path]:
    """고친 PDF로 합친 배열 이미지 (없으면 [])."""
    d = Path(file_dir) / EDITED_DIR / "images"
    return sorted(p for p in d.glob("*") if p.suffix.lower() in (".png", ".jpg")) if d.is_dir() else []


def import_edited_pdf(file_dir: Path | str, pdf: Path | str, progress: ProgressFn | None = None,
                      cancel: CancelToken | None = None) -> EditedImportResult:
    """한글 넣기 결과 PDF(translated/translated.pdf)를 사람이 고친 것을 받아, 원래 시트 크기·배열의 이미지로 합친다.
    고친 PDF는 파일 폴더의 edited/edited.pdf로 복사하고, 이미지는 edited/images/에 쓴다 (한글 넣기를 다시 해도 남는다).
    쪽 수가 한글 넣기 결과와 다르면 ValueError. 받은 PDF 원본은 바꾸지 않는다."""
    import pymupdf

    from app import images

    progress = progress or _noop
    fd = Path(file_dir)
    pdf = Path(pdf)
    t = fd / "translated"
    if not ((t / "translated.pdf").exists() and (t / "report.json").exists()):
        raise ValueError("이 파일은 아직 한글 넣기 결과가 없습니다. 먼저 ⑧ 한글 넣기를 하세요.")
    owner, members = sheet_family(fd)
    layouts = image_sheet_layouts(owner) if owner else []
    if not layouts:
        raise ValueError("배열 이미지로 넣은 파일이 아니라 원래 배열 이미지로 합칠 수 없습니다.")
    rep = json.loads((t / "report.json").read_text(encoding="utf-8"))
    try:
        new = pymupdf.open(pdf)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"PDF를 열 수 없습니다: {e}") from e
    warnings: list[str] = []
    with new, pymupdf.open(t / "translated.pdf") as old:
        if new.page_count != old.page_count:
            raise ValueError(f"쪽 수가 다릅니다: 고친 PDF {new.page_count}쪽, 한글 넣기 결과 {old.page_count}쪽.\n"
                             "쪽을 더하거나 빼지 말고 내용만 고친 PDF를 넣으세요.")
        odd = []
        for i in range(old.page_count):
            a, b = old[i].rect, new[i].rect
            if abs(a.width / a.height - b.width / b.height) > 0.02 * (a.width / a.height):
                odd.append(rep["pages"][i] if i < len(rep["pages"]) else i + 1)
        if odd:
            warnings.append("쪽 가로세로 비율이 한글 넣기 결과와 다른 쪽이 있어 카드 자리에 맞춰 늘이거나 줄였습니다: "
                            + ", ".join(f"{p}쪽" for p in odd[:10]) + (" …" if len(odd) > 10 else ""))
    if cancel:
        cancel.check()
    progress(ProgressEvent("render", 0, 1, "고친 PDF를 원래 배열 이미지로 합치는 중"))
    out = fd / EDITED_DIR
    out.mkdir(exist_ok=True)
    tmp_pdf = out / (EDITED_PDF + ".tmp")
    shutil.copyfile(pdf, tmp_pdf)
    extra = []
    for m in members:  # 같은 시트의 다른 그룹 카드: 그쪽도 고친 PDF가 있으면 그것을
        if m.resolve() == fd.resolve():
            continue
        mt = m / "translated"
        if (mt / "translated.pdf").exists() and (mt / "report.json").exists():
            mp = m / EDITED_DIR / EDITED_PDF
            extra.append((mp if mp.exists() else mt / "translated.pdf",
                          json.loads((mt / "report.json").read_text(encoding="utf-8"))["pages"]))
    tmp_imgs = out / ".images-tmp"
    if tmp_imgs.exists():
        shutil.rmtree(tmp_imgs)
    try:
        outs = images.compose_sheets(layouts, owner / "source" / "images", tmp_pdf, rep["pages"], tmp_imgs, extra=extra)
    except Exception:
        tmp_pdf.unlink(missing_ok=True)
        shutil.rmtree(tmp_imgs, ignore_errors=True)
        raise
    tmp_pdf.replace(out / EDITED_PDF)
    final_imgs = out / "images"
    if final_imgs.exists():
        shutil.rmtree(final_imgs)
    tmp_imgs.rename(final_imgs)
    progress(ProgressEvent("done", 1, 1, "고친 PDF 합치기 완료"))
    return EditedImportResult(out / EDITED_PDF, [final_imgs / o.name for o in outs], warnings)


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
    res = ComposeResult("done", final_dir, status_summary=rep["status_summary"])
    _attach_sheet_images(fd, final_dir, res, progress or _noop)
    return res


# ================================================================ 화면용 (PySide6 등)
# 화면은 아래 함수와 위의 작업 함수만 부른다. 결과는 경로·상태를 담은 dict/dataclass로 준다.
REGION_KINDS = ("text", "tag_pills")
# 글자 지우기 방식 (그림 카드, app.erase_text). [(키, 화면 문구)]
ERASE_MODES = [("auto", "자동 (배경 보고 고름)"), ("solid", "단색: 주변 색으로 덮기"),
               ("pattern", "무늬: 주변 무늬로 채우기"), ("rect_avg", "그림: 사각형 + 평균색"),
               ("inpaint", "그림: 뒤 그림 유추해 채우기")]
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

    from app.regions import load_template

    info = workspace.load_project_info(project_dir)
    translated = _translated_files(info) if info.status == "translating" else {}
    groups = []
    for g in workspace.list_groups(info):
        files = []
        rpath = g["dir"] / "regions.json"
        tdata = json.loads(rpath.read_text(encoding="utf-8")) if rpath.exists() else {}
        per_card = tdata.get("layout") == "per_card"
        for name in g["files"]:
            fd = g["dir"] / "files" / name
            n = workspace.page_count(fd)
            inc = workspace.included_pages(fd)
            all_inc = workspace.included_pages(fd, limited=False)
            done = set(load_extracted(fd)) & set(inc)
            if per_card:
                tpl = load_template(rpath, file_name=name)
                no_regions = [p for p in inc if not tpl.has_regions(p)]
            else:
                no_regions = [] if rpath.exists() else list(inc)
            files.append({
                "name": name, "key": workspace.file_key(fd), "dir": fd, "pages": n,
                "included": len(inc), "excluded": sorted(set(range(1, n + 1)) - set(all_inc)),
                "exclude_reason": workspace.file_settings(fd).get("exclude_reason", ""),
                "extracted": len(done), "erased": (fd / "cleared" / "cleared.pdf").exists(),
                "composed": (fd / "translated" / "translated.pdf").exists(),
                "review_xlsx": review_xlsx_for(fd),
                "no_regions": no_regions,
            })
        n_regions = (len({r.get("name") for ps in (tdata.get("page_regions") or {}).values()
                          for r in ps.get("regions", [])}) if per_card else g["regions"])
        for f in files:  # 파일마다 번역이 끝났나 (그룹만 번역해도 그 그룹은 한글 넣기를 할 수 있게)
            if info.status == "translated":
                f["translated"] = True
            elif f["key"] in translated:
                f["translated"] = translated[f["key"]]
            else:  # 번역할 칸이 없는 파일(그림만 있는 카드 등): 추출이 끝났으면 번역할 것도 끝
                f["translated"] = info.status == "translating" and f["included"] > 0 and f["extracted"] >= f["included"]
        groups.append({"name": g["name"], "dir": g["dir"], "regions": n_regions, "per_card": per_card,
                       "has_regions": rpath.exists() and (not per_card or any(
                           len(f["no_regions"]) < f["included"] for f in files)),
                       "files": files})
    review = None
    if info.status in ("translating", "translated"):
        try:
            review = review_progress(info.dir)
        except Exception:  # noqa: BLE001 - 검수 진행을 못 세어도 화면은 열린다
            review = None
    return {"name": info.name, "game": info.game, "dir": info.dir, "status": info.status,
            "status_label": STATUS_LABEL.get(info.status, info.status),
            "glossary_version": info.extra.get("glossary_version"), "groups": groups,
            "card_limit": workspace.card_limit(info),
            "review": review,
            "next_step": _next_step(info, groups, review), "next_key": _next_key(info, groups, review)}


_TRANSLATED_CACHE: dict = {}


def _translated_files(info) -> dict[str, bool]:
    """{파일 키: 처리할 칸이 모두 (지금 용어집으로) 번역됐나} (번역 캐시 기준). 그룹만 번역해 프로젝트가
    ‘번역 중’일 때 쓴다. 번역할 칸이 없는 파일은 여기 없다 (현황에서 추출이 끝났으면 번역된 것으로 본다).
    번역 캐시·용어집·추출 파일이 바뀌지 않았으면 다시 세지 않는다 (화면을 새로 고칠 때마다 부른다)."""
    from app import glossary, workspace
    from app.translate import _stale, cache_path, load_cache, lookup

    def mt(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    try:
        fds = workspace.file_dirs(info)
        stamp = (str(info.dir), mt(cache_path(info.dir)), mt(glossary.glossary_path(info)),
                 tuple((str(fd), max([mt(p) for p in (fd / "extract").glob("**/page-*.json")] + [mt(fd / "file.json")]))
                       for fd in fds))
        if _TRANSLATED_CACHE.get("stamp") == stamp:
            return _TRANSLATED_CACHE["value"]
        cache = load_cache(info.dir)
        terms = glossary.load_glossary(info).get("terms", [])
        total: dict[str, int] = {}
        done: dict[str, int] = {}
        for s_ in glossary.collect_sources(info):
            k = s_.key.split("#", 1)[0]
            total[k] = total.get(k, 0) + 1
            hit = lookup(cache, s_.text)
            if hit is not None and not (isinstance(s_.text, str) and _stale(hit, s_.text, terms)):
                done[k] = done.get(k, 0) + 1
        value = {k: done.get(k, 0) >= n for k, n in total.items()}
        _TRANSLATED_CACHE.update(stamp=stamp, value=value)
        return value
    except Exception:  # noqa: BLE001 - 현황 표시는 못 해도 화면은 열린다
        return {}


def _next_key(info, groups: list[dict], review: dict | None = None) -> str:
    """다음에 누를 단계 버튼 키 (화면의 ‘다음 할 일’ 버튼). _next_step 문장과 같은 순서로 판단한다.
    add | regions | extract | finish | glossary | translate | erase | compose | results"""
    if not groups or not any(g["files"] for g in groups):
        return "add"
    if any(not g["has_regions"] for g in groups):
        return "regions"
    if info.status == "extracting":
        if any(f["no_regions"] for g in groups if g.get("per_card") for f in g["files"]):
            return "regions"
        left = [f for g in groups for f in g["files"] if f["extracted"] < f["included"]]
        return "extract" if left else "finish"
    if info.status == "glossary_review":
        return "glossary"
    if info.status in ("glossary_confirmed", "translating"):
        return "translate"
    files = [f for g in groups for f in g["files"]]
    if not all(f["erased"] for f in files):
        return "erase"
    if review and review.get("must_left") and not all(f["composed"] for f in files):
        return "check"  # 꼭 고칠 칸이 남았으면 한글 넣기 전에 번역 검수
    if not all(f["composed"] for f in files):
        return "compose"
    return "results"


def _next_step(info, groups: list[dict], review: dict | None = None) -> str:
    if not groups or not any(g["files"] for g in groups):
        return "파일을 넣으세요"
    no_regions = [g["name"] for g in groups if not g["has_regions"]]
    if no_regions:
        return f"영역을 지정하세요: {', '.join(no_regions)}"
    missing = [f"{f['key']} {len(f['no_regions'])}장" for g in groups if g.get("per_card") for f in g["files"]
               if f["no_regions"]]
    if missing and info.status == "extracting":
        return f"카드마다 영역을 지정하세요 (남은 카드: {', '.join(missing)})"
    left = [f["key"] for g in groups for f in g["files"] if f["extracted"] < f["included"]]
    if info.status == "extracting":
        return f"글자를 추출하세요 ({len(left)}개 파일 남음)" if left else "‘④ 용어집 만들기’로 용어집을 만드세요"
    if info.status == "glossary_review":
        return "용어집 검증 엑셀을 고쳐서 가져오세요"
    if info.status in ("glossary_confirmed", "translating"):
        return "번역을 시작하세요 (끝나면 검수 엑셀까지 만듭니다)"
    files = [f for g in groups for f in g["files"]]
    if not all(f["erased"] for f in files):
        return "글자를 지우세요"
    if review and review.get("must_left") and not all(f["composed"] for f in files):
        return (f"번역을 검수하세요 (꼭 고칠 칸 {review['must_left']}개 · "
                f"확인 {review.get('checked', 0)}/{review.get('total', 0)})")
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
    return {"excluded": sorted(set(exclude)), "included": workspace.included_pages(Path(file_dir), limited=False)}


def move_pages_to_group(file_dir: Path | str, pages: list[int], group: str, *, new_group: bool = False) -> Path:
    """한 파일의 쪽 일부를 다른 그룹(형식)으로 보낸다. 같은 PDF가 그 그룹에도 들어가 그 쪽만 처리되고,
    이 그룹에서는 빠진다. 새 그룹이면 영역 지정이 필요하다."""
    from app import workspace

    return workspace.move_pages_to_group(Path(file_dir), pages, group, new_group=new_group)


def page_count(file_dir: Path | str) -> int:
    from app import workspace

    return workspace.page_count(Path(file_dir))


def shown_pages(file_dir: Path | str) -> list[int]:
    """화면(영역 지정)에서 보일 쪽: 뺄 쪽을 뺀 나머지 (시험 모드 제한은 적용하지 않음). 다 뺐으면 모든 쪽."""
    from app import workspace

    fd = Path(file_dir)
    return workspace.included_pages(fd, limited=False) or list(range(1, workspace.page_count(fd) + 1))


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
    from app import font_files

    cat = font_files.catalog()  # 프로그램의 기본 글꼴 + 따로 설치한 글꼴 묶음
    out = []
    for fam in cat.get("families", []):
        files = {p.name: p for d in font_files.family_dirs(fam["dir"]) for p in d.glob("*.[ot]tf")}
        for f in sorted((p for p in files.values() if "[" not in p.name), key=lambda p: p.name):  # 가변 글꼴 원본 제외
            weight = f.stem.split("-")[-1] if "-" in f.stem else ""
            family = fam["name"] + (f" {weight}" if weight and weight != "Regular" else "")
            out.append({"label": f"{family} — {fam.get('ko_name', '')}", "family": family,
                        "file": f"fonts/{fam['dir']}/{f.name}", "style": fam.get("style", "")})
    return out


def font_pack_status() -> dict:
    """따로 설치하는 글꼴 묶음 상태 {"wanted", "installed", "missing", "ok"} (app.font_files.pack_status)."""
    from app import font_files

    return font_files.pack_status()


# ---------------------------------------------------------------- 한글 넣기 전 글꼴 고르기
FONT_PICK_MODEL = "claude-sonnet-5-5"  # 원문과 견본을 보고 비슷한 글꼴 고르기


def font_plans(project_dir: Path | str, group: str | None = None, files: list | None = None,
               refresh: bool = False, progress: ProgressFn = _noop,
               cancel: CancelToken | None = None) -> list[dict]:
    """한글 넣기 직전 글꼴 창에 보여 줄 계획 (지운 결과가 있는 파일마다 app.font_plan.build).
    추천은 처음 한 번만 만들고 fonts.json에 남긴다 (refresh=True면 다시). Claude를 쓸 수 없으면 글자 모양 값으로만."""
    from app import font_plan, workspace
    from app.extract import make_client
    from app.extract_cc import ClaudeCodeClient

    info = workspace.load_project_info(project_dir)
    dirs = [fd for fd in scoped_file_dirs(info, group, files) if (fd / "cleared" / "cleared.pdf").exists()]
    client = None
    need = refresh or any(not font_plan.load(fd).get("recommended") for fd in dirs)
    if need:
        eng = get_settings().engine
        try:
            if engine_status()[eng]:
                client = ClaudeCodeClient() if eng == "claude-code" else make_client()
        except Exception:  # noqa: BLE001 - 추천만 못 받는다
            client = None
    out = []
    for i, fd in enumerate(dirs):
        if cancel:
            cancel.check()
        key = workspace.file_key(fd)

        def say(done, total, msg, _i=i, _key=key):
            progress(ProgressEvent("fonts", _i, len(dirs), f"{_key}: {msg}"))

        plan = font_plan.build(fd, client, FONT_PICK_MODEL, refresh=refresh, cancel=cancel, say=say)
        plan["file"] = key
        out.append(plan)
    progress(ProgressEvent("done", len(dirs), len(dirs), "글꼴 추천 완료"))
    return out


def save_font_choices(file_dir: Path | str, regions: dict, cards: dict | None = None,
                      sentences: dict | None = None, sizes: dict | None = None,
                      aligns: dict | None = None, slots: dict | None = None) -> Path:
    """sizes {영역: 글자 크기 배율} (1.0 = 원문 크기). aligns {영역: left | center | right} (영역 설정대로면 뺀다).
    글꼴 창에서 고른 것: regions {영역: 글꼴 파일}, cards {쪽: {영역: 글꼴 파일}} (영역 기본과 다를 때만),
    sentences {쪽: {영역: {문장 번호: 글꼴 파일}}} (‘문장마다 글꼴’ 영역)."""
    from app import font_plan

    return font_plan.save(Path(file_dir), regions, cards, sentences, sizes, aligns, slots)


def font_preview(file_dir: Path | str, page: int, region: str, font_file: str | None,
                 scale: float = 1.0, align: str | None = None, sentence: int | None = None,
                 sentence_slots: dict | None = None, base_font: str | None = None) -> dict | None:
    """글꼴 창 미리 보기: 원본·지운 카드의 그 영역 그림(PNG bytes, 같은 배율)과 한글 넣기에 쓸 글자 색·크기.
    {"original_png", "cleared_png", "composed_png" (한글 넣기와 똑같이 그린 그림, 못 그리면 None),
     "zoom" (px/pt), "w_pt", "h_pt", "color", "align", "size_pt" (고른 글꼴·배율), "kind"}. 만들 수 없으면 None.
    align: 글꼴 창에서 고른 정렬 (없으면 영역 설정), sentence: ‘문장마다 글꼴’ 영역에서 그 글꼴을 쓸 문장 번호."""
    from app import font_plan

    src = font_plan.preview_source(Path(file_dir), page, region)
    if src is None:
        return None
    composed = font_plan.preview_render(Path(file_dir), page, region, font_file, scale, align, sentence,
                                        slots=sentence_slots, base_font=base_font)
    return {**src, "size_pt": font_plan.preview_size(src, font_file, scale), "composed_png": composed}


def font_label(file: str) -> str:
    """글꼴 파일의 보이는 이름."""
    from app import font_plan

    return font_plan.label_for(file)


def font_path(file: str) -> Path:
    """글꼴 파일의 실제 경로 (fonts/... 상대 경로는 앱 폴더 기준)."""
    from app import font_plan

    return font_plan.resolve(file)


_INSTALLED_FONTS: list[dict] | None = None


def installed_fonts(refresh: bool = False) -> list[dict]:
    """PC에 설치된 글꼴 중 한글이 있고 PDF에 넣어도 되는 것 [{"file", "label", "family"}] (처음 한 번 몇 초)."""
    global _INSTALLED_FONTS
    from app import font_match

    if _INSTALLED_FONTS is None or refresh:
        _INSTALLED_FONTS = font_match.installed_fonts()
    return _INSTALLED_FONTS


def font_cache_dir() -> Path:
    """글꼴 모음(.ttc)에서 꺼낸 글꼴을 두는 곳."""
    import os

    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "CardTranslater" / "fonts"


def prepare_font(file: str) -> str:
    """고른 글꼴을 한글 넣기에 쓸 수 있는 파일로: 글꼴 모음의 한 글꼴('모음.ttc#2')이면 .ttf로 꺼낸 경로."""
    from app import font_match

    return str(font_match.extract_face(file, font_cache_dir()))


USER_FONT_DIR = "내 글꼴"  # 데이터 폴더 안: 사용자가 글꼴 파일을 넣어 두면 ‘글꼴 추가…’ 목록에 나온다
FONT_EXTS = (".ttf", ".otf", ".ttc", ".otc")


def user_font_dir(create: bool = True) -> Path:
    """사용자 글꼴 폴더 (데이터 폴더\\내 글꼴). create=True면 없을 때 만든다."""
    d = data_dir() / USER_FONT_DIR
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def user_fonts() -> dict:
    """사용자 글꼴 폴더의 글꼴 (하위 폴더 포함, 부를 때마다 새로 읽는다).
    {"dir", "fonts": [{"file", "label", "family", ...}], "skipped": [{"name", "reason"}]}.
    한글이 없거나 PDF에 넣을 수 없는 글꼴은 skipped에 이유와 함께."""
    from app import font_match

    d = user_font_dir(create=False)
    fonts, skipped, seen = [], [], set()
    files = sorted(p for p in d.rglob("*") if p.is_file() and p.suffix.lower() in FONT_EXTS) if d.is_dir() else []
    for p in files:
        fs = font_match.faces(p)
        if not fs:
            skipped.append({"name": p.name, "reason": "읽을 수 없는 글꼴 파일"})
            continue
        for info in fs:
            if not info["hangul"]:
                skipped.append({"name": p.name, "reason": "한글 글자가 없음"})
            elif not info["embeddable"]:
                skipped.append({"name": p.name, "reason": "PDF에 넣는 것이 허락되지 않은 글꼴 (라이선스)"})
            elif info["file"] not in seen:
                seen.add(info["file"])
                fonts.append(info)
    fonts.sort(key=lambda x: x["label"].lower())
    return {"dir": str(d), "fonts": fonts, "skipped": skipped}


def font_file_info(path: Path | str) -> dict:
    """사용자가 고른 글꼴 파일 검사: {"ok", "file", "label", "reason"}.
    글꼴 모음(.ttc)이면 한글이 있고 PDF에 넣어도 되는 첫 글꼴을 고른다."""
    from app import font_match

    if Path(str(path)).suffix.lower() in (".ttc", ".otc") and "#" not in str(path):
        fs = font_match.faces(path)
        good = [f for f in fs if f["hangul"] and f["embeddable"]]
        info = (good or fs or [None])[0]
    else:
        info = font_match.font_info(path)
    if info is None:
        return {"ok": False, "file": str(path), "label": Path(path).name,
                "reason": "읽을 수 없는 글꼴입니다 (.ttf·.otf·.ttc 글꼴 파일을 고르세요)"}
    if not info["hangul"]:
        return {"ok": False, **info, "reason": "한글 글자가 없는 글꼴입니다"}
    if not info["embeddable"]:
        return {"ok": False, **info, "reason": "PDF에 넣는 것이 허락되지 않은 글꼴입니다 (글꼴 라이선스)"}
    return {"ok": True, **info, "reason": ""}


def load_region_template(group_dir: Path | str) -> dict:
    """그룹의 regions.json 원본 dict (없으면 빈 템플릿). 편집기는 모르는 항목을 그대로 두고 저장한다."""
    p = Path(group_dir) / "regions.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"source_language": "ja", "reference_page": 1, "regions": [], "protect_rects": []}


def validate_region_template(data: dict) -> list[str]:
    """영역 템플릿 검사. 카드별 그룹(layout=per_card)은 카드마다 따로 검사한다."""
    if data.get("layout") == "per_card":
        sets = data.get("page_regions") or {}
        if not any(ps.get("regions") for ps in sets.values()):
            return ["영역을 정한 카드가 하나도 없습니다"]
        out = []
        for key, ps in sets.items():
            if ps.get("regions"):
                out += [f"{key}: {m}" for m in validate_region_template(
                    {"regions": ps.get("regions", []), "protect_rects": ps.get("protect_rects", [])})]
        return out
    return _validate_region_set(data)


def _validate_region_set(data: dict) -> list[str]:
    from app import font_files

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
        if f and not font_files.resolve(f).exists():
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
    from app.extract import api_key_source

    cc = bool(find_claude())
    api = bool(api_key_source()[0])
    eng = get_settings().engine
    if eng == "api":
        msg = "API 키로 실행 (사용한 만큼 요금)" if api else "API 키가 없습니다 — 파일 > 로그인 방식에서 키를 넣으세요"
    else:
        msg = ("Claude 구독으로 실행 (Claude Code)" if cc else
               "Claude Code가 없습니다 — 파일 > 로그인 방식에서 ‘Claude Code 설치’를 누르거나 API 키 방식을 쓰세요")
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


DONE_SOUND_MIN_S = 5.0  # 이만큼 넘게 걸린 작업이 끝나면 소리를 낸다


def save_done_sound(enabled: bool, path: str = "") -> Path:
    """작업 끝 소리 설정 (path가 비면 Windows 기본 알림 소리)."""
    from app import settings

    s = settings.load()
    s.done_sound_on, s.done_sound = bool(enabled), str(path or "")
    return settings.save(s)


def done_sound_file() -> Path | None:
    """작업이 끝날 때 낼 소리 파일. 꺼 두었으면 None. 고른 파일이 없으면 Windows 기본 알림 소리."""
    s = get_settings()
    if not s.done_sound_on:
        return None
    if s.done_sound and Path(s.done_sound).exists():
        return Path(s.done_sound)
    return default_done_sound()


def default_done_sound() -> Path | None:
    """Windows 기본 알림 소리 파일 (없으면 None)."""
    import os

    win = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Media"
    for name in ("Windows Notify System Generic.wav", "Windows Notify.wav", "tada.wav"):
        if (win / name).exists():
            return win / name
    return None


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
        for reg in proj.template.regions_for(pno):
            r = (ext.get(pno) or {}).get(reg.name)
            out.append({
                "page": pno, "region": reg.name, "kind": reg.kind,
                "source": None if r is None else r.get("source"),
                "label": "읽지 않음" if r is None else verification_label(r),
                "uncertain": bool(r and r.get("uncertain")),
                "note": (r or {}).get("note", ""),
                "readings": [{"model": MODEL_LABEL.get(x["model"], x["model"]), "source": x["source"]}
                             for x in (r or {}).get("readings") or []],
                "edited": bool(r and (r.get("edited_by") or r.get("checked_by"))),
            })
    return out


def known_icon_names(file_dir: Path | str) -> list[str]:
    """이 파일이 속한 게임의 아이콘 사전에 있는(이름을 정한) 아이콘 이름 (원문 확인 화면, 읽기만 함)."""
    from app import icons, workspace

    info = workspace.project_of(Path(file_dir))
    return sorted(icons.load_dictionary(info)["icons"]) if info else []


def region_crop(file_dir: Path | str, page: int, region: str) -> bytes:
    """원본에서 영역을 잘라 바로 세운 PNG (원문 확인 화면)."""
    import pymupdf

    from app.extract import ocr_excludes, region_crop_png

    fd = Path(file_dir)
    proj = load_project(fd)
    reg = next(r for r in proj.template.regions_for(page) if r.name == region)
    with pymupdf.open(proj.source_pdf) as d:  # 원문 확인도 모델이 본 그림 그대로 (읽기 제외 영역은 가림)
        return region_crop_png(d[page - 1], reg, 1600, exclude=ocr_excludes(proj.template, page))


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
        r["uncertain"] = False
        if old == source:  # 고치지 않고 저장 = 사람이 보고 맞다고 확인함
            r["checked_by"] = "사람"
            if not r.get("edited_by"):
                r["note"] = "사람이 확인 (읽은 그대로)"
        else:
            r["source"] = source
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
    work = info.game_dir / "work" / "icons"
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
                       "where": [{"file_dir": icons._occ_dir(info, o.file), "page": o.page, "region": o.region, "line": o.line,
                                  "png": o.png or None, "occ": o.id, "manual": o.manual,
                                  "label": f"{o.file_key} {o.page}쪽 {o.region} {o.line}줄"}
                                 for o in members],
                       "dict": gi.get("dict", ""),
                       "reason": members[0].reason if members and not gi.get("rep") else "",
                       "first": min((res.occurrences.index(o) for o in members), default=len(res.occurrences))})
    for g in groups:  # 사람이 봐야 하는 것: 한국어 이름이 비어 있음 (창에서 노랑 칸)
        g["issue"] = "" if g["ko"].strip() else "한국어 이름 없음"
    # 이슈가 있는 것을 맨 위로, 그 밖에는 원문에 처음 나온 순서
    groups.sort(key=lambda g: (not g["issue"], g["first"]))
    return {"groups": groups, "known": sorted(icons.load_dictionary(info)["icons"])}


def icon_place(project_dir: Path | str, occ_id: str) -> dict:
    """카드 보기에서 아이콘 자리를 잡는 화면용: {"png": 영역 그림 PNG, "size": (w, h), "box": 지금 자리 | None,
    "guess": 예상 자리 | None, "marks": [{"box", "name" | "group", "occ"}]} (좌표는 그 그림의 px)."""
    import cv2

    from app import icons, workspace

    info = workspace.load_project_info(project_dir)
    p = icons.place_info(info, info.game_dir / "work" / "icons", occ_id)
    img = p.pop("image")
    ok, buf = cv2.imencode(".png", cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
    if not ok:
        raise OSError("영역 그림을 만들지 못함")
    return {**p, "png": buf.tobytes(), "size": (img.shape[1], img.shape[0])}


def set_icon_box(project_dir: Path | str, occ_id: str, box) -> dict:
    """사람이 잡은 자리로 아이콘 그림을 다시 자르고 묶음을 다시 정한다 (검증 엑셀도 새로 만든다).
    {"group", "old", "old_gone", "new_group", "dict"}. 자리가 너무 작으면 ValueError."""
    from app import icons, workspace

    info = workspace.load_project_info(project_dir)
    work = info.game_dir / "work" / "icons"
    r = icons.set_manual_box(info, work, occ_id, tuple(box))
    try:
        icons.export_review(info, icons.load_scan(work), info.game_dir / "아이콘_검증.xlsx")
    except OSError:  # 엑셀이 열려 있으면 못 쓴다: 화면 확정에는 필요 없다
        pass
    return r


def suggest_icon_name(project_dir: Path | str, group: str, *, engine: str | None = None, client=None) -> dict:
    """한 묶음(직접 잡아 새로 생긴 줄)만 이름·뜻·한국어 이름을 제안 받아 저장한다.
    {"suggest", "meaning", "ko"} (못 받으면 빈 값과 "note")."""
    from app import icons, workspace
    from app.extract import make_client
    from app.extract_cc import ClaudeCodeClient

    info = workspace.load_project_info(project_dir)
    work = info.game_dir / "work" / "icons"
    res = icons.load_scan(work)
    gi = res.groups.get(group)
    if not gi or not gi.get("rep"):
        return {"suggest": "", "meaning": "", "ko": "", "note": "그림이 없는 줄"}
    one = icons.ScanResult([o for o in res.occurrences if o.id == gi["rep"]], {group: dict(gi)})
    try:
        if client is None:
            eng = engine or get_settings().engine
            client = ClaudeCodeClient() if eng == "claude-code" else make_client()
        icons.suggest_names(one, client, ICON_SUGGEST_MODEL, work / "suggest")
    except Exception as e:  # noqa: BLE001 - 제안 없이도 직접 적으면 된다
        return {"suggest": "", "meaning": "", "ko": "", "note": f"이름 제안을 받지 못함: {getattr(e, 'reason', e)}"}
    res = icons.load_scan(work)  # 그사이 바뀌었을 수 있어 다시 읽고 이 줄만 고친다
    if group in res.groups:
        for k in ("suggest", "meaning", "ko"):
            if one.groups[group].get(k):
                res.groups[group][k] = one.groups[group][k]
        icons._save_scan(work, res)
    g = one.groups[group]
    return {"suggest": g.get("suggest", ""), "meaning": g.get("meaning", ""), "ko": g.get("ko", "")}


def icon_dictionary(project_dir: Path | str) -> list[dict]:
    """게임 아이콘 사전: [{"name", "image", "ko", "meaning", "scale", "uses"}] (이름 순)."""
    from app import icons, workspace

    info = workspace.load_project_info(project_dir)
    d = icons.load_dictionary(info)["icons"]
    uses = icons.icon_uses(info)
    out = []
    for name, e in sorted(d.items()):
        png = icons.icons_dir(info) / e.get("file", f"{name}.png")
        out.append({"name": name, "image": str(png) if png.exists() else None, "ko": e.get("ko", ""),
                    "meaning": e.get("meaning", ""), "scale": float(e.get("scale", icons.DEFAULT_SCALE)),
                    "uses": uses.get(name, 0)})
    return out


def update_icon_dictionary(project_dir: Path | str, rows: list[dict]) -> dict:
    """사전 고치기. rows: [{"name", "ko", "meaning", "scale", "reset"}].
    reset이면 그 아이콘을 원문에서 [[?]]로 되돌리고 사전에서 뺀다 (다시 ‘아이콘 이름’으로 정한다).
    돌려줌 {"updated": [이름], "reset": {이름: 되돌린 곳 수}}."""
    from app import icons, workspace

    info = workspace.load_project_info(project_dir)
    d = icons.load_dictionary(info)
    out = {"updated": [], "reset": {}}
    for r in rows:
        name = r["name"]
        e = d["icons"].get(name)
        if e is None or r.get("reset"):
            continue
        new = {"ko": r.get("ko", e.get("ko", "")), "meaning": r.get("meaning", e.get("meaning", "")),
               "scale": float(r.get("scale") or e.get("scale", icons.DEFAULT_SCALE))}
        if any(e.get(k) != v for k, v in new.items()):
            e.update(new)
            out["updated"].append(name)
    icons.save_dictionary(info, d)
    for r in rows:
        if r.get("reset"):
            out["reset"][r["name"]] = icons.reset_icon(info, r["name"])
    return out


def confirm_icons(project_dir: Path | str, rows: list[dict]) -> IconImportResult:
    """화면에서 정한 아이콘 이름을 사전에 넣고 원문을 바꾼다.
    rows: [{"group", "name", "meaning", "ko", "scale", "not_icon"}]."""
    from app import icons, workspace

    info = workspace.load_project_info(project_dir)
    work = info.game_dir / "work" / "icons"
    if not (work / "scan.json").exists():
        return IconImportResult("blocked", blocking=["아이콘 찾기 결과가 없습니다"])
    xl = [{"묶음": r["group"], "이름": r.get("name", ""), "뜻(원문)": r.get("meaning", ""),
           "한국어 이름": r.get("ko", ""), "처리": icons.NOT_ICON if r.get("not_icon") else "",
           "글자": r.get("text", ""),
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


def card_erase_info(file_dir: Path | str, page: int) -> dict:
    """카드 한 장의 영역별 지우기: {영역: {"mode": 이 카드에 정한 방식, "default": 영역 설정, "used": 실제로 쓴 방식,
    "needs_choice": 그림 배경이라 확인이 필요한가, "text_mask": 글자 모양만 지우는 영역인가}}."""
    from app.workspace import file_settings

    fd = Path(file_dir)
    meta_f = fd / "cleared" / "meta" / f"page-{int(page):03d}.json"
    meta = json.loads(meta_f.read_text(encoding="utf-8")) if meta_f.exists() else {"regions": []}
    over = file_settings(fd).get("erase_modes", {}).get(str(int(page)), {})
    proj = load_project(fd)
    out = {}
    for reg in proj.template.regions_for(int(page)):
        m = next((r for r in meta["regions"] if r["name"] == reg.name), None)
        tm = ((m or {}).get("erase") or {}).get("text_mask")
        out[reg.name] = {"mode": over.get(reg.name), "default": (reg.erase or {}).get("mode") or "auto",
                         "used": (tm or {}).get("used"), "needs_choice": bool((tm or {}).get("needs_choice")),
                         "text_mask": tm is not None, "kind": reg.kind}
    return out


def set_card_erase_mode(file_dir: Path | str, page: int, region: str, mode: str | None) -> None:
    """이 카드의 이 영역만 다른 방식으로 지운다 (None·빈 값이면 영역 설정을 따름). 반영은 redo_file로."""
    from app import workspace

    fd = Path(file_dir)
    if mode and mode not in dict(ERASE_MODES):
        raise ValueError(f"모르는 지우기 방식: {mode}")
    data = workspace.file_settings(fd)
    modes = data.setdefault("erase_modes", {})
    card = modes.setdefault(str(int(page)), {})
    if mode:
        card[region] = mode
    else:
        card.pop(region, None)
    if not card:
        modes.pop(str(int(page)), None)
    if not modes:
        data.pop("erase_modes", None)
    workspace._write(fd / "file.json", data)


def card_edit_info(file_dir: Path | str, page: int, region: str) -> dict:
    """결과 보기에서 고칠 칸의 지금 값: {"ko": 쓰는 번역(글, 태그 칸은 줄마다), "base": 엑셀·초벌 번역 그대로,
    "edited": 결과 보기에서 고쳤나, "font": 이 카드만 고른 글꼴(없으면 ''), "region_font": 영역 글꼴, "kind",
    "size": 이 카드만 정한 글자 크기 배율(없으면 None), "region_size": 영역 배율, "align": 이 카드만 정한 정렬(없으면 ''),
    "region_align": 영역 정렬(글꼴 창, 없으면 ''), "paragraph_mode": 문단마다 글꼴 영역인가,
    "paragraph_fonts": {문단 번호: 이 카드에서 고른 글꼴}}."""
    from app import card_edits, font_plan

    fd = Path(file_dir)
    font_plan._raw_cache.clear()  # 엑셀·고친 것이 바뀌었을 수 있다
    base_cards = _translation_cards(fd)
    entry = (base_cards.get(int(page)) or {}).get(region) or {}
    base = entry.get("ko") if isinstance(entry, dict) else None
    e = card_edits.edit_for(card_edits.load(fd), page, region, base)
    data = font_plan.load(fd)
    pg = str(int(page))
    region_font = (data.get("regions") or {}).get(region) or ""
    card_font = ((data.get("cards") or {}).get(pg) or {}).get(region) or ""
    reg = next((r for r in load_project(fd).template.regions_for(int(page)) if r.name == region), None)
    card_size = ((data.get("card_sizes") or {}).get(pg) or {}).get(region)
    card_align = ((data.get("card_aligns") or {}).get(pg) or {}).get(region) or ""
    paras = ((data.get("sentences") or {}).get(pg) or {}).get(region) or {}
    mode = font_plan.region_font_modes(fd).get(region) == "sentence" and (reg is None or reg.kind == "text")
    return {"ko": card_edits.norm(e["ko"] if e else base), "base": card_edits.norm(base), "edited": bool(e),
            "font": card_font, "region_font": region_font, "kind": reg.kind if reg else "text",
            "size": float(card_size) if card_size else None,
            "region_size": float((data.get("sizes") or {}).get(region) or 1.0),
            "align": card_align, "region_align": font_plan.align_choices(data).get(region, ""),
            "paragraph_mode": mode, "paragraph_fonts": {int(i): f for i, f in paras.items() if f}}


def split_paragraphs(text: str) -> list[str]:
    """글을 문단으로 (결과 보기·글꼴 창의 문단 목록용, 한글 넣기와 같은 규칙)."""
    from app.typeset_layout import paragraphs

    return paragraphs(text or "")


_TRANSLATION_CARDS: dict = {}
_NOT_FOR_CARDS = ("card_edits", "review_checked", "erase_modes")  # 번역 읽기와 상관없는 file.json 키


def _file_json_sig(fd: Path) -> str:
    """file.json 중 번역 읽기에 쓰는 부분(뺀 쪽 등)의 지문. 검수 표시·고친 번역만 바뀌면 그대로 (엑셀을 다시 읽지 않게)."""
    import hashlib

    try:
        data = json.loads((Path(fd) / "file.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    data = {k: v for k, v in data.items() if k not in _NOT_FOR_CARDS} if isinstance(data, dict) else data
    return hashlib.sha1(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _regions_sig(fd: Path) -> tuple:
    """파일·그룹의 영역 템플릿(regions.json) 시각 (영역이 바뀌면 칸 목록이 바뀐다)."""
    out = []
    for p in (Path(fd) / "regions.json", Path(fd).parent.parent / "regions.json"):
        try:
            out.append(p.stat().st_mtime)
        except OSError:
            out.append(0.0)
    return tuple(out)


def _translation_cards(fd: Path) -> dict:
    """한글 넣기가 쓰는 번역 (고친 것을 덮기 전): 검수 엑셀이 있으면 그것, 없으면 초벌 번역.
    엑셀·번역 캐시·추출·file.json이 바뀌지 않았으면 다시 읽지 않는다 (게임 검수 엑셀은 그림이 있어 느리다)."""
    from app import workspace
    from app.translate import cache_path

    fd = Path(fd)
    xlsx = review_xlsx_for(fd)

    def mt(p) -> float:
        try:
            return Path(p).stat().st_mtime
        except OSError:
            return 0.0

    info = workspace.project_of(fd)
    stamp = (str(xlsx), mt(xlsx) if xlsx else 0.0, mt(cache_path(info.dir)) if info else 0.0, _file_json_sig(fd),
             _regions_sig(fd), max([mt(p) for p in (fd / "extract").glob("**/page-*.json")] + [0.0]))
    hit = _TRANSLATION_CARDS.get(str(fd))
    if hit and hit[0] == stamp:
        return hit[1]
    value = _translation_cards_read(fd, xlsx)
    _TRANSLATION_CARDS[str(fd)] = (stamp, value)
    return value


def _translation_cards_read(fd: Path, xlsx) -> dict:
    if xlsx:
        from app.excel_io import import_translation

        imp = import_translation(load_project(fd), xlsx)
        if not imp.blocking:
            return {int(k): v for k, v in (imp.cards or {}).items()}
    return {int(k): v for k, v in cards_from_translation(fd).items()}


def save_card_edit(file_dir: Path | str, page: int, region: str, *, text: str | None = None,
                   font: str | None = None, set_font: bool = False, size: float | None = None,
                   set_size: bool = False, align: str | None = None, set_align: bool = False,
                   paragraph_fonts: dict | None = None) -> None:
    """결과 보기에서 고친 것을 저장한다 (반영은 recompose_file·redo_file로).
    text: 고친 번역 (None이면 그대로, 엑셀·초벌 번역과 같으면 고친 것을 지움). 태그 칸은 줄마다 태그 하나.
    set_font=True면 font(None·''이면 영역 글꼴을 따름)를 이 카드의 이 영역 글꼴로.
    set_size·set_align: 이 카드만의 글자 크기 배율·정렬 (None·''이면 글꼴 창의 영역 설정).
    paragraph_fonts: {문단 번호: 글꼴 | None} — 이 카드의 문단마다 글꼴 (주면 그것으로 바꾼다)."""
    from app import card_edits, font_plan

    fd = Path(file_dir)
    if text is not None:
        entry = (_translation_cards(fd).get(int(page)) or {}).get(region) or {}
        base = entry.get("ko") if isinstance(entry, dict) else None
        ko = [t.strip() for t in text.splitlines() if t.strip()] if isinstance(base, list) else text.strip()
        card_edits.set_text(fd, page, region, ko, base)
        from app import review_marks

        review_marks.mark(fd, page, region, ko)  # 결과 보기에서 고쳐 저장한 칸 = 사람이 본 칸
        _clear_review_caches()
    if set_font:
        font_plan.set_card_font(fd, page, region, font or None)
    if set_size or set_align or paragraph_fonts is not None:
        font_plan.set_card_style(fd, page, region, size=size, align=align, set_size=set_size,
                                 set_align=set_align, paragraphs=paragraph_fonts)
    font_plan._raw_cache.clear()


def card_preview(file_dir: Path | str, page: int, region: str, text: str | None, font: str | None,
                 size: float | None = None, align: str | None = None,
                 paragraph_fonts: dict | None = None) -> bytes | None:
    """결과 보기에서 저장 전에 그 칸을 한글 넣기와 똑같이 그려 본 PNG (지금 지운 카드 위). 못 그리면 None.
    화면에서 고른 그대로 그린다: font·size가 None이면 영역 글꼴·영역 크기, align ''이면 영역 정렬
    (None이면 저장된 정렬), paragraph_fonts: 이 카드의 문단마다 글꼴 (None이면 저장된 것)."""
    from app import font_plan

    fd = Path(file_dir)
    data = font_plan.load(fd)
    f = font or (data.get("regions") or {}).get(region)  # 없으면 영역 글꼴 (화면이 ‘영역 글꼴대로’를 고른 것)
    entry = (_translation_cards(fd).get(int(page)) or {}).get(region) or {}
    ko = text
    if text is not None and isinstance(entry.get("ko"), list):
        ko = [t.strip() for t in text.splitlines() if t.strip()]
    scale = size if size else font_plan.size_scales(data).get(region, 1.0)  # 없으면 영역 크기
    if align:  # 이 카드만 고른 정렬
        al = align
    elif align == "":  # ‘영역 설정대로’: 글꼴 창의 영역 정렬
        al = font_plan.align_choices(data).get(region)
    else:  # 저장된 것 (카드별 위에 영역)
        al = font_plan.aligns_for_page(data, int(page)).get(region)
    slots = (data.get("sentence_slots") or {}).get(region)
    paras = paragraph_fonts if paragraph_fonts is not None else \
        {int(i): v for i, v in ((((data.get("sentences") or {}).get(str(int(page))) or {}).get(region)) or {}).items()}
    return font_plan.preview_render(fd, page, region, f, scale, al or None, None, ko, slots=slots, para_fonts=paras)


# ---------------------------------------------------------------- 화면용: 번역 검수
REVIEW_STATUS = {  # 엔진 상태 -> (화면 문구, 등급)
    "빈 영역": ("번역 없음", "must"), "원문 글자 남음": ("일본어 남음", "must"), "아이콘 불일치": ("아이콘 불일치", "must"),
    "결론 못 냄": ("원문 결론 못 냄", "must"), "용어 불일치": ("용어 불일치", "look"), "확인 필요": ("원문 불확실", "look"),
}


def _review_check(source, ko, terms: list[dict], ext: dict | None) -> dict:
    """번역 글 검사 (모델을 부르지 않음): {"status": [화면 문구], "level", "missing_terms", "terms"}."""
    from app.card_edits import norm
    from app.extract import verification_label
    from app.translate import check, ko_term_used, terms_in

    src, text = norm(source), norm(ko)
    raw: list[str] = []
    missing: list[str] = []
    if not text:
        raw.append("빈 영역")
    else:
        st, missing = check(src, text, terms)
        raw += st
    if ext is not None and verification_label(ext) == "결론 못 냄":
        raw.append("결론 못 냄")
    elif ext is not None and ext.get("uncertain"):
        raw.append("확인 필요")
    labels = [REVIEW_STATUS[s][0] for s in raw if s in REVIEW_STATUS]
    levels = {REVIEW_STATUS[s][1] for s in raw if s in REVIEW_STATUS}
    level = "must" if "must" in levels else "look" if "look" in levels else "ok"
    term_list = [{"source": t["source"], "ko": t["ko"], "ok": bool(text) and ko_term_used(t["ko"], text)}
                 for t in terms_in(src, terms)]
    return {"status": labels, "level": level, "missing_terms": list(missing), "terms": term_list}


def _review_file_rows(fd: Path) -> list[dict]:
    """파일 하나의 검수 줄 (아래 review_rows의 줄 모양, same_source 빼고)."""
    import hashlib

    from app import card_edits, review_marks, workspace
    from app.excel_io import _project_translation, source_of
    from app.extract import load_extracted, verification_label
    from app.translate import lookup

    proj = load_project(fd)
    extracted = load_extracted(fd)
    cache, terms = _project_translation(fd)
    base_cards = _translation_cards(fd)
    edits = card_edits.load(fd)
    marks = review_marks.load(fd)
    key = workspace.file_key(fd)
    group = workspace.group_names(fd.parent.parent)
    gname = json.loads((fd.parent.parent / "group.json").read_text(encoding="utf-8")).get("name") \
        if (fd.parent.parent / "group.json").exists() else fd.parent.parent.name
    erased = (fd / "cleared" / "cleared.pdf").exists()
    composed = (fd / "translated" / "translated.pdf").exists()
    rows = []
    for pno in workspace.included_pages(fd):
        for reg in proj.template.regions_for(pno):
            if reg.kind not in ("text", "tag_pills"):
                continue
            src, _unsure = source_of(proj, extracted, pno, reg.name)
            if src is None or not card_edits.norm(src):
                continue
            ext = (extracted.get(pno) or {}).get(reg.name)
            hit = lookup(cache, src) if cache else None
            draft = card_edits.norm(hit["ko"]) if hit else ""
            entry = (base_cards.get(pno) or {}).get(reg.name)
            base = entry.get("ko") if isinstance(entry, dict) else None
            e = card_edits.edit_for(edits, pno, reg.name, base)
            raw_e = (edits.get(pno) or {}).get(reg.name)
            now = e["ko"] if e else base
            conflict = ({"app": card_edits.norm(raw_e.get("ko")), "now": card_edits.norm(base)}
                        if raw_e and not e and card_edits.norm(raw_e.get("ko")) != card_edits.norm(base) else None)

            chk = _review_check(src, now, terms, ext)
            checked, checked_stale = review_marks.state(marks, pno, reg.name, now)
            rows.append({
                "id": f"{key}#p{pno}#{reg.name}", "file_dir": fd, "file_key": key, "group": gname,
                "group_names": sorted(group), "page": pno, "region": reg.name, "kind": reg.kind,
                "source": card_edits.norm(src), "source_check": verification_label(ext),
                "draft": draft, "base": card_edits.norm(base), "excel_changed": card_edits.norm(base) != draft,
                "ko": card_edits.norm(now), "edited": bool(e), "edit_conflict": conflict,
                **chk, "source_id": hashlib.sha1(card_edits.norm(src).encode("utf-8")).hexdigest()[:12],
                "checked": checked, "checked_stale": checked_stale, "erased": erased, "composed": composed,
            })
    return rows


def _review_counts(rows: list[dict]) -> dict:
    return {"total": len(rows), "checked": sum(1 for r in rows if r["checked"]),
            "edited": sum(1 for r in rows if r["edited"]),
            "must_left": sum(1 for r in rows if r["level"] == "must" and not r["checked"]),
            "look_left": sum(1 for r in rows if r["level"] == "look" and not r["checked"])}


def _excel_info(fds: list[Path], info) -> dict:
    path = next((p for p in (review_xlsx_for(fd) for fd in fds) if p), None)
    if path is None and info is not None:
        g = info.game_dir / "번역_검수.xlsx"
        path = g if g.exists() else None
    is_open = bool(path) and (Path(path).parent / f"~${Path(path).name}").exists()
    return {"path": str(path) if path else None, "open": is_open}


def review_rows(project_dir: Path | str, files: list | None = None) -> dict:
    """번역 검수 화면의 칸 목록 (범위: files = 파일 폴더 목록, None이면 프로젝트 전체; 처리할 쪽의 글·태그 칸).
    {"rows": [{id, file_dir, file_key, group, page, region, kind, source, source_check, draft, base, excel_changed,
               ko(한글 넣기가 쓸 글), edited, edit_conflict, status[화면 문구], level(must|look|ok), missing_terms,
               terms[{source, ko, ok}], source_id, same_source, checked, checked_stale, erased, composed}],
     "excel": {"path", "open"}, "counts": {total, checked, edited, must_left, look_left}}. 모델은 부르지 않는다."""
    from app import workspace

    info = workspace.load_project_info(project_dir)
    fds = scoped_file_dirs(info, None, files)
    rows: list[dict] = []
    for fd in fds:
        try:
            rows += _review_file_rows(fd)
        except FileNotFoundError:
            continue  # 영역 템플릿이 없는 그룹
    seen: dict[tuple, int] = {}
    for r in rows:
        k = (r["source"], r["ko"])
        seen[k] = seen.get(k, 0) + 1
    for r in rows:
        r["same_source"] = seen[(r["source"], r["ko"])] - 1
    return {"rows": rows, "excel": _excel_info(fds, info), "counts": _review_counts(rows)}


def _review_row(fd: Path, page: int, region: str) -> dict | None:
    return next((r for r in _review_file_rows(Path(fd)) if r["page"] == int(page) and r["region"] == region), None)


def check_review_text(file_dir: Path | str, page: int, region: str, text: str) -> dict:
    """입력 중 검사 (모델을 부르지 않음): {"status", "level", "missing_terms", "terms"}. 태그 칸은 한 줄에 태그 하나."""
    from app.excel_io import _project_translation, source_of
    from app.extract import load_extracted

    fd = Path(file_dir)
    proj = load_project(fd)
    extracted = load_extracted(fd)
    src, _u = source_of(proj, extracted, int(page), region)
    _cache, terms = _project_translation(fd)
    return _review_check(src, text, terms, (extracted.get(int(page)) or {}).get(region))


def save_review(file_dir: Path | str, page: int, region: str, text: str | None, *, checked: bool = True,
                same_source: bool = False, scope_files: list | None = None) -> list[dict]:
    """검수에서 고친 번역 저장(결과 보기와 같은 card_edits)·확인 표시. text가 None이면 확인 표시만.
    same_source=True면 범위(scope_files, None이면 이 파일) 안에서 원문이 같고 지금 번역도 같은 칸에도 같은 글을 넣고
    확인한다 (다른 번역을 쓰던 칸은 그대로). 바뀐 칸들의 새 줄을 돌려준다."""
    from app import card_edits, review_marks, workspace

    fd = Path(file_dir).resolve()
    before: dict[str, list[dict]] = {}

    def rows_of(d: Path) -> list[dict]:  # 파일마다 한 번만 (칸마다 파일 전체 줄을 다시 만들면 느리다)
        if str(d) not in before:
            before[str(d)] = _review_file_rows(d)
        return before[str(d)]
    me = next((r for r in rows_of(fd) if r["page"] == int(page) and r["region"] == region), None)
    if me is None:
        raise KeyError(f"검수할 칸이 없습니다: {page}쪽 {region}")
    targets = [(fd, int(page), region)]
    if same_source and text is not None:
        info = workspace.project_of(fd)
        scope = list(dict.fromkeys(Path(x).resolve() for x in (scope_files or [fd])))  # 같은 파일을 두 번 보지 않게
        for d in scope:
            for r in (rows_of(d) if info is not None else []):
                if (r["source"] == me["source"] and r["ko"] == me["ko"]
                        and not (Path(r["file_dir"]).resolve() == fd and int(r["page"]) == int(page)
                                 and r["region"] == region)):
                    targets.append((Path(r["file_dir"]).resolve(), int(r["page"]), r["region"]))
    for d, p_, reg in targets:
        if text is not None:
            entry = (_translation_cards(d).get(p_) or {}).get(reg) or {}
            base = entry.get("ko") if isinstance(entry, dict) else None
            ko = [t.strip() for t in text.splitlines() if t.strip()] if isinstance(base, list) else text.strip()
            card_edits.set_text(d, p_, reg, ko, base)
            final = ko
        else:
            final = me["ko"] if (d, p_, reg) == targets[0] else None
        if checked and final is not None:
            review_marks.mark(d, p_, reg, final)
    _clear_review_caches()
    after: dict = {}
    for d in dict.fromkeys(str(t[0]) for t in targets):
        for r in _review_file_rows(Path(d)):
            after[(d, int(r["page"]), r["region"])] = r
    return [after[k] for k in ((str(d), p_, reg) for d, p_, reg in targets) if k in after]


def set_review_checked(file_dir: Path | str, page: int, region: str, checked: bool) -> dict:
    """확인 표시/취소. 새 줄을 돌려준다."""
    from app import review_marks

    fd = Path(file_dir)
    r = _review_row(fd, page, region)
    if r is None:
        raise KeyError(f"검수할 칸이 없습니다: {page}쪽 {region}")
    review_marks.mark(fd, page, region, r["ko"] if checked else None)
    _clear_review_caches()
    return _review_row(fd, page, region)


def resolve_review_conflict(file_dir: Path | str, page: int, region: str, keep: str) -> dict:
    """앱에서 고친 뒤 엑셀·재번역으로 번역이 바뀐 칸: keep="app"이면 앱에서 고친 글을 지금 번역 위에 다시 쓰고,
    "now"면 앱에서 고친 것을 지운다 (바뀐 번역을 씀). 새 줄을 돌려준다."""
    from app import card_edits

    fd = Path(file_dir)
    r = _review_row(fd, page, region)
    if r is None or not r["edit_conflict"]:
        return r
    entry = (_translation_cards(fd).get(int(page)) or {}).get(region) or {}
    base = entry.get("ko") if isinstance(entry, dict) else None
    if keep == "app":
        app_ko = r["edit_conflict"]["app"]
        ko = [t for t in app_ko.splitlines() if t.strip()] if isinstance(base, list) else app_ko
        card_edits.set_text(fd, page, region, ko, base)
    else:
        card_edits.set_text(fd, page, region, base, base)  # 같으면 고친 것을 지운다
    _clear_review_caches()
    return _review_row(fd, page, region)


_REVIEW_PROGRESS: dict = {}


def _clear_review_caches() -> None:
    """검수 표시·고친 번역이 바뀜: 진행 수만 다시 센다 (번역 읽기 캐시는 지문으로 알아서 맞춘다)."""
    _REVIEW_PROGRESS.clear()


def review_progress(project_dir: Path | str) -> dict:
    """검수 진행 {"total", "checked", "must_left", "look_left", "by_file": {파일 키: 같은 키들}}.
    프로젝트 화면을 새로 고칠 때마다 부르므로 번역 캐시·용어집·엑셀·file.json·추출 파일 시각으로 캐시한다."""
    from app import glossary, workspace
    from app.translate import cache_path

    info = workspace.load_project_info(project_dir)

    def mt(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    fds = workspace.file_dirs(info)
    xl = _excel_info(fds, info)["path"]
    stamp = (str(info.dir), mt(cache_path(info.dir)), mt(glossary.glossary_path(info)), mt(Path(xl)) if xl else 0.0,
             mt(info.dir / "project.json"),  # 시험 모드(card_limit)가 바뀌면 칸 수가 바뀐다
             tuple((str(fd), mt(fd / "file.json"), _regions_sig(fd),
                    max([mt(p) for p in (fd / "extract").glob("**/page-*.json")] + [0.0])) for fd in fds))
    if _REVIEW_PROGRESS.get("stamp") == stamp:
        return _REVIEW_PROGRESS["value"]
    by_file = {}
    allrows: list[dict] = []
    for fd in fds:
        try:
            rows = _review_file_rows(fd)
        except FileNotFoundError:
            continue
        by_file[workspace.file_key(fd)] = _review_counts(rows)
        allrows += rows
    value = {**_review_counts(allrows), "by_file": by_file}
    _REVIEW_PROGRESS.update(stamp=stamp, value=value)
    return value


def recompose_file(file_dir: Path | str, progress: ProgressFn | None = None,
                   cancel: CancelToken | None = None) -> ComposeResult:
    """파일 하나에 한글만 다시 넣는다 (지우기는 그대로). 고친 번역·글꼴을 반영할 때.
    검수 엑셀이 있으면 그것(확인 항목은 이미 본 것으로 봄), 없으면 초벌 번역."""
    fd = Path(file_dir)
    xlsx = review_xlsx_for(fd)
    if xlsx:
        return compose(fd, excel=xlsx, accept_issues=True, progress=progress, cancel=cancel)
    return _compose_with_cards(fd, cards_from_translation(fd), progress, cancel)


def redo_file(file_dir: Path | str, progress: ProgressFn | None = None,
              cancel: CancelToken | None = None) -> ComposeResult:
    """파일 하나를 다시 지우고(지금 설정·카드별 방식으로) 한글을 다시 넣는다. 지운 쪽은 그대로.
    검수 엑셀이 있으면 그것(확인 항목은 이미 본 것으로 봄), 없으면 초벌 번역으로 넣는다."""
    from app import workspace

    fd = Path(file_dir)
    pages = sorted(_cleared_pages(fd)) or workspace.included_pages(fd)
    r = erase(fd, workspace.pages_spec(pages), progress=progress, cancel=cancel)
    if not r.ok:
        return ComposeResult("failed", error=r.error or "지우기 실패")
    xlsx = review_xlsx_for(fd)
    if xlsx:
        return compose(fd, excel=xlsx, accept_issues=True, progress=progress, cancel=cancel)
    return _compose_with_cards(fd, cards_from_translation(fd), progress, cancel)


@dataclass
class TestRunsResult:
    state: str  # done | blocked
    runs: list = field(default_factory=list)  # [TestRunResult]
    reason: str = ""


def test_run_cards(file_dirs: list, n: int, *, out_root: Path | str | None = None,
                   progress: ProgressFn | None = None, cancel: CancelToken | None = None) -> TestRunsResult:
    """그룹(파일 목록)의 앞 n장만 시험 폴더에서 지우기 -> 한글 넣기. 파일 순서대로, 각 파일의 처리할 쪽에서 채운다.
    프로젝트의 cleared/·translated/는 건드리지 않는다."""
    from app import workspace

    left = int(n)
    runs = []
    plan = []
    for fd in file_dirs:  # 처리할 쪽 (시험 모드 card_limit 안: 추출·번역한 쪽과 같은 범위)
        if left <= 0:
            break
        pages = workspace.included_pages(Path(fd))[:left]
        if pages:
            plan.append((fd, pages))
            left -= len(pages)
    for i, (fd, pages) in enumerate(plan):
        def say(e, _i=i, _fd=fd):  # 파일마다 0%부터 다시 가지 않게 전체 몇 번째 파일인지 붙인다
            if progress:
                progress(ProgressEvent(e.stage, e.done, e.total,
                                       f"{_i + 1}/{len(plan)} {workspace.file_key(Path(_fd))}: {e.message}", e.page))
        r = test_run(fd, workspace.pages_spec(pages), out_root=out_root, progress=say, cancel=cancel)
        if r.state != "done":
            return TestRunsResult("blocked", runs, r.reason)
        runs.append(r)
    if not runs:
        return TestRunsResult("blocked", reason="시험할 카드가 없습니다 (처리할 쪽이 없음)")
    return TestRunsResult("done", runs)


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
    xlsx = review_xlsx_for(fd)
    if xlsx:
        imp = import_translation(project, xlsx)
        cards, source = imp.cards, str(xlsx)
    else:
        cards, source = cards_from_translation(fd), "초벌 번역 (translate/cache.json)"
    _final, run_dir = _output_dirs(project.root, "translated", "compose", True, root)
    run_dir, _code = run_compose(project.root, er.output_dir, pages, run_dir, cards=cards, translation_source=source,
                                 out_name="composed.pdf", progress=progress or _noop, cancel=cancel)
    rep = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    _attach_sheet_images(fd, run_dir, None, progress or _noop, pdf_name="composed.pdf", test=True)  # 시험한 쪽만
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

    from app import font_files, settings

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
        ff = font_files.resolve(fonts[0]["file"] if fonts else "fonts/notosanskr/NotoSansKR-Regular.ttf")
        r = typeset_region(page, (10, 10, 190, 60), "한글 점검", TextStyle(font_file=str(ff), size_pt=10))
        finalize(doc)
        data = doc.tobytes()
        text = pymupdf.open(stream=data, filetype="pdf")[0].get_text()
        out["typeset"] = bool(r.drawn and "한글점검" in "".join(text.split()))
    except Exception as e:  # noqa: BLE001
        out["typeset"] = f"실패: {e}"
    out["engine"] = engine_status()
    out["release_notes"] = bool(release_notes())
    out["license"] = "BEER-WARE" in license_text() and "AGPL" in license_text()
    out["font_pack"] = font_files.pack_status()  # 글꼴 묶음은 따로 설치하므로 점검 실패로 보지 않는다
    out["ok"] = bool(out["fonts"]) and out["typeset"] is True and out["release_notes"] and out["license"] and not any(
        str(v).startswith("없음") for v in libs.values())
    return out


def license_text() -> str:
    """도움말 > 라이선스: 이 프로그램의 라이선스(Beerware)와 제3자 구성 요소 고지 (마크다운). 없으면 ''."""
    from app import release_notes as rn

    return rn.license_text()


def release_notes() -> str:
    """사용자용 릴리스 노트 글(버전별 새 기능·고친 점, 최신이 위). 없거나 읽지 못하면 ''."""
    from app import release_notes as rn

    return rn.load()


# ---------------------------------------------------------------- 화면용: 지우기(휴지통) — 프로젝트·그룹·파일
def _to_trash(path: Path, root: Path, label: str) -> Path:
    """완전히 지우지 않고 데이터 폴더의 .trash/<시각>-<이름>으로 옮긴다 (되살릴 수 있게)."""
    path = Path(path).resolve()
    trash = Path(root).resolve() / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    dest = trash / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}"
    n = 1
    while dest.exists():
        n += 1
        dest = trash / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}-{n}"
    shutil.move(str(path), str(dest))
    return dest


def delete_project(project_dir: Path | str) -> Path:
    """프로젝트 폴더를 휴지통(.trash)으로. 게임 용어집·아이콘 사전(games/)은 그대로 둔다."""
    from app import workspace

    info = workspace.load_project_info(project_dir)
    return _to_trash(info.dir, info.dir.parent.parent, f"project-{info.dir.name}")


RESET_WHAT = {
    "all": ("extract", "cleared", "translated"),  # 글자 추출부터 다시
    "output": ("cleared", "translated"),  # 지운 카드·한글 카드만 다시
}


def reset_files(project_dir: Path | str, files: list | None = None, what: str = "output") -> dict:
    """초기화: 고른 파일(없으면 전체)의 작업 결과를 휴지통(.trash)으로 옮긴다. 파일·영역·뺄 쪽 설정은 그대로.
    (삭제와 다름: 삭제는 그룹·파일 자체를 프로젝트에서 뺀다.)
    what="all"이면 추출 결과까지 -> 프로젝트 상태를 ‘추출 중’으로 되돌린다. 돌려준다 {"moved": [...], "files": n}."""
    from app import workspace

    if what not in RESET_WHAT:
        raise ValueError(f"모르는 초기화: {what}")
    info = workspace.load_project_info(project_dir)
    root = workspace.data_root(info)
    moved = []
    fds = scoped_file_dirs(info, None, files)
    for fd in fds:
        for sub in RESET_WHAT[what]:
            d = fd / sub
            if d.exists():
                moved.append(str(_to_trash(d, root, f"{info.name}-{fd.parent.parent.name}-{fd.name}-{sub}")))
    if what == "all" and fds and info.status != "extracting":
        workspace.set_status(info, "extracting")
    return {"moved": moved, "files": len(fds)}


def rename_group(project_dir: Path | str, old: str, new: str) -> Path:
    """그룹 이름을 바꾼다 (새 그룹 폴더). 검수 엑셀은 다시 만들기 전까지 예전 이름으로도 찾는다."""
    from app import workspace

    return workspace.rename_group(workspace.load_project_info(project_dir), old, new)


def delete_group(project_dir: Path | str, group: str) -> Path:
    """그룹(영역 템플릿과 그 안의 파일 결과)을 휴지통으로."""
    from app import workspace

    info = workspace.load_project_info(project_dir)
    gdir = workspace.group_dir(info, group)
    if not (gdir / "group.json").exists():
        raise KeyError(f"그룹이 없습니다: {group}")
    return _to_trash(gdir, info.dir.parent.parent, f"group-{info.dir.name}-{gdir.name}")


def delete_file(file_dir: Path | str) -> Path:
    """그룹에서 파일 하나(원본 사본·추출·지운 카드·한글 카드)를 휴지통으로. 원래 PDF는 건드리지 않는다."""
    from app import workspace

    fd = Path(file_dir).resolve()
    info = workspace.project_of(fd)
    if info is None or fd.parent.name != "files":
        raise ValueError(f"그룹 안의 파일 폴더가 아닙니다: {fd}")
    return _to_trash(fd, info.dir.parent.parent, f"file-{info.dir.name}-{fd.parent.parent.name}-{fd.name}")


# ---------------------------------------------------------------- 화면용: 로그인 (Claude 구독 / API 키)
def api_key_info() -> dict:
    """{"has_key", "source", "masked"}: 키 값 자체는 돌려주지 않는다."""
    from app.credentials import masked
    from app.extract import api_key_source

    k, src = api_key_source()
    return {"has_key": bool(k), "source": src, "masked": masked(k)}


def set_api_key(key: str, *, validate: bool = True, client_factory=None) -> tuple[bool, str]:
    """API 키를 확인한 뒤 암호화해 저장한다. (성공, 메시지). validate=False면 확인 없이 저장."""
    from app.credentials import save_api_key
    from app.extract import make_client

    key = (key or "").strip()
    if not key.startswith("sk-"):
        return False, "API 키 형식이 아닙니다 (sk-ant-… 로 시작)"
    if validate:
        try:
            client = (client_factory or make_client)(key)
            client.models.list(limit=1)  # 토큰을 쓰지 않는 확인
        except Exception as e:  # noqa: BLE001
            msg = str(getattr(e, "message", e))
            if "401" in msg or "authentication" in msg.lower() or "invalid" in msg.lower():
                return False, "키가 맞지 않습니다 (인증 실패)"
            return False, f"키를 확인하지 못했습니다: {msg[:200]}"
    save_api_key(key)
    return True, "API 키를 확인하고 이 컴퓨터에 암호화해 저장했습니다"


def clear_api_key() -> None:
    from app.credentials import delete_api_key

    delete_api_key()


def claude_login_status(check: bool = False, runner=None) -> dict:
    """Claude Code(구독) 상태: {"installed", "logged_in": True/False/None, "message"}.
    check=True면 아주 짧은 요청(Haiku)을 한 번 보내 로그인을 확인한다 (사용량이 조금 든다)."""
    from app.extract import Paused
    from app.extract_cc import CardError, ClaudeCodeClient, NotAvailable

    exe = find_claude()
    if not exe and runner is None:
        return {"installed": False, "logged_in": False,
                "message": "Claude Code가 설치되어 있지 않습니다 (claude 명령 없음)"}
    if not check:
        return {"installed": True, "logged_in": None, "message": "Claude Code를 찾았습니다 (로그인 확인 전)"}
    try:
        client = ClaudeCodeClient(exe=exe or "claude", runner=runner)
        client.ask_json("Reply with ok=true.", {"type": "object", "properties": {"ok": {"type": "boolean"}},
                                                "required": ["ok"], "additionalProperties": False},
                        "claude-haiku-5-5", "You are a health check.", data_dir() / "work" / "login-check")
    except NotAvailable as e:
        if "Git for Windows" in str(e):
            return {"installed": True, "logged_in": False, "message": str(e)[:300]}
        return {"installed": True, "logged_in": False,
                "message": f"로그인되어 있지 않습니다 — ‘로그인 창 열기’에서 로그인하세요 ({str(e)[:160]})"}
    except Paused as e:
        return {"installed": True, "logged_in": True, "message": f"로그인됨 (지금은 사용 한도: {e.reason[:120]})"}
    except CardError as e:
        return {"installed": True, "logged_in": None, "message": f"확인하지 못했습니다: {str(e)[:200]}"}
    return {"installed": True, "logged_in": True, "message": "Claude에 로그인되어 있습니다"}


def launch_claude_login() -> None:
    """새 터미널 창에서 claude를 띄운다 (사용자가 /login 으로 로그인)."""
    import subprocess
    import sys

    exe = find_claude()
    if not exe:
        raise OSError("Claude Code가 설치되어 있지 않습니다 — ‘Claude Code 설치’를 누르세요")
    if sys.platform == "win32":
        guide = ("echo ===== Claude 로그인 ===== & "
                 "echo  처음이면 안내가 차례로 나옵니다: 글자 색은 아무거나 Enter, "
                 "로그인 방법은 Claude account with subscription 을 골라 Enter, 브라우저에서 승인. & "
                 "echo  코드를 붙여 넣으라고 하면 브라우저의 코드를 복사해 마우스 오른쪽 클릭 후 Enter. & "
                 "echo  안내 없이 바로 ^> 입력 칸이 나오면 /login 을 입력하고 Enter. & "
                 "echo  로그인이 끝나면 이 창을 닫고 CardTranslater에서 [로그인 확인]을 누르세요. & echo.")
        subprocess.Popen(["cmd", "/c", "start", "Claude 로그인", "cmd", "/k", f'{guide} & "{exe}"'])
    else:
        subprocess.Popen(["x-terminal-emulator", "-e", exe])


def install_claude_code(progress: ProgressFn = _noop, cancel: CancelToken | None = None) -> str:
    """Claude Code가 없으면 Node.js(없을 때만, 사용자 폴더)와 Claude Code(npm)를 설치한다. claude 경로를 돌려준다.
    실패하면 claude_setup.SetupError (메시지는 그대로 보여 줘도 된다)."""
    from app import claude_setup

    def say(stage, done, total, message):
        progress(ProgressEvent(stage, done, total, message))

    return claude_setup.install_claude_code(say, cancel)


def start_logging() -> Path:
    """동작 로그를 열고 시작 정보(버전·Windows·데이터 폴더·엔진·Claude Code 위치)를 남긴다. 로그 파일 경로."""
    from app import applog

    path = applog.setup()
    applog.log_startup({"data_dir": str(data_dir()), "engine": engine_status(), "claude": find_claude()})
    return path


def log_folder() -> Path:
    """동작 로그 폴더 (테스트하는 사람이 보내 줄 파일)."""
    from app import applog

    return Path(applog._state["file"]).parent if applog._state.get("file") else applog.log_dir()


def export_logs(dest: Path) -> Path:
    """로그 파일과 환경 정보(버전·Windows·엔진·Claude Code 위치)를 zip 하나로 묶는다. API 키는 가린다."""
    from app import applog

    extra = {"data_dir": str(data_dir()), "engine": engine_status(), "claude": find_claude()}
    return applog.export(Path(dest), extra)


def set_engine(engine: str) -> None:
    """로그인 방식(엔진)만 바꾼다: claude-code(Claude 구독) | api(API 키)."""
    from app import settings

    s = settings.load()
    s.engine = engine
    settings.save(s)


# ---------------------------------------------------------------- 이미지(JPG·PNG) 카드 넣기 (F27)
@dataclass
class ImageAddResult:
    group: str
    file_dir: Path | None
    pages: int = 0
    warnings: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    has_regions: bool = False


def image_preview(image: Path | str, cols: int = 1, rows: int = 1) -> tuple[bytes, dict]:
    """자를 자리를 표시한 미리 보기 PNG와 {"cards", "gutters_found", "warnings", "size"}."""
    from app import images

    png, cut = images.preview_png(image, cols, rows)
    return png, {"cards": len(cut.boxes), "gutters_found": cut.gutters_found, "warnings": cut.warnings,
                 "size": cut.size}


def add_images(project_dir: Path | str, files: list, group: str, *, cols: int = 1, rows: int = 1,
               name: str | None = None, new_group: bool = False,
               regions_from: Path | str | None = None) -> ImageAddResult:
    """이미지 여러 장을 카드 한 장이 한 쪽인 PDF 하나로 바꿔 그룹에 넣는다 (파일 이름 name, 없으면 첫 이미지 이름).
    한 이미지에 카드가 가로 cols × 세로 rows로 놓여 있으면 잘라서 넣는다. 원본 이미지는 source/images/에 사본을 둔다."""
    import tempfile

    from app import images, workspace

    info = workspace.load_project_info(project_dir)
    imgs = [Path(f) for f in files if images.is_image(f)]
    skipped = [f"{Path(f).name}: 이미지가 아님" for f in files if not images.is_image(f)]
    if not imgs:
        raise ValueError("넣을 이미지가 없습니다 (JPG·PNG·WEBP·BMP·TIFF)")
    stem = (name or imgs[0].stem).strip()
    with tempfile.TemporaryDirectory() as td:
        pdf = Path(td) / f"{stem}.pdf"
        res = images.images_to_pdf(imgs, pdf, cols, rows)
        r = workspace.add_files(info, [pdf], group, new_group=new_group, regions_from=regions_from)
    skipped += r["skipped"]
    if not r["added"]:
        return ImageAddResult(group, None, 0, res.warnings, skipped, r["has_regions"])
    fd = Path(r["added"][0])
    keep = fd / "source" / "images"
    keep.mkdir(parents=True, exist_ok=True)
    for f in imgs:  # 원본 이미지 사본 (나중에 다시 자를 때·확인용)
        shutil.copyfile(f, keep / f.name)
    settings = workspace.file_settings(fd)
    settings["from_images"] = {"files": [f.name for f in imgs], "cols": cols, "rows": rows,
                               "sheets": [c.layout() for c in res.sheets]}  # 결과를 원래 배열로 다시 합칠 때
    workspace._write(fd / "file.json", settings)
    return ImageAddResult(group, fd, res.pages, res.warnings, skipped, r["has_regions"])
