"""1.21.0: 2B 카드 보기 (확정 아이콘 다시 열기·두 줄 높이 상자·원본 그대로·닫지 않기·이름 이어받기),
3A 가로 스크롤, 디버그 모드 버튼 크기."""

from __future__ import annotations

import json
import os

import numpy as np
import pytest

from app import icons, service
from tests.test_batch_1170 import _confirm_all
from tests.test_icons import _add_run_partner, _project

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _set_src(fd, text, page=1):
    p = fd / "extract" / "pages" / f"page-{page:03d}.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d["regions"]["본문"]["source"] = text
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def _src(fd, page=1):
    p = fd / "extract" / "pages" / f"page-{page:03d}.json"
    return json.loads(p.read_text(encoding="utf-8"))["regions"]["본문"]["source"]


def _named_project(tmp_path, name):
    """1쪽 원문 AB[[lily]]CD[[?]]: 확정 아이콘 하나 + 아직 정하지 않은 자리 하나."""
    info, fd = _project(tmp_path, name, pages=1)
    service.find_icons(info.dir, suggest=False)
    assert _confirm_all(info, "lily").state == "confirmed"
    _set_src(fd, "AB[[lily]]CD[[?]]")
    service.find_icons(info.dir, suggest=False)
    occ = next(o for o in icons.load_scan(info.game_dir / "work" / "icons").occurrences if o.page == 1)
    return info, fd, occ.id


# ---------------------------------------------------------------- 확정 아이콘 다시 열기
def test_place_items_named_has_token_index_and_picture(tmp_path):
    info, fd, occ = _named_project(tmp_path, "nm")
    items = service.icon_place(info.dir, occ)["items"]
    named = next(it for it in items if it["state"] == "named")
    assert named["name"] == "lily" and named["ti"] == 0 and named["png"].endswith(".png")


def test_reopen_named_icon_makes_slot_with_dictionary_name(tmp_path):
    info, fd, occ = _named_project(tmp_path, "reopen")
    r = service.reopen_icon(info.dir, occ, 0)
    assert _src(fd) == "AB[[?]]CD[[?]]"
    scan = icons.load_scan(info.game_dir / "work" / "icons")
    by = {o.id: o for o in scan.occurrences}
    assert by[r["occ"]].index == 0 and by[occ].index == 1  # 뒤 자리 순번이 밀린다
    gi = scan.groups[r["group"]]
    assert gi["dict"] == "lily" and gi["suggest"] == "lily"
    rev = service.icon_review(info.dir)
    g = next(x for x in rev["groups"] if x["group"] == r["group"])
    assert g["dict"] == "lily"  # 2A에서 사전 lily가 미리 골라진다
    with pytest.raises(ValueError):
        service.reopen_icon(info.dir, occ, 0)  # 이미 [[?]]


def test_dialog_merges_named_icon_with_slot(qapp, tmp_path):
    from PySide6.QtCore import Qt

    from app.qt.icon_place import IconPlaceDialog

    info, fd, occ = _named_project(tmp_path, "mnamed")
    g = next(x for x in service.icon_review(info.dir)["groups"] if any(w["occ"] == occ for w in x["where"]))
    dlg = IconPlaceDialog(info.dir, g, lambda grp: grp, lambda r: None)
    dlg.focus_named("#ti0")  # 누르기만: 원문은 그대로
    assert _src(fd) == "AB[[lily]]CD[[?]]" and dlg._named["name"] == "lily"
    for k in range(dlg.items.count()):
        dlg.items.item(k).setSelected(True)
    host, others = dlg._merge_plan()
    assert host and others and dlg.merge_btn.isEnabled()
    dlg.merge_btn.click()
    assert _src(fd).count("[[?]]") == 2 and "lily" not in _src(fd)  # 확정 아이콘을 열어 합침
    states = [it["state"] for it in service.icon_place(info.dir, occ)["items"]]
    assert "absorbed" in states
    dlg.close()


