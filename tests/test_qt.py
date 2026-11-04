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
    _g, name, meaning, ko, _ni, _sc, _same = dlg.widgets[0]
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
    monkeypatch.setattr("app.qt.region_editor.ask_choice", lambda *a, **k: 2)  # 취소
    ed.file_combo.setCurrentIndex(1)
    assert ed.group_dir == Path(g1["dir"]) and ed.file_combo.currentIndex() == 0
    # 버리면 아이템 그룹 영역으로
    monkeypatch.setattr("app.qt.region_editor.ask_choice", lambda *a, **k: 1)  # 저장 안 함
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
    assert v.pages.count() == 2 and v.image.pixmap() is not None and v.regions.count() == 2  # 카드 전체 + 영역 1
    assert v.regions.item(0).text() == "카드 전체" and v.regions.currentRow() == 0
    v.regions.setCurrentRow(1)
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


def test_delete_project_group_file_go_to_trash(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from app.qt.main import MainWindow

    info = _project(tmp_path)
    service.add_files(info.dir, [_pdf(tmp_path / "in" / "b.pdf", 2)], "카드")
    ov = service.project_overview(info.dir)
    f0 = ov["groups"][0]["files"][0]
    dest = service.delete_file(f0["dir"])
    assert dest.parent == tmp_path / ".trash" and (dest / "source").exists() and not Path(f0["dir"]).exists()
    assert len(service.project_overview(info.dir)["groups"][0]["files"]) == 1
    # 화면: 그룹 삭제 (예)
    win = MainWindow(root=tmp_path)
    win.open_project(info.dir)
    win.project.tree.setCurrentItem(win.project.tree.topLevelItem(0))
    assert win.project.b_delete.isEnabled()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    win.project.delete_selected()
    assert service.project_overview(info.dir)["groups"] == []
    # 홈: 프로젝트 삭제. 게임(용어집 폴더)은 남는다
    win.show_home()
    win.home.table.selectRow(0)
    win.home.delete_selected()
    assert service.list_projects(tmp_path) == [] and (tmp_path / "games" / "G" / "game.json").exists()
    assert len(list((tmp_path / ".trash").iterdir())) == 3


def test_region_editor_page_navigation(qapp, tmp_path):
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent

    from app.qt.region_editor import RegionEditor

    info = _project(tmp_path)  # 3쪽
    g = service.project_overview(info.dir)["groups"][0]
    service.save_region_template(g["dir"], {"regions": [
        {"name": "이름", "rect": {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.1}}]})
    ed = RegionEditor(Path(g["dir"]), [Path(f["dir"]) for f in g["files"]])

    def key(k):
        ed.view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, k, Qt.KeyboardModifier.NoModifier))

    assert ed.page_spin.value() == 1 and not ed.prev_btn.isEnabled()
    key(Qt.Key.Key_Right)  # 영역을 고르지 않았으면 다음 쪽
    assert ed.page_spin.value() == 2
    ed.next_btn.click()
    assert ed.page_spin.value() == 3 and not ed.next_btn.isEnabled()
    key(Qt.Key.Key_Right)  # 끝에서는 멈춤
    assert ed.page_spin.value() == 3
    it = ed.items()[0]
    it.setSelected(True)
    x0 = it.rect().left()
    key(Qt.Key.Key_Left)  # 영역을 고르면 화살표는 그 영역을 옮긴다
    assert ed.page_spin.value() == 3 and it.rect().left() == pytest.approx(x0 - 1)
    key(Qt.Key.Key_PageUp)  # PageUp은 언제나 쪽 넘기기
    assert ed.page_spin.value() == 2
    key(Qt.Key.Key_Escape)
    key(Qt.Key.Key_Left)
    assert ed.page_spin.value() == 1 and ed.prev_btn.isEnabled() is False
    ed.dirty = False


