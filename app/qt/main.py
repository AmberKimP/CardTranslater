"""메인 창: 홈(게임·프로젝트 목록) ↔ 프로젝트 화면(그룹·파일·다음 할 일)."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QHeaderView, QLabel, QMainWindow,
    QMenu, QMessageBox, QPushButton, QStackedWidget, QTextBrowser, QTableWidget, QTableWidgetItem, QTreeWidget, QToolButton, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from app import service
from app.qt.dialogs import (
    BANNER_QSS, DONE_BG, EMPTY_QSS, NEXT_QSS, PRIMARY_QSS, AddFilesDialog, ExcludePagesDialog, NewProjectDialog,
    SettingsDialog, card, file_label, fit_to_screen, grey_note, hint_bar, install_korean, make_danger, make_primary, page_title,
    section_title, set_hint, show_error, show_problem, subtitle,
)
from app.qt.region_editor import RegionEditor
from app.qt.results import OutputActions
from app.qt.steps import StepActions

APP_NAME = "CardTranslater"

STEP_QSS = (
    'QPushButton[step="todo"], QPushButton[step="done"], QPushButton[step="now"] {'
    " padding: 6px 8px; border: 1px solid #D0D6DE; border-radius: 8px; background: white; min-height: 20px; }"
    'QPushButton[step="todo"]:hover, QPushButton[step="done"]:hover { border-color: #0b5cad; background: #F3F8FE; }'
    'QPushButton[step="done"] { background: #E7F2E2; border-color: #B5D3A7; color: #2F5E23; }'
    'QPushButton[step="done"]:hover { background: #DCEDD5; }'
    'QPushButton[step="now"] { background: #0b5cad; color: white; border-color: #0b5cad; font-weight: bold; }'
    'QPushButton[step="now"]:hover { background: #0a4f95; }'
    'QPushButton[step="todo"]:disabled, QPushButton[step="done"]:disabled {'
    " color: #A3ACB8; background: #F4F6F9; border: 1px dashed #D9DEE5; }")

# 단계 버튼: (키, 글자, 하는 일, 아직 못 쓸 때 이유)
STEP_INFO = {
    "extract": ("① 글자 추출", "카드 이미지에서 원문 글자를 읽습니다", "먼저 파일을 넣고 영역을 지정하세요"),
    "icons": ("② 아이콘 정의", "원문 속 아이콘에 이름을 붙입니다 (원문 확인에서 아이콘이 이름으로 보이게 먼저)",
              "먼저 ① 글자 추출을 하세요"),
    "review": ("③ 원문 확인", "읽은 원문을 카드 그림과 비교해 고칩니다", "먼저 ① 글자 추출을 하세요"),
    "finish": ("④ 용어집 만들기", "용어 후보를 모아 용어집 엑셀을 만듭니다", "먼저 ① 글자 추출을 하세요"),
    "glossary": ("⑤ 용어집 가져오기", "엑셀에서 고친 용어집을 가져와 확정합니다",
                 "먼저 ④ 용어집 만들기로 용어집 엑셀을 만드세요"),
    "translate": ("⑥ 전체 번역", "확정한 용어집으로 번역합니다",
                  "먼저 ⑤ 용어집을 가져와 확정하세요"),
    "check": ("번역 검수", "번역을 칸마다 원문과 비교해 고치고 확인합니다", "먼저 ⑥ 전체 번역을 하세요"),
    "erase": ("⑦ 글자 지우기", "카드에서 원문 글자를 지웁니다", "먼저 파일을 넣고 영역을 지정하세요"),
    "compose": ("⑧ 한글 넣기", "지운 카드에 번역한 한글을 넣습니다", "먼저 ⑥ 전체 번역과 ⑦ 글자 지우기를 끝내세요"),
    "results": ("결과 보기", "한글을 넣은 카드를 원본과 나란히 봅니다", "아직 한글을 넣은 파일이 없습니다 (⑧ 한글 넣기)"),
    "test": ("시험 실행 (몇 쪽만)", "고른 쪽만 따로 지우고 한글을 넣어 봅니다 (프로젝트 결과는 그대로)",
             "⑥ 전체 번역을 끝낸 뒤 쓸 수 있습니다"),
}
# ‘다음 할 일’ 큰 버튼 글자 (service.project_overview의 next_key)
NEXT_TEXT = {"add": "파일 넣기", "regions": "영역 지정", **{k: v[0] for k, v in STEP_INFO.items()}}


def open_path(p: Path) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))


class HomePage(QWidget):
    """게임 > 프로젝트 목록. 용어집·아이콘 사전·검수 엑셀은 게임에 속하고, 프로젝트는 게임 안의 작업 한 건이다."""

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["게임 / 프로젝트", "상태", "다음 할 일", "게임 자료"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in (1, 2, 3):
            self.tree.header().setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.currentItemChanged.connect(lambda *_a: self._sync_buttons())
        self.tree.itemDoubleClicked.connect(lambda _it, _c: self.open_selected())
        self.tree.setRootIsDecorated(True)
        self.tree.setIndentation(18)
        new = QPushButton("＋  새 프로젝트")
        new.setStyleSheet(PRIMARY_QSS)
        new.setToolTip("고른 게임에 프로젝트를 만듭니다 (게임을 고르지 않았으면 새 게임도 만들 수 있음)")
        new.clicked.connect(self.new_project)
        self.b_open = QPushButton("열기")
        self.b_open.setToolTip("고른 프로젝트를 엽니다 (두 번 눌러도 열림)")
        self.b_open.clicked.connect(self.open_selected)
        folder = QPushButton("게임 폴더 열기")
        folder.setToolTip("용어집·아이콘 검증 엑셀과 번역 검수 엑셀이 있는 게임 폴더")
        folder.clicked.connect(self.open_game_folder)
        ref = QPushButton("새로 고침")
        ref.clicked.connect(self.refresh)
        self.b_delete = QPushButton("프로젝트 삭제…")
        self.b_delete.setToolTip("고른 프로젝트를 휴지통으로 옮깁니다 (확인 창이 뜹니다)")
        self.b_delete.clicked.connect(self.delete_selected)
        make_danger(self.b_delete)
        btns = QHBoxLayout()
        btns.setSpacing(8)
        for b in (self.b_open, folder):
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(ref)
        btns.addWidget(self.b_delete)
        title = page_title("내 프로젝트", 18)
        guide = subtitle("번역할 보드게임 카드를 게임별로 모아 둡니다. 프로젝트를 두 번 누르면 열립니다.")
        head = QHBoxLayout()
        hl = QVBoxLayout()
        hl.setSpacing(2)
        hl.addWidget(title)
        hl.addWidget(guide)
        head.addLayout(hl, 1)
        head.addWidget(new, alignment=Qt.AlignmentFlag.AlignBottom)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 18, 24, 16)
        lay.setSpacing(12)
        self.empty = QLabel("아직 프로젝트가 없습니다.\n\n오른쪽 위 ‘＋ 새 프로젝트’를 눌러 게임 이름과 프로젝트 이름을 정하면\n"
                            "바로 시작할 수 있습니다.")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet(EMPTY_QSS)
        self.body = QStackedWidget()
        self.body.addWidget(self.tree)
        self.body.addWidget(self.empty)
        note = grey_note("용어집·아이콘 사전·검수 엑셀은 게임마다 하나씩 있어, 같은 게임의 프로젝트가 함께 씁니다.")
        lay.addLayout(head)
        lay.addSpacing(4)
        lay.addLayout(btns)
        lay.addWidget(self.body, 1)
        lay.addWidget(note)
        self.projects: list[dict] = []
        self.games: list[dict] = []
        self._sync_buttons()

    def _sync_buttons(self):
        sel = self.selected()
        is_project = bool(sel) and sel[0] == "project"
        self.b_open.setEnabled(is_project)
        self.b_delete.setEnabled(is_project)

    def refresh(self):
        root = self.win.root
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
            gi = QTreeWidgetItem([g["name"], f"프로젝트 {len(ps)}개", "", " · ".join(extra)])
            gi.setFont(0, bold)
            for c in (1, 3):
                gi.setForeground(c, QColor("#5F6B7A"))
            gi.setData(0, Qt.ItemDataRole.UserRole, ("game", g))
            for p in ps:
                try:
                    nxt = service.project_overview(p["dir"]).get("next_step") or ""
                except Exception:  # noqa: BLE001 - 요약을 못 읽어도 목록은 보인다
                    nxt = ""
                pi = QTreeWidgetItem([p["name"], service.STATUS_LABEL.get(p["status"], p["status"]), nxt, ""])
                pi.setData(0, Qt.ItemDataRole.UserRole, ("project", p))
                pi.setForeground(1, QColor("#2F6B1F" if p["status"] == "translated" else "#0b5cad"))
                pi.setForeground(2, QColor("#5F6B7A"))
                pi.setToolTip(0, "두 번 누르면 열립니다")
                gi.addChild(pi)
            self.tree.addTopLevelItem(gi)
            gi.setExpanded(True)
        self.body.setCurrentWidget(self.tree if self.games else self.empty)

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

    def select_dir(self, project_dir: Path) -> bool:
        want = Path(project_dir).resolve()
        for i in range(self.tree.topLevelItemCount()):
            gi = self.tree.topLevelItem(i)
            for j in range(gi.childCount()):
                if Path(gi.child(j).data(0, Qt.ItemDataRole.UserRole)[1]["dir"]).resolve() == want:
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
                f"‘{p['name']}’ 프로젝트를 삭제할까요?\n\n그룹·파일·추출·번역·결과를 모두 휴지통으로 옮깁니다 "
                f"(데이터 폴더의 휴지통(.trash)에 남습니다).\n게임 ‘{p['game']}’의 용어집·아이콘 사전·검수 엑셀은 그대로 둡니다.") \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            dest = service.delete_project(p["dir"])
        except OSError as e:
            show_problem(self, "프로젝트를 삭제하지 못했습니다", str(e),
                         "이 프로젝트의 엑셀·PDF가 열려 있으면 닫고 다시 하세요.")
            return
        self.win.statusBar().showMessage(f"‘{p['name']}’ 프로젝트를 휴지통으로 옮겼습니다")
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
        self.erasing = False  # ⑦ 글자 지우기를 화면에서 돌리는 중 (그동안 뒤에서 지우기는 쉬게)
        from app.qt.bg_erase import BackgroundEraser

        self.bg = BackgroundEraser(self)  # 원문이 확정되면(④부터) 뒤에서 글자 지우기
        self.bg.file_done.connect(self.refresh)
        self.title = page_title("", 17)
        self.sub = subtitle("")
        # 시험 모드 띠 (파일마다 앞 몇 장만)
        self.banner = QFrame()
        self.banner.setObjectName("banner")
        self.banner.setStyleSheet(f"QFrame#banner {{ {BANNER_QSS} }}")
        self.banner_text = QLabel()
        self.banner_off = QPushButton("시험 모드 끄기")
        self.banner_off.setToolTip("다음 작업부터 모든 카드를 합니다. 다시 켜려면 ‘① 글자 추출’ 창에서 켭니다")
        self.banner_off.clicked.connect(self.turn_off_card_limit)
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(12, 6, 8, 6)
        bl.addWidget(self.banner_text, 1)
        bl.addWidget(self.banner_off)
        # 다음 할 일: 큰 버튼 + 설명 두 줄 (무엇을 · 왜/대상)
        self.next_btn = QPushButton()
        self.next_btn.setStyleSheet(NEXT_QSS)
        self.next_btn.clicked.connect(self.do_next)
        self.next = QLabel()
        self.next.setWordWrap(True)
        self.next.setStyleSheet("color: #102A43; font-weight: bold; font-size: 12pt; border: none; background: transparent;")
        self.next_why = QLabel()
        self.next_why.setWordWrap(True)
        self.next_why.setStyleSheet("color: #52606D; border: none; background: transparent;")
        self.next_box = QFrame()
        self.next_box.setObjectName("nextbox")
        self.next_box.setStyleSheet("QFrame#nextbox { background: #EAF2FC; border: 1px solid #C9D9EE; border-radius: 12px; }")
        next_box = QHBoxLayout(self.next_box)
        next_box.setContentsMargins(18, 12, 14, 12)
        next_box.setSpacing(16)
        cap = QLabel("다음 할 일")
        cap.setStyleSheet("color: #0b5cad; font-weight: bold; border: none; background: transparent;")
        why_col = QVBoxLayout()
        why_col.setSpacing(3)
        why_col.addWidget(cap)
        why_col.addWidget(self.next)
        why_col.addWidget(self.next_why)
        next_box.addLayout(why_col, 1)
        next_box.addWidget(self.next_btn, alignment=Qt.AlignmentFlag.AlignVCenter)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["그룹 / 파일", "쪽", "뺀 쪽", "글자 추출", "글자 지움", "한글"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c, w in ((1, 60), (2, 80), (3, 110), (4, 100), (5, 90)):
            self.tree.setColumnWidth(c, w)
        self.tree.header().setStretchLastSection(False)
        self.tree.itemSelectionChanged.connect(self._sync_buttons)
        self.tree.itemDoubleClicked.connect(lambda *_a: self.edit_regions())
        back = QPushButton("← 프로젝트 목록")
        back.setFlat(True)
        back.clicked.connect(self.win.show_home)
        self.b_add = QPushButton("파일 넣기")
        self.b_add.clicked.connect(self.add_files)
        self.b_regions = QPushButton("영역 지정")
        self.b_regions.clicked.connect(self.edit_regions)
        self.b_pages = QPushButton("뺄 쪽")
        self.b_pages.clicked.connect(self.exclude_pages)
        self.b_rename = QPushButton("이름 바꾸기…")
        self.b_rename.setToolTip("고른 그룹의 이름을 바꿉니다 (그룹 안 파일·결과는 그대로)")
        self.b_rename.clicked.connect(self.rename_group)
        self.b_folders = QToolButton()
        self.b_folders.setText("폴더 ▾")
        self.b_folders.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        fmenu = QMenu(self.b_folders)
        self.a_folder = fmenu.addAction("이 그룹·파일 폴더 열기")
        self.a_folder.setToolTip("고른 그룹·파일의 작업 폴더 (안 골랐으면 이 프로젝트 폴더)")
        self.a_folder.triggered.connect(lambda _c=False: self.open_folder())
        self.a_game_folder = fmenu.addAction("게임 폴더 열기 (용어집·검수 엑셀)")
        self.a_game_folder.setToolTip("용어집·아이콘 검증 엑셀과 번역 검수 엑셀이 있는 게임 폴더")
        self.a_game_folder.triggered.connect(lambda _c=False: self.open_game_folder())
        self.b_folders.setMenu(fmenu)
        self.b_refresh = QPushButton("새로 고침")
        self.b_refresh.clicked.connect(self.refresh)
        self.b_delete = QPushButton("삭제…")
        self.b_delete.setToolTip("고른 그룹 또는 파일을 휴지통으로 옮깁니다 (확인 창이 뜹니다)")
        self.b_delete.clicked.connect(self.delete_selected)
        self.b_reset = QPushButton("초기화…")
        self.b_reset.clicked.connect(self.reset_selected)
        make_danger(self.b_delete)
        make_danger(self.b_reset)
        # 맨 위 줄: 이동 | 폴더·새로 고침, 그 아래 제목·부제
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(self.title)
        titles.addWidget(self.sub)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addLayout(titles, 1)
        # 편집 줄 (목록 바로 위): 만들기·고치기 | 되돌리기 어려운 것은 오른쪽 끝
        edit = QHBoxLayout()
        edit.setSpacing(6)
        edit.addWidget(section_title("카드 파일"))
        edit.addSpacing(8)
        for b in (self.b_add, self.b_regions, self.b_pages, self.b_rename):
            edit.addWidget(b)
        edit.addStretch(1)
        edit.addWidget(self.b_reset)
        edit.addWidget(self.b_delete)
        # 2단계 작업: 추출 -> 원문 확인 -> 아이콘 -> 추출 완료(용어집) -> 용어집 가져오기 -> 번역(+검수 엑셀)
        self.steps = StepActions(self)
        self.step_buttons = {}
        steps = QHBoxLayout()
        steps.addWidget(self._row_caption("원문·번역"))
        self.step_fns = {}
        for i, (key, fn) in enumerate((
                ("extract", self.steps.extract),
                ("icons", self.steps.icons),
                ("review", self.steps.review_sources),
                ("finish", self.steps.finish),
                ("glossary", self.steps.import_glossary),
                ("translate", self.steps.translate),
                ("check", self.steps.review_translation))):
            if i:
                steps.addWidget(self._arrow())
            steps.addWidget(self._step_button(key, fn))
        steps.addStretch(1)
        # 3단계: 지우기 -> 한글 넣기 -> 결과 보기, 시험 실행
        self.out = OutputActions(self)
        outs = QHBoxLayout()
        outs.addWidget(self._row_caption("카드 만들기"))
        for i, (key, fn) in enumerate((
                ("erase", self.out.erase),
                ("compose", self.out.compose),
                ("results", self.out.results))):
            if i:
                outs.addWidget(self._arrow())
            outs.addWidget(self._step_button(key, fn))
        outs.addStretch(1)
        outs.addWidget(self._step_button("test", self.out.test_run))
        # 작업 대상: 목록 바로 위. 목록에서 고른 것이 위 단계 버튼의 범위가 된다
        self.scope_label = QLabel("작업 대상: 전체")
        self.scope_label.setStyleSheet("color: #0b5cad; font-weight: bold")
        self.scope_label.setToolTip("아래 목록에서 고른 항목.\n"
                                    "① 글자 추출 · ⑥ 전체 번역 · ⑦ 글자 지우기: 보통 전체에 합니다 "
                                    "(그룹·파일을 고른 채 누르면 전체/고른 항목만을 묻습니다).\n"
                                    "③ 원문 확인: 고른 범위만 보여 줍니다.\n"
                                    "⑧ 한글 넣기 · 결과 보기 · 시험 실행: 그룹마다 합니다 (전체를 고른 채 누르면 그룹을 고릅니다).\n"
                                    "② 아이콘 정의 · ④ 용어집 만들기 · ⑤ 용어집 가져오기: 늘 프로젝트(게임) 전체에 합니다.")
        self.scope_hint = QLabel()
        self.scope_hint.setStyleSheet("color: #5F6B7A")
        scope_row = QHBoxLayout()
        scope_row.addWidget(self.scope_label)
        scope_row.addWidget(self.scope_hint, 1)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.setStyleSheet(STEP_QSS)
        # 파일이 없을 때 표 자리에 보이는 안내
        empty = QWidget()
        empty.setObjectName("empty")
        empty.setStyleSheet(f"QWidget#empty {{ {EMPTY_QSS} }}")
        el = QVBoxLayout(empty)
        el.addStretch(1)
        msg = QLabel("아직 카드 파일이 없습니다.\n카드 PDF나 그림(JPG·PNG)을 넣으면 여기에 파일과 진행 상황이 보입니다.")
        msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg.setStyleSheet("color: #5F6B7A; font-size: 11pt; border: none; background: transparent;")
        el.addWidget(msg)
        add2 = QPushButton("＋  파일 넣기")
        add2.setStyleSheet(PRIMARY_QSS)
        add2.clicked.connect(self.add_files)
        el.addWidget(add2, alignment=Qt.AlignmentFlag.AlignCenter)
        el.addStretch(1)
        self.empty = empty
        self.body = QStackedWidget()
        self.body.addWidget(self.tree)
        self.body.addWidget(empty)
        nav = QHBoxLayout()
        nav.setContentsMargins(0, 0, 0, 0)
        nav.addWidget(back)
        nav.addStretch(1)
        nav.addWidget(self.b_folders)
        nav.addWidget(self.b_refresh)
        # 단계 버튼 두 줄은 흰 카드 하나에, 파일 목록은 다른 카드에
        steps.setSpacing(3)
        outs.setSpacing(3)
        steps_card = card(steps, outs, margins=(10, 10, 10, 10), spacing=8)
        files_card = card(edit, scope_row, self.body, margins=(12, 10, 12, 12), spacing=8)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 4, 12, 10)
        lay.setSpacing(10)
        lay.addLayout(nav)
        lay.addLayout(head)
        lay.addWidget(self.banner)
        lay.addWidget(self.next_box)
        lay.addWidget(steps_card)
        lay.addWidget(files_card, 1)

    @staticmethod
    def _row_caption(text: str) -> QLabel:
        lb = QLabel(text)
        lb.setFixedWidth(70)
        lb.setStyleSheet("color: #334E68; font-weight: bold")
        return lb

    @staticmethod
    def _arrow() -> QLabel:
        lb = QLabel("›")
        lb.setStyleSheet("color: #9AA5B4; font-size: 12pt;")
        return lb

    # ---- 목록 오른쪽 클릭 메뉴
    def _menu_items(self, kind: str) -> list[tuple[str, object] | None]:
        """줄 종류별 메뉴 [(글자, 부를 함수)], None은 구분선."""
        if kind == "file":
            return [("영역 지정", self.edit_regions), ("뺄 쪽…", self.exclude_pages), ("폴더 열기", self.open_folder),
                    None, ("초기화…", self.reset_selected), ("삭제…", self.delete_selected)]
        if kind == "group":
            return [("영역 지정", self.edit_regions), ("이름 바꾸기…", self.rename_group), ("폴더 열기", self.open_folder),
                    None, ("초기화…", self.reset_selected), ("삭제…", self.delete_selected)]
        return [("파일 넣기", self.add_files), ("초기화…", self.reset_selected)]

    def _context_menu(self, pos):
        it = self.tree.itemAt(pos)
        if it is None:
            return
        self.tree.setCurrentItem(it)
        sel = self._selected()
        if not sel:
            return
        menu = QMenu(self)
        for entry in self._menu_items(sel[0]):
            if entry is None:
                menu.addSeparator()
            else:
                text, fn = entry
                menu.addAction(text, lambda f=fn: f())
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _step_button(self, key: str, fn) -> QPushButton:
        b = QPushButton(STEP_INFO[key][0])
        b.setProperty("step", "todo")
        b.clicked.connect(lambda _c=False, f=fn: f())
        self.step_buttons[key] = b
        self.step_fns[key] = fn
        return b

    def load(self, project_dir: Path):
        self.project_dir = Path(project_dir)
        self.refresh()

    def refresh(self):
        if self.project_dir is None:
            return
        self.ov = service.project_overview(self.project_dir)
        o = self.ov
        self.title.setText(o["name"])
        gv = f" · 용어집 {o['glossary_version']}번째 확정본" if o.get("glossary_version") else ""
        self.sub.setText(f"게임: {o['game']} · 상태: {o['status_label']}{gv}")
        lim = o.get("card_limit")
        self.banner.setVisible(bool(lim))
        if lim:
            self.banner_text.setText(f"시험 모드: 파일마다 앞 {lim}장만 작업합니다 "
                                     "(추출·번역·검수 엑셀·지우기·한글 넣기 모두)")
        key = o.get("next_key") or "add"
        self.next_btn.setText(f"▶ {NEXT_TEXT.get(key, key)}")
        self.next.setText(o["next_step"])
        keep = self._selection_key()
        self.tree.blockSignals(True)  # 다시 채우는 동안 선택 바뀜 신호(옛 항목)를 막는다
        self.tree.clear()
        files_all = [f for g in o["groups"] for f in g["files"]]
        allit = QTreeWidgetItem([f"전체  (그룹 {len(o['groups'])}개 · 파일 {len(files_all)}개)"])
        allit.setData(0, Qt.ItemDataRole.UserRole, ("all", {"name": "전체"}))
        bold = QFont(self.tree.font())
        bold.setBold(True)
        allit.setFont(0, bold)
        allit.setToolTip(0, "전체를 고르면 작업·결과 버튼이 프로젝트의 모든 파일에 대해 진행합니다.\n"
                            "그룹이나 파일을 고르면 그것만 합니다.")
        self.tree.addTopLevelItem(allit)
        self.all_item = allit
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
            gi.setFont(0, bold)
            gi.setToolTip(0, "그룹: 같은 형식의 카드 파일 묶음. 두 번 누르면 영역 지정 창이 열립니다")
            if not g["has_regions"]:
                gi.setBackground(0, QColor("#F4B183"))
            allit.addChild(gi)
            for f in g["files"]:
                ex = f"{len(f['excluded'])}쪽 뺌" if f["excluded"] else "—"
                full = f["included"] > 0 and f["extracted"] >= f["included"]
                ext = f"{f['extracted']}/{f['included']}장" + (" (시험)" if lim else "")
                fi = QTreeWidgetItem([f["name"], str(f["pages"]), ex, ext,
                                      "완료" if f["erased"] else "—", "완료" if f["composed"] else "—"])
                fi.setData(0, Qt.ItemDataRole.UserRole, ("file", f))
                fi.setToolTip(3, "읽은 카드 / 작업할 카드")
                fi.setToolTip(4, "완료 = 이 파일 전체를 했습니다")
                fi.setToolTip(5, "완료 = 이 파일 전체를 했습니다")
                if f["excluded"]:
                    tip = "뺀 쪽: " + ", ".join(map(str, f["excluded"]))
                    fi.setToolTip(2, tip + (f"\n이유: {f['exclude_reason']}" if f["exclude_reason"] else ""))
                for col in range(1, 6):
                    fi.setTextAlignment(col, Qt.AlignmentFlag.AlignCenter)
                    if fi.text(col) == "—":
                        fi.setForeground(col, QColor("#A3ACB8"))
                for col, done in ((3, full), (4, f["erased"]), (5, f["composed"])):
                    if done:
                        fi.setBackground(col, QColor(DONE_BG))
                        fi.setForeground(col, QColor("#2F5E23"))
                gi.addChild(fi)
            gi.setExpanded(True)
        allit.setExpanded(True)
        self._restore_selection(keep)
        self.tree.blockSignals(False)
        self.body.setCurrentWidget(self.tree if o["groups"] else self.empty)
        self._sync_buttons()
        self._sync_steps()
        self.bg.kick()  # 지워야 할 파일이 있으면 뒤에서 지운다 (④ 용어집 만들기부터, 원문이 바뀐 카드도)

    def step_states(self) -> dict[str, str]:
        """단계 버튼마다 ‘done’(끝남) · ‘now’(다음에 할 일) · ‘todo’. overview 값으로만 판단한다."""
        o = self.ov
        files = [f for g in o["groups"] for f in g["files"]]
        st = o["status"]
        past_extract = st in ("glossary_review", "glossary_confirmed", "translating", "translated")
        done = {
            "extract": bool(files) and all(f["included"] > 0 and f["extracted"] >= f["included"] for f in files),
            "review": past_extract, "icons": past_extract, "finish": past_extract,
            "glossary": st in ("glossary_confirmed", "translating", "translated"),
            "translate": st == "translated",
            "check": self._review_done(),
            "erase": bool(files) and all(f["erased"] for f in files),
            "compose": bool(files) and all(f["composed"] for f in files),
            "results": False, "test": False,
        }
        nxt = o.get("next_key")
        return {k: "now" if k == nxt else ("done" if done[k] else "todo") for k in self.step_buttons}

    def _review_done(self) -> bool:
        """번역 검수 끝남: 확인할 칸(꼭 고칠·확인해 볼)을 모두 확인함."""
        rv = self.ov.get("review")
        return bool(rv and rv.get("total", 0) > 0 and not rv.get("must_left") and not rv.get("look_left"))

    def do_next(self):
        """‘다음 할 일’ 큰 버튼: next_key에 맞는 단계를 부른다."""
        key = self.ov.get("next_key") or "add"
        if key == "add":
            self.add_files()
        elif key == "regions":
            g = next((g for g in self.ov["groups"] if g["files"] and not g["has_regions"]), None) or next(
                (g for g in self.ov["groups"] if g.get("per_card") and any(f["no_regions"] for f in g["files"])), None)
            if g is not None:
                self._open_editor(g)
            else:
                self.edit_regions()
        elif key in self.step_fns:
            self.step_fns[key]()

    def turn_off_card_limit(self):
        service.set_card_limit(self.project_dir, None)
        self.win.statusBar().showMessage("시험 모드를 껐습니다. 다음 작업부터 모든 카드를 합니다")
        self.refresh()

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
            "check": st in ("translating", "translated") and any(f.get("translated") for f in files),
            "erase": ready,
            # 그룹만 번역·지우기를 끝내도 그 그룹은 한글을 넣을 수 있다
            "compose": st in ("translating", "translated") and any(f["erased"] and f.get("translated") for f in files),
            "results": any(f["composed"] for f in files),
            "test": ready and st in ("translating", "translated") and any(f.get("translated") for f in files),
        }
        states = self.step_states()
        for k, b in self.step_buttons.items():
            b.setEnabled(on[k])
            text, what, why = STEP_INFO[k]
            stt = states[k] if on[k] or states[k] == "done" else "todo"
            b.setText(text + (" ✓" if stt == "done" else ""))
            if not on[k]:
                b.setToolTip(f"{what}\n지금은 쓸 수 없음: {why}")
            elif k == "check" and o.get("review"):
                rv = o["review"]
                b.setToolTip(f"{what}\n확인 {rv['checked']}/{rv['total']}칸 · 꼭 고칠 칸 {rv['must_left']} · "
                             f"확인해 볼 칸 {rv['look_left']}")
            elif stt == "done":
                b.setToolTip(f"{what}\n끝난 단계입니다 (다시 할 수도 있습니다)")
            else:
                b.setToolTip(what)
            if b.property("step") != stt:
                b.setProperty("step", stt)
                b.style().unpolish(b)
                b.style().polish(b)

    def _selected(self):
        it = self.tree.currentItem()
        return it.data(0, Qt.ItemDataRole.UserRole) if it else None

    def _selection_key(self):
        sel = self._selected()
        if not sel:
            return ("all", None)
        kind, d = sel
        return (kind, None if kind == "all" else str(d["dir"]))

    def _restore_selection(self, key):
        """새로 고친 뒤에도 고른 항목을 그대로 (없어졌으면 ‘전체’)."""
        kind, ident = key or ("all", None)
        target = self.all_item
        if kind != "all":
            stack = [self.all_item]
            while stack:
                it = stack.pop()
                d = it.data(0, Qt.ItemDataRole.UserRole)
                if d and d[0] == kind and str(d[1].get("dir")) == ident:
                    target = it
                    break
                stack += [it.child(i) for i in range(it.childCount())]
        self.tree.setCurrentItem(target)

    def select_group(self, g: dict):
        """그리드에서 그 그룹을 고른다 (한글 넣기·결과 보기·시험 실행에서 고른 그룹을 목록에도 보이게)."""
        stack = [self.all_item] if getattr(self, "all_item", None) is not None else []
        while stack:
            it = stack.pop()
            d = it.data(0, Qt.ItemDataRole.UserRole)
            if d and d[0] == "group" and str(d[1].get("dir")) == str(g["dir"]):
                self.tree.setCurrentItem(it)
                return
            stack += [it.child(i) for i in range(it.childCount())]

    def select_all(self):
        """그리드에서 ‘전체’를 고른다."""
        if getattr(self, "all_item", None) is not None:
            self.tree.setCurrentItem(self.all_item)

    def scope(self) -> tuple[str, list | None]:
        """작업·결과 버튼이 할 범위: (설명, 파일 폴더 목록 | None=전체)."""
        sel = self._selected()
        if not sel or sel[0] == "all":
            return "전체", None
        kind, d = sel
        if kind == "group":
            return f"그룹 ‘{d['name']}’", [f["dir"] for f in d["files"]]
        return f"파일 ‘{d['key']}’", [d["dir"]]

    def scoped_files(self) -> list[dict]:
        """범위 안 파일(overview의 파일 dict)."""
        _label, dirs = self.scope()
        files = [f for g in self.ov["groups"] for f in g["files"]]
        if dirs is None:
            return files
        want = {str(Path(x)) for x in dirs}
        return [f for f in files if str(Path(f["dir"])) in want]

    def _group_of_selection(self) -> dict | None:
        sel = self._selected()
        if not sel or sel[0] == "all":
            return self.ov["groups"][0] if len(self.ov.get("groups", [])) == 1 else None
        kind, d = sel
        if kind == "group":
            return d
        # 목록을 다시 채우는 사이에는 옛 항목이 잠깐 남을 수 있다: 못 찾으면 None
        return next((g for g in self.ov["groups"] if any(str(f["dir"]) == str(d["dir"]) for f in g["files"])), None)

    def _sync_next_why(self):
        """‘다음 할 일’ 상자 둘째 줄: 그 단계가 하는 일과 대상."""
        if not hasattr(self, "next_why"):
            return
        key = self.ov.get("next_key") or "add"
        if key == "add":
            why = "카드 PDF나 그림(JPG·PNG)을 프로젝트에 넣습니다"
        elif key == "regions":
            why = "카드에서 번역할 글자가 있는 곳을 영역으로 지정합니다"
        elif key == "results":
            why = "모든 카드에 한글을 넣었습니다. 번역을 고치려면 검수 엑셀을 고친 뒤 ‘⑧ 한글 넣기’를 다시 누르세요."
        elif key in STEP_INFO:
            why = STEP_INFO[key][1] + " · 대상: " + self.scope()[0]
        else:
            why = ""
        self.next_why.setText(why)

    def _sync_buttons(self):
        sel = self._selected()
        self.b_delete.setEnabled(bool(sel) and sel[0] != "all")
        if hasattr(self, "b_rename"):
            self.b_rename.setEnabled(bool(sel) and sel[0] == "group")
        self._sync_next_why()
        if hasattr(self, "b_reset"):
            self.b_reset.setEnabled(bool(self.ov.get("groups")))
            label, _d = self.scope()
            self.b_reset.setToolTip(f"{label}의 작업 결과(추출·지운 카드·한글 카드)를 비우고 다시 할 수 있게 합니다.\n"
                                    "파일·영역·뺄 쪽 설정은 그대로입니다 (‘삭제’는 그룹·파일 자체를 뺍니다).")
        if hasattr(self, "scope_label"):
            label = self.scope()[0]
            self.scope_label.setText(f"작업 대상: {label}")
            self.scope_hint.setText(" — 추출·번역·지우기는 전체에, ⑧ 한글 넣기부터는 그룹마다 합니다 (목록에서 그룹을 고르면 그 그룹만)"
                                    if label == "전체"
                                    else " — ⑧ 한글 넣기·결과 보기는 이 범위에, 추출·번역·지우기는 누를 때 전체/이것만을 묻습니다")
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

    def reset_selected(self):
        """초기화: 고른 범위(전체·그룹·파일)의 작업 결과를 비운다. 파일·영역은 남긴다 (삭제와 다름)."""
        from app.qt.dialogs import ask_choice, show_failure

        label, dirs = self.scope()
        r = ask_choice(self, "초기화", f"{label}의 작업 결과를 비울까요?",
                       [("지운 카드·한글 카드만", "accept"), ("글자 추출부터 모두", "destructive"), ("취소", "reject")],
                       default=0,
                       informative="파일·영역·뺄 쪽 설정은 그대로 두고 결과만 휴지통(.trash)으로 옮깁니다.\n"
                                   "‘삭제’는 다릅니다: 그룹·파일 자체를 프로젝트에서 뺍니다.\n"
                                   "‘글자 추출부터 모두’는 읽은 원문도 비우므로 ① 글자 추출부터 다시 해야 합니다.")
        if r not in (0, 1):
            return
        try:
            res = service.reset_files(self.project_dir, dirs, "output" if r == 0 else "all")
        except OSError as e:
            show_failure(self, "초기화하지 못했습니다", str(e))
            return
        self.win.statusBar().showMessage(f"{label}: 결과 {len(res['moved'])}개를 휴지통으로 옮겼습니다")
        self.refresh()

    def rename_group(self):
        """고른 그룹의 이름을 바꾼다."""
        from PySide6.QtWidgets import QInputDialog, QLineEdit

        sel = self._selected()
        if not sel or sel[0] != "group":
            QMessageBox.information(self, "그룹 고르기", "이름을 바꿀 그룹을 목록에서 고르세요")
            return
        d = sel[1]
        new, ok = QInputDialog.getText(self, "그룹 이름 바꾸기", f"‘{d['name']}’ 그룹의 새 이름:", QLineEdit.EchoMode.Normal,
                                       d["name"])
        new = (new or "").strip()
        if not ok or not new or new == d["name"]:
            return
        try:
            gdir = service.rename_group(self.project_dir, d["name"], new)
        except (OSError, KeyError, ValueError) as e:
            show_problem(self, "그룹 이름을 바꾸지 못했습니다", str(e),
                         "이 그룹의 엑셀·PDF가 열려 있으면 닫고 다시 하세요." if isinstance(e, OSError) else "다른 이름을 쓰세요.")
            return
        self.win.statusBar().showMessage(f"그룹 이름을 ‘{d['name']}’에서 ‘{new}’(으)로 바꿨습니다")
        self.refresh()
        self._restore_selection(("group", str(gdir)))

    def delete_selected(self):
        sel = self._selected()
        if not sel:
            return
        kind, d = sel
        if kind == "group":
            what = (f"‘{d['name']}’ 그룹을 삭제할까요?\n\n그룹의 영역과 그룹 안 파일 {len(d['files'])}개의 "
                    "추출·지운 카드·한글 카드를 모두 휴지통으로 옮깁니다 (데이터 폴더의 휴지통(.trash)에 남습니다).")
        else:
            what = (f"‘{d['key']}’ 파일을 그룹에서 삭제할까요?\n\n이 파일의 원본 사본·추출·지운 카드·한글 카드를 "
                    "휴지통으로 옮깁니다 (데이터 폴더의 휴지통(.trash)에 남습니다). 처음에 고른 원래 PDF는 그대로입니다.")
        if QMessageBox.question(self, "삭제", what) != QMessageBox.StandardButton.Yes:
            return
        try:
            dest = service.delete_group(self.project_dir, d["name"]) if kind == "group" else service.delete_file(d["dir"])
        except (OSError, KeyError, ValueError) as e:
            show_problem(self, "삭제하지 못했습니다", str(e), "이 파일의 엑셀·PDF가 열려 있으면 닫고 다시 하세요.")
            return
        self.win.statusBar().showMessage(f"‘{d.get('key') or d['name']}’을(를) 휴지통으로 옮겼습니다")
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
            open_path(Path(sel[1]["dir"]).resolve())
        elif self.project_dir:
            open_path(Path(self.project_dir).resolve())

    def open_game_folder(self):
        if self.project_dir:
            d = service.game_dir(self.project_dir)
            d.mkdir(parents=True, exist_ok=True)
            open_path(d.resolve())


class ReleaseNotesDialog(QDialog):
    """도움말 > 릴리스 노트: 버전마다 새 기능·바뀐 점·고친 점 (CHANGELOG.md)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"릴리스 노트 — {APP_NAME} {service.VERSION}")
        fit_to_screen(self, 900, 760, maximize_below=0)
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        text = service.release_notes()  # 개발용 안내는 service가 빼고 준다
        if text:
            self.view.setMarkdown(text)
        else:
            self.view.setPlainText("릴리스 노트 파일을 찾지 못했습니다.")
        self.view.setStyleSheet("QTextBrowser { padding: 10px 14px; }")
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(page_title(f"릴리스 노트 · 지금 버전 {service.VERSION}", 14))
        lay.addWidget(self.view, 1)
        lay.addWidget(bb)


