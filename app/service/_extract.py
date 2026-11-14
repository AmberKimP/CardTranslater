"""① 글자 추출, 게임·프로젝트 만들기, 파일 넣기, ② 아이콘 찾기·가져오기."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.project import load_project, parse_page_spec
from app.service import _base
from app.service import _erase_compose
from app.service import _translate_erase


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
    lookalike_cells: int = 0  # 닮은 한자로 잘못 읽었을 법해 '확인 필요'로 바꾼 칸


EXTRACT_MODES = {  # 모드 -> (1차 모델, 다시 확인 모델 또는 None)
    "double-check": ("sonnet", "opus"),  # 기본: Sonnet으로 읽고 불확실한 칸만 Opus로 다시
    "opus": ("opus", None),  # Opus만
}


def extract(project_dir: Path | str, pages: str | None = None, *, mode: str = "double-check",
            model: str | None = None, engine: str = "claude-code", batch: bool = False,
            budget_usd: float | None = None, retry_failed: bool = False, test: bool = False,
            out_root: Path | str = "test_runs", client=None,
            progress: _base.ProgressFn | None = None, cancel: _base.CancelToken | None = None) -> ExtractResult:
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
        page_list = parse_page_spec(pages, d.page_count) if pages else _erase_compose._included(project.root, d.page_count)
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
                    model=model_id, out_dir=out_dir, progress=progress or _base._noop, cancel=cancel)
    res = ExtractResult(r.state, r.engine, r.equiv_usd, r.done_pages, r.remaining_pages, r.failed, r.spent_usd,
                        r.reason, r.retry_after, r.batch_id, r.model, r.out_dir, mode if not model else "single")
    if r.state == "done":  # 다른 카드에서 자주 나온 낱말과 닮은 한자 한 글자만 다른 칸: 확인 필요로 (두 번째 확인이 다시 읽는다)
        from app.extract import flag_lookalikes

        res.lookalike_cells = flag_lookalikes(r.out_dir, page_list)
    if check_id and r.state == "done":
        # 두 번째 확인: 1차가 끝난 카드의 '확인 필요' 칸만. 끊기면 다시 불렀을 때 남은 칸부터.
        rc = run_recheck(project, page_list, client, base=r.out_dir, check_model=check_id,
                         progress=progress or _base._noop, cancel=cancel)
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


def back_to_extracting(info) -> bool:
    """프로젝트를 ‘추출 중’으로 되돌린다 (용어집 버전 기록도 뺀다). 이미 추출 중이면 그대로. 바꿨으면 True.

    새 카드를 넣었거나, 파일을 모두 지웠거나, 게임 용어집·아이콘 사전을 지웠을 때: 저장된 진행 상태(용어집 확정·번역 끝)가
    지금 데이터와 맞지 않아 아이콘 정의~전체 번역이 끝난 것처럼 보이던 문제. 확정된 용어와 번역 캐시는 남으므로 다시 할 때
    새 카드의 용어·번역만 새로 한다."""
    from app import workspace

    if info.status == "extracting" and "glossary_version" not in info.extra:
        return False
    data = workspace._read(info.dir / "project.json")
    data["status"] = "extracting"
    data.pop("glossary_version", None)
    workspace._write(info.dir / "project.json", data)
    info.status = "extracting"
    info.extra.pop("glossary_version", None)
    return True


def add_files(project_dir: Path | str, files: list, group: str, *, new_group: bool = False,
              regions_from: Path | str | None = None) -> dict:
    """파일을 그룹에 넣는다. 화면은 먼저 list_groups로 기존 그룹을 보여 주고 ‘기존 그룹 / 새 그룹’을 고르게 한다.
    새 파일이 들어가면 프로젝트를 ‘추출 중’으로 되돌린다 (새 카드는 아직 읽지도 번역하지도 않았다)."""
    from app import workspace

    info = workspace.load_project_info(project_dir)
    r = workspace.add_files(info, files, group, new_group=new_group, regions_from=regions_from)
    if r.get("added"):
        back_to_extracting(info)
    return r


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
                    engine: str = "claude-code", client=None, progress: _base.ProgressFn | None = None,
                    cancel: _base.CancelToken | None = None, files: list | None = None) -> ProjectExtractResult:
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
            icons._save_scan(work, res)  # 받은 제안부터 남긴다 (아래 겹침 확인이 실패해도 제안은 쓴다)
            for g in list(res.groups):  # 제안이 사전 이름(crown 등)과 겹치면: 그림이 같을 때만 그 아이콘, 다르면 crown_2
                try:
                    icons.resolve_suggested_name(info, res, g)
                except Exception:  # noqa: BLE001 - 그 줄만 겹침 확인 없이 둔다
                    continue
            icons._save_scan(work, res)
        except (Paused, CardError, NotAvailable, Exception) as e:  # noqa: BLE001 - 제안 없이도 검증은 할 수 있다
            note = f"이름 제안을 받지 못함: {getattr(e, 'reason', e)}"
    xlsx = icons.export_review(info, res, info.game_dir / "아이콘_검증.xlsx")  # 아이콘 검증은 게임 단위
    _translate_erase._retire_legacy(info, ["아이콘_검증.xlsx"])
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
    # 아이콘 자리가 바뀌어 ③ 원문 확인에서 다시 읽어 확인할 칸 [{"file_dir", "page", "region"}]
    changed_cells: list[dict] = field(default_factory=list)


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
    return IconImportResult("confirmed", r.added, r.reused, r.replaced, r.removed,
                            changed_cells=[{"file_dir": fd, "page": pg, "region": rg} for fd, pg, rg in r.changed_cells])
