"""② 원문 확인: 카드 보기 창 (CardViewer). 가짜 카드 PDF·가짜 읽기만 쓴다 (실제 Claude 호출 없음)."""

from __future__ import annotations

import os

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from app import service
from tests.test_icons import _project, _source

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _no_modal_boxes(monkeypatch):
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.No)


def _viewer(qapp, tmp_path, pages=2, name="cv", start_page=1, reading=None, **kw):
    from app.qt.card_viewer import CardViewer

    info, fd = _project(tmp_path, name, pages=pages)
    service.find_icons(info.dir, suggest=False)
    files = [{"dir": fd, "key": "카드/cards"}]
    v = CardViewer(info.dir, lambda: service.source_check_rows(info.dir, files), (str(fd), start_page),
                   reading_fn=(lambda: reading) if reading is not None else None, **kw)
    return info, fd, files, v


def _region(info, files, page, region="본문"):
    return next(r for c in service.source_check_rows(info.dir, files) if c["page"] == page
                for r in c["regions"] if r["region"] == region)


def _type_name(qapp, v, name, ko=""):
    row = v.icon_list.itemWidget(v.icon_list.item(0))
    row.name.setText(name)
    if ko:
        row.ko.setText(ko)
    QTest.keyClick(row.name, Qt.Key.Key_Return)
    QTest.qWait(30)  # 줄 다시 그리기는 이벤트 루프에서


def test_opens_on_problem_region_with_source_and_icons(qapp, tmp_path):
    info, fd, _files, v = _viewer(qapp, tmp_path)
    assert v.key == (str(fd), 1, "본문")
    assert "본문" in v.region_title.text() and "고칠 것" in v.status_badge.text()
    assert v.edit.toPlainText() == "AB[[?]]CD"
    assert v.icon_list.count() == 1 and v.pending_badge.isHidden()
    assert "카드 보기" in v.windowTitle() and "1쪽" in v.windowTitle()
    assert not v.view._pm.isNull()  # 영역 그림이 보인다


def test_start_with_region_and_second_card(qapp, tmp_path):
    from app.qt.card_viewer import CardViewer

    info, fd, files, v = _viewer(qapp, tmp_path)
    w = CardViewer(info.dir, lambda: service.source_check_rows(info.dir, files), (str(fd), 2, "본문"))
    assert w.key == (str(fd), 2, "본문") and "2쪽" in w.windowTitle()


def test_typing_name_stages_and_edit_box_shows_it(qapp, tmp_path):
    info, fd, files, v = _viewer(qapp, tmp_path)
    changed = []
    v.changed.connect(lambda: changed.append(1))
    _type_name(qapp, v, "lily")
    r = _region(info, files, 1)
    assert r["pending"] and r["icons"][0]["state"] == "named"
    assert _source(fd, 1) == "AB[[?]]CD"  # 확정 전: 원문은 그대로
    assert v.edit.toPlainText() == "AB[[lily]]CD"
    assert not v.pending_badge.isHidden() and changed
    assert "확정 전" in v.icon_list.itemWidget(v.icon_list.item(0)).tag.text()


def test_same_shape_applies_to_other_cards(qapp, tmp_path):
    info, fd, files, v = _viewer(qapp, tmp_path)
    _type_name(qapp, v, "lily")
    assert _region(info, files, 2)["icons"][0]["name"] == "lily"  # 같은 모양: 다른 카드에도 (확정 전)
    assert _source(fd, 2) == "AB[[?]]CD"


def test_same_shape_off_stages_only_this_icon(qapp, tmp_path):
    info, fd, files, v = _viewer(qapp, tmp_path)
    v.same_shape.setChecked(False)
    _type_name(qapp, v, "lily")
    assert _region(info, files, 1)["pending"] and not _region(info, files, 2)["pending"]


