"""영역 템플릿 저장·검사와 그룹 정렬(맞추기)."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from app.service import _base
from app.service import _overview


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
    out = _validate_region_set(data)
    for key, ps in (data.get("page_regions") or {}).items():  # 공통 영역 그룹의 ‘이 카드만 따로 영역’
        if ps.get("regions") or ps.get("protect_rects"):
            out += [f"{key}: {m}" for m in _validate_region_set(
                {"regions": ps.get("regions", []), "protect_rects": ps.get("protect_rects", [])})
                if m != "영역이 하나도 없습니다"]
    return out


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
        if r.get("kind", "text") not in _overview.REGION_KINDS:
            problems.append(f"{r.get('name')}: 모르는 종류 {r.get('kind')}")
        st = r.get("style") or {}
        if int(st.get("rotate", 0) or 0) not in (0, 180):
            problems.append(f"{r.get('name')}: 회전은 0 또는 180만")
        if st.get("align", "left") not in _overview.ALIGNS:
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


# ---------------------------------------------------------------- 자동 정렬 (PRD 5.8, F21)
@dataclass
class AlignResult:
    state: str  # "done" | "blocked"
    reason: str = ""
    reference: dict = field(default_factory=dict)  # {"file": 파일 폴더 이름, "page": 쪽}
    files: list[dict] = field(default_factory=list)  # [{"file", "dir", "ok": [쪽], "check": [쪽], "manual": [쪽]}]

    @property
    def ok_count(self) -> int:
        return sum(len(f["ok"]) for f in self.files)

    @property
    def check_count(self) -> int:
        return sum(len(f["check"]) for f in self.files)


def _group_files(file_dir: Path) -> list[Path]:
    """같은 그룹의 파일 폴더 (폴더 이름 순: 화면의 파일 순서와 같다). 그룹 밖 폴더면 자기 자신만."""
    fd = Path(file_dir)
    files = fd.parent
    if files.name != "files" or not (files.parent / "group.json").exists():
        return [fd]
    return sorted(p for p in files.iterdir() if p.is_dir())


def _align_reference(file_dirs: list[Path], data: dict) -> tuple[Path, int]:
    """정렬 기준 카드: 그룹 첫 파일의 reference_page (뺀 쪽이면 그 파일에서 처리할 첫 쪽)."""
    from app import workspace

    fd = Path(file_dirs[0])
    pages = workspace.included_pages(fd, limited=False) or list(range(1, workspace.page_count(fd) + 1))
    ref = int(data.get("reference_page", 1) or 1)
    return fd, (ref if ref in pages else pages[0])


def align_group(file_dirs: list, progress: _base.ProgressFn | None = None,
                cancel: _base.CancelToken | None = None) -> AlignResult:
    """그룹(파일 폴더들)의 처리할 카드를 기준 카드에 맞춰 각 파일 폴더의 align.json에 쓴다.
    카드별 그룹·‘이 카드만 따로 영역’ 카드는 맞추지 않는다. 손으로 맞춘 카드(manual)는 그대로 둔다
    (기준 카드가 바뀌었으면 모두 새로). 이미 읽은(추출한) 카드의 원문은 다시 읽지 않는다."""
    from app import align, workspace

    progress = progress or _base._noop
    fds = [Path(f) for f in file_dirs]
    if not fds:
        return AlignResult("blocked", "파일이 없습니다")
    gdir = fds[0].parent.parent
    data = load_region_template(gdir)
    if data.get("layout") == "per_card":
        return AlignResult("blocked", "카드마다 영역을 정하는 그룹은 자동 정렬하지 않습니다")
    if not data.get("regions"):
        return AlignResult("blocked", "공통 영역이 없습니다. 먼저 영역을 그리고 저장하세요")
    # 기준 파일은 폴더 이름 순 첫 파일 (nudge_alignment의 _group_files와 같은 순서: 부른 쪽 순서와 무관하게)
    ref_fd, ref_page = _align_reference(sorted(fds), data)
    reference = {"file": ref_fd.name, "page": ref_page}
    own = set((data.get("page_regions") or {}).keys())
    targets, old = [], {}
    for fd in fds:
        prev = align.load(fd)
        old[fd] = (prev.get("pages") or {}) if prev.get("reference") == reference else {}
        pages = [p for p in workspace.included_pages(fd, limited=False)
                 if f"{fd.name}/{p}" not in own and (old[fd].get(str(p)) or {}).get("status") != align.STATUS_MANUAL]
        if fd == ref_fd and ref_page not in pages:
            pages = [ref_page] + pages
        targets.append((sorted((fd / "source").glob("*.pdf"))[0], pages))

    def on_page(i, n, _pdf, p):
        progress(_base.ProgressEvent("align", i, n, f"카드 맞추는 중 {i}/{n}", page=p))

    cancel_check = cancel.check if cancel is not None else None
    found = align.align_pages(sorted((ref_fd / "source").glob("*.pdf"))[0], ref_page, targets,
                              on_page=on_page, check_cancel=cancel_check)
    out = AlignResult("done", reference=reference)
    for fd, (pdf, pages) in zip(fds, targets):
        entries = {k: v for k, v in old[fd].items() if v.get("status") == align.STATUS_MANUAL
                   and f"{fd.name}/{k}" not in own}
        for p in pages:
            entries[str(p)] = found[(str(pdf), p)]
        align.save(fd, {"reference": reference, "pages": entries})
        out.files.append({"file": workspace.file_key(fd), "dir": str(fd),
                          "ok": sorted(int(k) for k, v in entries.items() if v.get("status") == align.STATUS_OK),
                          "check": sorted(int(k) for k, v in entries.items() if v.get("status") == align.STATUS_CHECK),
                          "manual": sorted(int(k) for k, v in entries.items()
                                           if v.get("status") == align.STATUS_MANUAL)})
    progress(_base.ProgressEvent("done", 1, 1, f"맞춘 카드 {out.ok_count}장, 정렬 확인 필요 {out.check_count}장"))
    return out


def align_info(file_dir: Path | str) -> dict:
    """파일의 자동 정렬 상태 {"reference": {...} | None, "pages": {쪽: {dx, dy, sx, sy, rot, score, status, applied}}}.
    applied: 영역에 적용하는 정렬인가 (점수가 아주 낮은 ‘정렬 확인 필요’는 적용하지 않는다)."""
    from app import align

    data = align.load(file_dir)
    pages = {int(k): {**v, "applied": align.applied(v)} for k, v in (data.get("pages") or {}).items()
             if str(k).isdigit() and isinstance(v, dict)}
    return {"reference": data.get("reference"), "pages": pages}


def nudge_alignment(file_dir: Path | str, page: int, ddx: float, ddy: float) -> dict | None:
    """그 카드의 정렬만 손으로 옮긴다 (비율 좌표 ddx·ddy, 상태 manual). 새 정렬 값. 기준 카드는 옮기지 않는다(None)."""
    from app import align

    fd = Path(file_dir)
    data = align.load(fd)
    pages = data.setdefault("pages", {})
    cur = pages.get(str(int(page)))
    if data.get("reference") is None:
        ref_fd, ref_page = _align_reference(_group_files(fd), load_region_template(fd.parent.parent))
        data["reference"] = {"file": ref_fd.name, "page": ref_page}
    ref = data["reference"]
    if (cur or {}).get("status") == align.STATUS_REF or (ref.get("file") == fd.name and int(ref.get("page", 0)) == int(page)):
        return None
    cur = dict(cur or {**align.IDENTITY, "score": 0.0})
    if cur.get("status") != align.STATUS_MANUAL:
        cur["auto"] = {k: cur.get(k) for k in ("dx", "dy", "sx", "sy", "rot", "score", "status", "method") if k in cur}
        if not align.applied(cur):  # 적용하지 않던 정렬(믿을 수 없음)이면 화면에 보이던 자리(변환 없음)에서 시작
            cur.update({k: align.IDENTITY[k] for k in ("dx", "dy", "sx", "sy")})
    cur["dx"] = round(float(cur.get("dx", 0.0)) + ddx, 5)
    cur["dy"] = round(float(cur.get("dy", 0.0)) + ddy, 5)
    cur["status"] = align.STATUS_MANUAL
    pages[str(int(page))] = cur
    align.save(fd, data)
    return cur


def reset_alignment(file_dir: Path | str, page: int | None = None) -> dict | None:
    """정렬 되돌리기. page를 주면 그 카드만: 손으로 맞춘 카드는 자동 정렬 값으로, 자동이면 정렬 없음으로.
    page가 없으면 이 파일의 정렬을 모두 지운다. 그 카드의 새 정렬 값 (없으면 None)."""
    from app import align

    fd = Path(file_dir)
    data = align.load(fd)
    if page is None:
        align.save(fd, {})
        return None
    pages = data.get("pages") or {}
    cur = pages.get(str(int(page)))
    if not cur or cur.get("status") == align.STATUS_REF:
        return cur
    if cur.get("status") == align.STATUS_MANUAL and cur.get("auto"):
        pages[str(int(page))] = cur = dict(cur["auto"])
    else:
        pages.pop(str(int(page)), None)
        cur = None
    if not any(v.get("status") != align.STATUS_REF for v in pages.values()):
        pages = {}
    data["pages"] = pages
    align.save(fd, data)
    return cur


def align_rect(rect: dict, t: dict | None, inverse: bool = False) -> dict:
    """화면용: 비율 rect {x, y, w, h}에 정렬 변환을 적용(inverse=True면 거꾸로)한 rect. 자르지 않는다."""
    from app.regions import RatioRect, align_rect as _fwd, unalign_rect as _inv

    if not t:
        return dict(rect)
    r = RatioRect(float(rect["x"]), float(rect["y"]), float(rect["w"]), float(rect["h"]))
    o = _inv(r, t) if inverse else _fwd(r, t, clip=False)
    return {"x": o.x, "y": o.y, "w": o.w, "h": o.h}
