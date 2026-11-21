"""② 원문 확인 목록 창: 카드·영역 트리, 거르기, 아이콘 종류 탭(확정 전), 모두 확정, 뒤에서 다시 읽기."""

from __future__ import annotations

import os
import sys
import types

import pytest

from app import icons, service
from tests.test_icons import _project, _source

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROLE = 256  # Qt.UserRole


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _no_modal(monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.No)


def _files(fd):
    return [{"dir": fd, "key": "카드/cards"}]


def _open(tmp_path, name, pages=2, **kw):
    from app.qt.source_check import SourceCheckDialog

    info, fd = _project(tmp_path, name, pages=pages)
    service.find_icons(info.dir, suggest=False)
    dlg = SourceCheckDialog(info.dir, _files(fd), engine="claude", **kw)
    return info, fd, dlg


def _heads(dlg):
    return [dlg.tree.topLevelItem(i) for i in range(dlg.tree.topLevelItemCount())]


def _group(info, page):
    scan = icons.load_scan(info.game_dir / "work" / "icons")
    return next(o.group for o in scan.occurrences if o.page == page)


def _name_kind(dlg, name="lily", ko="백합"):
    w = dlg.kind_widgets[0]
    w["name"].setText(name)
    w["name"].editingFinished.emit()
    w["ko"].setText(ko)
    w["ko"].editingFinished.emit()


def test_tree_lists_problem_cards_first_and_filters(qapp, tmp_path):
    info, fd, dlg = _open(tmp_path, "tree", pages=3)
    heads = _heads(dlg)
    assert len(heads) == 3 and "1쪽" in heads[0].text(0)  # 모두 고칠 것: 쪽 순서
    assert heads[0].isExpanded() and heads[0].childCount() == 1
    assert "고칠 것" in heads[0].child(0).text(0) and "AB[[?]]CD" in heads[0].child(0).text(2)
    assert dlg.chips["fix"].text() == "고칠 것 3" and dlg.tabs.tabText(0) == "카드·영역 (3)"
    # 한 쪽만 이름을 정해 확정하면 문제 카드만 보기에서 빠진다
    service.stage_icon_names(info.dir, [{"group": _group(info, 1), "name": "lily", "ko": "백합"}])
    r = service.confirm_regions(info.dir, [(fd, 1, "본문")])
    assert r["state"] == "confirmed"
    dlg.refresh()
    assert len(_heads(dlg)) == 2 and "2쪽" in _heads(dlg)[0].text(0)
    dlg.mode_all.click()
    heads = _heads(dlg)
    assert len(heads) == 3 and "2쪽" in heads[0].text(0) and "1쪽" in heads[2].text(0)  # 문제 카드가 위
    assert not heads[2].isExpanded() and "모두 확인됨" in heads[2].text(2)
    dlg.search.setText("lily")
    dlg._rebuild_tree()
    assert len(_heads(dlg)) == 1
    dlg.close()


def test_refresh_keeps_expansion_and_selection(qapp, tmp_path):
    info, fd, dlg = _open(tmp_path, "keep", pages=2)
    dlg.mode_all.click()
    _heads(dlg)[1].setExpanded(False)
    dlg.tree.setCurrentItem(_heads(dlg)[0].child(0))
    dlg.refresh()
    heads = _heads(dlg)
    assert not heads[1].isExpanded() and heads[0].isExpanded()
    assert dlg.tree.currentItem().data(0, ROLE)[0] == "region"
    dlg.close()


def test_focus_arg_selects_card(qapp, tmp_path):
    info, fd, dlg = _open(tmp_path, "focus", pages=2)
    assert dlg.focus_on((str(fd), 2))
    cur = dlg.tree.currentItem()
    assert cur.data(0, ROLE)[0] == "card" and "2쪽" in cur.text(0)
    assert dlg.focus_on((str(fd), 2, "본문")) and dlg.tree.currentItem().data(0, ROLE)[0] == "region"
    dlg.close()


def test_kinds_tab_stage_name_makes_regions_pending_and_confirm_all_writes(qapp, tmp_path):
    info, fd, dlg = _open(tmp_path, "kinds", pages=2)
    assert dlg.tabs.tabText(1) == "아이콘 종류 (1)" and dlg.confirm_btn.text() == "모두 확정 (0)"
    assert not dlg.confirm_btn.isEnabled()
    assert "이름 없음" in dlg.kind_table.item(0, 5).text()
    _name_kind(dlg)
    assert dlg.confirm_btn.text() == "모두 확정 (2)" and dlg.confirm_btn.isEnabled()
    assert dlg.pending_label.text() == "확정 전 2개 영역"
    row = _heads(dlg)[0].child(0)
    assert "확정 전" in row.text(0) and "AB[[lily]]CD" in row.text(2)
    assert _source(fd, 1) == "AB[[?]]CD"  # 확정 전: 원문은 그대로
    dlg.confirm_btn.click()
    assert _source(fd, 1) == "AB[[lily]]CD" and _source(fd, 2) == "AB[[lily]]CD"
    assert dlg.confirm_btn.text() == "모두 확정 (0)" and dlg.saved
    assert dlg.chips["fix"].text() == "고칠 것 0"
    dlg.close()