# ================================================================ 카드마다 영역, 휠, 처음 열 때 화면 맞춤
def test_per_card_template_engine(tmp_path):
    from app.project import load_project
    from app.regions import load_template

    info = _project(tmp_path)  # 3쪽
    g = service.project_overview(info.dir)["groups"][0]
    fd = Path(g["files"][0]["dir"])
    data = {"layout": "per_card", "regions": [], "protect_rects": [], "page_regions": {
        f"{fd.name}/1": {"regions": [{"name": "이름", "rect": {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.1}}]},
        f"{fd.name}/3": {"regions": [{"name": "설명", "rect": {"x": 0.2, "y": 0.5, "w": 0.5, "h": 0.2}}],
                         "protect_rects": [{"name": "그림", "rect": {"x": 0, "y": 0, "w": 0.2, "h": 0.2}}]},
        "다른파일/2": {"regions": [{"name": "x", "rect": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}}]}}}
    assert service.save_region_template(g["dir"], data) == []
    t = load_project(fd).template
    assert [r.name for r in t.regions_for(1)] == ["이름"] and t.regions_for(2) == []
    assert [r.name for r in t.regions_for(3)] == ["설명"] and [p.name for p in t.protect_for(3)] == ["그림"]
    assert sorted(r.name for r in t.regions) == ["설명", "이름"]  # 다른 파일의 카드 영역은 섞이지 않는다
    assert [r.name for r in t.for_page(3).regions] == ["설명"]
    assert load_template(Path(g["dir"]) / "regions.json").layout == "per_card"
    grp = service.project_overview(info.dir)["groups"][0]
    assert grp["per_card"] and grp["files"][0]["no_regions"] == [2]
    assert "카드마다 영역을 지정하세요" in service.project_overview(info.dir)["next_step"]
    assert service.validate_region_template({"layout": "per_card", "page_regions": {}}) == [
        "영역을 정한 카드가 하나도 없습니다"]


def test_extract_skips_cards_without_regions(tmp_path):
    from app.extract import NO_REGIONS
    from app.extract_cc import ClaudeCodeClient
    from tests.test_extract import FakeRunner, _cc_ok

    info = _project(tmp_path)
    g = service.project_overview(info.dir)["groups"][0]
    fd = Path(g["files"][0]["dir"])
    reg = [{"name": "이름", "rect": {"x": 0.1, "y": 0.6, "w": 0.6, "h": 0.15}}]
    service.save_region_template(g["dir"], {"layout": "per_card", "page_regions": {f"{fd.name}/2": {"regions": reg}}})
    run = FakeRunner([(0, _cc_ok(), "")])
    r = service.extract(fd, mode="opus", client=ClaudeCodeClient(exe="claude", runner=run))
    assert r.done_pages == [2] and r.failed == {1: NO_REGIONS, 3: NO_REGIONS} and len(run.calls) == 1
    # 1쪽 영역을 정하면 다음 실행에서 그 카드만 읽는다
    service.save_region_template(g["dir"], {"layout": "per_card", "page_regions": {
        f"{fd.name}/1": {"regions": reg}, f"{fd.name}/2": {"regions": reg}}})
    run2 = FakeRunner([(0, _cc_ok(), "")])
    r2 = service.extract(fd, mode="opus", client=ClaudeCodeClient(exe="claude", runner=run2))
    assert 1 in r2.done_pages and r2.failed == {3: NO_REGIONS} and len(run2.calls) == 1


