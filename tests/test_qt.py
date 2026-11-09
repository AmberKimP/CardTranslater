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
    g = win.home.tree.topLevelItem(0)  # 게임 > 프로젝트
    assert win.home.tree.topLevelItemCount() == 1 and g.text(0) == "G" and g.child(0).text(0) == "p"
    win.open_project(info.dir)
    assert win.stack.currentWidget() is win.project
    allit = win.project.tree.topLevelItem(0)  # 맨 위 ‘전체’ 아래에 그룹 > 파일
    assert allit.text(0).startswith("전체") and win.project.tree.currentItem() is allit
    top = allit.child(0)
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
    # 화면에는 쉬운 말: ‘결론 못 냄’ → ‘다시 봐야 함’, 모델 이름 대신 ‘보통 읽기’·‘정밀 읽기’
    assert dlg.table.rowCount() == 3 and dlg.table.item(0, 2).text() == "다시 봐야 함"
    rows = [(h.text(), v.text()) for h, v in dlg.reading_rows]
    assert [h for h, _v in rows] == ["보통 읽기", "정밀 읽기"] and "フェレット" in rows[1][1]
    assert "사유:" in dlg.info.text() and dlg.image.pixmap() is not None
    assert not any(w in dlg.info.text() + str(rows) for w in ("Opus", "Sonnet", "claude-"))
    # 원문 칸은 일본어가 잘 보이는 글꼴 (있으면 Yu Gothic UI → Meiryo, 없으면 기본)
    fams = dlg.edit.font().families()
    assert fams[-1] and all(f in ("Yu Gothic UI", "Meiryo") for f in fams[:-1])
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
                  "translate": False, "check": False, "erase": True, "compose": False, "results": False,
                  "test": False}
    workspace.set_status(workspace.load_project_info(info.dir), "glossary_confirmed")
    win.project.refresh()
    # 확정 뒤에도 고친 용어집 엑셀을 다시 가져올 수 있다
    assert win.project.step_buttons["translate"].isEnabled() and win.project.step_buttons["glossary"].isEnabled()


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
    v._wheel(-120)  # 아래로 한 칸: 다음 쪽
    assert v.pages.currentRow() == 1
    v._wheel(-120)  # 마지막 쪽에서 더 내려도 그대로
    assert v.pages.currentRow() == 1
    v._wheel(60)
    assert v.pages.currentRow() == 1  # 터치패드의 작은 값은 모아서
    v._wheel(60)
    assert v.pages.currentRow() == 0
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
    assert not win.project.b_delete.isEnabled()  # ‘전체’는 지울 수 없다
    win.project.tree.setCurrentItem(win.project.tree.topLevelItem(0).child(0))
    assert win.project.b_delete.isEnabled()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    win.project.delete_selected()
    assert service.project_overview(info.dir)["groups"] == []
    # 홈: 프로젝트 삭제. 게임(용어집 폴더)은 남는다
    win.show_home()
    assert win.home.select_project("p")
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
    ed.per_card.setChecked(True)  # 지금(공통) 영역이 모든 카드에 그대로 남는다
    assert "이 카드: 영역 1개" in ed.card_info.text() and "지정한 카드 3/3" in ed.card_info.text()
    ed.step_page(1)  # 2쪽: 공통 영역을 그대로 가짐 -> 이 카드만 비우고 새로 그린다
    assert [i.name for i in ed.items()] == ["이름"]
    ed.items()[0].setSelected(True)
    ed.delete_selected()
    ed._stash_card()
    ed._load_card(ed.card_key())
    assert ed.items() == []
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

    monkeypatch.setattr("app.service.find_claude", lambda: None)
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


def test_icon_review_warns_when_new_icons_share_a_name(qapp, tmp_path, monkeypatch):
    """다른 줄의 새 아이콘에 같은 이름(gold, gold)을 적으면 주황으로 표시하고, 확정할 때 합칠지 묻는다."""
    from app import icons
    from app.qt import steps as steps_mod
    from app.qt.steps import IconReviewDialog
    from tests.test_icons import _project as icon_project

    monkeypatch.setattr(icons, "SAME_SHAPE", 1.01)
    info, _fd = icon_project(tmp_path, "1차", pages=2)
    service.find_icons(info.dir, suggest=False)
    dlg = IconReviewDialog(info.dir, service.icon_review(info.dir))
    for w in dlg.widgets:
        w[1].setText("gold")
        w[3].setText("골드")
    assert dlg._dup_names() == {"gold": [0, 1]} and "F4B183" in dlg.widgets[1][1].styleSheet()
    asked = []
    monkeypatch.setattr(steps_mod, "ask_choice", lambda *a, **k: asked.append(a[2]) or 1)  # 돌아가서 고치기
    dlg.accept()
    assert dlg.result is None and "gold: 1번, 2번" in asked[0]
    dlg.widgets[1][1].setText("gold_2")  # 숫자가 붙은 금화: 다른 이름
    assert dlg._dup_names() == {} and dlg.widgets[1][1].styleSheet() == ""
    dlg.accept()
    assert dlg.result is not None and len(asked) == 1


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
                        (ExcludePagesDialog(Path(file_dir), [], ""), "체크한 쪽 빼기"), (SettingsDialog(), "저장")):
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
    assert ed.help_btn.isChecked() != low and ed.help_btn.text() == "단축키 도움말 (F1)"
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
    assert started == [] and asked["title"] == "용어집 다시 만들기" and "6번째 확정본" in asked["text"]
    assert "v6" not in asked["text"]
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
    # 오류 창(show_error)은 QMessageBox를 만들어 exec한다: 제목·본문·할 일·자세히를 모두 모은다
    monkeypatch.setattr(QMessageBox, "exec", lambda self: shown.append(
        (self.windowTitle(), "\n".join([self.text(), self.informativeText(), self.detailedText()]))))
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
    assert "건너뜀" in text and "‘그래도 다시 지우기’" in text
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
    assert dlg.edit.toPlainText() == "盗掘フェレット"  # 다시 봐야 하는 칸은 정밀 읽기 결과로 시작
    dlg.edit.setPlainText("盗掘フェレット改")
    monkeypatch.setattr(steps, "ask_choice", answer(2))
    dlg.table.setCurrentCell(1, 0)
    assert dlg.table.currentRow() == 0 and dlg.edit.toPlainText() == "盗掘フェレット改"
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


def test_card_erase_mode_override_and_redo(qapp, tmp_path):
    """결과 보기에서 카드 한 장의 영역만 다른 방식으로 다시 지우고 한글을 다시 넣는다."""
    from app.qt.results import ResultViewer

    info, fd = _translated_project(tmp_path)
    service.erase_project(info.dir)
    service.compose_project(info.dir)
    region = next(iter(service.card_erase_info(fd, 1)))
    with pytest.raises(ValueError):
        service.set_card_erase_mode(fd, 1, region, "bogus")
    service.set_card_erase_mode(fd, 1, region, "rect_avg")
    assert workspace.file_settings(fd)["erase_modes"] == {"1": {region: "rect_avg"}}
    r = service.redo_file(fd)
    assert r.state == "done"
    info1, info2 = service.card_erase_info(fd, 1)[region], service.card_erase_info(fd, 2)[region]
    assert info1["mode"] == "rect_avg" and info1["used"] == "rect_avg" and info1["text_mask"]
    assert info2["mode"] is None and not info2["text_mask"]  # 다른 카드는 영역 설정대로 (예전 방식)
    v = ResultViewer([("카드/cards", fd / "translated")])
    v.regions.setCurrentRow(1)
    assert v.erase_mode.currentData() == "rect_avg" and not v.redo_btn.isEnabled()  # 고친 것이 없으면 꺼짐
    v.erase_mode.setCurrentIndex(0)
    assert v.redo_btn.isEnabled()
    assert "지우기: 사각형으로 덮기" in v.regions.item(1).text()
    service.set_card_erase_mode(fd, 1, region, None)  # 영역 설정대로 되돌리기
    assert "erase_modes" not in workspace.file_settings(fd)


def test_home_new_project_starts_with_empty_game(qapp, tmp_path, monkeypatch):
    """‘+ 새 프로젝트’는 고른 게임이 있어도 게임 이름을 비운 채 연다 (사용자 요청). 목록에는 기존 게임이 있다."""
    from app.qt import main as qmain
    from app.qt.dialogs import NewProjectDialog
    from app.qt.main import MainWindow

    _project(tmp_path)
    win = MainWindow(root=tmp_path)
    seen = []

    class Fake:
        created = None

        def __init__(self, root, parent=None, game=None):
            seen.append(game)

        def exec(self):
            return 0

    monkeypatch.setattr(qmain, "NewProjectDialog", Fake)
    win.home.tree.setCurrentItem(win.home.tree.topLevelItem(0))  # 게임을 고른 채
    win.home.new_project()
    assert seen == [None]
    d = NewProjectDialog(tmp_path)
    assert d.game.currentText() == "" and d.game.count() >= 1


def test_region_editor_erase_mode_saves_and_loads(qapp, tmp_path):
    """영역 지정 창의 ‘글자 지우기’ 콤보가 regions.json erase.mode로 저장되고 다시 열면 보이며, 자동이면 키가 없어진다."""
    from app.qt.region_editor import RegionEditor

    info = _project(tmp_path)
    g = service.project_overview(info.dir)["groups"][0]
    gdir = Path(g["dir"])
    service.save_region_template(gdir, {"regions": [
        {"name": "본문", "kind": "text", "rect": {"x": 0.1, "y": 0.5, "w": 0.5, "h": 0.2}}]})
    files = [Path(f["dir"]) for f in g["files"]]
    ed = RegionEditor(gdir, files)
    it = ed.items()[0]
    it.setSelected(True)
    ed.f_erase_mode.setCurrentIndex(ed.f_erase_mode.findData("inpaint"))
    ed._from_form()
    assert ed.save()
    data = json.loads((gdir / "regions.json").read_text(encoding="utf-8"))
    assert data["regions"][0]["erase"]["mode"] == "inpaint"
    ed2 = RegionEditor(gdir, files)
    ed2.items()[0].setSelected(True)
    assert ed2.f_erase_mode.currentData() == "inpaint"
    ed2.f_erase_mode.setCurrentIndex(ed2.f_erase_mode.findData("auto"))
    ed2._from_form()
    assert ed2.save()
    data = json.loads((gdir / "regions.json").read_text(encoding="utf-8"))
    assert "erase" not in data["regions"][0]
    ed.dirty = ed2.dirty = False


def test_exclude_dialog_send_to_group(qapp, tmp_path, monkeypatch):
    """‘뺄 쪽’ 창에서 체크한 쪽을 다른 그룹으로 보내면 이 그룹에서는 빠지고, 체크가 없으면 아무것도 하지 않는다."""
    from PySide6.QtWidgets import QInputDialog

    from app.qt.dialogs import ExcludePagesDialog

    info = _project(tmp_path)
    fd = Path(service.project_overview(info.dir)["groups"][0]["files"][0]["dir"])
    e = ExcludePagesDialog(fd, [])
    e.send_to_group()  # 체크 없음
    assert not e.moved and workspace.included_pages(fd) == [1, 2, 3]
    e.list.item(1).setCheckState(Qt.CheckState.Checked)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("정예", True)))
    assert e.target.currentData() is None  # 다른 그룹이 없으면 ‘새 그룹 만들기’
    e.send_to_group()
    assert e.moved and workspace.included_pages(fd) == [1, 3]
    names = [x["name"] for x in service.list_groups(info.dir)]
    assert "정예" in names


# ================================================================ 쉬운 화면: 다음 할 일 버튼·단계 표시·빈 화면·오류 안내
def _boxes(monkeypatch):
    """show_error가 띄우는 창을 모은다: (제목, 본문, 할 일, 자세히)."""
    from PySide6.QtWidgets import QMessageBox

    got = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: got.append(
        (self.windowTitle(), self.text(), self.informativeText(), self.detailedText())))
    return got


def test_home_has_no_path_and_shows_empty_guide(qapp, tmp_path):
    from PySide6.QtWidgets import QLabel

    from app.qt.main import MainWindow

    win = MainWindow(root=tmp_path)
    assert win.home.body.currentWidget() is win.home.empty and "새 프로젝트" in win.home.empty.text()
    assert not any(str(tmp_path) in w.text() for w in win.home.findChildren(QLabel))  # 데이터 폴더 경로를 보이지 않는다
    _project(tmp_path)
    win.home.refresh()
    assert win.home.body.currentWidget() is win.home.tree


def test_next_button_runs_next_step_and_steps_show_progress(qapp, tmp_path, monkeypatch):
    from app.qt.main import MainWindow

    # 빈 프로젝트: 표 대신 안내, 큰 버튼은 ‘파일 넣기’
    info, _ = workspace.create_project("빈", "G", root=tmp_path)
    win = MainWindow(root=tmp_path)
    win.open_project(info.dir)
    page = win.project
    assert page.body.currentWidget() is page.empty and page.next_btn.text() == "▶ 파일 넣기"
    called = []
    monkeypatch.setattr(page, "add_files", lambda: called.append("add"))
    page.do_next()
    assert called == ["add"]
    # 영역 없는 그룹: ‘영역 지정’ 버튼이 그 그룹을 연다. 꺼진 단계 버튼은 이유를 툴팁으로
    info = _project(tmp_path)
    win.open_project(info.dir)
    assert page.body.currentWidget() is page.tree and page.next_btn.text() == "▶ 영역 지정"
    assert "영역을 지정하세요" in page.next.text()
    monkeypatch.setattr(page, "_open_editor", lambda g: called.append(g["name"]))
    page.do_next()
    assert called[-1] == "카드"
    b = page.step_buttons["review"]
    assert not b.isEnabled() and "먼저 ① 글자 추출" in b.toolTip()
    # 추출 중: ① 글자 추출이 ‘지금’, 큰 버튼도 같은 동작
    info2, _fd = _project_with_extract(tmp_path / "x")
    win2 = MainWindow(root=tmp_path / "x")
    win2.open_project(info2.dir)
    p2 = win2.project
    assert p2.ov["next_key"] == "extract" and p2.step_buttons["extract"].property("step") == "now"
    assert p2.next_btn.text() == "▶ ① 글자 추출"
    monkeypatch.setitem(p2.step_fns, "extract", lambda: called.append("extract"))
    p2.do_next()
    assert called[-1] == "extract"
    # 파일 표: 쉬운 표기
    fi = p2.tree.topLevelItem(0).child(0).child(0)
    assert fi.text(3) == "2/3장" and fi.text(4) == "—" and fi.text(5) == "—" and fi.text(2) == "—"


