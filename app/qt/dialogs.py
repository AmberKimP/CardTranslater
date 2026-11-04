"""새 프로젝트·파일 넣기·뺄 쪽·설정 대화상자."""

from __future__ import annotations

from pathlib import Path

import sys

from PySide6.QtCore import QLibraryInfo, QSize, Qt, QTranslator
from PySide6.QtGui import QGuiApplication, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QRadioButton, QVBoxLayout,
)

from app import service

LANGS = [("ja", "일본어"), ("en", "영어")]


def install_korean(app=None) -> bool:
    """Qt 표준 버튼(취소·예·아니요 등)을 한국어로. 번역 파일을 찾아 불렀으면 True."""
    app = app or QApplication.instance()
    if app is None:
        return False
    tr = QTranslator(app)
    dirs = [QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)]
    try:
        import PySide6

        dirs.append(str(Path(PySide6.__file__).parent / "translations"))
    except Exception:  # noqa: BLE001
        pass
    if getattr(sys, "_MEIPASS", None):
        dirs.append(str(Path(sys._MEIPASS) / "PySide6" / "translations"))
    for d in dirs:
        if tr.load("qtbase_ko", d):
            app.installTranslator(tr)
            app._ko_translator = tr  # 지워지지 않게 붙들어 둔다
            return True
    return False


def set_ok_text(bb: QDialogButtonBox, text: str) -> None:
    """확인 버튼을 ‘확인’ 대신 하는 일 이름(만들기·넣기·저장)으로."""
    bb.button(QDialogButtonBox.StandardButton.Ok).setText(text)


def fit_to_screen(w, width: int, height: int, margin: float = 0.92, maximize_below: int = 800) -> None:
    """원하는 크기로 열되 화면(작업 표시줄 제외)보다 크지 않게 줄인다. 화면 높이가 낮으면 최대화로 연다."""
    scr = None
    try:
        scr = (w.parentWidget() or w).screen()
    except Exception:  # noqa: BLE001
        pass
    scr = scr or QGuiApplication.primaryScreen()
    if scr is None:
        w.resize(width, height)
        return
    g = scr.availableGeometry()
    w.resize(min(width, int(g.width() * margin)), min(height, int(g.height() * margin)))
    if maximize_below and g.height() < maximize_below:
        w.setWindowState(w.windowState() | Qt.WindowState.WindowMaximized)


def ask_choice(parent, title: str, text: str, buttons: list[tuple[str, str]], default: int | None = None,
               informative: str = "") -> int | None:
    """동작 이름 버튼으로 묻는다. buttons: [(문구, 'accept'|'destructive'|'reject')]. 누른 버튼 번호, 닫으면 None."""
    roles = {"accept": QMessageBox.ButtonRole.AcceptRole, "destructive": QMessageBox.ButtonRole.DestructiveRole,
             "reject": QMessageBox.ButtonRole.RejectRole}
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Icon.Question)
    box.setText(text)
    if informative:
        box.setInformativeText(informative)
    made = [box.addButton(t, roles[r]) for t, r in buttons]
    if default is not None:
        box.setDefaultButton(made[default])
    for b, (_t, r) in zip(made, buttons):
        if r == "reject":
            box.setEscapeButton(b)
    box.exec()
    clicked = box.clickedButton()
    return made.index(clicked) if clicked in made else None


