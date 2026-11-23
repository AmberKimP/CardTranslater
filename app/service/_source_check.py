"""② 원문 확인 (1.27까지의 ‘아이콘 정의’와 ‘원문 확인’ 두 단계를 한 창으로).

한 줄 = 카드의 영역 하나, 카드마다 묶는다. 영역의 상태(고칠 것·볼 것·확인 안 함·확인됨·원본 그대로)는 여기서만 정한다
(목록 창·카드 보기가 같은 색을 보이게). 아이콘 이름·아이콘 아님은 ‘확정 전’으로 찾기 결과의 줄에 적어 두었다가
영역을 확정할 때 원문에 넣는다 (icons_review.import_rows partial). 확정한 영역 중 아이콘 자리가 바뀌었고 아이콘이 모두
끝난(남은 [[?]] 없음) 영역은 다시 읽을 칸으로 돌려준다 (화면이 바로 뒤에서 다시 읽는다).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from app.service import _cells_icons, _extract, _settings

log = logging.getLogger("cardtranslater.icons")

# 영역 상태 (나쁜 것부터). reading(읽는 중)은 화면이 다시 읽기 대기·진행 중인 영역에 덧씌운다
STATUS_ORDER = ("fix", "check", "reading", "todo", "ok", "keep")
STATUS_LABEL = {"fix": "고칠 것", "check": "볼 것", "reading": "읽는 중", "todo": "확인 안 함", "ok": "확인됨",
                "keep": "원본 그대로"}
_OK_LABELS = {"두 번 확인", "사람이 확인", "사람이 고침", "아이콘 고침"}


def _norm(p) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def _scan(project_dir):
    from app import icons, workspace

    info = workspace.load_project_info(project_dir)
    work = info.game_dir / "work" / "icons"
    res = icons.load_scan(work) if (work / "scan.json").exists() else None
    return info, work, res


def ensure_icon_scan(project_dir: Path | str, *, engine: str | None = None, client=None, suggest: bool = True,
                     full_if_missing: bool = True) -> dict:
    """창을 열 때: 찾기 결과가 없으면 게임 전체를 찾고(이름 제안 포함), 있으면 원문과 달라진 칸만 다시 찾는다.
    손으로 잡은 상자가 옆 숫자를 감쌌는지도 (한 번) 본다 (icons.recheck_boxed_numbers).
    돌려줌 {"state": "none"|"ok"|"scanned"|"synced", "regions": 다시 찾은 칸 수, "note"}. 오래 걸릴 수 있다 (작업 스레드)."""
    from app import icons, icons_stage, workspace

    info, work, res = _scan(project_dir)
    if not icons.token_count(info):
        return {"state": "none", "regions": 0, "note": ""}
    if res is None and not full_if_missing:
        return {"state": "ok", "regions": 0, "note": ""}
    if res is None:
        eng = engine or _settings.get_settings().engine
        r = _extract.find_icons(project_dir, engine=eng, client=client, suggest=suggest)
        if getattr(r, "state", "") not in ("done", "icons_review"):
            return {"state": "failed", "regions": 0, "note": getattr(r, "reason", "") or getattr(r, "note", "") or ""}
        return {"state": "scanned", "regions": 0, "note": getattr(r, "note", "") or ""}
    stale = icons_stage.stale_regions(info, res)
    if stale:
        res = icons_stage.rescan_regions(info, work, stale)
    # 다시 찾은 칸의 새 줄도 처음 찾기처럼 이름 제안을 받는다 (칸만 다시 찾을 때는 묻지 않아, 사전에 없는 아이콘은
    # 이름 칸이 비어 있었다: 다시 읽은 3쪽의 ‘5+상자’). 한 번도 묻지 않은 줄만 묻는다
    try:  # 숫자만 다른 사전 아이콘이 있는 줄: 숫자만 바꾼 이름을 먼저 채운다 (Claude 안 부름, 채운 줄은 묻지 않는다)
        _fill_variants(info, work, res)
    except Exception:  # noqa: BLE001 - 못 채워도 창은 연다
        log.warning("숫자만 다른 아이콘 이름 채우기 실패", exc_info=True)
    note = _suggest_unasked(info, work, res, engine, client) if suggest else ""
    try:  # 손으로 잡은 상자가 감싼 옆 숫자: 예전에 확정한 원문에도 남아 있으면 뺀다 (자리마다 한 번)
        with workspace.EXTRACT_LOCK:
            nfix = icons.recheck_boxed_numbers(info, work)
    except Exception:  # noqa: BLE001 - 검사를 못 해도 창은 연다
        log.warning("아이콘 속 숫자 검사 실패", exc_info=True)
        nfix = 0
    if nfix:
        log.info("아이콘 그림 속 숫자를 원문에서 뺌: %d칸", nfix)
    if not stale:
        return {"state": "ok", "regions": 0, "note": note}
    return {"state": "synced", "regions": len(stale), "note": note}


def card_region_rects(file_dir: Path | str, pages: list[int]) -> dict[int, dict[str, tuple]]:
    """그 파일의 카드(쪽)마다 쓰는 영역 상자 {쪽: {이름: (x, y, w, h) 비율}} (카드별·따로 영역·자동 정렬 포함).
    카드 보기에서 영역을 고치기 전후를 견준다 (공통 영역을 바꾸면 모든 카드가 바뀐다)."""
    from app.project import load_project

    proj = load_project(Path(file_dir))
    # 상자 자리·크기 + 돌림·종류 (돌리면 잘라 낸 그림이 돌아가 아이콘 자리가 맞지 않는다)
    return {int(p): {r.name: (*(round(float(v), 5) for v in (r.rect.x, r.rect.y, r.rect.w, r.rect.h)),
                              (r.style or {}).get("rotate", 0), r.kind)
                     for r in proj.template.regions_for(int(p))} for p in pages}


def rescan_card_icons(project_dir: Path | str, cards: list[tuple]) -> int:
    """카드들 [(파일 폴더, 쪽)]의 아이콘을 다시 찾는다 (그 카드의 영역 상자를 바꾼 뒤: 아이콘 자리는 영역 그림 기준이라
    상자가 바뀌면 맞지 않는다). 그 카드들의 확정 전 값·손으로 잡은 확정 전 자리는 사라진다. 돌려줌: 다시 찾은 칸 수."""
    from app import icons, icons_stage

    info, work, res = _scan(project_dir)
    if res is None or not cards:
        return 0
    want = {(_norm(fd), int(p)) for fd, p in cards}
    keys = {(o.file, o.page, o.region) for o in res.occurrences
            if (_norm(icons._occ_dir(info, o.file)), o.page) in want}
    if keys:
        icons_stage.rescan_regions(info, work, keys)
    return len(keys)


def _unasked_groups(res) -> set[str]:
    """이름 제안을 한 번도 받지 않은 이름 없는 줄 (그림이 있고, 사전·확정 이름·사람이 적은 이름이 없음)."""
    from app import icons

    if res is None:
        return set()
    has_png = {o.id for o in res.occurrences if getattr(o, "png", "")}  # 그림 파일이 없는 자리는 물을 수 없다
    return {g for g, gi in res.groups.items()
            if icons.needs_suggest(gi) and gi.get("rep") in has_png and not gi.get("suggest")
            and not gi.get("suggest_tried") and not gi.get("dict") and not _staged_name(gi)}


def _fill_variants(info, work, res) -> int:
    """이름 없는 줄 중 사전에 모양은 같고 그림 속 숫자만 다른 아이콘이 있는 줄: 숫자만 바꾼 이름(5_coinup)을 이름 칸에
    미리 채운다 (dict_how="variant", Claude를 부르지 않음). 예전에 찾은 줄도 창을 열 때 채운다. 돌려줌: 채운 줄 수."""
    from app import icons, icons_cells

    if res is None:
        return 0
    by_id = {o.id: o for o in res.occurrences}
    todo = []
    for g, gi in res.groups.items():
        o = by_id.get(gi.get("rep") or "")
        if (o is None or o.unique or not o.num or not o.png or o.named or gi.get("dict") or _staged_name(gi)
                or gi.get("not_icon") or gi.get("absorbed") or (gi.get("staged") or {}).get("not_icon")):
            continue
        todo.append((g, gi, o))
    if not todo:
        return 0
    known = icons._known_shapes(info)
    n = 0
    for _g, gi, o in todo:
        try:
            sh = icons.shape_pair(icons_cells.load_png(Path(o.png)))
        except Exception:  # noqa: BLE001 - 그림을 못 읽은 줄은 건너뛴다
            continue
        v = icons.number_variant(sh, known, o.num)
        if v:
            o.variant = v
            gi["dict"], gi["dict_how"] = v, "variant"
            n += 1
    if n:
        icons._save_scan(work, res)
    return n


def _staged_name(gi: dict) -> str:
    return str((gi.get("staged") or {}).get("name") or "").strip()


def _copy_same_shape(res, groups: set[str]) -> set[str]:
    """모양이 같은 다른 줄에 이미 이름(사람이 적은 이름·사전·제안)이 있으면 그것을 옮겨 적고 묻지 않는다
    (같은 아이콘에 다른 제안 이름이 붙지 않게·물음 한 번 아끼기). 돌려줌: 아직 물어야 할 줄."""
    def firm_dict(gi: dict) -> str:  # ‘비슷함(legacy)’ 힌트는 옮기지 않는다
        return gi.get("dict") if gi.get("dict_how") in ("named", "same") else ""

    named: dict[str, dict] = {}
    for g, gi in res.groups.items():
        sh = gi.get("shape")
        if gi.get("not_icon") or (gi.get("staged") or {}).get("not_icon") or gi.get("absorbed"):
            continue  # ‘아이콘 아님’·포함으로 정한 줄의 이름은 옮기지 않는다
        if sh and g not in groups and sh not in named and (_staged_name(gi) or firm_dict(gi) or gi.get("suggest")):
            named[sh] = gi
    left = set()
    for g in groups:
        gi = res.groups[g]
        src = named.get(gi.get("shape") or "")
        if src is None:
            left.add(g)
            continue
        gi["suggest"] = _staged_name(src) or firm_dict(src) or src.get("suggest")
        for k in ("meaning", "ko"):
            v = (src.get("staged") or {}).get(k) or src.get(k)
            if v:
                gi[k] = v
        gi["suggest_tried"] = True
    return left


def _suggest_unasked(info, work, res, engine: str | None, client=None) -> str:
    """_unasked_groups만 이름 제안을 받는다 (모양이 같은 줄에 이름이 있으면 옮겨 적기만). 돌려줌: 못 받은 까닭 또는 ""."""
    from app import icons

    groups = _unasked_groups(res)
    if not groups:
        return ""
    left = _copy_same_shape(res, groups)
    if left != groups:
        icons._save_scan(work, res)
    groups = left
    if not groups:
        return ""
    eng = engine or _settings.get_settings().engine
    return _extract.suggest_missing_names(info, work, res, engine=eng, client=client, groups=groups)


def rescan_icons(project_dir: Path | str, *, engine: str | None = None, client=None, suggest: bool = True) -> dict:
    """아이콘 다시 찾기 (게임 전체, 확정 전 값·손으로 잡은 확정 전 자리는 사라진다)."""
    eng = engine or _settings.get_settings().engine
    r = _extract.find_icons(project_dir, engine=eng, client=client, suggest=suggest)
    return {"state": "scanned", "note": getattr(r, "note", "") or ""}


def check_files(project_dir: Path | str) -> list[dict]:
    """② 원문 확인 창에 보일 파일: 이 프로젝트의 파일 + 같은 게임의 다른 프로젝트 파일 (아이콘은 게임 단위로 찾고
    용어집 만들기가 게임 전체의 이름 없는 아이콘을 보므로). [{"dir", "key", "mine"}] — 다른 프로젝트가 있으면 key는
    ‘프로젝트/그룹/파일’, 없으면 ‘그룹/파일’."""
    from app import workspace

    info = workspace.load_project_info(project_dir)
    all_fd = workspace.game_file_dirs(info)
    mine = {_norm(fd) for fd in workspace.file_dirs(info)}
    multi = any(_norm(fd) not in mine for fd in all_fd)
    out = [{"dir": fd, "key": workspace.game_file_key(fd) if multi else workspace.file_key(fd),
            "mine": _norm(fd) in mine} for fd in all_fd]
    out.sort(key=lambda f: (not f["mine"],))  # 이 프로젝트 파일을 먼저
    return out


def _icon_entries(info, res, dic: dict) -> dict[tuple, list[dict]]:
    """칸 (파일 폴더 norm, 쪽, 영역) -> 그 칸의 아이콘 줄 [{occ, group, name, named, ko, image, state, pending, shape, ...}]
    (원문 순서). 세트의 나머지 자리(자동)는 뺀다."""
    from app import icons

    out: dict[tuple, list[dict]] = {}
    if res is None:
        return out
    order = {o.id: i for i, o in enumerate(res.occurrences)}
    for g, gi in res.groups.items():
        if gi.get("auto") and gi.get("absorbed"):
            continue
        members = [o for o in res.occurrences if o.group == g]
        if not members:
            continue
        o = members[0]
        st = gi.get("staged") or {}
        name = str(st["name"] if "name" in st else icons.prefill_name(gi)).strip()
        not_icon = bool(st.get("not_icon", gi.get("not_icon")))
        e = dic.get(name) or {}
        absorbed = bool(gi.get("absorbed")) and not gi.get("auto")
        rep = next((x for x in res.occurrences if x.id == gi.get("rep")), None)
        if absorbed:
            state = "absorbed"
        elif not_icon:
            state = "not_icon"
        elif o.named and name == o.named:
            state = "ok"
        elif not (rep is not None or o.named or name in dic):
            state = "lost"  # 그림이 없고 사전에도 없는 이름: 카드 보기에서 자리를 잡아야 한다
        elif not icons.NAME_RE.match(name):
            state = "unnamed"
        else:
            state = "named"  # 이름이 정해짐 (확정하면 원문에 들어감)
        pending = state in ("named", "not_icon", "absorbed") and not (o.named and name == o.named and not not_icon)
        # 확정 전 이름을 아무도 보지 않았고 틀릴 수 있다: Claude가 그림만 보고 지은 이름(suggest), 사전과 모양이 비슷하거나
        # 숫자만 다른 이름(similar). 사람이 고른 이름(staged)·사전과 모양 같은 이름·확정했던 이름은 아니다
        guess = ""
        if pending and state == "named" and "name" not in st:
            how = gi.get("dict_how", "") if gi.get("dict") == name else ""
            if how in ("legacy", "variant"):
                guess = "similar"
            elif not how and name == gi.get("suggest"):
                guess = "suggest"
        dimg = None
        if e.get("file"):
            p = icons.icons_dir(info) / e["file"]
            dimg = str(p) if p.exists() else None
        out.setdefault((_norm(icons._occ_dir(info, o.file)), o.page, o.region), []).append({
            "occ": o.id, "group": g, "name": name, "named": o.named, "shape": gi.get("shape", ""),
            "ko": str(st.get("ko") or e.get("ko") or gi.get("ko") or ""),
            "meaning": str(st.get("meaning") or e.get("meaning") or gi.get("meaning") or ""),
            "scale": float(st.get("scale") or e.get("scale") or icons.DEFAULT_SCALE),
            "text": str(st.get("text") or ""), "not_icon": not_icon, "in_dict": name in dic,
            "manual": bool(o.manual or o.added),  # 사람이 카드 보기에서 잡거나 넣은 자리
            "image": (rep.png if rep is not None and rep.png else None) or dimg,
            "state": state, "staged": bool(st),
            "pending": pending, "guess": guess,
            "blocking": state in ("lost", "unnamed"), "reason": o.reason if rep is None else "",
            "line": o.line, "order": order[o.id], "index": o.index, "ti": o.ti,
            # 같은 영역의 다른 아이콘과 상자 순서가 원문 순서와 반대 (위아래 아이콘을 엇갈려 잡음: 그림이 엇갈려 들어간다)
            "order_conflict": bool(o.box and icons.order_conflicts(res, o)),
            # 확정할 때 원문에서 빼는 옆 숫자 (import_rows와 같은 규칙: 숫자가 그림에 들었고 사전 그림의 숫자와 같을 때.
            # 손으로 잡은 자리는 상자가 숫자까지 감쌌을 때만)
            "strip": dict(o.number) if (o.num_in_image and state == "named"
                                        and (name not in dic or icons.entry_number(dic[name]) == o.num)) else None,
            # 아이콘 그림에 든 옆 숫자 (이름과 상관없이). 이름이 아직 없는 자리는 화면에서만 글에서 뺀다
            # (strip_icon_numbers: 그림 속 숫자가 ‘5 [[?]]’처럼 글자로도 보였다)
            "num_image": dict(o.number) if o.num_in_image else None})
    for v in out.values():
        v.sort(key=lambda x: (x["line"] or 0, x["order"]))
    return out


def _set_members(info, res) -> dict[tuple, list[dict]]:
    """칸 -> 찾기가 스스로 합친 세트의 나머지 자리 [{"index", "host_group"}] (표에 따로 보이지 않고, 세트를 감싼 자리를
    확정하면 원문에서 빠진다)."""
    from app import icons

    out: dict[tuple, list[dict]] = {}
    if res is None:
        return out
    group_of = {o.id: o.group for o in res.occurrences}
    for g, gi in res.groups.items():
        if not (gi.get("auto") and gi.get("absorbed")):
            continue
        for o in (x for x in res.occurrences if x.group == g):
            out.setdefault((_norm(icons._occ_dir(info, o.file)), o.page, o.region), []).append(
                {"index": o.index, "host_group": group_of.get(gi["absorbed"], "")})
    return out


def strip_icon_numbers(text: str, row: dict, named_only: bool = False) -> str:
    """읽은 결과에서 아이콘 그림에 든 옆 숫자를 뺀다 (모든 자리를 한 번에 찾아 뺀다). 이름을 정한 자리는 확정할 때 빼는
    숫자(줄의 strip), 이름이 아직 없는 자리는 그림에 든 숫자(num_image).
    이름 없는 자리의 숫자는 보이기만 뺀다: 저장될 글(편집 칸·‘이것 쓰기’)에는 named_only=True로 이름 정한 자리만
    (확정 때 confirm_icons가 같은 숫자를 빼고 icon_numbers에 남긴다).
    자리는 그 칸의 몇째 [[…]]인가(ti)로 찾으므로 아이콘 수가 원문과 다른 글은 그대로 둔다."""
    from app import icons

    raw = row.get("source")
    raw = "\n".join(raw) if isinstance(raw, list) else (raw or "")
    toks = list(icons.TOKEN_RE.finditer(text))
    if not toks or len(toks) != len(icons.TOKEN_RE.findall(raw)):
        return text
    spans = []
    for e in row.get("icons") or []:
        ti = e.get("ti", -1)
        if e["state"] == "unnamed":
            want = None if named_only else e.get("num_image")
        else:
            want = e.get("strip")
        if not want or not want.get("text") or ti is None or not 0 <= ti < len(toks):
            continue
        t = toks[ti]
        hit = icons.number_near(text, t.start(), t.end())
        if hit is None or hit[0] != want.get("side", hit[0]):
            continue
        if icons.num_key(text[hit[1]:hit[2]]) != icons.num_key(want["text"]):
            continue
        spans.append((hit[1], hit[2]))
    return icons.strip_spans(text, spans) if spans else text


def preview_source(row: dict) -> str:
    """편집 칸에 보일 원문: 지금 원문에 확정 전 이름을 미리 넣은 것 ([[?]] → [[이름]], 아이콘 아님은 빼거나 그 글자).
    row = source_check_rows의 영역. 확정하면 원문이 이렇게 된다 (덜 끝난 아이콘은 [[?]] 그대로).
    이름 없는 아이콘의 그림 속 숫자는 여기서 빼지 않는다: 편집 칸은 저장되는 글이라, 빼 두면 고쳐서 확정할 때 숫자가
    원문에서 사라지고 icon_numbers에도 남지 않는다 (읽은 결과 표만 strip_icon_numbers로 뺀다)."""
    from app import icons

    src = row.get("source")
    if not isinstance(src, str):
        return "\n".join(src) if isinstance(src, list) else ""
    mapping: dict[int, str] = {}
    strip: dict[int, dict] = {}
    decided: set[str] = set()  # 확정하면 결정되는 줄 (세트의 나머지 자리도 함께 빠진다)
    for e in row.get("icons") or []:
        if e["state"] == "ok":
            decided.add(e["group"])
        if not e.get("pending") or e.get("index", -1) < 0:
            continue
        decided.add(e["group"])
        if e["state"] in ("not_icon", "absorbed"):
            mapping[e["index"]] = f"={e['text']}" if e.get("text") else ""
        elif e["state"] == "named":
            mapping[e["index"]] = e["name"]
            if e.get("strip"):
                strip[e["index"]] = e["strip"]
    for m in row.get("set_members") or []:
        if m["host_group"] in decided and m["index"] >= 0:
            mapping[m["index"]] = ""
    if not mapping:
        return src
    return icons.apply_icons(src, mapping, strip or None, row.get("icon_numbers"))[0]


def _same_but_icon_numbers(cell: dict, icons_: list[dict]) -> bool:
    """두 번 읽은 결과가 아이콘 그림에 든 옆 숫자(‘4[[?]]’과 ‘[[?]]’)만 다르다: 카드 보기의 읽은 결과 표는 그 숫자를 빼고
    보여 주므로 다른 글자(빨강)가 없는데 ‘두 번 읽기 다름’이 되었다 (3·4쪽). 그 숫자는 확정할 때 원문에서 빠진다."""
    from app.extract import _agree_norm

    # 확정한 아이콘도 그림 속 숫자(num_image)로 견준다 (줄의 strip은 확정 전 이름에만 있다)
    row = {**cell, "icons": [{**e, "state": "unnamed"} for e in icons_]}
    texts = []
    for rd in cell.get("readings") or []:
        src = rd.get("source")
        if src is None:
            continue
        t = "\n".join(src) if isinstance(src, list) else str(src)
        texts.append(_agree_norm(strip_icon_numbers(t, row)))  # 빈칸·문장 부호 차이는 두 번 읽기의 같다 규칙대로
    if not (len(texts) >= 2 and len(set(texts)) == 1 and texts[0] != ""):
        return False
    # 확정한 아이콘은 확정 때 숫자를 빼지 않는다(줄의 strip 없음): 원문에 그 숫자가 아직 있으면 두 번 인쇄된다 — 다른 것으로 둔다
    src = cell.get("source")
    src = "\n".join(src) if isinstance(src, list) else (src or "")
    done = {**cell, "icons": [{**e, "state": "unnamed"} for e in icons_ if e.get("state") == "ok"]}
    return strip_icon_numbers(src, done) == src


def region_status(cell: dict, icons_: list[dict]) -> tuple[str, list[str]]:
    """영역 한 줄의 상태와 까닭 (짧은 말). cell = extracted_cells의 칸 하나."""
    from app.extract import ICON_REVIEW_LABEL
    from app.icons import PLACEHOLDER_RE

    src = cell.get("source")
    text = "\n".join(src) if isinstance(src, list) else (src or "")
    reasons: list[str] = []
    fix = check = False
    if src is None:
        reasons.append("읽지 않음")
        fix = True
    n_lost = sum(1 for i in icons_ if i["state"] == "lost")
    n_unnamed = sum(1 for i in icons_ if i["state"] == "unnamed")
    if n_lost:
        reasons.append(f"아이콘 위치 못 찾음 {n_lost}개")
        fix = True
    if n_unnamed:
        reasons.append(f"이름 없는 아이콘 {n_unnamed}개")
        fix = True
    if not icons_ and PLACEHOLDER_RE.search(text) and cell.get("keep_mode") != "whole":
        reasons.append("이름 없는 아이콘 (찾기 전)")
        fix = True
    if cell.get("label") == "결론 못 냄" and not _same_but_icon_numbers(cell, icons_):
        reasons.append("두 번 읽기 다름")
        fix = True
    if cell.get("keep_mode") == "broken":
        reasons.append("원본 그대로 줄이 맞지 않음")
        fix = True
    if fix:
        return "fix", reasons
    if any(i.get("order_conflict") for i in icons_):
        # 확인한 영역이어도 다시 본다: 이대로면 한글 넣기에서 아이콘 그림이 서로 엇갈린다
        return "check", reasons + ["아이콘 순서가 원문과 다름 (아이콘을 골라 ‘이 자리로 저장’하면 자리를 바꿀 수 있음)"]
    if cell.get("keep_mode") == "whole":
        if _cells_icons.has_text_besides_icons(text):
            return "check", ["원본 그대로인데 글자도 있음"]
        return "keep", ["아이콘만 있음"]
    if cell.get("label") == ICON_REVIEW_LABEL:
        reasons.append("아이콘 고친 뒤 확인 필요" if not cell.get("icon_reread") else "아이콘 고친 뒤 아직 다시 읽지 않음")
        check = True
    elif cell.get("uncertain"):
        reasons.append("확실하지 않음")
        check = True
    # 아무도 보지 않은 확정 전 이름 (원본 그대로 칸은 위에서 끝남: 그 칸의 아이콘 이름은 쓰이지 않는다).
    # 확정하면 이 이름이 원문에 들어가 번역(뜻)과 한글 넣기(그림)에 쓰인다: 확정 전에 한 번 본다
    n_suggest = sum(1 for i in icons_ if i.get("guess") == "suggest")
    n_similar = sum(1 for i in icons_ if i.get("guess") == "similar")
    if n_suggest:
        reasons.append(f"이름 추측 {n_suggest}개")
        check = True
    if n_similar:
        reasons.append(f"사전과 비슷한 모양 이름 {n_similar}개")
        check = True
    if check:
        return "check", reasons
    if cell.get("edited") or cell.get("label") in _OK_LABELS:
        return "ok", reasons
    return "todo", reasons


def source_check_rows(project_dir: Path | str, files: list[dict]) -> list[dict]:
    """목록 창의 카드 묶음. files: [{"dir", "key"}] (프로젝트 화면의 파일).
    돌려줌 [{"key": (파일 폴더, 쪽), "file_key", "file_dir", "page", "status", "regions": [영역]}] (파일·쪽 순서).
    영역 = extracted_cells의 칸 + {"file_dir", "key": (파일 폴더, 쪽, 영역), "status", "reasons", "icons", "pending",
    "blocking"}."""
    from app import icons

    info, _work, res = _scan(project_dir)
    dic = icons.load_dictionary(info)["icons"]
    entries = _icon_entries(info, res, dic)
    sets = _set_members(info, res)
    rank = {s: i for i, s in enumerate(STATUS_ORDER)}
    cards: list[dict] = []
    for f in files:
        fd = str(f["dir"])
        by_page: dict[int, list[dict]] = {}
        for c in _cells_icons.extracted_cells(fd):
            ic = entries.get((_norm(fd), c["page"], c["region"]), [])
            if c.get("keep_mode") == "whole":  # 원본 그대로 칸의 아이콘은 확정 대상이 아니다
                ic = [dict(i, blocking=False) for i in ic]
            status, reasons = region_status(c, ic)
            by_page.setdefault(c["page"], []).append({
                **c, "file_dir": fd, "file_key": f.get("key", ""), "key": (fd, c["page"], c["region"]),
                "status": status, "reasons": reasons, "icons": ic,
                "set_members": sets.get((_norm(fd), c["page"], c["region"]), []),
                "pending": any(i["pending"] for i in ic), "blocking": any(i["blocking"] for i in ic)})
        for page in sorted(by_page):
            regs = by_page[page]
            cards.append({"key": (fd, page), "file_key": f.get("key", ""), "file_dir": fd, "page": page,
                          "status": min((r["status"] for r in regs), key=lambda s: rank[s]), "regions": regs})
    return cards


def icon_kinds(project_dir: Path | str) -> list[dict]:
    """‘아이콘 종류’ 탭: 같은 모양 = 한 줄 (모양이 없는 줄 — 위치 못 찾음 — 은 줄마다 따로).
    [{"shape", "groups": [줄], "image", "name", "names"(서로 다른 이름들), "ko", "scale", "uses", "state", "pending"}]."""
    from app import icons

    info, _work, res = _scan(project_dir)
    if res is None:
        return []
    dic = icons.load_dictionary(info)["icons"]
    entries = [e for v in _icon_entries(info, res, dic).values() for e in v]
    kinds: dict[str, dict] = {}
    for e in sorted(entries, key=lambda x: x["order"]):
        if e["state"] == "absorbed":
            continue
        k = e["shape"] or f"one:{e['group']}"
        d = kinds.setdefault(k, {"shape": e["shape"], "groups": [], "image": e["image"], "entries": []})
        d["groups"].append(e["group"])
        d["entries"].append(e)
        d["image"] = d["image"] or e["image"]
    out = []
    for k, d in kinds.items():
        es = d.pop("entries")
        live = [e for e in es if e["state"] != "not_icon"] or es
        names = list(dict.fromkeys(e["name"] for e in live if e["name"]))
        first = next((e for e in live if e["name"]), live[0])
        if any(e["state"] == "lost" for e in es):
            state = "lost"
        elif any(e["state"] == "unnamed" for e in live):
            state = "unnamed"
        elif not first["ko"].strip() and first["state"] != "not_icon":
            state = "no_ko"
        elif any(e["pending"] for e in es):
            state = "pending"
        else:
            state = "ok"
        out.append({**d, "name": first["name"], "names": names, "ko": first["ko"], "meaning": first["meaning"],
                    "scale": first["scale"], "uses": len(es), "state": state, "in_dict": first["in_dict"],
                    "not_icon": all(e["not_icon"] for e in es), "pending": any(e["pending"] for e in es)})
    rank = {"lost": 0, "unnamed": 1, "no_ko": 2, "pending": 3, "ok": 4}
    out.sort(key=lambda x: (rank[x["state"]], -x["uses"]))
    return out


def resolve_icon_name(project_dir: Path | str, group: str, name: str, exclude_groups=()) -> dict:
    """사람이 줄(group)에 적은 이름이 다른 아이콘의 이름(사전 이름, 다른 줄에 적었거나 확정한 이름)과 같으면 (대소문자는
    보지 않는다: Windows에서 Coin.png와 coin.png는 같은 파일이다) 그림을 견준다.
    - 같은 아이콘(모양이 같고 그림 속 숫자도 같음)이면 그 이름 그대로 (대소문자는 그 이름을 따른다).
    - 다른 아이콘이면 겹치지 않는 숫자를 붙인 이름 (coin → coin_2, 이미 있으면 coin_3 …).
    이 줄에 그림이 없으면(위치 못 찾음) 견줄 수 없어 그 이름을 그대로 쓴다 (사전 그림을 쓰려고 고른 이름).
    exclude_groups: 같은 이름으로 함께 적을 줄 (같은 모양 모두 적용·아이콘 종류 한 줄): 견줄 상대가 아니다
    (대소문자만 바꿀 때 그 줄들의 예전 이름으로 되돌아가지 않게).
    돌려줌 {"name": 쓸 이름, "renamed": 숫자를 붙였나, "same_as": 같은 아이콘으로 본 이름 | ""}."""
    from app import icons

    out = {"name": name, "renamed": False, "same_as": ""}
    info, _work, res = _scan(project_dir)
    if res is None or not name or not icons.NAME_RE.match(name):
        return out
    gi = res.groups.get(group) or {}
    by_id = {o.id: o for o in res.occurrences}
    dic = icons.load_dictionary(info)["icons"]
    low = name.casefold()

    skip = {group, *exclude_groups}

    def owner_name(g: str, x: dict) -> str:
        """그 줄의 지금 이름: 적은 이름, 없으면 화면에 미리 채운 이름 (확정된 이름·사전 이름·Claude 제안)."""
        st = x.get("staged") or {}
        return str(st["name"] if "name" in st else icons.prefill_name(x)).strip()

    cands: list[tuple[str, str, object]] = []  # (이름, "dict"|"group", 사전 항목 | 줄)
    cands += [(n, "dict", e) for n, e in dic.items() if n.casefold() == low]
    for g, x in res.groups.items():
        n = owner_name(g, x)
        if g not in skip and n and n.casefold() == low and not x.get("absorbed"):
            cands.append((n, "group", x))
    if not cands:
        return out
    rep = by_id.get(gi.get("rep") or "")
    if rep is None or not rep.png or not Path(rep.png).exists():
        return {**out, "name": cands[0][0], "same_as": cands[0][0]}
    mine = icons.shape_pair(icons._cells.load_png(Path(rep.png)))
    for n, kind, x in cands:
        if kind == "dict":
            p = icons.icons_dir(info) / x.get("file", f"{n}.png")
            en = icons.entry_number(x)
            if not p.exists() or (en is not None and en != rep.num):
                continue
            other = icons.shape_pair(icons._cells.load_png(p))
        else:
            o2 = by_id.get(x.get("rep") or "")
            if gi.get("shape") and x.get("shape") == gi.get("shape") and (o2 is None or o2.num == rep.num):
                return {**out, "name": n, "same_as": n}  # 찾기가 같은 모양으로 묶은 줄
            if o2 is None or not o2.png or not Path(o2.png).exists() or o2.num != rep.num:
                continue
            other = icons.shape_pair(icons._cells.load_png(Path(o2.png)))
        if icons.same_icon(mine, other) >= icons.SAME_SHAPE:
            return {**out, "name": n, "same_as": n}
    used = {n.casefold() for n in dic} | {owner_name(g, x).casefold() for g, x in res.groups.items() if g not in skip}
    k = 2
    while f"{name}_{k}".casefold() in used:
        k += 1
    return {**out, "name": f"{name}_{k}", "renamed": True}


def stage_icon_names(project_dir: Path | str, entries: list[dict]) -> None:
    """확정 전 값 적기: [{"group", "name"?, "ko"?, "meaning"?, "scale"?, "not_icon"?, "text"?}] 또는 {"group", "clear": True}.
    원문은 바꾸지 않는다 (영역 확정 때)."""
    from app import icons, icons_stage

    info, work, res = _scan(project_dir)
    if res is None:
        return
    for e in entries:
        icons_stage.stage(res, e["group"], None if e.get("clear") else e)
    icons._save_scan(work, res)


def _rows_for(info, res, dic: dict, keys: set[tuple]) -> list[dict]:
    """그 칸들에서 확정할 줄 (import_rows 행). 아이콘이 덜 끝난 줄(위치 못 찾음·이름 없음)과 그대로인 확정 아이콘은 뺀다."""
    out = []
    for key, es in _icon_entries(info, res, dic).items():
        if key not in keys:
            continue
        for e in es:
            if not e["pending"]:
                continue
            if e["state"] in ("not_icon", "absorbed"):
                out.append({"group": e["group"], "name": "", "meaning": "", "ko": "", "not_icon": True,
                            "text": e["text"], "scale": None})
            elif e["in_dict"]:
                out.append({"group": e["group"], "name": e["name"], "meaning": "", "ko": "", "not_icon": False,
                            "text": "", "scale": None, "keep_dict": True})
            else:
                out.append({"group": e["group"], "name": e["name"], "meaning": e["meaning"], "ko": e["ko"],
                            "not_icon": False, "text": "", "scale": e["scale"]})
    return out


def confirm_regions(project_dir: Path | str, keys: list[tuple], edits: dict | None = None,
                    mark_checked: bool = True) -> dict:
    """아래 _confirm_regions를 추출 결과 잠금 안에서 (뒤에서 다시 읽는 스레드가 같은 쪽 파일을 쓰는 중일 수 있다)."""
    from app import workspace

    with workspace.EXTRACT_LOCK:
        return _confirm_regions(project_dir, keys, edits, mark_checked)


def _confirm_regions(project_dir: Path | str, keys: list[tuple], edits: dict | None = None,
                     mark_checked: bool = True) -> dict:
    """영역 확정 (카드 보기 ‘이 영역 확정’, 목록 창 ‘모두 확정’). keys: [(파일 폴더, 쪽, 영역)].
    edits: {(파일 폴더, 쪽, 영역): {"source": 고친 원문, "breaks": 문단 나눔 | None}} — 사람이 원문을 고친 영역.

    1) 그 영역들의 확정 전 아이콘(이름·아이콘 아님)을 원문에 넣는다 (아이콘이 덜 끝난 줄은 남김).
    2) 원문을 고친 영역은 그 원문을 저장한다 (사람이 확인 — 다시 읽지 않음).
    3) 아이콘 자리가 바뀌었고 남은 [[?]]가 없는 영역은 다시 읽을 칸(reread)으로 돌려준다.
    4) 나머지(남은 [[?]]가 없고 읽은 원문이 있는 영역)는 고치지 않고 저장 = ‘사람이 확인’ — mark_checked일 때만
       (카드 보기에서 사람이 그 영역을 보고 확정할 때). 목록 창 ‘모두 확정’은 아이콘만 넣고 상태는 그대로 둔다
       (보지 않은 ‘결론 못 냄’·‘확실하지 않음’ 영역이 ‘사람이 확인’이 되지 않게).
    돌려줌 {"state": "confirmed"|"blocked", "blocking", "result", "reread": [{"file_dir","page","region"}],
    "still": [남은 [[?]]가 있어 확인하지 않은 영역 키], "saved": 저장한 영역 수}."""
    from app import icons
    from app.extract import keep_state, load_extracted

    edits = edits or {}
    info, _work, res = _scan(project_dir)
    nkeys = {(_norm(fd), int(p), r) for fd, p, r in keys}
    result = None
    if res is not None:
        dic = icons.load_dictionary(info)["icons"]
        rows = _rows_for(info, res, dic, nkeys)
        if rows:
            result = _cells_icons.confirm_icons(project_dir, rows, partial=True)
            if result.state != "confirmed":
                return {"state": "blocked", "blocking": list(result.blocking), "result": result, "reread": [],
                        "still": [], "saved": 0}
    changed = {(_norm(c["file_dir"]), c["page"], c["region"]) for c in (result.changed_cells if result else [])}
    nedits = {(_norm(fd), int(p), r): v for (fd, p, r), v in edits.items()}
    reread, still, saved = [], [], 0
    cache: dict[str, dict] = {}
    for fd, p, r in keys:
        k = (_norm(fd), int(p), r)
        if fd not in cache:
            cache[fd] = load_extracted(Path(fd))
        cell = (cache[fd].get(int(p)) or {}).get(r) or {}
        src = cell.get("source")
        if k in nedits:
            v = nedits[k]
            _cells_icons.save_source(fd, int(p), r, v["source"], v.get("breaks"))
            saved += 1
            continue
        text = "\n".join(src) if isinstance(src, list) else (src or "")
        if src is not None and icons.PLACEHOLDER_RE.search(text) and not keep_state(cell)[0]:
            still.append((fd, int(p), r))
            continue
        if k in changed or (cell.get("icon_reread") and cell.get("icon_changed")):
            reread.append({"file_dir": Path(fd), "page": int(p), "region": r})
            continue
        if src is not None and mark_checked:
            _cells_icons.save_source(fd, int(p), r, src)
            saved += 1
    if nedits and res is not None:  # 고친 원문의 아이콘 토큰이 달라졌으면 그 칸만 다시 찾는다 (찾기 결과를 원문에 맞춤)
        from app import icons_stage

        info, work, res = _scan(project_dir)
        stale = icons_stage.stale_regions(info, res) if res is not None else set()
        if stale:
            icons_stage.rescan_regions(info, work, stale)
    return {"state": "confirmed", "blocking": [], "result": result, "reread": reread, "still": still, "saved": saved}


_FIX_CACHE: dict = {}


def source_fix_count(project_dir: Path | str) -> int | None:
    """이 프로젝트 파일에서 ‘고칠 것’(빨강) 영역 수 (프로젝트 화면의 ② 원문 확인 끝남 표시). 추출 결과·찾기 결과·사전이
    바뀌지 않았으면 다시 세지 않는다 (화면을 새로 고칠 때마다 부른다). 셀 수 없으면 None."""
    from app import icons as icons_mod
    from app import workspace

    def mt(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    try:
        info = workspace.load_project_info(project_dir)
        fds = workspace.file_dirs(info)
        stamp = (mt(info.game_dir / "work" / "icons" / "scan.json"), mt(icons_mod.dictionary_path(info)),
                 mt(info.dir / "project.json"),
                 tuple((str(fd), max([mt(p) for p in (fd / "extract").glob("**/page-*.json")] + [0.0]),
                        mt(fd / "file.json"), max([mt(p) for p in fd.parent.parent.glob("*.json")] + [0.0]))
                       for fd in fds))  # 그룹 폴더의 영역 파일(카드별 영역 포함)도
        hit = _FIX_CACHE.get(str(info.dir))
        if hit and hit[0] == stamp:
            return hit[1]
        cards = source_check_rows(project_dir, [{"dir": fd, "key": ""} for fd in fds])
        n = sum(1 for c in cards for r in c["regions"] if r["status"] == "fix")
        _FIX_CACHE[str(info.dir)] = (stamp, n)  # 프로젝트마다 (홈 목록이 여러 프로젝트를 차례로 물어도 서로 밀어내지 않게)
        return n
    except Exception:  # noqa: BLE001 - 못 세면 표시만 예전대로
        return None


def erase_game_files(project_dir: Path | str, files: list, *, progress=None, cancel=None):
    """② 원문 확인을 닫은 뒤 원문이 바뀐 파일을 다시 지운다 — 같은 게임의 다른 프로젝트 파일도 그 프로젝트로.
    돌려줌: 모든 프로젝트 결과를 합친 ProjectStepResult (하나라도 막히면 그 상태와 까닭)."""
    from app import workspace
    from app.service import _translate_erase

    info = workspace.load_project_info(project_dir)
    want = {_norm(f) for f in files}
    out = _translate_erase.ProjectStepResult("done")
    for p in workspace.game_projects(info):
        mine = [fd for fd in workspace.file_dirs(p) if _norm(fd) in want]
        if not mine:
            continue
        r = _translate_erase.erase_project(p.dir, files=mine, progress=progress, cancel=cancel)
        out.files += r.files
        if r.state != "done" and out.state == "done":
            out.state, out.reason = r.state, r.reason
    return out

