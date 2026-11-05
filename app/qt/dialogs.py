"""새 프로젝트·파일 넣기·뺄 쪽·설정 대화상자."""

from __future__ import annotations

from pathlib import Path

import sys

from PySide6.QtCore import QLibraryInfo, QSize, Qt, QTranslator
from PySide6.QtGui import QGuiApplication, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QSpinBox,
    QInputDialog, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QRadioButton, QVBoxLayout,
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


# ---------------------------------------------------------------- 오류 창: 왜 → 무엇을 하면 되는지
UNKNOWN_TODO = "다시 해 보고, 계속되면 ‘자세히 보기’ 내용을 알려 주세요."

# (찾을 낱말들, 왜, 무엇을 하면 되는지). 위에서부터 먼저 맞는 것을 쓴다.
_CAUSES = [
    (("PermissionError", "Permission denied", "Errno 13", "WinError 32", "WinError 5", "다른 프로세스",
      "being used by another process", "액세스가 거부"),
     "파일이 다른 프로그램(엑셀·PDF 보기 등)에서 열려 있어 쓸 수 없습니다.",
     "이 프로젝트의 엑셀·PDF 파일을 닫고 다시 하세요."),
    (("NotAvailable", "로그인되어 있지 않", "인증 오류", "claude 명령", "API 키", "api key", "authentication",
      "invalid x-api-key", "error code: 401"),
     "Claude에 연결하지 못했습니다 (로그인이 풀렸거나, Claude 프로그램이나 API 키를 찾지 못함).",
     "‘파일 > 로그인 방식’에서 로그인을 확인한 뒤 다시 하세요."),
    (("사용 한도", "한도", "rate limit", "rate_limit", "usage limit", "error code: 429", "overloaded", "error code: 529"),
     "Claude 사용 한도에 걸렸습니다.",
     "잠시 뒤 다시 누르면 이미 끝낸 곳 다음부터 이어서 합니다."),
    (("ConnectionError", "connection error", "timed out", "Timeout", "연결이 끊", "연결할 수 없"),
     "인터넷 연결이 끊겼거나 응답이 너무 늦습니다.",
     "연결을 확인하고 다시 하세요. 이미 끝낸 부분은 다시 하지 않습니다."),
    (("FileNotFoundError", "No such file", "찾을 수 없", "찾지 못했"),
     "필요한 파일을 찾지 못했습니다 (옮겼거나 지웠을 수 있음).",
     "프로젝트 화면에서 ‘새로 고침’을 누른 뒤 다시 하세요. 계속되면 ‘파일 넣기’로 파일을 다시 넣으세요."),
    (("영역 없음", "영역이 없", "영역을 지정"),
     "영역을 지정하지 않은 그룹이나 카드가 있습니다.",
     "‘영역 지정’에서 영역을 그린 뒤 다시 하세요."),
]


def explain_error(text: str) -> tuple[str, str] | None:
    """흔한 오류 원인을 쉬운 말로: (왜, 무엇을 하면 되는지). 모르는 오류면 None."""
    t = text or ""
    low = t.lower()
    for keys, why, todo in _CAUSES:
        if any(k.lower() in low for k in keys):
            return why, todo
    return None


def show_error(parent, title: str, why: str, todo: str = "", detail: str | None = None,
               warn: bool = True) -> QMessageBox:
    """오류·경고 창. 제목=무엇이 안 됐는지, 본문=왜, 아래=무엇을 하면 되는지, 원문 메시지는 ‘자세히 보기’에."""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Icon.Warning if warn else QMessageBox.Icon.Critical)
    box.setText(why)
    if todo:
        box.setInformativeText(todo)
    if detail and detail.strip() and detail.strip() != why.strip():
        box.setDetailedText(detail.strip()[:6000])
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.exec()
    return box


def _first_line(text: str, limit: int = 200) -> str:
    line = next((x.strip() for x in (text or "").splitlines() if x.strip()), "")
    return line if len(line) <= limit else line[:limit] + "…"


def show_failure(parent, title: str, message: str) -> QMessageBox:
    """작업 중 예외가 난 경우 (worker 실패 콜백). 원인을 알아보면 쉬운 안내, 아니면 ‘자세히 보기’를 알려 달라고."""
    known = explain_error(message)
    if known:
        return show_error(parent, title, known[0], known[1], message)
    return show_error(parent, title, "예상하지 못한 문제가 생겼습니다.", UNKNOWN_TODO, message, warn=False)