def test_translated_project_marks_done_steps(qapp, tmp_path):
    from app.qt.main import MainWindow

    info, fd = _translated_project(tmp_path)
    service.erase_project(info.dir)
    service.compose_project(info.dir)
    win = MainWindow(root=tmp_path)
    win.open_project(info.dir)
    page = win.project
    st = page.step_states()
    assert st["translate"] == "done" and st["erase"] == "done" and st["compose"] == "done"
    assert st["results"] == "now" and page.next_btn.text() == "▶ 결과 보기"
    b = page.step_buttons["translate"]
    assert b.text().endswith("✓") and b.property("step") == "done" and "끝난 단계" in b.toolTip()
    fi = page.tree.topLevelItem(0).child(0).child(0)
    assert fi.text(4) == "완료" and fi.text(5) == "완료"
    assert "v" not in page.sub.text()  # 용어집 버전은 ‘N번째 확정본’으로


def test_card_limit_banner_and_turn_off(qapp, tmp_path):
    from app.qt.main import MainWindow

    info = _project(tmp_path)
    service.set_card_limit(info.dir, 2)
    win = MainWindow(root=tmp_path)
    win.open_project(info.dir)
    page = win.project
    assert not page.banner.isHidden() and "앞 2장만" in page.banner_text.text()
    assert "(시험)" in page.tree.topLevelItem(0).child(0).child(0).text(3)
    page.turn_off_card_limit()
    assert page.banner.isHidden() and service.project_overview(info.dir)["card_limit"] is None


def test_error_boxes_say_why_and_what_to_do(qapp, monkeypatch):
    from app.qt.dialogs import UNKNOWN_TODO, explain_error, show_failure, show_problem

    got = _boxes(monkeypatch)
    # 파일이 열려 있음: 쉬운 안내, 원문은 ‘자세히 보기’
    raw = "[Errno 13] Permission denied: 'C:/x/검수.xlsx'\n\nTraceback (most recent call last):\n  File ..."
    show_failure(None, "검수 엑셀을 만들지 못했습니다", raw)
    title, why, todo, detail = got[-1]
    assert title == "검수 엑셀을 만들지 못했습니다" and "열려 있어" in why and "닫고" in todo
    assert "Permission denied" not in why + todo and "Traceback" in detail and "Permission denied" in detail
    # 사용 한도로 멈춤: 언제 다시 하면 되는지
    show_problem(None, "번역을 끝내지 못했습니다", "rate limit (429)", retry_after="2026-11-05 14:30:00")
    _t, why, todo, detail = got[-1]
    assert "사용 한도" in why and "2026-11-05 14:30:00 이후" in todo and "rate limit" in detail
    # 로그인 문제
    assert "로그인 방식" in explain_error("NotAvailable: Claude Code에 로그인되어 있지 않습니다")[1]
    # 모르는 오류: 다시 해 보고 ‘자세히 보기’ 내용을 알려 달라고
    show_failure(None, "글자 지우기를 끝내지 못했습니다", "ZeroDivisionError: division by zero\nTraceback ...")
    _t, why, todo, detail = got[-1]
    assert todo == UNKNOWN_TODO and "ZeroDivisionError" not in why and "ZeroDivisionError" in detail
    # 엔진이 쓴 이유가 여러 줄이면 첫 줄 + 나머지 건수
    show_problem(None, "용어집을 가져오지 못했습니다", "炎: 번역이 비어 있음\n氷: 번역이 비어 있음", "엑셀을 고치세요.")
    _t, why, todo, detail = got[-1]
    assert why.startswith("炎: 번역이 비어 있음") and "외 1건" in why and "氷" in detail and todo == "엑셀을 고치세요."


def test_step_failures_use_plain_error_boxes(qapp, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.qt.main import MainWindow

    got = _boxes(monkeypatch)
    info = _project(tmp_path)
    win = MainWindow(root=tmp_path)
    win.open_project(info.dir)
    act = win.project.steps
    act._translate_done(SimpleNamespace(state="paused", reason="사용 한도에 걸림", retry_after="2026-11-05 14:30:00"))
    title, why, todo, _d = got[-1]
    assert title.startswith("번역을 끝내지 못했습니다") and "사용 한도" in why and "14:30:00 이후" in todo
    from app.qt.steps import _fail

    _fail(win.project, "PermissionError: [WinError 32] 다른 프로세스가 파일을 사용 중\nTraceback ...")
    title, why, todo, detail = got[-1]
    assert title == "작업을 끝내지 못했습니다" and "닫고" in todo and "WinError 32" in detail


def test_region_editor_per_card_copy_rest_reset_and_sync_by_name(qapp, tmp_path, monkeypatch):
    """카드마다 영역: 나머지 카드에 복사(빈 카드만/모두) · 초기화(이 카드/모두) · 설정은 같은 이름 영역에 함께."""
    from PySide6.QtWidgets import QMessageBox

    from app.qt import region_editor as re_mod
    from app.qt.region_editor import RegionEditor

    info = _project(tmp_path)  # 3쪽
    g = service.project_overview(info.dir)["groups"][0]
    service.save_region_template(g["dir"], {"regions": [
        {"name": "이름", "rect": {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.1}}]})
    ed = RegionEditor(Path(g["dir"]), [Path(f["dir"]) for f in g["files"]])
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    ed.per_card.setChecked(True)
    fname = Path(g["files"][0]["dir"]).name
    ed._cards()[f"{fname}/3"] = {"regions": [{"name": "다른", "kind": "text",
                                              "rect": {"x": 0.5, "y": 0.5, "w": 0.2, "h": 0.1}}], "protect_rects": []}
    answers = iter([0, 1])  # 처음: 빈 카드에만, 다음: 모두 덮어쓰기
    monkeypatch.setattr(re_mod, "ask_choice", lambda *a, **k: next(answers))
    ed.copy_to_rest()
    assert ed._cards()[f"{fname}/2"]["regions"][0]["name"] == "이름"
    assert ed._cards()[f"{fname}/3"]["regions"][0]["name"] == "다른"  # 영역 있던 카드는 그대로
    ed.copy_to_rest()
    assert ed._cards()[f"{fname}/3"]["regions"][0]["name"] == "이름"
    # 설정은 같은 이름 영역에 함께 (위치는 카드마다)
    ed._cards()[f"{fname}/2"]["regions"][0]["rect"] = {"x": 0.2, "y": 0.6, "w": 0.3, "h": 0.1}
    ed.items()[0].setSelected(True)
    ed.f_rotate.setChecked(True)
    ed.f_name.setText("위 효과")
    ed._from_form()
    r2 = ed._cards()[f"{fname}/2"]["regions"][0]
    assert r2["name"] == "위 효과" and r2["style"]["rotate"] == 180 and r2["rect"]["y"] == 0.6
    assert "적용했습니다" in ed.status.text()
    # 초기화: 이 카드만 -> 모든 카드
    answers2 = iter([0, 1])
    monkeypatch.setattr(re_mod, "ask_choice", lambda *a, **k: next(answers2))
    ed.reset_regions()
    assert ed.items() == [] and f"{fname}/2" in ed._cards()
    ed.reset_regions()
    ed._stash_card()
    assert not any(v.get("regions") for v in ed._cards().values())
    ed.dirty = False


def test_region_editor_shortcuts_and_fit_without_scrollbars(qapp, tmp_path):
    from PySide6.QtGui import QKeyEvent

    from app.qt.region_editor import RegionEditor

    info = _project(tmp_path)  # 3쪽
    g = service.project_overview(info.dir)["groups"][0]
    service.save_region_template(g["dir"], {"regions": [
        {"name": "이름", "rect": {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.1}},
        {"name": "본문", "rect": {"x": 0.1, "y": 0.5, "w": 0.6, "h": 0.2}}]})
    ed = RegionEditor(Path(g["dir"]), [Path(f["dir"]) for f in g["files"]])
    ed.resize(900, 600)
    ed.show()
    qapp.processEvents()
    v = ed.view
    # 화면에 맞춤: 카드 전체가 보이고 스크롤 막대 없음
    vis = v.mapToScene(v.viewport().rect()).boundingRect()
    assert vis.contains(ed.scene.page_rect)
    assert not v.verticalScrollBar().isVisible() and not v.horizontalScrollBar().isVisible()

    def key(k, mods=Qt.KeyboardModifier.NoModifier):
        v.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, k, mods))

    key(Qt.Key.Key_T)
    assert ed.scene.add_kind == "text"
    key(Qt.Key.Key_T)
    assert ed.scene.add_kind is None
    key(Qt.Key.Key_End)
    assert ed.page_spin.value() == 3
    key(Qt.Key.Key_Home)
    assert ed.page_spin.value() == 1
    ed._key_action("next_region")
    assert ed.selected().name == "이름"
    ed._key_action("next_region")
    assert ed.selected().name == "본문"
    key(Qt.Key.Key_R)
    assert ed.selected().data["style"]["rotate"] == 180
    ed.duplicate_selected()
    assert [i.name for i in ed.items()] == ["이름", "본문", "본문2"]
    ed.scene.clearSelection()
    ed.copy_regions()  # 고른 것 없으면 이 카드 전부
    assert len(ed._clip) == 3
    ed.paste_regions()
    assert len(ed.items()) == 6 and len({i.name for i in ed.items()}) == 6  # 이름은 겹치지 않게
    ed.dirty = False
    ed.close()


def _two_group_project(tmp_path):
    info = _project(tmp_path)
    service.add_files(info.dir, [_pdf(tmp_path / "in" / "more.pdf")], "둘째", new_group=True)
    return info


def test_project_tree_all_row_scope_and_buttons_use_selection(qapp, tmp_path, monkeypatch):
    """그리드 맨 위 ‘전체’. 작업·결과 버튼은 고른 범위(전체·그룹·파일)에 대해 한다."""
    from app.qt import results as res_mod
    from app.qt.main import MainWindow

    info = _two_group_project(tmp_path)
    win = MainWindow(root=tmp_path)
    win.open_project(info.dir)
    page = win.project
    allit = page.tree.topLevelItem(0)
    assert page.tree.topLevelItemCount() == 1 and allit.childCount() == 2
    assert page.scope() == ("전체", None) and "전체" in page.scope_label.text()
    g2 = next(allit.child(i) for i in range(allit.childCount()) if allit.child(i).text(0).startswith("둘째"))
    page.tree.setCurrentItem(g2)
    label, dirs = page.scope()
    assert "둘째" in label and len(dirs) == 1 and "둘째" in page.scope_label.text()
    page.tree.setCurrentItem(g2.child(0))
    assert page.scope()[0].startswith("파일") and len(page.scoped_files()) == 1
    # 새로 고쳐도 고른 항목 유지
    page.refresh()
    assert page.scope()[0].startswith("파일")
    # ⑦ 지우기: 고른 파일만 erase_project(files=...)로
    seen = {}

    def fake_run(parent, title, job, done, fail, **k):
        seen["files"] = job.kwargs.get("files")
        job.signals.done.emit(service.ProjectStepResult("done"))  # run_exclusive가 뒤에서 지우기를 놓아 준다
        done(service.ProjectStepResult("done"))
    monkeypatch.setattr(res_mod.worker, "run_with_dialog", fake_run)
    monkeypatch.setattr(res_mod.QMessageBox, "information", lambda *a, **k: None)
    all_dirs = [f["dir"] for g in page.ov["groups"] for f in g["files"]]
    monkeypatch.setattr(service, "files_needing_erase",  # 아직 안 지운 파일들 (지울 것만 지운다)
                        lambda _p, files=None: [str(d) for d in (files if files is not None else all_dirs)])
    import app.qt.dialogs as dlg_mod

    answers = []
    monkeypatch.setattr(dlg_mod, "ask_choice", lambda *a, **k: answers.pop(0))
    answers.append(1)  # 파일을 고른 채 ⑦: 전체/고른 항목 중 ‘고른 항목만’
    page.out.erase()
    assert seen["files"] == sorted(str(d) for d in dirs)
    answers.append(0)  # ‘전체’를 고르면 목록 선택도 전체로
    page.out.erase()
    assert seen["files"] == sorted(str(d) for d in all_dirs) and page.scope()[0] == "전체"
    page.out.erase()  # 전체를 고른 채면 묻지 않는다
    assert answers == [] and page.bg.holds == 0 and page.bg.idle