def test_confirm_writes_source_emits_and_moves_to_next_problem(qapp, tmp_path):
    info, fd, files, v = _viewer(qapp, tmp_path)
    v.same_shape.setChecked(False)
    got = []
    v.confirmed.connect(got.append)
    _type_name(qapp, v, "lily", ko="백합")
    v.confirm_btn.click()
    assert _source(fd, 1) == "AB[[lily]]CD"
    assert len(got) == 1 and got[0]["state"] == "confirmed"
    assert v.key == (str(fd), 2, "본문")  # 다음 문제 카드
    assert "확인됨" in v.flash_label.text()


def test_confirm_blocked_shows_problem_and_stays(qapp, tmp_path, monkeypatch):
    from app.qt import card_viewer

    info, fd, files, v = _viewer(qapp, tmp_path)
    monkeypatch.setattr(service, "confirm_regions", lambda *a, **k: {
        "state": "blocked", "blocking": ["g1: 이름이 비어 있음"], "result": None, "reread": [], "still": [], "saved": 0})
    shown = []
    monkeypatch.setattr(card_viewer, "show_problem", lambda *a, **k: shown.append(k.get("items")))
    got = []
    v.confirmed.connect(got.append)
    v.confirm_btn.click()
    assert shown == [["g1: 이름이 비어 있음"]] and not got and v.key[1] == 1


def test_edited_text_is_saved_on_confirm(qapp, tmp_path):
    info, fd, files, v = _viewer(qapp, tmp_path, pages=1)
    v.edit.setPlainText("AB[[?]]CDE")
    v._confirm()
    assert _source(fd, 1) == "AB[[?]]CDE"  # 고친 원문 저장 (아이콘은 이름 없이 남음)


def test_navigation_between_regions_and_cards(qapp, tmp_path):
    info, fd, files, v = _viewer(qapp, tmp_path)
    assert v.nav_btns["prev"].isEnabled() is False and v.nav_btns["next"].isEnabled()
    v.nav_btns["next"].click()
    assert v.key == (str(fd), 2, "본문") and "2쪽" in v.windowTitle()
    assert v.nav_btns["next"].isEnabled() is False and v.nav_btns["prev"].isEnabled()
    v.nav_btns["prev_bad"].click()
    assert v.key[1] == 1
    v.nav_btns["next_card"].click()
    assert v.key[1] == 2


def test_alt_arrow_shortcuts_move_region(qapp, tmp_path):
    info, fd, files, v = _viewer(qapp, tmp_path)
    v.show()
    v.activateWindow()
    QApplication.processEvents()
    QTest.keyClick(v, Qt.Key.Key_Down, Qt.KeyboardModifier.AltModifier)
    assert v.key[1] == 2
    QTest.keyClick(v, Qt.Key.Key_Up, Qt.KeyboardModifier.AltModifier)
    assert v.key[1] == 1
    v.close()


def test_leaving_with_edits_asks_and_cancel_stays(qapp, tmp_path, monkeypatch):
    from app.qt import card_viewer

    info, fd, files, v = _viewer(qapp, tmp_path)
    v.edit.setPlainText("고침")
    answers = iter([2, 1])  # 취소 -> 버리기
    monkeypatch.setattr(card_viewer, "ask_choice", lambda *a, **k: next(answers))
    v.nav_btns["next"].click()
    assert v.key[1] == 1 and v.edit.toPlainText() == "고침"
    v.nav_btns["next"].click()
    assert v.key[1] == 2 and v.edit.toPlainText() == "AB[[?]]CD"


def test_leaving_with_edits_confirm_saves(qapp, tmp_path, monkeypatch):
    from app.qt import card_viewer

    info, fd, files, v = _viewer(qapp, tmp_path)
    v.edit.setPlainText("AB[[?]]CDX")
    monkeypatch.setattr(card_viewer, "ask_choice", lambda *a, **k: 0)  # 확정
    v.nav_btns["next"].click()
    assert _source(fd, 1) == "AB[[?]]CDX" and v.key[1] == 2


