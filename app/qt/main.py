"""메인 창: 홈(게임·프로젝트 목록) ↔ 프로젝트 화면(그룹·파일·다음 할 일)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QHBoxLayout, QHeaderView, QLabel, QMainWindow, QMessageBox, QPushButton,
    QStackedWidget, QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from app import service
from app.qt.dialogs import AddFilesDialog, ExcludePagesDialog, NewProjectDialog, SettingsDialog
from app.qt.region_editor import RegionEditor

APP_NAME = "CardTranslater"


def open_path(p: Path) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))


class HomePage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.root_label = QLabel()
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["프로젝트", "게임", "상태"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.doubleClicked.connect(lambda _i: self.open_selected())
        new = QPushButton("새 프로젝트")
        new.clicked.connect(self.new_project)
        opn = QPushButton("열기")
        opn.clicked.connect(self.open_selected)
        ref = QPushButton("새로 고침")
        ref.clicked.connect(self.refresh)
        btns = QHBoxLayout()
        for b in (new, opn, ref):
            btns.addWidget(b)
        btns.addStretch(1)
        title = QLabel("프로젝트")
        title.setFont(QFont(title.font().family(), 14, QFont.Weight.Bold))
        lay = QVBoxLayout(self)
        lay.addWidget(title)
        lay.addWidget(self.root_label)
        lay.addLayout(btns)
        lay.addWidget(self.table, 1)
        self.projects: list[dict] = []

    def refresh(self):
        root = self.win.root
        self.root_label.setText(f"데이터 폴더: {root}")
        self.projects = service.list_projects(root)
        self.table.setRowCount(len(self.projects))
        for i, p in enumerate(self.projects):
            for j, v in enumerate((p["name"], p["game"], service.STATUS_LABEL.get(p["status"], p["status"]))):
                self.table.setItem(i, j, QTableWidgetItem(v))

    def open_selected(self):
        row = self.table.currentRow()
        if 0 <= row < len(self.projects):
            self.win.open_project(Path(self.projects[row]["dir"]))

    def new_project(self):
        dlg = NewProjectDialog(self.win.root, self)
        if dlg.exec() and dlg.created:
            self.refresh()
            self.win.open_project(dlg.created)


class ProjectPage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.project_dir: Path | None = None
        self.ov: dict = {}
        self.title = QLabel()
        self.title.setFont(QFont(self.title.font().family(), 14, QFont.Weight.Bold))
        self.sub = QLabel()
        self.next = QLabel()
        self.next.setStyleSheet("color: #0b5cad; font-weight: bold")
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["그룹 / 파일", "쪽", "뺀 쪽", "추출", "지움", "한글"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.itemSelectionChanged.connect(self._sync_buttons)
        self.tree.itemDoubleClicked.connect(lambda *_a: self.edit_regions())
        back = QPushButton("← 프로젝트 목록")
        back.clicked.connect(self.win.show_home)
        self.b_add = QPushButton("파일 넣기")
        self.b_add.clicked.connect(self.add_files)
        self.b_regions = QPushButton("영역 지정")
        self.b_regions.clicked.connect(self.edit_regions)
        self.b_pages = QPushButton("뺄 쪽")
        self.b_pages.clicked.connect(self.exclude_pages)
        self.b_folder = QPushButton("폴더 열기")
        self.b_folder.clicked.connect(self.open_folder)
        ref = QPushButton("새로 고침")
        ref.clicked.connect(self.refresh)
        btns = QHBoxLayout()
        for b in (back, self.b_add, self.b_regions, self.b_pages, self.b_folder, ref):
            btns.addWidget(b)
        btns.addStretch(1)
        lay = QVBoxLayout(self)
        lay.addWidget(self.title)
        lay.addWidget(self.sub)
        lay.addWidget(self.next)
        lay.addLayout(btns)
        lay.addWidget(self.tree, 1)

    def load(self, project_dir: Path):
        self.project_dir = Path(project_dir)
        self.refresh()

    def refresh(self):
        if self.project_dir is None:
            return
        self.ov = service.project_overview(self.project_dir)
        o = self.ov
        self.title.setText(o["name"])
        gv = f" · 용어집 v{o['glossary_version']}" if o.get("glossary_version") else ""
        self.sub.setText(f"게임: {o['game']} · 상태: {o['status_label']}{gv}")
        self.next.setText(f"다음 할 일: {o['next_step']}")
        self.tree.clear()
        for g in o["groups"]:
            gi = QTreeWidgetItem([f"{g['name']}  (영역 {g['regions']}개)" if g["has_regions"]
                                  else f"{g['name']}  (영역 없음 — 영역 지정 필요)"])
            gi.setData(0, Qt.ItemDataRole.UserRole, ("group", g))
            self.tree.addTopLevelItem(gi)
            for f in g["files"]:
                ex = ", ".join(map(str, f["excluded"])) or "-"
                fi = QTreeWidgetItem([f["name"], str(f["pages"]), ex, f"{f['extracted']}/{f['included']}",
                                      "예" if f["erased"] else "-", "예" if f["composed"] else "-"])
                fi.setData(0, Qt.ItemDataRole.UserRole, ("file", f))
                if f["exclude_reason"]:
                    fi.setToolTip(2, f["exclude_reason"])
                gi.addChild(fi)
            gi.setExpanded(True)
        self._sync_buttons()

    def _selected(self):
        it = self.tree.currentItem()
        return it.data(0, Qt.ItemDataRole.UserRole) if it else None

    def _group_of_selection(self) -> dict | None:
        sel = self._selected()
        if not sel:
            return self.ov["groups"][0] if len(self.ov.get("groups", [])) == 1 else None
        kind, d = sel
        if kind == "group":
            return d
        return next(g for g in self.ov["groups"] if any(f["dir"] == d["dir"] for f in g["files"]))

    def _sync_buttons(self):
        sel = self._selected()
        self.b_pages.setEnabled(bool(sel and sel[0] == "file"))
        self.b_regions.setEnabled(self._group_of_selection() is not None)

    def add_files(self):
        dlg = AddFilesDialog(self.project_dir, self)
        if dlg.exec():
            self.refresh()
            info = dlg.result_info or {}
            if info.get("added") and not info.get("has_regions"):
                if QMessageBox.question(self, "영역 지정", f"‘{info['group']}’ 그룹에 영역이 없습니다. 지금 지정할까요?") \
                        == QMessageBox.StandardButton.Yes:
                    g = next((g for g in self.ov["groups"] if g["name"] == info["group"]), None)
                    if g:
                        self._open_editor(g)

    def edit_regions(self):
        g = self._group_of_selection()
        if g is None:
            QMessageBox.information(self, "그룹 고르기", "영역을 지정할 그룹(또는 그 안의 파일)을 고르세요")
            return
        self._open_editor(g)

    def _open_editor(self, g: dict):
        if not g["files"]:
            QMessageBox.information(self, "파일 없음", "그룹에 파일을 먼저 넣으세요")
            return
        ed = RegionEditor(Path(g["dir"]), [Path(f["dir"]) for f in g["files"]], self)
        ed.saved.connect(self.refresh)
        ed.setWindowState(ed.windowState() | Qt.WindowState.WindowMaximized)
        ed.exec()
        self.refresh()

    def exclude_pages(self):
        sel = self._selected()
        if not sel or sel[0] != "file":
            return
        f = sel[1]
        if ExcludePagesDialog(Path(f["dir"]), f["excluded"], f["exclude_reason"], self).exec():
            self.refresh()

    def open_folder(self):
        sel = self._selected()
        if sel:
            open_path(Path(sel[1]["dir"]))
        elif self.project_dir:
            open_path(self.project_dir)


class MainWindow(QMainWindow):
    def __init__(self, root: Path | None = None):
        super().__init__()
        self.root = Path(root) if root else service.data_dir()
        self.setWindowTitle(APP_NAME)
        self.resize(1100, 720)
        self.stack = QStackedWidget()
        self.home = HomePage(self)
        self.project = ProjectPage(self)
        self.stack.addWidget(self.home)
        self.stack.addWidget(self.project)
        self.setCentralWidget(self.stack)
        menu = self.menuBar().addMenu("파일")
        act = QAction("설정…", self)
        act.triggered.connect(self.open_settings)
        menu.addAction(act)
        quit_ = QAction("끝내기", self)
        quit_.triggered.connect(self.close)
        menu.addAction(quit_)
        st = service.engine_status()
        self.statusBar().showMessage(st["message"])
        self.show_home()

    def show_home(self):
        self.home.refresh()
        self.stack.setCurrentWidget(self.home)

    def open_project(self, project_dir: Path):
        self.project.load(project_dir)
        self.stack.setCurrentWidget(self.project)

    def open_settings(self):
        if SettingsDialog(self).exec():
            self.root = service.data_dir()
            self.show_home()


def main(argv: list[str] | None = None) -> int:
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    if sys.platform == "win32" and not os.environ.get("QT_QPA_PLATFORM"):
        app.setFont(QFont("Malgun Gothic", 10))
    win = MainWindow()
    win.show()
    return app.exec()