def test_region_editor_per_card_mode(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from app.qt.region_editor import RegionEditor, RegionItem

    info = _project(tmp_path)  # 3쪽
    g = service.project_overview(info.dir)["groups"][0]
    service.save_region_template(g["dir"], {"regions": [
        {"name": "이름", "rect": {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.1}}]})
    ed = RegionEditor(Path(g["dir"]), [Path(f["dir"]) for f in g["files"]])
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    ed.per_card.setChecked(True)  # 지금 영역이 1쪽 카드의 영역이 된다
    assert "이 카드: 영역 1개" in ed.card_info.text() and "지정한 카드 1/3" in ed.card_info.text()
    ed.step_page(1)  # 2쪽: 아직 영역 없음
    assert ed.items() == [] and "영역 없음" in ed.card_info.text()
    pr = ed.scene.page_rect
    it = RegionItem(QRectF(pr.width() * 0.2, pr.height() * 0.5, pr.width() * 0.4, pr.height() * 0.2),
                    {"name": "", "kind": "text"})
    ed.scene.addItem(it)
    ed.scene.created_item.emit(it)
    ed.step_page(1)  # 3쪽: 앞 카드 영역 복사
    ed.copy_previous_card()
    assert [i.name for i in ed.items()] == ["본문"]
    ed.step_page(-1)
    ed.step_page(-1)  # 1쪽으로 돌아오면 1쪽 영역
    assert [i.name for i in ed.items()] == ["이름"]
    assert ed.save()
    data = json.loads((Path(g["dir"]) / "regions.json").read_text(encoding="utf-8"))
    fname = Path(g["files"][0]["dir"]).name
    assert data["layout"] == "per_card" and data["regions"] == []
    assert sorted(data["page_regions"]) == [f"{fname}/1", f"{fname}/2", f"{fname}/3"]
    assert data["page_regions"][f"{fname}/3"]["regions"][0]["name"] == "본문"
    # 다시 열면 카드마다 모드, 1쪽 영역
    ed2 = RegionEditor(Path(g["dir"]), [Path(f["dir"]) for f in g["files"]])
    assert ed2.per_card.isChecked() and [i.name for i in ed2.items()] == ["이름"]
    ed2.step_page(1)
    assert [i.name for i in ed2.items()] == ["본문"]
    ed.dirty = ed2.dirty = False


def test_region_editor_wheel_pages_and_auto_fit(qapp, tmp_path):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QWheelEvent

    from app.qt.region_editor import RegionEditor

    info = _project(tmp_path)
    g = service.project_overview(info.dir)["groups"][0]
    service.save_region_template(g["dir"], {"regions": [
        {"name": "이름", "rect": {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.1}}]})
    ed = RegionEditor(Path(g["dir"]), [Path(f["dir"]) for f in g["files"]])

    def wheel(dy, mods=Qt.KeyboardModifier.NoModifier):
        ev = QWheelEvent(QPointF(10, 10), QPointF(10, 10), QPoint(0, 0), QPoint(0, dy), Qt.MouseButton.NoButton,
                         mods, Qt.ScrollPhase.NoScrollPhase, False)
        ed.view.wheelEvent(ev)

    wheel(-120)  # 아래로: 다음 쪽
    assert ed.page_spin.value() == 2
    wheel(-60)
    assert ed.page_spin.value() == 2  # 터치패드의 작은 움직임은 모아서
    wheel(-60)
    assert ed.page_spin.value() == 3
    wheel(120)
    assert ed.page_spin.value() == 2
    # 처음 열 때: 창이 커지면 카드도 따라 커진다 (사용자가 확대하기 전까지)
    ed.resize(900, 650)
    ed.show()
    qapp.processEvents()
    s1 = ed.view.transform().m11()
    ed.resize(1700, 1200)
    qapp.processEvents()
    assert ed.view.transform().m11() > s1 * 1.3
    wheel(120, Qt.KeyboardModifier.ControlModifier)  # Ctrl+휠 확대 뒤에는 창 크기를 따라가지 않는다
    s2 = ed.view.transform().m11()
    ed.resize(1000, 700)
    qapp.processEvents()
    assert ed.view.transform().m11() == pytest.approx(s2)
    ed.dirty = False
    ed.close()


# ================================================================ 로그인: Claude 구독 / API 키
def test_api_key_is_encrypted_and_sources_in_order(tmp_path, monkeypatch):
    from app import credentials
    from app.extract import api_key_source

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("app.settings.REPO_ROOT", tmp_path / "repo")  # 저장소의 .env는 보지 않게
    assert api_key_source() == (None, "")
    p = credentials.save_api_key("sk-ant-api03-TESTKEY-abcdefgh")
    assert b"sk-ant" not in p.read_bytes()  # 평문으로 남지 않는다
    assert api_key_source() == ("sk-ant-api03-TESTKEY-abcdefgh", "저장한 키")
    info = service.api_key_info()
    assert info == {"has_key": True, "source": "저장한 키", "masked": "sk-ant-…efgh"}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env-0000000000")
    assert api_key_source()[1] == "환경 변수"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    service.clear_api_key()
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-dotenv-1111111111\n", encoding="utf-8")
    service.save_settings(tmp_path, "api")
    assert api_key_source() == ("sk-ant-dotenv-1111111111", ".env")


def test_set_api_key_validates_without_real_calls(tmp_path, monkeypatch):
    from app import credentials

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    class Models:
        def __init__(self, ok):
            self.ok = ok

        def list(self, limit=1):
            if not self.ok:
                raise RuntimeError("Error code: 401 - authentication_error")
            return []

    class Fake:
        def __init__(self, ok):
            self.models = Models(ok)

    ok, msg = service.set_api_key("not-a-key")
    assert not ok and "형식" in msg
    ok, msg = service.set_api_key("sk-ant-bad-0000000000", client_factory=lambda k: Fake(False))
    assert not ok and "인증 실패" in msg and credentials.load_api_key() is None
    ok, msg = service.set_api_key("sk-ant-good-0000000000", client_factory=lambda k: Fake(True))
    assert ok and credentials.load_api_key() == "sk-ant-good-0000000000"


def test_claude_login_status(tmp_path, monkeypatch):
    from tests.test_extract import FakeRunner

    monkeypatch.setattr("shutil.which", lambda *_a, **_k: None)
    assert service.claude_login_status()["installed"] is False
    ok = (0, json.dumps({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.0,
                         "modelUsage": {}, "structured_output": {"ok": True}}), "")
    st = service.claude_login_status(check=True, runner=FakeRunner([ok]))
    assert st["logged_in"] is True
    bad = (1, json.dumps({"type": "result", "subtype": "error", "is_error": True,
                          "result": "Not logged in · Please run /login"}), "")
    st = service.claude_login_status(check=True, runner=FakeRunner([bad]))
    assert st["logged_in"] is False and "로그인" in st["message"]


def test_login_dialog_sets_engine(qapp, tmp_path, monkeypatch):
    from app.qt.login import LoginDialog

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("app.settings.REPO_ROOT", tmp_path / "repo")
    dlg = LoginDialog()
    assert dlg.r_claude.isChecked()  # 기본: Claude 구독
    dlg.r_api.setChecked(True)
    assert dlg.key.isEnabled() and not dlg.b_check.isEnabled()
    dlg.accept()  # 키가 없으면 시작하지 않는다
    assert dlg.result() != 1 and service.get_settings().engine == "claude-code"
    service.set_api_key("sk-ant-good-0000000000", validate=False)
    dlg._refresh_api()
    dlg.accept()
    assert service.get_settings().engine == "api" and "sk-ant-…0000" in dlg.api_status.text()
    assert service.engine_status()["message"].startswith("API 키로 실행")
    dlg2 = LoginDialog(first_run=False)
    assert dlg2.r_api.isChecked()  # 다음에 열면 고른 방식


def test_icon_review_merge_rows_and_card_view(qapp, tmp_path, monkeypatch):
    from app import icons
    from app.qt.steps import IconReviewDialog
    from tests.test_icons import _project as icon_project, _source

    monkeypatch.setattr(icons, "SAME_SHAPE", 1.01)  # 같은 모양도 따로 묶이게 (잘못 나뉜 묶음 흉내)
    info, fd = icon_project(tmp_path, "1차", pages=2)
    service.find_icons(info.dir, suggest=False)
    review = service.icon_review(info.dir)
    assert len(review["groups"]) == 2 and review["groups"][1]["where"][0]["page"] == 2
    dlg = IconReviewDialog(info.dir, review)
    _g, name, meaning, ko, _ni, _sc, _same = dlg.widgets[0]
    name.setText("coin")
    ko.setText("금화")
    same2 = dlg.widgets[1][6]
    same2.setCurrentIndex(same2.findText("1번과 같음"))
    assert not dlg.widgets[1][1].isEnabled()  # 합치면 이름 칸은 꺼진다
    rows = dlg.rows()
    assert [r["name"] for r in rows] == ["coin", "coin"] and rows[1]["ko"] == "금화"
    dlg.accept()
    assert dlg.result.added == ["coin"] and dlg.result.replaced == 2
    assert _source(fd, 1) == _source(fd, 2) == "AB[[coin]]CD"
    assert list(icons.load_dictionary(info)["icons"]) == ["coin"]


def test_icon_review_merge_cycle_is_blocked(qapp, tmp_path, monkeypatch):
    from app import icons
    from app.qt.steps import IconReviewDialog
    from tests.test_icons import _project as icon_project

    monkeypatch.setattr(icons, "SAME_SHAPE", 1.01)
    info, _fd = icon_project(tmp_path, "1차", pages=2)
    service.find_icons(info.dir, suggest=False)
    dlg = IconReviewDialog(info.dir, service.icon_review(info.dir))
    dlg.widgets[0][6].setCurrentIndex(dlg.widgets[0][6].findText("2번과 같음"))
    dlg.widgets[1][6].setCurrentIndex(dlg.widgets[1][6].findText("1번과 같음"))
    with pytest.raises(ValueError):
        dlg.rows()
    dlg.accept()
    assert dlg.result is None


@pytest.mark.parametrize("size", [(1366, 768), (1400, 850), (1600, 900), (1920, 1080)])
def test_region_editor_number_fields_not_clipped(qapp, tmp_path, size):
    from PySide6.QtWidgets import QAbstractSpinBox, QComboBox

    from app.qt.region_editor import RegionEditor

    info = _project(tmp_path)
    g = service.project_overview(info.dir)["groups"][0]
    service.save_region_template(g["dir"], {"regions": [
        {"name": "이름", "rect": {"x": 0.0863, "y": 0.0863, "w": 0.3, "h": 0.1}}]})
    ed = RegionEditor(Path(g["dir"]), [Path(f["dir"]) for f in g["files"]])
    ed.resize(*size)
    ed.show()
    qapp.processEvents()
    ed.items()[0].setSelected(True)
    qapp.processEvents()
    assert ed.minimumSizeHint().width() <= 900
    for sp in (ed.f_x, ed.f_y, ed.f_w, ed.f_h):
        need = sp.fontMetrics().horizontalAdvance("0.0863") + 30  # 글자 + 위/아래 버튼
        assert sp.width() >= need, (size, sp.width(), need)
        assert sp.lineEdit().width() >= sp.fontMetrics().horizontalAdvance("0.0863")
    assert ed.f_keep_tall.width() >= ed.f_keep_tall.fontMetrics().horizontalAdvance("200.0 pt") + 30
    for cb in (ed.f_kind, ed.f_align, ed.f_font):
        assert cb.width() >= 80
    ed.dirty = False
    ed.close()


# ---------------------------------------------------------------- 화면 개선 (ui-ux 검토 P1)
def test_korean_standard_buttons_and_action_names(qapp, tmp_path):
    """표준 버튼은 한국어 번역으로, 확인 버튼은 하는 일 이름으로."""
    from PySide6.QtWidgets import QDialogButtonBox

    from app.qt.dialogs import AddFilesDialog, ExcludePagesDialog, NewProjectDialog, SettingsDialog, install_korean

    assert install_korean(qapp)
    try:
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Yes)
        assert bb.button(QDialogButtonBox.StandardButton.Cancel).text() == "취소"
        info = _project(tmp_path)
        file_dir = service.project_overview(info.dir)["groups"][0]["files"][0]["dir"]
        for dlg, ok in ((NewProjectDialog(tmp_path), "만들기"), (AddFilesDialog(info.dir), "넣기"),
                        (ExcludePagesDialog(Path(file_dir), [], ""), "저장"), (SettingsDialog(), "저장")):
            bb = dlg.findChild(QDialogButtonBox)
            assert bb.button(QDialogButtonBox.StandardButton.Ok).text() == ok, type(dlg).__name__
            assert bb.button(QDialogButtonBox.StandardButton.Cancel).text() == "취소"
    finally:
        qapp.removeTranslator(qapp._ko_translator)