def test_extract_asks_all_or_selected_and_moves_grid_to_all(qapp, tmp_path, monkeypatch):
    from app.qt import steps as steps_mod
    from app.qt.main import MainWindow

    info = _two_group_project(tmp_path)
    for g in service.project_overview(info.dir)["groups"]:
        service.save_region_template(g["dir"], {"regions": [
            {"name": "이름", "kind": "text", "rect": {"x": 0.1, "y": 0.7, "w": 0.6, "h": 0.15}}]})
    win = MainWindow(root=tmp_path)
    win.open_project(info.dir)
    page = win.project
    page.tree.setCurrentItem(page.tree.topLevelItem(0).child(1))
    asked = []
    answers = iter([1, 0])  # 처음: 고른 항목만, 다음: 전체
    monkeypatch.setattr("app.qt.dialogs.ask_choice", lambda *a, **k: (asked.append(a[2]), next(answers))[1])

    class FakeDlg:
        def __init__(self, *a, **k):
            pass

        def exec(self):
            return True

        def card_limit(self):
            return None

        def choice(self):
            return None, "double-check", "claude-code"
    monkeypatch.setattr(steps_mod, "ExtractDialog", FakeDlg)
    runs = []
    monkeypatch.setattr(steps_mod.StepActions, "_run_extract", lambda self, files, m, e: runs.append(files))
    page.steps.extract()
    assert len(runs[0]) == 1 and "고른" not in asked[0]
    page.steps.extract()
    assert runs[1] is None and page.tree.currentItem() is page.tree.topLevelItem(0)  # 전체로 옮김
    # 이미 ‘전체’를 고른 상태면 묻지 않는다
    page.steps.extract()
    assert len(asked) == 2 and runs[2] is None


def test_reset_keeps_files_and_regions_unlike_delete(qapp, tmp_path, monkeypatch):
    from app.qt.main import MainWindow

    info, fd = _project_with_extract(tmp_path)
    (fd / "cleared").mkdir()
    (fd / "cleared" / "cleared.pdf").write_bytes(b"x")
    (fd / "translated").mkdir()
    workspace.set_status(workspace.load_project_info(info.dir), "translated")
    r = service.reset_files(info.dir, [fd], "output")
    assert len(r["moved"]) == 2 and not (fd / "cleared").exists() and (fd / "extract").exists()
    assert workspace.load_project_info(info.dir).status == "translated"
    # 화면: 전체 초기화 -> 추출부터 모두
    win = MainWindow(root=tmp_path)
    win.open_project(info.dir)
    monkeypatch.setattr("app.qt.dialogs.ask_choice", lambda *a, **k: 1)
    win.project.reset_selected()
    assert not (fd / "extract").exists() and (fd / "source").exists()
    assert (Path(service.project_overview(info.dir)["groups"][0]["dir"]) / "regions.json").exists()
    assert workspace.load_project_info(info.dir).status == "extracting"


def test_scoped_service_steps(tmp_path):
    info = _two_group_project(tmp_path)
    fds = workspace.file_dirs(workspace.load_project_info(info.dir))
    assert service.scoped_file_dirs(workspace.load_project_info(info.dir), None, [fds[1]]) == [fds[1]]
    r = service.erase_project(info.dir, files=[fds[1]])
    assert [f["file"] for f in r.files] == [workspace.file_key(fds[1])]


def test_save_source_unchanged_marks_checked_and_reading_pick_buttons(qapp, tmp_path):
    from app.qt.steps import SourceReviewDialog

    info, fd = _project_with_extract(tmp_path)
    c1 = {(c["page"], c["region"]): c for c in service.extracted_cells(fd)}[(1, "이름")]
    assert c1["label"] == "결론 못 냄"
    service.save_source(fd, 1, "이름", c1["source"])  # 고치지 않고 저장 = 확인
    c1 = {(c["page"], c["region"]): c for c in service.extracted_cells(fd)}[(1, "이름")]
    assert c1["label"] == "사람이 확인" and not c1["uncertain"]
    # 두 번 읽은 결과가 다르면 그중 하나를 골라 넣는 버튼
    info2, fd2 = _project_with_extract(tmp_path / "b")
    dlg = SourceReviewDialog(service.project_overview(info2.dir)["groups"][0]["files"], None, only_check=True)
    row = next(i for i, c in enumerate(dlg.cells) if c["page"] == 1 and c["region"] == "이름")
    dlg.table.setCurrentCell(row, 0)
    assert len(dlg.pick_buttons) == 2 and dlg.pick_label.isVisibleTo(dlg)
    dlg.pick_buttons[1].click()  # 정밀 읽기대로
    assert dlg.edit.toPlainText() == "盗掘フェレット"
    dlg.save_and_next()
    c = {(x["page"], x["region"]): x for x in service.extracted_cells(fd2)}[(1, "이름")]
    assert c["source"] == "盗掘フェレット" and c["label"] == "사람이 고침"
    dlg._orig_text = dlg.edit.toPlainText()
    dlg.close()


def test_add_files_new_group_defaults_to_file_name(qapp, tmp_path):
    from app.qt.dialogs import AddFilesDialog

    info, _ = workspace.create_project("p", "G", root=tmp_path)
    dlg = AddFilesDialog(info.dir, files=[str(tmp_path / "in" / "Monster Cards.pdf")])
    assert dlg.r_new.isChecked() and dlg.new_name.text() == "Monster Cards"
    dlg.new_name.setText("내 이름")
    dlg.files.addItem(str(tmp_path / "in" / "other.pdf"))
    assert dlg.new_name.text() == "내 이름"  # 사람이 고친 이름은 그대로


def test_region_editor_select_keeps_view_and_zoom_pan(qapp, tmp_path):
    from app.qt.region_editor import RegionEditor

    info = _project(tmp_path)
    g = service.project_overview(info.dir)["groups"][0]
    service.save_region_template(g["dir"], {"regions": [
        {"name": "이름", "rect": {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.1}},
        {"name": "본문", "rect": {"x": 0.1, "y": 0.8, "w": 0.6, "h": 0.1}}]})
    ed = RegionEditor(Path(g["dir"]), [Path(f["dir"]) for f in g["files"]])
    ed.resize(900, 600)
    ed.show()
    qapp.processEvents()
    before = (ed.view.transform().m11(), ed.view.horizontalScrollBar().value(), ed.view.verticalScrollBar().value())
    ed.list.setCurrentRow(1)
    qapp.processEvents()
    assert (ed.view.transform().m11(), ed.view.horizontalScrollBar().value(),
            ed.view.verticalScrollBar().value()) == before  # 카드가 움직이지 않는다
    s0 = ed.view.transform().m11()
    ed.view.zoom(1.25)
    assert abs(ed.view.transform().m11() / s0 - 1.25) < 1e-6 and not ed.view.auto_fit
    ed.view.fit_again()
    assert ed.view.auto_fit
    ed.dirty = False
    ed.close()


def test_project_names_unique_per_game_and_new_project_dialog_defaults(qapp, tmp_path):
    from app.qt.dialogs import NewProjectDialog

    a, _ = workspace.create_project("기본판", "게임A", root=tmp_path)
    b, _ = workspace.create_project("기본판", "게임B", root=tmp_path)  # 다른 게임이면 같은 이름도 된다
    assert a.dir != b.dir and b.name == "기본판" and b.game == "게임B"
    try:
        workspace.create_project("기본판", "게임A", root=tmp_path)
        raise AssertionError("같은 게임에 같은 이름은 안 된다")
    except FileExistsError:
        pass
    # 창: 게임이 먼저, 새 게임이면 프로젝트 이름 = 게임 이름, 기존 게임이면 비움
    dlg = NewProjectDialog(tmp_path)
    form = dlg.form  # 흰 카드 안의 입력 칸 묶음
    assert form.itemAt(0, form.ItemRole.FieldRole).widget() is dlg.game
    dlg.game.setCurrentText("새게임")
    assert dlg.name.text() == "새게임"
    dlg.game.setCurrentText("게임A")
    assert dlg.name.text() == ""
    dlg.name.setText("내 프로젝트")
    dlg.name.textEdited.emit("내 프로젝트")
    dlg.game.setCurrentText("또다른게임")
    assert dlg.name.text() == "내 프로젝트"  # 사람이 넣은 이름은 그대로
    dlg2 = NewProjectDialog(tmp_path, game="게임B")
    assert dlg2.name.text() == ""


def test_extract_summary_counts_cards_not_files(qapp, tmp_path):
    from app.qt.main import MainWindow

    info = _project(tmp_path)
    win = MainWindow(root=tmp_path)
    win.open_project(info.dir)
    page = win.project
    n = sum(f["included"] for f in page.scoped_files())
    s = page.steps.extract_summary()
    assert f"카드 {n}장" in s and "파일" in s
    win.close()


def test_login_shows_banner_image_only_at_startup(qapp):
    from app.qt.login import LOGIN_IMAGE, LoginDialog

    assert LOGIN_IMAGE.exists()
    d1 = LoginDialog(None, first_run=True)
    assert d1.banner is not None and not d1.banner.pixmap().isNull()
    assert d1.banner.pixmap().width() <= 620
    d2 = LoginDialog(None, first_run=False)  # 파일 > 로그인 방식에서 열 때는 그림 없이
    assert d2.banner is None


# ---------------------------------------------------------------- 화면 재구성 (UI 제안)
def test_hint_bar_and_primary_helpers(qapp):
    from PySide6.QtWidgets import QDialogButtonBox

    from app.qt.dialogs import PRIMARY_QSS, hint_bar, set_hint, set_ok_text

    lb = hint_bar("안내", "todo")
    assert lb.text() == "안내" and "#FFE066" in lb.styleSheet() and lb.wordWrap()
    set_hint(lb, "끝", "done")
    assert "#D9EAD3" in lb.styleSheet() and lb.property("hint_kind") == "done"
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    set_ok_text(bb, "넣기")
    assert bb.button(QDialogButtonBox.StandardButton.Ok).styleSheet() == PRIMARY_QSS
    bb2 = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
    set_ok_text(bb2, "넣기", primary=False)
    assert bb2.button(QDialogButtonBox.StandardButton.Ok).styleSheet() == ""


def test_new_project_dialog_min_width_and_hint(qapp, tmp_path):
    from app.qt.dialogs import NewProjectDialog

    dlg = NewProjectDialog(tmp_path)
    assert dlg.minimumWidth() >= 460
    assert dlg.hint.isHidden()  # 게임을 안 골랐으면 힌트 줄이 없다
    dlg.game.setCurrentText("새게임")
    assert not dlg.hint.isHidden()


def test_add_files_dialog_empty_state(qapp, tmp_path):
    from PySide6.QtWidgets import QDialogButtonBox

    from app.qt.dialogs import AddFilesDialog

    info = _project(tmp_path)
    dlg = AddFilesDialog(info.dir)
    ok = dlg.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok)
    assert dlg.files_stack.currentWidget() is dlg.empty_hint and not ok.isEnabled()
    assert "#0b5cad" in dlg.pick.styleSheet()
    dlg.files.addItem(str(tmp_path / "in" / "cards.pdf"))
    assert dlg.files_stack.currentWidget() is dlg.files and ok.isEnabled() and dlg.pick.styleSheet() == ""


def test_exclude_pages_click_toggles_and_counts(qapp, tmp_path):
    from PySide6.QtTest import QTest

    from app.qt.dialogs import ExcludePagesDialog

    info = _project(tmp_path)
    fd = Path(service.project_overview(info.dir)["groups"][0]["files"][0]["dir"])
    dlg = ExcludePagesDialog(fd, [], "")
    dlg.show()
    qapp.processEvents()
    assert dlg.count.text() == "체크한 쪽: 0쪽" and dlg.checked() == []
    rect = dlg.list.visualItemRect(dlg.list.item(1))
    QTest.mouseClick(dlg.list.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())  # 그림을 눌러도 체크
    assert dlg.checked() == [2] and dlg.count.text() == "체크한 쪽: 1쪽 (2쪽)"
    assert dlg.list.item(1).background().color().name().upper() == "#F4B183"
    QTest.mouseClick(dlg.list.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    assert dlg.checked() == []


def test_project_page_layout_next_why_and_menu(qapp, tmp_path):
    from app.qt.main import MainWindow

    info = _project(tmp_path)
    win = MainWindow(root=tmp_path)
    win.open_project(info.dir)
    page = win.project
    assert page.b_reset.text() == "초기화…" and page.b_delete.text() == "삭제…"
    assert page.scope_label.text() == "작업 대상: 전체" and "목록에서" in page.scope_hint.text()
    assert page.next_why.text()  # 영역 지정 단계
    assert page.b_folders.menu().actions()[0].text().startswith("이 그룹")
    assert page.a_folder is not None and page.a_game_folder is not None
    group = page.all_item.child(0)
    page.tree.setCurrentItem(group)
    assert page.scope_label.text().startswith("작업 대상: 그룹") and "전체" in page.scope_hint.text()
    assert [e[0] if e else None for e in page._menu_items("group")] == [
        "영역 지정", "이름 바꾸기…", "폴더 열기", None, "초기화…", "삭제…"]
    assert page.b_rename.isEnabled()  # 그룹을 고르면 이름 바꾸기 버튼이 켜진다
    assert [e[0] for e in page._menu_items("all") if e] == ["파일 넣기", "초기화…"]
    assert "영역 지정" in [e[0] for e in page._menu_items("file") if e]
    # 모두 끝난 뒤의 다음 할 일 문구
    page.ov["next_key"] = "results"
    page._sync_next_why()
    assert "검수 엑셀" in page.next_why.text()
    page.ov["next_key"] = "extract"
    page._sync_next_why()
    assert "대상:" in page.next_why.text()


def test_home_buttons_follow_selection(qapp, tmp_path):
    from app.qt.main import MainWindow

    _project(tmp_path)
    win = MainWindow(root=tmp_path)
    home = win.home
    home.tree.setCurrentItem(None)
    assert not home.b_open.isEnabled() and not home.b_delete.isEnabled()
    g = home.tree.topLevelItem(0)
    home.tree.setCurrentItem(g)
    assert not home.b_open.isEnabled()
    home.tree.setCurrentItem(g.child(0))
    assert home.b_open.isEnabled() and home.b_delete.isEnabled() and home.b_delete.text() == "프로젝트 삭제…"
    assert g.child(0).text(2)  # 다음 할 일 열


def test_region_editor_guide_form_hint_and_save_button(qapp, tmp_path):
    from PySide6.QtCore import QPointF
    from PySide6.QtTest import QTest

    from app.qt.dialogs import PRIMARY_QSS
    from app.qt.region_editor import RegionEditor

    info = _project(tmp_path)
    g = service.project_overview(info.dir)["groups"][0]
    ed = RegionEditor(Path(g["dir"]), [Path(f["dir"]) for f in g["files"]])
    ed.show()
    qapp.processEvents()
    assert "+ 글자 영역" in ed.guide.text() and ed.guide.property("hint_kind") == "todo"
    assert ed.cards_bar.isHidden() and ed.form_box.isHidden() and not ed.form_hint.isHidden()
    assert ed.reset_btn.text() == "초기화…" and ed.save_btn.styleSheet() == ""
    ed._set_add_kind("text")
    vp = ed.view.viewport()
    c0 = ed.view.mapFromScene(QPointF(100, 100))
    c1 = ed.view.mapFromScene(QPointF(300, 200))
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, pos=c0)
    QTest.mouseMove(vp, c1)
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, pos=c1)
    assert len(ed.items()) == 1
    assert "저장하지 않은 변경" in ed.guide.text() and ed.guide.property("hint_kind") == "warn"
    assert ed.save_btn.styleSheet() == PRIMARY_QSS
    assert not ed.form_box.isHidden() and ed.form_hint.isHidden()
    assert not ed.g_style.isHidden()
    ed.f_kind.setCurrentIndex(ed.f_kind.findData("protect"))
    assert ed.g_style.isHidden() and ed.g_erase.isHidden() and not ed.f_ocr_exclude.isHidden()
    ed.f_kind.setCurrentIndex(ed.f_kind.findData("text"))
    assert not ed.g_style.isHidden() and ed.f_ocr_exclude.isHidden()
    assert ed.save() is True
    assert "① 글자 추출" in ed.guide.text() and ed.save_btn.styleSheet() == ""
    ed.per_card.blockSignals(True)
    ed.per_card.setChecked(True)
    ed.per_card.blockSignals(False)
    ed._update_card_info()
    assert not ed.cards_bar.isHidden()
    ed.dirty = False