# ---------------------------------------------------------------- 두 줄 높이 상자
def _two_line_image():
    img = np.full((140, 400, 3), 255, np.uint8)
    for y in (20, 80):  # 글자 줄 두 개 (글자 높이 24)
        for x in range(10, 390, 30):
            img[y:y + 24, x:x + 18] = 20
    return img


def test_tall_box_with_text_inside_is_a_problem():
    img = _two_line_image()
    assert icons.tall_box_problem(img, "あいう\nえおか", (95, 15, 160, 110))  # 두 줄 높이, 글자가 상자 안


def test_tall_box_beside_all_text_is_fine():
    img = _two_line_image()
    img[:, :60] = 255  # 왼쪽은 비워 두고 큰 아이콘 자리
    assert icons.tall_box_problem(img, "あいう\nえおか", (0, 15, 55, 110)) == ""
    assert icons.tall_box_problem(img, "あいう\nえおか", (95, 20, 160, 44)) == ""  # 한 줄 높이


# ---------------------------------------------------------------- 원본 그대로
def test_keep_cell_original_marks_slots_not_icon(tmp_path):
    info, fd = _project(tmp_path, "keep", pages=1)
    host, extra = _add_run_partner(info, fd)
    r = service.keep_cell_original(info.dir, host, True)
    assert r["keep"] and set(r["not_icon"]) == {host, extra}
    assert service.cell_kept(info.dir, host)
    states = {it["occ"]: it["state"] for it in service.icon_place(info.dir, host)["items"]}
    assert states == {host: "not_icon", extra: "not_icon"}
    service.keep_cell_original(info.dir, host, False)
    assert not service.cell_kept(info.dir, host)