def test_region_editor_enter_in_name_keeps_zoom_and_help_folds(qapp, tmp_path, monkeypatch):
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QPushButton

    from app.qt import region_editor
    from app.qt.region_editor import RegionEditor

    info = _project(tmp_path)
    g = service.project_overview(info.dir)["groups"][0]
    service.save_region_template(g["dir"], {"regions": [{"name": "이름", "rect": {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.2}}]})
    ed = RegionEditor(Path(g["dir"]), [Path(f["dir"]) for f in g["files"]])
    ed.show()
    qapp.processEvents()
    assert all(not b.autoDefault() for b in ed.findChildren(QPushButton))
    ed.items()[0].setSelected(True)
    ed.view.auto_fit = False  # 확대해서 보는 중
    ed.f_name.setFocus()
    QTest.keyClick(ed.f_name, Qt.Key.Key_Return)
    qapp.processEvents()
    assert ed.view.auto_fit is False  # ‘화면에 맞춤’이 눌리지 않는다
    # 도움말: 낮은 화면(900 미만)에서는 접힌 채 시작, 버튼으로 펼침·접힘, 툴팁에 전체 문장
    low = ed.screen().availableGeometry().height() < 900
    assert ed.help_btn.isChecked() != low and ed.help_btn.text() == "단축키 도움말"
    assert "Ctrl+휠" in ed.help_btn.toolTip()
    ed.help_btn.setChecked(False)
    assert ed.help_label.isHidden()
    ed.help_btn.setChecked(True)
    assert not ed.help_label.isHidden()
    # 저장 확인 버튼 이름
    seen = {}

    def fake(parent, title, text, buttons, **k):
        seen["buttons"] = [t for t, _r in buttons]
        return 2

    monkeypatch.setattr(region_editor, "ask_choice", fake)
    ed.dirty = True
    assert ed._confirm_leave() is False and seen["buttons"] == ["저장", "저장 안 함", "취소"]
    ed.dirty = False
    ed.close()