def test_source_review_hides_model_name_and_guides(qapp, tmp_path):
    from app.qt.steps import SourceReviewDialog, label_text

    assert label_text("Sonnet 1회") == "보통 읽기 1회" and label_text("결론 못 냄") == "다시 봐야 함"
    assert label_text("두 번 확인") == "두 번 확인"
    info, fd = _project_with_extract(tmp_path)
    files = service.project_overview(info.dir)["groups"][0]["files"]
    dlg = SourceReviewDialog(files)
    assert "확인할 칸 1개" in dlg.guide.text() and dlg.guide.property("hint_kind") == "todo"
    assert dlg.next_btn.styleSheet() and dlg.save_btn.text() == "고친 원문 저장"


def test_source_review_image_fits_tall_crop(qapp):
    """세로로 긴 그림도 잘리지 않게 칸 안에 비율 그대로 들어가고, 칸 크기가 바뀌면 다시 맞춘다."""
    from PySide6.QtGui import QColor, QPixmap

    from app.qt.steps import FitImage

    pm = QPixmap(200, 1600)
    pm.fill(QColor("red"))
    w = FitImage(100)
    w.resize(400, 300)
    w.set_image(pm)
    w.show()
    qapp.processEvents()

    def shown():
        p = w.pixmap()
        return p.width() / p.devicePixelRatio(), p.height() / p.devicePixelRatio()

    room = w.contentsRect()
    sw, sh = shown()
    assert sh <= room.height() and sw <= room.width() and sh >= room.height() - 2  # 높이에 맞춤
    assert abs(sw / sh - 200 / 1600) < 0.02 and w.source_size().height() == 1600  # 비율 그대로, 원본은 그대로
    w.resize(400, 700)
    qapp.processEvents()
    assert shown()[1] > sh and shown()[1] <= w.contentsRect().height()
    w.close()


def test_reading_diff_html_highlights_only_differences():
    from app.qt.steps import DIFF_STYLE, diff_html

    a, b = diff_html(["盗賊フェレット", "盗掘フェレット"], 1)
    assert f'<span style="{DIFF_STYLE}">賊</span>' in a and f'<span style="{DIFF_STYLE}">掘</span>' in b
    assert "フェレット" in a and "<span" not in a.split("</span>")[1]  # 같은 곳은 그대로
    x, y = diff_html(["a<b>\n4", "a<b>\n４"], 1)  # 글은 이스케이프, 줄바꿈은 <br>
    assert x.startswith("a&lt;b&gt;<br>") and "４</span>" in y and "<b>" not in x
    same = diff_html(["竜", "竜"], 1)
    assert same == ["竜", "竜"]
    # 아이콘 토큰은 한 덩어리로 비교한다
    p, q = diff_html(["[[sword]]が", "[[book]]が"], 1)
    assert f'<span style="{DIFF_STYLE}">[[book]]</span>が' == q


def test_fill_icon_names_uses_confirmed_names():
    from app.qt.steps import fill_icon_names

    src = "A-007\nプレイ時効果\n[[sword]] [[sword]]\n文明[[book]]が４以下なら[[number_1]]を得る"
    sonnet = "プレイ時効果\n文明[[?]]が4以下なら[[?]]を得る"
    opus = "A-007\nプレイ時効果\n[[?]] [[?]]\n文明[[?]]が４以下なら[[?]]を得る"
    assert fill_icon_names(opus, src) == src
    assert fill_icon_names(sonnet, src) == "プレイ時効果\n文明[[book]]が4以下なら[[number_1]]を得る"
    # 사전에서 정한 이름만 (정하지 않은 것은 [[?]] 그대로)
    assert fill_icon_names(sonnet, src, {"book"}) == "プレイ時効果\n文明[[book]]が4以下なら[[?]]を得る"
    assert fill_icon_names("文[[?]]", "文[[?]]") == "文[[?]]"


def _project_with_icon_cell(tmp_path):
    """3쪽 이름 칸: 두 번 읽은 결과가 다르고, 원문에는 아이콘 이름을 정해 넣은 상태."""
    from app import icons

    info, fd = _project_with_extract(tmp_path)
    icons.save_dictionary(workspace.load_project_info(info.dir),
                          {"game": "G", "icons": {"sword": {"file": "sword.png"}, "book": {"file": "book.png"}}})
    (fd / "extract" / "pages" / "page-003.json").write_text(json.dumps({"page": 3, "model": "claude-sonnet-5-5", "regions": {
        "이름": {"source": "[[sword]] 文明[[book]]が４以下", "uncertain": True,
                "note": "두 모델이 다르게 읽음 (claude-sonnet-5-5: x / claude-opus-5-5: y). 二文字目が薄い",
                "readings": [{"model": "claude-sonnet-5-5", "source": "文明[[?]]が4以下"},
                             {"model": "claude-opus-5-5", "source": "[[?]] 文明[[?]]が４以下"}]},
        "태그": {"source": ["人族"], "uncertain": False, "note": ""}}}, ensure_ascii=False), encoding="utf-8")
    return info, fd


def test_source_review_shows_icon_names_and_reason_grid(qapp, tmp_path):
    from app.qt.steps import DIFF_STYLE, SourceReviewDialog

    info, fd = _project_with_icon_cell(tmp_path)
    assert service.known_icon_names(fd) == ["book", "sword"]
    dlg = SourceReviewDialog(service.project_overview(info.dir)["groups"][0]["files"])
    row = next(i for i, c in enumerate(dlg.cells) if (c["page"], c["region"]) == (3, "이름"))
    dlg.table.setCurrentCell(row, 0)
    rows = [(h.text(), v.text()) for h, v in dlg.reading_rows]
    assert [h for h, _v in rows] == ["보통 읽기", "정밀 읽기"]
    assert "[[?]]" not in str(rows) and "[[book]]" in rows[0][1] and "[[sword]]" in rows[1][1]
    assert DIFF_STYLE in rows[0][1] and DIFF_STYLE in rows[1][1]  # 4 / ４ 가 다르다
    # 위쪽 사유: 무엇 때문에 확인하는지, 모델 메모는 쉬운 말로 (모델 이름 없이)
    info_text = dlg.info.text()
    assert "사유: 두 번 읽은 결과가 다릅니다" in info_text and "二文字目が薄い" in info_text
    assert "claude-" not in info_text and dlg.info.property("hint_kind") == "todo"
    # 편집 칸 = 정밀 읽기 (이름을 정한 아이콘은 그 이름으로), 고르는 버튼에도 이름
    assert dlg.edit.toPlainText() == "[[sword]] 文明[[book]]が４以下"
    assert "[[?]]" not in " ".join(b.toolTip() for b in dlg.pick_buttons)
    dlg.close()


def test_source_review_default_follows_precise_reading(qapp, tmp_path):
    from app.qt.steps import SourceReviewDialog

    info, fd = _project_with_extract(tmp_path)  # 1쪽 이름: 원문은 보통 읽기(盗賊), 정밀 읽기는 盗掘
    dlg = SourceReviewDialog(service.project_overview(info.dir)["groups"][0]["files"])
    assert dlg.current()["page"] == 1 and dlg.edit.toPlainText() == "盗掘フェレット"
    assert "정밀 읽기 결과를 넣어 두었습니다" in dlg.info.text()
    dlg.save_current()  # 그대로 저장하면 정밀 읽기 결과가 원문이 된다 (저장 방식은 그대로)
    c = next(x for x in service.extracted_cells(fd) if (x["page"], x["region"]) == (1, "이름"))
    assert c["source"] == "盗掘フェレット" and c["label"] == "사람이 고침" and not c["uncertain"]
    # 다시 봐야 하는 칸이 아니면 원문 그대로
    dlg.only.setChecked(False)
    row = next(i for i, x in enumerate(dlg.cells) if (x["page"], x["region"]) == (2, "이름"))
    dlg.table.setCurrentCell(row, 0)
    assert dlg.edit.toPlainText() == "竜" and len(dlg.reading_rows) == 2 and not dlg.pick_buttons
    dlg.close()


def test_result_viewer_colors_and_footer(qapp, tmp_path):
    from app.qt.results import ResultViewer

    info, fd = _translated_project(tmp_path)
    service.erase_project(info.dir)
    service.compose_project(info.dir)
    v = ResultViewer([("카드/cards", fd / "translated")])
    assert v.info.property("hint_kind") == "done" and "번역·글꼴·크기·정렬·지우기 방식을 고치고 저장" in v.footer.text()
    assert v.only.text() == "문제 있는 쪽만 보기"
    assert v.regions.count() >= 1
    r = service.test_run(fd, "2", out_root=tmp_path / "runs")
    v2 = ResultViewer([("시험", r.run_dir)])
    assert "시험 결과입니다" in v2.footer.text() and "바꿀 수 없습니다" in v2.erase_hint.text()


def _g(name, files):
    return {"name": name, "dir": f"D/{name}", "files": files}


def _f(**k):
    return {"included": 3, "erased": False, "translated": False, "composed": False, "dir": "x", "key": "k", **k}


def test_group_pick_dialog_only_ready_groups_and_card_count(qapp):
    """한글 넣기·시험 실행의 그룹 고르기: 할 수 있는 그룹만 고를 수 있고, 시험 실행은 몇 장인지도 고른다."""
    from app.qt.results import GroupPickDialog, OutputActions

    ready = _g("카드", [_f(erased=True, translated=True), _f(erased=True, translated=True, composed=True)])
    not_erased = _g("아이템", [_f(translated=True)])
    dlg = GroupPickDialog("⑧ 한글 넣기", [not_erased, ready], OutputActions._can_compose)
    assert not dlg.radios.button(0).isEnabled() and "⑦ 글자 지우기" in dlg.radios.button(0).text()
    assert dlg.radios.button(1).isChecked() and dlg.chosen() is ready and "한글 1/2" in dlg.radios.button(1).text()
    untranslated = _g("던전", [_f()])
    t = GroupPickDialog("시험 실행", [untranslated, ready, not_erased], OutputActions._can_test, cards=True,
                        current=not_erased)
    assert not t.radios.button(0).isEnabled()  # 번역 전 그룹은 시험 못 함
    assert t.chosen() is not_erased and t.n.maximum() == 3  # 목록에서 고른 그룹이 먼저 (시험은 스스로 지운다)
    t.radios.button(1).setChecked(True)
    assert t.chosen() is ready and t.n.maximum() == 6  # 3장 x 2파일
    assert OutputActions._can_test(_g("빈", []))[0] is False


def test_compose_picks_group_when_all_selected(qapp, tmp_path, monkeypatch):
    """목록에서 ‘전체’를 고른 채 ⑧: 그룹이 하나면 묻지 않고 그 그룹으로, 둘이면 그룹 고르기 창."""
    from app.qt import results as res_mod

    class Page:
        def __init__(self, groups):
            self.ov = {"groups": groups}
            self.picked = None

        def _selected(self):
            return ("all", None)

        def select_group(self, g):
            self.picked = g

    one = _g("카드", [_f(erased=True, translated=True)])
    page = Page([one])
    acts = res_mod.OutputActions(page)
    assert acts._pick_group("⑧ 한글 넣기", acts._can_compose) and page.picked is one
    other = _g("아이템", [_f(translated=True)])
    page2 = Page([other, one])
    shown = {}

    class Dlg:
        def __init__(self, title, groups, check, parent=None, **k):
            shown["n"] = len(groups)

        def exec(self):
            return True

        def chosen(self):
            return one
    monkeypatch.setattr(res_mod, "GroupPickDialog", Dlg)
    acts2 = res_mod.OutputActions(page2)
    assert acts2._pick_group("⑧ 한글 넣기", acts2._can_compose) and shown["n"] == 2 and page2.picked is one


