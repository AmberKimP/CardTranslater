"""시험 실행(어려운 카드 골라 시험하기)과 그 기록, 마지막 프로젝트."""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.project import load_project, parse_page_spec
from app.service import _base
from app.service import _erase_compose
from app.service import _review
from app.service import _settings
from app.service import _translate_erase


@dataclass
class TestRunsResult:
    state: str  # done | blocked
    runs: list = field(default_factory=list)  # [TestRunResult]
    reason: str = ""
    entry: dict | None = None  # 시험 기록(test_history.json)에 남긴 항목
    summary: dict = field(default_factory=dict)  # 문제 이름 -> 칸(정렬은 카드) 수


def test_run_cards(file_dirs: list, n: int, *, out_root: Path | str | None = None,
                   progress: _base.ProgressFn | None = None, cancel: _base.CancelToken | None = None) -> TestRunsResult:
    """그룹(파일 목록)의 앞 n장만 시험 폴더에서 지우기 -> 한글 넣기. 파일 순서대로, 각 파일의 처리할 쪽에서 채운다.
    프로젝트의 cleared/·translated/는 건드리지 않는다."""
    from app import workspace

    left = int(n)
    plan = []
    for fd in file_dirs:  # 처리할 쪽 (시험 모드 card_limit 안: 추출·번역한 쪽과 같은 범위)
        if left <= 0:
            break
        pages = workspace.included_pages(Path(fd))[:left]
        if pages:
            plan.append((fd, pages))
            left -= len(pages)
    return test_run_selection(plan, f"앞 {int(n)}장", out_root=out_root, progress=progress, cancel=cancel)


def test_run_selection(selection: list, label: str, *, out_root: Path | str | None = None,
                       progress: _base.ProgressFn | None = None, cancel: _base.CancelToken | None = None) -> TestRunsResult:
    """고른 카드만 시험 폴더에서 지우기 -> 한글 넣기. selection: [(파일 폴더, [쪽])], label: 시험 기록에 보일 방식
    ('앞 3장' · '쪽 1, 5' · '어려운 카드 5장'). 끝나면 그룹 폴더의 test_history.json에 남긴다 (최근 5번).
    프로젝트의 cleared/·translated/는 건드리지 않는다."""
    from app import workspace

    plan = [(Path(fd), list(pages)) for fd, pages in selection if pages]
    root = Path(out_root) if out_root else _test_runs_root()

    def listing() -> set:
        return set(root.iterdir()) if root.is_dir() else set()

    before = listing()

    def cleanup(done: list):
        """끝내지 못한 시험: 기록에 남지 않으니 이번에 만든 시험 폴더를 지운다 (데이터 폴더/test_runs 안만)."""
        for r in done:
            _remove_test_dir(r.run_dir)
            _remove_test_dir(r.erase_dir)
        for p in listing() - before:
            _remove_test_dir(p)

    runs = []
    try:
        for i, (fd, pages) in enumerate(plan):
            def say(e, _i=i, _fd=fd):  # 파일마다 0%부터 다시 가지 않게 전체 몇 번째 파일인지 붙인다
                if progress:
                    progress(_base.ProgressEvent(e.stage, e.done, e.total,
                                           f"{_i + 1}/{len(plan)} {workspace.file_key(_fd)}: {e.message}", e.page))
            r = test_run(fd, workspace.pages_spec(pages), out_root=out_root, progress=say, cancel=cancel)
            if r.state != "done":
                cleanup(runs + [r])
                return TestRunsResult("blocked", [], r.reason)
            runs.append(r)
    except BaseException:  # 취소·오류도 같은 정리
        cleanup(runs)
        raise
    if not runs:
        return TestRunsResult("blocked", reason="시험할 카드가 없습니다 (처리할 쪽이 없음)")
    summary = _test_summary([(fd, r) for (fd, _p), r in zip(plan, runs)])
    entry = {
        "label": label,
        "cards": [{"file_key": workspace.file_key(fd), "page": int(p)} for fd, pages in plan for p in pages],
        "runs": [{"file_key": workspace.file_key(fd), "compose_dir": str(r.run_dir), "erase_dir": str(r.erase_dir)}
                 for (fd, _p), r in zip(plan, runs)],
        "summary": summary,
    }
    try:  # 기록을 못 남겨도 시험 결과는 보여 준다
        entry = _record_test(_test_group_dir(plan[0][0]), entry)
    except OSError as e:
        logging.getLogger("cardtranslater.test_run").warning("시험 기록을 남기지 못했습니다: %s", e)
    return TestRunsResult("done", runs, entry=entry, summary=summary)