def test_fit_to_screen_never_exceeds_screen(qapp):
    from PySide6.QtWidgets import QDialog

    from app.qt.dialogs import fit_to_screen

    g = qapp.primaryScreen().availableGeometry()
    w = QDialog()
    fit_to_screen(w, 5000, 5000, maximize_below=0)
    assert w.width() <= g.width() and w.height() <= g.height()
    w2 = QDialog()
    fit_to_screen(w2, 100, 100, maximize_below=g.height() + 1)  # 낮은 화면으로 보고 최대화
    assert w2.windowState() & Qt.WindowState.WindowMaximized
    w3 = QDialog()
    fit_to_screen(w3, 300, 200, maximize_below=0)
    assert (w3.width(), w3.height()) == (300, 200)


def test_result_viewer_refits_image_and_has_whole_card_row(qapp, tmp_path):
    from PySide6.QtWidgets import QLabel

    from app.qt.results import ResultViewer

    info, fd = _translated_project(tmp_path)
    service.erase_project(info.dir)
    service.compose_project(info.dir)
    v = ResultViewer([("카드/cards", fd / "translated")])
    g = qapp.primaryScreen().availableGeometry()
    assert v.width() <= g.width() and v.height() <= g.height()
    v.setWindowState(Qt.WindowState.WindowNoState)
    v.resize(500, 420)
    v.show()
    qapp.processEvents()
    small = v.image.pixmap().width()
    v.resize(min(1400, g.width()), min(900, g.height()))
    qapp.processEvents()
    qapp.processEvents()
    assert v.image.pixmap().width() > small
    vp = v.scroll.viewport()
    assert v.image.pixmap().width() <= vp.width() and v.image.pixmap().height() <= vp.height()
    assert not any("600" in w.text() for w in v.findChildren(QLabel))
    # 영역을 고르면 그 영역, ‘카드 전체’를 고르면 다시 카드
    path_card = v._img_path
    v.regions.setCurrentRow(1)
    assert v._img_path != path_card
    v.regions.setCurrentRow(0)
    assert v._img_path == path_card
    v.close()