# ---------------------------------------------------------------- 앱 전체 모양 (theme)
def test_theme_applies_whole_app_style_and_restores(qapp, tmp_path):
    """테마: Fusion + 스타일시트(둥근 칸·주/보조/위험 버튼)를 앱에 입힌다. 화살표·체크 그림 파일도 만든다."""
    from pathlib import Path as _P

    from app.qt import theme
    from app.qt.main import MainWindow

    old_sheet, old_pal = qapp.styleSheet(), qapp.palette()
    try:
        theme.apply(qapp)
        sheet = qapp.styleSheet()
        assert "QPushButton" in sheet and "border-radius" in sheet and 'danger="true"' in sheet
        for name in ("down", "up", "check", "dot"):
            assert f"{name}.svg" in sheet
        for line in sheet.splitlines():
            if "url(" in line:
                assert _P(line.split("url(", 1)[1].split(")", 1)[0]).exists()
        theme.apply(qapp)  # 두 번 불러도 괜찮다
        info = _project(tmp_path)
        win = MainWindow(root=tmp_path)
        assert win.home.b_delete.property("danger") is True
        win.open_project(info.dir)
        page = win.project
        assert page.b_delete.property("danger") is True and page.b_reset.property("danger") is True
        assert page.next_btn.text().startswith("▶ ")
        win.close()
    finally:
        qapp.setStyleSheet(old_sheet)
        qapp.setPalette(old_pal)


def test_file_label_hides_repeated_group_name():
    from app.qt.dialogs import file_label

    assert file_label("카드/카드") == "카드"
    assert file_label("아이템/items_b") == "아이템 › items_b"
    assert file_label("하나") == "하나"


def test_add_files_list_shows_names_but_keeps_paths(qapp, tmp_path):
    """파일 넣기 목록은 파일 이름만 보이고(경로는 말풍선), 넣을 때는 전체 경로를 쓴다."""
    from app.qt.dialogs import AddFilesDialog

    info = _project(tmp_path)
    pdf = str(tmp_path / "in" / "cards.pdf")
    dlg = AddFilesDialog(info.dir, files=[pdf, pdf])
    assert dlg.files.count() == 1  # 같은 파일은 한 번만
    it = dlg.files.item(0)
    assert it.text() == "cards.pdf" and it.toolTip() == pdf and dlg.all_files() == [pdf]
    dlg.files.addItem(str(tmp_path / "in" / "other.pdf"))  # 경로를 글자로만 넣은 줄도 그대로 읽는다
    assert dlg.all_files()[1].endswith("other.pdf")


def test_settings_hides_log_path_and_source_review_shows_short_file_name(qapp, tmp_path):
    from app.qt.dialogs import SettingsDialog
    from app.qt.steps import SourceReviewDialog

    s = SettingsDialog()
    assert s.log_path.isHidden() and str(service.log_folder()) in s.b_log.toolTip()
    info, _fd = _project_with_extract(tmp_path)
    files = [f for g in service.project_overview(info.dir)["groups"] for f in g["files"]]
    dlg = SourceReviewDialog(files, None, False)
    assert "/" not in dlg.file.itemText(0) and not dlg.table.verticalHeader().isVisible()
    dlg.done(0)


def test_erase_labels_cover_every_service_key_and_keep_keys():
    """쉬운 말 문구는 service.ERASE_MODES의 모든 키를 덮고, 키 자체는 그대로 둔다(저장값이 문구로 바뀌지 않게)."""
    from app.qt.dialogs import ERASE_LABELS, erase_modes

    keys = [k for k, _ in service.ERASE_MODES]
    assert [k for k, _ in erase_modes()] == keys
    assert set(keys) <= set(ERASE_LABELS)
    assert all(k not in v for k, v in erase_modes() if k != "auto")


def test_theme_survives_unwritable_temp_dir(qapp, tmp_path, monkeypatch):
    """임시 폴더에 쓸 수 없어도(읽기 전용·없는 폴더) 테마는 화살표 그림 없이 그대로 적용되고 죽지 않는다."""
    from app.qt import theme

    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")  # 폴더가 와야 할 자리에 파일 -> mkdir 실패
    monkeypatch.setattr(theme.tempfile, "gettempdir", lambda: str(blocker))
    assert theme._arrow_files() == {}
    sheet = theme.stylesheet()
    assert "QPushButton" in sheet and "url(" not in sheet
    old_sheet, old_pal = qapp.styleSheet(), qapp.palette()
    try:
        theme.apply(qapp)
    finally:
        qapp.setStyleSheet(old_sheet)
        qapp.setPalette(old_pal)


def test_translate_can_chain_straight_into_erase(qapp, tmp_path, monkeypatch):
    """⑥ 번역 질문 창의 ‘끝나면 바로 ⑦ 글자 지우기’: 엑셀 없이 바로 지우고, 결과를 한 창에 보인다. 엑셀은 자동으로 안 만든다."""
    from types import SimpleNamespace

    from app.qt import results as res_mod
    from app.qt.main import MainWindow

    info = _two_group_project(tmp_path)
    win = MainWindow(root=tmp_path)
    win.open_project(info.dir)
    page = win.project
    page.step_buttons["erase"].setEnabled(False)
    assert page.steps._erase_option() is None  # 지울 수 없는 상태면 묻지 않음
    page.step_buttons["erase"].setEnabled(True)
    cb = page.steps._erase_option()
    assert cb is not None and cb.isChecked() and "⑦" in cb.text()

    seen = {}

    def fake_run(parent, title, job, done, fail, **k):
        seen.update(title=title, redo=job.kwargs.get("redo"), done=done)
    monkeypatch.setattr(res_mod.worker, "run_with_dialog", fake_run)
    monkeypatch.setattr("app.qt.steps.worker.run_with_dialog", fake_run)
    monkeypatch.setattr(service, "files_needing_erase", lambda _p, files=None: ["F"])  # 지울 파일이 있음
    exported = []
    monkeypatch.setattr(service, "export_project_excel", lambda *a, **k: exported.append(1) or [])
    opened = []
    monkeypatch.setattr("app.qt.main.open_path", lambda p: opened.append(p))
    done = SimpleNamespace(state="done", reason="", cells=3, translated=3, from_glossary=0, cached=0,
                           term_mismatch=[], icon_mismatch=[], leftover=[])
    page.steps._then_erase = True
    page.steps._translate_done(done)  # 엑셀 없이 바로 지우기
    assert seen["title"] == "글자 지우기" and not seen["redo"] and not page.steps._then_erase
    assert not exported and not opened  # 검수 엑셀을 만들지도 열지도 않는다
    # 지우기 끝: 번역 요약과 ‘번역 검수 열기’ 버튼
    boxes = []

    class Box:
        class ButtonRole:
            AcceptRole, RejectRole = 0, 1

        def __init__(self, parent=None):
            boxes.append(self)
            self.buttons = []
            self.objs = {}

        def setWindowTitle(self, t): pass
        def setText(self, t): self.text = t
        def addButton(self, t, role):
            self.buttons.append(t)
            self.objs[t] = object()
            return self.objs[t]
        def exec(self): pass
        def clickedButton(self): return self.objs["번역 검수 열기"]
    monkeypatch.setattr(res_mod, "QMessageBox", Box)
    called = []
    monkeypatch.setattr(page.steps, "review_translation", lambda: called.append(1))
    seen["done"](SimpleNamespace(state="done", reason="", files=[{"file": "a", "state": "done", "pages": 2, "warnings": 0}]))
    assert "서로 다른 원문 3칸" in boxes[0].text
    assert "a:" in boxes[0].text and boxes[0].buttons == ["번역 검수 열기", "닫기"] and called == [1]
    # 번역이 끝나면 엑셀 없이 안내 창만 (‘번역 검수 열기’)
    monkeypatch.setattr("app.qt.steps.QMessageBox", Box)
    boxes.clear()
    page.steps._translate_done(done)
    assert not exported and "번역 검수" in boxes[0].text and "엑셀은 다른 사람에게" in boxes[0].text
    assert boxes[0].buttons == ["번역 검수 열기", "닫기"] and called == [1, 1]
    # 번역이 끝나지 못하면 이어서 지우지 않는다
    page.steps._then_erase = True
    monkeypatch.setattr("app.qt.steps.show_problem", lambda *a, **k: None)
    page.steps._translate_done(SimpleNamespace(state="limited", reason="한도", retry_after=None))
    assert not page.steps._then_erase


def test_region_editor_skips_excluded_pages(qapp, tmp_path):
    """뺄 쪽으로 정한 쪽은 영역 지정에서 보이지 않는다 (▶·◀·처음·끝·쪽 번호 입력 모두 건너뜀)."""
    from app.qt.region_editor import RegionEditor

    info = _project(tmp_path)  # 3쪽
    g = service.project_overview(info.dir)["groups"][0]
    fd = Path(g["files"][0]["dir"])
    service.set_pages(fd, [2])
    service.save_region_template(g["dir"], {"regions": [{"name": "이름", "rect": {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.1}}]})
    ed = RegionEditor(Path(g["dir"]), [fd])
    assert ed.page_spin.value() == 1 and "뺀 쪽 1개" in ed.page_total.text()
    ed.step_page(1)
    assert ed.page_spin.value() == 3 and not ed.next_btn.isEnabled()
    ed.step_page(-1)
    assert ed.page_spin.value() == 1 and not ed.prev_btn.isEnabled()
    ed.page_spin.setValue(2)  # 직접 넣어도 건너뛴다
    assert ed.page_spin.value() == 3
    ed.jump_page(0)
    assert ed.page_spin.value() == 1
    assert ed.all_card_keys() == [f"{fd.name}/1", f"{fd.name}/3"]
    # 처음 열 때 기준 쪽이 뺀 쪽이면 다음 보일 쪽에서 연다
    service.set_pages(fd, [1])
    ed2 = RegionEditor(Path(g["dir"]), [fd])
    assert ed2.page_spin.value() == 2
    ed.dirty = ed2.dirty = False


def test_icon_place_dialog_saves_drawn_box_and_keeps_typed_names(qapp, tmp_path, monkeypatch):
    from app import icons
    from app.qt.steps import IconPlaceDialog, IconReviewDialog
    from tests.test_icons import _project as icon_project

    info, _fd = icon_project(tmp_path, "1차")
    real = icons.locate
    calls = {"n": 0}

    def flaky(img, text, index):
        calls["n"] += 1
        return (None, "줄 수가 다름") if calls["n"] == 2 else real(img, text, index)

    monkeypatch.setattr(icons, "locate", flaky)
    service.find_icons(info.dir, suggest=False)
    monkeypatch.setattr(icons, "locate", real)
    dlg = IconReviewDialog(info.dir, service.icon_review(info.dir))
    assert len(dlg.widgets) == 2
    views = [dlg.table.cellWidget(i, 8).text() for i in range(2)]
    assert views == ["카드 보기 · 자리 잡기"] * 2  # 찾은 줄도 자리를 다시 잡을 수 있다
    found_i = next(i for i, g in enumerate(dlg.review["groups"]) if g["image"])
    lost = next(g for g in dlg.review["groups"] if not g["image"])
    dlg.widgets[found_i][1].setText("coin")
    dlg.widgets[found_i][3].setText("금화")
    good = service.icon_place(info.dir, dlg.review["groups"][found_i]["where"][0]["occ"])["box"]
    place = IconPlaceDialog(info.dir, lost, dlg._row_label, dlg._after_place, dlg)
    assert place.view.guess is not None and place.view.box is None and not place.save_btn.isEnabled()
    place.view.new = list(good)
    place._drawn()
    assert place.save_btn.isEnabled()
    place._save()
    assert "합쳤습니다" in place.status.text() and place.view.box == list(good)
    assert len(dlg.widgets) == 1 and dlg.widgets[0][1].text() == "coin" and dlg.widgets[0][3].text() == "금화"
    # 다시 열면 직접 잡은 자리부터 보여 준다
    again = IconPlaceDialog(info.dir, dlg.review["groups"][0], dlg._row_label, dlg._after_place, dlg)
    assert again.where[again.combo.currentIndex()]["manual"] and again.view.box == list(good)
    dlg.accept()
    assert dlg.result.replaced == 2


def test_release_notes_menu_shows_changelog(qapp, tmp_path):
    from app import version
    from app.qt.main import MainWindow, ReleaseNotesDialog

    text = service.release_notes()
    assert text.startswith(f"## {version.VERSION}")  # 지금 버전의 노트가 맨 위
    win = MainWindow(root=tmp_path)
    help_menu = next(a.menu() for a in win.menuBar().actions() if a.text() == "도움말")
    assert "릴리스 노트…" in [a.text() for a in help_menu.actions()]
    dlg = ReleaseNotesDialog(win)
    assert version.VERSION in dlg.windowTitle() and version.VERSION in dlg.view.toPlainText()
    assert "app/version.py" not in dlg.view.toPlainText()  # 개발용 안내는 빼고 보여 준다
    assert service.self_check()["release_notes"] is True



