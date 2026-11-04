"""2차 화면: 추출 → 원문 확인 → 아이콘 이름 → ‘추출 완료’(용어집 검증) → 용어집 가져오기 → 번역·검수 엑셀.

긴 작업은 worker.Job으로 작업 스레드에서 돌린다. 엔진이 사람에게 물을 일(needs_confirmation 등)은
결과 상태로 돌려주므로, 여기서 확인 창을 띄우고 ‘계속’이면 다시 부른다 (PRD 6장).
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QRadioButton, QScrollArea, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from app import service
from app.qt import worker

MODE_LABELS = [("double-check", "두 번 확인 (Sonnet으로 읽고 불확실한 칸만 Opus로 다시) — 추천"),
               ("opus", "Opus만 사용")]
ENGINE_LABELS = [("claude-code", "Claude Code 요금제 (키 필요 없음)"), ("api", "API 키 (사용료)")]
LABEL_COLORS = {"결론 못 냄": "#F4B183", "두 번 확인": "#D9EAD3", "읽지 않음": "#EEEEEE", "사람이 고침": "#CFE2F3"}


def _ask_list(parent, title: str, intro: str, items: list[str], question: str) -> bool:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Icon.Question)
    box.setText(intro)
    box.setInformativeText(question)
    box.setDetailedText("\n".join(items))
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    return box.exec() == QMessageBox.StandardButton.Yes


def _fail(parent, msg: str):
    QMessageBox.critical(parent, "오류", msg[:3000])


# ================================================================ 추출
class ExtractDialog(QDialog):
    """추출할 그룹·모드·엔진 고르기."""

    def __init__(self, groups: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("글자 추출")
        self.group = QComboBox()
        self.group.addItem("모든 그룹", None)
        for g in groups:
            self.group.addItem(g, g)
        self.modes = QButtonGroup(self)
        mode_box = QVBoxLayout()
        for i, (k, t) in enumerate(MODE_LABELS):
            rb = QRadioButton(t)
            rb.setProperty("key", k)
            rb.setChecked(i == 0)
            self.modes.addButton(rb)
            mode_box.addWidget(rb)
        self.engine = QComboBox()
        st = service.engine_status()
        for k, t in ENGINE_LABELS:
            self.engine.addItem(t + ("" if st[k] else " — 사용할 수 없음"), k)
        self.engine.setCurrentIndex(max(0, self.engine.findData(service.get_settings().engine)))
        note = QLabel("이미 읽은 카드는 다시 읽지 않습니다. 사용 한도로 멈추면 다시 시작한 곳부터 이어 갑니다.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #666")
        form = QFormLayout()
        form.addRow("그룹", self.group)
        form.addRow("모드", mode_box)
        form.addRow("엔진", self.engine)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("추출 시작")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(note)
        lay.addWidget(bb)

    def choice(self) -> tuple[str | None, str, str]:
        return self.group.currentData(), self.modes.checkedButton().property("key"), self.engine.currentData()

    def accept(self):
        if not service.engine_status()[self.engine.currentData()]:
            QMessageBox.warning(self, "엔진을 쓸 수 없음", service.engine_status()["message"])
            return
        super().accept()


# ================================================================ 원문 확인
class SourceReviewDialog(QDialog):
    """파일의 추출 결과를 칸마다 보고, 잘못 읽은 원문을 이미지와 비교해 고친다."""

    COLS = ["쪽", "영역", "원문 확인", "원문"]

    def __init__(self, files: list[dict], parent=None, only_check: bool = True):
        super().__init__(parent)
        self.setWindowTitle("원문 확인")
        self.resize(1300, 820)
        self.files = files
        self.file = QComboBox()
        for f in files:
            self.file.addItem(f["key"], str(f["dir"]))
        self.only = QCheckBox("확인할 칸만 (결론 못 냄·확인 필요·읽지 않음)")
        self.only.setChecked(only_check)
        self.count = QLabel()
        top = QHBoxLayout()
        top.addWidget(QLabel("파일"))
        top.addWidget(self.file)
        top.addWidget(self.only)
        top.addStretch(1)
        top.addWidget(self.count)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.currentCellChanged.connect(lambda *_a: self._show())
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setMinimumHeight(160)
        scroll = QScrollArea()
        scroll.setWidget(self.image)
        scroll.setWidgetResizable(True)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.edit = QPlainTextEdit()
        self.edit.setPlaceholderText("원문 (줄바꿈 그대로. 태그 칸은 한 줄에 하나. 아이콘은 [[이름]])")
        self.save_btn = QPushButton("고친 원문 저장")
        self.save_btn.clicked.connect(self.save_current)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.addWidget(scroll, 2)
        rl.addWidget(self.info)
        rl.addWidget(self.edit, 1)
        rl.addWidget(self.save_btn)
        split = QSplitter()
        split.addWidget(self.table)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        close = QPushButton("닫기")
        close.clicked.connect(self.accept)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(split, 1)
        lay.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        self.file.currentIndexChanged.connect(lambda _i: self.reload())
        self.only.toggled.connect(lambda _c: self.reload())
        self.cells: list[dict] = []
        self.saved = 0
        self.reload()

    @staticmethod
    def needs_check(c: dict) -> bool:
        return c["uncertain"] or c["label"] in ("결론 못 냄", "읽지 않음")

    def reload(self, keep: tuple | None = None):
        fd = self.file.currentData()
        allc = service.extracted_cells(fd)
        self.cells = [c for c in allc if self.needs_check(c)] if self.only.isChecked() else allc
        n_check = sum(1 for c in allc if self.needs_check(c))
        self.count.setText(f"확인할 칸 {n_check} / 전체 {len(allc)}")
        self.table.setRowCount(len(self.cells))
        for i, c in enumerate(self.cells):
            src = c["source"]
            text = "\n".join(src) if isinstance(src, list) else (src or "")
            vals = [str(c["page"]), c["region"], c["label"], text.replace("\n", " / ")]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                color = LABEL_COLORS.get(c["label"]) or ("#FFE066" if c["uncertain"] else None)
                if color and j == 2:
                    it.setBackground(QColor(color))
                self.table.setItem(i, j, it)
        self.table.resizeColumnsToContents()
        row = 0
        if keep:
            row = next((i for i, c in enumerate(self.cells) if (c["page"], c["region"]) == keep), 0)
        if self.cells:
            self.table.setCurrentCell(row, 0)
        else:
            self.image.clear()
            self.info.setText("확인할 칸이 없습니다")
            self.edit.clear()
        self.save_btn.setEnabled(bool(self.cells))

    def current(self) -> dict | None:
        r = self.table.currentRow()
        return self.cells[r] if 0 <= r < len(self.cells) else None

    def _show(self):
        c = self.current()
        if c is None:
            return
        pm = QPixmap()
        pm.loadFromData(service.region_crop(self.file.currentData(), c["page"], c["region"]), "PNG")
        room = max(300, self.image.parentWidget().width() - 24) if self.image.parentWidget() else 800
        if pm.width() > room:  # 오른쪽 칸 너비에 맞춘다
            pm = pm.scaledToWidth(room, Qt.TransformationMode.SmoothTransformation)
        self.image.setPixmap(pm)
        lines = [f"{c['page']}쪽 · {c['region']} · {c['label']}"]
        for rd in c["readings"]:
            s = rd["source"]
            lines.append(f"{rd['model']}: {' / '.join(s) if isinstance(s, list) else s}")
        if c["note"]:
            lines.append(c["note"])
        self.info.setText("\n".join(lines))
        src = c["source"]
        self.edit.setPlainText("\n".join(src) if isinstance(src, list) else (src or ""))

    def save_current(self):
        c = self.current()
        if c is None:
            return
        text = self.edit.toPlainText().strip("\n")
        value = [t.strip() for t in text.split("\n") if t.strip()] if c["kind"] == "tag_pills" else text
        service.save_source(self.file.currentData(), c["page"], c["region"], value)
        self.saved += 1
        self.reload(keep=(c["page"], c["region"]))


# ================================================================ 아이콘 이름
class IconReviewDialog(QDialog):
    """새로 찾은 아이콘 묶음의 이름·뜻·한국어 이름을 정한다 (엑셀 대신).

    ‘같은 아이콘’ 칸에서 다른 줄(번호)이나 사전에 있는 아이콘을 고르면 그 아이콘과 하나로 합친다
    (잘못 잘렸거나 작게 찍혀 따로 묶인 것). ‘카드 보기’로 그 아이콘이 나온 카드 자리를 확인한다."""

    COLS = ["그림", "같은 아이콘", "이름", "뜻(원문)", "한국어 이름", "아이콘 아님", "표시 크기", "등장", "카드", "예문"]
    NEW = "(새 아이콘)"

    def __init__(self, project_dir: Path, review: dict, parent=None):
        super().__init__(parent)
        self.project_dir = Path(project_dir)
        self.review = review
        self.result = None
        self.setWindowTitle("아이콘 이름 정하기")
        self.resize(1400, 640)
        groups = review["groups"]
        self.table = QTableWidget(len(groups), len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setIconSize(QSize(40, 40))
        self.table.horizontalHeader().setSectionResizeMode(9, QHeaderView.ResizeMode.Stretch)
        self.widgets = []
        for i, g in enumerate(groups):
            img = QTableWidgetItem("" if g["image"] else "위치 못 찾음")
            if g["image"]:
                img.setIcon(QIcon(QPixmap(g["image"])))
            elif g.get("reason"):
                img.setToolTip(g["reason"])
            self.table.setItem(i, 0, img)
            same = QComboBox()
            same.addItem(self.NEW, None)
            for j, other in enumerate(groups):
                if j != i and other["image"]:
                    same.addItem(QIcon(QPixmap(other["image"])), f"{j + 1}번과 같음", ("row", j))
            for nm in review["known"]:
                same.addItem(f"사전: {nm}", ("known", nm))
            same.setIconSize(QSize(24, 24))
            name, meaning, ko = QLineEdit(g["suggest"]), QLineEdit(g["meaning"]), QLineEdit(g["ko"])
            name.setPlaceholderText("영문 소문자·숫자·_")
            not_icon = QCheckBox()
            scale = QDoubleSpinBox()
            scale.setRange(0.3, 3.0)
            scale.setSingleStep(0.05)
            scale.setValue(1.1)
            view = QPushButton("카드 보기")
            view.clicked.connect(lambda _c=False, gg=g: self.show_cards(gg))
            for j, w in ((1, same), (2, name), (3, meaning), (4, ko), (5, not_icon), (6, scale), (8, view)):
                self.table.setCellWidget(i, j, w)
            self.table.setItem(i, 7, QTableWidgetItem(str(g["count"])))
            ex = QTableWidgetItem(g["examples"][0] if g["examples"] else "")
            ex.setToolTip("\n".join(g["examples"]))
            self.table.setItem(i, 9, ex)
            self.table.setRowHeight(i, 46)
            same.currentIndexChanged.connect(lambda _x, r=i: self._sync_row(r))
            not_icon.toggled.connect(lambda _x, r=i: self._sync_row(r))
            self.widgets.append((g["group"], name, meaning, ko, not_icon, scale, same))
        self.table.resizeColumnToContents(1)
        known = QLabel("왼쪽 번호가 줄 번호입니다. ‘같은 아이콘’에서 다른 줄이나 사전 아이콘을 고르면 하나로 합칩니다 "
                       "(따로 묶인 같은 아이콘·잘못 잘린 조각). 정말 아이콘이 아니면 ‘아이콘 아님’(원문에서 뺌).\n"
                       "사전에 이미 있는 이름: " + (", ".join(review["known"]) or "(없음)"))
        known.setWordWrap(True)
        known.setStyleSheet("color: #666")
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("확정")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(self.table, 1)
        lay.addWidget(known)
        lay.addWidget(bb)

    def _sync_row(self, r: int):
        _g, name, meaning, ko, not_icon, scale, same = self.widgets[r]
        merged = same.currentData() is not None
        for w in (name, meaning, ko, scale):
            w.setEnabled(not merged and not not_icon.isChecked())
        same.setEnabled(not not_icon.isChecked())
        not_icon.setEnabled(not merged)

    def show_cards(self, g: dict):
        """그 묶음이 나온 카드 자리(영역 이미지)를 차례로 보여 준다."""
        dlg = QDialog(self)
        dlg.setWindowTitle("카드 보기")
        dlg.resize(1000, 640)
        combo = QComboBox()
        for w in g.get("where", []):
            combo.addItem(w["label"], w)
        pic = QLabel()
        pic.setAlignment(Qt.AlignmentFlag.AlignCenter)
        crop = QLabel()
        crop.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scroll = QScrollArea()
        scroll.setWidget(pic)
        scroll.setWidgetResizable(True)

        def show(_i=0):
            w = combo.currentData()
            if not w:
                return
            pm = QPixmap()
            pm.loadFromData(service.region_crop(w["file_dir"], w["page"], w["region"]), "PNG")
            if pm.width() > 940:
                pm = pm.scaledToWidth(940, Qt.TransformationMode.SmoothTransformation)
            pic.setPixmap(pm)
            if w.get("png"):
                crop.setPixmap(QPixmap(w["png"]).scaledToHeight(64, Qt.TransformationMode.SmoothTransformation))
                crop.setToolTip("이 자리에서 잘라 낸 그림")
            else:
                crop.setText("이 자리에서는 아이콘 위치를 찾지 못했습니다")

        combo.currentIndexChanged.connect(show)
        close = QPushButton("닫기")
        close.clicked.connect(dlg.accept)
        lay = QVBoxLayout(dlg)
        lay.addWidget(combo)
        lay.addWidget(scroll, 1)
        lay.addWidget(QLabel("잘라 낸 그림:"))
        lay.addWidget(crop)
        lay.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        show()
        dlg.exec()

    def rows(self) -> list[dict]:
        """합치기를 풀어 각 줄의 최종 이름으로. 합치기가 돌고 돌면 ValueError."""
        final: dict[int, str] = {}

        def resolve(i: int, seen: tuple = ()) -> str:
            if i in final:
                return final[i]
            if i in seen:
                raise ValueError(f"{i + 1}번 줄의 ‘같은 아이콘’이 서로를 가리킵니다")
            _g, name, _m, _k, _ni, _sc, same = self.widgets[i]
            d = same.currentData()
            if d is None:
                v = name.text().strip()
            elif d[0] == "known":
                v = d[1]
            else:
                if self.widgets[d[1]][4].isChecked():
                    raise ValueError(f"{i + 1}번 줄이 ‘아이콘 아님’인 {d[1] + 1}번과 같다고 되어 있습니다")
                v = resolve(d[1], seen + (i,))
            final[i] = v
            return v

        out = []
        for i, (g, n, m, k, ni, sc, same) in enumerate(self.widgets):
            target = same.currentData()
            src = self.widgets[target[1]] if target and target[0] == "row" else None
            out.append({"group": g, "name": "" if ni.isChecked() else resolve(i),
                        "meaning": (src[2] if src else m).text().strip(), "ko": (src[3] if src else k).text().strip(),
                        "not_icon": ni.isChecked(), "scale": (src[5] if src else sc).value()})
        return out

    def accept(self):
        try:
            rows = self.rows()
        except ValueError as e:
            QMessageBox.warning(self, "확정할 수 없음", str(e))
            return
        r = service.confirm_icons(self.project_dir, rows)
        if r.state != "confirmed":
            QMessageBox.warning(self, "확정할 수 없음", "\n".join(r.blocking))
            return
        self.result = r
        super().accept()


# ================================================================ 흐름 (프로젝트 화면의 버튼)
class StepActions:
    """프로젝트 화면 버튼이 부르는 2단계 흐름. page: ProjectPage (project_dir, ov, refresh)."""

    def __init__(self, page):
        self.page = page
        self.resume_timer: QTimer | None = None

    @property
    def pdir(self) -> Path:
        return self.page.project_dir

    def _engine(self) -> str | None:
        eng = service.get_settings().engine
        if not service.engine_status()[eng]:
            QMessageBox.warning(self.page, "엔진을 쓸 수 없음",
                                service.engine_status()["message"] + "\n파일 > 설정에서 엔진을 바꿀 수 있습니다.")
            return None
        return eng

    # ---- 추출
    def extract(self):
        groups = [g["name"] for g in self.page.ov["groups"]]
        dlg = ExtractDialog(groups, self.page)
        if not dlg.exec():
            return
        self._run_extract(*dlg.choice())

    def _run_extract(self, group, mode, engine):
        job = worker.Job(service.extract_project, self.pdir, group, mode=mode, engine=engine)
        worker.run_with_dialog(self.page, "글자 추출", job,
                               lambda r: self._extract_done(r, (group, mode, engine)),
                               lambda m: _fail(self.page, m))

    def _extract_done(self, r, args):
        self.page.refresh()
        lines = [f"{f['file']}: {f['state']} — 읽음 {f['done']}장, 남음 {f['remaining']}장, 실패 {f['failed']}장"
                 + (f", 두 번 확인 같음 {f['recheck'][0]}·다름 {f['recheck'][1]}" if f.get("recheck") else "")
                 for f in r.files]
        if r.state == "done":
            n_check = sum(1 for g in self.page.ov["groups"] for f in g["files"]
                          for c in service.extracted_cells(f["dir"]) if SourceReviewDialog.needs_check(c))
            QMessageBox.information(self.page, "추출 끝", "\n".join(lines)
                                    + (f"\n\n확인할 칸 {n_check}개 — ‘원문 확인’에서 이미지와 비교해 고치세요."
                                       if n_check else "\n\n확인할 칸이 없습니다."))
            return
        if r.state == "paused" and r.retry_after:
            when = _dt.datetime.strptime(r.retry_after, "%Y-%m-%d %H:%M:%S")
            if _ask_list(self.page, "추출 멈춤", f"{r.reason}", lines,
                         f"{r.retry_after} 이후 이어 할 수 있습니다. 앱을 켜 둔 채 그때 자동으로 이어 할까요?"):
                ms = max(0, int((when - _dt.datetime.now()).total_seconds() * 1000)) + 60_000
                self.resume_timer = QTimer(self.page)
                self.resume_timer.setSingleShot(True)
                self.resume_timer.timeout.connect(lambda: self._run_extract(*args))
                self.resume_timer.start(ms)
                self.page.win.statusBar().showMessage(f"추출을 {r.retry_after} 이후 자동으로 이어 합니다")
            return
        QMessageBox.warning(self.page, f"추출 {r.state}", (r.reason or "") + "\n\n" + "\n".join(lines))

    # ---- 원문 확인
    def review_sources(self, only_check: bool = True):
        files = [f for g in self.page.ov["groups"] for f in g["files"]]
        if not files:
            return
        dlg = SourceReviewDialog(files, self.page, only_check)
        dlg.exec()
        if dlg.saved:
            self.page.refresh()

    # ---- 아이콘
    def icons(self):
        """누를 때마다 다시 찾는다 (원문을 고쳤거나 찾기 방식이 나아졌을 수 있으므로 예전 결과를 쓰지 않음)."""
        eng = self._engine()
        if eng is None:
            return
        job = worker.Job(service.find_icons, self.pdir, engine=eng, with_progress=False)
        worker.run_with_dialog(self.page, "아이콘 찾는 중 (이름 제안 포함)", job, self._icons_found,
                               lambda m: _fail(self.page, m), cancellable=False)

    def _icons_found(self, r):
        msg = ("사전과 같아 바로 바꿈: " + ", ".join(f"[[{n}]] {c}곳" for n, c in r.auto.items())) if r.auto else ""
        if r.state == "done":
            QMessageBox.information(self.page, "아이콘", (msg + "\n" if msg else "") + "이름을 정할 아이콘이 없습니다.")
            return
        self._open_icon_review(service.icon_review(self.pdir))

    def _open_icon_review(self, review) -> bool:
        dlg = IconReviewDialog(self.pdir, review, self.page)
        if dlg.exec() and dlg.result:
            r = dlg.result
            QMessageBox.information(self.page, "아이콘 확정",
                                    f"새로 넣음: {', '.join(r.added) or '-'}\n사전에 있던 것: {', '.join(r.reused) or '-'}\n"
                                    f"원문에서 바꿈 {r.replaced}곳, 뺌 {r.removed}곳")
            self.page.refresh()
            return True
        return False

    # ---- 추출 완료 -> 용어집 검증
    def finish(self, accept: bool = False):
        eng = self._engine()
        if eng is None:
            return
        job = worker.Job(service.finish_extraction, self.pdir, accept_issues=accept, engine=eng, with_progress=False)
        worker.run_with_dialog(self.page, "용어 후보를 만드는 중 (몇 분 걸릴 수 있음)", job, self._finish_done,
                               lambda m: _fail(self.page, m), cancellable=False)

    def _finish_done(self, r):
        self.page.refresh()
        if r.state == "needs_confirmation":
            if _ask_list(self.page, "추출 완료 확인", "아래 항목이 남아 있습니다.", r.issues, "그래도 용어집을 만들까요?"):
                self.finish(accept=True)
            return
        if r.state == "icons_review":
            QMessageBox.information(self.page, "아이콘 먼저", f"먼저 아이콘 이름을 정해야 합니다: {r.reason}")
            review = service.icon_review(self.pdir)
            if review and self._open_icon_review(review):
                self.finish(accept=True)
            return
        if r.state in ("blocked", "paused"):
            QMessageBox.warning(self.page, f"용어집 {r.state}", (r.reason or "") +
                                (f"\n{r.retry_after} 이후 다시 하세요." if r.retry_after else ""))
            return
        box = QMessageBox(self.page)
        box.setWindowTitle("용어집 검증")
        box.setText(f"새 용어 후보 {r.new_terms}개, 이미 확정된 용어 {r.confirmed_terms}개.\n"
                    f"엑셀의 ‘번역’ 열을 확인·수정한 뒤 ‘용어집 가져오기’를 누르세요.\n(보라색: 번역이 갈릴 수 있는 용어)")
        openb = box.addButton("엑셀 열기", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("닫기", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is openb and r.review_xlsx:
            from app.qt.main import open_path

            open_path(r.review_xlsx)

    # ---- 용어집 가져오기
    def import_glossary(self, path: str | None = None, accept: bool = False):
        if path is None:
            default = str(self.pdir / "용어집_검증.xlsx")
            path, _ = QFileDialog.getOpenFileName(self.page, "검증한 용어집 엑셀", default, "Excel (*.xlsx)")
            if not path:
                return
        r = service.import_glossary(self.pdir, path, accept_issues=accept)
        if r.state == "blocked":
            QMessageBox.warning(self.page, "가져올 수 없음", "\n".join(r.blocking + r.issues))
            return
        if r.state == "needs_confirmation":
            if _ask_list(self.page, "용어집 확인", "아래 항목을 확인하세요.", r.issues, "이대로 확정할까요?"):
                self.import_glossary(path, accept=True)
            return
        self.page.refresh()
        QMessageBox.information(self.page, "용어집 확정",
                                f"게임 용어집 버전 {r.version}, 용어 {r.terms}개."
                                + (f"\n번역이 바뀐 용어: {', '.join(r.changed)}" if r.changed else "")
                                + "\n이제 번역을 시작할 수 있습니다.")

    # ---- 번역
    def translate(self):
        eng = self._engine()
        if eng is None:
            return
        box = QMessageBox(self.page)
        box.setWindowTitle("번역")
        box.setText("확정 용어집을 기준으로 모든 파일의 원문을 번역합니다. 이미 번역한 칸은 다시 하지 않습니다.")
        sonnet = box.addButton("Sonnet (추천)", QMessageBox.ButtonRole.AcceptRole)
        opus = box.addButton("Opus", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("취소", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        model = {sonnet: "sonnet", opus: "opus"}.get(box.clickedButton())
        if not model:
            return
        job = worker.Job(service.translate_project, self.pdir, model=model, engine=eng)
        worker.run_with_dialog(self.page, "번역", job, self._translate_done, lambda m: _fail(self.page, m))

    def _translate_done(self, r):
        self.page.refresh()
        if r.state != "done":
            QMessageBox.warning(self.page, f"번역 {r.state}", (r.reason or "")
                                + (f"\n{r.retry_after} 이후 다시 하면 이어 갑니다." if r.retry_after else ""))
            return
        issues = []
        if r.term_mismatch:
            issues.append(f"용어 불일치 {len(r.term_mismatch)}칸")
        if r.icon_mismatch:
            issues.append(f"아이콘 불일치 {len(r.icon_mismatch)}칸")
        if r.leftover:
            issues.append(f"일본어가 남은 번역 {len(r.leftover)}칸")
        if QMessageBox.question(self.page, "번역 끝",
                                f"서로 다른 원문 {r.cells}칸: 이번에 번역 {r.translated}, 용어집으로 바로 {r.from_glossary}, "
                                f"이미 번역됨 {r.cached}.\n" + ("확인: " + ", ".join(issues) + "\n" if issues else "")
                                + "\n파일마다 번역 검수 엑셀을 만들까요? (사람이 고친 칸은 그대로 둡니다)") \
                == QMessageBox.StandardButton.Yes:
            self.export_excel()

    def export_excel(self):
        job = worker.Job(service.export_project_excel, self.pdir, force=True, with_progress=False)
        worker.run_with_dialog(self.page, "번역 검수 엑셀 만드는 중", job, self._excel_done,
                               lambda m: _fail(self.page, m), cancellable=False)

    def _excel_done(self, paths):
        self.page.refresh()
        QMessageBox.information(self.page, "번역 검수 엑셀",
                                "만들었습니다:\n" + "\n".join(str(p) for p in paths)
                                + "\n\n파일을 고르고 ‘폴더 열기’로 열 수 있습니다.")
