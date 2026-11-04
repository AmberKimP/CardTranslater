"""PySide6 화면 (화면 없이 offscreen으로). 실제 모델 호출 없음."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QEventLoop, QPointF, QRectF, Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app import service, workspace  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _settings(tmp_path, monkeypatch):
    monkeypatch.setenv("CARDTRANSLATER_SETTINGS", str(tmp_path / "settings.json"))


@pytest.fixture(autouse=True)
def _no_modal_boxes(monkeypatch):
    """테스트에서는 알림 창이 사람을 기다리지 않게 바로 닫는다."""
    from PySide6.QtWidgets import QMessageBox

    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.No)


def _pdf(path: Path, pages: int = 3) -> Path:
    import pymupdf

    doc = pymupdf.open()
    for i in range(pages):
        pg = doc.new_page(width=250, height=180)
        pg.insert_text((30, 140), f"card {i + 1}", fontsize=10)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    return path


def _project(tmp_path):
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    service.add_files(info.dir, [_pdf(tmp_path / "in" / "cards.pdf")], "카드", new_group=True)
    return info


def test_qt_screens_only_use_service():
    """화면은 app.service(와 화면 모듈)만 부른다 (PRD 6장)."""
    allowed = {"app.service", "app.qt"}
    for f in sorted((ROOT / "app" / "qt").glob("*.py")):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app"):
                mods = [node.module] if node.module != "app" else [f"app.{a.name}" for a in node.names]
                for m in mods:
                    assert any(m == a or m.startswith(a + ".") for a in allowed), f"{f.name}: {m}"


def test_settings_roundtrip(tmp_path):
    assert service.data_dir() == ROOT  # 개발 중 기본값은 저장소 폴더
    service.save_settings(tmp_path / "data", "api")
    assert service.data_dir() == tmp_path / "data" and service.get_settings().engine == "api"


def test_region_template_validation(tmp_path):
    good = {"regions": [{"name": "본문", "kind": "text", "rect": {"x": 0.1, "y": 0.5, "w": 0.5, "h": 0.2}}]}
    assert service.validate_region_template(good) == []
    bad = {"regions": [{"name": "a", "rect": {"x": 0.8, "y": 0.5, "w": 0.5, "h": 0.2}},
                       {"name": "a", "rect": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1},
                        "style": {"rotate": 90, "font": {"file": "fonts/없음.ttf"}}}]}
    p = service.validate_region_template(bad)
    assert any("겹칩니다" in x for x in p) and any("카드 밖" in x for x in p)
    assert any("회전" in x for x in p) and any("글꼴 파일" in x for x in p)
    assert service.save_region_template(tmp_path, bad) and not (tmp_path / "regions.json").exists()


def test_overview_and_next_step(tmp_path):
    info = _project(tmp_path)
    o = service.project_overview(info.dir)
    assert o["next_step"].startswith("영역을 지정하세요") and o["groups"][0]["files"][0]["pages"] == 3
    fd = o["groups"][0]["files"][0]["dir"]
    service.set_pages(fd, [2], "규칙 카드")
    f = service.project_overview(info.dir)["groups"][0]["files"][0]
    assert f["excluded"] == [2] and f["included"] == 2 and f["exclude_reason"] == "규칙 카드"
    with pytest.raises(ValueError):
        service.set_pages(fd, [9])


def test_region_editor_draw_edit_save_keeps_unknown_keys(qapp, tmp_path):
    from app.qt.region_editor import RegionEditor

    info = _project(tmp_path)
    g = service.project_overview(info.dir)["groups"][0]
    gdir = Path(g["dir"])
    # 손으로 쓴 템플릿의 모르는 항목(태그 색 사전·메모·글꼴 고른 이유)은 저장해도 남아야 한다
    (gdir / "regions.json").write_text(json.dumps({
        "_comment": "손으로 씀", "source_language": "ja", "reference_page": 2,
        "regions": [{"name": "태그", "kind": "tag_pills", "rect": {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.1},
                     "extract": {"tag_colors": [{"color": "#9d2e3c", "text": "炎"}]},
                     "style": {"color": "#ffffff", "font": {"family": "Noto Sans KR Black",
                                                            "file": "fonts/notosanskr/NotoSansKR-Black.ttf",
                                                            "chosen": "사용자 선택"}}}],
        "protect_rects": []}, ensure_ascii=False), encoding="utf-8")
    ed = RegionEditor(gdir, [Path(f["dir"]) for f in g["files"]])
    assert ed.page_spin.value() == 2 and len(ed.items()) == 1
    pr = ed.scene.page_rect

    # 새 글자 영역 그리기 (빈 곳 끌기)
    ed._set_add_kind("text")
    from app.qt.region_editor import RegionItem
    item = RegionItem(QRectF(pr.width() * 0.1, pr.height() * 0.6, pr.width() * 0.5, pr.height() * 0.2),
                      {"name": "", "kind": "text"})
    ed.scene.addItem(item)
    ed.scene.created_item.emit(item)
    assert item.name == "본문" and ed.scene.add_kind is None
    item.setSelected(True)
    ed.f_rotate.setChecked(True)
    ed.f_align.setCurrentIndex(ed.f_align.findData("center"))
    ed.f_keep_tall.setValue(9)
    ed.f_font.setCurrentIndex(ed.f_font.findData("fonts/gaegu/Gaegu-Regular.ttf"))
    ed.f_name.setText("설명")
    ed._from_form()
    # 화살표로 1px 옮기기
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtCore import QEvent
    x0 = item.rect().left()
    ed.view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier))
    assert item.rect().left() == pytest.approx(x0 + 1)
    # 보호 영역
    prot = RegionItem(QRectF(0, 0, pr.width() * 0.1, pr.height()), {"name": "", "kind": "text"}, protect=True)
    ed.scene.addItem(prot)
    ed.scene.created_item.emit(prot)
    assert ed.save() and not ed.dirty

    data = json.loads((gdir / "regions.json").read_text(encoding="utf-8"))
    assert data["_comment"] == "손으로 씀" and data["reference_page"] == 2
    tag = next(r for r in data["regions"] if r["name"] == "태그")
    assert tag["extract"]["tag_colors"][0]["text"] == "炎" and tag["style"]["font"]["chosen"] == "사용자 선택"
    body = next(r for r in data["regions"] if r["name"] == "설명")
    assert body["kind"] == "text" and body["style"]["rotate"] == 180 and body["style"]["align"] == "center"
    assert body["erase"]["keep_ink_taller_than_pt"] == 9 and body["style"]["font"]["file"].endswith("Gaegu-Regular.ttf")
    assert body["rect"]["x"] == pytest.approx(0.1 + 1 / pr.width(), abs=1e-3) and body["rect"]["w"] == pytest.approx(0.5)
    assert data["protect_rects"][0]["name"] == "보호" and "kind" not in data["protect_rects"][0]
    assert (gdir / "regions.json.bak").exists()
    # 다시 열면 같은 영역
    ed2 = RegionEditor(gdir, [Path(f["dir"]) for f in g["files"]])
    assert [i.name for i in ed2.items()] == ["태그", "설명", "보호"]  # 템플릿 순서 그대로
    ed.dirty = ed2.dirty = False


def test_region_item_drag_resize_stays_on_page(qapp, tmp_path):
    from app.qt.region_editor import PageScene, RegionItem

    sc = PageScene()
    sc.page_rect = QRectF(0, 0, 100, 80)
    it = RegionItem(QRectF(10, 10, 20, 20), {"name": "a"})
    sc.addItem(it)
    it.set_scene_rect(QRectF(90, 70, 20, 20))  # 밖으로 끌면 안으로 맞춘다
    assert it.rect() == QRectF(80, 60, 20, 20)


def test_main_window_home_and_project(qapp, tmp_path):
    from app.qt.main import MainWindow

    info = _project(tmp_path)
    win = MainWindow(root=tmp_path)
    assert win.home.table.rowCount() == 1 and win.home.table.item(0, 0).text() == "p"
    win.open_project(info.dir)
    assert win.stack.currentWidget() is win.project
    top = win.project.tree.topLevelItem(0)
    assert "영역 없음" in top.text(0) and top.child(0).text(1) == "3"
    assert "영역을 지정하세요" in win.project.next.text()


def test_new_project_and_add_files_dialogs(qapp, tmp_path):
    from app.qt.dialogs import AddFilesDialog, ExcludePagesDialog, NewProjectDialog

    d = NewProjectDialog(tmp_path)
    d.name.setText("1차")
    d.game.setCurrentText("Dungeon")
    assert "새 게임" in d.hint.text()
    d.accept()
    assert d.created and (d.created / "project.json").exists()
    a = AddFilesDialog(d.created, files=[str(_pdf(tmp_path / "in" / "a.pdf", 2))])
    assert a.r_new.isChecked() and not a.r_exist.isEnabled()  # 그룹이 없으면 새 그룹만
    a.new_name.setText("카드")
    a.accept()
    assert a.result_info["added"] and not a.result_info["has_regions"]
    fd = a.result_info["added"][0]
    e = ExcludePagesDialog(fd, [])
    e.list.item(0).setCheckState(Qt.CheckState.Checked)
    e.reason.setText("규칙")
    e.accept()
    assert workspace.included_pages(fd) == [2]


def test_worker_runs_service_function_with_progress(qapp):
    from app.qt import worker

    def fn(n, progress=None, cancel=None):
        for i in range(n):
            cancel.check()
            progress(service.ProgressEvent("x", i + 1, n, "일"))
        return n * 2

    got, events = {}, []
    loop = QEventLoop()
    job = worker.Job(fn, 3)
    job.signals.progress.connect(events.append)
    job.signals.done.connect(lambda r: (got.update(r=r), loop.quit()))
    worker.start(job)
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    assert got["r"] == 6 and [e.done for e in events] == [1, 2, 3]

    cancelled = {}
    loop2 = QEventLoop()
    job2 = worker.Job(fn, 3)
    job2.cancel()
    job2.signals.cancelled.connect(lambda: (cancelled.update(ok=True), loop2.quit()))
    worker.start(job2)
    QTimer.singleShot(5000, loop2.quit)
    loop2.exec()
    assert cancelled.get("ok")
    _ = QPointF  # noqa


def test_region_editor_mouse_draw_move_resize(qapp, tmp_path):
    """실제 마우스 끌기로 그리기·옮기기·크기 조절."""
    from PySide6.QtTest import QTest

    from app.qt.region_editor import RegionEditor

    info = _project(tmp_path)
    g = service.project_overview(info.dir)["groups"][0]
    ed = RegionEditor(Path(g["dir"]), [Path(f["dir"]) for f in g["files"]])
    ed.resize(1000, 700)
    ed.show()
    qapp.processEvents()
    ed.view.resetTransform()  # 1:1 (장면 px = 화면 px)
    ed.view.centerOn(ed.scene.page_rect.center())
    qapp.processEvents()
    vp = ed.view.viewport()

    def at(x, y):
        return ed.view.mapFromScene(QPointF(x, y))

    ed._set_add_kind("text")
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(50, 60))
    QTest.mouseMove(vp, at(120, 90))
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(150, 100))
    assert len(ed.items()) == 1
    it = ed.items()[0]
    r = it.rect()
    assert (r.left(), r.top()) == pytest.approx((50, 60), abs=1.5) and r.width() == pytest.approx(100, abs=1.5)
    # 안쪽을 끌어 옮기기
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(100, 80))
    QTest.mouseMove(vp, at(110, 85))
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(120, 90))
    assert it.rect().left() == pytest.approx(70, abs=1.5) and it.rect().width() == pytest.approx(100, abs=1.5)
    # 오른쪽 가장자리를 끌어 넓히기
    right = it.rect().right()
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(right, 100))
    QTest.mouseMove(vp, at(right + 10, 100))
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(right + 30, 100))
    assert it.rect().width() == pytest.approx(130, abs=2) and it.rect().left() == pytest.approx(70, abs=1.5)
    ed.dirty = False
    ed.close()


# ================================================================ 2차: 추출 → 원문 확인 → 아이콘 → 용어집 → 번역
def _project_with_extract(tmp_path):
    """영역 2개(이름, 태그) + 1쪽은 결론 못 냄, 2쪽은 두 번 확인, 3쪽은 아직 안 읽음."""
    info = _project(tmp_path)
    g = service.project_overview(info.dir)["groups"][0]
    service.save_region_template(g["dir"], {"regions": [
        {"name": "이름", "kind": "text", "rect": {"x": 0.1, "y": 0.7, "w": 0.6, "h": 0.15}},
        {"name": "태그", "kind": "tag_pills", "rect": {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.1}}]})
    fd = Path(g["files"][0]["dir"])
    d = fd / "extract" / "pages"
    d.mkdir(parents=True)
    (d / "page-001.json").write_text(json.dumps({"page": 1, "model": "claude-sonnet-5-5", "regions": {
        "이름": {"source": "盗賊フェレット", "uncertain": True, "note": "다르게 읽음",
                "readings": [{"model": "claude-sonnet-5-5", "source": "盗賊フェレット"},
                             {"model": "claude-opus-5-5", "source": "盗掘フェレット"}]},
        "태그": {"source": ["人族"], "uncertain": False, "note": ""}}}, ensure_ascii=False), encoding="utf-8")
    (d / "page-002.json").write_text(json.dumps({"page": 2, "model": "claude-sonnet-5-5", "regions": {
        "이름": {"source": "竜", "uncertain": False, "note": "",
                "readings": [{"model": "claude-sonnet-5-5", "source": "竜"}, {"model": "claude-opus-5-5", "source": "竜"}]},
        "태그": {"source": ["竜"], "uncertain": False, "note": ""}}}, ensure_ascii=False), encoding="utf-8")
    return info, fd


def test_extracted_cells_and_save_source(tmp_path):
    info, fd = _project_with_extract(tmp_path)
    cells = {(c["page"], c["region"]): c for c in service.extracted_cells(fd)}
    assert cells[(1, "이름")]["label"] == "결론 못 냄" and cells[(1, "이름")]["readings"][1]["model"] == "Opus"
    assert cells[(2, "이름")]["label"] == "두 번 확인" and cells[(3, "이름")]["label"] == "읽지 않음"
    assert service.region_crop(fd, 1, "이름")[:4] == b"\x89PNG"
    service.save_source(fd, 1, "이름", "盗掘フェレット")
    service.save_source(fd, 3, "태그", ["人族", "魔"])  # 읽지 않은 쪽도 손으로 넣을 수 있다
    cells = {(c["page"], c["region"]): c for c in service.extracted_cells(fd)}
    c = cells[(1, "이름")]
    assert c["source"] == "盗掘フェレット" and not c["uncertain"] and c["label"] == "사람이 고침" and c["edited"]
    assert "盗賊フェレット" in c["note"] and cells[(3, "태그")]["source"] == ["人族", "魔"]


def test_source_review_dialog_edits_unresolved_cell(qapp, tmp_path):
    from app.qt.steps import SourceReviewDialog

    info, fd = _project_with_extract(tmp_path)
    files = service.project_overview(info.dir)["groups"][0]["files"]
    dlg = SourceReviewDialog(files)
    # 확인할 칸: 1쪽 이름(결론 못 냄) + 3쪽 두 칸(읽지 않음)
    assert dlg.table.rowCount() == 3 and dlg.table.item(0, 2).text() == "결론 못 냄"
    assert "Opus: 盗掘フェレット" in dlg.info.text() and dlg.image.pixmap() is not None
    dlg.edit.setPlainText("盗掘フェレット")
    dlg.save_current()
    assert dlg.table.rowCount() == 2 and dlg.saved == 1  # 고친 칸은 확인 목록에서 빠진다
    dlg.only.setChecked(False)
    assert dlg.table.rowCount() == 6


def test_icon_review_dialog_confirms_names(qapp, tmp_path):
    from app.qt.steps import IconReviewDialog
    from tests.test_icons import _project as icon_project, _source

    info, fd = icon_project(tmp_path, "1차", pages=2)
    assert service.find_icons(info.dir, suggest=False).state == "icons_review"
    review = service.icon_review(info.dir)
    assert review["groups"][0]["count"] == 2 and review["groups"][0]["image"]
    dlg = IconReviewDialog(info.dir, review)
    _g, name, meaning, ko, _ni, _sc = dlg.widgets[0]
    name.setText("Bad Name")
    dlg.accept()
    assert dlg.result is None  # 규칙을 어기면 확정하지 않는다
    name.setText("coin")
    meaning.setText("Gold")
    ko.setText("금화")
    dlg.accept()
    assert dlg.result.state == "confirmed" and dlg.result.added == ["coin"] and _source(fd, 2) == "AB[[coin]]CD"


def test_project_step_buttons_follow_status(qapp, tmp_path):
    from app.qt.main import MainWindow

    info, _fd = _project_with_extract(tmp_path)
    win = MainWindow(root=tmp_path)
    win.open_project(info.dir)
    on = {k: b.isEnabled() for k, b in win.project.step_buttons.items()}
    assert on == {"extract": True, "review": True, "icons": True, "finish": True, "glossary": False,
                  "translate": False, "excel": False, "erase": True, "compose": False, "results": False,
                  "test": False}
    workspace.set_status(workspace.load_project_info(info.dir), "glossary_confirmed")
    win.project.refresh()
    assert win.project.step_buttons["translate"].isEnabled() and not win.project.step_buttons["glossary"].isEnabled()


def test_step_flows_ask_and_continue(qapp, tmp_path, monkeypatch):
    """엔진이 확인을 요구하면 묻고, ‘예’면 accept로 다시 부른다. 한도로 멈추면 자동 이어 하기 타이머."""
    from app.qt import steps
    from app.qt.main import MainWindow

    info, _fd = _project_with_extract(tmp_path)
    win = MainWindow(root=tmp_path)
    win.open_project(info.dir)
    act = win.project.steps
    monkeypatch.setattr(steps, "_ask_list", lambda *a, **k: True)
    called = {}
    monkeypatch.setattr(act, "finish", lambda accept=False: called.update(accept=accept))
    act._finish_done(service.GlossaryReviewResult("needs_confirmation", issues=["카드 1장 남음"]))
    assert called == {"accept": True}

    import datetime as dt
    soon = (dt.datetime.now() + dt.timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    r = service.ProjectExtractResult("paused", files=[{"file": "카드/cards", "state": "paused", "done": 1,
                                                       "remaining": 2, "failed": 0, "recheck": (0, 0)}],
                                     reason="사용 한도", retry_after=soon)
    act._extract_done(r, (None, "double-check", "claude-code"))
    assert act.resume_timer is not None and act.resume_timer.isActive()
    assert 5 * 60_000 <= act.resume_timer.remainingTime() <= 7 * 60_000
    act.resume_timer.stop()


def test_region_editor_moves_between_groups_and_shows_used_fonts_first(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from app.qt.region_editor import RegionEditor

    info = _project(tmp_path)
    service.add_files(info.dir, [_pdf(tmp_path / "in2" / "items.pdf", 2)], "아이템", new_group=True)
    ov = service.project_overview(info.dir)
    g1, g2 = sorted(ov["groups"], key=lambda g: g["name"] != "카드")
    service.save_region_template(g1["dir"], {"regions": [
        {"name": "이름", "rect": {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.1},
         "style": {"font": {"family": "Jua", "file": "fonts/jua/Jua-Regular.ttf"}}},
        {"name": "본문", "rect": {"x": 0.1, "y": 0.5, "w": 0.3, "h": 0.1}}]})
    service.save_region_template(g2["dir"], {"regions": [
        {"name": "효과", "rect": {"x": 0.2, "y": 0.6, "w": 0.5, "h": 0.2},
         "style": {"font": {"family": "Gaegu", "file": "fonts/gaegu/Gaegu-Regular.ttf"}}}]})
    groups = [{"name": g["name"], "dir": g["dir"], "files": [f["dir"] for f in g["files"]]} for g in (g1, g2)]
    ed = RegionEditor(Path(g1["dir"]), groups[0]["files"], groups=groups)
    assert ed.file_combo.count() == 2 and ed.file_combo.itemText(1).startswith("아이템 / ")

    # 본문을 고르면 글꼴 목록 맨 위에 다른 영역(이름: Jua, 아이템/효과: Gaegu)에서 쓰는 글꼴이 굵게
    body = next(i for i in ed.items() if i.name == "본문")
    body.setSelected(True)
    top = [ed.f_font.itemText(i) for i in range(2)]
    assert "Jua" in top[0] and "쓰는 곳: 이름" in top[0] and "Gaegu" in top[1] and "아이템/효과" in top[1]
    assert ed.f_font.itemData(0, Qt.ItemDataRole.FontRole).bold()
    assert not (ed.f_font.itemData(4, Qt.ItemDataRole.FontRole) or ed.f_font.font()).bold()

    # 바꾼 게 있으면 그룹을 옮길 때 묻는다: 취소면 그대로
    ed._mark_dirty()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Cancel))
    ed.file_combo.setCurrentIndex(1)
    assert ed.group_dir == Path(g1["dir"]) and ed.file_combo.currentIndex() == 0
    # 버리면 아이템 그룹 영역으로
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Discard))
    ed.file_combo.setCurrentIndex(1)
    assert ed.group_dir == Path(g2["dir"]) and [i.name for i in ed.items()] == ["효과"] and not ed.dirty
    assert "아이템" in ed.windowTitle() and ed.page_total.text() == "/ 2"


# ================================================================ 3차: 지우기 → 한글 넣기 → 결과 보기, 시험 실행, 이어 하기
def _translated_project(tmp_path):
    from app.extract_cc import ClaudeCodeClient
    from app import glossary
    from tests.test_extract import FakeRunner
    from tests.test_project_steps import _project as steps_project
    from tests.test_service import GAEGU
    from tests.test_translate import _write_extract

    if not GAEGU.exists():
        pytest.skip("fonts/ 없음")
    info, fd = steps_project(tmp_path)
    _write_extract(fd, 1, "HELLO")
    _write_extract(fd, 2, "HELLO")
    glossary.confirm(info, [{"source": "HELLO", "ko": "안녕", "category": "기타"}])
    workspace.set_status(info, "glossary_confirmed")
    service.translate_project(info.dir, client=ClaudeCodeClient(exe="claude", runner=FakeRunner([])))
    return info, fd


def test_test_run_and_results_do_not_touch_project(tmp_path):
    import hashlib

    info, fd = _translated_project(tmp_path)
    assert service.erase_project(info.dir).state == "done"
    assert service.compose_project(info.dir).state == "done"
    pdf = fd / "translated" / "translated.pdf"
    before = hashlib.sha256(pdf.read_bytes()).hexdigest()
    pages = service.result_pages(fd / "translated")
    assert [p["page"] for p in pages] == [1, 2] and pages[0]["card_png"].exists()
    assert pages[0]["regions"][0]["name"] == "이름" and pages[0]["regions"][0]["png"].exists()

    r = service.test_run(fd, "2", out_root=tmp_path / "runs")
    assert r.state == "done" and r.pages == [2] and r.run_dir.parent == tmp_path / "runs"
    assert [p["page"] for p in service.result_pages(r.run_dir)] == [2]
    assert hashlib.sha256(pdf.read_bytes()).hexdigest() == before  # 프로젝트 결과는 그대로


def test_result_viewer_and_output_buttons(qapp, tmp_path):
    from app.qt.main import MainWindow
    from app.qt.results import ResultViewer

    info, fd = _translated_project(tmp_path)
    service.erase_project(info.dir)
    service.compose_project(info.dir)
    v = ResultViewer([("카드/cards", fd / "translated")])
    assert v.pages.count() == 2 and v.image.pixmap() is not None and v.regions.count() == 1
    v.regions.setCurrentRow(0)
    v.only.setChecked(True)  # 문제 있는 쪽만: 없음
    assert v.pages.count() == 0 and "문제 있는 쪽 0" in v.info.text()

    win = MainWindow(root=tmp_path)
    win.open_project(info.dir)
    on = {k: win.project.step_buttons[k].isEnabled() for k in ("erase", "compose", "results", "test")}
    assert on == {"erase": True, "compose": True, "results": True, "test": True}
    # 다시 켜면 마지막 프로젝트를 연다
    win2 = MainWindow(root=tmp_path)
    assert win2.resume_last() and win2.stack.currentWidget() is win2.project
    assert win2.project.project_dir.resolve() == info.dir.resolve()