@dataclass
class TestRunResult:
    state: str  # done | blocked
    run_dir: Path | None = None  # 시험 한글 넣기 폴더 (compare/, report.json)
    erase_dir: Path | None = None
    pages: list[int] = field(default_factory=list)
    status_summary: dict = field(default_factory=dict)
    reason: str = ""


def test_run(file_dir: Path | str, pages: str, *, out_root: Path | str | None = None,
             progress: _base.ProgressFn | None = None, cancel: _base.CancelToken | None = None) -> TestRunResult:
    """고른 쪽만 시험 폴더(데이터 폴더/test_runs)에서 지우기 -> 한글 넣기. 프로젝트의 cleared/·translated/는
    건드리지 않는다. 번역은 검수 엑셀이 있으면 그것, 없으면 초벌 번역."""
    from app.excel_io import import_translation
    from app.typeset_compose import run_compose

    fd = Path(file_dir)
    root = Path(out_root) if out_root else _settings.data_dir() / "test_runs"
    project = load_project(fd)
    import pymupdf

    with pymupdf.open(project.source_pdf) as d:
        page_list = parse_page_spec(pages, d.page_count)
    if not page_list:
        return TestRunResult("blocked", reason="쪽을 고르세요")
    er = _erase_compose.erase(fd, pages, test=True, out_root=root, progress=progress, cancel=cancel)
    if not er.ok:
        return TestRunResult("blocked", erase_dir=er.output_dir, reason=er.error or "지우기 실패")
    xlsx = _translate_erase.review_xlsx_for(fd)
    if xlsx:
        imp = import_translation(project, xlsx)
        cards, source = imp.cards, str(xlsx)
    else:
        cards, source = _translate_erase.cards_from_translation(fd), "초벌 번역 (translate/cache.json)"
    _final, run_dir = _erase_compose._output_dirs(project.root, "translated", "compose", True, root)
    run_dir, _code = run_compose(project.root, er.output_dir, pages, run_dir, cards=cards, translation_source=source,
                                 out_name="composed.pdf", progress=progress or _base._noop, cancel=cancel)
    rep = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    _translate_erase._attach_sheet_images(fd, run_dir, None, progress or _base._noop, pdf_name="composed.pdf", test=True)  # 시험한 쪽만
    return TestRunResult("done", run_dir, er.output_dir, page_list, rep.get("status_summary", {}))


# ---------------------------------------------------------------- 시험 실행: 어려운 카드 고르기·기록·비교 (5.9절)
TEST_HISTORY_FILE = "test_history.json"
TEST_HISTORY_KEEP = 5  # 최근 시험 몇 번을 남길지 (넘치면 오래된 시험 폴더를 지운다)
ALIGN_CHECK = "정렬 확인 필요"
_ICON_TOKEN = re.compile(r"\[\[[^\]]*\]\]")  # [[coin]] · [[?]]
_FORMAT_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9]*>")  # <b> </b> <i> <c1> …


def _source_counts(text: str) -> tuple[int, int]:
    """원문 -> (글자 수, 아이콘 수). 글자 수는 [[아이콘]]·서식 태그·빈칸을 빼고 센다."""
    icons = len(_ICON_TOKEN.findall(text))
    plain = _FORMAT_TAG.sub("", _ICON_TOKEN.sub("", text))
    return len("".join(plain.split())), icons