def test_show_groups_filters_tree_and_clear(qapp, tmp_path):
    info, fd, dlg = _open(tmp_path, "grp", pages=2)
    dlg.tabs.setCurrentIndex(1)
    dlg.kind_table.cellWidget(0, 4).findChild(type(dlg.group_clear)).click()
    assert dlg.tabs.currentIndex() == 0 and not dlg.group_label.isHidden() and len(_heads(dlg)) == 2
    dlg.group_clear.click()
    assert dlg.group_label.isHidden()
    dlg.close()


def test_not_icon_checkbox_stages(qapp, tmp_path):
    info, fd, dlg = _open(tmp_path, "noticon", pages=1)
    dlg.kind_widgets[0]["not_icon"].setChecked(True)
    assert service.icon_kinds(info.dir)[0]["not_icon"] and not dlg.kind_widgets[0]["name"].isEnabled()
    dlg.close()


def _force_reread(info):
    scan = icons.load_scan(info.game_dir / "work" / "icons")
    o = scan.occurrences[0]
    service.set_icon_box(info.dir, o.id, o.box)


def test_confirm_queues_reread_in_background(qapp, tmp_path, monkeypatch):
    from app.qt import source_check

    info, fd, dlg = _open(tmp_path, "reread", pages=1)
    _force_reread(info)
    seen = []

    def fake_reread(cells, engine=None, progress=None, cancel=None, **k):
        seen.append(([(c["page"], c["region"]) for c in cells], engine, len(dlg.reading)))
        return {"state": "done", "done": len(cells), "failed": [], "reason": "", "retry_after": None, "settled": 0}

    monkeypatch.setattr(service, "reread_cells", fake_reread)
    started = []
    monkeypatch.setattr(source_check.worker, "start", lambda job: started.append(job))
    dlg.refresh()
    _name_kind(dlg)
    dlg.confirm_btn.click()
    assert len(started) == 1 and len(dlg.reading) == 1
    assert dlg.chips["reading"].text() == "읽는 중 1" and "읽는 중" in _heads(dlg)[0].child(0).text(0)
    assert dlg.reading_keys() == {(str(fd), 1, "본문")}
    started[0].run()  # 작업 스레드 대신 바로
    assert seen == [([(1, "본문")], "claude", 1)]
    assert not dlg.reading and not dlg._running and dlg.reading_keys() == set()
    dlg.close()


def test_reread_failure_shows_problem_once(qapp, tmp_path, monkeypatch):
    from app.qt import source_check

    info, fd, dlg = _open(tmp_path, "fail", pages=1)
    problems = []
    monkeypatch.setattr(source_check, "show_problem", lambda *a, **k: problems.append((a, k)))
    monkeypatch.setattr(service, "reread_cells", lambda cells, **k: {
        "state": "paused", "done": 0, "failed": [("1쪽 본문", "한도")], "reason": "한도", "retry_after": None})
    monkeypatch.setattr(source_check.worker, "start", lambda job: job.run())
    dlg.enqueue_reread([{"file_dir": fd, "page": 1, "region": "본문"}])
    assert len(problems) == 1 and not dlg.reading and not dlg._running
    dlg.close()