def test_finish_asks_before_redoing_confirmed_glossary(qapp, tmp_path, monkeypatch):
    from app.qt import steps, worker
    from app.qt.main import MainWindow

    info, _fd = _project_with_extract(tmp_path)
    workspace.set_status(workspace.load_project_info(info.dir), "translated", glossary_version=6)
    win = MainWindow(root=tmp_path)
    win.open_project(info.dir)
    act = win.project.steps
    started = []
    monkeypatch.setattr(act, "_engine", lambda: "claude-code")
    monkeypatch.setattr(worker, "run_with_dialog", lambda *a, **k: started.append(a[1]))
    asked = {}

    def answer(n):
        def f(parent, title, text, buttons, **k):
            asked.update(title=title, text=text, buttons=[t for t, _r in buttons], more=k.get("informative", ""))
            return n
        return f

    monkeypatch.setattr(steps, "ask_choice", answer(1))  # 취소
    act.finish()
    assert started == [] and asked["title"] == "용어집 다시 만들기" and "v6" in asked["text"]
    assert asked["buttons"] == ["다시 만들기", "취소"] and "용어집 검증 중" in asked["more"]
    monkeypatch.setattr(steps, "ask_choice", answer(0))
    act.finish()
    assert len(started) == 1
    # 아직 확정 전이면 묻지 않는다
    workspace.set_status(workspace.load_project_info(info.dir), "extracting")
    win.project.refresh()
    asked.clear()
    act.finish()
    assert not asked and len(started) == 2