class NewProjectDialog(QDialog):
    """새 게임 이름을 넣거나 기존 게임을 골라 프로젝트를 만든다 (PRD 0단계)."""

    def __init__(self, root: Path, parent=None):
        super().__init__(parent)
        self.root = Path(root)
        self.created = None
        self.setWindowTitle("새 프로젝트")
        self.name = QLineEdit()
        self.game = QComboBox()
        self.game.setEditable(True)
        self.games = [g["name"] for g in service.list_games(self.root)]
        self.game.addItems(self.games)
        self.game.setCurrentText("")
        self.game.lineEdit().setPlaceholderText("기존 게임을 고르거나 새 게임 이름을 입력")
        self.lang = QComboBox()
        for k, t in LANGS:
            self.lang.addItem(t, k)
        self.hint = QLabel()
        self.hint.setStyleSheet("color: #666")
        self.game.currentTextChanged.connect(self._hint)
        form = QFormLayout()
        form.addRow("프로젝트 이름", self.name)
        form.addRow("게임", self.game)
        form.addRow("", self.hint)
        form.addRow("원문 언어", self.lang)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        set_ok_text(bb, "만들기")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(bb)
        self._hint(self.game.currentText())

    def _hint(self, text: str):
        t = text.strip()
        if not t:
            self.hint.setText("")
        elif t in self.games:
            self.hint.setText("기존 게임: 확정된 용어집과 아이콘 사전을 그대로 씁니다")
        else:
            self.hint.setText("새 게임을 만듭니다")

    def accept(self):
        name, game = self.name.text().strip(), self.game.currentText().strip()
        if not name or not game:
            QMessageBox.warning(self, "입력 필요", "프로젝트 이름과 게임을 넣으세요")
            return
        try:
            info, _new = service.new_project(name, game, self.lang.currentData(), root=self.root)
        except FileExistsError:
            QMessageBox.warning(self, "이미 있음", f"같은 이름의 프로젝트가 있습니다: {name}")
            return
        except ValueError as e:
            QMessageBox.warning(self, "쓸 수 없는 이름", str(e))
            return
        self.created = info.dir
        super().accept()


class AddFilesDialog(QDialog):
    """카드 PDF를 기존 그룹에 넣거나 새 그룹을 만든다 (PRD 1단계)."""

    def __init__(self, project_dir: Path, parent=None, files: list[str] | None = None):
        super().__init__(parent)
        self.project_dir = Path(project_dir)
        self.result_info = None
        self.setWindowTitle("파일 넣기")
        self.groups = service.list_groups(self.project_dir)
        self.files = QListWidget()
        for f in files or []:
            self.files.addItem(f)
        pick = QPushButton("PDF 고르기…")
        pick.clicked.connect(self._pick)
        self.r_exist = QRadioButton("기존 그룹에 넣기")
        self.r_new = QRadioButton("새 그룹 만들기")
        bg = QButtonGroup(self)
        bg.addButton(self.r_exist)
        bg.addButton(self.r_new)
        self.group_combo = QComboBox()
        for g in self.groups:
            self.group_combo.addItem(f"{g['name']}  (파일 {len(g['files'])}개, 영역 {g['regions']}개)", g["name"])
        self.new_name = QLineEdit()
        self.new_name.setPlaceholderText("예: 아이템 카드")
        self.copy_from = QComboBox()
        self.copy_from.addItem("(복사하지 않음: 영역을 새로 그림)", "")
        for g in self.groups:
            if g["regions"]:
                self.copy_from.addItem(f"{g['name']}의 영역 복사", str(Path(g["dir"]) / "regions.json"))
        if self.groups:
            self.r_exist.setChecked(True)
        else:
            self.r_new.setChecked(True)
            self.r_exist.setEnabled(False)
        self.r_exist.toggled.connect(self._sync)
        self._sync()
        form = QFormLayout()
        form.addRow(self.r_exist, self.group_combo)
        form.addRow(self.r_new, self.new_name)
        form.addRow("새 그룹 영역", self.copy_from)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        set_ok_text(bb, "넣기")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("카드 PDF (같은 카드 형식끼리 한 그룹)"))
        lay.addWidget(self.files)
        lay.addWidget(pick)
        lay.addLayout(form)
        lay.addWidget(bb)
        self.resize(560, 420)

    def _sync(self):
        ex = self.r_exist.isChecked()
        self.group_combo.setEnabled(ex)
        self.new_name.setEnabled(not ex)
        self.copy_from.setEnabled(not ex)

    def _pick(self):
        fs, _ = QFileDialog.getOpenFileNames(self, "카드 PDF 고르기", "", "PDF (*.pdf)")
        for f in fs:
            if not self.files.findItems(f, Qt.MatchFlag.MatchExactly):
                self.files.addItem(f)

    def accept(self):
        files = [self.files.item(i).text() for i in range(self.files.count())]
        if not files:
            QMessageBox.warning(self, "파일 없음", "PDF를 고르세요")
            return
        new = self.r_new.isChecked()
        group = self.new_name.text().strip() if new else self.group_combo.currentData()
        if not group:
            QMessageBox.warning(self, "그룹 이름", "새 그룹 이름을 넣으세요")
            return
        try:
            self.result_info = service.add_files(self.project_dir, files, group, new_group=new,
                                                 regions_from=(self.copy_from.currentData() or None) if new else None)
        except (FileExistsError, KeyError, ValueError) as e:
            QMessageBox.warning(self, "넣을 수 없음", str(e).strip("'"))
            return
        if self.result_info["skipped"]:
            QMessageBox.information(self, "건너뛴 파일", "\n".join(self.result_info["skipped"]))
        super().accept()