def show_problem(parent, title: str, reason: str, todo: str = "", retry_after: str | None = None,
                 detail: str | None = None) -> QMessageBox:
    """엔진이 ‘할 수 없음·멈춤·실패’ 상태와 이유를 돌려준 경우. reason은 엔진이 쓴 한국어 이유."""
    full = "\n\n".join(x for x in ((reason or "").strip(), (detail or "").strip()) if x)
    if retry_after:
        return show_error(parent, title, "Claude 사용 한도에 걸려 멈췄습니다.",
                          f"{retry_after} 이후 다시 누르면 멈춘 곳부터 이어서 합니다.", full)
    known = explain_error(reason or "")
    if known:
        return show_error(parent, title, known[0], todo or known[1], full)
    why = _first_line(reason) or "작업을 마치지 못했습니다."
    more = len([x for x in (reason or "").splitlines() if x.strip()]) - 1
    if more > 0:
        why += f"\n(외 {more}건 — ‘자세히 보기’에 모두 있습니다)"
    return show_error(parent, title, why, todo or UNKNOWN_TODO, full)


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

    def __init__(self, root: Path, parent=None, game: str | None = None):
        super().__init__(parent)
        self.root = Path(root)
        self.created = None
        self.setWindowTitle("새 프로젝트")
        self.name = QLineEdit()
        self.game = QComboBox()
        self.game.setEditable(True)
        self.games = [g["name"] for g in service.list_games(self.root)]
        self.game.addItems(self.games)
        self.game.setCurrentText(game or "")
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
            show_error(self, "프로젝트를 만들 수 없습니다", "프로젝트 이름이나 게임 이름이 비어 있습니다.",
                       "두 칸을 모두 채운 뒤 ‘만들기’를 누르세요.")
            return
        try:
            info, _new = service.new_project(name, game, self.lang.currentData(), root=self.root)
        except FileExistsError:
            show_error(self, "프로젝트를 만들 수 없습니다", f"같은 이름의 프로젝트(‘{name}’)가 이미 있습니다.",
                       "다른 이름을 넣으세요.")
            return
        except ValueError as e:
            show_problem(self, "프로젝트를 만들 수 없습니다", str(e), "이름에서 쓸 수 없는 글자를 빼고 다시 넣으세요.")
            return
        self.created = info.dir
        super().accept()


IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff")


