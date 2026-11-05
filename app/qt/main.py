"""메인 창: 홈(게임·프로젝트 목록) ↔ 프로젝트 화면(그룹·파일·다음 할 일)."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QHBoxLayout, QHeaderView, QLabel, QMainWindow, QMessageBox, QPushButton,
    QStackedWidget, QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from app import service
from app.qt.dialogs import AddFilesDialog, ExcludePagesDialog, NewProjectDialog, SettingsDialog, install_korean
from app.qt.region_editor import RegionEditor
from app.qt.results import OutputActions
from app.qt.steps import StepActions

APP_NAME = "CardTranslater"


def open_path(p: Path) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))


class HomePage(QWidget):
    """게임 > 프로젝트 목록. 용어집·아이콘 사전·검수 엑셀은 게임에 속하고, 프로젝트는 게임 안의 작업 한 건이다."""

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.root_label = QLabel()
        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["게임 / 프로젝트", "상태", "게임 자료"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemDoubleClicked.connect(lambda _it, _c: self.open_selected())
        new = QPushButton("새 프로젝트")
        new.setToolTip("고른 게임에 프로젝트를 만듭니다 (게임을 고르지 않았으면 새 게임도 만들 수 있음)")
        new.clicked.connect(self.new_project)
        opn = QPushButton("열기")
        opn.clicked.connect(self.open_selected)
        folder = QPushButton("게임 폴더 열기")
        folder.setToolTip("용어집·아이콘 검증 엑셀과 번역 검수 엑셀이 있는 게임 폴더")
        folder.clicked.connect(self.open_game_folder)
        ref = QPushButton("새로 고침")
        ref.clicked.connect(self.refresh)
        dele = QPushButton("프로젝트 삭제")
        dele.clicked.connect(self.delete_selected)
        btns = QHBoxLayout()
        for b in (new, opn, folder, ref, dele):
            btns.addWidget(b)
        btns.addStretch(1)
        title = QLabel("게임 · 프로젝트")
        title.setFont(QFont(title.font().family(), 14, QFont.Weight.Bold))
        lay = QVBoxLayout(self)
        lay.addWidget(title)
        lay.addWidget(self.root_label)
        lay.addLayout(btns)
        lay.addWidget(self.tree, 1)
        self.projects: list[dict] = []
        self.games: list[dict] = []

    def refresh(self):
        root = self.win.root
        self.root_label.setText(f"데이터 폴더: {root}")
        self.projects = service.list_projects(root)
        self.games = service.list_games(root)
        known = {g["name"] for g in self.games}
        for p in self.projects:  # 게임 폴더가 없는 프로젝트(예전 데이터)도 보여 준다
            if p["game"] not in known:
                self.games.append({"name": p["game"], "dir": None, "terms": 0, "projects": [], "files": {}})
                known.add(p["game"])
        self.tree.clear()
        bold = QFont(self.tree.font())
        bold.setBold(True)
        for g in self.games:
            ps = [p for p in self.projects if p["game"] == g["name"]]
            extra = [f"용어 {g['terms']}개"] if g.get("terms") else []
            extra += [label for key, label in (("glossary_review", "용어집 검증"), ("icon_review", "아이콘 검증"),
                                               ("translation_review", "검수 엑셀")) if (g.get("files") or {}).get(key)]
            gi = QTreeWidgetItem([g["name"], f"프로젝트 {len(ps)}개", " · ".join(extra)])
            gi.setFont(0, bold)
            gi.setData(0, Qt.ItemDataRole.UserRole, ("game", g))
            for p in ps:
                pi = QTreeWidgetItem([p["name"], service.STATUS_LABEL.get(p["status"], p["status"]), ""])
                pi.setData(0, Qt.ItemDataRole.UserRole, ("project", p))
                gi.addChild(pi)
            self.tree.addTopLevelItem(gi)
            gi.setExpanded(True)

    def selected(self):
        it = self.tree.currentItem()
        return it.data(0, Qt.ItemDataRole.UserRole) if it else None

    def select_project(self, name: str) -> bool:
        for i in range(self.tree.topLevelItemCount()):
            gi = self.tree.topLevelItem(i)
            for j in range(gi.childCount()):
                if gi.child(j).data(0, Qt.ItemDataRole.UserRole)[1]["name"] == name:
                    self.tree.setCurrentItem(gi.child(j))
                    return True
        return False

    def open_selected(self):
        sel = self.selected()
        if not sel:
            return
        if sel[0] == "project":
            self.win.open_project(Path(sel[1]["dir"]))
        else:
            it = self.tree.currentItem()
            it.setExpanded(not it.isExpanded())

    def _game_of_selection(self) -> str | None:
        sel = self.selected()
        return sel[1]["name"] if sel and sel[0] == "game" else (sel[1]["game"] if sel else None)

    def open_game_folder(self):
        game = self._game_of_selection()
        g = next((x for x in self.games if x["name"] == game), None)
        if not g or not g.get("dir"):
            QMessageBox.information(self, "게임 고르기", "폴더를 열 게임(또는 그 게임의 프로젝트)을 고르세요")
            return
        open_path(Path(g["dir"]))

    def delete_selected(self):
        sel = self.selected()
        if not sel or sel[0] != "project":
            QMessageBox.information(self, "프로젝트 고르기", "삭제할 프로젝트를 고르세요 (게임은 지우지 않습니다)")
            return
        p = sel[1]
        if QMessageBox.question(
                self, "프로젝트 삭제",
                f"‘{p['name']}’ 프로젝트를 삭제할까요?\n\n그룹·파일·추출·번역·결과가 모두 데이터 폴더의 .trash로 옮겨집니다 "
                f"(되살리려면 그 폴더에서 projects/로 다시 옮기세요).\n게임 ‘{p['game']}’의 용어집·아이콘 사전·검수 엑셀은 남습니다.") \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            dest = service.delete_project(p["dir"])
        except OSError as e:
            QMessageBox.warning(self, "삭제할 수 없음", f"{e}\n(엑셀 등 이 프로젝트의 파일이 열려 있으면 닫고 다시 하세요)")
            return
        self.win.statusBar().showMessage(f"휴지통으로 옮김: {dest}")
        self.refresh()

    def new_project(self):
        dlg = NewProjectDialog(self.win.root, self, game=self._game_of_selection())
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
        self.b_delete = QPushButton("삭제")
        self.b_delete.setToolTip("고른 그룹 또는 파일을 휴지통(.trash)으로")
        self.b_delete.clicked.connect(self.delete_selected)
        btns = QHBoxLayout()
        for b in (back, self.b_add, self.b_regions, self.b_pages, self.b_folder, ref, self.b_delete):
            btns.addWidget(b)
        btns.addStretch(1)
        # 2단계 작업: 추출 -> 원문 확인 -> 아이콘 -> 추출 완료(용어집) -> 용어집 가져오기 -> 번역 -> 검수 엑셀
        self.steps = StepActions(self)
        self.step_buttons = {}
        steps = QHBoxLayout()
        steps.addWidget(QLabel("작업:"))
        for key, text, fn in (
                ("extract", "① 글자 추출", self.steps.extract),
                ("review", "② 원문 확인", self.steps.review_sources),
                ("icons", "③ 아이콘 이름", self.steps.icons),
                ("finish", "④ 추출 완료 → 용어집", self.steps.finish),
                ("glossary", "⑤ 용어집 가져오기", self.steps.import_glossary),
                ("translate", "⑥ 번역", self.steps.translate),
                ("excel", "⑦ 검수 엑셀", self.steps.export_excel)):
            b = QPushButton(text)
            b.clicked.connect(lambda _c=False, f=fn: f())
            self.step_buttons[key] = b
            steps.addWidget(b)
        steps.addStretch(1)
        # 3단계: 지우기 -> 한글 넣기 -> 결과 보기, 시험 실행
        self.out = OutputActions(self)
        outs = QHBoxLayout()
        outs.addWidget(QLabel("결과:"))
        for key, text, fn in (
                ("erase", "⑧ 글자 지우기", self.out.erase),
                ("compose", "⑨ 한글 넣기", self.out.compose),
                ("results", "결과 보기", self.out.results),
                ("test", "시험 실행 (몇 쪽만)", self.out.test_run)):
            b = QPushButton(text)
            b.clicked.connect(lambda _c=False, f=fn: f())
            self.step_buttons[key] = b
            outs.addWidget(b)
        outs.addStretch(1)
        lay = QVBoxLayout(self)
        lay.addWidget(self.title)
        lay.addWidget(self.sub)
        lay.addWidget(self.next)
        lay.addLayout(btns)
        lay.addLayout(steps)
        lay.addLayout(outs)
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
        lim = f" · 시험 모드: 파일마다 앞 {o['card_limit']}장만" if o.get("card_limit") else ""
        self.sub.setText(f"게임: {o['game']} · 상태: {o['status_label']}{gv}{lim}")
        self.next.setText(f"다음 할 일: {o['next_step']}")
        self.tree.clear()
        for g in o["groups"]:
            if g.get("per_card"):
                left = sum(len(f["no_regions"]) for f in g["files"])
                total = sum(f["included"] for f in g["files"])
                label = f"{g['name']}  (카드마다 영역 — 지정 {total - left}/{total}장)"
            elif g["has_regions"]:
                label = f"{g['name']}  (영역 {g['regions']}개)"
            else:
                label = f"{g['name']}  (영역 없음 — 영역 지정 필요)"
            gi = QTreeWidgetItem([label])
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
        self._sync_steps()

    def _sync_steps(self):
        o = self.ov
        files = [f for g in o["groups"] for f in g["files"]]
        ready = bool(files) and all(g["has_regions"] for g in o["groups"] if g["files"])
        extracted = any(f["extracted"] for f in files)
        st = o["status"]
        on = {
            "extract": ready, "review": extracted, "icons": extracted,
            "finish": extracted and st in ("extracting", "glossary_review", "glossary_confirmed", "translated"),
            # 확정 뒤에도 고친 엑셀을 다시 가져올 수 있다 (바뀐 용어가 든 칸만 다시 번역됨)
            "glossary": st in ("glossary_review", "glossary_confirmed", "translating", "translated"),
            "translate": st in ("glossary_confirmed", "translating", "translated"),
            "excel": st == "translated",
            "erase": ready,
            "compose": any(f["erased"] for f in files) and st == "translated",
            "results": any(f["composed"] for f in files),
            "test": ready and st == "translated",
        }
        for k, b in self.step_buttons.items():
            b.setEnabled(on[k])

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
        self.b_delete.setEnabled(bool(sel))
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
        groups = [{"name": x["name"], "dir": x["dir"], "files": [f["dir"] for f in x["files"]]}
                  for x in self.ov["groups"]]
        ed = RegionEditor(Path(g["dir"]), [Path(f["dir"]) for f in g["files"]], self, groups=groups)
        ed.saved.connect(self.refresh)
        ed.setWindowState(ed.windowState() | Qt.WindowState.WindowMaximized)
        ed.exec()
        self.refresh()

    def delete_selected(self):
        sel = self._selected()
        if not sel:
            return
        kind, d = sel
        if kind == "group":
            what = (f"‘{d['name']}’ 그룹을 삭제할까요?\n\n영역 템플릿과 그룹 안 파일 {len(d['files'])}개의 "
                    "추출·지운 카드·한글 카드가 모두 데이터 폴더의 .trash로 옮겨집니다.")
        else:
            what = (f"‘{d['key']}’ 파일을 그룹에서 삭제할까요?\n\n이 파일의 원본 사본·추출·지운 카드·한글 카드가 "
                    "데이터 폴더의 .trash로 옮겨집니다. 처음에 고른 원래 PDF는 그대로입니다.")
        if QMessageBox.question(self, "삭제", what) != QMessageBox.StandardButton.Yes:
            return
        try:
            dest = service.delete_group(self.project_dir, d["name"]) if kind == "group" else service.delete_file(d["dir"])
        except (OSError, KeyError, ValueError) as e:
            QMessageBox.warning(self, "삭제할 수 없음", f"{e}\n(이 파일의 엑셀·PDF가 열려 있으면 닫고 다시 하세요)")
            return
        self.win.statusBar().showMessage(f"휴지통으로 옮김: {dest}")
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
        login = QAction("로그인 방식…", self)
        login.triggered.connect(self.change_login)
        menu.addAction(login)
        act = QAction("설정…", self)
        act.triggered.connect(self.open_settings)
        menu.addAction(act)
        quit_ = QAction("끝내기", self)
        quit_.triggered.connect(self.close)
        menu.addAction(quit_)
        st = service.engine_status()
        self.statusBar().showMessage(st["message"])
        self.show_home()

    def resume_last(self) -> bool:
        """앱을 다시 켜면 마지막에 연 프로젝트를 이어서 연다 (데이터 폴더 안의 것만)."""
        last = service.last_project()
        if last and self.root.resolve() in last.resolve().parents:
            self.open_project(last)
            return True
        return False

    def show_home(self):
        self.home.refresh()
        self.stack.setCurrentWidget(self.home)

    def open_project(self, project_dir: Path):
        self.project.load(project_dir)
        self.stack.setCurrentWidget(self.project)
        service.remember_project(project_dir)

    def change_login(self):
        from app.qt.login import ask_login

        if ask_login(self, first_run=False):
            self.statusBar().showMessage(service.engine_status()["message"])

    def open_settings(self):
        if SettingsDialog(self).exec():
            self.root = service.data_dir()
            self.show_home()


def main(argv: list[str] | None = None) -> int:
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    if sys.platform == "win32":  # 한글 화면 글꼴 (화면 없이 띄우는 점검 모드 포함)
        app.setFont(QFont("Malgun Gothic", 10))
    ko = install_korean(app)  # 표준 버튼(취소·예·아니요)을 한국어로
    args = list(argv if argv is not None else sys.argv)
    if "--selftest" in args:  # exe 점검: 창을 띄우지 않고 결과를 JSON으로 쓴다
        import json

        out = Path(args[args.index("--selftest") + 1]) if len(args) > args.index("--selftest") + 1 else None
        res = service.self_check()
        res["ko_translation"] = ko
        from PySide6.QtWidgets import QDialogButtonBox

        probe = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)  # 취소 버튼 문구가 한국어인지
        res["ko_cancel_text"] = probe.button(QDialogButtonBox.StandardButton.Cancel).text()
        if not ko or res["ko_cancel_text"] != "취소":
            res["ok"] = False
        try:
            MainWindow()
            res["ui"] = True
        except Exception as e:  # noqa: BLE001
            res["ui"] = f"실패: {e}"
            res["ok"] = False
        text = json.dumps(res, ensure_ascii=False, indent=2, default=str)
        if out:
            out.write_text(text, encoding="utf-8")
        return 0 if res["ok"] else 1
    if "--smoke" in args:  # exe 점검: 프로젝트를 열고 몇 쪽 시험 실행, 화면 캡처 (프로젝트 결과는 바뀌지 않음)
        return _smoke(Path(args[args.index("--smoke") + 1]), Path(args[args.index("--smoke") + 2]),
                      args[args.index("--smoke") + 3] if len(args) > args.index("--smoke") + 3 else "1")
    if "--no-login" not in args:  # 시작할 때 로그인 방식 고르기 (Claude 구독 / API 키)
        from app.qt.login import ask_login

        if not ask_login(None, first_run=True):
            return 0
    win = MainWindow()
    win.resume_last()
    win.show()
    return app.exec()


def _smoke(project_dir: Path, out: Path, pages: str) -> int:
    import json

    from app.qt.region_editor import RegionEditor
    from app.qt.results import ResultViewer

    out.mkdir(parents=True, exist_ok=True)
    res: dict = {}
    win = MainWindow(root=project_dir.parent.parent)
    win.resize(1200, 600)
    win.show()
    win.home.grab().save(str(out / "1_home.png"))
    res["projects"] = win.home.table.rowCount()
    win.open_project(project_dir)
    QApplication.processEvents()
    win.grab().save(str(out / "2_project.png"))
    ov = win.project.ov
    g = ov["groups"][-1]
    groups = [{"name": x["name"], "dir": x["dir"], "files": [f["dir"] for f in x["files"]]} for x in ov["groups"]]
    ed = RegionEditor(Path(g["dir"]), [Path(f["dir"]) for f in g["files"]], groups=groups)
    ed.resize(1400, 850)
    ed.show()
    QApplication.processEvents()
    ed.view.fit()
    QApplication.processEvents()
    ed.grab().save(str(out / "3_regions.png"))
    res["regions"] = len(ed.items())
    ed.dirty = False
    ed.close()
    fd = Path(g["files"][0]["dir"])
    r = service.test_run(fd, pages, out_root=out / "runs")
    res["test_run"] = {"state": r.state, "pages": r.pages, "summary": {k: len(v) for k, v in r.status_summary.items()},
                       "run_dir": str(r.run_dir), "reason": r.reason}
    if r.state == "done":
        v = ResultViewer([("시험", r.run_dir)])
        v.resize(1400, 850)
        v.show()
        QApplication.processEvents()
        v._show_page()
        QApplication.processEvents()
        v.grab().save(str(out / "4_result.png"))
    res["ok"] = r.state == "done" and res["regions"] > 0
    (out / "smoke.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if res["ok"] else 1