def test_result_viewer_edits_translation_and_font_then_recomposes(qapp, tmp_path, monkeypatch):
    """결과 보기에서 한 카드의 번역·글꼴을 고쳐 저장하면 그 파일에 한글만 다시 넣는다 (검수 엑셀은 그대로)."""
    import pymupdf

    from app import card_edits
    from app.qt import results as qres
    from app.qt.results import ResultViewer

    info, fd = _translated_project(tmp_path)
    service.erase_project(info.dir)
    service.compose_project(info.dir)
    v = ResultViewer([("카드/cards", fd / "translated")])
    names = [r["name"] for r in v.current()["regions"]]
    v.regions.setCurrentRow(names.index("이름") + 1)
    assert v.edit_form.isVisible() or v.edit_text.toPlainText() == "안녕"
    assert v.edit_text.toPlainText() == "안녕" and not v.redo_btn.isEnabled()
    v.edit_text.setPlainText("반가워")
    font = next(f["file"] for f in service.font_choices() if "gaegu" in f["file"].lower())
    v.edit_font.setCurrentIndex(v.edit_font.findData(font))
    assert v.redo_btn.isEnabled() and "저장하지 않은" in v.edit_note.text()
    png = service.card_preview(fd, 1, "이름", "반가워", font)
    assert png and png[1:4] == b'PNG'
    ran = {}

    def run_now(_parent, _title, job, on_done, _on_fail=None, cancellable=True):  # 작업 스레드 대신 바로
        ran["fn"] = job.fn
        on_done(job.fn(*job.args, **job.kwargs))

    monkeypatch.setattr(qres.worker, "run_with_dialog", run_now)
    v.save_edit()
    assert ran["fn"] is service.recompose_file  # 지우기 방식은 그대로라 다시 지우지 않는다
    assert card_edits.load(fd) == {1: {"이름": {"ko": "반가워", "base": "안녕"}}}
    with pymupdf.open(fd / "translated" / "translated.pdf") as d:
        assert "반가워" in d[0].get_text() and "안녕" in d[1].get_text()  # 2쪽은 그대로
    rep = json.loads((fd / "translated" / "report.json").read_text(encoding="utf-8"))
    assert rep["pages_detail"][0]["regions"]["이름"]["font_file"] == font
    again = service.card_edit_info(fd, 1, "이름")
    assert again["ko"] == "반가워" and again["edited"] and again["base"] == "안녕" and again["font"] == font
    # 원래대로 저장하면 고친 것을 지운다
    service.save_card_edit(fd, 1, "이름", text="안녕", font=None, set_font=True)
    assert card_edits.load(fd) == {} and service.card_edit_info(fd, 1, "이름")["font"] == ""


def test_card_edit_yields_to_newer_excel_edit(tmp_path):
    """결과 보기에서 고친 뒤 검수 엑셀에서 같은 칸을 고치면 엑셀이 우선 (몰래 덮지 않는다)."""
    from app import card_edits

    cards = {1: {"page": 1, "이름": {"source": "HELLO", "ko": "안녕"}}}
    fd = tmp_path / "f"
    fd.mkdir()
    card_edits.set_text(fd, 1, "이름", "반가워", "안녕")
    out, notes = card_edits.apply(fd, cards)
    assert out[1]["이름"]["ko"] == "반가워" and out[1]["이름"]["source"] == "HELLO" and not notes
    assert cards[1]["이름"]["ko"] == "안녕"  # 원래 dict는 그대로
    newer = {1: {"page": 1, "이름": {"source": "HELLO", "ko": "어서 와"}}}
    out, notes = card_edits.apply(fd, newer)
    assert out[1]["이름"]["ko"] == "어서 와" and notes
    # 번역이 아예 없던 칸에도 넣는다 (원문은 찾아서)
    card_edits.set_text(fd, 2, "이름", "새 번역", None)
    out, _ = card_edits.apply(fd, {}, lambda p, r: "SRC")
    assert out[2]["이름"] == {"source": "SRC", "ko": "새 번역"}


def test_rename_group_keeps_review_excel_working(qapp, tmp_path):
    """그룹 이름을 바꿔도 폴더·결과가 따라가고, 예전 이름으로 만든 게임 검수 엑셀을 계속 찾는다."""
    from app.excel_io import import_translation
    from app.project import load_project

    info, fd = _translated_project(tmp_path)
    old = fd.parent.parent.name
    service.export_excel(fd)
    assert service.review_xlsx_for(fd) is not None
    with pytest.raises(ValueError):
        service.rename_group(info.dir, old, "  ")
    gdir = service.rename_group(info.dir, old, "새 그룹")
    assert gdir.name == "새 그룹" and not (fd.parent.parent).exists()
    assert json.loads((gdir / "group.json").read_text(encoding="utf-8"))["old_names"] == [old]
    names = [g["name"] for g in service.project_overview(info.dir)["groups"]]
    assert "새 그룹" in names and old not in names
    nfd = gdir / "files" / fd.name
    x = service.review_xlsx_for(nfd)
    assert x is not None and not import_translation(load_project(nfd), x).blocking  # 예전 이름으로 적힌 엑셀
    workspace.add_files(info, [next((nfd / "source").glob("*.pdf"))], "다른 그룹", new_group=True)
    with pytest.raises(ValueError):
        service.rename_group(info.dir, "새 그룹", "다른 그룹")  # 이미 있는 이름


def test_result_viewer_card_size_align_and_recompose(qapp, tmp_path, monkeypatch):
    """결과 보기에서 이 카드만 글자 크기·정렬을 바꿔 저장하면 그 카드만 그 크기·정렬로 다시 넣는다."""
    from app import font_plan
    from app.qt import results as qres
    from app.qt.results import ResultViewer

    info, fd = _translated_project(tmp_path)
    service.erase_project(info.dir)
    service.compose_project(info.dir)
    before = json.loads((fd / "translated" / "report.json").read_text(encoding="utf-8"))
    s1 = before["pages_detail"][0]["regions"]["이름"]["start_size_pt"]
    v = ResultViewer([("카드/cards", fd / "translated")])
    names = [r["name"] for r in v.current()["regions"]]
    v.regions.setCurrentRow(names.index("이름") + 1)
    assert v.edit_size.value() == v.edit_size.minimum() and "영역 설정대로" in v.edit_size.specialValueText()
    assert not v.para_box.isVisible() or not v._para_combos  # 문단마다 영역이 아니면 문단 글꼴 없음
    v.edit_size.setValue(150)
    v.edit_align.setCurrentIndex(v.edit_align.findData("right"))
    assert v.redo_btn.isEnabled()
    monkeypatch.setattr(qres.worker, "run_with_dialog",
                        lambda _p, _t, job, on_done, _f=None, cancellable=True: on_done(job.fn(*job.args, **job.kwargs)))
    v.save_edit()
    data = font_plan.load(fd)
    assert data["card_sizes"] == {"1": {"이름": 1.5}} and data["card_aligns"] == {"1": {"이름": "right"}}
    after = json.loads((fd / "translated" / "report.json").read_text(encoding="utf-8"))
    r1, r2 = after["pages_detail"][0]["regions"]["이름"], after["pages_detail"][1]["regions"]["이름"]
    assert r1["start_size_pt"] > s1 * 1.3 and abs(r2["start_size_pt"] - s1) < 0.01  # 2쪽은 그대로
    info1 = service.card_edit_info(fd, 1, "이름")
    assert info1["size"] == 1.5 and info1["align"] == "right"
    service.save_card_edit(fd, 1, "이름", size=None, set_size=True, align="", set_align=True)  # 영역 설정대로
    assert "card_sizes" not in font_plan.load(fd) and "card_aligns" not in font_plan.load(fd)


def test_result_viewer_paragraph_fonts_for_paragraph_region(qapp, tmp_path, monkeypatch):
    from app import font_plan
    from tests.test_fonts_sentences import GAEGU, _project_with_mode

    info, fd = _project_with_mode(tmp_path, "sentence")
    cards = {1: {"이름": {"ko": "첫 문단.\n둘째 문단."}}}
    assert service._compose_with_cards(fd, cards, None, None).state == "done"
    monkeypatch.setattr(service, "_translation_cards", lambda _fd: cards)
    i0 = service.card_edit_info(fd, 1, "이름")
    assert i0["paragraph_mode"] and i0["paragraph_fonts"] == {}
    assert service.split_paragraphs("첫 문단.\n둘째 문단.") == ["첫 문단.", "둘째 문단."]
    service.save_card_edit(fd, 1, "이름", paragraph_fonts={1: GAEGU})
    assert font_plan.load(fd)["sentences"] == {"1": {"이름": {"1": GAEGU}}}
    assert service.card_edit_info(fd, 1, "이름")["paragraph_fonts"] == {1: GAEGU}
    png = service.card_preview(fd, 1, "이름", "첫 문단.\n둘째 문단.", None, paragraph_fonts={1: GAEGU})
    assert png and png[1:4] == b"PNG"


def test_rename_group_keeps_reviewer_edits_links_and_case_only(qapp, tmp_path):
    """이름을 바꾼 그룹: 검수 엑셀을 다시 만들어도 사람 고침이 남고, 다른 그룹의 쪽 연결도 새 이름, 대소문자만 바꿔도 된다."""
    from openpyxl import load_workbook

    from app.excel_io import game_files

    info, fd = _translated_project(tmp_path)
    old = fd.parent.parent.name
    x = service.export_project_excel(info.dir)[0]  # 게임 검수 엑셀
    files = game_files(x)
    wb = load_workbook(x)
    ws = wb[files[0]["시트"]]
    head = [c.value for c in ws[1]]
    ws.cell(2, head.index("번역") + 1, "사람이 고침")
    for s_ in wb.worksheets:
        s_._images = []
    wb.save(x)
    # 다른 그룹 파일이 이 그룹을 이름으로 가리키는 연결
    other = workspace.add_files(info, [next((fd / "source").glob("*.pdf"))], "B", new_group=True)
    ofd = next(d for d in workspace.file_dirs(info) if d.parent.parent.name == "B")
    data = workspace.file_settings(ofd)
    data["pages_in_groups"] = {old: [1]}
    workspace._write(ofd / "file.json", data)
    gdir = service.rename_group(info.dir, old, "Monster")
    assert workspace.file_settings(ofd)["pages_in_groups"] == {"Monster": [1]}
    nfd = gdir / "files" / fd.name
    x2 = service.export_project_excel(info.dir, force=True)[0]
    wb2 = load_workbook(x2, data_only=True)
    f2 = next(f for f in game_files(x2) if f["그룹"] == "Monster")
    vals = [r[head.index("번역")] for r in wb2[f2["시트"]].iter_rows(min_row=2, values_only=True)]
    assert "사람이 고침" in vals  # 예전 이름 시트의 고침을 이어받음
    g3 = service.rename_group(info.dir, "Monster", "monster")  # 대소문자만
    assert g3.name == "monster" and (g3 / "group.json").exists()
    assert [p.name for p in g3.parent.iterdir() if p.name.lower() == "monster"] == ["monster"]


def test_result_viewer_keeps_each_cells_paragraph_fonts_and_preview_uses_region(qapp, tmp_path, monkeypatch):
    from app import font_plan
    from app.qt.results import ResultViewer
    from tests.test_fonts_sentences import GAEGU, SANS, _project_with_mode

    info, fd = _project_with_mode(tmp_path, "sentence")
    cards = {1: {"이름": {"ko": "가.\nB."}}, 2: {"이름": {"ko": "하나.\n둘.\n셋."}}}
    assert service._compose_with_cards(fd, cards, None, None).state == "done"
    monkeypatch.setattr(service, "_translation_cards", lambda _fd: cards)
    service.save_card_edit(fd, 2, "이름", paragraph_fonts={0: GAEGU})
    v = ResultViewer([("이미지", fd / "translated")])
    v.pages.setCurrentRow(0)
    v.regions.setCurrentRow(1)
    assert len(v._para_combos) == 2 and not v.redo_btn.isEnabled()
    v.pages.setCurrentRow(1)  # 다른 카드: 앞 카드의 문단 칸이 이 카드의 저장된 문단 글꼴을 덮지 않는다
    v.regions.setCurrentRow(1)
    assert v._para_combos[0].currentData() == GAEGU and not v.redo_btn.isEnabled()
    v.edit_size.setValue(v.edit_size.minimum())
    v.edit_size.stepUp()
    assert v.edit_size.value() == 50  # 영역 설정대로 다음은 50 %
    # 미리 보기: font·size None = 영역 글꼴·크기 (저장된 이 카드 값이 아님)
    font_plan.save(fd, {"이름": SANS})
    service.save_card_edit(fd, 2, "이름", font=GAEGU, set_font=True, size=1.6, set_size=True)
    calls = []
    monkeypatch.setattr(font_plan, "preview_render", lambda *a, **k: calls.append((a, k)) or b"x")
    service.card_preview(fd, 2, "이름", "하나.", None, None)
    a, _k = calls[-1]
    assert a[3] == SANS and a[4] == 1.0


def test_compose_enabled_when_one_of_two_groups_translated_and_erased(qapp, tmp_path):
    """그룹 2개 중 1개만 번역·지우기를 끝내도 ⑧ 한글 넣기가 켜진다 (프로젝트는 아직 ‘번역 중’)."""
    from app import glossary
    from app.extract_cc import ClaudeCodeClient
    from app.qt.main import MainWindow
    from tests.test_extract import FakeRunner
    from tests.test_project_steps import _project as steps_project
    from tests.test_service import GAEGU
    from tests.test_translate import _write_extract

    if not GAEGU.exists():
        pytest.skip("fonts/ 없음")
    info, fd = steps_project(tmp_path)
    _write_extract(fd, 1, "HELLO")
    _write_extract(fd, 2, "HELLO")
    workspace.add_files(info, [next((fd / "source").glob("*.pdf"))], "둘째", new_group=True,
                        regions_from=fd.parent.parent / "regions.json")
    fd2 = next(d for d in workspace.file_dirs(info) if d.parent.parent.name == "둘째")
    _write_extract(fd2, 1, "WORLD")
    glossary.confirm(info, [{"source": "HELLO", "ko": "안녕", "category": "기타"}])
    workspace.set_status(info, "glossary_confirmed")
    r = service.translate_project(info.dir, client=ClaudeCodeClient(exe="claude", runner=FakeRunner([])), files=[fd])
    assert r.state == "done" and workspace.load_project_info(info.dir).status == "translating"
    assert service.erase_project(info.dir, files=[fd]).state == "done"
    ov = service.project_overview(info.dir)
    by = {f["dir"].parent.parent.name: f for g in ov["groups"] for f in g["files"]}
    assert by[fd.parent.parent.name]["translated"] and not by["둘째"]["translated"]
    win = MainWindow(root=tmp_path)
    win.open_project(info.dir)
    page = win.project
    assert page.step_buttons["compose"].isEnabled() and page.step_buttons["test"].isEnabled()