def test_screens_do_not_show_developer_words(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from app.qt.main import MainWindow
    from app.qt.steps import state_label

    shown = []
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda p, title, text, *a, **k: shown.append((title, text))))
    info, fd = _translated_project(tmp_path)
    win = MainWindow(root=tmp_path)
    win.open_project(info.dir)
    act = win.project.steps
    r = service.ProjectExtractResult("blocked", files=[{"file": "카드/cards", "state": "blocked", "done": 0,
                                                        "remaining": 2, "failed": 0}], reason="엔진 없음")
    act._extract_done(r, (None, "double-check", "claude-code"))
    act._finish_done(service.GlossaryReviewResult("blocked", reason="x"))
    first = service.erase_project(info.dir)
    again = service.erase_project(info.dir)  # 이미 지운 파일: 건너뜀 문구
    win.project.out._erase_done(again)
    assert first.state == "done"
    assert state_label("done") == "끝남" and state_label("blocked") == "할 수 없음" and state_label("x") == "x"
    text = "\n".join(f"{t}\n{x}" for t, x in shown)
    for bad in ("done", "blocked", "skipped", "paused", "--redo", "erase-project", "cleared/", "translated/", "600dpi"):
        assert bad not in text, (bad, text)
    assert "건너뜀" in text and "다시 지우려면 ‘모두 다시 지우기’" in text
    assert any("할 수 없음" in t for t, _x in shown)  # 창 제목도 한국어


def test_source_review_protects_unsaved_edit(qapp, tmp_path, monkeypatch):
    from app.qt import steps
    from app.qt.steps import SourceReviewDialog

    info, fd = _project_with_extract(tmp_path)
    files = service.project_overview(info.dir)["groups"][0]["files"]
    dlg = SourceReviewDialog(files)
    dlg.show()
    qapp.processEvents()
    # 확인할 칸과 아직 읽지 않은 칸을 따로 센다
    assert dlg.count.text() == "확인할 칸 1 · 아직 읽지 않은 칸 2 / 전체 6"
    assert "아직 읽지 않음" in dlg.only.text()
    dlg.only.setChecked(False)
    assert dlg.table.rowCount() == 6
    dlg.only.setChecked(True)
    assert dlg.table.rowCount() == 3
    asked = []

    def answer(n):
        def f(parent, title, text, buttons, **k):
            asked.append((title, text, [t for t, _r in buttons]))
            return n
        return f

    def saved(page=1, region="이름"):
        return next(c for c in service.extracted_cells(fd) if (c["page"], c["region"]) == (page, region))

    # 글을 고치고 다른 줄을 누르면 묻는다: 취소 -> 원래 줄·고친 글 그대로, 저장 안 됨
    dlg.edit.setPlainText("盗掘フェレット")
    monkeypatch.setattr(steps, "ask_choice", answer(2))
    dlg.table.setCurrentCell(1, 0)
    assert dlg.table.currentRow() == 0 and dlg.edit.toPlainText() == "盗掘フェレット"
    assert asked[0] == ("고친 원문", "1쪽 이름의 고친 원문을 저장할까요?", ["저장", "저장 안 함", "취소"])
    assert saved()["source"] == "盗賊フェレット"
    # 저장 안 함 -> 옮겨 가고 저장되지 않음
    monkeypatch.setattr(steps, "ask_choice", answer(1))
    dlg.table.setCurrentCell(1, 0)
    assert dlg.current()["page"] == 3 and saved()["source"] == "盗賊フェレット"
    # 같은 줄의 다른 열을 눌러도 고친 글이 사라지거나 묻지 않는다
    dlg.edit.setPlainText("手で直した")
    n = len(asked)
    dlg.table.setCurrentCell(1, 3)
    assert dlg.edit.toPlainText() == "手で直した" and len(asked) == n
    # 저장 -> 저장하고 옮겨 감
    monkeypatch.setattr(steps, "ask_choice", answer(0))
    dlg.table.setCurrentCell(0, 0)
    assert dlg.saved == 1 and dlg.current() is not None
    assert any(c["edited"] and c["source"] == "手で直した" for c in service.extracted_cells(fd))
    # 닫기도 고친 글이 있으면 묻는다 (취소하면 그대로)
    dlg.edit.setPlainText(dlg.edit.toPlainText() + "!")
    monkeypatch.setattr(steps, "ask_choice", answer(2))
    dlg.accept()
    assert dlg.isVisible()
    monkeypatch.setattr(steps, "ask_choice", answer(1))
    dlg.accept()
    assert not dlg.isVisible()


