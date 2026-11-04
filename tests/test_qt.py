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