class ExcludePagesDialog(QDialog):
    """파일에서 그룹 형식과 다른 쪽(예: 규칙 카드)을 뺀다."""

    def __init__(self, file_dir: Path, excluded: list[int], reason: str = "", parent=None):
        super().__init__(parent)
        self.file_dir = Path(file_dir)
        self.setWindowTitle(f"뺄 쪽 — {self.file_dir.name}")
        self.list = QListWidget()
        self.list.setViewMode(QListWidget.ViewMode.IconMode)
        self.list.setIconSize(QSize(150, 150))
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.setSpacing(6)
        for p in range(1, service.page_count(self.file_dir) + 1):
            png, _ = service.page_image(self.file_dir, p, dpi=36)
            pm = QPixmap()
            pm.loadFromData(png, "PNG")
            it = QListWidgetItem(QIcon(pm), f"{p}쪽")
            it.setData(Qt.ItemDataRole.UserRole, p)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if p in excluded else Qt.CheckState.Unchecked)
            self.list.addItem(it)
        self.reason = QLineEdit(reason)
        self.reason.setPlaceholderText("뺀 이유 (예: 규칙 카드라 형식이 다름)")
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        set_ok_text(bb, "저장")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("체크한 쪽은 추출·용어집·번역·지우기·한글 넣기에서 모두 빠집니다"))
        lay.addWidget(self.list, 1)
        lay.addWidget(self.reason)
        lay.addWidget(bb)
        fit_to_screen(self, 900, 640, maximize_below=0)

    def checked(self) -> list[int]:
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked]

    def accept(self):
        service.set_pages(self.file_dir, self.checked(), self.reason.text().strip())
        super().accept()


class SettingsDialog(QDialog):
    """데이터 폴더와 기본 엔진."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("설정")
        s = service.get_settings()
        self.dir = QLineEdit(str(service.data_dir()))
        browse = QPushButton("바꾸기…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.dir)
        row.addWidget(browse)
        self.engine = s.engine
        info = QLabel(service.engine_status()["message"] + "\n(Claude 구독 / API 키는 파일 > 로그인 방식에서 바꿉니다)")
        info.setStyleSheet("color: #666")
        form = QFormLayout()
        form.addRow("데이터 폴더 (games·projects)", row)
        form.addRow("로그인 방식", info)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        set_ok_text(bb, "저장")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(bb)
        self.resize(640, 160)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "데이터 폴더", self.dir.text())
        if d:
            self.dir.setText(d)

    def accept(self):
        d = Path(self.dir.text().strip())
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, "폴더를 만들 수 없음", str(e))
            return
        service.save_settings(d, self.engine)
        super().accept()