def test_scope_review_fixes(qapp, tmp_path, monkeypatch):
    from app.qt import results as res_mod

    # 같은 초의 시험 폴더는 겹치지 않는다
    d1, _ = service._output_dirs(tmp_path, "translated", "compose", True, tmp_path / "t")
    d1.mkdir(parents=True)
    d2, _ = service._output_dirs(tmp_path, "translated", "compose", True, tmp_path / "t")
    assert d2 != d1 and d2.name.endswith("-compose")
    # 목록에서 고른 그룹이 아직 한글 넣기를 못 하면 알리고 멈춘다
    shown = []
    monkeypatch.setattr(res_mod.QMessageBox, "information", lambda *a, **k: shown.append(a[2]))

    class Page:
        ov = {"groups": []}

        def _selected(self):
            return ("group", {"name": "Y", "files": [{"erased": False, "translated": False, "composed": False}]})

    acts = res_mod.OutputActions(Page())
    assert acts._pick_group("⑧ 한글 넣기", acts._can_compose) is False and "⑥ 전체 번역" in shown[0]


def test_file_without_text_counts_as_translated_when_extracted(tmp_path, monkeypatch):
    from tests.test_project_steps import _project as steps_project
    from tests.test_translate import _write_extract

    info, fd = steps_project(tmp_path)
    _write_extract(fd, 1, "HELLO")
    _write_extract(fd, 2, "HELLO")
    workspace.set_status(info, "translating")
    monkeypatch.setattr(service, "_translated_files", lambda _i: {})  # 번역할 칸이 하나도 없는 경우
    f = service.project_overview(info.dir)["groups"][0]["files"][0]
    assert f["translated"] is True


def test_review_service_rows_save_same_source_marks_and_conflict(qapp, tmp_path):
    """번역 검수 service: 칸 목록·검사·저장(같은 원문 함께)·확인 표시·엑셀과 다름 해결·진행·다음 할 일."""
    from app import card_edits, review_marks

    info, fd = _translated_project(tmp_path)
    data = service.review_rows(info.dir)
    rows = [r for r in data["rows"] if r["region"] == "이름"]
    assert len(rows) == 2 and all(r["ko"] == "안녕" and r["level"] == "ok" for r in rows)
    assert rows[0]["same_source"] == 1 and data["counts"]["total"] >= 2 and data["excel"]["path"] is None
    chk = service.check_review_text(fd, 1, "이름", "HELLO 안녕")
    assert "일본어 남음" not in chk["status"]  # 영어는 일본어가 아님
    chk = service.check_review_text(fd, 1, "이름", "")
    assert chk["status"] == ["번역 없음"] and chk["level"] == "must"
    chk = service.check_review_text(fd, 1, "이름", "こんにちは")
    assert "일본어 남음" in chk["status"] and any(not t["ok"] for t in chk["terms"])  # 용어 HELLO→안녕 빠짐
    out = service.save_review(fd, 1, "이름", "반가워", same_source=True)
    assert {(r["page"], r["ko"], r["checked"]) for r in out} == {(1, "반가워", True), (2, "반가워", True)}
    assert card_edits.load(fd)[2]["이름"]["ko"] == "반가워"
    # 확인만 (글 그대로), 확인 취소
    r = service.set_review_checked(fd, 1, "이름", False)
    assert not r["checked"]
    r = service.save_review(fd, 1, "이름", None)[0]
    assert r["checked"] and r["ko"] == "반가워"
    # 확인한 뒤 글이 바뀌면 ‘확인 안 함’ + 바뀜 표시
    review_marks.mark(fd, 2, "이름", "다른 글")
    r2 = next(x for x in service.review_rows(info.dir, [fd])["rows"] if x["page"] == 2 and x["region"] == "이름")
    assert not r2["checked"] and r2["checked_stale"]
    # 엑셀·재번역으로 바탕 번역이 바뀐 칸: edit_conflict, 해결
    d = workspace.file_settings(fd)
    d["card_edits"]["2"]["이름"]["base"] = "옛 번역"
    workspace._write(fd / "file.json", d)
    service._clear_review_caches()
    r2 = next(x for x in service.review_rows(info.dir, [fd])["rows"] if x["page"] == 2 and x["region"] == "이름")
    assert r2["edit_conflict"] == {"app": "반가워", "now": "안녕"} and r2["ko"] == "안녕"
    r2 = service.resolve_review_conflict(fd, 2, "이름", "app")
    assert r2["ko"] == "반가워" and not r2["edit_conflict"]
    # 진행·다음 할 일: 지운 뒤 꼭 고칠 칸이 남으면 ‘check’
    service.save_review(fd, 1, "이름", "", checked=False)  # 비움 -> 번역 없음 (꼭 고칠 칸)
    assert service.erase_project(info.dir).state == "done"
    ov = service.project_overview(info.dir)
    assert ov["review"]["must_left"] >= 1 and ov["next_key"] == "check" and "번역을 검수하세요" in ov["next_step"]
    service.save_review(fd, 1, "이름", "안녕")
    ov = service.project_overview(info.dir)
    assert ov["review"]["must_left"] == 0 and ov["next_key"] == "compose"



def test_review_no_conflict_when_excel_already_has_app_edit_and_progress_follows_card_limit(qapp, tmp_path):
    """엑셀을 다시 만들어 앱에서 고친 번역이 엑셀에 들어갔으면 ‘엑셀과 앱이 다름’이 아니다. 시험 모드를 바꾸면 진행 수도 바뀐다."""
    info, fd = _translated_project(tmp_path)
    d = workspace.file_settings(fd)
    d["card_edits"] = {"2": {"이름": {"ko": "안녕", "base": "옛 번역"}}}  # 앱 고침 = 지금 번역
    workspace._write(fd / "file.json", d)
    r2 = next(x for x in service.review_rows(info.dir, [fd])["rows"] if x["page"] == 2 and x["region"] == "이름")
    assert r2["edit_conflict"] is None and r2["ko"] == "안녕"
    total = service.review_progress(info.dir)["total"]
    service.set_card_limit(info.dir, 1)
    assert service.review_progress(info.dir)["total"] < total  # 캐시가 시험 모드를 따른다


def test_translate_refreshes_existing_review_excel(tmp_path, monkeypatch):
    """⑥ 전체 번역이 새로 번역하면, 이미 있는 검수 엑셀을 다시 만든다 (한글 넣기가 옛 번역을 쓰지 않게)."""
    from app import translate

    info, _fd = _translated_project(tmp_path)
    made = []
    monkeypatch.setattr(translate, "run_translate", lambda *a, **k: translate.TranslateResult("done", translated=1))
    monkeypatch.setattr(service, "export_project_excel", lambda p, force=False: made.append(force))
    assert service.translate_project(info.dir, client=object()).state == "done" and made == []  # 엑셀 없음: 만들지 않음
    (info.game_dir / "번역_검수.xlsx").write_bytes(b"")
    assert service.translate_project(info.dir, client=object()).state == "done" and made == [True]

    def locked(p, force=False):
        raise PermissionError("열려 있음")
    monkeypatch.setattr(service, "export_project_excel", locked)
    r = service.translate_project(info.dir, client=object())
    assert r.state == "done" and "다시 만들지 못했습니다" in r.excel_note

# ================================================================ 번역 검수 화면
def _sync_workers(monkeypatch):
    """검수 창의 작업 스레드를 그 자리에서 돌려 테스트가 기다리지 않게 한다."""
    from app.qt import review

    def run(parent, title, job, done, fail=None, **k):
        job.signals.done.connect(done)
        if fail:
            job.signals.failed.connect(fail)
        job.run()

    monkeypatch.setattr(review.worker, "run_with_dialog", run)
    monkeypatch.setattr(review.worker, "start", lambda job: job.run())


def _review(tmp_path, monkeypatch, preset=None):
    from app.qt.review import TranslationReviewDialog

    _sync_workers(monkeypatch)
    info, fd = _translated_project(tmp_path)
    return info, fd, TranslationReviewDialog(info.dir, None, preset)


def _answers(monkeypatch, *answers):
    """검수 창이 묻는 말에 차례로 답하고 (제목, 글, 버튼들)을 모은다."""
    from app.qt import review

    asked, it = [], iter(answers)

    def fake(parent, title, text, buttons, default=None, informative=""):
        asked.append((title, text, [b[0] for b in buttons]))
        return next(it)
    monkeypatch.setattr(review, "ask_choice", fake)
    return asked


def test_review_button_follows_translation_and_shows_progress(qapp, tmp_path):
    from app.qt.main import MainWindow

    info = _project(tmp_path)
    win = MainWindow(root=tmp_path)
    win.open_project(info.dir)
    b = win.project.step_buttons["check"]
    assert b.text() == "번역 검수" and not b.isEnabled() and "먼저 ⑥ 전체 번역" in b.toolTip()
    win.close()
    info2, _fd = _translated_project(tmp_path / "t")
    win = MainWindow(root=tmp_path / "t")
    win.open_project(info2.dir)
    b = win.project.step_buttons["check"]
    assert b.isEnabled() and "확인 0/2칸" in b.toolTip() and "꼭 고칠 칸 0" in b.toolTip()
    keys = list(win.project.step_buttons)
    assert keys.index("check") == keys.index("translate") + 1
    win.close()


