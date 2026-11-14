"""1.18.0 묶음: 아이콘 정의 뒤 ‘나중에’ 고른 칸 다시 읽기, 용어집 만들기 멈춤, 이름 겹침, 사전 값 지키기,
‘아이콘만 있음 · 원본 그대로’, 아이콘 정의 창의 사전 탭."""

from __future__ import annotations

import os

import pytest

from app import icons, service, workspace
from tests.test_batch_1170 import _confirm_all
from tests.test_icons import _add_run_partner, _project

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _changed_project(tmp_path, name="chg"):
    info, fd = _project(tmp_path, name, pages=2)
    service.find_icons(info.dir, suggest=False)
    scan = icons.load_scan(info.game_dir / "work" / "icons")
    o1 = next(o for o in scan.occurrences if o.page == 1)
    service.set_icon_box(info.dir, o1.id, o1.box)  # 1쪽: 사람이 자리를 다시 잡음
    assert _confirm_all(info).state == "confirmed"
    return info, fd


# ---------------------------------------------------------------- 1. ‘나중에 (원문 확인에서)’ → 원문 확인에서 다시 읽기
def test_changed_cells_wait_for_reread_until_read(tmp_path, monkeypatch):
    info, fd = _changed_project(tmp_path)
    pending = service.icon_reread_pending([fd])
    assert [(c["page"], c["region"]) for c in pending] == [(1, "본문")]

    def fake_read(client, proj, page, src_doc, model_id, out_dir, regs):
        return {regs[0].name: {"source": "AB[[lily]]CD", "uncertain": False}}, 0.0, 0.0

    monkeypatch.setattr("app.extract._read_regions_once", fake_read)
    r = service.reread_cells(pending, client=object())
    assert r["done"] == 1
    assert service.icon_reread_pending([fd]) == []  # 다시 읽었으니 원문 확인을 열 때 또 읽지 않는다
    c = next(c for c in service.extracted_cells(fd) if c["page"] == 1)
    assert c["icon_changed"]  # 사람이 보고 저장하기 전까지는 ‘확인 필요’


def test_review_sources_rereads_pending_before_opening(qapp, tmp_path, monkeypatch):
    from app.qt import steps
    from app.qt.main import MainWindow

    info, fd = _changed_project(tmp_path, "rv")
    win = MainWindow(root=tmp_path)
    win.open_project(info.dir)
    acts = win.project.steps
    ran, opened = {}, []
    monkeypatch.setattr(acts, "_engine", lambda: "api")

    def fake_run(parent, title, job, on_done, on_failed=None, cancellable=True, **_kw):
        ran["fn"], ran["cells"] = job.fn, job.args[0]
        on_done({"done": 1, "failed": [], "state": "done", "reason": "", "retry_after": None, "spent_usd": 0.0})

    monkeypatch.setattr(steps.worker, "run_with_dialog", fake_run)
    monkeypatch.setattr(acts, "_open_source_review", lambda files, *a: opened.append(a))
    acts.review_sources(only_check=True)
    assert ran["fn"] is service.reread_cells and [(c["page"]) for c in ran["cells"]] == [1]
    assert opened == [(True, None, None)]


# ---------------------------------------------------------------- 7. 용어집 만들기: 아이콘이 바뀐 칸이 남으면 원문 확인으로
def test_finish_lists_icon_changed_cells(tmp_path):
    info, fd = _changed_project(tmp_path, "fin")
    r = service.finish_extraction(info.dir, client=object())
    assert r.state == "needs_confirmation"
    assert any(i.startswith("아이콘이 바뀐 칸 (③ 원문 확인 필요): ") and i.endswith(" 1칸") for i in r.issues)