def pick_hard_cards(file_dirs: list, n_long: int = 3, n_icons: int = 2) -> list[dict]:
    """시험할 어려운 카드: 글자가 가장 긴 카드 n_long장 + 아이콘이 가장 많은 카드 n_icons장 (서로 다른 카드).
    추출한 원문으로 고르고 처리할 쪽만 본다. 카드가 모자라면 있는 만큼만.
    [{"file_dir", "file_key", "page", "reason"}] (고른 순서: 긴 카드 먼저)."""
    from app import workspace
    from app.extract import load_extracted

    stats = []  # (글자 수, 아이콘 수, 파일 순서, 쪽, 파일 폴더)
    for order, fd in enumerate(Path(x) for x in file_dirs):
        try:
            extracted = load_extracted(fd)
            included = set(workspace.included_pages(fd))
        except (OSError, ValueError, KeyError):
            continue
        for page, regions in extracted.items():
            if page not in included:
                continue
            text = "\n".join(str((r or {}).get("source") or "") for r in regions.values())
            chars, icons = _source_counts(text)
            stats.append((chars, icons, order, page, fd))

    def pick(fd, page, reason):
        return {"file_dir": str(fd), "file_key": workspace.file_key(fd), "page": int(page), "reason": reason}

    out, taken = [], set()
    for chars, _i, _o, page, fd in sorted((s for s in stats if s[0] > 0), key=lambda s: (-s[0], s[2], s[3]))[:n_long]:
        out.append(pick(fd, page, f"글자 가장 긴 카드 ({chars}자)"))
        taken.add((str(fd), page))
    icon_cards = [s for s in sorted(stats, key=lambda s: (-s[1], s[2], s[3])) if s[1] > 0
                  and (str(s[4]), s[3]) not in taken]
    for _c, icons, _o, page, fd in icon_cards[:n_icons]:
        out.append(pick(fd, page, f"아이콘 가장 많은 카드 ({icons}개)"))
    return out


def parse_test_pages(file_dir: Path | str, spec: str) -> list[int]:
    """시험 실행 ‘쪽 지정’ 입력('1, 5, 12-14') -> 쪽 목록. 잘못 적었거나 범위 밖·뺀 쪽이면 ValueError(화면에 보일 문구)."""
    from app import workspace

    fd = Path(file_dir)
    # 쉼표로 나눈 칸마다 '5' 또는 '12-14'만 (1-2-3·1 2·-3 같은 것은 막는다)
    if not all(re.fullmatch(r"\s*(\d+\s*(-\s*\d+\s*)?)?", part) for part in (spec or "").split(",")):
        raise ValueError("쪽은 1, 5, 12-14처럼 적으세요")
    pages = parse_page_spec(spec, workspace.page_count(fd))
    included = set(workspace.included_pages(fd, limited=False))
    excluded = [p for p in pages if p not in included]
    if excluded:
        raise ValueError(f"처리하지 않는(뺀) 쪽입니다: {', '.join(map(str, excluded))}")
    limited = set(workspace.included_pages(fd))  # 시험 모드(card_limit)면 추출·번역한 앞쪽만
    not_yet = [p for p in pages if p not in limited]
    if not_yet:
        raise ValueError(f"아직 추출·번역하지 않은 쪽입니다 (시험 모드): {', '.join(map(str, not_yet))}")
    return pages


def _test_group_dir(file_dir: Path | str) -> Path:
    """시험 기록을 둘 곳: 그룹 안 파일이면 그룹 폴더, 아니면(예전 방식) 파일 폴더."""
    fd = Path(file_dir)
    g = fd.parent.parent
    return g if (g / "group.json").exists() else fd


def _test_file_dir(group_dir: Path, file_key: str) -> Path | None:
    """시험 기록의 file_key('그룹/파일') -> 파일 폴더 (그룹 이름을 바꿨어도 찾게 파일 이름만 쓴다).
    / · \\ 어느 쪽으로 나눠 적었어도 마지막 이름만 쓰고, <그룹>/files 밖을 가리키면 None."""
    from pathlib import PureWindowsPath

    g = Path(group_dir)
    if not (g / "group.json").exists():
        return g
    name = PureWindowsPath(str(file_key or "")).name  # Windows 경로 규칙은 / 와 \ 를 모두 나눈다
    if name in ("", ".", ".."):
        return None
    files = (g / "files").resolve()
    fd = g / "files" / name
    return fd if fd.resolve().parent == files else None