def test_review_dialog_defaults_edit_and_confirm_next(qapp, tmp_path, monkeypatch):
    from PySide6.QtTest import QTest

    from app import card_edits

    info, fd, dlg = _review(tmp_path, monkeypatch)
    assert dlg.view.currentData() == "unchecked"  # 꼭 고칠·확인해 볼 칸이 없으면 ‘확인 안 한 칸’부터
    assert len(dlg.view_ids) == 2 and dlg.table.rowCount() == 2 and dlg.table.currentRow() == 0
    assert dlg.same_box.text() == "같은 원문 칸 1개에도 똑같이 넣기" and not dlg.same_box.isHidden()
    assert dlg.pill.text().startswith("확인 0/2")
    assert dlg.table.item(0, 3).text() == "안녕" and dlg.table.item(0, 1).text() == "1쪽"  # 파일이 하나라 쪽만
    dlg.show()
    dlg.activateWindow()
    QTest.qWaitForWindowActive(dlg)
    dlg.edit.setFocus()
    dlg.edit.setPlainText("안녕하세요")
    QTest.keyClick(dlg.edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert [card_edits.load(fd)[p]["이름"]["ko"] for p in (1, 2)] == ["안녕하세요", "안녕하세요"]
    rows = {r["page"]: r for r in service.review_rows(info.dir)["rows"]}
    assert rows[1]["checked"] and rows[2]["checked"] and rows[1]["edited"]  # 같은 원문 칸도 함께
    assert dlg.counts["checked"] == 2 and dlg.pill.text().startswith("확인 2/2")
    assert dlg.current_row()["page"] == 2 and dlg.table.currentRow() == 1  # 지금 목록의 다음 줄
    assert dlg.table.item(0, 3).background().color().name().upper() == "#CFE2F3"  # 고친 번역은 하늘색
    assert "2개" in dlg.closing_message() and "다음 ⑧" in dlg.closing_message()
    dlg.done(0)


def test_review_confirm_without_change_only_marks_checked(qapp, tmp_path, monkeypatch):
    info, fd, dlg = _review(tmp_path, monkeypatch)
    asked = _answers(monkeypatch)
    dlg.confirm_next()
    assert not asked
    rows = {r["page"]: r for r in service.review_rows(info.dir)["rows"]}
    assert rows[1]["checked"] and not rows[1]["edited"] and not rows[2]["checked"]
    assert not dlg.closing_message() and dlg.current_row()["page"] == 2
    dlg.done(0)


def test_review_same_source_box_can_be_turned_off(qapp, tmp_path, monkeypatch):
    from app import card_edits

    info, fd, dlg = _review(tmp_path, monkeypatch)
    dlg.edit.setPlainText("다르게")
    dlg.same_box.setChecked(False)
    dlg.confirm_next()
    assert list(card_edits.load(fd)) == [1]
    dlg.done(0)


def test_review_asks_before_confirming_must_or_empty_text(qapp, tmp_path, monkeypatch):
    from app import card_edits

    info, fd, dlg = _review(tmp_path, monkeypatch)
    asked = _answers(monkeypatch, 1, 1, 0)
    dlg.edit.setPlainText("こんにちは")  # 일본어가 남음 = 꼭 고칠 칸
    dlg.confirm_next()
    assert "일본어 남음" in asked[0][1] and asked[0][2] == ["이대로 확인", "돌아가 고치기"] and not card_edits.load(fd)
    dlg.edit.setPlainText("")
    dlg.confirm_next()
    assert "번역이 비어 있습니다" in asked[1][1] and not card_edits.load(fd)
    dlg.confirm_next()  # 이번에는 ‘비운 채로 두기’
    assert card_edits.load(fd)[1]["이름"]["ko"] == ""
    assert next(r for r in service.review_rows(info.dir)["rows"] if r["page"] == 1)["checked"]
    dlg.done(0)


def test_review_unsaved_text_asks_before_moving_or_closing(qapp, tmp_path, monkeypatch):
    from app import card_edits

    info, fd, dlg = _review(tmp_path, monkeypatch)
    asked = _answers(monkeypatch, 2, 1, 0, 2, 1)
    dlg.edit.setPlainText("고침")
    dlg.table.setCurrentCell(1, 0)  # 취소: 그 자리에
    assert dlg.table.currentRow() == 0 and dlg.edit.toPlainText() == "고침" and asked[0][2] == ["저장", "버리기", "취소"]
    dlg.table.setCurrentCell(1, 0)  # 버리기: 옮기고 저장 안 함
    assert dlg.table.currentRow() == 1 and not card_edits.load(fd) and dlg.edit.toPlainText() == "안녕"
    dlg.edit.setPlainText("둘째")
    dlg.same_box.setChecked(False)
    dlg.table.setCurrentCell(0, 0)  # 저장
    assert dlg.table.currentRow() == 0 and card_edits.load(fd)[2]["이름"]["ko"] == "둘째"
    # 거르기를 바꿔 보던 칸이 사라질 때도, 닫을 때도 묻는다
    dlg.edit.setPlainText("또")
    dlg.view.setCurrentIndex(4)  # 확인한 칸: 지금 칸이 빠진다 -> 취소
    assert dlg.view.currentData() == "unchecked" and dlg.edit.toPlainText() == "또"
    dlg.done(0)  # 버리고 닫기
    assert dlg._closed and len(asked) == 5


def test_review_revert_and_use_base(qapp, tmp_path, monkeypatch):
    from app import card_edits

    info, fd, dlg = _review(tmp_path, monkeypatch)
    dlg.edit.setPlainText("고침")
    dlg.same_box.setChecked(False)
    dlg.save_here()
    assert card_edits.load(fd)[1]["이름"]["ko"] == "고침" and dlg.current_row()["edited"]
    dlg.edit.setPlainText("또 고침")
    dlg.use_base()  # 편집 칸만 초벌로
    assert dlg.edit.toPlainText() == "안녕" and card_edits.load(fd)[1]["이름"]["ko"] == "고침"
    dlg.revert_to_base(dlg.current_row()["id"])
    assert not card_edits.load(fd) and not dlg.current_row()["edited"] and dlg.edit.toPlainText() == "안녕"
    dlg.done(0)


def test_review_edit_conflict_buttons(qapp, tmp_path, monkeypatch):
    from app import card_edits

    info, fd, dlg = _review(tmp_path, monkeypatch)
    assert dlg.conflict.isHidden()
    for keep, want in (("now", None), ("app", "앱고침")):
        card_edits.set_text(fd, 1, "이름", "앱고침", "옛번역")  # 앱에서 고친 뒤 번역이 바뀐 칸
        service._clear_review_caches()
        dlg.load()
        dlg.status.setCurrentIndex(7)  # 엑셀과 앱이 다름
        assert dlg.view_ids and dlg.current_row()["edit_conflict"] == {"app": "앱고침", "now": "안녕"}
        assert not dlg.conflict.isHidden() and dlg.edit.toPlainText() == "안녕"
        (dlg.keep_now if keep == "now" else dlg.keep_app).click()
        got = card_edits.load(fd).get(1, {}).get("이름")
        assert (got["ko"] if got else None) == want
        assert dlg.conflict.isHidden() and dlg.current_row()["edit_conflict"] is None
        dlg.status.setCurrentIndex(0)
    dlg.done(0)


def test_review_filters_and_search(qapp, tmp_path, monkeypatch):
    info, fd, dlg = _review(tmp_path, monkeypatch)
    dlg.edit.setPlainText("안녕하세요")
    dlg.same_box.setChecked(False)
    dlg.save_here()
    assert len(dlg.view_ids) == 2  # 저장한 줄은 바로 사라지지 않는다
    dlg.view.setCurrentIndex(3)  # 사람이 고친 칸
    assert len(dlg.view_ids) == 1
    dlg.view.setCurrentIndex(4)  # 확인한 칸
    assert len(dlg.view_ids) == 1
    dlg.view.setCurrentIndex(2)
    dlg.search.setText("하세요")
    dlg._on_filter_changed()
    assert len(dlg.view_ids) == 1
    dlg.search.setText("없는말")
    dlg._on_filter_changed()
    assert not dlg.view_ids and not dlg.edit.isEnabled() and "맞는 칸이 없습니다" in dlg.head.text()
    dlg.search.setText("")
    dlg._on_filter_changed()
    dlg.status.setCurrentIndex(1)  # 번역 없음: 없다
    assert not dlg.view_ids
    dlg.done(0)


def test_review_preview_only_for_erased_cards(qapp, tmp_path, monkeypatch):
    info, fd, dlg = _review(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(service, "card_preview", lambda *a, **k: calls.append(a) or None)
    dlg.request_preview()
    assert not calls and "글자를 지운 뒤에" in dlg.pv_note.text()  # 지운 카드가 없으면 그리지 않는다
    dlg.current_row()["erased"] = True
    dlg.edit.setPlainText("그릴 글")
    dlg.request_preview()
    assert len(calls) == 1 and calls[0][3] == "그릴 글" and "그려 볼 번역이 없습니다" in dlg.pv_note.text()
    dlg.done(0)


def test_review_stale_preview_result_is_dropped(qapp, tmp_path, monkeypatch):
    info, fd, dlg = _review(tmp_path, monkeypatch)
    dlg._pv_gen = 5
    dlg.pv_note.setText("그리는 중…")
    dlg._preview_done(4, None)  # 옛 결과는 버린다
    assert dlg.pv_note.text() == "그리는 중…"
    dlg.done(0)
    dlg._preview_done(dlg._pv_gen, None)  # 닫은 뒤에 와도 죽지 않는다


def test_review_excel_row_and_export(qapp, tmp_path, monkeypatch):
    info, fd, dlg = _review(tmp_path, monkeypatch)
    assert dlg.excel_label.text() == "검수 엑셀: 없음" and dlg.b_xl_make.text() == "엑셀로 내보내기…"
    assert dlg.b_xl_open.isHidden()
    dlg.export_excel()
    assert dlg.excel["path"] and Path(dlg.excel["path"]).exists()
    assert "검수 엑셀이 있습니다" in dlg.excel_label.text() and dlg.b_xl_make.text() == "엑셀 다시 만들기"
    assert not dlg.b_xl_open.isHidden() and dlg.excel_hint.isHidden()
    dlg.excel["open"] = True
    dlg._sync_excel()
    assert not dlg.excel_hint.isHidden() and "한쪽에서만" in dlg.excel_hint.text()
    dlg.done(0)


def test_review_layout_fits_small_screen(qapp, tmp_path, monkeypatch):
    info, fd, dlg = _review(tmp_path, monkeypatch)
    assert dlg.minimumSizeHint().width() <= 1366 and dlg.table.parentWidget().minimumWidth() >= 420
    for c in (dlg.scope, dlg.view, dlg.status, dlg.region):
        assert c.sizeAdjustPolicy() == c.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
    dlg.done(0)


def test_review_opens_from_next_step_with_selected_scope(qapp, tmp_path, monkeypatch):
    from app.qt import review
    from app.qt.main import MainWindow

    _sync_workers(monkeypatch)
    info, fd = _translated_project(tmp_path)
    win = MainWindow(root=tmp_path)
    win.open_project(info.dir)
    page = win.project
    shown = []
    monkeypatch.setattr(review.TranslationReviewDialog, "exec", lambda self: shown.append(self.scope.currentText()) or 0)
    page.ov["next_key"] = "check"
    page.do_next()
    assert shown == ["모든 그룹"]
    page.tree.setCurrentItem(page.all_item.child(0))  # 그룹을 고른 채 열면 범위를 미리 고른다
    page.ov["next_key"] = "check"
    page.do_next()
    assert shown[1].startswith("그룹 ")
    win.close()


def test_compose_asks_when_must_cards_left(qapp, tmp_path, monkeypatch):
    from app.qt import results as res_mod
    from app.qt.main import MainWindow

    info, fd = _translated_project(tmp_path)
    win = MainWindow(root=tmp_path)
    win.open_project(info.dir)
    out = win.project.out
    key = win.project.scoped_files()[0]["key"]
    monkeypatch.setattr(service, "review_progress", lambda p: {"by_file": {key: {"must_left": 0}}})
    got = []
    monkeypatch.setattr(res_mod, "ask_choice", lambda *a, **k: got.append(a[2]) or 0)
    assert out._review_left_ok() and not got  # 남은 칸이 없으면 묻지 않는다
    monkeypatch.setattr(service, "review_progress", lambda p: {"by_file": {key: {"must_left": 12}}})
    opened = []
    monkeypatch.setattr(win.project.steps, "review_translation", lambda: opened.append(1))
    assert out._review_left_ok() is False and opened == [1] and "12개" in got[0]  # 번역 검수 열기
    monkeypatch.setattr(res_mod, "ask_choice", lambda *a, **k: 1)
    assert out._review_left_ok() is True  # 그대로 넣기
    monkeypatch.setattr(res_mod, "ask_choice", lambda *a, **k: 2)
    assert out._review_left_ok() is False  # 취소
    win.close()


def test_background_erase_starts_after_glossary_step_reerases_changed_and_boosts(qapp, tmp_path, monkeypatch):
    """뒤에서 글자 지우기: ④부터 저절로, 원문이 바뀐 파일은 다시, ⑦을 누르면 서두르거나 이미 지웠다고 알린다."""
    from app.qt import bg_erase
    from app.qt import results as res_mod
    from app.qt.main import MainWindow
    from tests.test_translate import _write_extract

    monkeypatch.delenv("CARDTRANSLATER_NO_BG_ERASE", raising=False)
    started = []

    def run_now(job):  # 작업 스레드 대신 바로 (테스트가 기다리지 않게)
        started.append(job)
        job.run()
        return job
    monkeypatch.setattr(bg_erase.worker, "start", run_now)
    info, fd = _translated_project(tmp_path)
    workspace.set_status(info, "extracting")
    win = MainWindow(root=tmp_path)
    win.open_project(info.dir)
    assert not started and not (fd / "cleared" / "cleared.pdf").exists()  # ④ 전에는 하지 않는다
    workspace.set_status(info, "glossary_review")  # ④ 용어집 만들기를 누름
    win.project.refresh()
    assert len(started) == 1 and (fd / "cleared" / "cleared.pdf").exists() and not win.project.bg.active
    assert win.bg_label.text() == "" and service.files_needing_erase(info.dir) == []
    _write_extract(fd, 2, "HELLO AGAIN")  # ③ 원문 확인에서 고침
    win.project.refresh()
    assert len(started) == 2 and service.stale_erased_pages(fd) == []  # 바뀐 파일을 다시 지움
    # ⑦: 다 지웠으면 알리기만
    shown = []
    monkeypatch.setattr(res_mod.QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    ran = []
    monkeypatch.setattr(res_mod.worker, "run_with_dialog", lambda *a, **k: ran.append(a))
    asked = []
    monkeypatch.setattr(res_mod, "ask_choice", lambda _p, _t, text, *a, **k: asked.append(text) or 0)
    win.project.out.erase()
    assert "이미 지웠습니다" in asked[-1] and not ran
    monkeypatch.setattr(res_mod, "ask_choice", lambda *a, **k: 1)  # ‘그래도 다시 지우기’
    win.project.out.erase()
    assert len(ran) == 1 and ran[0][2].kwargs.get("redo") is True
    assert win.project.bg.holds == 1  # 다시 지우는 동안 뒤에서 지우기는 쉰다
    ran[0][2].signals.cancelled.emit()  # 취소해도 놓아 준다
    assert win.project.bg.holds == 0
    ran.clear()
    # ⑦: 뒤에서 지우는 중이면 우선순위만 높인다
    bg = win.project.bg
    from types import SimpleNamespace

    bg.job, bg._current, bg.queue = SimpleNamespace(thread=None, cancel=lambda: None), fd, []  # 지우는 중인 척
    bg.text = "뒤에서 글자 지우기 1/1 파일 (천천히)"
    win.project.out.erase()
    assert bg.boosted and "우선순위를 높였습니다" in shown[-1] and not ran
    bg.job = None
    bg.stop()
    assert not bg.active and win.bg_label.text() == ""


def test_run_exclusive_waits_for_stopped_background_thread(qapp, tmp_path, monkeypatch):
    """화면 작업(⑦·⑧·결과 보기)은 뒤에서 지우던 스레드가 정말 끝난 뒤에 시작하고, 끝나면(취소도) 뒤에서 지우기를 놓아 준다."""
    from app.qt import bg_erase, worker

    class Page:
        project_dir = None
        ov: dict = {}

    from PySide6.QtWidgets import QWidget

    holder = QWidget()
    bg = bg_erase.BackgroundEraser(holder)
    bg.page = Page()
    old = worker.Job(lambda **k: None)
    bg.job = old  # 지우는 중
    bg.stop()
    assert not bg.active and not bg.idle  # 멈추라고 했지만 스레드는 아직
    started = []
    monkeypatch.setattr(bg_erase.worker, "run_with_dialog", lambda *a, **k: started.append(a[2]))
    fg = worker.Job(lambda **k: None)
    bg_erase.run_exclusive(holder, "글자 지우기", fg, lambda r: None)
    assert started == [] and bg.holds == 1  # 옛 스레드가 끝나기를 기다린다
    bg._ended(old)  # 옛 스레드가 다음 쪽에서 멈춤
    assert started == [fg] and bg.idle
    fg.signals.cancelled.emit()
    assert bg.holds == 0
