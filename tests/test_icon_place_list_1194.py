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
def _dialog(info, host):
    from app.qt.icon_place import IconPlaceDialog

    g = next(x for x in service.icon_review(info.dir)["groups"] if any(w["occ"] == host for w in x["where"]))
    saved = []
    return IconPlaceDialog(info.dir, g, lambda grp: grp, saved.append), saved


def _select(dlg, occs):
    from PySide6.QtCore import Qt

    for k in range(dlg.items.count()):
        it = dlg.items.item(k)
        it.setSelected(it.data(Qt.ItemDataRole.UserRole) in occs)


def test_dialog_lists_cell_items_and_merges_without_closing(qapp, tmp_path):
    from PySide6.QtWidgets import QDialog

    info, fd = _project(tmp_path, "dlg", pages=1)
    host, extra = _add_run_partner(info, fd)
    dlg, saved = _dialog(info, host)
    dlg._cur_occ = host
    dlg._load()
    assert dlg.items.count() == 2
    assert dlg.items.item(0).text().startswith("▶ 1.") and "찾음" in dlg.items.item(0).text()
    assert "위치 못 찾음" in dlg.items.item(1).text()
    assert not dlg.merge_btn.isEnabled()  # 지금 자리 하나만 고름
    _select(dlg, {host, extra})
    assert dlg.merge_btn.isEnabled() and "2개" in dlg.merge_btn.text()
    dlg.merge_btn.click()
    assert dlg.result() != QDialog.DialogCode.Accepted and saved  # 닫지 않고 표를 새로 채움
    assert _states(info, host) == [(host, "found"), (extra, "absorbed")]
    assert "포함" in dlg.items.item(1).text()
    # 되돌리기
    _select(dlg, {extra})
    assert dlg.restore_btn.isEnabled() and not dlg.del_btn.isEnabled()
    dlg.restore_btn.click()
    assert _states(info, host)[1] == (extra, "lost")
    # 삭제 (아이콘 아님)
    _select(dlg, {extra})
    assert dlg.del_btn.isEnabled()
    dlg.del_btn.click()
    assert _states(info, host)[1] == (extra, "not_icon")
    assert dlg.result() != QDialog.DialogCode.Accepted
    dlg.close()


def test_dialog_clicking_list_item_makes_it_current(qapp, tmp_path):
    info, fd = _project(tmp_path, "cur", pages=1)
    host, extra = _add_run_partner(info, fd)
    dlg, _saved = _dialog(info, host)
    dlg.select_item(extra)
    assert dlg._cur()["occ"] == extra
    assert dlg.view.box is None and dlg.view.guess  # 못 찾은 자리: 예상 위치를 보여 준다
    assert dlg.items.item(1).text().startswith("▶ 2.")
    dlg.select_item(host)
    assert dlg._cur()["occ"] == host and dlg.view.box
    dlg.close()


def test_restore_one_slot_of_multi_slot_not_icon_group(tmp_path):
    info, fd = _project(tmp_path, "multi", pages=2)
    service.find_icons(info.dir, suggest=False)
    work = info.game_dir / "work" / "icons"
    scan = icons.load_scan(work)
    g = next(g for g, gi in scan.groups.items() if gi.get("count", 1) > 1)
    members = [o.id for o in scan.occurrences if o.group == g]
    sp = work / "scan.json"
    d = json.loads(sp.read_text(encoding="utf-8"))
    d["groups"][g]["not_icon"] = True  # 2A에서 묶음째 아이콘 아님
    sp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    service.restore_icon(info.dir, members[0])
    scan = icons.load_scan(work)
    by = {o.id: o for o in scan.occurrences}
    assert scan.groups[g]["not_icon"] and scan.groups[g]["count"] == len(members) - 1  # 다른 자리는 그대로
    assert by[members[0]].group != g and not scan.groups[by[members[0]].group].get("not_icon")
    assert scan.groups[g]["rep"] in members[1:]


def test_delete_does_not_include_current_unless_selected(qapp, tmp_path):
    info, fd = _project(tmp_path, "delcur", pages=1)
    host, extra = _add_run_partner(info, fd)
    dlg, _saved = _dialog(info, host)
    dlg.select_item(host)
    _select(dlg, {extra})  # Ctrl로 다른 자리만 고름
    dlg.del_btn.click()
    assert _states(info, host) == [(host, "found"), (extra, "not_icon")]  # 지금 자리는 그대로
    dlg.close()


def test_other_group_slot_stays_current_after_action(qapp, tmp_path):
    # 목록에서 다른 묶음의 자리를 고른 뒤 동작해도 지금 자리가 콤보 자리로 돌아가지 않는다 (리뷰)
    info, fd = _project(tmp_path, "keepcur", pages=1)
    host, extra = _add_run_partner(info, fd)
    dlg, _saved = _dialog(info, host)  # 콤보 = host 묶음, extra는 다른 묶음
    dlg.select_item(extra)
    assert dlg._cur()["occ"] == extra and dlg._cur_occ == extra
    _select(dlg, {extra})
    dlg.del_btn.click()
    assert dlg._cur()["occ"] == extra and dlg.items.item(1).text().startswith("▶ 2.")
    dlg.close()


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
def test_merge_selected_among_themselves(qapp, tmp_path):
    info, fd = _project(tmp_path, "msel", pages=1)
    host, extra = _add_run_partner(info, fd)
    dlg, _saved = _dialog(info, host)
    dlg.select_item(extra)  # 지금 자리 = 못 찾은 자리 (상자 없음)
    _select(dlg, {extra})
    assert not dlg.merge_btn.isEnabled()  # 하나만 고름
    _select(dlg, {host, extra})
    assert dlg._merge_plan() == (host, [extra])  # 상자가 있는 자리로 합친다
    assert dlg.merge_btn.isEnabled() and dlg.merge_btn.text() == "합치기 (2개 → 1번)"
    dlg.merge_btn.click()
    assert _states(info, host) == [(host, "found"), (extra, "absorbed")]
    dlg.close()


def test_merge_needs_a_boxed_slot(qapp, tmp_path):
    info, fd = _project(tmp_path, "mbox", pages=1)
    host, extra = _add_run_partner(info, fd)
    service.set_icon_not_icon(info.dir, host)  # 상자 있는 자리를 아이콘 아님으로
    dlg, _saved = _dialog(info, host)
    _select(dlg, {host, extra})
    assert dlg._merge_plan() == (None, []) and not dlg.merge_btn.isEnabled()
    assert "끌어 감싸" in dlg.merge_btn.toolTip()
    dlg.close()