def test_source_review_save_and_next_and_keys(qapp, tmp_path):
    from PySide6.QtTest import QTest

    from app.qt.steps import SourceReviewDialog

    info, fd = _project_with_extract(tmp_path)
    files = service.project_overview(info.dir)["groups"][0]["files"]
    dlg = SourceReviewDialog(files, only_check=False)
    dlg.show()
    qapp.processEvents()
    assert dlg.table.rowCount() == 6 and dlg.table.currentRow() == 0
    # Ctrl+Enter(편집기에 포커스): 저장하고 다음 칸
    dlg.edit.setFocus()
    dlg.edit.setPlainText("盗掘フェレット")
    QTest.keyClick(dlg.edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert dlg.saved == 1 and dlg.table.currentRow() == 1
    assert next(c for c in service.extracted_cells(fd) if (c["page"], c["region"]) == (1, "이름"))["source"] == "盗掘フェレット"
    # Alt+아래/위로 칸 이동
    QTest.keyClick(dlg.edit, Qt.Key.Key_Down, Qt.KeyboardModifier.AltModifier)
    assert dlg.table.currentRow() == 2
    QTest.keyClick(dlg.edit, Qt.Key.Key_Up, Qt.KeyboardModifier.AltModifier)
    assert dlg.table.currentRow() == 1
    # Ctrl+S: 저장하고 그 칸에 머문다
    dlg.edit.setPlainText("人")
    QTest.keyClick(dlg.edit, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    assert dlg.saved == 2 and dlg.table.currentRow() == 1
    dlg.edit.setPlainText("人人")
    dlg.next_btn.click()
    assert dlg.saved == 3 and dlg.table.currentRow() == 2
    # ‘확인할 칸만’일 때는 저장한 칸이 빠지므로 같은 줄 번호가 다음 칸
    dlg2 = SourceReviewDialog(files, only_check=True)
    first, n_before = dlg2.current(), dlg2.table.rowCount()
    assert n_before >= 2
    dlg2.edit.setPlainText("盗掘")
    dlg2.save_and_next()
    assert dlg2.table.rowCount() == n_before - 1 and dlg2.table.currentRow() == 0 and dlg2.current() != first
    dlg.close()


def test_selftest_reports_korean_translation(qapp, tmp_path):
    """exe 점검(--selftest)이 표준 버튼 한국어 번역을 불렀는지 알려 준다."""
    from app.qt.main import main

    out = tmp_path / "st.json"
    try:
        main(["prog", "--selftest", str(out)])
    finally:
        tr = getattr(qapp, "_ko_translator", None)
        if tr is not None:
            qapp.removeTranslator(tr)
    res = json.loads(out.read_text(encoding="utf-8"))
    assert res["ko_translation"] is True and res["ko_cancel_text"] == "취소" and res["ui"] is True
