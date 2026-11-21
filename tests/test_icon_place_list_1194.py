"""1.20.0: 2B 카드 보기 — 이 칸의 아이콘 목록, 목록에서 고쳐 수정·삭제·합치기·되돌리기."""

from __future__ import annotations

import json
import os

import pytest

from app import icons, service
from tests.test_icons import _add_run_partner, _project

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _states(info, occ):
    return [(it["occ"], it["state"]) for it in service.icon_place(info.dir, occ)["items"]]


# ---------------------------------------------------------------- 엔진: 칸의 자리 목록
def test_place_items_list_every_slot_in_cell(tmp_path):
    info, fd = _project(tmp_path, "items", pages=1)
    host, extra = _add_run_partner(info, fd)
    assert _states(info, host) == [(host, "found"), (extra, "lost")]
    service.absorb_icon_run(info.dir, host, [extra])
    items = service.icon_place(info.dir, host)["items"]
    assert [(it["occ"], it["state"], it["host"]) for it in items] == [(host, "found", ""), (extra, "absorbed", host)]


def test_place_items_include_named_icons(tmp_path):
    info, fd = _project(tmp_path, "named", pages=1)
    host, extra = _add_run_partner(info, fd)
    p = fd / "extract" / "pages" / "page-001.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d["regions"]["본문"]["source"] = "AB[[?]][[?]]CD[[coin]]"
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    items = service.icon_place(info.dir, host)["items"]
    assert [it["state"] for it in items] == ["found", "lost", "named"] and items[2]["name"] == "coin"


def test_boxed_absorbed_slot_drawn_as_settled(tmp_path):
    info, fd = _project(tmp_path, "boxabs", pages=1)
    host, extra = _add_run_partner(info, fd)
    sp = info.game_dir / "work" / "icons" / "scan.json"
    scan = json.loads(sp.read_text(encoding="utf-8"))
    h = next(o for o in scan["occurrences"] if o["id"] == host)
    e = next(o for o in scan["occurrences"] if o["id"] == extra)
    e.update(box=[h["box"][2], h["box"][1], h["box"][2] + 10, h["box"][3]], png=h["png"])  # 상자가 있는 자리
    sp.write_text(json.dumps(scan, ensure_ascii=False), encoding="utf-8")
    service.absorb_icon_run(info.dir, host, [extra])
    m = next(m for m in service.icon_place(info.dir, host)["marks"] if m.get("occ") == extra)
    assert m["settled"] == "absorbed" and m["not_icon"]


# ---------------------------------------------------------------- 엔진: 되돌리기
def test_restore_icon_undoes_absorb_and_not_icon(tmp_path):
    info, fd = _project(tmp_path, "restore", pages=1)
    host, extra = _add_run_partner(info, fd)
    service.absorb_icon_run(info.dir, host, [extra])
    service.restore_icon(info.dir, extra)
    assert _states(info, host) == [(host, "found"), (extra, "lost")]
    service.set_icon_not_icon(info.dir, extra)
    assert _states(info, host)[1] == (extra, "not_icon")
    service.restore_icon(info.dir, extra)
    assert _states(info, host)[1] == (extra, "lost")
    with pytest.raises(ValueError):
        service.restore_icon(info.dir, host)  # 되돌릴 것이 없음
    gi = icons.load_scan(info.game_dir / "work" / "icons").groups
    assert not any(g.get("not_icon") or g.get("absorbed") for g in gi.values())


# ---------------------------------------------------------------- 화면: 목록에서 고르고 합치기·되돌리기·삭제
def _select(dlg, occs):
    from PySide6.QtCore import Qt

    for k in range(dlg.items.count()):
        it = dlg.items.item(k)
        it.setSelected(it.data(Qt.ItemDataRole.UserRole) in occs)


def test_restore_one_slot_of_old_multi_slot_not_icon_group(tmp_path):
    """예전 scan.json(같은 모양 자리 여럿 = 한 묶음, 묶음째 아이콘 아님)도 읽으면 자리마다 한 줄로 나뉘고,
    한 자리를 되돌려도 다른 자리는 아이콘 아님 그대로다."""
    info, fd = _project(tmp_path, "multi", pages=2)
    service.find_icons(info.dir, suggest=False)
    work = info.game_dir / "work" / "icons"
    sp = work / "scan.json"
    d = json.loads(sp.read_text(encoding="utf-8"))
    g = d["occurrences"][0]["group"]
    for o in d["occurrences"]:  # 1.22 이전 형식으로: 두 자리가 한 묶음
        d["groups"].pop(o["group"], None) if o["group"] != g else None
        o["group"] = g
    d["groups"][g].update(count=2, not_icon=True, suggest="coin")  # 2A에서 묶음째 아이콘 아님
    sp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    members = [o["id"] for o in d["occurrences"]]
    scan = icons.load_scan(work)
    by = {o.id: o for o in scan.occurrences}
    assert len({o.group for o in scan.occurrences}) == 2 and all(scan.groups[o.group]["count"] == 1 for o in scan.occurrences)
    assert all(scan.groups[o.group]["not_icon"] and scan.groups[o.group]["suggest"] == "coin" for o in scan.occurrences)
    assert len({scan.groups[o.group]["shape"] for o in scan.occurrences}) == 1  # 예전 묶음 = 같은 모양
    service.restore_icon(info.dir, members[0])
    scan = icons.load_scan(work)
    by = {o.id: o for o in scan.occurrences}
    assert not scan.groups[by[members[0]].group].get("not_icon")
    assert scan.groups[by[members[1]].group]["not_icon"]  # 다른 자리는 그대로


# ---------------------------------------------------------------- ‘한 세트로’ 후보: 가운데 점이 아니라 겹친 넓이 40%
def _boxed_partner(info, fd):
    host, extra = _add_run_partner(info, fd)
    sp = info.game_dir / "work" / "icons" / "scan.json"
    scan = json.loads(sp.read_text(encoding="utf-8"))
    h = next(o for o in scan["occurrences"] if o["id"] == host)
    e = next(o for o in scan["occurrences"] if o["id"] == extra)
    x0, y0, x1, y1 = h["box"]
    e.update(box=[x1, y0, x1 + (x1 - x0), y1], png=h["png"])  # 바로 오른쪽, 같은 크기
    sp.write_text(json.dumps(scan, ensure_ascii=False), encoding="utf-8")
    return host, extra, (x0, y0, x1, y1)


def test_set_candidate_uses_overlap_not_center(tmp_path):
    info, fd = _project(tmp_path, "ov40", pages=1)
    host, extra, (x0, y0, x1, y1) = _boxed_partner(info, fd)
    w = x1 - x0
    # 옆 아이콘의 45%만 덮음 (가운데 점은 밖): 예전에는 빠졌다
    r = service.set_icon_box(info.dir, host, [x0, y0, x1 + int(w * 0.45), y1])
    assert extra in r["run_unfound"]


def test_set_candidate_ignores_slight_touch(tmp_path):
    info, fd = _project(tmp_path, "ov10", pages=1)
    host, extra, (x0, y0, x1, y1) = _boxed_partner(info, fd)
    w = x1 - x0
    r = service.set_icon_box(info.dir, host, [x0, y0, x1 + max(1, int(w * 0.1)), y1])
    assert extra not in r["run_unfound"]


# ---------------------------------------------------------------- 여러 개 고른 뒤 합치기: 고른 것끼리, 상자 있는 자리로