"""⑤ 번역, 검수 엑셀, 프로젝트 단위 ⑦ 지우기·⑧ 합성, 이미지 시트, 고친 PDF 가져오기."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from app import workspace
from app.project import load_project
from app.service import _base
from app.service import _erase_compose
from app.service import _extract
from app.service import _manage
from app.service import _review


# ---------------------------------------------------------------- 5단계: 초벌 번역 (확정 용어집 기준)
TRANSLATE_MODELS = {"sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}


def translate_project(project_dir: Path | str, *, model: str = "sonnet", engine: str = "claude-code",
                      client=None, progress: _base.ProgressFn | None = None, cancel: _base.CancelToken | None = None,
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
        keys = {workspace.file_key(fd) for fd in _extract.scoped_file_dirs(info, None, files)}
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
                      work_dir=info.dir / "work" / "translate", progress=progress or _base._noop, cancel=cancel,
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
                    moved.append(_manage._to_trash(old, workspace.data_root(p_), f"{p_.name}-{old.parent.name}-{old.name}"))
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
                  progress: _base.ProgressFn | None = None, cancel: _base.CancelToken | None = None,
                  files: list | None = None, stale: bool = True, workers: int | None = None) -> ProjectStepResult:
    """7단계를 모든 그룹·파일에. 이미 지운 파일(cleared/)은 건너뛴다(redo=True면 다시).
    stale=True면 지운 뒤 원문·영역·카드별 지우기 방식이 바뀐 파일은 다시 지운다.
    workers: 파일마다 쪽을 나눠 동시에 돌릴 프로세스 수 (None이면 자동, 뒤에서 지우기는 1)."""
    from app import workspace

    info = workspace.load_project_info(project_dir)
    out = ProjectStepResult("done")
    for fd in _extract.scoped_file_dirs(info, group, files):
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
            r = _erase_compose.erase(fd, workspace.pages_spec(pages), progress=progress, cancel=cancel, workers=workers)
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


def erase_sig(project, page: int, sources: dict, modes: dict | None, kept=()) -> str:
    """지운 결과가 무엇으로 만들어졌나 (원문·그 쪽 영역 설정·카드별 지우기 방식·‘번역 안 함’ 칸). 바뀌면 다시 지워야 한다."""
    import hashlib

    regs = [{"name": r.name, "kind": r.kind, "rect": r.rect.to_dict(), "style": r.style, "erase": r.erase}
            for r in project.template.regions_for(int(page))]
    # ‘번역 안 함’ 칸은 원문 대신 표시를 넣는다 (1.15.0과 같은 서명이 되게: 이미 지운 쪽을 괜히 다시 지우지 않는다)
    src_sig = {k: (dict(KEEP_ORIGINAL) if k in kept else v) for k, v in (sources or {}).items()}
    d = {"src": src_sig, "regions": regs, "modes": modes or {}}
    skip = icon_only_regions(sources, kept)
    if skip:  # 아이콘만 있는 칸을 건너뛰게 된 뒤로는 예전에 지운 쪽도 다시 지운다 (없는 쪽은 서명이 그대로)
        d["icon_only"] = skip
    blob = json.dumps(d, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


KEEP_ORIGINAL = {"keep_original": True}  # 서명(erase_sig)에서만 쓰는 표시: 사람이 ‘전체가 아이콘 — 번역 안 함’으로 정한 칸


def erase_sources(regions: dict | None) -> tuple[dict, set]:
    """쪽의 추출 결과 -> 지우기에 넘길 ({칸: 원문 글(또는 None)}, ‘번역 안 함’으로 정한 칸 이름들).
    원문 사전에는 늘 글만 든다. ‘번역 안 함’ 칸은 원본 그대로 두고, 표시를 바꾸면 서명도 바뀌어 다시 지운다."""
    regions = regions or {}
    return ({k: v.get("source") for k, v in regions.items()},
            {k for k, v in regions.items() if v.get("keep_original")})


def icon_only_regions(sources: dict | None, kept=()) -> list[str]:
    """원문이 아이콘(과 숫자·기호)만으로 된 칸 이름 (예: ‘[[sword]][[sword]]’, ‘2[[coin]] [[book]]’),
    그리고 사람이 ‘전체가 아이콘 — 번역 안 함’으로 정한 칸 (kept).
    번역해도 같으므로 글자 지우기·한글 넣기를 하지 않고 원본 그대로 둔다. 아이콘이 하나도 없는 칸(빈 칸·숫자만)은
    넣지 않는다 (그런 칸은 예전처럼 처리)."""
    from app.erase_text import is_icon_line
    from app.icons import TOKEN_RE

    out = set(kept)
    for name, src in (sources or {}).items():
        if isinstance(src, str) and TOKEN_RE.search(src) and is_icon_line(src):
            out.add(name)
    return sorted(out)


def set_keep_original(file_dir: Path | str, page: int, region: str, keep: bool = True) -> None:
    """③ 원문 확인 ‘전체가 아이콘 — 번역 안 함’: 그 칸은 번역·용어집에서 빼고, 글자 지우기·한글 넣기도 하지 않고
    원본 그대로 둔다 (원문은 그대로 남긴다). keep=False면 되돌린다. 사람이 확인한 칸이 된다."""
    from app.extract import page_file

    fd = Path(file_dir)
    files = [p for p in (page_file(fd / "extract", page), fd / "extract" / f"page-{page:03d}.json") if p.exists()]
    if not files:
        raise FileNotFoundError(f"{page}쪽은 아직 읽지 않았습니다")
    if keep:  # 아직 읽지 않은 칸은 정할 수 없다 (읽기 전에 지우기·한글 넣기에서 빠져 버린다)
        recs = [json.loads(f.read_text(encoding="utf-8")) for f in files]
        if not any(((rec.get("regions") or {}).get(region) or {}).get("source") is not None for rec in recs):
            raise ValueError(f"{page}쪽 {region} 칸은 아직 읽지 않았습니다")
    for f in files:
        rec = json.loads(f.read_text(encoding="utf-8"))
        r = rec.setdefault("regions", {}).setdefault(region, {"source": None, "uncertain": False, "note": ""})
        if keep:
            r["keep_original"] = True
            r.pop("icon_changed", None)  # 원본 그대로 둘 칸: ② 아이콘 정의 뒤 확인·다시 읽기도 필요 없다
            r.pop("icon_reread", None)
            r["uncertain"] = False
            r["checked_by"] = "사람"
        else:
            r.pop("keep_original", None)
        workspace._write(f, rec)  # 임시 파일로 쓰고 바꿔 넣기 (뒤에서 지우는 스레드가 읽는 중이면 다시 시도)
    _review._clear_review_caches()  # 번역 검수 줄(이 칸이 빠지거나 다시 나옴)·진행 수를 새로 센다


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
        srcs, kept = erase_sources(extracted.get(pno))
        try:
            now = erase_sig(project, pno, srcs, modes.get(str(pno)), kept)
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
    for fd in _extract.scoped_file_dirs(info, None, files):
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
                    use_draft: bool = False, progress: _base.ProgressFn | None = None,
                    cancel: _base.CancelToken | None = None, files: list | None = None) -> ProjectStepResult:
    """8단계를 모든 그룹·파일에. 파일마다 검수한 번역_검수.xlsx가 있으면 그것, 없으면(또는 use_draft)
    초벌 번역으로 한글을 넣는다. 지우기는 다시 하지 않는다."""
    from app import workspace

    info = workspace.load_project_info(project_dir)
    out = ProjectStepResult("done")
    for fd in _extract.scoped_file_dirs(info, group, files):
        key = workspace.file_key(fd)
        if not (fd / "cleared").exists():
            out.files.append({"file": key, "state": "blocked", "reason": "지운 결과 없음 (먼저 ⑦ 글자 지우기)"})
            out.state, out.reason = "blocked", f"{key}: 지운 결과가 없습니다"
            continue
        xlsx = review_xlsx_for(fd)
        if xlsx and not use_draft:
            r = _erase_compose.compose(fd, excel=xlsx, accept_issues=accept_issues, progress=progress, cancel=cancel)
            src = "번역_검수.xlsx"
        else:
            r = _compose_with_cards(fd, cards_from_translation(fd), progress, cancel)
            src = "초벌 번역"
        rec = {"file": key, "state": r.state, "from": src, "issues": r.issues, "blocking": r.blocking,
               "summary": {k: len(v) for k, v in r.status_summary.items()},
               "dir": str(r.output_dir) if r.output_dir else None,
               "sheet_images": [str(p) for p in r.sheet_images], "sheet_error": r.sheet_error,
               "reused": len(r.reused_pages), "composed": len(r.composed_pages)}
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


def _attach_sheet_images(file_dir: Path, final_dir: Path, res, progress: _base.ProgressFn,
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
        progress(_base.ProgressEvent("render", 0, 1, "원래 배열 이미지로 합치는 중"))
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


def import_edited_pdf(file_dir: Path | str, pdf: Path | str, progress: _base.ProgressFn | None = None,
                      cancel: _base.CancelToken | None = None) -> EditedImportResult:
    """한글 넣기 결과 PDF(translated/translated.pdf)를 사람이 고친 것을 받아, 원래 시트 크기·배열의 이미지로 합친다.
    고친 PDF는 파일 폴더의 edited/edited.pdf로 복사하고, 이미지는 edited/images/에 쓴다 (한글 넣기를 다시 해도 남는다).
    쪽 수가 한글 넣기 결과와 다르면 ValueError. 받은 PDF 원본은 바꾸지 않는다."""
    import pymupdf

    from app import images

    progress = progress or _base._noop
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
    progress(_base.ProgressEvent("render", 0, 1, "고친 PDF를 원래 배열 이미지로 합치는 중"))
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
    progress(_base.ProgressEvent("done", 1, 1, "고친 PDF 합치기 완료"))
    return EditedImportResult(out / EDITED_PDF, [final_imgs / o.name for o in outs], warnings)


def _compose_with_cards(fd: Path, cards: dict, progress, cancel) -> _erase_compose.ComposeResult:
    """번역 카드(dict)로 바로 한글 넣기 (엑셀 없이)."""
    from app.typeset_compose import run_compose

    project = load_project(fd)
    final_dir, run_dir = _erase_compose._output_dirs(project.root, "translated", "compose", False, Path("test_runs"))
    try:
        run_dir, code = run_compose(project.root, fd / "cleared", None, run_dir, cards=cards,
                                    translation_source="초벌 번역 (translate/cache.json)", out_name="translated.pdf",
                                    progress=progress or _base._noop, cancel=cancel,
                                    reuse_dir=_erase_compose._reuse_dir(final_dir, fd / "cleared"))
        rep = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
        if code:
            return _erase_compose.ComposeResult("failed", run_dir, status_summary=rep["status_summary"],
                                 error="원본 또는 지운 PDF 해시가 바뀌었습니다")
    except BaseException:
        _erase_compose._discard(run_dir, final_dir)
        raise
    _erase_compose._commit(run_dir, final_dir)
    res = _erase_compose.ComposeResult("done", final_dir, status_summary=rep["status_summary"],
                        reused_pages=rep.get("reused_pages", []), composed_pages=rep.get("composed_pages", []))
    _attach_sheet_images(fd, final_dir, res, progress or _base._noop)
    return res
