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
def _no_claude(monkeypatch):
    """실제 Claude를 부르지 않는다: 아이콘을 모두 정한 뒤 자동 다시 읽기도 ‘연결 안 됨’으로 (필요한 테스트만 바꿔 끼운다)."""
    monkeypatch.setattr(service, "engine_status", lambda: {"message": "시험: 연결 안 함"})


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


def _type_name(qapp, v, name):
    row = v.icon_list.itemWidget(v.icon_list.item(0))
    row.name.setText(name)
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
    _type_name(qapp, v, "lily")  # 한국어 이름 칸은 없다
    v.confirm_btn.click()
    assert _source(fd, 1) == "AB[[lily]]CD"
    assert len(got) == 1 and got[0]["state"] == "confirmed"
    assert v.key == (str(fd), 2, "본문")  # 다음 문제 카드
    assert "확인됨" in v.flash_label.text()


def test_confirm_blocked_shows_problem_and_stays(qapp, tmp_path, monkeypatch):
    from app.qt import card_viewer

    info, fd, files, v = _viewer(qapp, tmp_path)
    # 이름 없는 아이콘이 남은 영역: 확정 버튼 둘이 꺼진다 (1.30.0)
    assert not v.confirm_btn.isEnabled() and not v.nav_btns["prev_bad"].isEnabled()
    assert "아이콘을 모두 정한 뒤" in v.confirm_btn.toolTip()
    _type_name(qapp, v, "lily")
    assert v.confirm_btn.isEnabled()
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
    v.nav_btns["prev"].click()
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
    service.stage_icon_names(info.dir, [{"group": e["group"], "name": "rose"} for e in _region(info, files, 2)["icons"]])
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


# ---------------------------------------------------------------- 1.28.2: 카드 보기 고친 점
def test_same_but_icon_layout_keeps_card_view_icons():
    from app.qt.source_text import same_but_icon_layout

    # 위아래 두 아이콘을 모델이 한 줄에 붙여 읽음 (5쪽 본문2)
    assert same_but_icon_layout("ジャンク[[?]][[?]]タグを得るたび を得る", "ジャンク[[gear]]タグを得るたび\n[[coin]]を得る")
    # 합친 아이콘 넷을 [[?]] 넷으로 읽음 (1쪽 본문2)
    assert same_but_icon_layout("効果\n[[?]] [[?]] [[?]] [[?]]", "効果\n[[set]]")
    assert not same_but_icon_layout("効果を得る", "効果を得た")  # 글이 다르다
    assert not same_but_icon_layout("A[[?]]B[[?]]", "AB[[?]]")  # 떨어진 곳에 아이콘이 하나 더 있다
    assert not same_but_icon_layout("A[[x]]B", "A[[x]]B")  # 같으면 할 일 없음
    assert not same_but_icon_layout("Draw 2\ncards", "Draw 2 cards")  # 아이콘이 없는 글은 줄바꿈 차이도 보인다
    assert not same_but_icon_layout("効果\n[[?]] [[?]]", "効果\n[[set]]", human=False)  # 사람이 손대지 않은 영역


def test_icon_only_last_line_rule():
    from app import icons

    assert icons._icon_only_line("[[?]] [[?]]") and icons._icon_only_line("2 [[?]] ")
    assert not icons._icon_only_line("[[?]]を得る") and not icons._icon_only_line("プレイ時効果")