def _align_checks(file_dir: Path, pages: list[int]) -> list[int]:
    """자동 정렬(align.json)에서 ‘정렬 확인 필요’로 남은 쪽 (파일이 없거나 깨졌으면 없음)."""
    try:
        data = json.loads((Path(file_dir) / "align.json").read_text(encoding="utf-8"))
        recs = data.get("pages") or {}
        return [p for p in pages if isinstance(recs.get(str(p)), dict) and recs[str(p)].get("status") == "check"]
    except (OSError, ValueError, AttributeError):
        return []


def _test_summary(runs: list) -> dict:
    """[(파일 폴더, TestRunResult)] -> {문제 이름: 수}. 넘침·글자 축소·서식 불일치·아이콘 오류 등은 칸 수,
    정렬 확인 필요는 카드 수."""
    out: dict[str, int] = {}
    for fd, r in runs:
        for st, keys in (r.status_summary or {}).items():
            out[st] = out.get(st, 0) + len(keys)
        n = len(_align_checks(fd, r.pages))
        if n:
            out[ALIGN_CHECK] = out.get(ALIGN_CHECK, 0) + n
    return out


def _test_runs_root() -> Path:
    return _settings.data_dir() / "test_runs"


def _remove_test_dir(p: Path | str | None) -> bool:
    """시험 폴더를 지운다. 데이터 폴더/test_runs 안에 있는 폴더만 (다른 곳은 절대 지우지 않는다)."""
    if not p:
        return False
    try:
        root = _test_runs_root().resolve()
        rp = Path(p).resolve()
    except OSError:
        return False
    if rp == root or root not in rp.parents or not rp.is_dir():
        return False
    shutil.rmtree(rp, ignore_errors=True)
    return True