class AddFilesDialog(QDialog):
    """카드 파일(PDF 또는 이미지)을 기존 그룹에 넣거나 새 그룹을 만든다 (PRD 1단계).

    이미지(JPG·PNG 등)는 카드 한 장이 한 쪽인 PDF 하나로 묶어 넣는다. 이미지 한 장에 카드가
    가로 X × 세로 Y로 놓여 있으면 ‘여러 장 배열’을 고르고, 미리 보기로 자를 자리를 확인한다."""

    def __init__(self, project_dir: Path, parent=None, files: list[str] | None = None):
        super().__init__(parent)
        self.project_dir = Path(project_dir)
        self.result_info = None
        self.setWindowTitle("파일 넣기")
        self.groups = service.list_groups(self.project_dir)
        self.files = QListWidget()
        self.files.setTextElideMode(Qt.TextElideMode.ElideMiddle)  # 긴 경로는 가운데를 줄여 파일 이름이 보이게
        self.files.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        for f in files or []:
            self.files.addItem(f)
        pick = QPushButton("파일 고르기… (PDF·JPG·PNG)")
        pick.clicked.connect(self._pick)
        remove = QPushButton("목록에서 빼기")
        remove.clicked.connect(self._remove)
        prow = QHBoxLayout()
        prow.addWidget(pick)
        prow.addWidget(remove)
        prow.addStretch(1)
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
        form = QFormLayout()
        form.addRow(self.r_exist, self.group_combo)
        form.addRow(self.r_new, self.new_name)
        form.addRow("새 그룹 영역", self.copy_from)

        # ---- 이미지일 때
        self.img_box = QGroupBox("이미지로 된 카드")
        self.r_single = QRadioButton("이미지 한 장에 카드 한 장")
        self.r_grid = QRadioButton("이미지 한 장에 카드 여러 장 (배열)")
        ib = QButtonGroup(self)
        ib.addButton(self.r_single)
        ib.addButton(self.r_grid)
        self.r_single.setChecked(True)
        self.cols, self.rows = QSpinBox(), QSpinBox()
        for sp, v in ((self.cols, 3), (self.rows, 3)):
            sp.setRange(1, 20)
            sp.setValue(v)
        grow = QHBoxLayout()
        grow.addWidget(QLabel("가로"))
        grow.addWidget(self.cols)
        grow.addWidget(QLabel("장 × 세로"))
        grow.addWidget(self.rows)
        grow.addWidget(QLabel("장"))
        grow.addStretch(1)
        self.img_name = QLineEdit()
        self.img_name.setPlaceholderText("비워 두면 첫 이미지 이름")
        self.b_preview = QPushButton("자를 자리 미리 보기")
        self.b_preview.clicked.connect(self.preview)
        self.img_info = QLabel()
        self.img_info.setWordWrap(True)
        self.img_info.setStyleSheet("color: #666")
        il = QFormLayout(self.img_box)
        il.addRow(self.r_single)
        il.addRow(self.r_grid, grow)
        il.addRow("파일 이름", self.img_name)
        il.addRow(self.b_preview)
        il.addRow(self.img_info)
        for w in (self.r_single, self.r_grid):
            w.toggled.connect(lambda _c: self._sync())
        for sp in (self.cols, self.rows):
            sp.valueChanged.connect(lambda _v: self._sync())
        self.files.model().rowsInserted.connect(lambda *_a: self._sync())
        self.files.model().rowsRemoved.connect(lambda *_a: self._sync())

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        set_ok_text(bb, "넣기")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("카드 파일 (같은 카드 형식끼리 한 그룹). 이미지는 카드 한 장이 한 쪽인 PDF 하나로 묶어 넣습니다."))
        lay.addWidget(self.files)
        lay.addLayout(prow)
        lay.addLayout(form)
        lay.addWidget(self.img_box)
        lay.addWidget(bb)
        self.resize(620, 560)
        self._sync()

    def all_files(self) -> list[str]:
        return [self.files.item(i).text() for i in range(self.files.count())]

    def image_files(self) -> list[str]:
        return [f for f in self.all_files() if Path(f).suffix.lower() in IMAGE_EXTS]

    def _sync(self):
        # 새 그룹 이름: 비어 있거나 앞서 자동으로 넣은 이름이면 첫 파일 이름으로 채운다 (사람이 고친 이름은 둔다)
        files = self.all_files()
        stem = Path(files[0]).stem if files else ""
        if self.new_name.text().strip() in ("", getattr(self, "_auto_name", "")):
            self.new_name.setText(stem)
        self._auto_name = stem
        ex = self.r_exist.isChecked()
        self.group_combo.setEnabled(ex)
        self.new_name.setEnabled(not ex)
        self.copy_from.setEnabled(not ex)
        imgs = self.image_files()
        self.img_box.setVisible(bool(imgs))
        grid = self.r_grid.isChecked()
        self.cols.setEnabled(grid)
        self.rows.setEnabled(grid)
        if imgs:
            per = self.cols.value() * self.rows.value() if grid else 1
            self.img_info.setText(f"이미지 {len(imgs)}장 → 카드 약 {len(imgs) * per}장 (한 쪽에 한 장씩 PDF로)")

    def _pick(self):
        exts = " ".join(f"*{e}" for e in IMAGE_EXTS)
        fs, _ = QFileDialog.getOpenFileNames(self, "카드 파일 고르기", "",
                                             f"카드 파일 (*.pdf {exts});;PDF (*.pdf);;이미지 ({exts})")
        for f in sorted(fs):
            if not self.files.findItems(f, Qt.MatchFlag.MatchExactly):
                self.files.addItem(f)

    def _remove(self):
        for it in self.files.selectedItems():
            self.files.takeItem(self.files.row(it))

    def layout_choice(self) -> tuple[int, int]:
        return (self.cols.value(), self.rows.value()) if self.r_grid.isChecked() else (1, 1)

    def preview(self):
        imgs = self.image_files()
        if not imgs:
            return
        sel = [it.text() for it in self.files.selectedItems() if Path(it.text()).suffix.lower() in IMAGE_EXTS]
        path = sel[0] if sel else imgs[0]
        cols, rows = self.layout_choice()
        try:
            png, info = service.image_preview(path, cols, rows)
        except Exception as e:  # noqa: BLE001
            show_problem(self, "자를 자리를 미리 볼 수 없습니다", str(e),
                         "그림 파일이 열리는지 확인하고, 가로·세로 장수를 바꿔 다시 해 보세요.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"자를 자리 — {Path(path).name}")
        pic = QLabel()
        pm = QPixmap()
        pm.loadFromData(png, "PNG")
        pic.setPixmap(pm)
        msg = f"카드 {info['cards']}장으로 자릅니다 (빨간 상자)."
        if info["warnings"]:
            msg += "\n" + "\n".join(info["warnings"])
        note = QLabel(msg)
        note.setWordWrap(True)
        close = QPushButton("닫기")
        close.clicked.connect(dlg.accept)
        dl = QVBoxLayout(dlg)
        dl.addWidget(pic)
        dl.addWidget(note)
        dl.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        dlg.exec()

    def accept(self):
        files = self.all_files()
        if not files:
            show_error(self, "넣을 파일이 없습니다", "아직 고른 파일이 없습니다.",
                       "‘파일 고르기…’로 카드 PDF나 그림(JPG·PNG)을 고르세요.")
            return
        new = self.r_new.isChecked()
        group = self.new_name.text().strip() if new else self.group_combo.currentData()
        if not group:
            show_error(self, "새 그룹 이름이 비어 있습니다", "새 그룹을 만들려면 이름이 필요합니다.",
                       "‘새 그룹 만들기’ 옆 칸에 이름을 넣으세요 (예: 아이템 카드).")
            return
        regions_from = (self.copy_from.currentData() or None) if new else None
        pdfs = [f for f in files if Path(f).suffix.lower() == ".pdf"]
        imgs = self.image_files()
        others = [f for f in files if f not in pdfs and f not in imgs]
        added, skipped, warnings = [], [f"{Path(f).name}: PDF·이미지가 아님" for f in others], []
        has_regions = False
        try:
            if pdfs:
                r = service.add_files(self.project_dir, pdfs, group, new_group=new, regions_from=regions_from)
                added += r["added"]
                skipped += r["skipped"]
                has_regions = r["has_regions"]
                new, regions_from = False, None  # 그룹은 이미 만들어졌다
            if imgs:
                cols, rows = self.layout_choice()
                ri = service.add_images(self.project_dir, imgs, group, cols=cols, rows=rows,
                                        name=self.img_name.text().strip() or None, new_group=new,
                                        regions_from=regions_from)
                if ri.file_dir:
                    added.append(ri.file_dir)
                skipped += ri.skipped
                warnings += ri.warnings
                has_regions = ri.has_regions
                if ri.file_dir:
                    warnings.insert(0, f"이미지 {len(imgs)}장 → 카드 {ri.pages}장 ({ri.file_dir.name})")
        except (FileExistsError, KeyError, ValueError, OSError) as e:
            show_problem(self, "파일을 넣지 못했습니다", str(e).strip("'"))
            return
        self.result_info = {"group": group, "added": added, "skipped": skipped, "has_regions": has_regions,
                            "warnings": warnings}
        if skipped or warnings:
            QMessageBox.information(self, "파일 넣기", "\n".join(warnings + skipped))
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
        # 한 PDF에 형식이 다른 카드가 섞여 있으면: 체크한 쪽을 다른 그룹에서 처리
        self.moved = False
        self.target = QComboBox()
        pdir = self.file_dir.parents[3]
        here = self.file_dir.parent.parent.name
        for g in service.list_groups(pdir):
            if g["dir"].name != here:
                self.target.addItem(g["name"], g["name"])
        self.target.addItem("새 그룹 만들기…", None)
        send = QPushButton("체크한 쪽을 이 그룹으로 보내기")
        send.clicked.connect(self.send_to_group)
        mrow = QHBoxLayout()
        mrow.addWidget(QLabel("다른 형식이면:"))
        mrow.addWidget(self.target, 1)
        mrow.addWidget(send)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        set_ok_text(bb, "저장")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("체크한 쪽은 추출·용어집·번역·지우기·한글 넣기에서 모두 빠집니다.\n"
                             "다른 형식의 카드라면 아래에서 다른 그룹으로 보내면 그 그룹의 영역으로 처리합니다."))
        lay.addWidget(self.list, 1)
        lay.addWidget(self.reason)
        lay.addLayout(mrow)
        lay.addWidget(bb)
        fit_to_screen(self, 900, 640, maximize_below=0)

    def checked(self) -> list[int]:
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked]

    def accept(self):
        service.set_pages(self.file_dir, self.checked(), self.reason.text().strip())
        super().accept()

    def send_to_group(self):
        pages = self.checked()
        if not pages:
            show_error(self, "보낼 쪽이 없습니다", "다른 그룹으로 보낼 쪽을 아직 고르지 않았습니다.",
                       "그림 옆 칸을 체크한 뒤 다시 누르세요.")
            return
        group, new = self.target.currentData(), False
        if group is None:
            group, ok = QInputDialog.getText(self, "새 그룹", "새 그룹 이름 (카드 형식, 예: 정예 카드)")
            group = (group or "").strip()
            if not ok or not group:
                return
            new = True
        try:
            service.move_pages_to_group(self.file_dir, pages, group, new_group=new)
        except (ValueError, FileExistsError) as e:
            show_problem(self, "쪽을 다른 그룹으로 보내지 못했습니다", str(e))
            return
        self.moved = True
        QMessageBox.information(self, "보냄", f"{len(pages)}쪽을 ‘{group}’ 그룹으로 보냈습니다. 이 그룹에서는 빠집니다."
                                + ("\n새 그룹이므로 영역을 지정하세요." if new else ""))
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
        form.addRow("데이터 폴더", row)
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
            show_problem(self, "데이터 폴더를 쓸 수 없습니다", str(e), "다른 폴더를 고르거나, 쓸 수 있는 폴더인지 확인하세요.")
            return
        service.save_settings(d, self.engine)
        super().accept()