# ---------------------------------------------------------------- 저장 뒤 위치 못 찾음이 남으면 닫지 않음
def test_save_keeps_window_open_when_lost_remains(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QDialog

    import app.qt.icon_place as ip
    from app.qt.icon_place import IconPlaceDialog

    info, fd = _project(tmp_path, "stay", pages=1)
    host, extra = _add_run_partner(info, fd)
    g = next(x for x in service.icon_review(info.dir)["groups"] if any(w["occ"] == host for w in x["where"]))
    monkeypatch.setattr(ip, "ask_choice", lambda *a, **k: 1)  # 따로 있는 아이콘
    monkeypatch.setattr(service, "engine_status", lambda: {})
    dlg = IconPlaceDialog(info.dir, g, lambda grp: grp, lambda r: None)
    dlg.select_item(host)
    b = dlg.view.box
    dlg.view.new = [b[0], b[1], b[2] - 1, b[3]]
    dlg._save()
    assert dlg.result() != QDialog.DialogCode.Accepted  # extra가 아직 위치 못 찾음
    assert "닫지 않았습니다" in dlg.status.text()
    dlg.close()


# ---------------------------------------------------------------- 2A: 다시 잡아 새 줄이 생기면 이름을 이어받는다
def test_rebox_new_group_inherits_typed_name(qapp, tmp_path):
    from app.qt.icon_review import IconReviewDialog

    info, fd = _project(tmp_path, "inherit", pages=2)
    service.find_icons(info.dir, suggest=False)
    dlg = IconReviewDialog(info.dir, service.icon_review(info.dir))
    i = next(k for k, g in enumerate(dlg.review["groups"]) if g["count"] > 1)
    g = dlg.review["groups"][i]
    dlg.widgets[i][1].setText("lily")
    dlg.widgets[i][3].setText("백합")
    occ = g["where"][0]["occ"]
    box = service.icon_place(info.dir, occ)["box"]
    r = service.set_icon_box(info.dir, occ, [box[0], box[1], box[2] + 6, box[3] + 6])
    assert dlg._after_place(r) is (bool(r.get("new_group")))
    if r.get("new_group"):
        j = next(k for k, w in enumerate(dlg.widgets) if w[0] == r["group"])
        assert dlg.widgets[j][1].text() == "lily" and dlg.widgets[j][3].text() == "백합"


# ---------------------------------------------------------------- 3A: 줄을 골라도 가로 스크롤 그대로
def test_row_table_keeps_horizontal_scroll(qapp):
    from PySide6.QtWidgets import QTableWidgetItem

    from app.qt.source_review import RowTable

    t = RowTable(3, 3)
    t.resize(200, 120)
    for r in range(3):
        for c in range(3):
            t.setItem(r, c, QTableWidgetItem("x" * (5 if c < 2 else 300)))
    t.resizeColumnsToContents()
    t.show()
    qapp.processEvents()
    t.horizontalScrollBar().setValue(0)
    t.setCurrentCell(2, 2)
    qapp.processEvents()
    assert t.horizontalScrollBar().value() == 0
    t.close()


# ---------------------------------------------------------------- 디버그 모드: 버튼 크기 유지
def test_debug_mode_keeps_button_width(qapp):
    from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget

    from app.qt.ui_codes import UiCodes

    w = QWidget()
    lay = QHBoxLayout(w)
    b = QPushButton("아주 긴 버튼 글자입니다 정말로")
    lay.addWidget(b)
    lay.addStretch(1)
    w.show()
    qapp.processEvents()
    width = b.width()
    ui = UiCodes.__new__(UiCodes)
    ui.debug = True
    ui._label(b)
    qapp.processEvents()
    assert b.text() != "아주 긴 버튼 글자입니다 정말로" and b.width() >= width
    ui._unlabel(b)
    assert b.text() == "아주 긴 버튼 글자입니다 정말로"
    w.close()


# ---------------------------------------------------------------- 그림 배경 칸: 비슷한 아이콘도 모두 따로
def _picture(fd, pages=(1, 2)):
    for p in pages:
        service.set_card_erase_mode(fd, p, "본문", "picture")


def test_picture_background_icons_are_not_grouped(tmp_path):
    info, fd = _project(tmp_path, "pic", pages=2)
    service.find_icons(info.dir, suggest=False)
    groups = [g for g in service.icon_review(info.dir)["groups"] if g["image"]]
    assert any(g["count"] == 2 for g in groups)  # 보통 칸: 같은 아이콘 두 쪽이 한 묶음
    _picture(fd)
    service.find_icons(info.dir, suggest=False)
    groups = [g for g in service.icon_review(info.dir)["groups"] if g["image"]]
    assert len(groups) == 2 and all(g["count"] == 1 for g in groups)
    scan = icons.load_scan(info.game_dir / "work" / "icons")
    assert all(o.unique for o in scan.occurrences if o.png)


def test_picture_background_skips_dictionary_auto_match(tmp_path):
    info, fd = _project(tmp_path, "picdict", pages=2)
    service.find_icons(info.dir, suggest=False)
    assert _confirm_all(info, "lily").state == "confirmed"
    _set_src(fd, "AB[[?]]CD", page=1)
    service.find_icons(info.dir, suggest=False)
    assert _src(fd, 1) == "AB[[lily]]CD"  # 보통 칸: 사전과 같아 바로 바뀜
    _set_src(fd, "AB[[?]]CD", page=1)
    _picture(fd, pages=(1,))
    service.find_icons(info.dir, suggest=False)
    assert _src(fd, 1) == "AB[[?]]CD"  # 그림 배경 칸: 자동으로 바꾸지 않는다
    rev = service.icon_review(info.dir)
    assert not any(g.get("dict") for g in rev["groups"])


def test_picture_background_same_suggestion_gets_new_name(tmp_path):
    info, fd = _project(tmp_path, "picname", pages=2)
    _picture(fd)
    service.find_icons(info.dir, suggest=False)
    work = info.game_dir / "work" / "icons"
    res = icons.load_scan(work)
    g1, g2 = [g for g, gi in res.groups.items() if gi.get("rep")]
    res.groups[g1]["suggest"] = res.groups[g2]["suggest"] = "lily"
    assert icons.resolve_suggested_name(info, res, g2) == "renamed"
    assert res.groups[g2]["suggest"] == "lily_2"


def test_picture_background_rebox_does_not_join_group(tmp_path):
    info, fd = _project(tmp_path, "picbox", pages=2)
    _picture(fd, pages=(1,))
    service.find_icons(info.dir, suggest=False)
    scan = icons.load_scan(info.game_dir / "work" / "icons")
    o1 = next(o for o in scan.occurrences if o.page == 1)
    o2 = next(o for o in scan.occurrences if o.page == 2)
    assert o1.unique and not o2.unique and o1.group != o2.group
    r = service.set_icon_box(info.dir, o2.id, o2.box)  # 보통 칸의 자리를 다시 잡아도 그림 배경 묶음에 들어가지 않는다
    scan = icons.load_scan(info.game_dir / "work" / "icons")
    assert {o.id: o.group for o in scan.occurrences}[o1.id] != r["group"]


# ---------------------------------------------------------------- 9A: 글자 위치 옮기기
def test_result_viewer_moves_text_offset(qapp, tmp_path, monkeypatch):
    from app.qt import bg_erase
    from app.qt.results import ResultViewer
    from tests.test_qt import _translated_project

    info, fd = _translated_project(tmp_path)
    service.erase_project(info.dir)
    service.compose_project(info.dir)
    v = ResultViewer([("카드/cards", fd / "translated")])
    v.regions.setCurrentRow(1)  # 첫 영역
    page, region = v._edit["page"], v._edit["region"]
    assert (v.off_x.value(), v.off_y.value()) == (0.0, 0.0) and not v.redo_btn.isEnabled()
    v.nudge_text(1, 0)
    v.nudge_text(0, -1)
    assert v._offset() == (1.0, -1.0) and v.redo_btn.isEnabled()
    ran = []
    monkeypatch.setattr(bg_erase, "run_exclusive", lambda *a, **k: ran.append(a[1]))
    v.save_edit()
    assert tuple(service.card_text_offset(fd, page, region)) == (1.0, -1.0) and ran
    v.close()


# ---------------------------------------------------------------- 리뷰: 숫자를 넣었던 아이콘 다시 열기·색 바꾸기 조건
def test_reopen_restores_number_and_skips_dict_when_not_absorbed(tmp_path):
    info, fd, occ = _named_project(tmp_path, "renum")
    d = icons.load_dictionary(info)
    d["icons"]["lily"]["number"] = "2"
    icons.save_dictionary(info, d)
    p = fd / "extract" / "pages" / "page-001.json"
    rec = json.loads(p.read_text(encoding="utf-8"))
    rec["regions"]["본문"]["icon_numbers"] = [{"index": 0, "name": "lily", "text": "2", "side": "before", "sep": ""},
                                            {"index": 9, "name": "x", "text": ""}]
    p.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    r = service.reopen_icon(info.dir, occ, 0)
    assert _src(fd).startswith("AB2[[?]]")
    rec = json.loads(p.read_text(encoding="utf-8"))
    assert all(e.get("index") != 0 for e in rec["regions"]["본문"].get("icon_numbers") or [])
    o = next(x for x in icons.load_scan(info.game_dir / "work" / "icons").occurrences if x.id == r["occ"])
    gi = icons.load_scan(info.game_dir / "work" / "icons").groups[r["group"]]
    if not o.number:  # 그림에 숫자를 넣지 못함: 사전 lily(숫자 2)를 미리 고르지 않는다 (확정이 막히지 않게)
        assert not gi.get("dict")


def test_swap_line_accents_only_when_clear():
    from app.typeset_compose import swap_line_accents

    src = "加える.<c1>(以降手札は1枚増加する)</c1>"
    line = {"color": "#888888", "bbox_pt": [0.0, 0.0, 100.0, 10.0]}
    small = {**line, "accents": [{"color": "#E03030", "x_pt": [(0.0, 5.0)]}]}  # 태그 밖 앞부분만 조금
    assert swap_line_accents([small], [src])[0]["color"] == "#888888"
    full = {**line, "color": "#E03030", "accents": [{"color": "#888888", "x_pt": [(0.0, 22.0)]}]}
    assert swap_line_accents([full], [src])[0]["color"] == "#888888"  # 회색이 태그 밖 글을 다 덮음: 바꾼다
    two = "<c1>A</c1>加える<c2>B</c2>"
    assert swap_line_accents([full], [two])[0]["color"] == "#E03030"  # 태그가 여러 가지면 그대로


# ---------------------------------------------------------------- 1.21.2: 조작 중 잠금, 원본 그대로 확인·되돌리기
def test_keep_cell_original_off_restores_only_keep_slots(tmp_path):
    """원본 그대로를 끄면 그것 때문에 ‘아이콘 아님’이 된 자리만 되돌린다 (사람이 따로 지운 자리는 그대로)."""
    info, fd = _project(tmp_path, "keepoff", pages=1)
    host, extra = _add_run_partner(info, fd)
    service.set_icon_not_icon(info.dir, extra)  # 사람이 먼저 지운 자리
    r = service.keep_cell_original(info.dir, host, True)
    assert r["not_icon"] == [host]
    r = service.keep_cell_original(info.dir, host, False)
    assert r["restored"] == [host]
    states = {it["occ"]: it["state"] for it in service.icon_place(info.dir, host)["items"]}
    assert states == {host: "found", extra: "not_icon"}
    gi = icons.load_scan(info.game_dir / "work" / "icons").groups
    assert not any(g.get("by_keep") for g in gi.values())


def test_place_dialog_locks_while_action_runs(qapp, tmp_path, monkeypatch):
    """2B 조작(삭제·합치기·되돌리기·원본 그대로 등)이 도는 동안 창 전체가 잠기고, 끝나면 풀린다."""
    from app.qt import icon_place as ip

    info, fd = _project(tmp_path, "busy", pages=1)
    host, extra = _add_run_partner(info, fd)
    g = next(x for x in service.icon_review(info.dir)["groups"] if any(w["occ"] == host for w in x["where"]))
    dlg = ip.IconPlaceDialog(info.dir, g, lambda grp: grp, lambda r: None)
    seen = []
    real = service.set_icon_not_icon

    def slow(project_dir, occ):
        seen.append((dlg.isEnabled(), dlg.keep_btn.isEnabled(), dlg.items.isEnabled()))
        return real(project_dir, occ)

    monkeypatch.setattr(service, "set_icon_not_icon", slow)
    from PySide6.QtCore import Qt
    for k in range(dlg.items.count()):
        it = dlg.items.item(k)
        it.setSelected(it.data(Qt.ItemDataRole.UserRole) == extra)
    dlg.del_btn.click()
    assert seen == [(False, False, False)]  # 도는 동안: 창·버튼·목록 모두 잠김
    assert dlg.isEnabled() and dlg.keep_btn.isEnabled() and dlg.items.isEnabled()  # 끝나면 풀림
    dlg.reject()


def test_keep_button_asks_before_turning_on(qapp, tmp_path, monkeypatch):
    from app.qt import icon_place as ip

    info, fd = _project(tmp_path, "keepask", pages=1)
    host, extra = _add_run_partner(info, fd)
    g = next(x for x in service.icon_review(info.dir)["groups"] if any(w["occ"] == host for w in x["where"]))
    dlg = ip.IconPlaceDialog(info.dir, g, lambda grp: grp, lambda r: None)
    asked = []
    monkeypatch.setattr(ip, "ask_choice", lambda *a, **k: (asked.append(a[2]), 1)[1])  # 취소
    dlg.keep_btn.click()
    assert asked and not service.cell_kept(info.dir, host)
    monkeypatch.setattr(ip, "ask_choice", lambda *a, **k: 0)  # 원본 그대로 두기
    dlg.keep_btn.click()
    assert service.cell_kept(info.dir, host) and dlg.keep_btn.text() == dlg.KEEP_UNDO_TEXT
    asked.clear()
    monkeypatch.setattr(ip, "ask_choice", lambda *a, **k: (asked.append(a[2]), 1)[1])
    dlg.keep_btn.click()  # 끌 때는 묻지 않고 되돌린다
    assert not asked and not service.cell_kept(info.dir, host)
    states = {it["occ"]: it["state"] for it in service.icon_place(info.dir, host)["items"]}
    assert states[host] == "found"
    dlg.reject()


def test_click_queued_during_action_is_dropped(qapp, tmp_path, monkeypatch):
    """조작이 도는 동안 쌓인 클릭(여기서는 ‘원본 그대로’)은 끝난 뒤 실행되지 않는다 (6쪽 칸이 통째로 아이콘 아님이 된 일)."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    from app.qt import icon_place as ip

    info, fd = _project(tmp_path, "queued", pages=1)
    host, extra = _add_run_partner(info, fd)
    g = next(x for x in service.icon_review(info.dir)["groups"] if any(w["occ"] == host for w in x["where"]))
    dlg = ip.IconPlaceDialog(info.dir, g, lambda grp: grp, lambda r: None)
    dlg.show()
    monkeypatch.setattr(ip, "ask_choice", lambda *a, **k: 0)  # 묻기가 나와도 ‘원본 그대로 두기’
    real = service.set_icon_not_icon

    def slow(project_dir, occ):
        c = QPointF(dlg.keep_btn.rect().center())
        for t in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
            QApplication.postEvent(dlg.keep_btn, QMouseEvent(t, c, c, Qt.MouseButton.LeftButton,
                                                             Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
        return real(project_dir, occ)

    monkeypatch.setattr(service, "set_icon_not_icon", slow)
    for k in range(dlg.items.count()):
        it = dlg.items.item(k)
        it.setSelected(it.data(Qt.ItemDataRole.UserRole) == extra)
    dlg.del_btn.click()
    QApplication.processEvents()
    assert not service.cell_kept(info.dir, host)
    dlg.reject()


def test_keep_off_does_not_undo_deliberate_merge(tmp_path):
    """원본 그대로로 아이콘 아님이 된 자리를 사람이 다른 아이콘에 합쳤으면, 원본 그대로를 꺼도 합친 것을 풀지 않는다."""
    info, fd = _project(tmp_path, "keepmerge", pages=1)
    host, extra = _add_run_partner(info, fd)
    service.keep_cell_original(info.dir, host, True)
    service.restore_icon(info.dir, host)
    service.absorb_icon_run(info.dir, host, [extra])
    r = service.keep_cell_original(info.dir, host, False)
    assert r["restored"] == [] and r["left"] == []
    states = {it["occ"]: it["state"] for it in service.icon_place(info.dir, host)["items"]}
    assert states == {host: "found", extra: "absorbed"}


def test_keep_off_legacy_cell_offers_restore(qapp, tmp_path, monkeypatch):
    """예전 버전에서 원본 그대로를 켠 칸(표시 없음): 끌 때 남은 아이콘 아님 자리를 되돌릴지 묻고, 고르면 되돌린다."""
    from app.qt import icon_place as ip

    info, fd = _project(tmp_path, "keeplegacy", pages=1)
    host, extra = _add_run_partner(info, fd)
    service.keep_cell_original(info.dir, host, True)
    sp = info.game_dir / "work" / "icons" / "scan.json"
    scan = json.loads(sp.read_text(encoding="utf-8"))
    for g in scan["groups"].values():
        g.pop("by_keep", None)  # 예전 데이터
    sp.write_text(json.dumps(scan, ensure_ascii=False), encoding="utf-8")
    g = next(x for x in service.icon_review(info.dir)["groups"] if any(w["occ"] == host for w in x["where"]))
    dlg = ip.IconPlaceDialog(info.dir, g, lambda grp: grp, lambda r: None)
    asked = []
    monkeypatch.setattr(ip, "ask_choice", lambda *a, **k: (asked.append(a[2]), 0)[1])
    assert dlg.keep_btn.text() == dlg.KEEP_UNDO_TEXT
    dlg.keep_btn.click()
    assert len(asked) == 1 and "2개" in asked[0]
    states = {it["occ"]: it["state"] for it in service.icon_place(info.dir, host)["items"]}
    assert states == {host: "found", extra: "lost"}
    dlg.reject()


# ---------------------------------------------------------------- 1.22.0: 원본 그대로 칸의 원문 확인 문구, 결과 보기 위치
def test_source_review_kept_cell_says_not_translated(qapp, tmp_path):
    """원본 그대로 칸: ‘다시 봐야 함’ 같은 문구가 아니라 ‘번역하지 않기로 함’과 사용자 선택·아이콘 그림 그대로를 알린다.
    글자도 있는 칸은 확인하라고 알린다."""
    from app.qt.source_review import SourceReviewDialog
    from tests.test_qt import _project_with_extract

    info, fd = _project_with_extract(tmp_path)
    files = service.project_overview(info.dir)["groups"][0]["files"]
    dlg = SourceReviewDialog(files, only_check=False)
    c0 = dlg.current()
    dlg.keep_btn.click()
    dlg.table.setCurrentCell(0, 0)
    t = dlg.info.text()
    assert "번역하지 않기로 함" in t and "사용자가" in t and "원본 그대로" in t
    assert ("글자도 있어" in t) == SourceReviewDialog.kept_with_text(dlg.current())
    assert SourceReviewDialog.kept_with_text({"keep_original": True, "source": "[[a]] / [[b]] 2"}) is False
    assert SourceReviewDialog.kept_with_text({"keep_original": True, "source": "いずれかを選択\n[[a]]"}) is True
    assert (c0["page"], c0["region"]) == (dlg.current()["page"], dlg.current()["region"])
    dlg.reject()


def test_result_viewer_nudge_buttons_show_arrows_and_preview_live(qapp, tmp_path, monkeypatch):
    """위치 화살표 버튼은 글자가 보일 만큼 넓고(테마 여백 없이), 누르면 저장 전에도 옮긴 자리로 미리 보기를 다시 그린다."""
    from app.qt.results import ResultViewer
    from tests.test_qt import _translated_project

    info, fd = _translated_project(tmp_path)
    service.erase_project(info.dir)
    service.compose_project(info.dir)
    v = ResultViewer([("카드/cards", fd / "translated")])
    v.show()
    v.regions.setCurrentRow(1)
    qapp.processEvents()
    for b in v.nudge_btns:  # 버튼 안 글자 칸(테마 여백을 뺀 곳)에 화살표가 들어간다
        assert b.width() >= 30 and b.sizeHint().width() <= b.width() + 2
    seen = []
    real = service.card_preview
    monkeypatch.setattr(service, "card_preview", lambda *a, **k: (seen.append(k.get("offset")), real(*a, **k))[1])
    v.nudge_text(1, 0)
    v._offset_timer.timeout.emit()  # (기다리지 않고 바로)
    assert seen and seen[-1] == (1.0, 0.0)
    png = real(fd, v._edit["page"], v._edit["region"], None, None, offset=(5.0, 0.0))
    assert png  # 옮긴 자리로도 그려진다
    v.close()


def test_result_viewer_live_preview_failure_does_not_pop_dialog(qapp, tmp_path, monkeypatch):
    """위치를 옮길 때 저절로 하는 미리 보기는 못 만들어도 알림 창을 띄우지 않고 아래 글로만 알린다
    (미리 보기 단추로 부른 것은 예전처럼 창으로)."""
    from app.qt import results
    from app.qt.results import ResultViewer
    from tests.test_qt import _translated_project

    info, fd = _translated_project(tmp_path)
    service.erase_project(info.dir)
    service.compose_project(info.dir)
    v = ResultViewer([("카드/cards", fd / "translated")])
    v.regions.setCurrentRow(1)
    popped = []
    monkeypatch.setattr(results, "show_error", lambda *a, **k: popped.append(a[1]))
    monkeypatch.setattr(results, "show_problem", lambda *a, **k: popped.append(a[1]))
    monkeypatch.setattr(service, "card_preview", lambda *a, **k: None)
    v.nudge_text(1, 0)
    v._offset_timer.timeout.emit()
    assert not popped and "미리 보기를 만들지 못했습니다" in v.edit_note.text()

    def boom(*a, **k):
        raise RuntimeError("깨짐")

    monkeypatch.setattr(service, "card_preview", boom)
    v._offset_timer.timeout.emit()
    assert not popped and "깨짐" in v.edit_note.text()
    v.preview_btn.click()  # 사람이 누른 미리 보기는 창으로 알린다
    assert popped == ["미리 보기를 만들지 못했습니다"]
    v.close()
