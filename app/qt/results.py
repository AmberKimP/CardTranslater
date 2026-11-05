"""3차 화면: 결과 보기(원본·결과 비교, 문제 있는 쪽 걸러 보기)와 시험 실행(고른 쪽만 시험 폴더에서)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QSplitter, QVBoxLayout, QWidget,
)

from app import service
from app.qt import worker
from app.qt.dialogs import fit_to_screen

OK_STATUS = {"글자 축소"}  # 알려 주기만 하는 상태 (문제로 보지 않음)


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
            self.run.addItem(name, str(d))
        self.only = QCheckBox("문제 있는 쪽만 (넘침·아이콘 오류·글리프 없음 등)")
        self.info = QLabel()
        pdf_btn = QPushButton("PDF 열기")
        pdf_btn.clicked.connect(self.open_pdf)
        folder_btn = QPushButton("폴더 열기")
        folder_btn.clicked.connect(lambda: _open(self.run_dir()))
        top = QHBoxLayout()
        top.addWidget(QLabel("파일"))
        top.addWidget(self.run)
        top.addWidget(self.only)
        top.addStretch(1)
        top.addWidget(self.info)
        top.addWidget(pdf_btn)
        top.addWidget(folder_btn)
        self.pages = QListWidget()
        self.pages.setMaximumWidth(230)
        self.pages.currentRowChanged.connect(lambda _r: self._show_page())
        self.regions = QListWidget()
        self.regions.setMaximumHeight(150)
        self.regions.currentRowChanged.connect(lambda _r: self._show_region())
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.image)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.image.setMinimumSize(1, 1)
        self.scroll.viewport().installEventFilter(self)  # 창·칸 크기가 바뀌면 그림을 다시 맞춘다. 휠은 이전/다음 쪽
        self.scroll.setToolTip("마우스 휠: 위로 이전 쪽, 아래로 다음 쪽")
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.addWidget(self.scroll, 1)
        rl.addWidget(QLabel("영역 — 고르면 그 부분을 크게 비교합니다"))
        rl.addWidget(self.regions)
        split = QSplitter()
        split.addWidget(self.pages)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        close = QPushButton("닫기")
        close.clicked.connect(self.accept)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(split, 1)
        lay.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        self.run.currentIndexChanged.connect(lambda _i: self.reload())
        self.only.toggled.connect(lambda _c: self.reload())
        self.items: list[dict] = []
        self.reload()

    def run_dir(self) -> Path:
        return Path(self.run.currentData())

    @staticmethod
    def problems(st: list[str]) -> list[str]:
        return [s for s in st if s not in OK_STATUS]

    def reload(self):
        allp = service.result_pages(self.run_dir())
        n_bad = sum(1 for p in allp if self.problems(p["status"]))
        self.info.setText(f"{len(allp)}쪽, 문제 있는 쪽 {n_bad}")
        self.items = [p for p in allp if self.problems(p["status"])] if self.only.isChecked() else allp
        self.pages.clear()
        for p in self.items:
            it = QListWidgetItem(f"{p['page']}쪽" + (f" · {', '.join(p['status'])}" if p["status"] else ""))
            if self.problems(p["status"]):
                it.setBackground(QColor("#F4B183"))
            elif p["status"]:
                it.setBackground(QColor("#FFF2CC"))
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
        for r in p["regions"]:
            text = f"{r['name']}" + (f" · {', '.join(r['status'])}" if r["status"] else "")
            if r["errors"]:
                text += f" — {'; '.join(r['errors'])[:120]}"
            it = QListWidgetItem(text)
            if self.problems(r["status"]):
                it.setBackground(QColor("#F4B183"))
            self.regions.addItem(it)
        self.regions.setCurrentRow(0)
        self.regions.blockSignals(False)

    def _show_region(self):
        p, r = self.current(), self.regions.currentRow() - 1  # 0번 줄은 ‘카드 전체’
        if p is None:
            return
        if r < 0:
            self._set_image(p["card_png"])
        elif r < len(p["regions"]):
            self._set_image(p["regions"][r]["png"])

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
        self.file = QComboBox()
        for f in files:
            self.file.addItem(f"{f['key']} ({f['pages']}쪽)", str(f["dir"]))
        self.pages = QLineEdit()
        self.pages.setPlaceholderText("예: 2-4 또는 3,7,9")
        note = QLabel("고른 쪽만 따로 지우고 한글을 넣어 전후를 비교합니다.\n"
                      "프로젝트의 지운 카드·한글 카드는 바뀌지 않습니다.")
        note.setStyleSheet("color: #666")
        form = QFormLayout()
        form.addRow("파일", self.file)
        form.addRow("쪽", self.pages)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("시험 실행")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(note)
        lay.addWidget(bb)

    def accept(self):
        if not self.pages.text().strip():
            QMessageBox.warning(self, "쪽", "시험할 쪽을 넣으세요 (예: 2-4)")
            return
        super().accept()

    def choice(self) -> tuple[Path, str]:
        return Path(self.file.currentData()), self.pages.text().strip()


class OutputActions:
    """프로젝트 화면의 ⑧ 지우기 · ⑨ 한글 넣기 · 결과 보기 · 시험 실행."""

    def __init__(self, page):
        self.page = page

    @property
    def pdir(self) -> Path:
        return self.page.project_dir

    def _files(self) -> list[dict]:
        return [f for g in self.page.ov["groups"] for f in g["files"]]

    # ---- 지우기
    def erase(self):
        files = self._files()
        done = [f for f in files if f["erased"]]
        redo = False
        if done:
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
        job = worker.Job(service.erase_project, self.pdir, redo=redo)
        worker.run_with_dialog(self.page, "글자 지우기", job, self._erase_done,
                               lambda m: QMessageBox.critical(self.page, "오류", m[:3000]))

    def _erase_done(self, r):
        from app.qt.steps import state_label

        self.page.refresh()
        lines = [f"{f['file']}: {state_label(f['state'])}" + (f" — {f['pages']}쪽, 경고 {f['warnings']}" if "pages" in f
                                                 else f" — {f.get('reason', '')}") for f in r.files]
        (QMessageBox.information if r.state == "done" else QMessageBox.warning)(
            self.page, "글자 지우기", "\n".join(lines) + (f"\n\n{r.reason}" if r.reason else ""))

    # ---- 한글 넣기
    def compose(self, accept: bool = False):
        job = worker.Job(service.compose_project, self.pdir, accept_issues=accept)
        worker.run_with_dialog(self.page, "한글 넣기", job, self._compose_done,
                               lambda m: QMessageBox.critical(self.page, "오류", m[:3000]))

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
                               lambda m: QMessageBox.critical(self.page, "오류", m[:3000]))

    def _test_done(self, r):
        if r.state != "done":
            QMessageBox.warning(self.page, "시험 실행", r.reason)
            return
        ResultViewer([(f"시험: {r.run_dir.name}", r.run_dir)], self.page, title="시험 실행 결과").exec()