class MainWindow(QMainWindow):
    def __init__(self, root: Path | None = None):
        super().__init__()
        self.root = Path(root) if root else service.data_dir()
        self.setWindowTitle(f"{APP_NAME} {service.VERSION}")
        self.resize(1100, 720)
        self.stack = QStackedWidget()
        self.home = HomePage(self)
        self.project = ProjectPage(self)
        self.bg_label = QLabel()  # 상태 줄 오른쪽: 뒤에서 글자 지우기 진행
        self.bg_label.setStyleSheet("color: #5F6B7A; padding-right: 8px")
        self.statusBar().addPermanentWidget(self.bg_label)
        self.project.bg.changed.connect(self.bg_label.setText)
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
        help_ = self.menuBar().addMenu("도움말")
        act = QAction("로그 묶어서 저장… (문제 보고용)", self)
        act.triggered.connect(self.save_logs)
        help_.addAction(act)
        act = QAction("로그 폴더 열기", self)
        act.triggered.connect(self.open_log_folder)
        help_.addAction(act)
        help_.addSeparator()
        act = QAction("릴리스 노트…", self)
        act.triggered.connect(self.show_release_notes)
        help_.addAction(act)
        st = service.engine_status()
        self.statusBar().showMessage(st["message"])
        self.show_home()
        self._select_last()

    def _select_last(self):
        """홈에서 마지막에 연 프로젝트를 골라 둔다."""
        try:
            last = service.last_project()
            if last and self.root.resolve() in last.resolve().parents:
                self.home.select_dir(last)
        except Exception:  # noqa: BLE001
            pass

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

    def save_logs(self):
        import time

        from PySide6.QtWidgets import QFileDialog, QMessageBox

        name = f"CardTranslater-로그-{time.strftime('%Y%m%d-%H%M')}.zip"
        default = str(Path.home() / "Desktop" / name)
        path, _ = QFileDialog.getSaveFileName(self, "로그 묶어서 저장", default, "ZIP 파일 (*.zip)")
        if not path:
            return
        try:
            out = service.export_logs(Path(path))
        except OSError as e:
            show_error(self, "로그를 저장하지 못했습니다", str(e), "다른 폴더를 골라 다시 저장하세요.")
            return
        QMessageBox.information(self, "로그 저장", f"저장했습니다:\n{out}\n\n이 파일을 보내 주세요. "
                                "(API 키는 가려져 있습니다. 카드 이미지·원문은 들어 있지 않습니다)")

    def open_log_folder(self):
        from app.qt.dialogs import open_log_folder

        open_log_folder(self)

    def show_release_notes(self):
        ReleaseNotesDialog(self).exec()

    def closeEvent(self, ev):
        self.project.bg.stop()  # 지우던 파일은 저장하지 않고 다음에 이어 한다
        super().closeEvent(ev)

    def open_settings(self):
        if SettingsDialog(self).exec():
            self.root = service.data_dir()
            self.show_home()


def main(argv: list[str] | None = None) -> int:
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    if sys.platform == "win32":  # 한글 화면 글꼴 (화면 없이 띄우는 점검 모드 포함)
        app.setFont(QFont("Malgun Gothic", 10))
    from app.qt import theme

    theme.apply(app)  # 앱 전체 모양 (둥근 칸·버튼 구분·여백)
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
    _start_log()
    if "--no-login" not in args:  # 시작할 때 로그인 방식 고르기 (Claude 구독 / API 키)
        from app.qt.login import ask_login

        if not ask_login(None, first_run=True):
            return 0
    win = MainWindow()
    win.resume_last()
    win.show()
    return app.exec()


def _start_log() -> None:
    """동작 로그 시작 (도움말 > 로그 묶어서 저장). 로그를 못 열어도 프로그램은 계속 쓴다."""
    try:
        service.start_logging()
    except Exception:  # noqa: BLE001
        pass


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
    res["projects"] = win.home.tree.topLevelItemCount()
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