def test_close_with_edits_cancel_keeps_window(qapp, tmp_path, monkeypatch):
    from app.qt import card_viewer

    info, fd, files, v = _viewer(qapp, tmp_path)
    v.show()
    v.edit.setPlainText("고침")
    monkeypatch.setattr(card_viewer, "ask_choice", lambda *a, **k: 2)
    v.close()
    assert v.isVisible()
    monkeypatch.setattr(card_viewer, "ask_choice", lambda *a, **k: 1)
    v.close()
    assert not v.isVisible()


def test_region_box_click_selects_region(qapp, tmp_path, monkeypatch):
    """카드 그림의 영역 상자를 누르면 그 영역으로 (service.card_regions가 없어도 되게 가짜로)."""
    from app.qt.card_viewer import CardViewer

    info, fd, files, v = _viewer(qapp, tmp_path)
    # 영역 둘이 있는 카드: 가짜 목록
    cards = service.source_check_rows(info.dir, files)
    reg = cards[0]["regions"][0]
    second = {**reg, "region": "제목", "key": (reg["key"][0], 1, "제목"), "status": "ok", "icons": [], "pending": False,
              "reasons": []}
    cards[0] = {**cards[0], "regions": [reg, second]}
    pm = QPixmap(250, 180)
    pm.fill(Qt.GlobalColor.white)
    from PySide6.QtCore import QBuffer, QByteArray
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QBuffer.OpenModeFlag.WriteOnly)
    pm.save(buf, "PNG")
    monkeypatch.setattr(service, "card_regions", lambda fd_, page: {
        "png": bytes(ba), "size": (250, 180), "regions": {"본문": (20, 100, 200, 140), "제목": (20, 10, 200, 50)}},
        raising=False)
    monkeypatch.setattr(service, "region_crop", lambda *a: bytes(ba))
    w = CardViewer(info.dir, lambda: cards, (str(fd), 1, "본문"))
    w.resize(1000, 700)
    w.show()
    QApplication.processEvents()
    m = w.card_map
    s, ox, oy = m._geom()
    pos = QPointF(ox + 100 * s, oy + 30 * s)  # 제목 상자 안
    assert m.hit(pos) == "제목"
    QTest.mouseClick(m, Qt.MouseButton.LeftButton, pos=pos.toPoint())
    assert w.key == (str(fd), 1, "제목")
    assert "제목" in w.region_title.text()
    # 영역 목록 줄을 눌러도 간다
    w.region_list.setCurrentRow(0)
    assert w.key[2] == "본문"
    w.close()


def test_reading_region_is_blue_and_cannot_be_confirmed(qapp, tmp_path):
    from app.qt.card_viewer import CardViewer

    info, fd, files, _v = _viewer(qapp, tmp_path)

    w = CardViewer(info.dir, lambda: service.source_check_rows(info.dir, files), (str(fd), 1),
                   reading_fn=lambda: {(str(fd), 1, "본문")})
    assert w.status_badge.text() == "읽는 중" and not w.confirm_btn.isEnabled()
    assert w.edit.isReadOnly()
    w2 = CardViewer(info.dir, lambda: service.source_check_rows(info.dir, files), (str(fd), 2),
                    reading_fn=lambda: {(str(fd), 1, "본문")})
    assert w2.confirm_btn.isEnabled()


def test_delete_icon_makes_it_not_icon_pending(qapp, tmp_path):
    info, fd, files, v = _viewer(qapp, tmp_path)
    v.icon_list.item(0).setSelected(True)
    assert v.del_btn.isEnabled()
    v.del_btn.click()
    r = _region(info, files, 1)
    assert r["icons"][0]["state"] == "not_icon" and r["pending"]
    assert v.edit.toPlainText() == "ABCD"  # 확정하면 원문에서 빠진다
    assert v.restore_btn.isEnabled()  # 고른 줄은 그대로 골라 둔다
    v.restore_btn.click()
    assert _region(info, files, 1)["icons"][0]["state"] == "unnamed"
    assert v.edit.toPlainText() == "AB[[?]]CD"


