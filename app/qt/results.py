"""3차 화면: 결과 보기(원본·결과 비교, 문제 있는 쪽 걸러 보기)와 시험 실행(고른 쪽만 시험 폴더에서)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QSplitter, QVBoxLayout, QWidget,
)

from app import service
from app.qt import worker
from app.qt.dialogs import (
    card, erase_modes, file_label, fit_to_screen, grey_note, hint_bar, make_primary, page_title, section_title, set_hint, set_ok_text,
    show_error, show_failure, show_problem, subtitle,
)

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
        # 이 카드의 이 영역만 다른 방식으로 지우기 (그림 배경 등)
        self.erase_mode = QComboBox()
        self.erase_mode.setToolTip("이 카드의 고른 영역만 다른 방식으로 지웁니다. 다른 카드는 영역 설정대로입니다.")
        self.redo_btn = QPushButton("이 방식으로 다시 지우고 한글 넣기")
        self.redo_btn.clicked.connect(self.redo_card)
        erow = QHBoxLayout()
        erow.addWidget(self.erase_mode, 1)
        erow.addWidget(self.redo_btn)
        self.erase_hint = grey_note("")
        self.erase_box = QGroupBox("이 카드의 이 영역만 다르게 지우기")
        egl = QVBoxLayout(self.erase_box)
        egl.addWidget(self.erase_hint)
        egl.addLayout(erow)
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
        self.footer.setText("번역을 고치려면 검수 엑셀을 고친 뒤 프로젝트 화면에서 ‘⑧ 한글 넣기’를 다시 누르세요."
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

    def _show_page(self):
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

    def _sync_erase(self):
        p, r = self.current(), self.regions.currentRow() - 1
        name = p["regions"][r]["name"] if p and 0 <= r < len(p["regions"]) else None
        ei = (getattr(self, "_erase_info", {}) or {}).get(name) if name else None
        ok = bool(ei) and ei.get("kind") == "text"
        self.erase_mode.blockSignals(True)
        self.erase_mode.clear()
        if ok:
            labels = dict(erase_modes())
            used = f", 이번에 쓴 방식: {labels.get(ei['used'], ei['used'])}" if ei.get("used") else ""
            self.erase_mode.addItem(f"영역 설정대로 ({labels.get(ei['default'], ei['default'])}{used})", None)
            for k, t in erase_modes():
                if k != "auto":
                    self.erase_mode.addItem(t, k)
            self.erase_mode.setCurrentIndex(max(0, self.erase_mode.findData(ei.get("mode"))) if ei.get("mode") else 0)
        self.erase_mode.blockSignals(False)
        for w in (self.erase_mode, self.redo_btn):
            w.setEnabled(ok)
            w.setVisible(ok)
        self.erase_hint.setVisible(not ok)
        if not ok:
            self.erase_hint.setText("위 목록에서 영역(카드 전체 말고)을 고르면 그 영역의 지우기 방식을 바꿀 수 있습니다."
                                    if self.file_dir() else "시험 결과에서는 바꿀 수 없습니다.")

    def redo_card(self):
        p, r = self.current(), self.regions.currentRow() - 1
        fd = self.file_dir()
        if not (p and fd and 0 <= r < len(p["regions"])):
            return
        page, region = p["page"], p["regions"][r]["name"]
        service.set_card_erase_mode(fd, page, region, self.erase_mode.currentData())

        def done(res):
            if res.state != "done":
                show_problem(self, "이 카드를 다시 만들지 못했습니다", res.error or "\n".join(res.blocking) or res.state)
            row = next((i for i, it in enumerate(self.items) if it["page"] == page), 0)
            self.reload()
            self.pages.setCurrentRow(min(row, max(0, len(self.items) - 1)))
            names = [x["name"] for x in (self.current() or {}).get("regions", [])]
            if region in names:
                self.regions.setCurrentRow(names.index(region) + 1)

        job = worker.Job(service.redo_file, fd)
        worker.run_with_dialog(self, "다시 지우고 한글 넣기", job, done,
                               lambda m: show_failure(self, "이 카드를 다시 만들지 못했습니다", m))

    def _show_region(self):
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


class TestRunDialog(QDialog):
    """고른 파일의 몇 쪽만 시험 폴더에서 지우기 -> 한글 넣기. 프로젝트 결과는 바뀌지 않는다."""

    def __init__(self, files: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("시험 실행")
        self.setMinimumWidth(420)
        self.file = QComboBox()
        for f in files:
            self.file.addItem(f"{file_label(f['key'])} ({f['pages']}쪽)", str(f["dir"]))
        self.pages = QLineEdit()
        self.pages.setPlaceholderText("예: 2-4 또는 3,7,9")
        note = subtitle("고른 쪽만 따로 지우고 한글을 넣어 전후를 비교합니다. "
                        "프로젝트의 지운 카드·한글 카드는 바뀌지 않습니다.")
        form = QFormLayout()
        form.setVerticalSpacing(10)
        form.addRow("파일", self.file)
        form.addRow("쪽", self.pages)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        set_ok_text(bb, "시험 실행")
        self.ok_btn = bb.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_btn.setEnabled(False)  # 쪽을 넣어야 켜진다
        self.pages.textChanged.connect(lambda t: self.ok_btn.setEnabled(bool(t.strip())))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.setSpacing(10)
        lay.addWidget(page_title("시험 실행 (몇 쪽만)", 14))
        lay.addWidget(note)
        lay.addWidget(card(form))
        lay.addWidget(bb)
        self.setMinimumWidth(480)

    def accept(self):
        if not self.pages.text().strip():
            show_error(self, "시험할 쪽이 비어 있습니다", "어느 쪽을 시험할지 정하지 않았습니다.",
                       "‘쪽’ 칸에 쪽 번호를 넣으세요 (예: 2-4 또는 3,7,9).")
            return
        super().accept()

    def choice(self) -> tuple[Path, str]:
        return Path(self.file.currentData()), self.pages.text().strip()


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
        files = self._files()
        done = [f for f in files if f["erased"]]
        redo = False
        self._note = note
        if done and not auto:
            box = QMessageBox(self.page)
            box.setWindowTitle("글자 지우기")
            box.setText(f"이미 지운 파일이 {len(done)}개 있습니다 ({', '.join(f['key'] for f in done)}).")
            skip = box.addButton("지운 파일은 건너뛰기", QMessageBox.ButtonRole.AcceptRole)
            again = box.addButton("모두 다시 지우기", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("취소", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() not in (skip, again):
                return
            redo = box.clickedButton() is again
        job = worker.Job(service.erase_project, self.pdir, redo=redo, files=self.page.scope()[1])
        worker.run_with_dialog(self.page, "글자 지우기", job, self._erase_done,
                               lambda m: show_failure(self.page, "글자 지우기를 끝내지 못했습니다", m))

    def _erase_done(self, r):
        from app.qt.steps import state_label

        self.page.refresh()
        lines = [f"{f['file']}: {state_label(f['state'])}" + (f" — {f['pages']}쪽, 경고 {f['warnings']}" if "pages" in f
                                                 else f" — {f.get('reason', '')}") for f in r.files]
        note, self._note = getattr(self, "_note", ""), ""
        if note:
            lines = ["⑥ 전체 번역", note, "", "글자 지우기"] + lines
        if r.state in ("done", "skipped"):
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
        job = worker.Job(service.font_plans, self.pdir, files=self.page.scope()[1])
        worker.run_with_dialog(self.page, "글꼴 추천 받는 중", job, self._fonts_ready,
                               lambda m: show_failure(self.page, "글꼴 추천을 받지 못했습니다", m))

    def _fonts_ready(self, plans):
        from app.qt.fonts_dialog import FontPlanDialog

        if plans:  # 지운 결과가 없는 파일만 있으면 글꼴 창 없이 (한글 넣기가 이유를 알려 준다)
            dlg = FontPlanDialog(plans, self.page)
            if not dlg.exec():
                return
            for c in dlg.choices():
                service.save_font_choices(c["file_dir"], c["regions"], c["cards"], c["sentences"],
                                          c.get("sizes"), c.get("aligns"))
        self._run_compose(False)

    def _run_compose(self, accept: bool):
        job = worker.Job(service.compose_project, self.pdir, accept_issues=accept, files=self.page.scope()[1])
        worker.run_with_dialog(self.page, "한글 넣기", job, self._compose_done,
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

    # ---- 결과 보기
    def results(self):
        runs = [(f["key"], Path(f["dir"]) / "translated") for f in self._files() if f["composed"]]
        if not runs:
            QMessageBox.information(self.page, "결과 보기", "아직 한글을 넣은 파일이 없습니다")
            return
        ResultViewer(runs, self.page).exec()

    # ---- 시험 실행
    def test_run(self):
        files = [f for f in self._files()]
        if not files:
            return
        dlg = TestRunDialog(files, self.page)
        if not dlg.exec():
            return
        fd, pages = dlg.choice()
        job = worker.Job(service.test_run, fd, pages)
        worker.run_with_dialog(self.page, "시험 실행", job, self._test_done,
                               lambda m: show_failure(self.page, "시험 실행을 끝내지 못했습니다", m))

    def _test_done(self, r):
        if r.state != "done":
            show_problem(self.page, "시험 실행을 끝내지 못했습니다", r.reason or "")
            return
        ResultViewer([(f"시험: {r.run_dir.name}", r.run_dir)], self.page, title="시험 실행 결과").exec()