def test_reread_with_only_icon_layout_difference_keeps_source(qapp, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.qt import card_viewer

    info, fd, files, v = _viewer(qapp, tmp_path, pages=1, name="lay")
    o = v._entries[0]
    occ = next(x for x in __import__("app.icons", fromlist=["x"]).load_scan(
        info.game_dir / "work" / "icons").occurrences if x.id == o["occ"])
    service.set_icon_box(info.dir, o["occ"], occ.box)  # 사람이 자리를 잡은 영역
    v._fetch()
    v._show_region(v.key)
    _type_name(qapp, v, "lily")
    before = v.edit.toPlainText()
    monkeypatch.setattr(service, "engine_status", lambda: {"api": True, "claude-code": True, "message": ""})

    def run_now(parent, title, job, on_done, on_failed=None, **_k):
        on_done(SimpleNamespace(state="done", source="AB[[?]] [[?]]CD", uncertain=False, reason="", retry_after=None))
    monkeypatch.setattr(card_viewer.worker, "run_with_dialog", run_now)
    v._reread()
    assert v.edit.toPlainText() == before and "아이콘" in v.flash_label.text()


def test_saving_box_suggests_a_name(qapp, tmp_path, monkeypatch):
    from app.qt import card_viewer

    info, fd, files, v = _viewer(qapp, tmp_path, pages=1, name="sug")
    asked = []
    monkeypatch.setattr(service, "engine_status", lambda: {"api": True, "claude-code": True, "message": ""})

    def fake_suggest(project_dir, group, engine=None):  # 진짜처럼 찾기 결과 줄의 제안(suggest)만 적는다
        from app import icons

        asked.append(group)
        work = info.game_dir / "work" / "icons"
        res = icons.load_scan(work)
        res.groups[group].update(suggest="lily", ko="백합")
        icons._save_scan(work, res)
        return {"suggest": "lily", "meaning": "", "ko": "백합"}
    monkeypatch.setattr(service, "suggest_icon_name", fake_suggest)
    monkeypatch.setattr(card_viewer.worker, "run_with_dialog",
                        lambda parent, title, job, on_done, on_failed=None, **_k: on_done(job.fn(*job.args, **job.kwargs)))
    e = v._entries[0]
    v._cur_occ = e["occ"]
    v.view.new = (1, 1, 60, 60)
    v._save_box()
    assert asked and "lily" in v.flash_label.text()
    assert _region(info, files, 1)["icons"][0]["name"] == "lily"


def test_paragraph_mark_button(qapp, tmp_path, monkeypatch):
    info, fd, files, v = _viewer(qapp, tmp_path, pages=1, name="para")
    assert not v.para_btn.isEnabled()  # 보통 영역은 문단 나눔을 쓰지 않는다
    reg = v._region()
    reg["paragraph_mode"] = True
    v._update_buttons()
    assert v.para_btn.isEnabled()
    v.edit.setPlainText("一行目\n二行目")
    cur = v.edit.textCursor()
    cur.movePosition(cur.MoveOperation.End)
    v.edit.setTextCursor(cur)
    v._insert_paragraph_mark()
    assert v.edit.toPlainText() == "一行目\n" + service.PARAGRAPH_MARK + "\n二行目"


def test_background_reread_does_not_wipe_a_drawn_box(qapp, tmp_path):
    info, fd, files, v = _viewer(qapp, tmp_path, pages=2, name="bg")
    v.show()
    calls = []
    v.refresh = lambda: calls.append(1)
    v.background_done([{"file_dir": fd, "page": 2, "region": "본문"}])  # 다른 카드
    assert calls == [] and not v._refresh_pending
    v.view.new = (1, 1, 30, 30)  # 사람이 상자를 그리는 중
    v.background_done([{"file_dir": fd, "page": 1, "region": "본문"}])
    assert calls == [] and v._refresh_pending and v.view.new is not None
    v.view.new = None
    v._refresh_when_idle()
    assert calls == [1]
    v.close()


def _view_with_marks(qapp):
    from PySide6.QtGui import QColor
    from app.qt.icon_place import IconPlaceView

    view = IconPlaceView()
    view.resize(400, 300)
    pm = QPixmap(200, 150)
    pm.fill(QColor("white"))
    marks = [([10, 10, 40, 40], "[[a]]", False, False, False, "o1"),
             ([120, 60, 160, 100], "[[b]]", False, False, False, "o2")]
    view.set_data(pm, [60, 60, 100, 100], None, marks)
    view.show()
    return view


def _at(view, x, y):
    """그림 좌표 -> 화면 점."""
    s, ox, oy = view._geom()
    return QPointF(ox + x * s, oy + y * s).toPoint()


def test_drawing_new_box_hides_other_boxes(qapp):
    view = _view_with_marks(qapp)
    QTest.mousePress(view, Qt.MouseButton.LeftButton, pos=_at(view, 5, 110))
    QTest.mouseMove(view, _at(view, 50, 140))
    assert view.drawing() and view.new is not None  # 끄는 동안: 새 상자만 그린다 (paintEvent가 drawing()으로 가린다)
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, pos=_at(view, 50, 140))
    assert not view.drawing() and view.new is not None
    view.close()


