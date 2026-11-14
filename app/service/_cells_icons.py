"""③ 원문 확인 칸(읽은 결과·다시 읽기)과 ② 아이콘 정의 창의 동작."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from app import workspace
from app.project import load_project
from app.service import _base
from app.service import _extract
from app.service import _settings


# ---------------------------------------------------------------- 화면용: 원문 확인 (2차)
def extracted_cells(file_dir: Path | str) -> list[dict]:
    """파일의 추출 결과를 칸마다: {"page", "region", "kind", "source", "label", "uncertain", "note",
    "readings", "edited"}. 뺀 쪽은 빠지고, 아직 읽지 않은 쪽은 source=None.
    readings = 화면의 읽은 결과 표: 두 번 읽기 결과, 그 뒤에 마지막 ‘이 칸 다시 읽기’ 결과(reread=True) 하나.
    다시 읽기 결과만 있으면 지금 원문을 처음 줄(original=True)로 넣어 비교할 수 있게 한다."""
    from app import workspace
    from app.extract import MODEL_LABEL, cell_rereads, load_extracted, model_readings, verification_label

    def row(x: dict, **flags) -> dict:
        return {"model": MODEL_LABEL.get(x["model"], x["model"]), "source": x["source"],
                "reread": False, "original": False, **flags}

    fd = Path(file_dir)
    proj = load_project(fd)
    ext = load_extracted(fd)
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
                "keep_original": bool(r and r.get("keep_original")),  # 전체가 아이콘 — 번역 안 함
                "icon_changed": list((r or {}).get("icon_changed") or []),  # ② 아이콘 정의에서 자리가 바뀜
                # ② 아이콘 정의에서 ‘아이콘 아님’으로 뺀 [[?]] 수 (None = 그 기록 전에 정한 칸·정한 적 없음)
                "icon_removed": None if (r or {}).get("icon_removed") is None else int(r["icon_removed"]),
            })
    return out


def icon_reread_pending(file_dirs) -> list[dict]:
    """② 아이콘 정의에서 아이콘 자리가 바뀌었는데 아직 다시 읽지 않은 칸 [{"file_dir", "page", "region"}].
    ‘나중에 (원문 확인에서)’를 고르면 ③ 원문 확인을 열 때 이 칸들을 먼저 다시 읽는다."""
    from app import workspace
    from app.extract import load_extracted

    out = []
    for fd in map(Path, file_dirs):
        inc = set(workspace.included_pages(fd))
        tpl = load_project(fd).template
        for pno, regs in sorted(load_extracted(fd).items()):
            if pno not in inc:
                continue
            names = {reg.name for reg in tpl.regions_for(pno)}  # 영역을 지운 칸은 다시 읽을 수 없다
            out += [{"file_dir": fd, "page": pno, "region": name} for name, r in regs.items()
                    if r and r.get("icon_reread") and r.get("icon_changed") and name in names
                    and not r.get("keep_original")]
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
        r.pop("icon_changed", None)  # 아이콘 정의 뒤 확인할 칸: 사람이 보고 저장했으니 풀린다
        r.pop("icon_reread", None)
        if old == source:  # 고치지 않고 저장 = 사람이 보고 맞다고 확인함
            r["checked_by"] = "사람"
            if not r.get("edited_by"):
                r["note"] = "사람이 확인 (읽은 그대로)"
        else:
            r["source"] = source
            # 아이콘에 넣으며 뺀 숫자의 기록(몇 번째 [[…]], 그 옆 숫자)은 예전 원문 기준이다. 새 원문에 두면
            # ‘다시 정하기’가 숫자를 엉뚱한 자리에 되살리므로 버린다 (숫자는 사람이 고친 원문에 있는 그대로)
            r.pop("icon_numbers", None)
            r.pop("icon_number_note", None)
            r["edited_by"] = "사람"
            r["note"] = (f"사람이 고침 (모델: {json.dumps(old, ensure_ascii=False)})" if old is not None
                         else "사람이 입력")
        workspace._write(f, rec)  # 임시 파일로 쓰고 바꿔 넣기 (뒤에서 지우는 스레드가 읽는 중이면 다시 시도)


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


CELL_BLOCKED = ("다시 읽지 못했습니다", "응답에 이 칸의 결과가 없습니다")  # 로그인·한도가 아닌, 그 칸만의 실패


def reread_cells(cells: list[dict], *, model: str = "opus", engine: str | None = None, client=None,
                 progress: _base.ProgressFn | None = None, cancel: _base.CancelToken | None = None) -> dict:
    """여러 칸을 차례로 다시 읽는다 (② 아이콘 정의에서 아이콘 자리가 바뀐 칸 등). cells: [{"file_dir", "page", "region"}].
    결과는 칸마다 rereads에 덧붙고 원문은 그대로다 (③ 원문 확인에서 사람이 비교해 고른다).
    {"done": 읽은 수, "failed": [(칸, 이유)], "state": done|paused|blocked, "reason", "retry_after", "spent_usd"}.
    한도에 걸리면 거기서 멈춘다 (남은 칸은 ‘확인 필요’로 남아 원문 확인에서 하나씩 다시 읽을 수 있다)."""
    progress = progress or _base._noop
    out = {"done": 0, "failed": [], "state": "done", "reason": "", "retry_after": None, "spent_usd": 0.0}
    for i, c in enumerate(cells):
        if cancel:
            cancel.check()
        progress(_base.ProgressEvent("reread", i, len(cells), f"{c['page']}쪽 {c['region']} 다시 읽는 중"))
        r = reread_region(c["file_dir"], c["page"], c["region"], model=model, engine=engine, client=client)
        out["spent_usd"] += r.spent_usd
        if r.state == "done":
            out["done"] += 1
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
                  engine: str | None = None, client=None) -> RereadResult:
    """원문 확인 화면에서 고른 칸 하나만 다시 읽는다. 결과는 그 칸의 rereads에 덧붙이고, 원문(source)·
    readings(두 번 읽기 결과)·확인 필요는 바꾸지 않는다 — 칸 상태는 사람이 보고 저장할 때 바뀐다.
    시험 모드·뺀 쪽이어도 읽는다(사람이 고른 칸).
    한도·로그인 문제는 state="paused"·"blocked"로 돌려준다 (예외 아님)."""
    import pymupdf

    from app.extract import (Paused, _iso_after, _read_regions_once, cell_rereads, make_client, model_readings,
                             page_file, resolve_model)
    from app.extract_cc import CardError, ClaudeCodeClient, NotAvailable

    log = logging.getLogger("cardtranslater.extract")
    fd = Path(file_dir)
    try:
        model_id = resolve_model(model)
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
    base = fd / "extract"
    try:
        # 잘라 낸 그림은 extract/reread/에 둔다 (extract/crops/의 그 쪽 첫 칸 그림을 덮어쓰지 않게)
        with pymupdf.open(proj.source_pdf) as src_doc:
            got, spent, equiv = _read_regions_once(client, proj, page, src_doc, model_id, base / "reread", [reg])
    except Paused as p:
        log.warning("칸 다시 읽기 일시 정지 (%d쪽 %s): %s", page, region, p.reason)
        return RereadResult("paused", reason=p.reason, retry_after=_iso_after(p.retry_after_s))
    except NotAvailable as e:
        return RereadResult("blocked", reason=str(e))
    except CardError as e:
        log.warning("칸 다시 읽기 실패 (%d쪽 %s): %s", page, region, e)
        return RereadResult("blocked", reason=f"다시 읽지 못했습니다: {e}")
    r = got.get(region)
    if r is None:
        return RereadResult("blocked", reason="응답에 이 칸의 결과가 없습니다", spent_usd=spent, equiv_usd=equiv)
    entry = {"model": model_id, "source": r["source"], "uncertain": bool(r.get("uncertain")),
             "note": r.get("note", ""), "at": time.strftime("%Y-%m-%d %H:%M:%S"),
             "cost_usd": round(spent, 6), "cost_equiv_usd": round(equiv, 6)}
    # 이미 읽은 칸에만 덧붙인다 (아직 읽지 않은 칸은 편집 칸에 넣어 주고, 저장할 때 원문이 생긴다)
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
        cell.setdefault("rereads", []).append(entry)
        cell.pop("icon_reread", None)  # ② 아이콘 정의 뒤 다시 읽기를 마침
        workspace._write(f, rec)  # 임시 파일로 쓰고 바꿔 넣기 (뒤에서 지우는 스레드가 읽는 중이면 다시 시도)
    log.info("칸 다시 읽음: %d쪽 %s (%s)", page, region, model_id)
    return RereadResult("done", r["source"], bool(r.get("uncertain")), r.get("note", ""), spent, equiv)


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
    set_extra: dict[str, int] = {}  # 세트 첫 자리 id -> 함께 붙은 아이콘 수 (자동으로 합친 것)
    for gi in res.groups.values():
        if gi.get("auto") and gi.get("absorbed"):
            set_extra[gi["absorbed"]] = set_extra.get(gi["absorbed"], 0) + 1
    for g, gi in res.groups.items():
        if gi.get("auto") and gi.get("absorbed"):  # 세트의 나머지 자리: 표에 따로 보이지 않는다 (확정하면 원문에서 뺀다)
            continue
        members = [o for o in res.occurrences if o.group == g]
        groups.append({"group": g,
                       "set_size": max((set_extra.get(o.id, 0) + 1 for o in members), default=1), "image": by_id[gi["rep"]].png if gi.get("rep") else None,
                       "suggest": gi.get("suggest", ""), "meaning": gi.get("meaning", ""), "ko": gi.get("ko", ""),
                       "count": len(members),
                       "examples": [f"{o.file_key} p{o.page} {o.region} {o.line}줄: {o.context}" for o in members],
                       "where": [{"file_dir": icons._occ_dir(info, o.file), "page": o.page, "region": o.region, "line": o.line,
                                  "png": o.png or None, "occ": o.id, "manual": o.manual,
                                  "label": f"{o.file_key} {o.page}쪽 {o.region} {o.line}줄"}
                                 for o in members],
                       "dict": gi.get("dict", ""),
                       "not_icon": bool(gi.get("not_icon")),  # 카드 보기에서 ‘이 자리는 아이콘 아님’으로 뺀 자리
                       "absorbed_group": by_id[gi["absorbed"]].group if gi.get("absorbed") in by_id else "",
                       "absorbed_name": gi.get("absorbed_name", ""),  # 세트 첫 자리가 사전 이름으로 바로 바뀐 경우
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
    work = info.game_dir / "work" / "icons"
    p = icons.place_info(info, work, occ_id)
    # 못 찾은 자리인데 바로 옆에 붙은 자리에 그림이 있으면: 그 상자가 이 자리까지 감쌌을 수 있다
    p["run_host"] = icons.run_partners(info, work, occ_id)["boxed"][:1] if not p["box"] else []
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
    # 바로 옆에 붙은 [[?]] 중 합칠 후보: 못 찾은 자리, 그리고 찾은 자리 중 이 상자와 겹친 것 (세트를 한 상자로 잡음).
    # 겹친 넓이가 작은 쪽 상자의 OVERLAP_MIN 이상이면 후보 (‘겹친 아이콘 흡수’와 같은 기준. 가운데 점으로 보면
    # 상자를 몇 px 작게 그렸을 때 옆 아이콘이 빠졌다)
    part = icons.run_partners(info, work, occ_id)
    boxes = {o.id: o.box for o in icons.load_scan(work).occurrences}
    covered = [i for i in part["boxed"] if boxes.get(i)
               and icons._overlap_ratio(r["box"], boxes[i]) >= icons.OVERLAP_MIN]
    r["run_unfound"] = part["unfound"] + covered
    try:
        icons.export_review(info, icons.load_scan(work), info.game_dir / "아이콘_검증.xlsx")
    except OSError:  # 엑셀이 열려 있으면 못 쓴다: 화면 확정에는 필요 없다
        pass
    return r


def absorb_icon_run(project_dir: Path | str, host_occ: str, occ_ids: list[str]) -> dict:
    """붙어 있는 [[?]] 자리들을 host 자리의 아이콘 하나에 포함한다 (상자 하나로 문양 여러 개를 감싼 경우).
    그 자리들은 ‘아이콘 아님’으로 옮겨 확정할 때 원문에서 빠진다. {"groups": [옮긴 묶음]}"""
    from app import icons, workspace

    info = workspace.load_project_info(project_dir)
    work = info.game_dir / "work" / "icons"
    groups = icons.absorb_into(info, work, host_occ, list(occ_ids))
    try:
        icons.export_review(info, icons.load_scan(work), info.game_dir / "아이콘_검증.xlsx")
    except OSError:
        pass
    return {"groups": groups}


def overlapping_icons(marks: list[dict], box, exclude: str = "") -> list[str]:
    """카드 보기 표시(icon_place의 marks) 중 box와 겹친 다른 [[?]] 자리 id (흡수 후보)."""
    from app import icons

    return icons.overlapping_occ(marks, box, exclude)


def split_icon_box(project_dir: Path | str, occ_id: str, part_box) -> dict:
    """한 자리 상자에 아이콘이 둘이면 둘로: part_box(그중 하나)와 나머지. 원문에 [[?]]가 하나 늘어난다.
    {"first", "second", "groups"}. 나눌 수 없으면 ValueError."""
    from app import icons, workspace

    info = workspace.load_project_info(project_dir)
    work = info.game_dir / "work" / "icons"
    r = icons.split_icon(info, work, occ_id, part_box)
    try:
        icons.export_review(info, icons.load_scan(work), info.game_dir / "아이콘_검증.xlsx")
    except OSError:
        pass
    return r


def set_icon_not_icon(project_dir: Path | str, occ_id: str) -> dict:
    """한 자리만 ‘아이콘 아님’: 그 자리를 ‘아이콘 아님’으로 표시한 혼자인 묶음으로 옮긴다 (검증 엑셀도 새로).
    {"group", "old", "old_gone", "new_group"}. 그 줄의 ‘뜻’ 칸에 글자를 적으면 [[?]]를 그 글자로, 비우면 뺀다."""
    from app import icons, workspace

    info = workspace.load_project_info(project_dir)
    work = info.game_dir / "work" / "icons"
    r = icons.set_not_icon(info, work, occ_id)
    try:
        icons.export_review(info, icons.load_scan(work), info.game_dir / "아이콘_검증.xlsx")
    except OSError:
        pass
    return r


def reopen_icon(project_dir: Path | str, anchor_occ: str, ti: int) -> dict:
    """카드 보기: 이름을 정한 아이콘(그 칸 ti번째 토큰)을 다시 고칠 수 있게 [[?]] 자리로 연다. {"occ", "group"}."""
    from app import icons, workspace

    info = workspace.load_project_info(project_dir)
    work = info.game_dir / "work" / "icons"
    r = icons.reopen_icon(info, work, anchor_occ, int(ti))
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


def keep_cell_original(project_dir: Path | str, occ_id: str, keep: bool = True) -> dict:
    """카드 보기 ‘이 칸 원본 그대로’: 그 자리가 있는 칸을 한글화하지 않는다 (set_keep_original).
    켜면 그 칸의 [[?]] 자리들은 이름이 필요 없으므로 ‘아이콘 아님’으로 한다 (되돌리기로 풀 수 있다).
    {"file_dir", "page", "region", "keep", "not_icon": [자리 id]}"""
    from app import icons, workspace

    info = workspace.load_project_info(project_dir)
    work = info.game_dir / "work" / "icons"
    res = icons.load_scan(work)
    o = next(x for x in res.occurrences if x.id == occ_id)
    fd = icons._occ_dir(info, o.file)
    from app.service._translate_erase import set_keep_original
    set_keep_original(fd, o.page, o.region, keep)
    done = []
    if keep:
        for x in res.occurrences:
            gi = res.groups.get(x.group, {})
            if (x.file, x.page, x.region) == (o.file, o.page, o.region) and not x.set_of \
                    and not gi.get("not_icon") and not gi.get("absorbed"):
                icons.set_not_icon(info, work, x.id)
                done.append(x.id)
        try:
            icons.export_review(info, icons.load_scan(work), info.game_dir / "아이콘_검증.xlsx")
        except OSError:
            pass
    return {"file_dir": fd, "page": o.page, "region": o.region, "keep": keep, "not_icon": done}


def cell_kept(project_dir: Path | str, occ_id: str) -> bool:
    """그 자리가 있는 칸이 ‘원본 그대로’인가."""
    from app import icons, workspace
    from app.extract import load_extracted

    info = workspace.load_project_info(project_dir)
    o = next(x for x in icons.load_scan(info.game_dir / "work" / "icons").occurrences if x.id == occ_id)
    r = load_extracted(icons._occ_dir(info, o.file)).get(o.page, {}).get(o.region) or {}
    return bool(r.get("keep_original"))


def restore_icon(project_dir: Path | str, occ_id: str) -> dict:
    """카드 보기: ‘아이콘 아님’·옆 아이콘에 포함으로 정한 자리를 다시 아이콘 자리로 (검증 엑셀도 새로). {"group"}.
    되돌릴 것이 없으면 ValueError."""
    from app import icons, workspace

    info = workspace.load_project_info(project_dir)
    work = info.game_dir / "work" / "icons"
    r = icons.restore_icon(info, work, occ_id)
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


def confirm_icons(project_dir: Path | str, rows: list[dict]) -> _extract.IconImportResult:
    """화면에서 정한 아이콘 이름을 사전에 넣고 원문을 바꾼다.
    rows: [{"group", "name", "meaning", "ko", "scale", "not_icon"}]."""
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
    r = icons.import_rows(info, xl, work)
    if r.blocking:
        return _extract.IconImportResult("blocked", blocking=r.blocking)
    return _extract.IconImportResult("confirmed", r.added, r.reused, r.replaced, r.removed,
                            changed_cells=[{"file_dir": fd, "page": pg, "region": rg} for fd, pg, rg in r.changed_cells])
