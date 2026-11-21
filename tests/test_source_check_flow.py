"""프로젝트 화면의 ‘② 원문 확인’ 버튼 흐름: 아이콘 찾기 결과 맞추기 → 목록 창 → 닫은 뒤 지웠던 파일 다시 지우기.
(1.27까지의 ② 아이콘 정의 → ③ 원문 확인 자동 연쇄·열 때 몰래 다시 읽기는 없다.)"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app import service, workspace
from tests.test_icons import _project

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _acts(tmp_path, name, pages=1):
    from app.qt.main import MainWindow

    info, fd = _project(tmp_path, name, pages=pages)
    win = MainWindow(root=tmp_path)
    win.open_project(info.dir)
    return info, fd, win.project.steps


class _FakeDlg:
    opened: list = []
    reading_after_exec: set = set()

    def __init__(self, project_dir, files, parent=None, engine=None, focus=None):
        _FakeDlg.opened.append({"files": [f["key"] for f in files], "focus": focus, "engine": engine})
        self.project_dir = project_dir
        self.reading: set = set()
        self.on_idle = None
        self.refreshed = 0

    def refresh(self):
        self.refreshed += 1

    def focus_on(self, key):
        pass

    def exec(self):
        self.reading = set(_FakeDlg.reading_after_exec)
        return 0


@pytest.fixture
def fake_window(monkeypatch):
    from app.qt import source_check

    _FakeDlg.opened = []
    _FakeDlg.reading_after_exec = set()
    monkeypatch.setattr(source_check, "SourceCheckDialog", _FakeDlg)
    return _FakeDlg.opened


def _run_now(monkeypatch, calls):
    from app.qt import worker

    def run(parent, title, job, on_done, on_failed=None, cancellable=True, **_kw):
        calls.append((job.fn, job.args, job.kwargs))
        try:
            r = job.fn(*job.args, **job.kwargs)
        except Exception as e:  # noqa: BLE001
            on_failed(str(e))
            return job
        on_done(r)
        return job

    monkeypatch.setattr(worker, "run_with_dialog", run)


def test_button_syncs_icon_scan_then_opens_window_for_all_files(qapp, tmp_path, monkeypatch, fake_window):
    info, fd, acts = _acts(tmp_path, "flow")
    calls = []
    _run_now(monkeypatch, calls)
    monkeypatch.setattr(service, "engine_status", lambda: {"claude-code": False, "api": False, "message": ""})
    acts.source_check()
    assert calls[0][0] is service.ensure_icon_scan and calls[0][2]["suggest"] is False  # 연결 없으면 제안 없이 찾기
    assert (info.game_dir / "work" / "icons" / "scan.json").exists()
    assert fake_window == [{"files": [workspace.file_key(fd)], "focus": None, "engine": calls[0][2]["engine"]}]
    # 두 번째: 원문이 그대로면 다시 찾지 않는다
    assert service.ensure_icon_scan(info.dir)["state"] == "ok"


def test_window_opens_even_if_icon_search_fails(qapp, tmp_path, monkeypatch, fake_window):
    from app.qt import steps

    _info, _fd, acts = _acts(tmp_path, "fail")
    _run_now(monkeypatch, [])
    problems = []
    monkeypatch.setattr(service, "ensure_icon_scan", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(steps, "show_problem", lambda *a, **k: problems.append(a))
    acts.source_check()
    assert len(problems) == 1 and len(fake_window) == 1


def test_finish_with_unnamed_icons_opens_source_check(qapp, tmp_path, monkeypatch):
    from app.qt import steps

    _info, _fd, acts = _acts(tmp_path, "fin")
    opened, finished = [], []
    monkeypatch.setattr(steps.QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(acts, "source_check", lambda **k: opened.append(k))
    monkeypatch.setattr(acts, "finish", lambda accept=False: finished.append(accept))
    acts._finish_done(service.GlossaryReviewResult("icons_review", reason="이름 없는 아이콘 1개"))
    assert opened == [{}] and finished == []  # 용어집 만들기로 이어 가지 않는다 (확정한 뒤 다시 누름)


def test_finish_issue_no_opens_source_check_on_that_file(qapp, tmp_path, monkeypatch):
    from app.qt import steps

    _info, fd, acts = _acts(tmp_path, "iss")
    opened = []
    monkeypatch.setattr(steps, "_ask_list", lambda *a, **k: False)
    monkeypatch.setattr(acts, "source_check", lambda **k: opened.append(k))
    key, gkey = workspace.file_key(fd), workspace.game_file_key(fd)
    acts._finish_done(service.GlossaryReviewResult(
        "needs_confirmation", issues=[f"‘결론 못 냄’ 칸 (원문이 틀렸을 수 있음): {gkey} 1칸"]))
    assert opened == [{"focus_key": key, "focus_label": "결론 못 냄"}]


def test_closing_window_reerases_stale_erased_files(qapp, tmp_path, monkeypatch, fake_window):
    from app.qt import bg_erase, steps

    _info, fd, acts = _acts(tmp_path, "era")
    jobs, shown = [], []
    monkeypatch.setattr(service, "stale_erase_files", lambda dirs, cells=None: [Path(d) for d in dirs])

    def fake_excl(parent, title, job, on_done, on_failed=None):
        jobs.append((job.fn, job.kwargs))
        on_done(service.ProjectStepResult("done", files=[{"file": "g/f", "state": "done", "pages": 1, "warnings": 0}]))

    monkeypatch.setattr(bg_erase, "run_exclusive", fake_excl)
    monkeypatch.setattr(steps.QMessageBox, "information", lambda *a, **k: shown.append(a))
    files = [f for g in acts.page.ov["groups"] for f in g["files"]]
    acts._open_source_check(files, None, "api", {"state": "ok"})
    assert len(jobs) == 1 and jobs[0][0] is service.erase_project and jobs[0][1]["files"] == [Path(fd)]
    assert len(shown) == 1


def test_unverified_icon_cells_hold_reerase(qapp, tmp_path, monkeypatch, fake_window):
    """아이콘이 바뀌어 아직 확인이 남은 칸이 있는 파일은 다시 지우지 않고 알린다."""
    from app.qt import bg_erase, steps

    _info, fd, acts = _acts(tmp_path, "hold")
    jobs, shown = [], []

    def stale(dirs, cells=None):
        return [] if cells else [Path(d) for d in dirs]

    monkeypatch.setattr(service, "stale_erase_files", stale)
    monkeypatch.setattr(bg_erase, "run_exclusive", lambda *a, **k: jobs.append(a))
    monkeypatch.setattr(steps.QMessageBox, "information", lambda *a, **k: shown.append(a))
    monkeypatch.setattr(acts, "_changed_cells", lambda files: [{"file_dir": fd, "page": 1, "region": "본문"}])
    files = [f for g in acts.page.ov["groups"] for f in g["files"]]
    acts._open_source_check(files, None, "api", {"state": "ok"})
    assert jobs == [] and len(shown) == 1


def test_reerase_waits_until_background_reread_finishes_and_window_is_reused(qapp, tmp_path, monkeypatch, fake_window):
    """닫을 때 아직 다시 읽는 중이면: 다 읽은 뒤에 지웠던 파일을 다시 지우고, 다시 누르면 그 창을 다시 연다."""
    from app.qt import bg_erase, steps

    _info, fd, acts = _acts(tmp_path, "idle")
    jobs = []
    monkeypatch.setattr(service, "stale_erase_files", lambda dirs, cells=None: [Path(d) for d in dirs])
    monkeypatch.setattr(bg_erase, "run_exclusive", lambda *a, **k: jobs.append(a))
    monkeypatch.setattr(steps.QMessageBox, "information", lambda *a, **k: None)
    files = service.check_files(acts.pdir)
    _FakeDlg.reading_after_exec = {("x",)}
    acts._open_source_check(files, None, "api", {"state": "ok"})
    dlg = acts._sc_dlg
    assert jobs == [] and dlg.on_idle is not None
    _FakeDlg.reading_after_exec = {("x",)}
    acts._open_source_check(files, None, "api", {"state": "ok"})  # 아직 읽는 중: 같은 창
    assert acts._sc_dlg is dlg and len(fake_window) == 1 and dlg.refreshed == 1
    dlg.reading = set()
    dlg.on_idle()  # 다 읽음
    assert len(jobs) == 1


def test_window_lists_other_projects_of_the_game(tmp_path):
    from app import workspace as ws

    info, fd = _project(tmp_path, "p1", pages=1)
    info2, _ = ws.create_project("p2", "G", root=tmp_path)
    pdf = next((tmp_path / "src" / "p1").glob("*.pdf"))
    ws.add_files(info2, [pdf], "카드", new_group=True)
    files = service.check_files(info.dir)
    assert [f["mine"] for f in files] == [True, False]
    assert all(f["key"].count("/") == 2 for f in files)  # 다른 프로젝트가 있으면 ‘프로젝트/그룹/파일’


def test_glossary_gate_keeps_staged_icon_work(tmp_path):
    """③ 용어집이 이름 없는 아이콘으로 멈출 때 찾기 결과(확정 전 이름)를 다시 만들지 않는다."""
    from app import icons

    info, fd = _project(tmp_path, "gate", pages=1)
    service.find_icons(info.dir, suggest=False)
    work = info.game_dir / "work" / "icons"
    g = icons.load_scan(work).occurrences[0].group
    service.stage_icon_names(info.dir, [{"group": g, "name": "lily"}])
    r = service.finish_extraction(info.dir, accept_issues=True, client=object())
    assert r.state == "icons_review" and "이름 없는 아이콘 1곳" in r.reason and "cards" in r.reason
    assert icons.load_scan(work).groups[g]["staged"]["name"] == "lily"


def test_switching_project_while_reading_does_not_reuse_or_erase(qapp, tmp_path, monkeypatch, fake_window):
    from app.qt import bg_erase, steps

    _info, fd, acts = _acts(tmp_path, "swa")
    jobs = []
    monkeypatch.setattr(service, "stale_erase_files", lambda dirs, cells=None: [Path(d) for d in dirs])
    monkeypatch.setattr(bg_erase, "run_exclusive", lambda *a, **k: jobs.append(a))
    monkeypatch.setattr(steps.QMessageBox, "information", lambda *a, **k: None)
    files = service.check_files(acts.pdir)
    _FakeDlg.reading_after_exec = {("x",)}
    acts._open_source_check(files, None, "api", {"state": "ok"})
    old = acts._sc_dlg
    info2, _fd2 = _project(tmp_path, "swb", pages=1)
    acts.page.project_dir = info2.dir  # 다른 프로젝트를 엶 (프로젝트 화면은 다시 쓴다)
    old.reading = set()
    old.on_idle()  # 앞 프로젝트의 다시 읽기가 끝남: 다른 프로젝트 이름으로 지우지 않는다
    assert jobs == []
    _FakeDlg.reading_after_exec = set()
    old.reading = {("x",)}
    acts._open_source_check(service.check_files(info2.dir), None, "api", {"state": "ok"})
    assert acts._sc_dlg is not old and len(fake_window) == 2
