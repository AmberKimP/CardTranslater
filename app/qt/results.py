"""3차 화면: 결과 보기(원본·결과 비교, 문제 있는 쪽 걸러 보기)와 시험 실행(고른 쪽만 시험 폴더에서)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QSplitter, QVBoxLayout, QWidget,
)

from app import service
from app.qt import bg_erase, worker
from app.qt.dialogs import (
    ask_choice, card, erase_modes, file_label, fit_to_screen, grey_note, hint_bar, make_primary, page_title, section_title, set_hint, set_ok_text,
    show_error, show_failure, show_problem, subtitle,
)

from app.qt.fonts_dialog import ALIGN_CHOICES  # noqa: E402 (글꼴 창과 같은 정렬 이름)

ALIGN_TEXT = [(k, t) for k, t in ALIGN_CHOICES if k]
OK_STATUS = {"글자 축소"}  # 알려 주기만 하는 상태 (문제로 보지 않음)
STATUS_TEXT = {"글리프 없음": "글꼴에 없는 글자", "글자 축소": "글자 줄임"}  # 엔진 상태 낱말을 화면용으로


def status_text(st: list[str]) -> str:
    return ", ".join(STATUS_TEXT.get(x, x) for x in st)


def _open(p: Path):
    from app.qt.main import open_path

    open_path(p)


class ResultViewer(QDialog):
    """한글 넣기 결과를 쪽마다 원본과 나란히 본다. runs: [(이름, 결과 폴더)]."""

    def __init__(self, runs: list[tuple[str, Path]], parent=None, title: str = "결과 보기"):
        super().__init__(parent)
        self.setWindowTitle(title)
        fit_to_screen(self, 1500, 900)  # 낮은 화면에서는 최대화
        self._img_path: Path | None = None
        self.runs = runs
        self.run = QComboBox()
        for name, d in runs:
            self.run.addItem(file_label(name), str(d))
            self.run.setItemData(self.run.count() - 1, name, Qt.ItemDataRole.ToolTipRole)
        self.run.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.run.setMinimumContentsLength(18)
        self.only = QCheckBox("문제 있는 쪽만 보기")
        self.only.setToolTip("넘침·아이콘 오류·글꼴에 없는 글자 등")
        self.info = hint_bar("", "done")
        pdf_btn = QPushButton("PDF 열기")
        make_primary(pdf_btn)
        pdf_btn.setDefault(False)
        pdf_btn.setAutoDefault(False)
        pdf_btn.clicked.connect(self.open_pdf)
        folder_btn = QPushButton("폴더 열기")
        folder_btn.clicked.connect(lambda: _open(self.run_dir()))
        self.sheet_btn = QPushButton("원래 배열 이미지")
        self.sheet_btn.setToolTip("배열 이미지로 넣은 카드를 원래 시트 크기·배열로 다시 합친 이미지 (원본 해상도)")
        self.sheet_btn.clicked.connect(self.open_sheets)
        self.import_btn = QPushButton("고친 PDF 넣기…")
        self.import_btn.setToolTip("이 한글 넣기 결과 PDF를 다른 프로그램에서 고쳤다면 그 PDF를 골라 넣으세요.\n"
                                   "카드를 원래 시트 크기·배열의 이미지로 다시 합쳐 보여 줍니다.\n"
                                   "한글 넣기를 다시 해도 고친 PDF와 그 이미지는 지워지지 않습니다.")
        self.import_btn.clicked.connect(self.import_edited)
        self.edited_btn = QPushButton("고친 배열 이미지")
        self.edited_btn.setToolTip("넣은 고친 PDF로 합친 원래 배열 이미지를 엽니다")
        self.edited_btn.clicked.connect(self.open_edited)
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(QLabel("파일"))
        top.addWidget(self.run)
        top.addWidget(self.info, 1)
        top.addWidget(pdf_btn)
        top.addWidget(self.sheet_btn)
        top.addWidget(self.import_btn)
        top.addWidget(self.edited_btn)
        top.addWidget(folder_btn)
        self.pages = QListWidget()
        self.pages.currentRowChanged.connect(lambda _r: self._show_page())
        left = QWidget()
        left.setMaximumWidth(230)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.only)
        ll.addWidget(self.pages, 1)
        self.regions = QListWidget()
        self.regions.setMaximumHeight(160)
        self.regions.currentRowChanged.connect(lambda _r: self._show_region())
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.image)
        self.scroll.setWidgetResizable(True)
        self.scroll.setObjectName("cardview")
        self.scroll.setStyleSheet("QScrollArea#cardview { background: white; border: 1px solid #E6E9EE; border-radius: 10px; }"
                                  " QScrollArea#cardview > QWidget > QWidget { background: white; }")
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.image.setMinimumSize(1, 1)
        self.scroll.viewport().installEventFilter(self)  # 창·칸 크기가 바뀌면 그림을 다시 맞춘다. 휠은 이전/다음 쪽
        self.scroll.setToolTip("마우스 휠: 위로 이전 쪽, 아래로 다음 쪽")
        # 이 카드의 이 영역 고치기: 번역·글꼴·지우기 방식 (다른 카드는 그대로)
        self.edit_text = QPlainTextEdit()
        self.edit_text.setPlaceholderText("이 칸에 넣을 한글 (줄을 바꾸면 그 자리에서 줄을 바꿉니다)")
        self.edit_text.setMaximumHeight(96)
        self.edit_text.setToolTip("검수 엑셀은 바꾸지 않습니다. 저장하면 이 파일의 한글 넣기에 쓰고, 검수 엑셀을 다시 만들면 들어갑니다.\n"
                                  "그 뒤 검수 엑셀에서 같은 칸을 고치면 엑셀 쪽을 씁니다.")
        self.edit_font = QComboBox()
        self.edit_font.setToolTip("이 카드의 이 영역만 쓸 글꼴. 다른 카드는 글꼴 창에서 고른 영역 글꼴 그대로입니다.")
        self.erase_mode = QComboBox()
        self.erase_mode.setToolTip("이 카드의 고른 영역만 다른 방식으로 지웁니다. 다른 카드는 영역 설정대로입니다.")
        # 글자 크기·정렬: 이 카드만 (맨 아래 값 = 글꼴 창의 영역 설정대로)
        self.edit_size = QSpinBox()
        self.edit_size.setRange(45, 200)  # 맨 아래(45) = 영역 설정대로, 올리면 50·55·…
        self.edit_size.setSingleStep(5)
        self.edit_size.setSuffix(" %")
        self.edit_size.setMinimumWidth(240)  # ‘영역 설정대로 (100 %)’가 잘리지 않게
        self.edit_size.setToolTip("원본 글자 크기 대비 배율 (이 카드의 이 영역만). 맨 아래로 내리면 글꼴 창의 영역 설정대로입니다.\n"
                                  "한글 넣기는 이 크기로 시작해, 칸에 넘치면 조금씩 줄입니다.")
        self.edit_align = QComboBox()
        self.edit_align.setToolTip("이 카드의 이 영역만 한글을 놓는 방법")
        srow = QHBoxLayout()
        srow.addWidget(self.edit_size)
        srow.addSpacing(16)
        srow.addWidget(QLabel("정렬"))
        srow.addWidget(self.edit_align, 1)
        # 문단마다 글꼴 (‘문단마다’ 영역만): 번역을 고치면 문단 목록도 바뀐다
        self.para_box = QWidget()
        self.para_form = QFormLayout(self.para_box)
        self.para_form.setContentsMargins(0, 0, 0, 0)
        self._para_combos: list[QComboBox] = []
        self._para_picks: dict = {}
        self._fonts: list[tuple[str, str]] | None = None  # (파일, 이름): 창에서 한 번만 만든다
        self._para_timer = QTimer(self)
        self._para_timer.setSingleShot(True)
        self._para_timer.setInterval(400)
        self._para_timer.timeout.connect(self._rebuild_paragraphs)
        form = QFormLayout()
        form.addRow("번역", self.edit_text)
        form.addRow("글꼴", self.edit_font)
        self.para_label = QLabel("문단 글꼴")
        form.addRow(self.para_label, self.para_box)
        form.addRow("글자 크기", srow)
        form.addRow("지우기", self.erase_mode)
        self.preview_btn = QPushButton("미리 보기")
        self.preview_btn.setToolTip("저장하기 전에 이 칸을 한글 넣기와 똑같이 그려 봅니다 (지우기 방식 바꾼 것은 저장해야 보입니다)")
        self.preview_btn.clicked.connect(self.preview_edit)
        self.reset_btn = QPushButton("원래대로")
        self.reset_btn.setToolTip("번역은 검수 엑셀(없으면 초벌 번역) 그대로, 글꼴·문단 글꼴·크기·정렬·지우기는 영역 설정대로 돌려 놓습니다 (저장해야 반영)")
        self.reset_btn.clicked.connect(self.reset_edit)
        self.redo_btn = QPushButton("저장하고 다시 넣기")
        make_primary(self.redo_btn)
        self.redo_btn.setAutoDefault(False)
        self.redo_btn.clicked.connect(self.save_edit)
        erow = QHBoxLayout()
        self.edit_note = grey_note("")
        erow.addWidget(self.edit_note, 1)
        erow.addWidget(self.reset_btn)
        erow.addWidget(self.preview_btn)
        erow.addWidget(self.redo_btn)
        self.erase_hint = grey_note("")
        self.erase_box = QGroupBox("이 카드의 이 영역 고치기")
        egl = QVBoxLayout(self.erase_box)
        egl.addWidget(self.erase_hint)
        self.edit_form = QWidget()
        fl = QVBoxLayout(self.edit_form)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.addLayout(form)
        fl.addLayout(erow)
        egl.addWidget(self.edit_form)
        self._edit = None  # 고치는 칸: {"page", "region", "info", "erase", "erase_ok"}
        self._sel = (-1, -1)  # 저장 안 한 고침이 있을 때 되돌릴 (쪽 줄, 영역 줄)
        self.edit_text.textChanged.connect(self._mark_dirty)
        self.edit_text.textChanged.connect(lambda: self._para_timer.start())
        self.edit_font.currentIndexChanged.connect(lambda _i: self._mark_dirty())
        self.erase_mode.currentIndexChanged.connect(lambda _i: self._mark_dirty())
        self.edit_size.valueChanged.connect(lambda _v: self._mark_dirty())
        self.edit_align.currentIndexChanged.connect(lambda _i: self._mark_dirty())
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.scroll, 1)
        rrow = QHBoxLayout()
        rrow.addWidget(section_title("영역"))
        rrow.addWidget(grey_note("고르면 그 부분을 크게 비교합니다"), 1)
        rl.addLayout(rrow)
        rl.addWidget(self.regions)
        rl.addWidget(self.erase_box)
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        close = QPushButton("닫기")
        close.clicked.connect(self.accept)
        self.footer = grey_note("")
        bottom = QHBoxLayout()
        bottom.addWidget(self.footer, 1)
        bottom.addWidget(close)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(10)
        lay.addLayout(top)
        lay.addWidget(split, 1)
        lay.addLayout(bottom)
        self.run.currentIndexChanged.connect(lambda _i: self.reload())
        self.only.toggled.connect(lambda _c: self.reload())
        self.items: list[dict] = []
        self.reload()

    def run_dir(self) -> Path:
        return Path(self.run.currentData())

    def file_dir(self) -> Path | None:
        """프로젝트 결과(translated/)를 볼 때의 파일 폴더. 시험 폴더면 None (다시 지우기 없음)."""
        d = self.run_dir()
        return d.parent if d.name == "translated" and (d.parent / "cleared").exists() else None

    @staticmethod
    def problems(st: list[str]) -> list[str]:
        return [s for s in st if s not in OK_STATUS]

    def reload(self):
        self.sheet_btn.setVisible(bool(self.sheet_images()))  # 배열 이미지로 넣은 파일만
        fd = self.file_dir()
        self.import_btn.setVisible(fd is not None and service.can_import_edited(fd))  # 시험 결과는 빼고
        self.edited_btn.setVisible(fd is not None and bool(service.edited_sheet_images(fd)))
        allp = service.result_pages(self.run_dir())
        n_bad = sum(1 for p in allp if self.problems(p["status"]))
        if n_bad:
            set_hint(self.info, f"{len(allp)}쪽, 문제 있는 쪽 {n_bad} — 주황 쪽을 먼저 보세요", "warn")
        else:
            set_hint(self.info, f"{len(allp)}쪽, 문제 있는 쪽 0", "done")
        self.footer.setText("영역을 고르면 아래에서 그 칸의 번역·글꼴·크기·정렬·지우기 방식을 고치고 저장할 수 있습니다."
                            if self.file_dir() is not None
                            else "시험 결과입니다. 프로젝트의 지운 카드·한글 카드는 바뀌지 않았습니다.")
        self.items = [p for p in allp if self.problems(p["status"])] if self.only.isChecked() else allp
        self.pages.clear()
        for p in self.items:
            it = QListWidgetItem(f"{p['page']}쪽" + (f" · {status_text(p['status'])}" if p["status"] else ""))
            if self.problems(p["status"]):
                it.setBackground(QColor("#F4B183"))
            elif p["status"]:  # 알림만 있는 쪽: 배경 없이 회색 글자
                it.setForeground(QColor("#666"))
            self.pages.addItem(it)
        if self.items:
            self.pages.setCurrentRow(0)
        else:
            self.image.setText("보여 줄 쪽이 없습니다")
            self.regions.clear()

    def current(self) -> dict | None:
        r = self.pages.currentRow()
        return self.items[r] if 0 <= r < len(self.items) else None

    def eventFilter(self, obj: QObject, ev: QEvent) -> bool:
        if obj is self.scroll.viewport() and ev.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self._refit)
        if obj is self.scroll.viewport() and ev.type() == QEvent.Type.Wheel:
            self._wheel(ev.angleDelta().y())
            return True
        return super().eventFilter(obj, ev)

    def _wheel(self, dy: int):
        """그림 위에서 휠: 아래로 굴리면 다음 쪽, 위로 굴리면 이전 쪽 (터치패드의 작은 값은 모아서 한 칸)."""
        self._wheel_acc = getattr(self, "_wheel_acc", 0) + dy
        step = 0
        while abs(self._wheel_acc) >= 120:
            step += -1 if self._wheel_acc > 0 else 1
            self._wheel_acc -= 120 if self._wheel_acc > 0 else -120
        if step and self.items:
            row = max(0, min(len(self.items) - 1, self.pages.currentRow() + step))
            self.pages.setCurrentRow(row)

    def showEvent(self, ev):
        super().showEvent(ev)
        QTimer.singleShot(0, self._refit)

    def _refit(self):
        try:
            self._set_image(self._img_path)
        except RuntimeError:  # 창이 이미 닫혀 지워졌을 때
            pass

    def _set_pixmap(self, pm: QPixmap):
        """파일이 아닌 그림(저장 전 미리 보기)을 보여 준다."""
        self._img_path = None
        room = self.scroll.viewport().size() - QSize(10, 10)
        if room.width() > 50 and room.height() > 50 and (pm.width() > room.width() or pm.height() > room.height()):
            pm = pm.scaled(room, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.image.setPixmap(pm)

    def _set_image(self, path: Path | None):
        self._img_path = path
        if not path:
            self.image.setPixmap(QPixmap())
            self.image.setText("비교 이미지가 없습니다")
            return
        pm = QPixmap(str(path))
        room = self.scroll.viewport().size() - QSize(10, 10)
        if room.width() > 50 and room.height() > 50 and (pm.width() > room.width() or pm.height() > room.height()):
            pm = pm.scaled(room, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.image.setPixmap(pm)  # 원본보다 크게 키우지 않는다

    def _leave_ok(self) -> bool:
        """저장하지 않은 고침이 있으면 버리고 넘어갈지 묻는다. 아니요면 고르던 칸으로 돌아간다."""
        if not self._dirty():
            return True
        r = QMessageBox.question(self, "저장하지 않은 고침", "이 칸에서 고친 번역·글꼴·지우기 방식을 저장하지 않았습니다.\n"
                                 "저장하지 않고 다른 칸으로 넘어갈까요?")
        if r == QMessageBox.StandardButton.Yes:
            self._edit = None
            return True
        pr, rr = self._sel
        for w, row in ((self.pages, pr), (self.regions, rr)):
            w.blockSignals(True)
            w.setCurrentRow(row)
            w.blockSignals(False)
        return False

    def _show_page(self):
        if not self._leave_ok():
            return
        p = self.current()
        if p is None:
            return
        self._set_image(p["card_png"])
        self.regions.blockSignals(True)
        self.regions.clear()
        self.regions.addItem("카드 전체")
        fd = self.file_dir()
        self._erase_info = service.card_erase_info(fd, p["page"]) if fd else {}
        for r in p["regions"]:
            text = f"{r['name']}" + (f" · {status_text(r['status'])}" if r["status"] else "")
            ei = self._erase_info.get(r["name"]) or {}
            if ei.get("mode"):
                text += f" · 지우기: {dict(erase_modes())[ei['mode']]}"
            elif ei.get("needs_choice"):
                text += " · 그림 배경: 지우기 방식 확인"
            if r["errors"]:
                text += f" — {'; '.join(r['errors'])[:120]}"
            it = QListWidgetItem(text)
            if self.problems(r["status"]):
                it.setBackground(QColor("#F4B183"))
            elif not ei.get("mode") and ei.get("needs_choice"):
                it.setBackground(QColor("#FFE066"))
            self.regions.addItem(it)
        self.regions.setCurrentRow(0)
        self.regions.blockSignals(False)
        self._sync_erase()

    def _region_name(self) -> str | None:
        p, r = self.current(), self.regions.currentRow() - 1
        return p["regions"][r]["name"] if p and 0 <= r < len(p["regions"]) else None

    def _sync_erase(self):
        """고른 칸의 지금 값(번역·글꼴·지우기 방식)을 고치기 칸에 채운다."""
        p, name, fd = self.current(), self._region_name(), self.file_dir()
        ei = (getattr(self, "_erase_info", {}) or {}).get(name) if name else None
        self._edit = None
        info = None
        if p and name and fd:
            try:
                info = service.card_edit_info(fd, p["page"], name)
            except Exception as e:  # noqa: BLE001 - 고치기는 못 해도 보기는 된다
                self.erase_hint.setText(f"이 칸의 번역을 읽지 못했습니다: {e}")
        ok = info is not None
        self.edit_form.setVisible(ok)
        self.erase_hint.setVisible(not ok)
        self._sel = (self.pages.currentRow(), self.regions.currentRow())
        if not ok:
            if not (p and name and fd):
                self.erase_hint.setText("위 목록에서 영역(카드 전체 말고)을 고르면 그 칸의 번역·글꼴·크기·정렬·지우기 방식을 고칠 수 있습니다."
                                        if fd else "시험 결과에서는 바꿀 수 없습니다.")
            return
        for w in (self.edit_text, self.edit_font, self.erase_mode, self.edit_size, self.edit_align):
            w.blockSignals(True)
        self.edit_text.setPlainText(info["ko"])
        self.edit_size.setSpecialValueText(f"영역 설정대로 ({round(info['region_size'] * 100)} %)")
        self.edit_size.setValue(round(info["size"] * 100) if info["size"] else self.edit_size.minimum())
        self.edit_align.clear()
        ra = dict(ALIGN_TEXT).get(info["region_align"], "원문 줄을 따라감")
        self.edit_align.addItem(f"영역 설정대로 ({ra})", "")
        for k, t in ALIGN_TEXT:
            self.edit_align.addItem(t, k)
        self.edit_align.setCurrentIndex(max(0, self.edit_align.findData(info["align"])) if info["align"] else 0)
        self.edit_font.clear()
        rf = info["region_font"]
        self.edit_font.addItem(f"영역 글꼴대로 ({service.font_label(rf) if rf else '영역 설정'})", "")
        fonts = list(self._font_list())
        if info["font"] and all(f != info["font"] for f, _l in fonts):
            fonts.append((info["font"], service.font_label(info["font"])))
        for f, label in fonts:
            self.edit_font.addItem(label, f)
        self.edit_font.setCurrentIndex(max(0, self.edit_font.findData(info["font"])) if info["font"] else 0)
        self.erase_mode.clear()
        erase_ok = bool(ei) and ei.get("kind") == "text"
        if erase_ok:
            labels = dict(erase_modes())
            used = f", 이번에 쓴 방식: {labels.get(ei['used'], ei['used'])}" if ei.get("used") else ""
            self.erase_mode.addItem(f"영역 설정대로 ({labels.get(ei['default'], ei['default'])}{used})", None)
            for k, t in erase_modes():
                if k != "auto":
                    self.erase_mode.addItem(t, k)
            self.erase_mode.setCurrentIndex(max(0, self.erase_mode.findData(ei.get("mode"))) if ei.get("mode") else 0)
        self.erase_mode.setEnabled(erase_ok)
        for w in (self.edit_text, self.edit_font, self.erase_mode, self.edit_size, self.edit_align):
            w.blockSignals(False)
        self._edit = {"page": p["page"], "region": name, "info": info,
                      "erase": self.erase_mode.currentData() if erase_ok else None, "erase_ok": erase_ok}
        self._para_picks = dict(info["paragraph_fonts"])
        self._rebuild_paragraphs(load=True)
        self._mark_dirty()

    # ---- 문단 글꼴 (‘문단마다’ 영역)
    def _font_list(self) -> list[tuple[str, str]]:
        if self._fonts is None:
            self._fonts = [(f["file"], service.font_label(f["file"])) for f in service.font_choices()]
        return self._fonts

    def _rebuild_paragraphs(self, load: bool = False):
        """번역 글의 문단마다 글꼴 칸을 다시 만든다 (고른 것은 문단 번호로 지킨다).
        load=True: 새 칸을 열 때 (앞 칸의 문단 칸 값을 이어받지 않는다)."""
        e = self._edit
        on = bool(e and e["info"]["paragraph_mode"])
        self.para_label.setVisible(on)
        self.para_box.setVisible(on)
        if not on:
            while self.para_form.rowCount():
                self.para_form.removeRow(0)
            self._para_combos = []
            return
        if not load:
            for i, cb in enumerate(self._para_combos):  # 글을 고치는 중: 지금 고른 것을 기억
                self._para_picks[i] = cb.currentData() or None
        while self.para_form.rowCount():
            self.para_form.removeRow(0)
        self._para_combos = []
        paras = service.split_paragraphs(self.edit_text.toPlainText())
        fonts = list(self._font_list())
        fonts += [(f, service.font_label(f)) for f in set(self._para_picks.values())
                  if f and all(f != x for x, _l in fonts)]
        for i, t in enumerate(paras):
            cb = QComboBox()
            cb.addItem("(기준 카드 문단 설정대로)", "")
            for f, label in fonts:
                cb.addItem(label, f)
            pick = self._para_picks.get(i)
            cb.setCurrentIndex(max(0, cb.findData(pick)) if pick else 0)
            cb.setToolTip("이 카드의 이 문단만 쓸 글꼴. ‘기준 카드 문단 설정대로’는 글꼴 창에서 정한 문단 글꼴(없으면 영역 글꼴)")
            cb.currentIndexChanged.connect(lambda _i: self._mark_dirty())
            lab = QLabel(f"{i + 1}. {t[:28]}{'…' if len(t) > 28 else ''}")
            lab.setToolTip(t)
            self.para_form.addRow(lab, cb)
            self._para_combos.append(cb)
        self._mark_dirty()

    def _saved_paras(self) -> dict:
        """저장된 문단 글꼴 중 지금 글에 있는 문단만 (글이 짧아져 없는 문단은 비교하지 않는다)."""
        n = len(self._para_combos)
        return {i: f for i, f in ((self._edit or {}).get("info") or {}).get("paragraph_fonts", {}).items() if i < n}

    def _para_fonts(self) -> dict:
        """지금 고른 문단 글꼴 {문단 번호: 글꼴} (‘설정대로’는 빠짐)."""
        return {i: cb.currentData() for i, cb in enumerate(self._para_combos) if cb.currentData()}

    def _size_value(self) -> float | None:
        v = self.edit_size.value()
        return None if v == self.edit_size.minimum() else v / 100.0

    def _dirty(self) -> bool:
        e = getattr(self, "_edit", None)
        if not e:
            return False
        info = e["info"]
        return (self.edit_text.toPlainText().strip() != info["ko"].strip()
                or (self.edit_font.currentData() or "") != info["font"]
                or (e["erase_ok"] and self.erase_mode.currentData() != e["erase"])
                or self._size_value() != info["size"]
                or (self.edit_align.currentData() or "") != info["align"]
                or (info["paragraph_mode"] and self._para_fonts() != self._saved_paras()))

    def _mark_dirty(self):
        e = self._edit
        if not e:
            return
        bits = []
        if e["info"]["edited"]:
            bits.append("번역: 결과 보기에서 고친 것")
        if e["info"]["font"]:
            bits.append("글꼴: 이 카드만 따로")
        if e["info"]["size"] or e["info"]["align"]:
            bits.append("크기·정렬: 이 카드만 따로")
        if e["info"]["paragraph_fonts"]:
            bits.append("문단 글꼴: 이 카드만 따로")
        if self._dirty():
            bits.append("저장하지 않은 고침 있음")
        self.edit_note.setText(" · ".join(bits))
        self.redo_btn.setEnabled(self._dirty())

    def reset_edit(self):
        e = self._edit
        if not e:
            return
        self.edit_text.setPlainText(e["info"]["base"])
        self.edit_font.setCurrentIndex(0)
        self.edit_size.setValue(self.edit_size.minimum())
        self.edit_align.setCurrentIndex(0)
        self._para_picks = {}
        for cb in self._para_combos:
            cb.setCurrentIndex(0)
        if e["erase_ok"]:
            self.erase_mode.setCurrentIndex(0)

    def preview_edit(self):
        e, fd = self._edit, self.file_dir()
        if not (e and fd):
            return
        try:
            png = service.card_preview(fd, e["page"], e["region"], self.edit_text.toPlainText(),
                                       self.edit_font.currentData() or None, self._size_value(),
                                       self.edit_align.currentData() or "",
                                       self._para_fonts() if e["info"]["paragraph_mode"] else None)
        except Exception as ex:  # noqa: BLE001
            show_error(self, "미리 보기를 만들지 못했습니다", ex)
            return
        if not png:
            show_problem(self, "미리 보기를 만들지 못했습니다", "이 칸은 그려 볼 수 없습니다 (번역이 비었거나 지운 결과가 없음).")
            return
        pm = QPixmap()
        pm.loadFromData(png, "PNG")
        self._set_pixmap(pm)
        self.edit_note.setText("위 그림은 저장 전 미리 보기입니다 (지우기 방식을 바꾼 것은 저장해야 보입니다)")

    def save_edit(self):
        """고친 번역·글꼴·지우기 방식을 저장하고 이 파일에 다시 넣는다 (지우기 방식을 바꿨을 때만 다시 지운다)."""
        e, fd = self._edit, self.file_dir()
        if not (e and fd and self._dirty()):
            return
        page, region = e["page"], e["region"]
        text = self.edit_text.toPlainText()
        erase_changed = bool(e["erase_ok"] and self.erase_mode.currentData() != e["erase"])
        try:
            info = e["info"]
            paras = self._para_fonts() if info["paragraph_mode"] else None
            service.save_card_edit(fd, page, region,
                                   text=text if text.strip() != info["ko"].strip() else None,
                                   font=self.edit_font.currentData() or None,
                                   set_font=(self.edit_font.currentData() or "") != info["font"],
                                   size=self._size_value(), set_size=self._size_value() != info["size"],
                                   align=self.edit_align.currentData() or "",
                                   set_align=(self.edit_align.currentData() or "") != info["align"],
                                   paragraph_fonts=paras if paras is not None and paras != self._saved_paras()
                                   else None)
            if erase_changed:
                service.set_card_erase_mode(fd, page, region, self.erase_mode.currentData())
        except Exception as ex:  # noqa: BLE001
            show_error(self, "저장하지 못했습니다", ex)
            return
        self._edit = None

        def done(res):
            if res.state != "done":
                show_problem(self, "다시 넣지 못했습니다", res.error or "\n".join(res.blocking) or res.state)
            row = next((i for i, it in enumerate(self.items) if it["page"] == page), 0)
            self.reload()
            self.pages.setCurrentRow(min(row, max(0, len(self.items) - 1)))
            names = [x["name"] for x in (self.current() or {}).get("regions", [])]
            if region in names:
                self.regions.setCurrentRow(names.index(region) + 1)

        job = worker.Job(service.redo_file if erase_changed else service.recompose_file, fd)
        bg_erase.run_exclusive(self, "다시 지우고 한글 넣기" if erase_changed else "한글 다시 넣기", job, done,
                               lambda m: show_failure(self, "다시 넣지 못했습니다", m))

    redo_card = save_edit  # 예전 이름

    def _show_region(self):
        if not self._leave_ok():
            return
        self._sync_erase()
        p, r = self.current(), self.regions.currentRow() - 1  # 0번 줄은 ‘카드 전체’
        if p is None:
            return
        if r < 0:
            self._set_image(p["card_png"])
        elif r < len(p["regions"]):
            self._set_image(p["regions"][r]["png"])

    def sheet_images(self) -> list[Path]:
        d = self.run_dir()
        return sorted(p for p in (d / "images").glob("*") if p.suffix.lower() in (".png", ".jpg")) if d else []

    def open_sheets(self):
        """시트가 하나면 그 이미지를, 여럿이면 폴더를 연다 (사진 보기 프로그램에서 원본 해상도로 확대)."""
        imgs = self.sheet_images()
        if imgs:
            _open(imgs[0] if len(imgs) == 1 else imgs[0].parent)

    def import_edited(self):
        """고친 한글 PDF를 골라 원래 배열 이미지로 합치고, 다 되면 그 이미지를 연다."""
        from PySide6.QtWidgets import QFileDialog

        fd = self.file_dir()
        if fd is None:
            return
        path, _ = QFileDialog.getOpenFileName(self, "고친 PDF 고르기", str(self.run_dir()), "PDF (*.pdf)")
        if not path:
            return

        def done(r):
            self.reload()
            msg = f"고친 PDF로 원래 배열 이미지 {len(r.sheet_images)}장을 만들었습니다."
            if r.warnings:
                msg += "\n\n" + "\n".join(r.warnings)
            QMessageBox.information(self, "고친 PDF 넣기", msg)
            self.open_edited()

        job = worker.Job(service.import_edited_pdf, fd, path)
        worker.run_with_dialog(self, "고친 PDF 넣기", job, done,
                               lambda m: show_failure(self, "고친 PDF를 넣지 못했습니다", m))

    def open_edited(self):
        """고친 배열 이미지가 하나면 그 이미지를, 여럿이면 폴더를 연다."""
        fd = self.file_dir()
        imgs = service.edited_sheet_images(fd) if fd else []
        if imgs:
            _open(imgs[0] if len(imgs) == 1 else imgs[0].parent)

    def open_pdf(self):
        d = self.run_dir()
        pdf = next((d / n for n in ("translated.pdf", "composed.pdf") if (d / n).exists()), None)
        if pdf:
            _open(pdf)


def _group_cards(g: dict) -> int:
    return sum(f["included"] for f in g["files"])


class GroupPickDialog(QDialog):
    """어느 그룹에 할지 고른다 (한글 넣기·결과 보기·시험 실행). 할 수 없는 그룹은 흐리게 이유와 함께.
    groups: overview 그룹 dict, check(g) -> (할 수 있나, 설명). cards=True면 몇 장 할지도 고른다 (시험 실행)."""

    def __init__(self, title: str, groups: list[dict], check, parent=None, cards: bool = False,
                 current: dict | None = None):
        from PySide6.QtWidgets import QButtonGroup, QRadioButton

        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(520)
        self.groups = groups
        self.radios = QButtonGroup(self)
        box = QVBoxLayout()
        first = None
        for i, g in enumerate(groups):
            ok, note = check(g)
            rb = QRadioButton(f"{g['name']}    {note}")
            rb.setEnabled(ok)
            self.radios.addButton(rb, i)
            box.addWidget(rb)
            if ok and (first is None or (current is not None and str(g["dir"]) == str(current["dir"]))):
                first = rb
        if first is not None:
            first.setChecked(True)
        self.n = None
        form = QFormLayout()
        if cards:
            self.n = QSpinBox()
            self.n.setRange(1, 999)
            self.n.setValue(3)
            self.n.setSuffix(" 장")
            self.n.setToolTip("그룹의 파일 순서대로, 처리할 카드 앞에서부터 이만큼만 시험합니다")
            form.addRow("몇 장", self.n)
            self.radios.idToggled.connect(lambda i, on: on and self._limit(i))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        set_ok_text(bb, "시험 실행" if cards else "다음")
        self.ok_btn = bb.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_btn.setEnabled(first is not None)
        self.radios.idToggled.connect(lambda _i, _on: self.ok_btn.setEnabled(self.radios.checkedId() >= 0))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.setSpacing(10)
        lay.addWidget(page_title(title, 14))
        lay.addWidget(subtitle("어느 그룹에 할까요?" + (" 고른 그룹의 앞 몇 장만 따로 지우고 한글을 넣어 봅니다 "
                                                    "(프로젝트의 지운 카드·한글 카드는 바뀌지 않습니다)." if cards else "")))
        lay.addLayout(box)
        if cards:
            lay.addLayout(form)
            if self.radios.checkedId() >= 0:
                self._limit(self.radios.checkedId())
        lay.addWidget(bb)

    def _limit(self, i: int):
        n = max(1, _group_cards(self.groups[i]))
        self.n.setMaximum(n)
        self.n.setValue(min(self.n.value(), n))

    def chosen(self) -> dict | None:
        i = self.radios.checkedId()
        return self.groups[i] if i >= 0 else None

    def cards(self) -> int:
        return self.n.value() if self.n is not None else 0


class OutputActions:
    """프로젝트 화면의 ⑦ 지우기 · ⑧ 한글 넣기 · 결과 보기 · 시험 실행."""

    def __init__(self, page):
        self.page = page

    @property
    def pdir(self) -> Path:
        return self.page.project_dir

    def _files(self) -> list[dict]:
        """그리드에서 고른 범위(전체·그룹·파일)의 파일."""
        return self.page.scoped_files()

    # ---- 지우기
    def erase(self, auto: bool = False, note: str = ""):
        """auto=True: 번역에 이어서 부를 때. 묻지 않고 지운 파일은 건너뛴다. note는 끝 창 맨 위에 보인다."""
        if not auto and self.page.steps.ask_scope("⑦ 글자 지우기") is False:  # 그룹·파일을 고른 채면 전체/고른 항목 (기본 전체)
            return
        self._note = note
        scope = self.page.scope()[1]
        bg = getattr(self.page, "bg", None)
        if bg is not None and bg.busy_with(scope):  # 뒤에서 지우는 중: 서두르게만 한다
            bg.boost()
            msg = f"{note}\n\n" if note else ""
            QMessageBox.information(self.page, "글자 지우기",
                                    msg + f"뒤에서 글자를 지우는 중입니다 ({bg.text or '진행 중'}).\n"
                                    "우선순위를 높였습니다. 끝나면 목록의 ‘글자 지움’이 채워집니다.")
            return
        need = {str(d) for d in service.files_needing_erase(self.pdir, scope)}
        if not need:  # 이미 다 지웠고 그 뒤 원문·설정도 그대로: 알리기만
            if auto and note:
                self._erase_done(service.ProjectStepResult("done"))
                return
            again = ask_choice(self.page, "글자 지우기", "이미 지웠습니다 (그 뒤 원문·영역 설정도 바뀌지 않았습니다).",
                               [("확인", "accept"), ("그래도 다시 지우기", "destructive")], default=0,
                               informative="카드 한 장만 다르게 지우려면 결과 보기에서 그 영역의 ‘지우기’를 바꾸세요.\n"
                                           "‘그래도 다시 지우기’는 고른 범위를 처음부터 다시 지웁니다.")
            if again != 1:
                return
            job = worker.Job(service.erase_project, self.pdir, redo=True, files=scope)
        else:
            job = worker.Job(service.erase_project, self.pdir, files=sorted(need))
        bg_erase.run_exclusive(self.page, "글자 지우기", job, self._erase_done, self._erase_failed)

    def _erase_failed(self, m):
        show_failure(self.page, "글자 지우기를 끝내지 못했습니다", m)

    def _erase_done(self, r):
        from app.qt.steps import state_label

        self.page.refresh()
        lines = [f"{f['file']}: {state_label(f['state'])}" + (f" — {f['pages']}쪽, 경고 {f['warnings']}" if "pages" in f
                                                 else f" — {f.get('reason', '')}") for f in r.files]
        note, self._note = getattr(self, "_note", ""), ""
        if note:
            lines = ["⑥ 전체 번역", note, "", "글자 지우기"] + lines
        if r.state in ("done", "skipped") and note:  # 번역에 이어 지운 경우: 바로 번역 검수로 갈 수 있게
            box = QMessageBox(self.page)
            box.setWindowTitle("글자 지우기")
            box.setText("\n".join(lines) + (f"\n\n{r.reason}" if r.reason else "")
                        + "\n\n‘번역 검수’에서 번역을 칸마다 확인하세요.")
            openb = box.addButton("번역 검수 열기", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("닫기", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is openb:
                self.page.steps.review_translation()
        elif r.state in ("done", "skipped"):
            QMessageBox.information(self.page, "글자 지우기", "\n".join(lines) + (f"\n\n{r.reason}" if r.reason else ""))
        else:
            show_problem(self.page, f"글자 지우기를 끝내지 못했습니다 ({state_label(r.state)})", r.reason or "",
                         detail="\n".join(lines))

    # ---- 한글 넣기
    def compose(self, accept: bool = False):
        """한글 넣기. 처음 누르면 먼저 글꼴 고르기 창을 띄운다 (엑셀 확인 뒤 다시 부를 때는 묻지 않음)."""
        if accept:
            self._run_compose(True)
            return
        if not self._pick_group("⑧ 한글 넣기", self._can_compose):
            return
        if not self._review_left_ok():
            return
        job = worker.Job(service.font_plans, self.pdir, files=self.page.scope()[1])
        worker.run_with_dialog(self.page, "글꼴 추천 받는 중", job, self._fonts_ready,
                               lambda m: show_failure(self.page, "글꼴 추천을 받지 못했습니다", m))

    def _review_left_ok(self) -> bool:
        """고른 범위에 번역 검수에서 ‘꼭 고칠 칸’이 남았으면 묻는다. 넣어도 되면 True."""
        try:
            by_file = (service.review_progress(self.pdir) or {}).get("by_file") or {}
        except Exception:  # noqa: BLE001 - 검수 숫자를 못 구해도 한글 넣기는 막지 않는다
            return True
        left = sum((by_file.get(f["key"]) or {}).get("must_left", 0) for f in self._files())
        if not left:
            return True
        r = ask_choice(self.page, "번역 검수", f"꼭 고칠 칸이 {left}개 남았습니다. 그대로 한글을 넣을까요?",
                       [("번역 검수 열기", "accept"), ("그대로 넣기", "accept"), ("취소", "reject")], default=0)
        if r == 0:
            self.page.steps.review_translation()
        return r == 1

    def _fonts_ready(self, plans):
        from app.qt.fonts_dialog import FontPlanDialog

        if plans:  # 지운 결과가 없는 파일만 있으면 글꼴 창 없이 (한글 넣기가 이유를 알려 준다)
            dlg = FontPlanDialog(plans, self.page)
            if not dlg.exec():
                return
            for c in dlg.choices():
                service.save_font_choices(c["file_dir"], c["regions"], c["cards"], c["sentences"],
                                          c.get("sizes"), c.get("aligns"), c.get("slots"))
        self._run_compose(False)

    def _run_compose(self, accept: bool):
        job = worker.Job(service.compose_project, self.pdir, accept_issues=accept, files=self.page.scope()[1])
        bg_erase.run_exclusive(self.page, "한글 넣기", job, self._compose_done,
                               lambda m: show_failure(self.page, "한글 넣기를 끝내지 못했습니다", m))

    def _compose_done(self, r):
        from app.qt.steps import _ask_list, state_label

        self.page.refresh()
        if r.state == "needs_confirmation":
            issues = [f"{f['file']}: {i}" for f in r.files for i in f.get("issues", [])]
            if _ask_list(self.page, "한글 넣기 확인", "검수 엑셀에 확인할 항목이 있어 그 파일은 넣지 않았습니다.",
                         issues, "확인했으면 이대로 넣을까요?"):
                self.compose(accept=True)
            return
        lines = []
        for f in r.files:
            summ = ", ".join(f"{k} {v}" for k, v in (f.get("summary") or {}).items())
            lines.append(f"{f['file']}: {state_label(f['state'])} ({f.get('from', '')})" + (f" — {summ}" if summ else "")
                         + (f" — {'; '.join(f.get('blocking') or [])}" if f.get("blocking") else ""))
            if f.get("sheet_images"):
                lines.append(f"    원래 배열 이미지 {len(f['sheet_images'])}장: {Path(f['sheet_images'][0]).parent}")
            if f.get("sheet_error"):
                lines.append(f"    {f['sheet_error']}")
        box = QMessageBox(self.page)
        box.setWindowTitle("한글 넣기")
        box.setText("\n".join(lines) + (f"\n\n{r.reason}" if r.reason else ""))
        view = box.addButton("결과 보기", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("닫기", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is view:
            self.results()

    # ---- 그룹 고르기 (한글 넣기부터는 그룹 단위)
    @staticmethod
    def _can_compose(g: dict) -> tuple[bool, str]:
        files = g["files"]
        n = len(files)
        if not files:
            return False, "파일 없음"
        if not all(f.get("translated") for f in files):
            return False, "아직 못 함: 먼저 ⑥ 전체 번역"
        if not all(f["erased"] for f in files):
            return False, "아직 못 함: 먼저 ⑦ 글자 지우기"
        done = sum(1 for f in files if f["composed"])
        return True, f"파일 {n}개 · 한글 {done}/{n}" + (" 완료" if done == n else "")

    @staticmethod
    def _can_view(g: dict) -> tuple[bool, str]:
        done = sum(1 for f in g["files"] if f["composed"])
        return (done > 0, f"한글 넣은 파일 {done}/{len(g['files'])}" if done else "아직 한글을 넣지 않음")

    @staticmethod
    def _can_test(g: dict) -> tuple[bool, str]:
        if not g["files"] or not all(f.get("translated") for f in g["files"]):
            return False, "아직 못 함: 먼저 ⑥ 전체 번역"
        return True, f"카드 {_group_cards(g)}장"

    def _pick_group(self, title: str, check) -> bool:
        """목록에서 ‘전체’를 고른 채면 그룹을 고른다 (그룹이 하나면 묻지 않음). 고른 그룹을 목록에서도 고른다.
        그룹·파일을 고른 채면 그대로. 진행하면 True."""
        sel = self.page._selected()
        if sel and sel[0] in ("group", "file"):  # 고른 그룹·파일 그대로, 다만 할 수 있는지는 본다
            target = sel[1] if sel[0] == "group" else {"name": sel[1].get("key", ""), "files": [sel[1]]}
            ok, note = check(target)
            if not ok:
                QMessageBox.information(self.page, title, f"{target.get('name', '')}: {note}")
            return ok
        groups = [g for g in self.page.ov["groups"] if g["files"]]
        if not groups:
            return False
        if len(groups) == 1:
            ok, note = check(groups[0])
            if not ok:
                QMessageBox.information(self.page, title, note)
                return False
            self.page.select_group(groups[0])
            return True
        if not any(check(g)[0] for g in groups):
            QMessageBox.information(self.page, title, "할 수 있는 그룹이 없습니다\n\n"
                                    + "\n".join(f"{g['name']}: {check(g)[1]}" for g in groups))
            return False
        dlg = GroupPickDialog(f"{title} — 그룹 고르기", groups, check, self.page)
        if not dlg.exec() or dlg.chosen() is None:
            return False
        self.page.select_group(dlg.chosen())
        return True

    # ---- 결과 보기
    def results(self):
        if not self._pick_group("결과 보기", self._can_view):
            return
        runs = [(f["key"], Path(f["dir"]) / "translated") for f in self._files() if f["composed"]]
        if not runs:
            QMessageBox.information(self.page, "결과 보기", "아직 한글을 넣은 파일이 없습니다")
            return
        ResultViewer(runs, self.page).exec()

    # ---- 시험 실행
    def test_run(self):
        """시험 실행: 그룹을 고르고 그 그룹의 앞 몇 장만 시험 폴더에서 지우기 -> 한글 넣기."""
        groups = [g for g in self.page.ov["groups"] if g["files"]]
        if not groups:
            return
        sel = self.page._selected()
        current = None
        if sel and sel[0] == "group":
            current = sel[1]
        elif sel and sel[0] == "file":
            current = next((g for g in groups if any(str(f["dir"]) == str(sel[1]["dir"]) for f in g["files"])), None)
        dlg = GroupPickDialog("시험 실행 (몇 장만)", groups, self._can_test, self.page, cards=True, current=current)
        if not dlg.exec() or dlg.chosen() is None:
            return
        g = dlg.chosen()
        self.page.select_group(g)
        job = worker.Job(service.test_run_cards, [f["dir"] for f in g["files"]], dlg.cards())
        bg_erase.run_exclusive(self.page, "시험 실행", job, self._test_done,
                               lambda m: show_failure(self.page, "시험 실행을 끝내지 못했습니다", m))

    def _test_done(self, r):
        if r.state != "done":
            show_problem(self.page, "시험 실행을 끝내지 못했습니다", r.reason or "")
            return
        runs = [(f"시험: {x.run_dir.name}", x.run_dir) for x in r.runs]
        ResultViewer(runs, self.page, title="시험 실행 결과").exec()