def _read_test_history(group_dir: Path) -> list[dict]:
    try:
        data = json.loads((Path(group_dir) / TEST_HISTORY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("tests") if isinstance(data, dict) else data
    return [e for e in items or [] if isinstance(e, dict) and e.get("id")]


def _record_test(group_dir: Path, entry: dict) -> dict:
    """시험 기록 맨 앞에 넣고 최근 TEST_HISTORY_KEEP번만 남긴다. 밀려난 시험의 폴더는 지운다
    (데이터 폴더/test_runs 안에 있을 때만). 넣은 항목(id·at 붙음)을 돌려준다."""
    items = _read_test_history(group_dir)
    ids = {e["id"] for e in items}
    base = time.strftime("%Y%m%d-%H%M%S")
    eid, k = base, 1
    while eid in ids:
        k += 1
        eid = f"{base}-{k}"
    entry = {"id": eid, "at": time.strftime("%Y-%m-%d %H:%M:%S"), **entry}
    items.insert(0, entry)
    keep, drop = items[:TEST_HISTORY_KEEP], items[TEST_HISTORY_KEEP:]
    for old in drop:
        for run in old.get("runs") or []:
            _remove_test_dir(run.get("compose_dir"))
            _remove_test_dir(run.get("erase_dir"))
    Path(group_dir).mkdir(parents=True, exist_ok=True)
    p = Path(group_dir) / TEST_HISTORY_FILE
    tmp = p.with_suffix(".tmp")  # 쓰다가 멈춰도 반쪽짜리 기록이 남지 않게 임시 파일에 쓰고 바꾼다
    tmp.write_text(json.dumps({"tests": keep}, ensure_ascii=False, indent=2), encoding="utf-8")
    for i in range(20):  # Windows: 다른 스레드가 마침 읽는 중이면 바꿔 쓰기를 거절한다
        try:
            tmp.replace(p)
            break
        except PermissionError:
            if i == 19:
                raise
            time.sleep(0.05)
    return entry


def test_history(group_dir: Path | str) -> list[dict]:
    """그룹의 최근 시험 (새것부터, 최대 5번). 결과 폴더가 없어진 시험은 뺀다.
    항목: {"id", "at", "label", "cards": [{"file_key", "page"}], "runs": [{"file_key", "compose_dir", "erase_dir"}],
    "summary": {문제 이름: 수}}."""
    out = []
    for e in _read_test_history(Path(group_dir)):
        runs = e.get("runs") or []
        if runs and all(r.get("compose_dir") and Path(r["compose_dir"]).is_dir() for r in runs):
            out.append(e)
    return out


def _test_entry(group_dir: Path | str, entry_id: str) -> dict | None:
    return next((e for e in test_history(group_dir) if e["id"] == entry_id), None)


def rerun_test(group_dir: Path | str, entry_id: str, progress: _base.ProgressFn | None = None,
               cancel: _base.CancelToken | None = None, *, out_root: Path | str | None = None) -> TestRunsResult:
    """기록에 있는 시험과 같은 카드로 다시 시험한다 (새 기록이 생긴다). 영역·서식을 고친 뒤 비교할 때."""
    entry = _test_entry(group_dir, entry_id)
    if entry is None:
        return TestRunsResult("blocked", reason="그 시험 기록이 없습니다 (결과 폴더가 지워졌을 수 있습니다)")
    selection: list[tuple[Path, list[int]]] = []
    for c in entry.get("cards") or []:
        fd = _test_file_dir(Path(group_dir), c.get("file_key") or "")
        if fd is None:
            return TestRunsResult("blocked", reason=f"시험 기록의 파일 이름이 잘못됐습니다: {c.get('file_key')}")
        if selection and selection[-1][0] == fd:
            selection[-1][1].append(int(c["page"]))
        else:
            selection.append((fd, [int(c["page"])]))
    missing = [str(fd) for fd, _p in selection if not fd.exists()]
    if missing:
        return TestRunsResult("blocked", reason="파일이 없습니다: " + ", ".join(missing))
    return test_run_selection(selection, entry.get("label") or "다시 시험", out_root=out_root,
                              progress=progress, cancel=cancel)


def _test_cards(group_dir: Path, entry: dict) -> dict:
    """시험 기록 하나 -> {(file_key, 쪽): {"png", "status"}}. 정렬 확인 필요는 지금 align.json으로 본다."""
    out = {}
    for run in entry.get("runs") or []:
        d = Path(run.get("compose_dir") or "")
        pages = _review.result_pages(d)
        fd = _test_file_dir(group_dir, run.get("file_key") or "")
        checks = set(_align_checks(fd, [p["page"] for p in pages])) if fd is not None else set()
        for p in pages:
            png = d / f"p{p['page']:03d}.png"
            st = list(p["status"]) + ([ALIGN_CHECK] if p["page"] in checks else [])
            out[(run["file_key"], p["page"])] = {"png": png if png.exists() else None, "status": st}
    return out


def compare_tests(group_dir: Path | str, id_a: str, id_b: str) -> list[dict]:
    """두 시험에 함께 있는 카드마다 결과를 나란히: [{"file_key", "page", "a_png", "b_png", "a_status", "b_status"}]
    (시험 a의 카드 순서). 기록이 없으면 빈 목록."""
    g = Path(group_dir)
    a, b = _test_entry(g, id_a), _test_entry(g, id_b)
    if a is None or b is None:
        return []
    ca, cb = _test_cards(g, a), _test_cards(g, b)
    order = [(c["file_key"], int(c["page"])) for c in a.get("cards") or []]
    order += [k for k in ca if k not in order]
    return [{"file_key": k[0], "page": k[1], "a_png": ca[k]["png"], "b_png": cb[k]["png"],
             "a_status": ca[k]["status"], "b_status": cb[k]["status"]} for k in order if k in ca and k in cb]


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
