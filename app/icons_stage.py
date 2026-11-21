"""② 원문 확인(아이콘 정의 + 원문 확인 한 창)의 아이콘 찾기 결과(scan) 관리.

예전에는 ② 원문 확인을 누를 때마다 게임 전체를 다시 찾고, 이름을 확정하면 찾기 결과를 지웠다. 한 창에서 영역마다
확정하려면 찾기 결과가 남아 있어야 한다 (다른 영역에서 손으로 잡은 자리·적어 둔 이름이 사라지지 않게). 그래서:

- 찾기 결과를 지우지 않는다. 영역을 확정하면 icons_review.import_rows(partial=True)가 그 자리만 이름 있는 자리로 고친다.
- 원문의 아이콘 토큰이 찾기 결과와 달라진 칸(다시 추출·사람이 고친 원문 등)만 그 칸만 다시 찾아 합친다
  (stale_regions → rescan_regions). 다른 칸의 자리·줄 번호는 그대로다.
- 아직 확정하지 않은 이름·한국어 이름·아이콘 아님은 그 줄(groups)에 "staged"로 적어 둔다 (확정 전).
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from app import icons as _ic


def region_sig(src: str) -> tuple:
    """칸 원문의 아이콘 토큰 지문: ([[?]] 수, 이름 있는 토큰 이름들(정렬))."""
    names = _ic.TOKEN_RE.findall(src or "")
    return (sum(1 for n in names if n == "?"), tuple(sorted(n for n in names if n != "?")))


def scan_sigs(res) -> dict[tuple, tuple]:
    """찾기 결과가 아는 칸마다의 토큰 지문 (자리마다 줄이 하나씩 있으므로 자리로 센다)."""
    out: dict[tuple, list] = {}
    for o in res.occurrences:
        e = out.setdefault((o.file, o.page, o.region), [0, []])
        if o.named:
            e[1].append(o.named)
        else:
            e[0] += 1
    return {k: (n, tuple(sorted(names))) for k, (n, names) in out.items()}


def current_sigs(info) -> dict[tuple, tuple]:
    """지금 원문에서 찾기 대상인 칸마다의 토큰 지문 (scan이 찾는 칸과 같은 규칙: 뺀 쪽·글자만 영역·지운 영역 제외)."""
    from app.extract import load_extracted
    from app.project import load_project
    from app.workspace import data_root, game_file_dirs, included_pages

    root = data_root(info)
    out: dict[tuple, tuple] = {}
    for fd in game_file_dirs(info):
        inc = set(included_pages(fd))
        skip = _ic.text_only_cells(fd)
        tpl = None
        for pno, regs in load_extracted(fd).items():
            if pno not in inc:
                continue
            for name, r in regs.items():
                src = r.get("source") if isinstance(r, dict) else None
                if not isinstance(src, str) or not _ic.TOKEN_RE.search(src) or skip(pno, name):
                    continue
                if tpl is None:
                    tpl = load_project(fd).template
                if name not in {x.name for x in tpl.regions_for(pno)}:
                    continue
                out[(str(fd.relative_to(root)), pno, name)] = region_sig(src)
    return out


def stale_regions(info, res) -> set[tuple]:
    """찾기 결과와 원문의 아이콘 토큰이 다른 칸 {(파일 상대 경로, 쪽, 영역)}."""
    have, cur = scan_sigs(res), current_sigs(info)
    return {k for k in have.keys() | cur.keys() if have.get(k) != cur.get(k)}


def _max_num(values, pattern: str) -> int:
    rx = re.compile(pattern)
    return max((int(m.group(1)) for v in values if (m := rx.match(v))), default=0)


def rescan_regions(info, work: Path, keys: set[tuple]):
    """그 칸들만 다시 찾아 찾기 결과에 합친다. 그 칸의 예전 자리·줄은 버리고, 다른 칸은 그대로 둔다.
    새 자리·줄 번호는 이어서 매기고, 모양은 이미 있는 줄과 같으면 그 모양 값을 쓴다. 돌려줌: 합친 ScanResult."""
    work = Path(work)
    tmp = work.parent / "icons_rescan"
    res = _ic.load_scan(work) if (work / "scan.json").exists() else _ic.ScanResult()
    try:
        tres = _ic.scan(info, tmp, only=set(keys))
        n = _max_num([o.id for o in res.occurrences], r"^o(\d+)$")  # 지울 자리 포함 (지운 번호를 다시 쓰지 않게)
        gmax = _max_num(list(res.groups), r"^g(\d+)$")
        res.occurrences = [o for o in res.occurrences if (o.file, o.page, o.region) not in keys]
        alive = {o.group for o in res.occurrences}
        res.groups = {g: gi for g, gi in res.groups.items() if g in alive}
        idmap: dict[str, str] = {}
        for o in tres.occurrences:
            n += 1
            idmap[o.id] = f"o{n:03d}"
        (work / "crops").mkdir(parents=True, exist_ok=True)
        for o in tres.occurrences:
            o.id = idmap[o.id]
            if o.png:
                dst = work / "crops" / f"{o.id}.png"
                shutil.copy2(o.png, dst)
                o.png = str(dst)
            if o.set_of:
                o.set_of = idmap.get(o.set_of, o.set_of)
        by_id = {o.id: o for o in tres.occurrences}
        gmap: dict[str, str] = {}
        shape_map: dict[str, str] = {}
        for g, gi in tres.groups.items():
            gi = dict(gi)
            for k in ("rep", "absorbed"):
                if gi.get(k):
                    gi[k] = idmap.get(gi[k], gi[k])
            if re.match(r"^g\d+$", g):
                gmax += 1
                ng = f"g{gmax:02d}"
            else:  # ‘세트 o001’·‘위치 못 찾음 o001’
                prefix, _sp, oid = g.rpartition(" ")
                ng = f"{prefix} {idmap.get(oid, oid)}" if prefix else g
            sg = gi.get("shape")
            if sg:
                if sg.startswith("s-"):
                    gi["shape"] = f"s-{idmap.get(sg[2:], sg[2:])}"
                else:
                    if sg not in shape_map:
                        rep = by_id.get(gi.get("rep") or "")
                        shape_map[sg] = (_ic.shape_of_row(res, rep, _ic.shape_pair(_ic._cells.load_png(Path(rep.png))))
                                         if rep is not None and rep.png else f"s-{ng}")
                    gi["shape"] = shape_map[sg]
            gmap[g] = ng
            res.groups[ng] = gi
        for o in tres.occurrences:
            o.group = gmap.get(o.group, o.group)
        res.occurrences.extend(tres.occurrences)
        res.occurrences.sort(key=lambda o: (o.file, o.page))  # 카드 순서 (같은 쪽 안의 순서는 그대로)
        work.mkdir(parents=True, exist_ok=True)
        _ic._save_scan(work, res)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return res


def stage(res, group: str, values: dict | None) -> None:
    """줄의 확정 전 값 적기 (values None이면 지움). 키: name, ko, meaning, scale, not_icon, text."""
    gi = res.groups.get(group)
    if gi is None:
        return
    if values is None:
        gi.pop("staged", None)
        return
    st = dict(gi.get("staged") or {})
    st.update({k: v for k, v in values.items() if k in ("name", "ko", "meaning", "scale", "not_icon", "text")})
    gi["staged"] = st
