"""② 원문 확인의 칸(읽은 결과·다시 읽기)과 아이콘 동작."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from app import workspace
from app.project import load_project
from app.regions import text_only
from app.service import _base
from app.service import _extract
from app.service import _settings

log = logging.getLogger("cardtranslater.icons")  # 카드 보기 조작 기록 (나중에 로그로 무엇을 눌렀는지 본다)


# ---------------------------------------------------------------- 화면용: 원문 확인 (2차)


def _locked(fn):
    """추출 결과(쪽 파일)를 고치는 조작을 workspace.EXTRACT_LOCK 안에서 (뒤에서 다시 읽는 스레드와 섞이지 않게)."""
    import functools

    @functools.wraps(fn)
    def run(*a, **k):
        with workspace.EXTRACT_LOCK:
            return fn(*a, **k)
    return run


def extracted_cells(file_dir: Path | str) -> list[dict]:
    """파일의 추출 결과를 칸마다: {"page", "region", "kind", "source", "label", "uncertain", "note",
    "readings", "edited"}. 뺀 쪽은 빠지고, 아직 읽지 않은 쪽은 source=None.
    readings = 화면의 읽은 결과 표: 두 번 읽기 결과, 그 뒤에 마지막 ‘이 칸 다시 읽기’ 결과(reread=True) 하나.
    다시 읽기 결과만 있으면 지금 원문을 처음 줄(original=True)로 넣어 비교할 수 있게 한다."""
    from app import workspace
    from app.extract import (
        MODEL_LABEL, breaks_for_text, cell_rereads, keep_state, load_extracted, model_readings, verification_label,
    )

    def row(x: dict, **flags) -> dict:
        return {"model": MODEL_LABEL.get(x["model"], x["model"]), "source": x["source"],
                "reread": False, "original": False, **flags}

    from app import font_plan

    fd = Path(file_dir)
    proj = load_project(fd)
    ext = load_extracted(fd)
    try:  # ‘문단마다 다른 글꼴’ 영역 (원문 편집 칸에 문단 나눔 표시 [====])
        region_modes = font_plan.region_font_modes(fd)
    except Exception:  # noqa: BLE001 - 설정을 못 읽으면 표시만 안 한다
        region_modes = {}

    def para_mode(reg) -> bool:
        # region_keep_mode와 같은 규칙 (카드별 영역 설정이 있으면 그것, 없으면 기본 영역 설정). 칸마다 프로젝트를
        # 다시 읽지 않도록 이미 읽은 영역(reg)으로 정한다
        mode = (getattr(reg, "style", None) or {}).get("font_mode") or region_modes.get(reg.name) or "cell"
        return mode == "sentence" and reg.kind == "text"
    out = []
    for pno in workspace.included_pages(fd):
        for reg in proj.template.regions_for(pno):
            r = (ext.get(pno) or {}).get(reg.name)
            readings = [row(x) for x in model_readings(r or {})]
            # 다시 읽은 결과는 마지막 것만 보여 준다 (읽을 때마다 표에 줄이 쌓이지 않게. 기록은 파일에 모두 남는다).
            # 마지막이 정밀 읽기(Opus)가 아니면 가장 최근 정밀 읽기도 함께 (편집 칸 기본값·비교 기준이 그것이다)
            all_re = cell_rereads(r or {})
            keep = all_re[-1:]
            if keep and "opus" not in (keep[0].get("model") or "").lower():
                opus = [x for x in all_re[:-1] if "opus" in (x.get("model") or "").lower()]
                keep = opus[-1:] + keep
            rereads = [row(x, reread=True) for x in keep]
            if any("opus" in (x.get("model") or "").lower() for x in keep):
                # 정밀 읽기로 다시 읽었으면 처음의 정밀 읽기는 보이지 않는다 (같은 모델의 예전 결과라 헷갈림. 기록은 파일에)
                readings = [x for x in readings if "opus" not in x["model"].lower()]  # 표시 이름 Opus·모델 id 모두
            if rereads and not readings and r is not None and r.get("source") is not None:
                first = "지금 원문" if r.get("edited_by") else (r.get("read_by") or "처음 읽은 결과")
                readings = [row({"model": first, "source": r["source"]}, original=True)]
            out.append({
                "page": pno, "region": reg.name, "kind": reg.kind,
                "source": None if r is None else r.get("source"),
                "label": "읽지 않음" if r is None else verification_label(r),
                "uncertain": bool(r and r.get("uncertain")),
                "note": (r or {}).get("note", ""),
                "readings": readings + rereads,
                "edited": bool(r and (r.get("edited_by") or r.get("checked_by"))),
                # 전체가 아이콘 — 번역 안 함 (아이콘 문단만으로 정했는데 글 줄이 모두 남길 줄인 칸도 칸 전체)
                "keep_original": keep_state(r)[0] == "whole",
                # ‘원본 그대로’: "" | whole(칸 전체) | partial(아이콘 문단만) | broken(아이콘 문단만인데 원문 줄 수가
                # 바뀌어 맞출 수 없음 — 칸 전체를 원본 그대로 둔다)
                "keep_mode": keep_state(r)[0],
                "keep_lines": keep_state(r)[1],  # partial일 때 원본 그대로 둘 원문 줄 번호 (빈 줄 포함, 0부터)
                "icon_changed": list((r or {}).get("icon_changed") or []),  # ② 원문 확인에서 자리가 바뀜
                # ② 원문 확인에서 ‘아이콘 아님’으로 뺀 [[?]] 수 (None = 그 기록 전에 정한 칸·정한 적 없음)
                "icon_removed": None if (r or {}).get("icon_removed") is None else int(r["icon_removed"]),
                # 문단마다 다른 글꼴 영역 (글 칸): 원문 편집 칸에 문단 나눔을 [====] 줄로 보이고 고칠 수 있다
                "paragraph_mode": para_mode(reg),
                # ②에서 정한 문단 나눔 (원문의 빈 줄 뺀 줄 번호), 정한 적이 없거나 맞출 수 없으면 None (기본 규칙)
                "paragraph_breaks": breaks_for_text(r, (r or {}).get("source")) if r else None,
            })
    return out


def icon_reread_pending(file_dirs) -> list[dict]:
    """② 원문 확인에서 아이콘 자리가 바뀌었는데 아직 다시 읽지 않은 칸 [{"file_dir", "page", "region"}].
    ‘나중에 (원문 확인에서)’를 고르면 ② 원문 확인을 열 때 이 칸들을 먼저 다시 읽는다."""
    from app import workspace
    from app.extract import keep_state, load_extracted

    out = []
    for fd in map(Path, file_dirs):
        inc = set(workspace.included_pages(fd))
        tpl = load_project(fd).template
        for pno, regs in sorted(load_extracted(fd).items()):
            if pno not in inc:
                continue
            # 영역을 지운 칸은 다시 읽을 수 없다. ‘글자만’ 영역은 아이콘을 찾지 않으므로 아이콘 때문에 다시 읽지 않는다
            names = {reg.name for reg in tpl.regions_for(pno) if not text_only(reg)}
            out += [{"file_dir": fd, "page": pno, "region": name} for name, r in regs.items()
                    if r and r.get("icon_reread") and r.get("icon_changed") and name in names
                    # 칸 전체 원본 그대로만 뺀다: 문장만 원본 그대로(partial)인 칸은 나머지 문장을 번역하므로 다시 읽는다
                    and not r.get("keep_original") and keep_state(r)[0] not in ("whole", "broken")]
    return out


def skip_icon_reread(cells: list[dict]) -> None:
    """원문 확인을 열 때 먼저 다시 읽기를 이 칸들은 하지 않는다 (취소했거나 그 칸만 읽지 못함).
    ‘확인 필요’·icon_changed는 그대로라 원문 확인에서 보이고 ‘이 칸 다시 읽기’로 읽을 수 있다."""
    from app.icons import _extract_files

    for c in cells:
        fd, page, region = Path(c["file_dir"]), c["page"], c["region"]
        for f in _extract_files(fd, page):
            rec = json.loads(f.read_text(encoding="utf-8"))
            cell = (rec.get("regions") or {}).get(region)
            if cell is None or "icon_reread" not in cell:
                continue
            cell.pop("icon_reread")
            workspace._write(f, rec)


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


def save_source(file_dir: Path | str, page: int, region: str, source, paragraph_breaks: list[int] | None = None) -> None:
    """사람이 고친 원문을 추출 결과에 저장한다 (확인 필요를 지우고, 모델이 읽은 값은 note에 남긴다).
    source: 글자 칸은 문자열, 태그 칸은 목록. 그 칸의 번역은 다음 번역 때 새 원문으로 다시 한다.
    paragraph_breaks: ②의 [====]로 정한 문단 나눔 (문단마다 다른 글꼴 영역, 빈 줄 뺀 줄 번호; None이면 그대로 둔다).
    기본 규칙과 같고 아직 정한 적이 없으면 적지 않는다 (나중에 규칙이 나아지면 따르게)."""
    from app.extract import PARAGRAPH_BREAKS, page_file
    from app.service._review import paragraph_starts

    fd = Path(file_dir)
    files = [p for p in (page_file(fd / "extract", page), fd / "extract" / f"page-{page:03d}.json") if p.exists()]
    if not files:  # 아직 읽지 않은 쪽: 손 입력으로 새로 만든다
        f = page_file(fd / "extract", page)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"page": page, "model": "사람", "regions": {}}, ensure_ascii=False), encoding="utf-8")
        files = [f]
    with workspace.EXTRACT_LOCK:
        _save_source_files(files, region, source, paragraph_breaks)


def _save_source_files(files, region: str, source, paragraph_breaks) -> None:
    from app.extract import PARAGRAPH_BREAKS
    from app.service._review import paragraph_starts

    for f in files:
        rec = json.loads(f.read_text(encoding="utf-8"))
        r = rec.setdefault("regions", {}).setdefault(region, {"source": None, "uncertain": False, "note": ""})
        old = r.get("source")
        r["uncertain"] = False
        r.pop("icon_changed", None)  # 아이콘 정의 뒤 확인할 칸: 사람이 보고 저장했으니 풀린다
        r.pop("icon_reread", None)
        r.pop("icon_settled", None)  # ‘아이콘 고침’ 알림도: 사람이 보고 저장했다
        if paragraph_breaks is not None and isinstance(source, str):
            br = sorted({int(i) for i in paragraph_breaks})
            if PARAGRAPH_BREAKS in r or br != paragraph_starts(source):
                r[PARAGRAPH_BREAKS] = br
        if old == source:  # 고치지 않고 저장 = 사람이 보고 맞다고 확인함
            r["checked_by"] = "사람"
            if not r.get("edited_by"):
                r["note"] = "사람이 확인 (읽은 그대로)"
        else:
            r["source"] = source
            kp = _remap_blank_lines(old, source, r.get("keep_paragraphs"))
            if kp is not None:
                # 빈 줄만 넣거나 뺐다: 아이콘 문단만 원본 그대로의 줄 번호를 새 원문에 맞춘다
                # (그대로 두면 줄 수가 달라 ‘맞출 수 없음’ → 칸 전체가 말없이 원본 그대로가 된다). 아이콘·숫자도 그대로
                r["keep_paragraphs"] = kp
            else:
                # 아이콘에 넣으며 뺀 숫자의 기록(몇 번째 [[…]], 그 옆 숫자)은 예전 원문 기준이다. 새 원문에 두면
                # ‘다시 정하기’가 숫자를 엉뚱한 자리에 되살리므로 버린다 (숫자는 사람이 고친 원문에 있는 그대로)
                r.pop("icon_numbers", None)
                r.pop("icon_number_note", None)
            r["edited_by"] = "사람"
            r["note"] = (f"사람이 고침 (모델: {json.dumps(old, ensure_ascii=False)})" if old is not None
                         else "사람이 입력")
        workspace._write(f, rec)  # 임시 파일로 쓰고 바꿔 넣기 (뒤에서 지우는 스레드가 읽는 중이면 다시 시도)


def _remap_blank_lines(old, new, kp: dict | None) -> dict | None:
    """원문이 빈 줄만 달라졌을 때(글 줄은 순서·글 그대로) 아이콘 문단만 원본 그대로(keep_paragraphs)의 줄 번호·줄 수를
    새 원문에 맞춘 값. 빈 줄 말고도 바뀌었거나 그 기록이 없으면 None (예전처럼: 줄 수가 달라지면 맞출 수 없는 칸)."""
    if not kp or not isinstance(old, str) or not isinstance(new, str):
        return None
    o_rows, n_rows = old.split("\n"), new.split("\n")
    o_idx = [i for i, t in enumerate(o_rows) if t.strip()]
    n_idx = [i for i, t in enumerate(n_rows) if t.strip()]
    if [o_rows[i] for i in o_idx] != [n_rows[i] for i in n_idx] or kp.get("n") != len(o_rows):
        return None
    from app.extract import keep_row_print

    where = dict(zip(o_idx, n_idx))  # 예전 글 줄 번호 -> 새 번호 (남길 줄 중 빈 줄은 뺀다)
    lines = sorted(where[i] for i in kp.get("lines") or [] if i in where)
    out = {**kp, "lines": lines, "n": len(n_rows)}
    if isinstance(kp.get("rows"), list):
        out["rows"] = [keep_row_print(n_rows[i]) for i in lines]
    return out


@dataclass
class RereadResult:
    state: str  # done | paused | blocked
    source: object = None  # 다시 읽은 원문 (글자 칸은 문자열, 태그 칸은 목록)
    uncertain: bool = False
    note: str = ""
    spent_usd: float = 0.0  # API로 실제 쓴 비용
    equiv_usd: float = 0.0  # Claude Code 요금제로 읽은 분량의 API 정가 환산 (청구되지 않음)
    reason: str = ""  # 멈춘·못 한 이유
    retry_after: str | None = None
    # 아이콘이 바뀐 칸을 보통·정밀 읽기로 두 번 읽어(double=True) 두 결과가 같고 지금 원문과도 같아 확인이 끝남
    settled: bool = False


CELL_BLOCKED = ("다시 읽지 못했습니다", "응답에 이 칸의 결과가 없습니다")  # 로그인·한도가 아닌, 그 칸만의 실패


def reread_cells(cells: list[dict], *, model: str | None = None, engine: str | None = None, client=None,
                 progress: _base.ProgressFn | None = None, cancel: _base.CancelToken | None = None) -> dict:
    """② 원문 확인에서 아이콘 자리가 바뀐 칸을 차례로 다시 읽는다. cells: [{"file_dir", "page", "region"}].
    model=None(기본): 원문 추출의 꼼꼼히 모드와 같은 두 모델(보통·정밀 읽기)로 모두 읽어, 두 결과가 같고 지금 원문과도
    같으면 확인을 끝내고(icon_changed가 풀림) 아니면 ‘아이콘 고침 · 확인 필요’로 둔다 (비용은 한 번 읽기의 약 두 배).
    model을 주면 그 모델로 한 번만 읽어 rereads에 덧붙인다 (예전 동작, 원문·상태는 그대로).
    {"done": 읽은 수, "settled": 확인이 끝난 수, "failed": [(칸, 이유)], "state": done|paused|blocked, "reason",
    "retry_after", "spent_usd"}.
    한도에 걸리면 거기서 멈춘다 (남은 칸은 ‘확인 필요’로 남아 원문 확인에서 하나씩 다시 읽을 수 있다)."""
    progress = progress or _base._noop
    out = {"done": 0, "settled": 0, "failed": [], "state": "done", "reason": "", "retry_after": None,
           "spent_usd": 0.0}
    for i, c in enumerate(cells):
        if cancel:
            cancel.check()
        progress(_base.ProgressEvent("reread", i, len(cells), f"{c['page']}쪽 {c['region']} 다시 읽는 중"))
        if model is None:
            r = reread_region(c["file_dir"], c["page"], c["region"], engine=engine, client=client, double=True)
        else:
            r = reread_region(c["file_dir"], c["page"], c["region"], model=model, engine=engine, client=client)
        out["spent_usd"] += r.spent_usd
        if r.state == "done":
            out["done"] += 1
            out["settled"] += bool(getattr(r, "settled", False))
        elif r.state == "blocked" and r.reason.startswith(CELL_BLOCKED):  # 이 칸만의 문제: 기록하고 다음 칸
            out["failed"].append((f"{c['page']}쪽 {c['region']}", r.reason))
            skip_icon_reread([c])  # 다음에 원문 확인을 열 때 또 읽지(돈을 쓰지) 않는다 — ‘이 칸 다시 읽기’로 읽는다
        elif r.state in ("paused", "blocked"):
            out.update(state=r.state, reason=r.reason, retry_after=r.retry_after)
            # 한도·로그인: 남은 칸은 다음에 원문 확인을 열 때 또 자동으로 읽지 않는다 (또 한도에 걸려 못 넘어감).
            # ‘확인 필요’·icon_changed는 남아 원문 확인에서 ‘이 칸 다시 읽기’로 읽는다
            skip_icon_reread(cells[i:])
            break
        else:
            out["failed"].append((f"{c['page']}쪽 {c['region']}", r.reason))
            skip_icon_reread([c])
    progress(_base.ProgressEvent("reread", len(cells), len(cells), "다시 읽기 끝"))
    return out


def reread_region(file_dir: Path | str, page: int, region: str, *, model: str = "opus",
                  engine: str | None = None, client=None, double: bool = False) -> RereadResult:
    """원문 확인 화면에서 고른 칸 하나만 다시 읽는다. 결과는 그 칸의 rereads에 덧붙이고, 원문(source)·
    readings(두 번 읽기 결과)·확인 필요는 바꾸지 않는다 — 칸 상태는 사람이 보고 저장할 때 바뀐다.
    double=True: ② 원문 확인에서 아이콘 자리가 바뀐 칸 — 보통·정밀 읽기(원문 추출 꼼꼼히 모드의 두 모델)로 모두 읽어
    readings에 둘을 남긴다 (_settle_icon_cell). model은 쓰지 않는다.
    시험 모드·뺀 쪽이어도 읽는다(사람이 고른 칸).
    한도·로그인 문제는 state="paused"·"blocked"로 돌려준다 (예외 아님)."""
    from app.extract import make_client, resolve_model
    from app.extract_cc import ClaudeCodeClient

    log = logging.getLogger("cardtranslater.extract")
    fd = Path(file_dir)
    try:
        model_ids = ([resolve_model(m) for m in _extract.EXTRACT_MODES["double-check"]] if double
                     else [resolve_model(model)])
    except ValueError as e:
        return RereadResult("blocked", reason=str(e))
    proj = load_project(fd)
    reg = next((r for r in proj.template.regions_for(page) if r.name == region), None)
    if reg is None:
        return RereadResult("blocked", reason=f"{page}쪽에 ‘{region}’ 영역이 없습니다")
    if client is None:
        eng = engine or _settings.get_settings().engine
        try:
            client = ClaudeCodeClient() if eng == "claude-code" else make_client()
        except Exception as e:  # noqa: BLE001  (키·claude 명령이 없을 때 등)
            return RereadResult("blocked", reason=f"Claude에 연결할 수 없습니다: {e}")
    entries: list[dict] = []
    spent = equiv = 0.0
    for model_id in model_ids:  # 보통 읽기 먼저 (싸다): 그것이 한도에 걸리면 정밀 읽기에 돈을 쓰지 않는다
        got = _read_cell(client, proj, page, reg, model_id, fd / "extract")
        if isinstance(got, RereadResult):  # 멈춤·실패: 먼저 읽은 결과는 ‘이 칸 다시 읽기’ 결과로 남긴다 (쓴 돈을 버리지 않게)
            if entries:
                _update_cell(fd, page, region, lambda cell: cell.setdefault("rereads", []).extend(entries))
            got.spent_usd += spent
            got.equiv_usd += equiv
            return got
        entry, s, e = got
        entries.append(entry)
        spent, equiv = spent + s, equiv + e
    if double:
        a, b = entries
        settled = _update_cell(fd, page, region, lambda cell: _settle_icon_cell(cell, a, b))
        log.info("아이콘 고친 칸 두 번 읽음: %d쪽 %s (%s)", page, region,
                 "같음 — 확인 끝" if settled else "확인 필요")
        return RereadResult("done", b["source"], b["uncertain"], b["note"], spent, equiv, settled=bool(settled))

    # 이미 읽은 칸에만 덧붙인다 (아직 읽지 않은 칸은 편집 칸에 넣어 주고, 저장할 때 원문이 생긴다)
    def add(cell: dict):
        cell.setdefault("rereads", []).append(entries[0])
        cell.pop("icon_reread", None)  # ② 원문 확인 뒤 다시 읽기를 마침
    _update_cell(fd, page, region, add)
    log.info("칸 다시 읽음: %d쪽 %s (%s)", page, region, model_ids[0])
    e0 = entries[0]
    return RereadResult("done", e0["source"], e0["uncertain"], e0["note"], spent, equiv)


def _read_cell(client, proj, page: int, reg, model_id: str, base: Path):
    """칸 하나를 한 모델로 읽는다. (rereads·readings에 넣을 결과, API 비용, 요금제 환산), 못 읽으면 RereadResult."""
    import pymupdf

    from app.extract import Paused, _iso_after, _read_regions_once
    from app.extract_cc import CardError, NotAvailable

    log = logging.getLogger("cardtranslater.extract")
    try:
        # 잘라 낸 그림은 extract/reread/에 둔다 (extract/crops/의 그 쪽 첫 칸 그림을 덮어쓰지 않게)
        with pymupdf.open(proj.source_pdf) as src_doc:
            got, spent, equiv = _read_regions_once(client, proj, page, src_doc, model_id, base / "reread", [reg])
    except Paused as p:
        log.warning("칸 다시 읽기 일시 정지 (%d쪽 %s): %s", page, reg.name, p.reason)
        return RereadResult("paused", reason=p.reason, retry_after=_iso_after(p.retry_after_s))
    except NotAvailable as e:
        return RereadResult("blocked", reason=str(e))
    except CardError as e:
        log.warning("칸 다시 읽기 실패 (%d쪽 %s): %s", page, reg.name, e)
        return RereadResult("blocked", reason=f"다시 읽지 못했습니다: {e}")
    r = got.get(reg.name)
    if r is None:
        return RereadResult("blocked", reason="응답에 이 칸의 결과가 없습니다", spent_usd=spent, equiv_usd=equiv)
    entry = {"model": model_id, "source": r["source"], "uncertain": bool(r.get("uncertain")),
             "note": r.get("note", ""), "at": time.strftime("%Y-%m-%d %H:%M:%S"),
             "cost_usd": round(spent, 6), "cost_equiv_usd": round(equiv, 6)}
    return entry, spent, equiv


def _update_cell(fd: Path, page: int, region: str, fn):
    """그 쪽의 추출 결과 파일들에서 칸 하나를 fn(cell)로 고쳐 저장한다 (이미 읽은 칸만). 마지막 fn의 반환값."""
    from app.extract import cell_rereads, model_readings, page_file

    with workspace.EXTRACT_LOCK:
        return _update_cell_locked(fd, page, region, fn)


def _update_cell_locked(fd: Path, page: int, region: str, fn):
    from app.extract import cell_rereads, model_readings, page_file

    base = fd / "extract"
    ret = None
    for f in (page_file(base, page), base / f"page-{page:03d}.json"):
        if not f.exists():
            continue
        rec = json.loads(f.read_text(encoding="utf-8"))
        cell = (rec.get("regions") or {}).get(region)
        if cell is None:
            continue
        # 1.12.0이 readings에 넣은 다시 읽기 결과(와 그 앞에 넣은 처음 결과)는 rereads로 옮긴다
        legacy = [x for x in cell.get("readings") or [] if x.get("reread")]
        if legacy:
            keep = model_readings(cell)
            cell["rereads"] = cell_rereads(cell)
            if keep:
                cell["readings"] = keep
            else:
                cell.pop("readings", None)
        ret = fn(cell)
        workspace._write(f, rec)  # 임시 파일로 쓰고 바꿔 넣기 (뒤에서 지우는 스레드가 읽는 중이면 다시 시도)
    return ret


ICON_SETTLED_NOTE = "아이콘 고친 뒤 보통 읽기·정밀 읽기가 같게 읽음"


def _icon_core(text, numbers: list | None = None, sets: bool = False) -> str:
    """지금 원문과 다시 읽은 결과를 견줄 글 (추출 비교처럼 공백·전각/반각 무시).
    아이콘 토큰은 이름과 상관없이 같은 표시 하나로 바꾼다: 모델은 이름을 모르고 [[?]]로 읽지만, 아이콘의 자리와 수는
    같아야 한다 (② 카드 보기에서 넣은 아이콘의 자리가 틀렸으면 확인 끝으로 보지 않는다).
    numbers: 칸의 icon_numbers — ②가 아이콘 그림에 넣어 원문에서 뺀 숫자를 그 자리에 되살려 견준다
    (다른 숫자는 그대로 견준다: 숫자를 잘못 읽은 것을 숨기지 않게)."""
    from app.extract import _agree_norm
    from app.icons import TOKEN_RE

    if isinstance(text, list):
        text = "\n".join(text)
    text = text or ""
    by_idx = {e["index"]: e for e in (numbers or []) if isinstance(e, dict) and isinstance(e.get("index"), int)}
    out, last = [], 0
    for ti, t in enumerate(TOKEN_RE.finditer(text)):
        out.append(text[last:t.start()])
        rep = MARK
        e = by_idx.get(ti)
        if e and e.get("text"):
            sep = e.get("sep", "")
            rep = f"{e['text']}{sep}{MARK}" if e.get("side") == "before" else f"{MARK}{sep}{e['text']}"
        out.append(rep)
        last = t.end()
    out.append(text[last:])
    # 문장 부호 ‘、’=‘，’·‘。’=‘．’ (B6). sets: ②에서 세트로 합친 칸 — 붙어 있는 아이콘 여럿을 하나로 본다
    # (세트 아이콘은 원문에 [[이름]] 하나지만 그림을 읽는 모델은 [[?]][[?]][[?]]로 읽는다: 공주와기사 5쪽 quatrefoil_x3)
    s = _agree_norm("".join(out))
    return re.sub(MARK + "+", MARK, s) if sets else s


MARK = "\x00"  # 견줄 때 아이콘 하나 (글자로 나올 일이 없는 표시)


def _settle_icon_cell(cell: dict, a: dict, b: dict) -> bool:
    """아이콘이 바뀐 칸을 보통 읽기(a)·정밀 읽기(b)로 읽은 결과를 칸에 적는다. 확인이 끝났으면 True.
    - 두 결과가 같고(추출의 두 번 읽기와 같은 비교) 둘 다 확실하고, 지금 원문과도 (아이콘 말고) 같으면: 확인 끝 —
      icon_changed·확인 필요를 지운다. 원문은 그대로 둔다 (② 원문 확인에서 정한 [[이름]]을 지키려고).
    - 아니면 ‘아이콘 고침 · 확인 필요’(icon_changed 남김)로 두고 ② 원문 확인에서 사람이 고른다. 원문은 그대로.
    두 결과는 readings에 둔다 (원문 확인이 두 번 읽은 칸처럼 나란히 보인다). 예전 readings·rereads는 아이콘을 고치기 전
    결과라 before_icon_reread에 기록만 남긴다."""
    from app.extract import _agree_norm, prefer_punct
    from app.icons import ICON_CHANGED_NOTE

    # 문장 부호(、/，·。/．)만 다르면 같게 읽은 것 (B6: 추출의 두 번 읽기와 같은 비교)
    same = _agree_norm(a["source"]) == _agree_norm(b["source"])
    sure = not a["uncertain"] and not b["uncertain"]
    sets = any("세트" in str(w) for w in (cell.get("icon_changed") or []))
    as_source = (_icon_core(b["source"], sets=sets)
                 == _icon_core(cell.get("source"), cell.get("icon_numbers"), sets=sets))
    # 두 읽기가 같으면 ‘확실하지 않음’이어도 확인 끝 (모델은 ‘、’와 ‘,’를 못 가린다는 정도로도 확실하지 않다고 한다:
    # 사용자 요청 — 두 읽기가 같으면 ‘아이콘 고침’ 표시만). 지금 원문과 아이콘 자리·수가 다르면 확인 필요 그대로
    settled = same and as_source
    prev = {k: cell[k] for k in ("readings", "rereads") if cell.get(k)}
    if prev:
        cell.setdefault("before_icon_reread", []).append({**prev, "at": b["at"]})
    cell.pop("rereads", None)
    cell["readings"] = [a, b]
    cell.pop("icon_reread", None)  # ② 원문 확인 뒤 다시 읽기를 마침
    notes = [s for s in (cell.get("note") or "").split(" / ") if s]
    if settled:
        cell.pop("icon_changed", None)
        # 확인 끝이지만 아이콘을 고친 칸이라는 알림은 남긴다: ‘아이콘 고침’ (보라·확인할 칸 아님, 용어집도 막지 않음)
        cell["icon_settled"] = True
        # 문장 부호만 맞선 곳은 ‘，’·‘．’ 쪽으로 (원문의 [[이름]]·줄바꿈은 그대로)
        cell["source"] = prefer_punct(prefer_punct(cell.get("source"), a["source"]), b["source"])
        cell["uncertain"] = False
        cell["note"] = " / ".join([s for s in notes if not s.startswith((ICON_CHANGED_NOTE, "아이콘 고친 뒤"))]
                                 + [ICON_SETTLED_NOTE])
        return True
    if not same:
        why = "두 모델이 다르게 읽음"
    elif not sure:
        why = "두 모델이 같게 읽었지만 확실하지 않다고 함"
    else:
        why = "두 모델이 같게 읽었지만 지금 원문과 다름"
    cell["uncertain"] = True
    cell.pop("icon_settled", None)
    cell["icon_changed"] = cell.get("icon_changed") or ["다시 읽어 확인 필요"]
    extra = " ".join(x for x in (a.get("note"), b.get("note")) if x)
    cell["note"] = " / ".join([s for s in notes if not s.startswith("아이콘 고친 뒤")]
                              + [f"아이콘 고친 뒤 {why}" + (f". {extra}" if extra else "")])
    return False


def resettle_icon_cells(file_dirs) -> int:
    """이미 두 번 읽어 ‘아이콘 고침 · 확인 필요’로 남은 칸을 지금 규칙으로 다시 견준다 (다시 읽지 않음, 사용료 없음).
    규칙이 바뀐 뒤(세트 아이콘·확실하지 않음) 예전에 남은 칸이 확인 끝이 되게. 확인 끝이 된 칸 수를 돌려준다."""
    import copy

    n = 0
    for fd in file_dirs:
        fd = Path(fd)
        pages = sorted({int(p.stem.split("-")[-1]) for p in (fd / "extract").rglob("page-*.json")
                        if p.stem.split("-")[-1].isdigit()})
        for page in pages:
            for region, cell in _page_cells(fd, page).items():
                rd = cell.get("readings") or []
                if (not cell.get("icon_changed") or cell.get("icon_reread") or len(rd) != 2
                        or not all(isinstance(x, dict) and "source" in x for x in rd)
                        or "아이콘 고친 뒤" not in (cell.get("note") or "")):
                    continue

                def resettle(c: dict, a=rd[0], b=rd[1]) -> bool:
                    trial = copy.deepcopy(c)
                    trial.pop("readings", None)  # 같은 두 결과를 before_icon_reread에 또 넣지 않게
                    trial.pop("rereads", None)
                    if not _settle_icon_cell(trial, dict(a, uncertain=bool(a.get("uncertain"))),
                                             dict(b, uncertain=bool(b.get("uncertain")))):
                        return False
                    trial["rereads"] = c.get("rereads") or []
                    if not trial["rereads"]:
                        trial.pop("rereads")
                    c.clear()
                    c.update(trial)
                    return True
                if _update_cell(fd, page, region, resettle):
                    n += 1
    return n


def _page_cells(fd: Path, page: int) -> dict:
    from app.extract import page_file

    base = fd / "extract"
    for f in (page_file(base, page), base / f"page-{page:03d}.json"):
        if f.exists():
            return (json.loads(f.read_text(encoding="utf-8")).get("regions") or {})
    return {}


# ---------------------------------------------------------------- 화면용: 아이콘 이름 확인 (2차)
ICON_HOW_LABEL = {"named": "확정됨", "same": "사전과 모양 같음", "legacy": "사전 그림과 비슷 (확인)",
                  "prev": "확정됨 · 다시 잡는 중"}


def icons_pending(project_dir: Path | str) -> int:
    """게임 원문(모든 프로젝트, 뺀 쪽 제외)에 남은 이름 없는 아이콘 [[?]] 수. ③ 용어집이 멈출지 본다."""
    from app import icons, workspace

    return icons.pending_count(workspace.load_project_info(project_dir))


def icon_review(project_dir: Path | str) -> dict | None:
    """find_icons 뒤 ② 원문 확인 표: 카드의 아이콘 하나 = 한 줄 (세트에 포함된 자리는 빼고).
    {"groups": [{"group", "occ", "image", "dict_image", "suggest"(이름 칸에 처음 보일 이름), "meaning", "ko", "scale",
    "named"(원문에 이미 있는 이름), "dict", "dict_how", "how_label", "shape", "shape_count"(같은 모양 줄 수), "count"(1),
    "card": (파일 폴더, 쪽), "card_label", "region", "line", "order", "examples", "where": [자리 하나], "not_icon",
    "absorbed_group", "absorbed_name", "reason", "first", "set_size", "issue"}], "known": [사전 이름]}.
    줄은 카드 순서(파일·쪽, 그 안에서는 원문 순서). 찾기 결과가 없으면 None.
    이름 칸: 확정된 이름 → 사전과 모양이 같은 이름 → 모델 제안. 그 이름이 사전에 있으면 뜻·한국어 이름·크기는 사전 값."""
    from app import icons, workspace

    info = workspace.load_project_info(project_dir)
    work = info.game_dir / "work" / "icons"
    if not (work / "scan.json").exists():
        return None
    res = icons.load_scan(work)
    dic = icons.load_dictionary(info)["icons"]
    by_id = {o.id: o for o in res.occurrences}
    order = {o.id: i for i, o in enumerate(res.occurrences)}
    set_extra: dict[str, int] = {}  # 세트 첫 자리 id -> 함께 붙은 아이콘 수 (자동으로 합친 것)
    for gi in res.groups.values():
        if gi.get("auto") and gi.get("absorbed"):
            set_extra[gi["absorbed"]] = set_extra.get(gi["absorbed"], 0) + 1
    shape_n: dict[str, int] = {}
    for gi in res.groups.values():
        if gi.get("shape") and not (gi.get("auto") and gi.get("absorbed")):
            shape_n[gi["shape"]] = shape_n.get(gi["shape"], 0) + 1
    multi_files = len({o.file for o in res.occurrences}) > 1
    groups = []
    for g, gi in res.groups.items():
        if gi.get("auto") and gi.get("absorbed"):  # 세트의 나머지 자리: 표에 따로 보이지 않는다 (확정하면 원문에서 뺀다)
            continue
        members = [o for o in res.occurrences if o.group == g]
        if not members:
            continue
        o = members[0]
        name = icons.prefill_name(gi)
        e = dic.get(name)
        de = dic.get(o.named or gi.get("dict") or "") or {}
        dimg = icons.icons_dir(info) / de.get("file", f"{o.named or gi.get('dict')}.png") if de else None
        fd = icons._occ_dir(info, o.file)
        card_label = (f"{o.file_key} " if multi_files else "") + f"{o.page}"
        groups.append({
            "group": g, "occ": o.id,
            "set_size": max((set_extra.get(x.id, 0) + 1 for x in members), default=1),
            "image": by_id[gi["rep"]].png if gi.get("rep") in by_id else None,
            "dict_image": str(dimg) if dimg is not None and dimg.exists() else None,
            "suggest": name,
            "meaning": (e.get("meaning", "") if e else gi.get("meaning", "")) or "",
            "ko": (e.get("ko", "") if e else gi.get("ko", "")) or "",
            "scale": float(e.get("scale", icons.DEFAULT_SCALE)) if e else icons.DEFAULT_SCALE,
            "named": o.named, "dict": gi.get("dict", ""), "dict_how": gi.get("dict_how", ""),
            "how_label": ICON_HOW_LABEL.get(gi.get("dict_how", ""), ""),
            "shape": gi.get("shape", ""), "shape_count": shape_n.get(gi.get("shape", ""), 1),
            "added": o.added,
            "count": len(members),
            "card": (str(fd), o.page), "card_label": card_label, "region": o.region, "line": o.line, "order": o.order,
            "examples": [f"{x.file_key} p{x.page} {x.region} {x.line}줄: {x.context}" for x in members],
            "where": [{"file_dir": icons._occ_dir(info, x.file), "page": x.page, "region": x.region, "line": x.line,
                       "png": x.png or None, "occ": x.id, "manual": x.manual, "named": x.named,
                       "label": f"{x.file_key} {x.page}쪽 {x.region} {x.line}줄"}
                      for x in members],
            "not_icon": bool(gi.get("not_icon")),  # 카드 보기에서 ‘이 자리는 아이콘 아님’으로 뺀 자리
            "absorbed_group": by_id[gi["absorbed"]].group if gi.get("absorbed") in by_id else "",
            "absorbed_name": gi.get("absorbed_name", ""),  # (예전) 세트 첫 자리가 사전 이름으로 바로 바뀐 경우
            "reason": o.reason if not gi.get("rep") else "",
            "first": min(order[x.id] for x in members)})
    for g in groups:  # 사람이 봐야 하는 것 (창에서 노랑): 위치 못 찾음·이름 빈 칸·한국어 이름 빈 칸 (아이콘 아님이면 없음)
        g["issue"] = "" if g["not_icon"] else (
            "위치 못 찾음" if not (g["image"] or g["named"]) else "이름 없음" if not g["suggest"].strip()
            else "한국어 이름 없음" if not g["ko"].strip() else "")
    # 카드 순서 (문제 카드를 위로 올리는 것은 화면이 한다: 사람이 적는 값에 따라 바뀌므로)
    groups.sort(key=lambda g: g["first"])
    return {"groups": groups, "known": sorted(dic)}


def icon_place(project_dir: Path | str, occ_id: str) -> dict:
    """카드 보기에서 아이콘 자리를 잡는 화면용: {"png": 영역 그림 PNG, "size": (w, h), "box": 지금 자리 | None,
    "guess": 예상 자리 | None, "marks": [{"box", "name" | "group", "occ"}]} (좌표는 그 그림의 px)."""
    import cv2

    from app import icons, workspace

    info = workspace.load_project_info(project_dir)
    work = info.game_dir / "work" / "icons"
    p = icons.place_info(info, work, occ_id)
    # 못 찾은 자리인데 바로 옆에 붙은 자리에 그림이 있으면: 그 상자가 이 자리까지 감쌌을 수 있다
    # (같은 줄에 붙은 자리만: 위아래 줄의 아이콘에 포함하자고 제안하지 않는다)
    p["run_host"] = icons.run_partners(info, work, occ_id)["boxed_line"][:1] if not p["box"] else []
    img = p.pop("image")
    ok, buf = cv2.imencode(".png", cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
    if not ok:
        raise OSError("영역 그림을 만들지 못함")
    try:
        card = _card_overview_png(info, work, occ_id)
    except Exception:  # noqa: BLE001 - 카드 전체 그림은 보조 (없어도 자리는 잡는다)
        card = b""
    return {**p, "png": buf.tobytes(), "size": (img.shape[1], img.shape[0]), "card_png": card}


CARD_OVERVIEW_DPI = 60


def card_regions(file_dir: Path | str, page: int, dpi: int = CARD_OVERVIEW_DPI) -> dict:
    """② 원문 확인 카드 보기의 카드 전체 그림과 영역 자리: {"png": bytes, "size": (w, h), "regions": {이름: (x0, y0, x1, y1)}}
    (좌표는 그 그림의 px). 영역은 그 쪽의 영역 (카드별 영역이 있으면 그것)."""
    import pymupdf

    from app.regions import ratio_to_pt

    proj = load_project(Path(file_dir))
    with pymupdf.open(proj.source_pdf) as doc:
        pg = doc[int(page) - 1]
        pm = pg.get_pixmap(matrix=pymupdf.Matrix(dpi / 72, dpi / 72), alpha=False)
        png, pr = pm.tobytes("png"), tuple(pg.rect)
    k = dpi / 72
    regs = {r.name: tuple(int(round((v - o) * k)) for v, o in zip(ratio_to_pt(r.rect, pr), (pr[0], pr[1], pr[0], pr[1])))
            for r in proj.template.regions_for(int(page))}
    return {"png": png, "size": (pm.width, pm.height), "regions": regs}


def _card_overview_png(info, work: Path, occ_id: str) -> bytes:
    """카드 보기 옆에 보일 카드 전체 그림 (PNG): 그 자리의 영역은 굵은 파랑, 그 카드의 다른 아이콘 영역은 옅은 회색 테두리."""
    import cv2
    import numpy as np
    import pymupdf

    from app import icons
    from app.regions import ratio_to_pt

    res = icons.load_scan(work)
    o = next(x for x in res.occurrences if x.id == occ_id)
    fd = icons._occ_dir(info, o.file)
    proj = load_project(fd)
    others = {x.region for x in res.occurrences if (x.file, x.page) == (o.file, o.page)}
    with pymupdf.open(proj.source_pdf) as doc:
        page = doc[o.page - 1]
        pm = page.get_pixmap(matrix=pymupdf.Matrix(CARD_OVERVIEW_DPI / 72, CARD_OVERVIEW_DPI / 72), alpha=False)
        img = np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, pm.n)[:, :, :3].copy()
        pr = tuple(page.rect)
    k = CARD_OVERVIEW_DPI / 72
    for reg in proj.template.regions_for(o.page):
        if reg.name != o.region and reg.name not in others:
            continue
        x0, y0, x1, y1 = (int(round((v - off) * k)) for v, off in zip(ratio_to_pt(reg.rect, pr), (pr[0], pr[1], pr[0], pr[1])))
        mine = reg.name == o.region
        cv2.rectangle(img, (x0, y0), (x1, y1), (31, 111, 235) if mine else (150, 150, 150), 3 if mine else 1)
    ok, buf = cv2.imencode(".png", cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
    return buf.tobytes() if ok else b""


@_locked
def add_icon_box(project_dir: Path | str, anchor_occ: str, box) -> dict:
    """카드 보기 ‘아이콘 추가’: 원문에 없던 아이콘을 그림에서 감싼 자리에 넣는다. 그 칸 원문에 [[?]]가 하나 늘고
    (그 줄에서 상자 가로 위치에 맞는 글자 사이 — icons.insert_at) 표에 새 줄이 생긴다.
    {"occ", "group", "line", "order", "dict"}. 상자가 너무 작으면 ValueError."""
    from app import icons, workspace

    info = workspace.load_project_info(project_dir)
    work = info.game_dir / "work" / "icons"
    r = icons.add_icon(info, work, anchor_occ, tuple(box))
    log.info("아이콘 추가: %s 칸에 %s (%d줄 %d번째)", anchor_occ, r.get("occ"), r.get("line", 0), r.get("order", 0))
    try:
        icons.export_review(info, icons.load_scan(work), info.game_dir / "아이콘_검증.xlsx")
    except OSError:
        pass
    return r


VERTICAL_COVER_MIN = 0.8  # 위아래 줄의 찾은 자리는 잡은 상자가 이만큼 감쌀 때만 ‘한 세트로’ 후보


def region_icon_image(file_dir: Path | str, page: int, region: str) -> bytes:
    """아이콘 자리를 잡는 영역 그림 PNG (icon_place의 png와 같은 좌표: 그 위에서 감싼 상자를 add_icon_box_region에 넘긴다)."""
    import cv2
    import pymupdf

    from app import icons

    fd = Path(file_dir)
    proj = load_project(fd)
    reg = next(r for r in proj.template.regions_for(int(page)) if r.name == region)
    with pymupdf.open(proj.source_pdf) as doc:
        img = icons.region_image(doc[int(page) - 1], reg)
    ok, buf = cv2.imencode(".png", cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
    if not ok:
        raise OSError("영역 그림을 만들지 못함")
    return buf.tobytes()


@_locked
def add_icon_box_region(project_dir: Path | str, file_dir: Path | str, page: int, region: str, box) -> dict:
    """② 원문 확인 카드 보기 ‘아이콘 추가’ — 아이콘이 하나도 없는 영역에 (add_icon_box는 같은 칸의 자리가 있어야 함).
    {"occ", "group", "line", "order", "dict"}. 상자가 너무 작거나 ‘글자만’ 영역이면 ValueError."""
    from app import icons, workspace

    info = workspace.load_project_info(project_dir)
    work = info.game_dir / "work" / "icons"
    r = icons.add_icon_in_region(info, work, Path(file_dir), int(page), region, tuple(box))
    log.info("아이콘 추가 (빈 영역): %s %s쪽 %s에 %s", file_dir, page, region, r.get("occ"))
    return r


@_locked
def set_icon_box(project_dir: Path | str, occ_id: str, box) -> dict:
    """사람이 잡은 자리로 아이콘 그림을 다시 자르고 묶음을 다시 정한다 (검증 엑셀도 새로 만든다).
    {"group", "old", "old_gone", "new_group", "dict"}. 자리가 너무 작으면 ValueError."""
    from app import icons, workspace

    info = workspace.load_project_info(project_dir)
    work = info.game_dir / "work" / "icons"
    r = icons.set_manual_box(info, work, occ_id, tuple(box))
    log.info("자리 저장: %s %s → %s", occ_id, list(box), r.get("group"))
    # 바로 옆에 붙은 [[?]] 중 합칠 후보: 못 찾은 자리, 그리고 찾은 자리 중 이 상자와 겹친 것 (세트를 한 상자로 잡음).
    # 겹친 넓이가 작은 쪽 상자의 OVERLAP_MIN 이상이면 후보 (‘겹친 아이콘 흡수’와 같은 기준. 가운데 점으로 보면
    # 상자를 몇 px 작게 그렸을 때 옆 아이콘이 빠졌다)
    part = icons.run_partners(info, work, occ_id)
    boxes = {o.id: o.box for o in icons.load_scan(work).occurrences}
    # 다른 줄(위아래)의 자리는 사람이 상자로 거의 다 감쌌을 때만 후보 (자동으로 합치지 않는다: 화면이 ‘한 세트로’를
    # 묻고, 위아래가 섞이면 기본 답은 ‘따로 있는 아이콘’ — run_vertical)
    covered = [i for i in part["boxed"] if boxes.get(i)
               and icons._overlap_ratio(r["box"], boxes[i]) >= (
                   VERTICAL_COVER_MIN if i in part["other_line"] else icons.OVERLAP_MIN)]
    r["run_unfound"] = part["unfound"] + covered
    r["run_vertical"] = any(i in part["other_line"] for i in r["run_unfound"])
    try:
        icons.export_review(info, icons.load_scan(work), info.game_dir / "아이콘_검증.xlsx")
    except OSError:  # 엑셀이 열려 있으면 못 쓴다: 화면 확정에는 필요 없다
        pass
    return r


@_locked
def absorb_icon_run(project_dir: Path | str, host_occ: str, occ_ids: list[str]) -> dict:
    """붙어 있는 [[?]] 자리들을 host 자리의 아이콘 하나에 포함한다 (상자 하나로 문양 여러 개를 감싼 경우).
    그 자리들은 ‘아이콘 아님’으로 옮겨 확정할 때 원문에서 빠진다. {"groups": [옮긴 묶음]}"""
    from app import icons, workspace

    info = workspace.load_project_info(project_dir)
    work = info.game_dir / "work" / "icons"
    groups = icons.absorb_into(info, work, host_occ, list(occ_ids))
    log.info("합치기: %s ← %s", host_occ, list(occ_ids))
    try:
        icons.export_review(info, icons.load_scan(work), info.game_dir / "아이콘_검증.xlsx")
    except OSError:
        pass
    return {"groups": groups}


def overlapping_icons(marks: list[dict], box, exclude: str = "", row: int | None = None) -> list[str]:
    """카드 보기 표시(icon_place의 marks) 중 box와 겹친 다른 [[?]] 자리 id (흡수 후보). row: 지금 자리의 원문 줄
    (주면 다른 줄의 자리는 빼고 — 위아래 아이콘은 흡수 후보가 아니다)."""
    from app import icons

    return icons.overlapping_occ(marks, box, exclude, row)


@_locked
def split_icon_box(project_dir: Path | str, occ_id: str, part_box) -> dict:
    """한 자리 상자에 아이콘이 둘이면 둘로: part_box(그중 하나)와 나머지. 원문에 [[?]]가 하나 늘어난다.
    {"first", "second", "groups"}. 나눌 수 없으면 ValueError."""
    from app import icons, workspace

    info = workspace.load_project_info(project_dir)
    work = info.game_dir / "work" / "icons"
    r = icons.split_icon(info, work, occ_id, part_box)
    log.info("둘로 나누기: %s → %s", occ_id, r.get("second"))
    try:
        icons.export_review(info, icons.load_scan(work), info.game_dir / "아이콘_검증.xlsx")
    except OSError:
        pass
    return r


@_locked
def set_icon_not_icon(project_dir: Path | str, occ_id: str) -> dict:
    """한 자리만 ‘아이콘 아님’: 그 자리를 ‘아이콘 아님’으로 표시한 혼자인 묶음으로 옮긴다 (검증 엑셀도 새로).
    {"group", "old", "old_gone", "new_group"}. 그 줄의 ‘뜻’ 칸에 글자를 적으면 [[?]]를 그 글자로, 비우면 뺀다."""
    from app import icons, workspace

    info = workspace.load_project_info(project_dir)
    work = info.game_dir / "work" / "icons"
    r = icons.set_not_icon(info, work, occ_id)
    log.info("아이콘 아님: %s", occ_id)
    try:
        icons.export_review(info, icons.load_scan(work), info.game_dir / "아이콘_검증.xlsx")
    except OSError:
        pass
    return r


@_locked
def reopen_icon(project_dir: Path | str, anchor_occ: str, ti: int) -> dict:
    """카드 보기: 이름을 정한 아이콘(그 칸 ti번째 토큰)을 다시 고칠 수 있게 [[?]] 자리로 연다. {"occ", "group"}."""
    from app import icons, workspace

    info = workspace.load_project_info(project_dir)
    work = info.game_dir / "work" / "icons"
    r = icons.reopen_icon(info, work, anchor_occ, int(ti))
    log.info("확정 아이콘 다시 열기: %s 칸 %d번째 → %s", anchor_occ, int(ti), r.get("occ"))
    try:
        icons.export_review(info, icons.load_scan(work), info.game_dir / "아이콘_검증.xlsx")
    except OSError:
        pass
    return r


def icon_box_problem(project_dir: Path | str, occ_id: str, box) -> str:
    """사람이 잡으려는 상자가 아이콘 하나로 맞지 않으면 그 이유 (글자 두 줄 높이인데 글자와 섞임), 맞으면 ""."""
    from app import icons, workspace

    info = workspace.load_project_info(project_dir)
    work = info.game_dir / "work" / "icons"
    o = next(x for x in icons.load_scan(work).occurrences if x.id == occ_id)
    _fd, img, text = icons._occ_region(info, o)
    return icons.tall_box_problem(img, text, box)


@_locked
def keep_cell_original(project_dir: Path | str, occ_id: str, keep: bool = True, whole: bool = False) -> dict:
    """카드 보기 ‘이 칸 원본 그대로’: 그 자리가 있는 칸을 한글화하지 않는다.
    whole=True(카드 보기 ‘이 칸 전체 원본 그대로’): 영역 설정과 상관없이 칸 전체 (set_keep_original).
    whole=False(예전 동작): 영역이 ‘한 칸 모두 같은 글꼴’이면 칸 전체, ‘문단마다 다른 글꼴’이면 아이콘이 있는 문단 모두만
    원본 그대로 두고(set_keep_paragraphs, extract.keep_paragraph_lines) 다른 문단은 그대로 한글화한다. 아이콘 문단이
    칸의 글 줄 전부면 칸 전체와 같다 (문단마다 아이콘이 있으면 칸 전체가 되어 버리므로 카드 보기는 whole=True 또는
    문장 하나만 고르는 keep_sentence_original을 쓴다).
    켜면 그 칸의 [[?]] 자리들은 이름이 필요 없으므로 ‘아이콘 아님’으로 한다 (그 묶음에 by_keep 표시. 확정하면 [[?]]는
    원문에서 빠지지만 줄바꿈은 남아 남길 줄 번호가 맞는다).
    끄면 그렇게 ‘아이콘 아님’이 된 자리만 다시 아이콘 자리로 되돌린다 (사람이 따로 지운 자리는 그대로).
    {"file_dir", "page", "region", "keep", "mode": "whole" | "partial" | "" (끔), "paragraphs": 남긴 원문 줄 수(partial),
     "not_icon": [자리 id], "restored": [자리 id], "left": 끈 뒤에도 남은 그 칸의 ‘아이콘 아님’ 자리 (옆 아이콘에 포함은 뺌)}"""
    from app import icons, workspace
    from app.extract import breaks_for_text, keep_paragraph_lines, load_extracted

    info = workspace.load_project_info(project_dir)
    work = info.game_dir / "work" / "icons"
    res = icons.load_scan(work)
    o = next(x for x in res.occurrences if x.id == occ_id)
    fd = icons._occ_dir(info, o.file)
    from app.service._translate_erase import set_keep_original, set_keep_paragraphs
    mode, kept_lines = ("whole" if keep else ""), []
    cell0 = load_extracted(fd).get(o.page, {}).get(o.region) or {}
    src = cell0.get("source")
    if keep and not whole and region_keep_mode(fd, o.page, o.region) == "sentence" and isinstance(src, str):
        kept_lines = keep_paragraph_lines(src, breaks_for_text(cell0, src))  # ②에서 정한 문단 나눔
        rows = src.split("\n")
        if kept_lines and any(t.strip() and i not in kept_lines for i, t in enumerate(rows)):
            mode = "partial"  # 아이콘 문단 밖에 글 줄이 남는다: 그 줄만 한글화
    if mode == "partial":
        set_keep_paragraphs(fd, o.page, o.region, kept_lines, _keep_boxes(res, o, fd, src, kept_lines))
    else:
        set_keep_original(fd, o.page, o.region, keep)
    done, restored = [], []
    # 이미 이름이 있는 아이콘(named)은 그대로 둔다 (원본 그대로여도 원문의 [[이름]]은 남는다: 예전과 같게)
    cell = [x for x in res.occurrences if (x.file, x.page, x.region) == (o.file, o.page, o.region) and not x.set_of
            and not x.named]
    if keep:
        done = _mark_keep_slots(info, work, res, cell)
    left = []
    if not keep:
        restored, left = _restore_keep_slots(info, work, o.file, o.page, o.region)
    log.info("원본 그대로 %s: %s %d쪽 %s (아이콘 아님 %s, 되돌림 %s, 남은 아이콘 아님 %s)",
             {"whole": "켬 (칸 전체)", "partial": f"켬 (아이콘 문단만, 원문 줄 {kept_lines})"}.get(mode, "끔"),
             o.file_key, o.page, o.region, done, restored, left)
    if done or restored:
        try:
            icons.export_review(info, icons.load_scan(work), info.game_dir / "아이콘_검증.xlsx")
        except OSError:
            pass
    return {"file_dir": fd, "page": o.page, "region": o.region, "keep": keep, "mode": mode,
            "paragraphs": len(kept_lines) if mode == "partial" else 0, "not_icon": done, "restored": restored,
            "left": left}


def region_keep_mode(file_dir: Path | str, page: int, region: str) -> str:
    """그 쪽 그 영역의 글꼴 단위 "cell" | "sentence" (카드별 영역에 설정이 있으면 그것, 없으면 기본 영역 설정)."""
    from app import font_plan

    fd = Path(file_dir)
    try:
        reg = next((r for r in load_project(fd).template.regions_for(int(page)) if r.name == region), None)
    except Exception:  # noqa: BLE001
        reg = None
    mode = (getattr(reg, "style", None) or {}).get("font_mode") if reg is not None else None
    return mode or font_plan.region_font_modes(fd).get(region) or "cell"


def _keep_boxes(res, o, fd: Path, src: str, kept_lines: list[int]) -> list:
    """남길 줄에 있는 그 칸 아이콘 자리(원본 쪽 pt): 지우기가 남길 줄을 찾고 아이콘을 지키는 데 쓴다 (확정하면 아이콘 찾기
    결과가 지워지므로 칸에 적어 둔다). 이미 ‘아이콘 아님’·옆 아이콘에 포함으로 정한 자리는 넣지 않는다 (글 줄의 잘못 찾은
    자리일 수 있다: 넣으면 지우기가 그 줄을 남길 줄로 세고 일본어 글자를 지키지 못하게 된다)."""
    import pymupdf

    from app import icons

    proj = load_project(fd)
    reg = next((r for r in proj.template.regions_for(o.page) if r.name == o.region), None)
    if reg is None:
        return []
    # 자리의 원문 줄 번호: 지금 원문의 index번째 [[?]]가 있는 줄 (scan의 line은 찾을 때의 원문 기준)
    ph_rows = [src[:m.start()].count("\n") for m in icons.TOKEN_RE.finditer(src) if m.group(1) == "?"]

    def row_of(x) -> int:
        return ph_rows[x.index] if 0 <= x.index < len(ph_rows) else x.line - 1

    def keeps(x) -> bool:
        gi = res.groups.get(x.group, {})
        # 원본 그대로 때문에 아이콘 아님이 된 자리(by_keep: 앞서 남긴 문장의 아이콘)는 넣는다 — set_keep_paragraphs가
        # boxes_pt를 통째로 바꾸므로 빼면 문장을 더하거나 뺄 때 앞 문장의 아이콘 자리를 잃는다
        return not gi.get("absorbed") and (not gi.get("not_icon") or bool(gi.get("by_keep")))

    with pymupdf.open(proj.source_pdf) as doc:
        prect = doc[o.page - 1].rect
    return [b for x in res.occurrences if (x.file, x.page, x.region) == (o.file, o.page, o.region)
            and not x.named and not x.set_of and row_of(x) in kept_lines and keeps(x)
            for b in [icons.region_box_to_pt(prect, reg, x.box)] if b]


def _mark_keep_slots(info, work: Path, res, slots: list) -> list[str]:
    """원본 그대로로 둘 [[?]] 자리들을 ‘아이콘 아님’(by_keep: 끄면 되돌림)으로. 이미 아이콘 아님·포함인 자리는 그대로."""
    from app import icons

    done = []
    for x in slots:
        gi = res.groups.get(x.group, {})
        if not gi.get("not_icon") and not gi.get("absorbed"):
            g = icons.set_not_icon(info, work, x.id)["group"]
            cur = icons.load_scan(work)
            cur.groups[g]["by_keep"] = True  # 원본 그대로 때문에 아이콘 아님 (끄면 되돌린다)
            icons._save_scan(work, cur)
            done.append(x.id)
    return done


def sentence_units(source: str | None, breaks: list[int] | None = None) -> list[list[int]]:
    """원문을 ‘문장(문단)’ 단위 줄 묶음으로: [[원문 줄 번호, …], …] (source.split("\n") 기준, 빈 줄은 없음).
    extract.keep_paragraph_lines와 같은 규칙 (typeset_layout.paragraph_spans, breaks = 칸의 문단 나눔,
    아이콘·숫자·기호만 있는 줄은 혼자 한 문단)."""
    from app.erase_text import is_icon_line
    from app.typeset_layout import paragraph_spans

    src = source or ""
    rows, pos = [], 0
    for ln in src.split("\n"):
        rows.append((pos, pos + len(ln)))
        pos += len(ln) + 1
    units: list[list[int]] = []
    for a, b in paragraph_spans(src, breaks):
        cur: list[int] = []
        for i, (la, lb) in enumerate(rows):
            if max(a, la) >= min(b, lb):
                continue
            if is_icon_line(src[la:lb]):
                if cur:
                    units.append(cur)
                    cur = []
                units.append([i])
            else:
                cur.append(i)
        if cur:
            units.append(cur)
    return units


def _sentence_rows(src: str, tis) -> set[int]:
    """그 칸 원문에서 토큰 순번 tis가 있는 원문 줄 번호."""
    from app import icons

    toks = list(icons.TOKEN_RE.finditer(src))
    return {src[:toks[int(t)].start()].count("\n") for t in tis if 0 <= int(t) < len(toks)}


@_locked
def keep_sentence_original(project_dir: Path | str, anchor_occ: str, tis: list[int], keep: bool = True) -> dict:
    """카드 보기 ‘이 문장만 원본 그대로’: 그 칸에서 고른 아이콘(그 칸 토큰 순번 tis)이 든 문장(문단)만 원본 그대로 둔다
    (지우기·번역·한글 넣기 안 함, set_keep_paragraphs). 다른 문장은 그대로 한글화한다. 이미 남긴 문장에 더한다.
    keep=False면 그 문장만 다시 한글화한다 (남은 문장이 없으면 칸의 원본 그대로를 끈다). 그 문장의 [[?]] 자리는
    ‘아이콘 아님’(by_keep)으로 하고, 다시 한글화하면 되돌린다. anchor_occ = 같은 칸의 아무 자리.
    {"file_dir", "page", "region", "keep", "mode": "partial" | "whole" | "", "lines": 남긴 원문 줄, "changed": 이번에 바뀐 줄,
     "not_icon": [자리 id], "restored": [자리 id]}. 고른 토큰이 원문에 없으면 ValueError."""
    from app import icons, workspace
    from app.extract import keep_state, load_extracted
    from app.service._translate_erase import set_keep_original, set_keep_paragraphs

    info = workspace.load_project_info(project_dir)
    work = info.game_dir / "work" / "icons"
    res = icons.load_scan(work)
    o = next(x for x in res.occurrences if x.id == anchor_occ)
    fd = icons._occ_dir(info, o.file)
    r = load_extracted(fd).get(o.page, {}).get(o.region) or {}
    src = r.get("source")
    if not isinstance(src, str):
        raise ValueError("이 칸의 원문을 찾지 못했습니다")
    picked = _sentence_rows(src, tis)
    if not picked:
        raise ValueError("고른 아이콘을 원문에서 찾지 못했습니다 (원문이 바뀌었으면 ② 원문 확인을 다시 누르세요)")
    from app.extract import breaks_for_text

    changed = sorted({i for u in sentence_units(src, breaks_for_text(r, src)) if picked & set(u) for i in u})
    mode0, lines0 = keep_state(r)
    rows = src.split("\n")
    kept = {i for i, t in enumerate(rows) if t.strip()} if mode0 in ("whole", "broken") else set(lines0)
    kept = (kept | set(changed)) if keep else (kept - set(changed))
    same = [x for x in res.occurrences if (x.file, x.page, x.region) == (o.file, o.page, o.region)
            and not x.set_of and not x.named]
    ph_rows = [src[:t.start()].count("\n") for t in icons.TOKEN_RE.finditer(src) if t.group(1) == "?"]

    def row_of(x) -> int:
        return ph_rows[x.index] if 0 <= x.index < len(ph_rows) else x.line - 1

    done, restored = [], []
    if not kept:
        set_keep_original(fd, o.page, o.region, False)
        mode = ""
    else:
        set_keep_paragraphs(fd, o.page, o.region, sorted(kept), _keep_boxes(res, o, fd, src, sorted(kept)))
        mode = keep_state(load_extracted(fd).get(o.page, {}).get(o.region) or {})[0]
    if keep:
        done = _mark_keep_slots(info, work, res, [x for x in same if row_of(x) in changed])
    else:
        ids = {x.id: row_of(x) for x in same}
        cur = icons.load_scan(work)
        for x in [x for x in cur.occurrences if x.id in ids and ids[x.id] in changed]:
            if cur.groups.get(x.group, {}).get("by_keep"):
                icons.restore_icon(info, work, x.id)
                restored.append(x.id)
                cur = icons.load_scan(work)
    log.info("문장만 원본 그대로 %s: %s %d쪽 %s 원문 줄 %s (남긴 줄 %s, 아이콘 아님 %s, 되돌림 %s)",
             "켬" if keep else "끔", o.file_key, o.page, o.region, changed, sorted(kept), done, restored)
    if done or restored:
        try:
            icons.export_review(info, icons.load_scan(work), info.game_dir / "아이콘_검증.xlsx")
        except OSError:
            pass
    return {"file_dir": fd, "page": o.page, "region": o.region, "keep": keep, "mode": mode, "lines": sorted(kept),
            "changed": changed, "not_icon": done, "restored": restored}


def sentence_kept(project_dir: Path | str, anchor_occ: str, tis: list[int]) -> bool | None:
    """고른 아이콘들이 든 문장이 모두 원본 그대로인가 (칸 전체 원본 그대로면 True). 고를 것이 없으면 None."""
    from app import icons, workspace
    from app.extract import keep_state, load_extracted

    info = workspace.load_project_info(project_dir)
    o = next(x for x in icons.load_scan(info.game_dir / "work" / "icons").occurrences if x.id == anchor_occ)
    r = load_extracted(icons._occ_dir(info, o.file)).get(o.page, {}).get(o.region) or {}
    src = r.get("source")
    if not isinstance(src, str):
        return None
    picked = _sentence_rows(src, tis)
    if not picked:
        return None
    mode, lines = keep_state(r)
    if mode in ("whole", "broken"):
        return True
    return picked <= set(lines)


def _restore_keep_slots(info, work: Path, file: str, page: int, region: str) -> tuple[list[str], list[str]]:
    """원본 그대로 때문에 ‘아이콘 아님’이 된(by_keep) 그 칸의 자리를 다시 아이콘 자리로.
    (되돌린 자리 id, 되돌린 뒤에도 남은 그 칸의 ‘아이콘 아님’ 자리 id (옆 아이콘에 포함은 뺌))."""
    from app import icons

    restored = []
    cur = icons.load_scan(work)
    for x in [x for x in cur.occurrences if (x.file, x.page, x.region) == (file, page, region) and not x.set_of]:
        if cur.groups.get(x.group, {}).get("by_keep"):
            icons.restore_icon(info, work, x.id)
            restored.append(x.id)
            cur = icons.load_scan(work)
    left = [x.id for x in cur.occurrences if (x.file, x.page, x.region) == (file, page, region)
            and not x.set_of and cur.groups.get(x.group, {}).get("not_icon")
            and not cur.groups.get(x.group, {}).get("absorbed")]
    return restored, left


@_locked
def set_cell_keep_original(file_dir: Path | str, page: int, region: str, keep: bool) -> dict:
    """결과 보기·원문 확인의 ‘원본 그대로’ 단추: 켜면 칸 전체를 원본 그대로 (set_keep_original). 끄면 칸 전체든 아이콘
    문단만이든 풀고, 원본 그대로 때문에 ‘아이콘 아님’이 된 그 칸의 자리도 다시 아이콘 자리로 되돌린다
    (카드 보기에서 끈 것과 같게). {"restored": [자리 id], "left": [남은 ‘아이콘 아님’ 자리 id]}."""
    from app import icons
    from app.service._translate_erase import set_keep_original

    fd = Path(file_dir)
    set_keep_original(fd, page, region, keep)
    restored, left = [], []
    if keep:
        return {"restored": restored, "left": left}
    info = workspace.project_of(fd)
    work = info.game_dir / "work" / "icons" if info is not None else None
    if work is None or not (work / "scan.json").exists():
        return {"restored": restored, "left": left}
    target = fd.resolve()
    files = {x.file for x in icons.load_scan(work).occurrences
             if (x.page, x.region) == (int(page), region) and icons._occ_dir(info, x.file).resolve() == target}
    for f in sorted(files):
        r_, l_ = _restore_keep_slots(info, work, f, int(page), region)
        restored += r_
        left += l_
    if restored:
        log.info("원본 그대로 끔: %s %d쪽 %s (되돌림 %s)", fd.name, int(page), region, restored)
        try:
            icons.export_review(info, icons.load_scan(work), info.game_dir / "아이콘_검증.xlsx")
        except OSError:
            pass
    return {"restored": restored, "left": left}


def has_text_besides_icons(source: str | None) -> bool:
    """원문에 아이콘·숫자·기호 말고 글자도 있는가 (‘원본 그대로’ 칸이면 글자도 번역되지 않고 남는다)."""
    from app.erase_text import is_icon_line

    src = (source or "").strip()
    return bool(src) and not is_icon_line(src)


def cell_kept(project_dir: Path | str, occ_id: str) -> bool:
    """그 자리가 있는 칸이 ‘원본 그대로’인가 (칸 전체든 아이콘 문단만이든)."""
    return bool(cell_keep_mode(project_dir, occ_id))


def cell_keep_mode(project_dir: Path | str, occ_id: str) -> str:
    """그 자리가 있는 칸의 ‘원본 그대로’: "" | "whole" 칸 전체 | "partial" 아이콘 문단만 |
    "broken" 아이콘 문단만으로 정했는데 원문 줄 수가 바뀌어 칸 전체를 원본 그대로 둠 (extract.keep_state)."""
    from app import icons, workspace
    from app.extract import keep_state, load_extracted

    info = workspace.load_project_info(project_dir)
    o = next(x for x in icons.load_scan(info.game_dir / "work" / "icons").occurrences if x.id == occ_id)
    r = load_extracted(icons._occ_dir(info, o.file)).get(o.page, {}).get(o.region) or {}
    return keep_state(r)[0]


@_locked
def restore_icon(project_dir: Path | str, occ_id: str) -> dict:
    """카드 보기: ‘아이콘 아님’·옆 아이콘에 포함으로 정한 자리를 다시 아이콘 자리로 (검증 엑셀도 새로). {"group"}.
    되돌릴 것이 없으면 ValueError."""
    from app import icons, workspace

    info = workspace.load_project_info(project_dir)
    work = info.game_dir / "work" / "icons"
    r = icons.restore_icon(info, work, occ_id)
    log.info("되돌리기: %s → %s", occ_id, r.get("group"))
    try:
        icons.export_review(info, icons.load_scan(work), info.game_dir / "아이콘_검증.xlsx")
    except OSError:
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
            eng = engine or _settings.get_settings().engine
            client = ClaudeCodeClient() if eng == "claude-code" else make_client()
        icons.suggest_names(one, client, _extract.ICON_SUGGEST_MODEL, work / "suggest")
    except Exception as e:  # noqa: BLE001 - 제안 없이도 직접 적으면 된다
        return {"suggest": "", "meaning": "", "ko": "", "note": f"이름 제안을 받지 못함: {getattr(e, 'reason', e)}"}
    res = icons.load_scan(work)  # 그사이 바뀌었을 수 있어 다시 읽고 이 줄만 고친다
    how = ""
    if group in res.groups:
        for k in ("suggest", "meaning", "ko"):
            if one.groups[group].get(k):
                res.groups[group][k] = one.groups[group][k]
        how = icons.resolve_suggested_name(info, res, group)  # 이미 있는 이름과 겹치면 같은 아이콘 / 다른 이름
        icons._save_scan(work, res)
    g = res.groups.get(group) or one.groups[group]
    return {"suggest": g.get("suggest", ""), "meaning": g.get("meaning", ""), "ko": g.get("ko", ""),
            "dict": g.get("dict", ""), "name_clash": how}


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


@_locked
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


@_locked
def confirm_icons(project_dir: Path | str, rows: list[dict], partial: bool = False) -> _extract.IconImportResult:
    """화면에서 정한 아이콘 이름을 사전에 넣고 원문을 바꾼다.
    rows: [{"group", "name", "meaning", "ko", "scale", "not_icon"}].
    partial: 넘긴 줄만 확정하고 찾기 결과를 남긴다 (② 원문 확인의 영역 확정, icons_review.import_rows)."""
    from app import icons, workspace

    info = workspace.load_project_info(project_dir)
    work = info.game_dir / "work" / "icons"
    if not (work / "scan.json").exists():
        return _extract.IconImportResult("blocked", blocking=["아이콘 찾기 결과가 없습니다"])
    xl = [{"묶음": r["group"], "이름": r.get("name", ""), "뜻(원문)": r.get("meaning", ""),
           "한국어 이름": r.get("ko", ""), "처리": icons.NOT_ICON if r.get("not_icon") else "",
           "글자": r.get("text", ""),
           # keep_dict: 사전에 있는 아이콘과 같다고 고른 줄 — 사전의 한국어 이름·뜻·크기를 덮어쓰지 않는다
           "표시 크기": "" if r.get("keep_dict") else (r.get("scale") or icons.DEFAULT_SCALE)} for r in rows]
    r = icons.import_rows(info, xl, work, partial=partial)
    if r.blocking:
        return _extract.IconImportResult("blocked", blocking=r.blocking)
    return _extract.IconImportResult("confirmed", r.added, r.reused, r.replaced, r.removed,
                            changed_cells=[{"file_dir": fd, "page": pg, "region": rg} for fd, pg, rg in r.changed_cells],
                            notes=r.notes)