def test_finish_after_icon_review_does_not_continue_when_cells_changed(qapp, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.qt import steps
    from app.qt.main import MainWindow

    info, _fd = _project(tmp_path, "fin2", pages=1)
    win = MainWindow(root=tmp_path)
    win.open_project(info.dir)
    acts = win.project.steps
    finished = []
    monkeypatch.setattr(steps.QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(service, "icon_review", lambda _p: {"groups": [1]})
    monkeypatch.setattr(acts, "finish", lambda accept=False: finished.append(accept))
    changed = SimpleNamespace(changed_cells=[{"page": 1}])
    monkeypatch.setattr(acts, "_open_icon_review", lambda review, from_finish=False: changed)
    acts._finish_done(service.GlossaryReviewResult("icons_review", reason="x"))
    assert finished == []  # 원문 확인을 다시 해야 하므로 용어집 만들기로 이어 가지 않는다
    monkeypatch.setattr(acts, "_open_icon_review", lambda review, from_finish=False: SimpleNamespace(changed_cells=[]))
    acts._finish_done(service.GlossaryReviewResult("icons_review", reason="x"))
    assert finished == [True]


# ---------------------------------------------------------------- 3. 제안 이름이 겹칠 때
def _scan_with_two(tmp_path):
    info, fd = _project(tmp_path, "nm", pages=1)
    host, extra = _add_run_partner(info, fd)
    work = info.game_dir / "work" / "icons"
    return info, work, host, extra


def test_suggested_name_same_dictionary_icon_is_registered_as_same(tmp_path):
    import shutil

    info, fd = _project(tmp_path, "same", pages=2)
    service.find_icons(info.dir, suggest=False)
    work = info.game_dir / "work" / "icons"
    shutil.copytree(work, tmp_path / "scan_copy")  # 확정하면 찾기 결과가 지워지므로 같은 그림의 결과를 남겨 둔다
    assert _confirm_all(info, "lily").state == "confirmed"  # 사전에 lily
    shutil.copytree(tmp_path / "scan_copy", work, dirs_exist_ok=True)
    res = icons.load_scan(work)
    g = next(iter(res.groups))
    res.groups[g]["suggest"] = "lily"
    res.groups[g].pop("dict", None)
    assert icons.resolve_suggested_name(info, res, g) == "same_dict"
    assert res.groups[g]["dict"] == "lily" and res.groups[g]["suggest"] == "lily"


def test_suggested_name_different_icon_gets_new_name(tmp_path, monkeypatch):
    info, fd = _project(tmp_path, "diff", pages=1)
    service.find_icons(info.dir, suggest=False)
    work = info.game_dir / "work" / "icons"
    res = icons.load_scan(work)
    g = next(iter(res.groups))
    res.groups[g]["suggest"] = "coin"
    monkeypatch.setattr(icons, "load_dictionary", lambda _i: {"icons": {"coin": {}, "coin_2": {}}})
    monkeypatch.setattr(icons, "_known_shapes", lambda _i: {})  # 사전 coin은 그림이 다름 (모양 없음)
    assert icons.resolve_suggested_name(info, res, g) == "renamed"
    assert res.groups[g]["suggest"] == "coin_3"


# ---------------------------------------------------------------- 사전과 같다고 고른 줄은 사전 값을 덮어쓰지 않는다
def test_keep_dict_row_does_not_overwrite_dictionary(tmp_path):
    info, fd = _project(tmp_path, "kd", pages=2)
    service.find_icons(info.dir, suggest=False)
    assert _confirm_all(info, "lily").state == "confirmed"
    service.update_icon_dictionary(info.dir, [{"name": "lily", "ko": "백합꽃", "meaning": "百合", "scale": 1.6,
                                               "reset": False}])
    for p in (1, 2):
        f = fd / "extract" / "pages" / f"page-{p:03d}.json"
        f.write_text(f.read_text(encoding="utf-8").replace("[[lily]]", "[[?]]"), encoding="utf-8")
    service.find_icons(info.dir, suggest=False)
    rev = service.icon_review(info.dir)
    rows = [{"group": g["group"], "name": "lily", "meaning": "", "ko": "", "not_icon": False, "text": "",
             "scale": None, "keep_dict": True} for g in rev["groups"]]
    assert service.confirm_icons(info.dir, rows).state == "confirmed"
    e = icons.load_dictionary(info)["icons"]["lily"]
    assert e["ko"] == "백합꽃" and float(e["scale"]) == 1.6


# ---------------------------------------------------------------- 5. 아이콘만 있음 · 원본 그대로
def test_keep_original_in_result_view_info_and_review_rows(tmp_path):
    info, fd = _project(tmp_path, "keep", pages=1)
    assert service.card_edit_info(fd, 1, "본문")["keep_original"] is False
    service.set_keep_original(fd, 1, "본문", True)
    assert service.card_edit_info(fd, 1, "본문")["keep_original"] is True
    service.set_keep_original(fd, 1, "본문", False)
    assert service.card_edit_info(fd, 1, "본문")["keep_original"] is False


def test_review_dialog_keep_button_sets_keep_original(qapp, tmp_path, monkeypatch):
    from app.qt import review as review_mod

    info, fd = _project(tmp_path, "rk", pages=1)
    calls = []
    monkeypatch.setattr(review_mod.service, "set_keep_original", lambda *a: calls.append(a))
    dlg = review_mod.TranslationReviewDialog.__new__(review_mod.TranslationReviewDialog)
    row = {"id": "x#p1#본문", "file_dir": fd, "page": 1, "region": "본문"}
    loaded = []
    dlg.current_row = lambda: row
    dlg.view_ids = ["x#p1#본문", "y"]
    dlg.edit = type("E", (), {"toPlainText": lambda self: ""})()
    dlg.load = lambda keep_id=None: loaded.append(keep_id)
    review_mod.TranslationReviewDialog.keep_original(dlg)
    assert calls == [(fd, 1, "본문", True)] and loaded == ["y"]


# ---------------------------------------------------------------- 4. 아이콘 정의 창: 사전 전체 탭
def test_icon_review_has_dictionary_tab_without_reset(qapp, tmp_path):
    from app.qt.icon_review import IconReviewDialog

    info, fd = _project(tmp_path, "tab", pages=2)
    service.find_icons(info.dir, suggest=False)
    assert _confirm_all(info, "lily").state == "confirmed"
    f = fd / "extract" / "pages" / "page-001.json"
    f.write_text(f.read_text(encoding="utf-8").replace("AB[[lily]]CD", "AB[[lily]]CD[[?]]"), encoding="utf-8")
    dlg = IconReviewDialog(info.dir, service.icon_review(info.dir) or {"groups": [], "known": ["lily"]})
    assert dlg.tabs.count() == 2 and dlg.tabs.tabText(1).startswith("사전 전체 (1)")
    panel = dlg.dict_panel
    assert not panel.resets[0].isEnabled()  # 새 아이콘을 정하는 중에는 다시 정하기를 막는다
    panel.table.item(0, 2).setText("흰 백합")
    assert panel.dirty and dlg.save_dictionary() and not panel.dirty
    assert icons.load_dictionary(info)["icons"]["lily"]["ko"] == "흰 백합"
    dlg.close()
    assert workspace.load_project_info(info.dir)


def test_not_icon_does_not_send_suggested_meaning_as_text(qapp, tmp_path):
    """‘아이콘 아님’을 체크하면 모델이 제안한 뜻을 그 자리 글자로 보내지 않는다 (원문에 뜻 문장이 들어가던 문제)."""
    from app.qt.icon_review import IconReviewDialog

    info, fd = _project(tmp_path, "ni", pages=1)
    service.find_icons(info.dir, suggest=False)
    work = info.game_dir / "work" / "icons"
    res = icons.load_scan(work)
    g = next(iter(res.groups))
    res.groups[g]["meaning"] = "social crown-shaped icon"
    icons._save_scan(work, res)
    dlg = IconReviewDialog(info.dir, service.icon_review(info.dir))
    _g, name, meaning, ko, not_icon, scale, same = dlg.widgets[0]
    assert meaning.text() == "social crown-shaped icon"
    not_icon.setChecked(True)
    assert meaning.text() == "" and dlg.rows()[0]["text"] == ""
    not_icon.setChecked(False)
    assert meaning.text() == "social crown-shaped icon"
    dlg.close()


def test_keep_original_clears_icon_changed_flags(tmp_path):
    """원본 그대로 둘 칸은 ‘아이콘이 바뀐 칸’ 확인·다시 읽기에서 빠진다 (용어집 만들기 경고가 풀리지 않던 문제)."""
    info, fd = _changed_project(tmp_path, "kc")
    service.set_keep_original(fd, 1, "본문", True)
    assert service.icon_reread_pending([fd]) == []
    c = next(c for c in service.extracted_cells(fd) if c["page"] == 1)
    assert not c["icon_changed"]


def test_chained_same_row_to_dictionary_keeps_dictionary_values(qapp, tmp_path):
    """‘3번과 같음’ → 3번이 ‘사전: X’이면 그 줄도 사전 값을 덮어쓰지 않는다."""
    from app.qt.icon_review import IconReviewDialog

    info, fd = _project(tmp_path, "ch", pages=1)
    host, extra = _add_run_partner(info, fd)
    dlg = IconReviewDialog(info.dir, {**service.icon_review(info.dir), "known": ["lily"]})
    w0, w1 = dlg.widgets[0], dlg.widgets[1]
    def pick(combo, data):  # findData는 튜플 값을 찾지 못한다 (PySide6)
        combo.setCurrentIndex(next(k for k in range(combo.count()) if combo.itemData(k) == data))

    pick(w0[6], ("known", "lily"))
    pick(w1[6], ("row", 0))
    rows = dlg.rows()
    assert rows[0]["keep_dict"] and rows[1].get("keep_dict") and rows[1]["name"] == "lily"
    dlg.close()
