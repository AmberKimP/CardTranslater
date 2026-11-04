"""새 프로젝트·파일 넣기·뺄 쪽·설정 대화상자."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QRadioButton, QVBoxLayout,
)

from app import service

LANGS = [("ja", "일본어"), ("en", "영어")]


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
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("체크한 쪽은 추출·용어집·번역·지우기·한글 넣기에서 모두 빠집니다"))
        lay.addWidget(self.list, 1)
        lay.addWidget(self.reason)
        lay.addWidget(bb)
        self.resize(900, 640)

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
        self.engine = QComboBox()
        self.engine.addItem("Claude Code (요금제)", "claude-code")
        self.engine.addItem("API 키", "api")
        self.engine.setCurrentIndex(max(0, self.engine.findData(s.engine)))
        st = service.engine_status()
        info = QLabel(st["message"] + ("" if st["api"] else "\nAPI 키: 없음 (.env의 ANTHROPIC_API_KEY)"))
        info.setStyleSheet("color: #666")
        form = QFormLayout()
        form.addRow("데이터 폴더 (games·projects)", row)
        form.addRow("기본 엔진", self.engine)
        form.addRow("", info)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
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
        service.save_settings(d, self.engine.currentData())
        super().accept()
