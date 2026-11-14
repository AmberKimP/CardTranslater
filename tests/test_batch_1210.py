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