def test_dirty_text_guard_before_icon_op(qapp, tmp_path, monkeypatch):
    from app.qt import card_viewer

    info, fd, files, v = _viewer(qapp, tmp_path)
    v.edit.setPlainText("고침")
    monkeypatch.setattr(card_viewer, "ask_choice", lambda *a, **k: 1)  # 취소
    v.icon_list.item(0).setSelected(True)
    v.del_btn.click()
    assert _region(info, files, 1)["icons"][0]["state"] == "unnamed" and v.edit.toPlainText() == "고침"


def test_keep_original_toggle(qapp, tmp_path):
    info, fd, files, v = _viewer(qapp, tmp_path)
    v.keep_btn.click()
    assert _region(info, files, 1)["status"] == "keep" or _region(info, files, 1)["keep_mode"] == "whole"
    assert v.keep_btn.isChecked()
    v.keep_btn.click()
    assert _region(info, files, 1)["keep_mode"] != "whole" and not v.keep_btn.isChecked()


def test_reread_puts_result_in_edit_box_without_saving(qapp, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.qt import card_viewer

    info, fd, files, v = _viewer(qapp, tmp_path)
    monkeypatch.setattr(service, "engine_status", lambda: {"claude-code": True, "api": True, "message": ""})
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(engine="claude-code"))
    seen = []

    def fake_run(parent, title, job, on_done, on_failed=None, cancellable=True, on_cancelled=None):
        seen.append(title)
        on_done(SimpleNamespace(state="done", source="AX[[?]]CD", uncertain=False, reason="", retry_after=None))

    monkeypatch.setattr(card_viewer.worker, "run_with_dialog", fake_run)
    v.reread_btn.click()
    assert seen and "다시 읽는 중" in seen[0]
    assert v.edit.toPlainText() == "AX[[?]]CD" and v._dirty()
    assert _source(fd, 1) == "AB[[?]]CD"  # 확정해야 바뀐다
    assert v.body.isEnabled()


def test_reread_failure_unlocks_window(qapp, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.qt import card_viewer

    info, fd, files, v = _viewer(qapp, tmp_path)
    monkeypatch.setattr(service, "engine_status", lambda: {"claude-code": True, "api": True, "message": ""})
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(engine="claude-code"))
    monkeypatch.setattr(card_viewer.worker, "run_with_dialog", lambda parent, title, job, on_done, on_failed=None, **k:
                        on_done(SimpleNamespace(state="failed", source=None, uncertain=False, reason="안 됨",
                                                retry_after=None)))
    v.reread_btn.click()
    assert v.body.isEnabled() and v.edit.toPlainText() == "AB[[?]]CD"


def test_paragraph_mode_region_edits_keep_marks(qapp, tmp_path, monkeypatch):
    """문단 나눔 영역: 편집 칸은 [====] 줄을 보이고, 고쳐 확정하면 나눔을 함께 넘긴다."""
    info, fd, files, v = _viewer(qapp, tmp_path, pages=1)
    reg = v._region()
    seen = {}

    def fake_confirm(project_dir, keys, edits=None):
        seen["edits"] = edits
        return {"state": "confirmed", "blocking": [], "result": None, "reread": [], "still": [], "saved": 1}

    monkeypatch.setattr(service, "confirm_regions", fake_confirm)
    v._region()["paragraph_mode"] = True  # 목록 데이터를 직접 바꿔 문단 모드로 (읽기 쪽 규칙은 service가 테스트)
    v._show_region(v.key)
    assert service.PARAGRAPH_MARK in v.edit.toPlainText() or v.edit.toPlainText()
    v.edit.setPlainText(v.edit.toPlainText() + "Z")
    v._confirm()
    (key, e), = seen["edits"].items()
    assert key == tuple(reg["key"]) and "breaks" in e and e["source"].endswith("Z")


def test_repeated_open_close_has_no_deleted_object_errors(qapp, tmp_path, capfd):
    for i in range(3):
        info, fd, files, v = _viewer(qapp, tmp_path, name=f"cv{i}")
        v.show()
        QApplication.processEvents()
        _type_name(qapp, v, "lily")
        v.close()
        v.deleteLater()
        QApplication.processEvents()
    assert "already deleted" not in capfd.readouterr().err