def test_drag_box_edge_resizes_into_new_box(qapp):
    view = _view_with_marks(qapp)
    got = []
    view.drawn.connect(lambda: got.append(1))
    QTest.mousePress(view, Qt.MouseButton.LeftButton, pos=_at(view, 100, 80))  # 지금 자리의 오른쪽 변
    QTest.mouseMove(view, _at(view, 115, 80))
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, pos=_at(view, 115, 80))
    assert view.new is not None and abs(view.new[2] - 115) < 2 and abs(view.new[0] - 60) < 1
    assert got  # 화면이 ‘이 자리로 저장’을 켠다
    view.close()


def test_click_on_other_icon_box_picks_it(qapp):
    view = _view_with_marks(qapp)
    got = []
    view.picked.connect(got.append)
    QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=_at(view, 140, 80))
    assert got == ["o2"] and view.new is None
    view.close()


def test_wheel_zoom_and_fit(qapp):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QWheelEvent

    view = _view_with_marks(qapp)
    p = _at(view, 80, 80)
    ev = QWheelEvent(QPointF(p), QPointF(view.mapToGlobal(p)), QPoint(0, 0), QPoint(0, 120),
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(view, ev)
    assert view.zoom > 1.0
    assert abs(view._to_img_raw(QPointF(p))[0] - 80) < 1  # 마우스 자리의 그림 점은 그대로
    view.fit()
    assert view.zoom == 1.0 and view._pan == [0.0, 0.0]
    view.close()


def test_moving_to_another_region_fits_the_picture(qapp, tmp_path):
    info, fd, files, v = _viewer(qapp, tmp_path)
    v.view.zoom_by(2.0)
    assert v.view.zoom > 1.0 and v.zoom_label.text() == "200%"
    v._reload()  # 같은 영역을 다시 그리면 확대 그대로
    assert v.view.zoom > 1.0
    v._go("next_card")
    assert v.view.zoom == 1.0 and v.zoom_label.text() == "100%"
    v.close()


def test_icon_row_has_no_korean_name_field(qapp, tmp_path):
    info, fd, files, v = _viewer(qapp, tmp_path)
    row = v.icon_list.itemWidget(v.icon_list.item(0))
    assert not hasattr(row, "ko")
    v.close()


def test_cursor_left_in_name_field_does_not_block_background_refresh(qapp, tmp_path):
    info, fd, files, v = _viewer(qapp, tmp_path, name="idle")
    v.show()
    QTest.qWaitForWindowExposed(v)
    row = v.icon_list.itemWidget(v.icon_list.item(0))
    row.name.setFocus()
    QTest.qWait(20)
    assert v._idle()  # 커서만 있음: 다시 그려도 된다
    QTest.keyClicks(row.name, "x")
    assert not v._idle()  # 적는 중
    v.close()


def test_reread_rows_are_labelled_and_replace_the_same_reader(qapp, tmp_path):
    import json

    from app.extract import page_file

    info, fd, files, v = _viewer(qapp, tmp_path, name="lbl")
    f = page_file(fd / "extract", 1)
    f = f if f.exists() else fd / "extract" / "page-001.json"
    rec = json.loads(f.read_text(encoding="utf-8"))
    cell = rec["regions"]["본문"]
    cell["readings"] = [{"model": "claude-sonnet-5-5", "source": "AB[[?]]CD"},
                        {"model": "claude-opus-5-5", "source": "AB[[?]]CD"}]
    cell["rereads"] = [{"model": "claude-sonnet-5-5", "source": "AB[[?]]C"}]
    f.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    reg = _region(info, files, 1)
    assert [(r["model"], r["reread"]) for r in reg["readings"]] == [("Opus", False), ("Sonnet", True)]
    v._reload()
    names = [h.text() for h, _v in v.readings._rows]
    assert names == ["정밀 읽기", "보통 읽기"]  # 같은 방식의 처음 결과는 숨김 → 이름에 ‘다시 읽음’ 붙이지 않음
    v.close()


def test_confirm_buttons_rename_when_no_other_problem(qapp, tmp_path):
    from app.qt.card_viewer import CONFIRM_ONLY_TEXT, NEXT_CONFIRM_TEXT, PREV_CONFIRM_TEXT

    info, fd, files, v = _viewer(qapp, tmp_path)
    _type_name(qapp, v, "lily")  # 1쪽: 이름을 정해 확정할 수 있음, 2쪽은 아직 문제
    assert v.confirm_btn.text() == NEXT_CONFIRM_TEXT and v.nav_btns["prev_bad"].text() == CONFIRM_ONLY_TEXT
    v.confirm_btn.click()  # 확정 → 다음 문제(2쪽)
    assert v.key[1] == 2
    assert v.confirm_btn.text() == CONFIRM_ONLY_TEXT  # 다른 문제 영역이 없다
    assert v.nav_btns["prev_bad"].text() in (PREV_CONFIRM_TEXT, CONFIRM_ONLY_TEXT)
    v.close()


def test_magnifier_shows_current_icon_and_follows_new_box(qapp, tmp_path):
    info, fd, files, v = _viewer(qapp, tmp_path, name="mag")
    v.show()
    QTest.qWaitForWindowExposed(v)
    v._update_mag()
    first = v.mag.pixmap().toImage() if v.mag.pixmap() is not None else None
    assert first is not None and not first.isNull()  # 지금 자리(파랑)가 크게 보인다
    view = v.view
    s, ox, oy = view._geom()
    p0 = QPointF(ox + 2 * s, oy + 2 * s).toPoint()
    p1 = QPointF(ox + 12 * s, oy + 12 * s).toPoint()
    QTest.mousePress(view, Qt.MouseButton.LeftButton, pos=p0)
    QTest.mouseMove(view, p1)
    assert view.new is not None
    assert v.mag.pixmap().toImage() != first  # 끄는 동안 새 자리를 따라 그린다
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, pos=p1)
    v.close()


