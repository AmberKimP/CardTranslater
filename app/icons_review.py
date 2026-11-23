"""아이콘 이름 제안(Claude)과 아이콘 검증 엑셀 내보내기·가져오기 (app.icons에서 나눔)."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from app import workspace
from app import icons as _ic

if TYPE_CHECKING:
    from app.icons import ScanResult


# ---------------------------------------------------------------- 이름 제안 (모델)
SUGGEST_SYSTEM ="""You name small icons printed inside board game card text, for a translation tool.
For each icon image, give a short identifier (lowercase English letters, digits or _ only, e.g. coin, heart,
sword), what the icon stands for in the original language of the card text (meaning), and a natural Korean name
(ko) used when the icon replaces a noun in a Korean sentence. Use the sentence where it appears as context:
the icon's own place in the sentence is marked [[THIS]]; other icons in the sentence are [[?]] or [[name]] and are
NOT part of this image. Name only what is drawn in the image itself — never combine it with a neighbouring icon or
with words of the sentence (e.g. an arrow image next to the word for "crown" is just an arrow, not "crown_arrow").
Each group is a visually different icon. A number, sign or small mark printed on or right next to the icon is
part of that icon: put it in the identifier (e.g. gold_2, coin_plus1, card_minus) and in meaning/ko. Give two
groups the same identifier only when their images are truly the same icon."""


def _suggest_schema() -> dict:
    return {"type": "object", "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"group": {"type": "string"}, "name": {"type": "string"},
                       "meaning": {"type": "string"}, "ko": {"type": "string"}},
        "required": ["group", "name", "meaning", "ko"], "additionalProperties": False}}},
        "required": ["items"], "additionalProperties": False}


def needs_suggest(gi: dict) -> bool:
    """모델에게 이름을 물을 줄: 그림이 있고, 이미 확정된 이름·사전과 모양이 같은 이름이 없고, 아이콘 아님이 아닌 줄."""
    return bool(gi.get("rep")) and not gi.get("not_icon") and not gi.get("absorbed") \
        and gi.get("dict_how") not in ("named", "same", "variant")  # variant: 숫자만 바꾼 사전 이름이 있어 묻지 않는다


def marked_context(o) -> str:
    """이름을 물을 때 보낼 문장: 그 아이콘 자리의 토큰만 [[THIS]]로 바꾼다. 칸에 [[?]]가 여럿이면 모델이 어느 것이
    이 그림인지 몰라 옆 아이콘·낱말과 섞어 이름을 짓는다 (공주와기사 3쪽 화살표 → crown_return_arrow)."""
    text = o.context or ""
    toks = list(_ic.TOKEN_RE.finditer(text))
    k = o.ti if 0 <= getattr(o, "ti", -1) < len(toks) else None
    if k is None and o.index >= 0:  # 예전 스캔(ti 없음): 그 칸의 몇 번째 [[?]]
        ph = [i for i, t in enumerate(toks) if t.group(1) == "?"]
        k = ph[o.index] if o.index < len(ph) else None
    if k is None:
        return text
    t = toks[k]
    return text[:t.start()] + "[[THIS]]" + text[t.end():]


def suggest_names(res: ScanResult, client, model: str, work_dir: Path) -> tuple[float, float]:
    """모양(shape)마다 한 번 이름·뜻·한국어 이름을 제안 받아, 모양이 같은 줄 모두에 넣는다 (자리마다 물으면 비용이
    카드 수만큼 든다). 이미 확정된 줄·사전과 모양이 같은 줄은 묻지 않는다."""
    from app.llm import complete_json

    by_id = {o.id: o for o in res.occurrences}
    want = {g: gi for g, gi in res.groups.items() if needs_suggest(gi)}
    first: dict[str, str] = {}  # 모양 -> 그 모양의 첫 줄
    for g, gi in want.items():
        first.setdefault(gi.get("shape") or g, g)
    lines, images = [], []
    for g in first.values():
        o = by_id[want[g]["rep"]]
        images.append(Path(o.png))
        lines.append(json.dumps({"group": g, "image": Path(o.png).name, "sentence": marked_context(o)},
                                ensure_ascii=False))
    if not lines:
        return 0.0, 0.0
    prompt = "Icons (JSON lines; open each image file):\n" + "\n".join(lines)
    data, api, eq = complete_json(client, model, SUGGEST_SYSTEM, prompt, _suggest_schema(), work_dir, images=images)
    for it in data.get("items", []):
        gi = res.groups.get(it["group"])
        if gi is None:
            continue
        nm = re.sub(r"[^a-z0-9_]", "_", it["name"].strip().lower()).strip("_")
        shape = gi.get("shape") or it["group"]
        for h, hi in want.items():
            if h == it["group"] or (hi.get("shape") or h) == shape:
                hi.update(suggest=nm, meaning=it["meaning"], ko=it["ko"])
    return api, eq




# ---------------------------------------------------------------- 검증 엑셀
REVIEW_HEADERS = ["묶음", "그림", "제안 이름", "이름", "뜻(원문)", "한국어 이름", "처리", "표시 크기", "등장 횟수", "예문"]


def export_review(info, res: ScanResult, out: Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "아이콘"
    ws.append(REVIEW_HEADERS)
    for c in ws[1]:
        c.font = Font(bold=True)
    col = {h: i + 1 for i, h in enumerate(REVIEW_HEADERS)}
    by_id = {o.id: o for o in res.occurrences}
    dic = _ic.load_dictionary(info)["icons"]
    known = sorted(dic)
    yellow = PatternFill("solid", fgColor="FFE066")
    r = 1
    for g, gi in res.groups.items():
        if gi.get("absorbed"):  # 세트에 포함된 자리: 따로 정할 것이 없다 (확정하면 원문에서 뺀다)
            continue
        r += 1
        members = [o for o in res.occurrences if o.group == g]
        ex = "\n".join(f"{o.file_key} p{o.page} {o.region}: {o.context}" for o in members[:3])
        ws.cell(r, col["묶음"], g)
        name = _ic.prefill_name(gi)  # 이미 확정된 이름·사전과 모양이 같은 이름·모델 제안
        e = dic.get(name) or {}  # 사전에 있는 이름이면 사전 값 그대로
        ws.cell(r, col["제안 이름"], gi.get("suggest") or name or None)
        ws.cell(r, col["이름"], name or None)
        ws.cell(r, col["뜻(원문)"], (e.get("meaning") if e else gi.get("meaning")) or None)
        ws.cell(r, col["한국어 이름"], (e.get("ko") if e else gi.get("ko")) or None)
        ws.cell(r, col["표시 크기"], float(e.get("scale", _ic.DEFAULT_SCALE)) if e else _ic.DEFAULT_SCALE)
        if gi.get("not_icon"):
            ws.cell(r, col["처리"], _ic.NOT_ICON)
        ws.cell(r, col["등장 횟수"], len(members))
        ws.cell(r, col["예문"], ex).alignment = Alignment(wrap_text=True, vertical="top")
        if gi.get("rep"):
            img = XLImage(by_id[gi["rep"]].png)
            scale = 48 / max(img.height, 1)
            img.width, img.height = int(img.width * scale), int(img.height * scale)
            ws.add_image(img, f"B{r}")
            ws.row_dimensions[r].height = 42
        else:
            reason = members[0].reason if members else ""
            ws.cell(r, col["그림"], "없음").fill = yellow
            ws.cell(r, col["그림"]).comment = Comment(f"위치를 못 찾음: {reason}. 사전에 있는 이름을 쓰거나 "
                                                     f"‘{_ic.NOT_ICON}’으로 표시하세요", "CardTranslater")
    # 모든 등장 위치 (묶음 시트의 예문은 3개까지만 보이므로)
    wl = wb.create_sheet("위치")
    loc_head = ["묶음", "카드", "영역", "줄", "줄에서 순서", "그림", "위치 찾기", "원문"]
    wl.append(loc_head)
    for c in wl[1]:
        c.font = Font(bold=True)
    for i, o in enumerate(res.occurrences, start=2):
        wl.append([o.group, f"{o.file_key} p{o.page}", o.region, o.line or None, o.order or None, None,
                   "찾음" if o.png else f"못 찾음: {o.reason}", o.context])
        if o.png:
            img = XLImage(o.png)
            sc = 28 / max(img.height, 1)
            img.width, img.height = int(img.width * sc), int(img.height * sc)
            wl.add_image(img, f"F{i}")
            wl.row_dimensions[i].height = 24
        else:
            wl.cell(i, 7).fill = yellow
    for colw, w in zip("ABCDEFGH", (18, 26, 10, 5, 9, 8, 24, 70)):
        wl.column_dimensions[colw].width = w
    wl.freeze_panes = "A2"
    ws2 = wb.create_sheet("안내")
    for line in [
        "한 줄 = 카드의 아이콘 하나. 이름: 영문(대소문자)·숫자·_ 만. 같은 이름을 쓴 줄은 하나의 아이콘입니다.",
        f"처리: 아이콘이 아니면 ‘{_ic.NOT_ICON}’ (원문에서 뺍니다).",
        "사전에 이미 있는 이름을 쓰면 그 아이콘으로 바꿉니다 (그림은 사전 것을 씀).",
        "표시 크기: 한글 글자 높이 대비 (기본 1.1).",
        "이미 있는 이름: " + (", ".join(known) if known else "(없음)"),
    ]:
        ws2.append([line])
    for h, w in {"묶음": 18, "그림": 10, "제안 이름": 14, "이름": 14, "뜻(원문)": 16, "한국어 이름": 14,
                 "처리": 12, "표시 크기": 9, "등장 횟수": 9, "예문": 70}.items():
        ws.column_dimensions[chr(64 + col[h])].width = w
    out = Path(out)
    wb.save(out)
    return out


@dataclass
class IconImport:
    blocking: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    removed: int = 0
    replaced: int = 0
    # 아이콘 자리가 바뀐 칸 [(파일 폴더, 쪽, 칸)]: 손으로 다시 잡음·세트로 합침·아이콘 아님·위치 못 찾음.
    # 모델이 그 자리를 잘못 읽었을 수 있어 ② 원문 확인에서 다시 읽고 확인해야 한다 (확인 필요로 표시해 둔다)
    changed_cells: list = field(default_factory=list)
    # 알릴 것 (막지는 않음): 같은 이름 줄들의 한국어 이름·뜻이 서로 다름 등
    notes: list[str] = field(default_factory=list)


def _group_num(res: ScanResult, by_id: dict, g: str) -> str:
    """묶음 대표 자리의 옆 숫자 (사전 항목 number로 적는 그대로의 글자, 손으로 잡은 자리는 "")."""
    o = by_id.get(res.groups.get(g, {}).get("rep") or "")
    return "" if o is None or not o.num_in_image else (o.number or {}).get("text", "")


def _number_conflicts(res: ScanResult, by_id: dict, decision: dict[str, str], d: dict) -> list[str]:
    """그림 속 숫자가 어긋나는 이름 정하기를 막는 문구들.
    - 옆 숫자가 다른 묶음(상자+2·상자+5)을 한 이름으로: 그림은 하나라 다른 묶음 자리에 다른 숫자가 함께 보인다.
    - 숫자가 있는 사전 항목과 숫자가 다른 묶음: 사전 그림의 숫자가 카드의 수량과 다르게 보인다.
    - 예전 사전 항목(number 키 없음)을 숫자 있는 묶음에: 예전 그림에 숫자가 들어 있는지 몰라 수량을 잃거나 겹친다.
    모두 숫자가 없으면 괜찮다. 그림 없는 묶음(위치 못 찾음)은 숫자를 모르므로 보지 않는다."""
    def shown(ns):
        return ", ".join(n or "숫자 없음" for n in ns)

    def named(name, n, old=""):  # crate_5를 숫자 2로 나누라고 할 때는 crate_2 (crate_5_2가 아니라)
        base = name[:-len(old) - 1] if old and name.endswith(f"_{old}") else name
        return f"{base}_{n}" if n else base

    out = []
    for name in dict.fromkeys(n for n in decision.values() if n and not n.startswith("=")):
        seen = [g for g, n in decision.items() if n == name and res.groups[g].get("rep")]
        nums = sorted({_ic.num_key(_group_num(res, by_id, g)) for g in seen})
        if len(nums) > 1:
            out.append(f"수량이 다른 아이콘을 같은 이름 {name}로 정할 수 없습니다 ({shown(nums)}): "
                       f"{'·'.join(named(name, n) for n in nums)}처럼 나눠 주세요")
            continue
        e = d["icons"].get(name)
        if e is None or not nums:
            continue
        en, n = _ic.entry_number(e), nums[0]
        if en is None:
            if n:
                out.append(f"사전의 예전 아이콘 {name}은 그림에 숫자가 들어 있는지 알 수 없어 숫자 {n}이 붙은 아이콘에 "
                           f"쓸 수 없습니다: {name}_{n}처럼 새 이름을 정해 주세요")
        elif en != n:
            out.append(f"수량이 다른 아이콘을 같은 이름 {name}로 정할 수 없습니다 (사전 그림: {shown([en])}, "
                       f"이 아이콘: {shown([n])}): {named(name, n, en) if n else '다른 이름'}처럼 새 이름을 정해 주세요")
    return out


def import_review(info, xlsx: Path, work: Path) -> IconImport:
    """검증 엑셀을 읽어 사전에 넣고 원문 [[?]]를 바꾼다. 막는 문제가 있으면 아무것도 바꾸지 않는다."""
    from openpyxl import load_workbook

    ws = load_workbook(xlsx)["아이콘"]
    head = [c.value for c in ws[1]]
    rows = [dict(zip(head, r)) for r in ws.iter_rows(min_row=2, values_only=True) if any(r)]
    return import_rows(info, rows, work)


def import_rows(info, rows: list[dict], work: Path, partial: bool = False) -> IconImport:
    """검증 행(엑셀이나 화면): {"묶음", "이름", "뜻(원문)", "한국어 이름", "처리", "표시 크기"} — 한 행 = 카드의 아이콘 하나.
    같은 이름을 적은 행들은 한 아이콘(사전 한 항목)이다. 사전에 이미 있는 이름이면 사전 값(그림·한국어 이름·뜻·크기)을
    그대로 쓰고 빈 칸만 채운다. 새 이름이면 그 이름 행 중 첫 행(그림이 있는)의 그림으로 넣고, 한국어 이름·뜻·크기는
    적힌 첫 값을 쓴다 (행마다 다르면 notes로 알린다).
    이미 이름이 있던 아이콘(named)의 행은 이름을 그대로 두면 원문을 바꾸지 않고, 이름을 바꾸거나 ‘아이콘 아님’이면
    그 토큰을 [[?]]로 열어(open_named) 다른 행과 같은 방법으로 바꾼다.
    partial: 넘긴 행만 확정한다 (② 원문 확인의 ‘이 영역 확정’). 행이 없는 이름 없는 자리는 [[?]]로 남고, 찾기 결과(scan)는
    지우지 않고 확정한 자리만 이름 있는 자리로 고쳐 둔다 (다른 칸에서 손으로 잡은 자리·적어 둔 이름이 남게)."""
    res = _ic.load_scan(work)
    d = _ic.load_dictionary(info)
    out = IconImport()
    named_of = {o.group: o.named for o in res.occurrences if o.named}  # 행 -> 원문의 지금 이름
    decision: dict[str, str] = {}  # 묶음 -> 이름 ('' = 아이콘 아님)
    meta: dict[str, dict] = {}
    seen_vals: dict[str, dict[str, list[str]]] = {}  # 이름 -> 뜻·한국어 이름 -> 적힌 값들 (행 순서)
    first_rep: dict[str, str] = {}  # 새 이름 -> 그림이 있는 첫 행
    case_of = {n.casefold(): n for n in d["icons"]}  # 대소문자를 뺀 이름 -> 쓸 철자
    pictured = {str(r.get("이름") or "").strip().casefold() for r in rows  # ‘아이콘 아님’ 행의 그림은 사전 그림이 되지 않는다
                if res.groups.get(str(r.get("묶음") or ""), {}).get("rep")
                and str(r.get("처리") or "").strip() != _ic.NOT_ICON}
    for row in rows:
        g = str(row.get("묶음") or "")
        if g not in res.groups:
            out.blocking.append(f"모르는 묶음: {g} (다른 프로젝트·이전 검증 엑셀?)")
            continue
        if res.groups[g].get("absorbed") and not str(row.get("이름") or "").strip() \
                and not str(row.get("글자") or "").strip():
            decision[g] = ""  # 세트(옆 아이콘)에 포함된 자리: 원문에서 뺀다 (글자를 적었으면 아래에서 그 글자로)
            continue
        if str(row.get("처리") or "").strip() == _ic.NOT_ICON:
            text = str(row.get("글자") or "").strip()
            decision[g] = f"={text}" if text else ""  # 글자를 적으면 그 글자로, 비우면 뺀다
            continue
        name = str(row.get("이름") or "").strip()
        if not name:
            out.blocking.append(f"{g}: 이름이 비어 있음 (아이콘이 아니면 처리 열에 ‘{_ic.NOT_ICON}’)")
            continue
        if not _ic.NAME_RE.match(name):
            out.blocking.append(f"{g}: 이름은 영문·숫자·_ 만 ({name})")
            continue
        # 대소문자만 다른 이름은 같은 이름이다 (Windows에서 Coin.png와 coin.png는 같은 파일: 사전 그림을 덮어썼다).
        # 사전에 있는 이름, 없으면 이 엑셀에서 먼저 나온 철자를 따른다
        name = case_of.setdefault(name.casefold(), name)
        unchanged = named_of.get(g) == name  # 이미 확정된 아이콘을 그대로 둠: 원문을 바꾸지 않는다
        if not unchanged and not res.groups[g].get("rep") and name not in d["icons"] and name.casefold() not in pictured:
            out.blocking.append(f"{g}: 그림이 없어 새 아이콘으로 만들 수 없음 (사전에 있는 이름을 쓰거나 ‘{_ic.NOT_ICON}’)")
            continue
        decision[g] = name
        if unchanged and name in d["icons"]:
            continue  # 사전도 원문도 그대로
        m = meta.setdefault(name, {})
        for k_src, k in (("뜻(원문)", "meaning"), ("한국어 이름", "ko")):
            v = str(row.get(k_src) or "").strip()
            if v:
                seen_vals.setdefault(name, {}).setdefault(k, []).append(v)
                m.setdefault(k, v)
        if row.get("표시 크기"):
            try:
                m.setdefault("scale", float(row["표시 크기"]))
            except (TypeError, ValueError):
                out.blocking.append(f"{g}: 표시 크기가 숫자가 아님 ({row['표시 크기']})")
        if res.groups[g].get("rep"):
            first_rep.setdefault(name, g)
    occ_group = {o.id: o.group for o in res.occurrences}
    for g, gi in res.groups.items():
        if g in decision or any(g in b for b in out.blocking):
            continue
        host = occ_group.get(gi["absorbed"]) if gi.get("absorbed") else None
        if partial and gi.get("absorbed") and host not in decision and not named_of.get(host or ""):
            continue  # 세트를 감싼 자리를 이번에 확정하지 않음: 세트의 나머지 자리도 그대로 둔다
        if gi.get("absorbed"):  # 세트에 포함된 자리는 표·엑셀에 없어도 원문에서 뺀다
            decision[g] = ""
        elif named_of.get(g):  # 행이 없는 확정 아이콘 (예전 엑셀 등): 그대로 둔다
            decision[g] = named_of[g]
        elif partial:  # 이번에 확정하지 않는 자리: [[?]]로 남긴다
            continue
        else:
            out.blocking.append(f"{g}: 엑셀에 행이 없음")
    by_id = {o.id: o for o in res.occurrences}
    # 그대로 둔 확정 아이콘은 숫자를 다시 보지 않는다 (이미 사전대로 원문에서 뺐다). 새로 그 이름을 쓴 행은 사전 항목과 비교된다
    moving = {g: n for g, n in decision.items() if named_of.get(g) != n}
    out.blocking += _number_conflicts(res, by_id, moving, d)
    if out.blocking:
        return out
    labels = {"meaning": "뜻", "ko": "한국어 이름"}
    for name, kv in seen_vals.items():
        for k, vs in kv.items():
            if len(set(vs)) > 1:
                shown = ", ".join(dict.fromkeys(vs))
                used = d["icons"].get(name, {}).get(k) or vs[0]
                out.notes.append(f"{name}: 줄마다 {labels[k]}이 다름 ({shown}) — ‘{used}’로 넣음")
    # 사전에 넣기
    dst = _ic.icons_dir(info)
    for name, m in meta.items():
        groups = [g for g, n in decision.items() if n == name]
        moved = [g for g in groups if named_of.get(g) != name]  # 원문이 바뀌는 행
        reps = [by_id[res.groups[g]["rep"]].png for g in moved if res.groups[g].get("rep")]
        if name in d["icons"]:
            e = d["icons"][name]
            for k in ("meaning", "ko"):  # 사전 값을 그대로 쓴다 (빈 칸만 채움; 고치는 것은 ‘사전 전체’ 탭)
                if not e.get(k) and m.get(k):
                    e[k] = m[k]
            seen = [g for g in moved if res.groups[g].get("rep")]  # 그대로 둔 확정 아이콘은 빼고
            # 예전 항목: 숫자 없는 행으로 확정하면 사람이 ② 창에서 사전 그림을 보고 고른 것이므로 number ""로 적는다.
            # 숫자 있는 행은 _number_conflicts가 막았다 (그림 속 숫자를 몰라 행 숫자를 적지도, 원문에서 빼지도 않는다).
            if _ic.entry_number(e) is None and seen and all(_ic.num_key(_group_num(res, by_id, g)) == "" for g in seen):
                e["number"] = ""
            if moved:
                out.reused.append(name)
        else:
            top = first_rep.get(name)
            if top is None:  # 그림 없는 확정 아이콘만 이 이름 (사전에서 지운 이름): 원문은 그대로, 사전에는 못 넣는다
                out.notes.append(f"{name}: 그림이 없어 사전에 넣지 못함 (카드 보기에서 자리를 잡으세요)")
                continue
            best = by_id[res.groups[top]["rep"]].png  # 그 이름 행 중 첫 행의 그림
            _ic.save_png(_ic.load_png(Path(best)), dst / f"{name}.png")
            d["icons"][name] = {"file": f"{name}.png", "meaning": m.get("meaning", ""), "ko": m.get("ko", ""),
                                "scale": m.get("scale", _ic.DEFAULT_SCALE), "valign": "center",
                                "number": _group_num(res, by_id, top),
                                "from_project": info.name}
            out.added.append(name)
            if not reps:
                reps = [best]
        samples = dst / "samples" / name
        for p in reps:  # 카드별 원본 조각도 보관
            samples.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, samples / f"{info.name}_{Path(p).name}")
    # 열어야 할 확정 아이콘이 원문에 그대로 있는지 먼저 본다: 하나라도 없으면 사전도 원문도 바꾸지 않고 멈춘다
    # (사전을 먼저 쓰거나 앞 토큰을 열어 둔 채 멈추면 반쯤 바뀐 채로 남는다)
    from app.extract import load_extracted

    for o in res.occurrences:
        if o.named and decision.get(o.group, o.named) != o.named:
            src = (load_extracted(_ic._occ_dir(info, o.file)).get(o.page, {}).get(o.region) or {}).get("source")
            toks = list(_ic.TOKEN_RE.finditer(src if isinstance(src, str) else ""))
            if not (0 <= o.ti < len(toks)) or toks[o.ti].group(1) != o.named:
                out.blocking.append(f"{o.group}: 원문에서 [[{o.named}]]을 찾지 못했습니다 "
                                    "(원문이 바뀌었으면 ② 원문 확인을 다시 누르세요)")
    if out.blocking:
        return out
    _ic.save_dictionary(info, d)
    # 이름을 바꾸거나 아이콘 아님으로 한 확정 아이콘: 먼저 [[?]]로 연다 (아래에서 다른 행과 같이 바꾼다)
    for o in res.occurrences:
        if o.named and decision.get(o.group, o.named) != o.named:
            try:
                _ic.open_named(info, res, o)
            except ValueError as e:
                out.blocking.append(f"{o.group}: {e}")
    if out.blocking:
        _ic._save_scan(work, res)
        return out
    # 원문 바꾸기 (칸마다 한 번에)
    # 옆 숫자는 아이콘 이름으로 정했고 상자를 손으로 잡지 않았고 사전 그림의 숫자와 같을 때만 원문에서 뺀다
    # (‘아이콘 아님’·손으로 잡은 자리는 숫자를 남긴다. 숫자가 다른 행을 같은 이름으로 합치는 것은 _number_conflicts가
    # 막는다. 예전 항목(None)은 어떤 숫자와도 같지 않아 빼지 않는다)
    per_region: dict[tuple, dict[int, str]] = {}
    strips: dict[tuple, dict[int, dict]] = {}
    changed: dict[tuple, list[str]] = {}
    for o in res.occurrences:
        if o.named:  # 그대로 둔 확정 아이콘
            continue
        if partial and o.group not in decision:  # 이번에 확정하지 않는 자리
            continue
        key = (o.file, o.page, o.region)
        name = decision[o.group]
        gi = res.groups.get(o.group, {})
        # 찾기가 스스로 합친 세트(auto)는 사람이 바꾼 것이 아니고 읽은 글자도 그대로라 다시 읽지 않는다
        why = ("아이콘을 추가함" if o.added else "손으로 자리를 다시 잡음" if o.manual
               else "세트로 합침" if (gi.get("absorbed") and not gi.get("auto"))
               else "" if (o.set_of or gi.get("auto")) else "아이콘 아님" if (not name or name.startswith("="))
               else "위치를 못 찾음" if not (o.box or o.png) else "")
        if why and not gi.get("by_keep"):  # ‘원본 그대로’로 빠진 자리: 사람이 정한 칸이라 다시 읽어 확인할 것이 없다
            changed.setdefault(key, []).append(why)
        per_region.setdefault(key, {})[o.index] = name
        e = d["icons"].get(name) if name and not name.startswith("=") else None
        if e is not None and o.num_in_image and _ic.entry_number(e) == o.num:
            strips.setdefault(key, {})[o.index] = o.number
    old_src: dict[tuple, str] = {}
    new_src: dict[tuple, str] = {}
    for (f, page, region), mapping in per_region.items():
        if partial:
            from app.extract import load_extracted as _le

            v = (_le(_ic._occ_dir(info, f)).get(page, {}).get(region) or {}).get("source")
            old_src[(f, page, region)] = v if isinstance(v, str) else ""
        new = _ic.apply_region(_ic._occ_dir(info, f), page, region, mapping, strips.get((f, page, region)))
        if partial and new is not None:
            new_src[(f, page, region)] = new
        out.removed += sum(1 for v in mapping.values() if v == "")
        out.replaced += sum(1 for v in mapping.values() if v)
    for (f, page, region), whys in changed.items():
        fd = _ic._occ_dir(info, f)
        mark_icon_changed(fd, page, region, sorted(set(whys)))
        out.changed_cells.append((fd, page, region))
    # 확정한 아이콘의 자리(손으로 잡은 상자 포함)를 칸에 남긴다: ②를 다시 열어 찾을 때 그 자리를 그대로 쓴다
    # (다시 찾기는 확정된 [[이름]]의 자리를 그림으로 새로 찾으므로, 손으로 잡은 자리가 사라지거나 비슷한 아이콘 둘이
    # 한 자리로 잡혔다 — 공주와기사 6쪽)
    boxes: dict[tuple, list] = {}
    for o in res.occurrences:
        name = decision.get(o.group, o.named) if o.named else decision.get(o.group)
        if not name or name.startswith("=") or not o.box:
            continue
        if partial and (o.file, o.page, o.region) not in per_region:  # 부분 확정: 이번에 바꾼 칸만 적는다
            continue
        boxes.setdefault((o.file, o.page, o.region), []).append((o.line, o.order, name, o.box))
    for (f, page, region), items in boxes.items():
        save_icon_boxes(_ic._occ_dir(info, f), page, region, items)
    if partial:
        _settle_partial(res, decision, per_region, old_src, new_src)
        _ic._save_scan(work, res)
        return out
    shutil.rmtree(work, ignore_errors=True)
    return out


def _settle_partial(res, decision: dict, per_region: dict, old_src: dict, new_src: dict | None = None) -> None:
    """부분 확정 뒤 찾기 결과(scan)를 원문에 맞춘다: 확정한 자리는 이름 있는 자리(named, ti)로, 뺀 자리는 지우고,
    남은 [[?]] 자리의 순번(index)·토큰 번호(ti)는 바뀐 원문에 맞게 당긴다. 원문 토큰 수는 뺀 자리만큼만 줄어든다."""
    drop: set[str] = set()
    for key, mapping in per_region.items():
        src = old_src.get(key, "")
        toks = list(_ic.TOKEN_RE.finditer(src))
        ph = [ti for ti, t in enumerate(toks) if t.group(1) == "?"]
        removed = {ph[i] for i, v in mapping.items() if i < len(ph) and (not v or v.startswith("="))}

        def newpos(p: int) -> int:
            return p - sum(1 for r in removed if r < p)
        cell = [o for o in res.occurrences if (o.file, o.page, o.region) == key]
        if new_src and key in new_src:
            for o in cell:
                o.context = new_src[key].replace("\n", " / ")
        left_ph: list[tuple[int, object]] = []  # (예전 토큰 번호, 남은 [[?]] 자리)
        for o in cell:
            if o.named:
                o.ti = newpos(o.ti) if o.ti >= 0 else o.ti
                continue
            if not (0 <= o.index < len(ph)):
                continue
            p = ph[o.index]
            v = mapping.get(o.index)
            if v is None:  # 이번에 확정하지 않은 자리
                left_ph.append((p, o))
                continue
            if not v or v.startswith("="):  # 아이콘 아님·세트에 포함·글자로 바꿈: 원문에서 빠졌다
                drop.add(o.id)
                continue
            o.named, o.ti, o.index, o.prev = v, newpos(p), -1, ""
            gi = res.groups.get(o.group)
            if gi is not None:
                gi.update(named=v, dict=v, dict_how="named")
                for k in ("staged", "not_icon"):
                    gi.pop(k, None)
        new_toks = list(_ic.TOKEN_RE.finditer(new_src[key])) if new_src and key in new_src else None
        for k, (p, o) in enumerate(sorted(left_ph, key=lambda x: x[0])):
            o.ti = newpos(p)
            # 순번 = 새 원문에서 그 토큰 앞의 [[?]] 수 (자리 없는 [[?]]가 있어도 맞게)
            o.index = (sum(1 for t in new_toks[:o.ti] if t.group(1) == "?")
                       if new_toks is not None and 0 <= o.ti < len(new_toks) else k)
    if drop:
        res.occurrences = [o for o in res.occurrences if o.id not in drop]
    alive = {o.group for o in res.occurrences}
    for g in [g for g in res.groups if g not in alive]:
        del res.groups[g]


def save_icon_boxes(file_dir: Path, page: int, region: str, items: list[tuple]) -> None:
    """칸의 확정한 아이콘 자리 [(줄, 순서, 이름, 상자)]를 원문 순서대로 icon_boxes에 적는다:
    [{"name", "k"(그 칸에서 같은 이름의 몇 번째), "box"}]. 다시 찾기(scan)가 [[이름]]의 자리로 먼저 쓴다."""
    seen: dict[str, int] = {}
    entries = []
    for _line, _order, name, box in sorted(items, key=lambda x: (x[0] or 0, x[1] or 0)):
        k = seen.get(name, 0)
        seen[name] = k + 1
        entries.append({"name": name, "k": k, "box": [round(float(v), 1) for v in box]})
    for f in _ic._extract_files(file_dir, page):
        rec = json.loads(f.read_text(encoding="utf-8"))
        r = rec["regions"].get(region)
        if not r or not isinstance(r.get("source"), str):
            continue
        r["icon_boxes"] = entries
        workspace._write(f, rec)




ICON_CHANGED_NOTE = "② 원문 확인에서 아이콘 자리가 바뀜"


def mark_icon_changed(file_dir: Path, page: int, region: str, whys: list[str]) -> None:
    """아이콘 자리가 바뀐 칸을 ‘확인 필요’로 표시한다 (② 원문 확인이 그 칸을 보여 주고, 다시 읽어 비교하게).
    사람이 원문 확인에서 저장하면 풀린다."""
    for f in _ic._extract_files(file_dir, page):
        rec = json.loads(f.read_text(encoding="utf-8"))
        r = rec["regions"].get(region)
        if not r or not isinstance(r.get("source"), str):
            continue
        r["uncertain"] = True
        r["icon_changed"] = whys
        r["icon_reread"] = True  # 아직 다시 읽지 않음: ② 원문 확인을 열 때 먼저 다시 읽는다 (다시 읽으면 풀림)
        note = f"{ICON_CHANGED_NOTE}: {', '.join(whys)} — 다시 읽어 확인하세요"
        # 예전 ‘아이콘 자리가 바뀜’ 메모만 새것으로 바꾸고, 그 앞의 메모(사람이 고친 기록 등)는 남긴다
        kept = [s for s in (r.get("note") or "").split(" / ") if s and not s.startswith(ICON_CHANGED_NOTE)]
        r["note"] = " / ".join(kept + [note])
        r.pop("checked_by", None)
        workspace._write(f, rec)  # 임시 파일로 쓰고 바꿔 넣기 (뒤에서 지우는 스레드가 읽는 중이면 다시 시도)