def test_viewer_opens_at_region_and_confirmed_signal_queues(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import Signal
    from PySide6.QtWidgets import QDialog

    class FakeViewer(QDialog):
        changed = Signal()
        confirmed = Signal(dict)
        made = []

        def __init__(self, project_dir, rows_fn, start, parent=None, engine=None, reading_fn=None):
            super().__init__(parent)
            self.args = (rows_fn, start, engine, reading_fn)
            FakeViewer.made.append(self)

    mod = types.ModuleType("app.qt.card_viewer")
    mod.CardViewer = FakeViewer
    monkeypatch.setitem(sys.modules, "app.qt.card_viewer", mod)
    info, fd, dlg = _open(tmp_path, "viewer", pages=2)
    dlg.show()
    queued = []
    monkeypatch.setattr(dlg, "enqueue_reread", lambda cells: queued.append(cells))
    dlg._on_double(_heads(dlg)[1].child(0), 0)
    v = FakeViewer.made[-1]
    assert v.args[1] == (str(fd), 2, "본문") and v.args[2] == "claude"
    assert [c["page"] for c in v.args[0]()] == [1, 2] and v.args[3]() == set()
    dlg._open_card(_heads(dlg)[0].data(0, ROLE)[1])  # 카드 보기 = 카드의 첫 문제 영역
    assert len(FakeViewer.made) == 2 and FakeViewer.made[-1].args[1] == (str(fd), 1, "본문")
    v2 = FakeViewer.made[-1]
    v2.confirmed.emit({"reread": [{"file_dir": fd, "page": 1, "region": "본문"}]})
    assert queued == [[{"file_dir": fd, "page": 1, "region": "본문"}]] and dlg.saved
    v2.changed.emit()
    dlg.close()
    assert not v2.isVisible()


def test_close_while_reading_keeps_going(qapp, tmp_path, monkeypatch):
    from app.qt import source_check

    info, fd, dlg = _open(tmp_path, "close", pages=1)
    started = []
    monkeypatch.setattr(source_check.worker, "start", lambda job: started.append(job))
    monkeypatch.setattr(service, "reread_cells", lambda cells, **k: {
        "state": "done", "done": 1, "failed": [], "reason": "", "retry_after": None})
    dlg.enqueue_reread([{"file_dir": fd, "page": 1, "region": "본문"}])
    dlg.reject()
    started[0].run()  # 닫은 뒤에도 끝까지, 오류 없이
    assert not dlg.reading and dlg._closed


def test_project_selection_is_initial_file_filter(qapp, tmp_path):
    import shutil

    from app import workspace
    from app.qt.source_check import SourceCheckDialog
    from tests.test_icons import _project

    info, fd = _project(tmp_path, "scope", pages=1)
    pdf2 = tmp_path / "src" / "scope" / "cards2.pdf"
    shutil.copy(next((tmp_path / "src" / "scope").glob("cards.pdf")), pdf2)
    workspace.add_files(info, [pdf2], "카드", new_group=False)
    fd2 = next(d for d in workspace.file_dirs(info) if d != fd)
    service.find_icons(info.dir, suggest=False)
    files = [{"dir": fd, "key": "카드/cards"}, {"dir": fd2, "key": "카드/cards2"}]
    one = SourceCheckDialog(info.dir, files, scope=("파일 ‘카드/cards’", [fd]))
    assert one.file_combo.currentData() == str(fd)
    grp = SourceCheckDialog(info.dir, files, scope=("그룹 ‘카드’", [fd, fd2]))
    assert isinstance(grp.file_combo.currentData(), list) and len(grp.file_combo.currentData()) == 2
    allf = SourceCheckDialog(info.dir, files, scope=("전체", None))
    assert allf.file_combo.currentData() is None
    for d in (one, grp, allf):
        d.close()


def test_dictionary_reset_reopens_icon_and_resyncs_scan(qapp, tmp_path, monkeypatch):
    from app import icons, icons_stage
    from app.qt import source_check
    from app.qt.source_check import SourceCheckDialog
    from tests.test_icons import _project, _source

    info, fd = _project(tmp_path, "reset", pages=1)
    service.find_icons(info.dir, suggest=False)
    g = icons.load_scan(info.game_dir / "work" / "icons").occurrences[0].group
    service.stage_icon_names(info.dir, [{"group": g, "name": "lily", "ko": "백합"}])
    assert service.confirm_regions(info.dir, [(fd, 1, "본문")])["state"] == "confirmed"
    assert _source(fd, 1) == "AB[[lily]]CD"
    dlg = SourceCheckDialog(info.dir, [{"dir": fd, "key": "카드/cards"}])
    monkeypatch.setattr(source_check, "ask_choice", lambda *a, **k: 0)

    def run_now(parent, title, job, on_done, on_failed=None, **_k):
        assert job.kwargs.get("full_if_missing") is False  # 사전 저장에서 게임 전체를 다시 찾지 않는다
        on_done(job.fn(*job.args, **job.kwargs))
    monkeypatch.setattr(source_check.worker, "run_with_dialog", run_now)
    monkeypatch.setattr(dlg.dict_panel, "rows", lambda: [{"name": "lily", "ko": "백합", "meaning": "", "scale": 1.1,
                                                          "reset": True}])
    assert dlg.save_dictionary()
    assert _source(fd, 1) == "AB[[?]]CD"
    scan = icons.load_scan(info.game_dir / "work" / "icons")
    assert icons_stage.stale_regions(info, scan) == set()
    r = next(r for c in dlg._cards for r in c["regions"] if r["page"] == 1)
    assert r["status"] == "fix"
    dlg.close()