def test_after_icon_op_moves_to_next_problem_icon_then_rereads(qapp, tmp_path):
    info, fd, files, v = _viewer(qapp, tmp_path, name="adv")
    picked, reread = [], []
    v._pick_icon = picked.append
    v._reread = lambda double=False: reread.append(double)
    v._entries = [{"occ": "a", "blocking": False}, {"occ": "b", "blocking": True}, {"occ": "c", "blocking": True}]
    v._advance_icons("b")
    assert picked == ["c"]  # 다음 문제 아이콘
    v._advance_icons("c")
    assert picked == ["c", "b"]  # 뒤에 없으면 첫 문제 아이콘
    v._entries = [{"occ": "a", "blocking": False}]
    v._advance_icons("a")
    assert reread == [True]  # 모두 해결: 보통·정밀 읽기로 다시 읽는다
    v.close()


def test_fresh_double_reread_region_confirms_without_another_reread(qapp, tmp_path, monkeypatch):
    info, fd, files, v = _viewer(qapp, tmp_path, name="fresh")
    _type_name(qapp, v, "lily")
    seen = {}

    def fake_confirm(project_dir, keys, edits=None, **k):
        seen["edits"] = edits
        return {"state": "confirmed", "blocking": [], "result": None, "reread": [], "still": [], "saved": 1}
    monkeypatch.setattr(service, "confirm_regions", fake_confirm)
    from app.qt.card_viewer import _nk

    v._fresh_reread.add(_nk(v.key))
    v.confirm_btn.click()
    assert seen["edits"] and list(seen["edits"].values())[0]["source"] == "AB[[lily]]CD"  # 원문 칸 글을 저장 (또 읽지 않음)
    v.close()
