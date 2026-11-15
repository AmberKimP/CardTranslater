"""사람이 아이콘 자리 잡기: 카드 보기의 손으로 잡기·아이콘 아님·세트·되돌리기·흡수·나누기 (app.icons에서 나눔)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from app import workspace
from app import icons as _ic

if TYPE_CHECKING:
    from app.icons import Occurrence, ScanResult


# ---------------------------------------------------------------- 사람이 아이콘 자리 잡기 (카드 보기)
def _occ_region(info, o: Occurrence) -> tuple[Path, np.ndarray, str]:
    """그 자리의 (파일 폴더, 영역 이미지, 지금 원문)."""
    import pymupdf

    from app.extract import load_extracted
    from app.project import load_project

    fd = _ic._occ_dir(info, o.file)
    proj = load_project(fd)
    reg = next(r for r in proj.template.regions_for(o.page) if r.name == o.region)
    with pymupdf.open(next((fd / "source").glob("*.pdf"))) as doc:
        img = _ic.region_image(doc[o.page - 1], reg)
    src = (load_extracted(fd).get(o.page, {}).get(o.region) or {}).get("source")
    return fd, img, src if isinstance(src, str) else ""


def cell_occurrences(res: "ScanResult", o: "Occurrence", text: str) -> dict[int, "Occurrence"]:
    """그 칸의 [[…]] 토큰 순번(ti) -> 자리. [[?]]는 지금 원문의 j번째 [[?]] = index j인 자리,
    이름이 있는 토큰은 ti와 이름이 맞는 자리(named)."""
    toks = list(_ic.TOKEN_RE.finditer(text))
    ph = [ti for ti, t in enumerate(toks) if t.group(1) == "?"]
    same = [x for x in res.occurrences if (x.file, x.page, x.region) == (o.file, o.page, o.region)]
    out: dict[int, Occurrence] = {}
    for x in same:
        if x.named:
            if 0 <= x.ti < len(toks) and toks[x.ti].group(1) == x.named:
                out[x.ti] = x
        elif 0 <= x.index < len(ph):
            out.setdefault(ph[x.index], x)
    return out


def place_info(info, work: Path, occ_id: str) -> dict:
    """카드 보기에서 자리를 잡는 데 필요한 것: {"image": RGB, "box": 지금 자리 | None, "guess": 예상 자리 | None,
    "marks": [{"box", "name" | "group", "occ"}], "items": 목록 줄} (같은 칸의 다른 아이콘 = 이미 찾은 것). 좌표는 image px.
    이름이 있는 아이콘의 자리(named)도 자리 id가 있어 목록에서 고르고 고칠 수 있다 (표시에는 "named": 이름)."""
    res = _ic.load_scan(work)
    o = next(x for x in res.occurrences if x.id == occ_id)
    _fd, img, text = _occ_region(info, o)
    toks = list(_ic.TOKEN_RE.finditer(text))
    by_ti = cell_occurrences(res, o, text)
    out = {"image": img, "box": None, "guess": None, "marks": [], "items": []}
    for ti, t in enumerate(toks):
        x = by_ti.get(ti)
        if t.group(1) != "?" and x is None:  # 자리가 없는 확정 아이콘 (예전 찾기 결과): 이름만 보여 준다
            box = _ic.locate(img, text, ti)[0]
            if box is None:  # 줄 배치로 못 찾으면 사전 그림으로 칸 안을 찾는다
                box = match_known(info, img, t.group(1), text)
            guess = False
            if box is None:
                box, guess = _ic.estimate(img, text, ti), True
            if box is not None:
                out["marks"].append({"box": list(box), "name": t.group(1), "ti": ti,
                                     **({"guess": True} if guess else {})})
            e = _ic.load_dictionary(info)["icons"].get(t.group(1)) or {}
            pth = _ic.icons_dir(info) / e.get("file", f"{t.group(1)}.png")
            out["items"].append({"occ": "", "group": "", "name": t.group(1), "state": "named", "ti": ti,
                                 "box": list(box) if box is not None else None, "guess": guess,
                                 "png": str(pth) if pth.exists() else ""})
            continue
        if x is None:  # 찾기 뒤에 원문이 바뀌어 짝이 없는 [[?]]: 예상 자리만
            g = _ic.estimate(img, text, ti)
            if g is not None:
                out["marks"].append({"box": list(g), "group": "", "occ": "", "guess": True})
            continue
        box = x.box or (list(_ic.locate(img, text, ti)[0] or []) if x.png else [])
        item = _place_item(res, x, box)
        item["ti"] = ti
        if x.named:
            item["prev"] = x.named
            if item["state"] in ("found", "manual", "lost"):
                item["state"] = "named"
            item["png"] = x.png
        out["items"].append(item)
        if x.id == o.id:
            out["box"] = box or None
            if not box:
                out["guess"] = _ic.estimate(img, text, ti)
            continue
        if x.set_of:  # 세트의 나머지 자리는 첫 자리 상자와 같다
            continue
        gi = res.groups.get(x.group, {})
        # 아이콘 아님·옆 아이콘에 포함으로 정한 자리는 상자가 있어도 회색 (일반 아이콘과 헷갈리지 않게)
        settled = "absorbed" if gi.get("absorbed") else "not_icon" if gi.get("not_icon") else ""
        extra = {**({"not_icon": True} if gi.get("not_icon") else {}), **({"settled": settled} if settled else {}),
                 **({"named": x.named, "ti": ti} if x.named else {})}
        if box:
            out["marks"].append({"box": box, "group": x.group, "occ": x.id, **extra})
        else:  # 위치를 못 찾은 다른 자리도 예상 자리에 보여 준다 (빠뜨리지 않게).
            # 아이콘 아님·옆 아이콘에 합친 자리는 화면이 ‘위치 못 찾음’(주황)과 다르게 회색으로 그린다
            g = _ic.estimate(img, text, ti)
            if g is not None:
                out["marks"].append({"box": list(g), "group": x.group, "occ": x.id, "guess": True, **extra})
    return out


def _place_item(res: "ScanResult", x: "Occurrence", box) -> dict:
    """카드 보기 목록의 한 줄: {"occ", "group", "name": "", "state", "box", "host"}.
    state: found(찾음) | manual(직접 잡음) | lost(위치 못 찾음) | not_icon | absorbed(host 자리에 포함) | set(찾기가 합친 세트)
    | named(이미 이름이 있는 아이콘, place_info가 바꿔 넣음)."""
    gi = res.groups.get(x.group, {})
    host = gi.get("absorbed") or x.set_of or ""
    if x.set_of or (gi.get("absorbed") and gi.get("auto")):
        state = "set"
    elif gi.get("absorbed"):
        state = "absorbed"
    elif gi.get("not_icon"):
        state = "not_icon"
    elif not x.png:
        state = "lost"
    else:
        state = "manual" if x.manual else "found"
    return {"occ": x.id, "group": x.group, "name": "", "state": state, "box": list(box) if box else None,
            "host": host}


def match_known(info, img: np.ndarray, name: str, text: str) -> tuple[int, int, int, int] | None:
    """사전 아이콘 그림(name)을 칸 그림 안에서 찾는다 (여러 크기로 맞춰 봄). (x0, y0, x1, y1) 또는 None.
    크기는 글 줄 높이의 0.6~2.4배 사이에서 본다 (글 속 아이콘·아이콘만 있는 줄)."""
    import cv2

    e = _ic.load_dictionary(info)["icons"].get(name)
    if not e:
        return None
    pth = _ic.icons_dir(info) / e.get("file", f"{name}.png")
    if not pth.exists():
        return None
    tpl = _ic.load_png(pth)
    if tpl.ndim == 3 and tpl.shape[2] == 4:  # 투명한 곳은 칸 바탕(중간 회색)으로
        a = tpl[..., 3:4].astype(np.float32) / 255.0
        tpl = (tpl[..., :3].astype(np.float32) * a + 200.0 * (1 - a)).astype(np.uint8)
    g_img = cv2.cvtColor(np.ascontiguousarray(img[..., :3]), cv2.COLOR_RGB2GRAY)
    g_tpl = cv2.cvtColor(np.ascontiguousarray(tpl[..., :3]), cv2.COLOR_RGB2GRAY)
    n_lines = max(1, len([ln for ln in text.split("\n") if ln.strip()]))
    line_h = img.shape[0] / n_lines
    best, best_box = _ic.MATCH_KNOWN_MIN, None
    for f in np.linspace(0.6, 2.4, 13):
        h = int(round(line_h * f))
        w = int(round(g_tpl.shape[1] * h / max(1, g_tpl.shape[0])))
        if h < 8 or w < 8 or h > g_img.shape[0] or w > g_img.shape[1]:
            continue
        t = cv2.resize(g_tpl, (w, h), interpolation=cv2.INTER_AREA)
        if float(t.std()) < 4:
            continue
        r = cv2.matchTemplate(g_img, t, cv2.TM_CCOEFF_NORMED)
        _mn, mx, _l, loc = cv2.minMaxLoc(r)
        if mx > best:
            best, best_box = mx, (loc[0], loc[1], loc[0] + w, loc[1] + h)
    return best_box






def legacy_hint(sh, known: dict, occ_num: str) -> str:
    """모양이 같은 예전 사전 항목(number 키 없음) 이름: 자동으로 바꾸지 않고 ② 검토에 제안만 한다.
    옆 숫자가 있는 자리에는 제안하지 않는다 (예전 그림에 숫자가 들어 있는지 몰라 확정을 막으므로, _number_conflicts)."""
    if occ_num:
        return ""
    cand = [(nm, _ic.same_icon(sh, ks)) for nm, (ks, en) in known.items() if en is None]
    nm, s = max(cand, key=lambda t: t[1], default=("", 0.0))
    return nm if s >= _ic.SAME_SHAPE else ""


def set_manual_box(info, work: Path, occ_id: str, box: tuple[int, int, int, int]) -> dict:
    """사람이 잡은 자리로 그림을 다시 자른다. 줄(묶음)은 그대로이고 모양(shape)·사전 이름 제안(dict)만 다시 정한다.
    이름이 있는 아이콘의 자리면 먼저 원문의 그 토큰을 [[?]]로 연다 (open_named: 확정할 때 다시 이름이 들어간다).
    {"group", "old"(= group), "old_gone": False, "new_group": 그림이 없던 줄이 처음 그림을 얻음, "dict": 사전 이름, "box"}"""
    work = Path(work)
    res = _ic.load_scan(work)
    o = next(x for x in res.occurrences if x.id == occ_id)
    if o.named:
        open_named(info, res, o)
        _ic._save_scan(work, res)
    _fd, img, _text = _occ_region(info, o)
    H, W = img.shape[:2]
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    x0, x1 = sorted((max(0, min(W, x0)), max(0, min(W, x1))))
    y0, y1 = sorted((max(0, min(H, y0)), max(0, min(H, y1))))
    if x1 - x0 < 4 or y1 - y0 < 4:
        raise ValueError("잡은 자리가 너무 작습니다")
    rgba = _ic.cut_out(img, (x0, y0, x1, y1))
    n = 1
    while (work / "crops" / f"{o.id}_m{n}.png").exists():  # 화면이 예전 그림을 들고 있지 않게 새 이름
        n += 1
    png = work / "crops" / f"{o.id}_m{n}.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    _ic.save_png(rgba, png)
    had_picture = bool(o.png)
    o.png, o.box, o.manual, o.reason = str(png), [x0, y0, x1, y1], True, ""
    o.set_of, o.set_name = "", ""  # 사람이 따로 잡았으면 이 자리는 더는 다른 아이콘의 세트 조각이 아니다
    for x in res.occurrences:  # 이 자리를 다시 잡으면 합친 세트를 푼다 (합칠지는 저장할 때 다시 묻는다)
        if x.set_of == o.id:
            res.groups.pop(x.group, None)
            x.set_of, x.set_name, x.box, x.group = "", "", [], f"위치 못 찾음 {x.id}"
            x.reason = "세트로 합쳤던 자리 (앞 아이콘을 다시 잡아 풀었음)"
            res.groups[x.group] = {"rep": "", "count": 1}
        elif x.id != o.id and res.groups.get(x.group, {}).get("absorbed") == o.id:  # 손으로 합친 자리
            for k in ("absorbed", "not_icon", "auto"):
                res.groups[x.group].pop(k, None)
    g = o.group
    gi = res.groups.setdefault(g, {"count": 1})
    for k in ("absorbed", "auto"):  # 세트 조각이었던 줄: 이제 따로 있는 아이콘
        if gi.pop(k, None) is not None:
            gi.pop("not_icon", None)
    gi.update(rep=o.id, count=1)
    sh = _ic.shape_pair(rgba)
    gi["shape"] = _ic.shape_of_row(res, o, sh)
    known = _ic._known_shapes(info)
    best = "" if o.unique else _ic.best_known(sh, known, o.num)[0]
    legacy = "" if (o.unique or best) else legacy_hint(sh, known, o.num)
    match = best or legacy  # 제안만 (이름 칸에 미리 채움)
    if match:
        gi["dict"], gi["dict_how"] = match, ("same" if best else "legacy")
    elif gi.get("dict_how") in ("same", "legacy"):  # 예전 자리의 사전 이름은 새 그림과 맞지 않는다
        gi.pop("dict", None)
        gi.pop("dict_how", None)
    _ic._save_scan(work, res)
    return {"group": g, "old": g, "old_gone": False, "new_group": not had_picture, "dict": match,
            "box": [x0, y0, x1, y1]}


def open_named(info, res: "ScanResult", o: "Occurrence") -> None:
    """이름이 있는 아이콘의 자리(named)를 보통 자리로: 원문의 그 토큰을 [[?]]로 바꾸고(아이콘에 넣었던 숫자는 제자리에
    되살림), 같은 칸 다른 자리의 [[?]] 순번을 맞춘다. res만 고친다 (저장은 부른 쪽이). 줄의 이름 칸에는 예전 이름이
    미리 채워진다 (dict_how="prev"). 토큰을 원문에서 찾지 못하면 ValueError."""
    from app.extract import load_extracted

    fd = _ic._occ_dir(info, o.file)
    src = (load_extracted(fd).get(o.page, {}).get(o.region) or {}).get("source")
    src = src if isinstance(src, str) else ""
    toks = list(_ic.TOKEN_RE.finditer(src))
    if not (0 <= o.ti < len(toks)) or toks[o.ti].group(1) != o.named:
        raise ValueError(f"원문에서 [[{o.named}]]을 찾지 못했습니다 (원문이 바뀌었으면 ② 아이콘 정의를 다시 누르세요)")
    new_src, rep = _reopen_token(fd, o.page, o.region, src, o.ti, "② 아이콘 정의에서 고치려고 열었음")
    t = toks[o.ti]
    at = t.start() + rep.index(_ic.PLACEHOLDER)
    idx = sum(1 for m in _ic.TOKEN_RE.finditer(new_src) if m.group(1) == "?" and m.start() < at)
    for x in res.occurrences:
        if (x.file, x.page, x.region) != (o.file, o.page, o.region):
            continue
        if x is not o and not x.named and x.index >= idx:
            x.index += 1
        x.context = new_src.replace("\n", " / ")
    if rep != _ic.PLACEHOLDER:  # 되살린 숫자: 그 숫자가 사전 그림에 들어 있으면 확정할 때 다시 뺀다
        m = re.match(r"^(.*?)([ 　]*)\[\[\?\]\]$", rep) if rep.endswith(_ic.PLACEHOLDER) else None
        if m:
            o.number = {"text": m.group(1), "side": "before"}
        else:
            m2 = re.match(r"^\[\[\?\]\]([ 　]*)(.*)$", rep)
            if m2:
                o.number = {"text": m2.group(2), "side": "after"}
    o.index, o.prev, o.named = idx, o.named, ""
    gi = res.groups.get(o.group)
    if gi is not None:
        gi.pop("named", None)
        if gi.get("dict_how") == "named":
            gi["dict_how"] = "prev"


def _reopen_token(fd: Path, page: int, region: str, src: str, ti: int, note: str) -> tuple[str, str]:
    """원문 src(그 칸의 지금 원문)의 ti번째 토큰 [[이름]]을 [[?]]로 바꿔 추출 결과 파일에 쓴다. 아이콘에 넣으며 뺀
    숫자(icon_numbers)는 제자리에 되살린다. 돌려줌 (새 원문, 그 토큰 자리에 넣은 글자)."""
    t = list(_ic.TOKEN_RE.finditer(src))[ti]
    name = t.group(1)
    rep = _ic.PLACEHOLDER
    for f in _ic._extract_files(fd, page):
        rec = json.loads(f.read_text(encoding="utf-8"))
        r = (rec.get("regions") or {}).get(region)
        if r is None or r.get("source") != src:
            continue
        nums = [e for e in (r.get("icon_numbers") or []) if isinstance(e, dict)]
        e = next((e for e in nums if e.get("index") == ti and e.get("name") == name), None)
        if e and e.get("text"):  # 아이콘에 넣었던 숫자를 제자리에 (그림을 다시 자르면 다시 정한다)
            sep = e.get("sep", "")
            rep = f"{e['text']}{sep}{_ic.PLACEHOLDER}" if e.get("side") == "before" else f"{_ic.PLACEHOLDER}{sep}{e['text']}"
        if e is not None:
            nums = [x for x in nums if x is not e]
        r["source"] = src[:t.start()] + rep + src[t.end():]
        if nums:
            r["icon_numbers"] = nums
        else:
            r.pop("icon_numbers", None)
        r["icon_note"] = f"[[{name}]]: {note}"
        workspace._write(f, rec)
    return src[:t.start()] + rep + src[t.end():], rep


def set_not_icon(info, work: Path, occ_id: str) -> dict:
    """한 자리만 ‘아이콘 아님’: 그 자리를 묶음에서 빼 ‘아이콘 아님’으로 표시한 새 묶음(혼자)으로 옮긴다.
    (같은 묶음의 다른 자리는 아이콘인데 이 자리만 글자·장식을 잘못 잡은 경우. 묶음 줄의 ‘아이콘 아님’은 묶음 전체.)
    이미 혼자인 묶음이면 그 묶음을 ‘아이콘 아님’으로 표시만 한다. {"group", "old", "old_gone", "new_group"}"""
    work = Path(work)
    res = _ic.load_scan(work)
    o = next(x for x in res.occurrences if x.id == occ_id)
    old = o.group
    gi = res.groups.get(old, {})
    if gi.get("count", 1) <= 1:  # 혼자인 묶음: 옮길 것 없이 표시만
        gi["not_icon"] = True
        gi.pop("by_keep", None)  # 사람이 다시 정함: 원본 그대로를 꺼도 되돌리지 않는다 (원본 그대로가 부르면 다시 붙인다)
        res.groups[old] = gi
        _ic._save_scan(work, res)
        return {"group": old, "old": old, "old_gone": False, "new_group": False}
    gi["count"] -= 1
    if gi.get("rep") == o.id:
        rest = [x for x in res.occurrences if x.group == old and x.id != o.id and x.png]
        gi["rep"] = max(rest, key=lambda x: _ic._area(x.png)).id if rest else ""
    nums = [int(k[1:]) for k in res.groups if re.fullmatch(r"g\d+", k)]
    g = f"g{max(nums, default=0) + 1:02d}"
    res.groups[g] = {"rep": o.id if o.png else "", "count": 1, "not_icon": True}
    o.group = g
    _ic._save_scan(work, res)
    return {"group": g, "old": old, "old_gone": False, "new_group": True}


SET_ASPECT_PER_ICON = 0.8  # 세트로 볼 덩어리: 가로/세로가 아이콘 수 × 이 값 이상 (아이콘이 옆으로 늘어선 모양)


def set_box_fits(box, n: int) -> bool:
    """붙어 있는 [[?]] n개를 한 덩어리로 찾았을 때, 그 덩어리가 아이콘 n개가 옆으로 늘어선 모양인가.
    (아이콘 하나 크기인 덩어리에 이웃 [[?]]가 잘못 붙은 것은 세트가 아니다: 건물·상자 아이콘이 같은 자리로 잡힌 경우)"""
    w, h = box[2] - box[0], box[3] - box[1]
    return h > 0 and w / h >= SET_ASPECT_PER_ICON * n


SHARED_BOX_REASON = "다른 아이콘과 같은 자리로 잡혀 위치를 못 찾음"
SHARED_BOX_IOU = 0.5  # 두 자리가 이만큼 겹치면 같은 자리로 본다


def _iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def shared_boxes(boxes: list, runs: list) -> set[int]:
    """같은 칸의 [[?]]들(순번 k) 중 글자를 사이에 둔 다른 [[?]]와 같은(거의 겹치는) 자리로 잡힌 것들의 순번.
    인쇄된 아이콘은 서로 다른 자리에 있으므로 이런 자리는 적어도 하나가 잘못 찾은 것인데 어느 쪽인지 모른다:
    모두 ‘위치 못 찾음’으로 돌려 사람이 ② 카드 보기에서 잡게 한다 (자동으로 사전 이름을 넣지 않는다).
    붙어 있는 [[?]](같은 덩어리 runs[k])끼리 같은 자리는 세트일 수 있어 여기서 보지 않는다 (scan이 따로 가린다)."""
    out: set[int] = set()
    for i, a in enumerate(boxes):
        if a is None:
            continue
        for j in range(i + 1, len(boxes)):
            b = boxes[j]
            if b is not None and runs[i] != runs[j] and _iou(a, b) >= SHARED_BOX_IOU:
                out.update((i, j))
    return out


def placeholder_runs(text: str, same_line: bool = False) -> list[list[int]]:
    """[[?]]가 글자 없이 바로 붙어 있는 덩어리들. 각 덩어리는 [[?]] 순번(= 자리의 index) 목록.
    예: "[[?]][[?]][[?]]の山札" -> [[0, 1, 2]] (문양 셋이 붙은 아이콘을 모델이 셋으로 읽은 경우가 많다).
    same_line: 줄이 바뀌면 다른 덩어리 (위아래로 이어진 아이콘은 자동으로 한 세트로 합치지 않는다 — 좌우로 붙은 것만)."""
    runs: list[list[int]] = []
    j, prev_end = -1, None
    for t in _ic.TOKEN_RE.finditer(text or ""):
        if t.group(1) != "?":
            prev_end = None
            continue
        j += 1
        gap = text[prev_end:t.start()] if prev_end is not None else ""
        if runs and prev_end is not None and not gap.strip() and not (same_line and "\n" in gap):
            runs[-1].append(j)
        else:
            runs.append([j])
        prev_end = t.end()
    return runs


def run_partners(info, work: Path, occ_id: str) -> dict:
    """그 자리와 글자 없이 붙어 있는 다른 [[?]] 자리: {"boxed": [그림이 있는 자리 id], "unfound": [위치 못 찾은 자리 id]}.
    ‘아이콘 아님’으로 이미 뺀 자리는 넣지 않는다. 다른 줄(위아래)의 자리는 위치를 못 찾았으면 넣지 않는다:
    위아래로 이어진 아이콘은 사람이 상자로 함께 감쌌을 때(찾은 자리가 그 상자 안)만 합친다."""
    from app.extract import load_extracted

    res = _ic.load_scan(Path(work))
    o = next(x for x in res.occurrences if x.id == occ_id)
    src = (load_extracted(_ic._occ_dir(info, o.file)).get(o.page, {}).get(o.region) or {}).get("source")
    run = next((r for r in placeholder_runs(src if isinstance(src, str) else "") if o.index in r), [])
    out = {"boxed": [], "unfound": []}
    for x in res.occurrences:
        if (x.file, x.page, x.region) != (o.file, o.page, o.region) or x.id == o.id or x.index not in run:
            continue
        if res.groups.get(x.group, {}).get("not_icon"):
            continue
        if not x.png and x.line != o.line:
            continue
        out["boxed" if x.png else "unfound"].append(x.id)
    return out


def reopen_icon(info, work: Path, anchor_occ: str, ti: int) -> dict:
    """카드 보기: 이미 이름을 정한 아이콘([[이름]], 그 칸 모든 토큰 중 ti번째)을 다시 고칠 수 있게 [[?]] 자리로 연다.
    anchor_occ = 같은 칸의 아무 자리 (파일·쪽·영역을 알려고). 그 토큰만 [[?]]로 (아이콘에 넣었던 숫자는 되살린다),
    새 자리를 만들어 사전 그림 자리(찾을 수 있으면)를 잘라 두고, 2A에는 그 사전 이름을 ‘같은 아이콘’으로 미리 고른다.
    {"occ": 새 자리 id, "group": 묶음}. 그 토큰이 이름 있는 아이콘이 아니면 ValueError."""
    work = Path(work)
    res = _ic.load_scan(work)
    a = next(x for x in res.occurrences if x.id == anchor_occ)
    fd, img, src = _occ_region(info, a)
    toks = list(_ic.TOKEN_RE.finditer(src))
    if not (0 <= ti < len(toks)) or toks[ti].group(1) == "?":
        raise ValueError("다시 열 아이콘을 원문에서 찾지 못했습니다 (원문이 바뀌었으면 ② 아이콘 정의를 다시 누르세요)")
    mine = cell_occurrences(res, a, src).get(ti)
    if mine is not None and mine.named:  # 그 토큰의 자리가 이미 있다 (2A의 한 줄): 그 자리를 연다
        open_named(info, res, mine)
        _ic._save_scan(work, res)
        return {"occ": mine.id, "group": mine.group}
    t = toks[ti]
    name = t.group(1)
    _new, rep = _reopen_token(fd, a.page, a.region, src, ti, "카드 보기에서 다시 고치려고 열었음")
    new_src = src[:t.start()] + rep + src[t.end():]
    ph = [m for m in _ic.TOKEN_RE.finditer(new_src) if m.group(1) == "?"]
    new_ti = next(i for i, m in enumerate(_ic.TOKEN_RE.finditer(new_src)) if m.start() == t.start() + rep.index(_ic.PLACEHOLDER))
    idx = next(i for i, m in enumerate(ph) if m.start() == t.start() + rep.index(_ic.PLACEHOLDER))
    same = [x for x in res.occurrences if (x.file, x.page, x.region) == (a.file, a.page, a.region)]
    for x in same:  # 같은 칸의 뒤 [[?]] 자리들은 순번이 하나씩 밀린다
        if x.index >= idx:
            x.index += 1
        x.context = new_src.replace("\n", " / ")
    nums_ = [int(x.id[1:]) for x in res.occurrences if re.fullmatch(r"o\d+", x.id)]
    oid = f"o{max(nums_, default=0) + 1:03d}"
    line, order, _nm = _ic.tokens_in(new_src)[new_ti]
    o = _ic.Occurrence(oid, a.file, a.file_key, a.page, a.region, idx, new_src.replace("\n", " / "), line=line, order=order,
                       unique=a.unique, ti=new_ti, prev=name)
    box = _ic.locate(img, new_src, new_ti)[0] or match_known(info, img, name, new_src)
    e = _ic.load_dictionary(info)["icons"].get(name) or {}
    restored_num = rep != _ic.PLACEHOLDER
    if box is not None:
        tk = [m for m in _ic.TOKEN_RE.finditer(new_src)][new_ti]
        num = _ic.number_near(new_src, tk.start(), tk.end())
        if num is not None:  # 되살린 숫자는 scan처럼 그림에 다시 넣는다 (사전 항목의 숫자와 맞아야 확정이 막히지 않는다)
            nb = _ic.absorb_number(img, box, num[0], num[2] - num[1])
            if nb is not None:
                box = nb
                o.number = {"text": new_src[num[1]:num[2]], "side": num[0]}
        o.box = [int(v) for v in box]
        png = work / "crops" / f"{oid}.png"
        png.parent.mkdir(parents=True, exist_ok=True)
        _ic.save_png(_ic.cut_out(img, tuple(o.box)), png)
        o.png = str(png)
        nums_g = [int(k[1:]) for k in res.groups if re.fullmatch(r"g\d+", k)]
        o.group = f"g{max(nums_g, default=0) + 1:02d}"
        res.groups[o.group] = {"rep": oid, "count": 1}
    else:
        o.group, o.reason = f"위치 못 찾음 {oid}", "다시 연 확정 아이콘 (자리를 찾지 못함)"
        res.groups[o.group] = {"rep": "", "count": 1}
    en = _ic.entry_number(e)
    # 사전 항목과 숫자가 맞을 때만 그 이름을 쓴다 (예전 항목은 자리에 숫자가 없을 때만): 맞지 않으면 확정이 막힌다
    fits = not (restored_num and not o.number) and (en == o.num if en is not None else o.num == "")
    res.groups[o.group].update(suggest=name if fits else "", meaning=e.get("meaning", ""), ko=e.get("ko", ""))
    if fits:
        # 사전의 그 아이콘으로 미리 고른다. 숫자를 되살렸는데 그림에 넣지 못했으면 고르지 않는다 (숫자가 달라 확정이 막힘)
        res.groups[o.group]["dict"] = name
        res.groups[o.group]["dict_how"] = "prev"
    if o.png:
        res.groups[o.group]["shape"] = _ic.shape_of_row(res, o, _ic.shape_pair(_ic.load_png(Path(o.png))))
    pos = max((res.occurrences.index(x) for x in same), default=len(res.occurrences) - 1)
    res.occurrences.insert(pos + 1, o)
    _ic._save_scan(work, res)
    return {"occ": oid, "group": o.group}


def restore_icon(info, work: Path, occ_id: str) -> dict:
    """카드 보기에서 ‘아이콘 아님’·옆 아이콘에 포함으로 정한 자리를 다시 아이콘 자리로 (그 자리만).
    찾기가 스스로 합친 세트의 나머지 자리(auto)는 되돌리지 않는다. {"group"}. 되돌릴 것이 없으면 ValueError."""
    work = Path(work)
    res = _ic.load_scan(work)
    o = next(x for x in res.occurrences if x.id == occ_id)
    gi = res.groups.get(o.group, {})
    if o.set_of or gi.get("auto") or not (gi.get("not_icon") or gi.get("absorbed")):
        raise ValueError("되돌릴 것이 없는 자리입니다 (아이콘 아님·포함으로 정한 자리만 되돌립니다)")
    if gi.get("count", 1) > 1:  # 묶음 전체를 아이콘 아님으로 한 경우: 이 자리만 빼서 새 묶음으로 (다른 카드는 그대로)
        gi["count"] -= 1
        if gi.get("rep") == o.id:
            rest = [x for x in res.occurrences if x.group == o.group and x.id != o.id and x.png]
            gi["rep"] = max(rest, key=lambda x: _ic._area(x.png)).id if rest else ""
        if o.png:
            nums = [int(k[1:]) for k in res.groups if re.fullmatch(r"g\d+", k)]
            o.group = f"g{max(nums, default=0) + 1:02d}"
            res.groups[o.group] = {"rep": o.id, "count": 1}
        else:
            o.group = f"위치 못 찾음 {o.id}"
            res.groups[o.group] = {"rep": "", "count": 1}
    else:
        gi.pop("not_icon", None)
        gi.pop("absorbed", None)
        gi.pop("by_keep", None)
        if not o.png and not o.group.startswith("위치 못 찾음"):  # 그림이 없으면 ‘위치 못 찾음’ 줄로
            del res.groups[o.group]
            o.group = f"위치 못 찾음 {o.id}"
            gi = res.groups.setdefault(o.group, {"rep": "", "count": 1})
            gi.pop("not_icon", None)
    _ic._save_scan(work, res)
    return {"group": o.group}


def absorb_into(info, work: Path, host_id: str, occ_ids: list[str]) -> list[str]:
    """붙어 있는 자리들을 host 아이콘 하나에 포함: 그 자리들은 ‘아이콘 아님’(뜻 비움 = 원문에서 뺌)으로 옮기고
    어느 아이콘에 들어갔는지 적어 둔다. 사람이 상자 하나로 문양 여러 개를 감싼 경우. 옮긴 묶음 목록."""
    work = Path(work)
    groups = []
    for oid in occ_ids:
        r = set_not_icon(info, work, oid)
        groups.append(r["group"])
    res = _ic.load_scan(work)
    for g in groups:
        res.groups[g]["absorbed"] = host_id
    _ic._save_scan(work, res)
    return groups


OVERLAP_MIN = 0.4  # 겹친 넓이가 작은 쪽 상자의 이만큼이면 ‘겹친 아이콘’ (흡수 후보)


def _overlap_ratio(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return ix * iy / small if small > 0 else 0.0


def overlapping_occ(marks: list[dict], box, exclude: str = "") -> list[str]:
    """카드 보기 표시(place_info의 marks) 중 box와 겹친 다른 [[?]] 자리 id (이미 이름이 있는 [[이름]]은 빼고).
    한 아이콘을 두 [[?]]로 읽어 두 자리가 같은 그림을 가리키면, 한쪽에 흡수시켜 다른 쪽을 원문에서 뺀다."""
    if not box:
        return []
    return [m["occ"] for m in marks
            if m.get("occ") and m["occ"] != exclude and m.get("box") and not m.get("not_icon") and not m.get("guess")
            and not m.get("named") and _overlap_ratio(box, m["box"]) >= OVERLAP_MIN]


def split_icon(info, work: Path, occ_id: str, part_box) -> dict:
    """한 자리로 잡힌 상자 안에 아이콘이 둘이면 둘로 나눈다. part_box: 사람이 감싼 그중 하나.
    지금 상자에서 part_box를 뺀 나머지가 다른 하나다 (나란히: 가로로, 위아래: 세로로 가른다).
    원문에는 그 [[?]] 바로 뒤에 [[?]]를 하나 더 넣고 (읽는 순서: 왼쪽·위가 먼저), 새 자리를 만든다.
    돌려줌 {"first": 앞 자리 id, "second": 새 자리 id, "groups": [앞 묶음, 뒤 묶음]}. 나눌 수 없으면 ValueError."""
    from app.extract import load_extracted

    work = Path(work)
    res = _ic.load_scan(work)
    o = next((x for x in res.occurrences if x.id == occ_id), None)
    if o is None:
        raise ValueError("그 아이콘 자리를 찾지 못했습니다: ② 아이콘 정의를 다시 눌러 찾은 뒤 하세요")
    if not o.box:
        raise ValueError("이 아이콘은 아직 자리가 없습니다: 먼저 아이콘 둘을 함께 감싸 ‘이 자리로 저장’을 하세요")
    bx0, by0, bx1, by1 = o.box
    px0, py0, px1, py1 = (float(v) for v in part_box)
    # 지금 상자 밖으로 끈 부분은 잘라 낸다 (나중에 그림 끝에서 잘려 한쪽이 없어지지 않게: 원문을 바꾸기 전에 확인)
    px0, px1 = sorted((min(max(px0, bx0), bx1), min(max(px1, bx0), bx1)))
    py0, py1 = sorted((min(max(py0, by0), by1), min(max(py1, by0), by1)))
    pcx, pcy = (px0 + px1) / 2, (py0 + py1) / 2
    bcx, bcy = (bx0 + bx1) / 2, (by0 + by1) / 2
    # 감싼 상자가 지금 상자의 폭을 덜 차지하면 좌우로, 높이를 덜 차지하면 위아래로 가른다
    horizontal = (px1 - px0) / max(1, bx1 - bx0) <= (py1 - py0) / max(1, by1 - by0)
    if horizontal:
        part_first = pcx < bcx
        cut = px1 if part_first else px0
        a, b = ([bx0, by0, cut, by1], [cut, by0, bx1, by1])
    else:
        part_first = pcy < bcy
        cut = py1 if part_first else py0
        a, b = ([bx0, by0, bx1, cut], [bx0, cut, bx1, by1])
    if part_first:
        a = [px0, py0, px1, py1]
    else:
        b = [px0, py0, px1, py1]
    if min(a[2] - a[0], a[3] - a[1], b[2] - b[0], b[3] - b[1]) < 4:
        raise ValueError("나눈 한쪽이 너무 작습니다: 아이콘 하나만 감싸 주세요 (지금 상자 안에서)")
    if o.named:  # 이름이 있는 아이콘: 먼저 [[?]]로 연다 (둘 다 새로 이름을 정한다)
        open_named(info, res, o)
    # 원문: 그 [[?]] 뒤에 [[?]] 하나 더
    fd = _ic._occ_dir(info, o.file)
    src = (load_extracted(fd).get(o.page, {}).get(o.region) or {}).get("source")
    if not isinstance(src, str):
        raise ValueError("이 칸의 원문을 찾지 못했습니다")
    ph = [t for t in _ic.TOKEN_RE.finditer(src) if t.group(1) == "?"]
    if o.index >= len(ph):
        raise ValueError("원문의 [[?]] 자리가 바뀌었습니다: ② 아이콘 정의를 다시 눌러 찾은 뒤 하세요")
    new_src, new_ti, new_idx = _insert_token(info, res, o, src, ph[o.index].end(), "카드 보기에서 아이콘 하나를 둘로 나눔")
    nums = [int(x.id[1:]) for x in res.occurrences if re.fullmatch(r"o\d+", x.id)]
    new_id = f"o{max(nums, default=0) + 1:03d}"
    g = f"위치 못 찾음 {new_id}"
    line, order, _nm = _ic.tokens_in(new_src)[new_ti]
    res.occurrences.insert(res.occurrences.index(o) + 1, _ic.Occurrence(
        new_id, o.file, o.file_key, o.page, o.region, new_idx, new_src.replace("\n", " / "),
        group=g, line=line, order=order, reason="카드 보기에서 나눈 자리", unique=o.unique, ti=new_ti))
    res.groups[g] = {"rep": "", "count": 1}
    _ic._save_scan(work, res)
    r1 = set_manual_box(info, work, o.id, a)  # 두 자리 모두 그 상자로 다시 잘라 같은 모양끼리 다시 묶는다
    r2 = set_manual_box(info, work, new_id, b)
    return {"first": o.id, "second": new_id, "groups": [r1["group"], r2["group"]]}


def _insert_token(info, res: "ScanResult", cell_occ: "Occurrence", src: str, at: int, note: str) -> tuple[str, int, int]:
    """그 칸 원문 src의 글자 위치 at에 [[?]]를 넣어 추출 결과 파일에 쓰고, 같은 칸 자리들의 순번(index·ti·줄·순서)과
    icon_numbers 순번을 맞춘다 (res만 고침, 저장은 부른 쪽이). 돌려줌 (새 원문, 새 토큰의 ti, 새 [[?]]의 index)."""
    fd = _ic._occ_dir(info, cell_occ.file)
    toks = list(_ic.TOKEN_RE.finditer(src))
    ph_old = [ti for ti, t in enumerate(toks) if t.group(1) == "?"]
    new_ti = sum(1 for t in toks if t.start() < at)
    new_idx = sum(1 for t in toks if t.group(1) == "?" and t.start() < at)
    new_src = src[:at] + _ic.PLACEHOLDER + src[at:]
    for f in _ic._extract_files(fd, cell_occ.page):
        rec = json.loads(f.read_text(encoding="utf-8"))
        r = (rec.get("regions") or {}).get(cell_occ.region)
        if r is not None and r.get("source") == src:
            r["source"] = new_src
            r["icon_note"] = note
            for e in r.get("icon_numbers") or []:  # 아이콘에 넣은 숫자의 자리: 뒤 아이콘들은 하나씩 밀린다
                if isinstance(e, dict) and isinstance(e.get("index"), int) and e["index"] >= new_ti:
                    e["index"] += 1
            workspace._write(f, rec)
    rows = _ic.tokens_in(new_src)
    for x in res.occurrences:
        if (x.file, x.page, x.region) != (cell_occ.file, cell_occ.page, cell_occ.region):
            continue
        ti = x.ti if (x.named or not (0 <= x.index < len(ph_old))) else ph_old[x.index]
        if ti >= new_ti:
            ti += 1
        if not x.named and x.index >= new_idx:
            x.index += 1
        x.ti = ti
        if 0 <= ti < len(rows):
            x.line, x.order = rows[ti][0], rows[ti][1]
        x.context = new_src.replace("\n", " / ")
    return new_src, new_ti, new_idx


def insert_at(text: str, box, token_boxes: dict, size: tuple[int, int], bands: list | None = None,
              span: tuple[float, float] | None = None) -> int:
    """원문에 없던 아이콘을 그림에서 새로 잡았을 때 [[?]]를 넣을 글자 위치 (카드 보기 ‘아이콘 추가’).
    1) 줄 고르기: 같은 줄(세로로 상자 높이의 절반 이상 겹침)에 자리를 아는 아이콘 토큰이 있으면 그 토큰의 줄. 없으면 상자
       가운데 높이로 고른다 (글 줄 띠 bands 수가 원문의 글 줄 수와 같으면 그 띠, 아니면 높이 비율).
    2) 줄 안 자리: 새 상자 왼쪽의 가장 가까운 토큰(없으면 줄 왼쪽 끝 span[0])과 오른쪽의 가장 가까운 토큰(없으면 줄 오른쪽
       끝 span[1]) 사이의 글자들에, 상자 가운데의 가로 비율만큼 들어간 글자 경계 (글자 폭: 전각 1, 반각 0.5, 토큰 1).
       토큰 안에는 넣지 않는다. 예: ‘AB●CD’에서 CD 오른쪽을 감싸면 ‘AB[[?]]CD[[?]]’.
    글자 사이의 자리는 짐작이다: 그 칸은 확정한 뒤 ③ 원문 확인에서 다시 읽어 확인한다 (‘아이콘을 추가함’).
    token_boxes: {토큰 순번 ti: 상자}, size: (그림 폭, 높이), span: 그 줄 잉크의 (왼쪽, 오른쪽) x (없으면 그림 폭 전체)."""
    toks = list(_ic.TOKEN_RE.finditer(text))
    x0, y0, x1, y1 = (float(v) for v in box)
    cx, cy, h = (x0 + x1) / 2, (y0 + y1) / 2, max(1.0, y1 - y0)
    W, H = size
    lines = text.split("\n")
    starts, pos = [], 0
    for ln in lines:
        starts.append(pos)
        pos += len(ln) + 1

    def line_of(at: int) -> int:
        return max(i for i, st in enumerate(starts) if st <= at)

    same = []
    for ti, b in token_boxes.items():
        if not b or not (0 <= ti < len(toks)):
            continue
        ov = min(y1, b[3]) - max(y0, b[1])
        if ov >= 0.5 * min(h, max(1.0, b[3] - b[1])):
            same.append(ti)
    if same:
        li = max(set(line_of(toks[ti].start()) for ti in same), key=lambda k: sum(
            1 for ti in same if line_of(toks[ti].start()) == k))
    else:
        filled = [i for i, ln in enumerate(lines) if ln.strip()] or [0]
        if bands and len(bands) == len(filled):
            k = min(range(len(bands)), key=lambda i: 0 if bands[i][0] <= cy <= bands[i][1]
                    else min(abs(cy - bands[i][0]), abs(cy - bands[i][1])))
        else:
            k = min(len(filled) - 1, max(0, int(cy / max(1, H) * len(filled))))
        li = filled[k]
    s0 = starts[li]
    s1 = s0 + len(lines[li].rstrip())
    s0 += len(lines[li]) - len(lines[li].lstrip())
    on_line = sorted(((token_boxes[ti][0] + token_boxes[ti][2]) / 2, ti) for ti in same
                     if line_of(toks[ti].start()) == li)
    left = [t for t in on_line if t[0] < cx]
    right = [t for t in on_line if t[0] >= cx]
    sx0, sx1 = span if span else (0.0, float(W))
    a, xa = (toks[left[-1][1]].end(), float(token_boxes[left[-1][1]][2])) if left else (s0, float(sx0))
    b, xb = (toks[right[0][1]].start(), float(token_boxes[right[0][1]][0])) if right else (s1, float(sx1))
    if b <= a:
        return a
    # 글자 칸 (토큰은 한 칸): [(시작, 끝, 폭)]
    cells, i = [], a
    seg_toks = {t.start(): t.end() for t in toks if a <= t.start() < b}
    while i < b:
        if i in seg_toks:
            cells.append((i, seg_toks[i], 1.0))
            i = seg_toks[i]
        else:
            cells.append((i, i + 1, _ic._units(text[i])))
            i += 1
    total = sum(c[2] for c in cells) or 1.0
    f = min(1.0, max(0.0, (cx - xa) / (xb - xa))) if xb > xa else (0.0 if cx <= xa else 1.0)
    want = f * total
    best, best_d, acc = a, abs(want), 0.0
    for c0, c1, wdt in cells:
        acc += wdt
        if abs(acc - want) < best_d:
            best, best_d = c1, abs(acc - want)
    return best


def add_icon(info, work: Path, anchor_occ: str, box) -> dict:
    """카드 보기 ‘아이콘 추가’: 원문에 없던 아이콘을 사람이 그림에서 감쌌다. 그 칸 원문에 [[?]]를 넣고(insert_at 규칙)
    새 자리(줄)를 만들어 그 상자로 그림을 자른다. anchor_occ = 같은 칸의 아무 자리.
    {"occ": 새 자리 id, "group": 새 줄, "line": 원문 줄, "order": 그 줄에서 몇째}. 상자가 너무 작으면 ValueError."""
    work = Path(work)
    res = _ic.load_scan(work)
    a = next(x for x in res.occurrences if x.id == anchor_occ)
    _fd, img, src = _occ_region(info, a)
    H, W = img.shape[:2]
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    x0, x1 = sorted((max(0, min(W, x0)), max(0, min(W, x1))))
    y0, y1 = sorted((max(0, min(H, y0)), max(0, min(H, y1))))
    if x1 - x0 < 4 or y1 - y0 < 4:
        raise ValueError("잡은 자리가 너무 작습니다")
    token_boxes = {ti: x.box for ti, x in cell_occurrences(res, a, src).items() if x.box}
    for ti, t in enumerate(_ic.TOKEN_RE.finditer(src)):  # 자리가 없는 토큰은 줄 배치로 찾아 본다
        if ti not in token_boxes:
            b = _ic.locate(img, src, ti)[0]
            if b is not None:
                token_boxes[ti] = list(b)
    bands, span = None, None
    try:
        ink = _ic._ink(img, _ic.text_em(img, src))
        bands = _ic._bands(ink)
        cols = np.where(ink[y0:y1].any(axis=0))[0]  # 새 상자 높이의 잉크 (그 줄의 왼쪽·오른쪽 끝)
        if len(cols):
            span = (float(cols[0]), float(cols[-1]))
    except Exception:  # noqa: BLE001 - 띠를 못 찾으면 높이 비율·그림 폭으로
        pass
    at = insert_at(src, (x0, y0, x1, y1), token_boxes, (W, H), bands, span)
    new_src, new_ti, new_idx = _insert_token(info, res, a, src, at, "카드 보기에서 원문에 없던 아이콘을 넣음")
    nums = [int(x.id[1:]) for x in res.occurrences if re.fullmatch(r"o\d+", x.id)]
    new_id = f"o{max(nums, default=0) + 1:03d}"
    g = f"위치 못 찾음 {new_id}"
    line, order, _nm = _ic.tokens_in(new_src)[new_ti]
    same = [k for k, x in enumerate(res.occurrences) if (x.file, x.page, x.region) == (a.file, a.page, a.region)]
    res.occurrences.insert(max(same) + 1, _ic.Occurrence(
        new_id, a.file, a.file_key, a.page, a.region, new_idx, new_src.replace("\n", " / "),
        group=g, line=line, order=order, reason="카드 보기에서 새로 넣은 자리", unique=a.unique, ti=new_ti, added=True))
    res.groups[g] = {"rep": "", "count": 1}
    _ic._save_scan(work, res)
    r = set_manual_box(info, work, new_id, (x0, y0, x1, y1))
    return {"occ": new_id, "group": r["group"], "line": line, "order": order, "dict": r.get("dict", "")}


def resolve_suggested_name(info, res: "ScanResult", group: str) -> str:
    """제안 받은 이름이 이미 있는 이름과 겹칠 때: 그림이 같으면 같은 아이콘으로(사전 아이콘이면 group["dict"]),
    다르면 뒤에 _2, _3 …을 붙여 다른 이름으로 바꾼다. 결과: "same_dict" | "same_row" | "renamed" | ""."""
    gi = res.groups.get(group) or {}
    name = (gi.get("suggest") or "").strip()
    by_id = {o.id: o for o in res.occurrences}
    rep = by_id.get(gi.get("rep") or "")
    if not name or rep is None or not rep.png:
        return ""
    sh = _ic.shape_pair(_ic.load_png(Path(rep.png)))
    known = _ic._known_shapes(info)
    taken = set(_ic.load_dictionary(info)["icons"])
    if rep.unique:  # 그림 배경 칸의 아이콘: 그림이 같아 보여도 같은 아이콘으로 보지 않고 겹치면 다른 이름
        for g, o_gi in res.groups.items():
            if g != group and not o_gi.get("not_icon"):
                taken.add(o_gi.get("suggest") or "")
        if name not in taken:
            return ""
        gi["suggest"] = free_name(name, taken)
        return "renamed"
    if name in known and _ic.number_allows(known[name][1], rep.num) and _ic.same_icon(sh, known[name][0]) >= _ic.SAME_SHAPE:
        gi["dict"], gi["dict_how"] = name, "same"  # 사전의 그 아이콘과 같음: 같은 아이콘으로 등록
        return "same_dict"
    for g, o_gi in res.groups.items():
        if g == group or o_gi.get("not_icon"):
            continue
        if gi.get("shape") and o_gi.get("shape") == gi.get("shape"):
            continue  # 모양이 같은 줄: 같은 이름이 맞다 (확정할 때 한 아이콘이 된다)
        o_name = o_gi.get("suggest") or ""
        taken.add(o_name)
        if o_name == name and name not in known:
            o_rep = by_id.get(o_gi.get("rep") or "")
            if (o_rep is not None and o_rep.png and o_rep.num == rep.num
                    and _ic.same_icon(sh, _ic.shape_pair(_ic.load_png(Path(o_rep.png)))) >= _ic.SAME_SHAPE):
                return "same_row"  # 다른 줄과 같은 그림: 같은 이름 그대로 (확정할 때 한 아이콘이 된다)
    if name not in taken:
        return ""
    gi["suggest"] = free_name(name, taken)  # 그림이 다른 아이콘: 다른 이름
    return "renamed"


def free_name(name: str, taken: set) -> str:
    """name이 이미 있을 때 쓸 다른 이름.
    coin·coin_2가 있으면 coin_2 -> coin_3 처럼 이미 있는 번호만 이어 간다 (coin, coin_2, …, coin_N이 빠짐없이 있을 때만).
    그 밖에는 숫자에 뜻이 있는 이름으로 보고 떼지 않는다: dice·dice_6만 있으면 dice_6_2, gold_2만 있으면 gold_2_2
    (이름 제안은 gold_2처럼 그림의 숫자를 이름에 넣으므로, 번호를 바꾸면 다른 값이 된다)."""
    m = re.fullmatch(r"(.+)_(\d+)", name)
    copies = bool(m) and m.group(1) in taken and all(f"{m.group(1)}_{i}" in taken for i in range(2, int(m.group(2))))
    base = m.group(1) if copies else name
    k = 2
    while f"{base}_{k}" in taken:
        k += 1
    return f"{base}_{k}"
