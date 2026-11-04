"""영역 지정 화면 (PRD 2단계). 카드 위에 사각형을 그리고 이름·종류·서식을 정해 그룹의 regions.json에 저장한다.

- 빈 곳을 끌면 새 영역, 영역 안을 끌면 옮기기, 가장자리를 끌면 크기 조절.
- 화살표 키로 1px(Shift: 10px) 옮기기, Alt+화살표로 크기 조절, Delete로 지우기, Ctrl+휠로 확대.
- 좌표는 카드 대비 비율로 저장한다. 손으로 쓴 regions.json의 모르는 항목은 그대로 둔다.
"""

from __future__ import annotations

import copy
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QGraphicsRectItem,
    QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QToolButton, QVBoxLayout, QWidget,
)

from app import service

KIND_LABELS = [("text", "글자"), ("tag_pills", "태그 라벨"), ("protect", "보호 영역(건드리지 않음)")]
ALIGN_LABELS = [("left", "왼쪽"), ("center", "가운데"), ("right", "오른쪽")]
COLORS = {"text": QColor(30, 110, 230), "tag_pills": QColor(150, 60, 200), "protect": QColor(220, 40, 40)}
EDGE = 6.0  # 가장자리로 보는 거리 (화면 px)
MIN_SIZE = 4.0


class RegionItem(QGraphicsRectItem):
    """영역 하나. rect는 장면(쪽 이미지 px) 좌표로 직접 들고 있다 (pos는 늘 0)."""

    def __init__(self, rect: QRectF, data: dict, protect: bool = False):
        super().__init__(rect)
        self.data = data  # 원래 regions.json 항목 (모르는 키 보존)
        self.kind = "protect" if protect else data.get("kind", "text")
        self.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)
        self.label = QGraphicsSimpleTextItem(self)
        self.label.setFont(QFont("Malgun Gothic", 9))
        self._drag = None  # (모드, 시작점, 시작 rect)
        self.refresh()

    # ---- 표시
    @property
    def name(self) -> str:
        return self.data.get("name", "")

    def refresh(self) -> None:
        c = COLORS.get(self.kind, COLORS["text"])
        pen = QPen(c, 3 if self.isSelected() else 2)
        pen.setCosmetic(True)
        if self.kind == "protect":
            pen.setStyle(Qt.PenStyle.DashLine)
        self.setPen(pen)
        fill = QColor(c)
        fill.setAlpha(45 if self.isSelected() else 25)
        self.setBrush(QBrush(fill))
        rot = " ↻180" if int((self.data.get("style") or {}).get("rotate", 0) or 0) == 180 else ""
        self.label.setText(f"{self.name}{rot}")
        self.label.setBrush(QBrush(c.darker(130)))
        r = self.rect()
        self.label.setPos(r.left() + 2, r.top() - 16 if r.top() > 18 else r.top() + 2)

    def paint(self, painter, option, widget=None):
        from PySide6.QtWidgets import QStyle

        option.state &= ~QStyle.StateFlag.State_Selected  # Qt 기본 점선 선택 테두리 대신 굵은 테두리
        super().paint(painter, option, widget)

    def itemChange(self, change, value):
        if change == QGraphicsRectItem.GraphicsItemChange.ItemSelectedHasChanged:
            try:
                self.refresh()
            except RuntimeError:  # 장면이 이미 지워지는 중 (창을 닫을 때)
                pass
        return super().itemChange(change, value)

    # ---- 끌기 (옮기기·크기 조절)
    def _edges(self, p: QPointF) -> str:
        view = self.scene().views()[0] if self.scene() and self.scene().views() else None
        tol = EDGE / (view.transform().m11() if view else 1.0)
        r = self.rect()
        e = ""
        if abs(p.y() - r.top()) <= tol:
            e += "t"
        elif abs(p.y() - r.bottom()) <= tol:
            e += "b"
        if abs(p.x() - r.left()) <= tol:
            e += "l"
        elif abs(p.x() - r.right()) <= tol:
            e += "r"
        return e

    def hoverMoveEvent(self, ev):
        e = self._edges(ev.pos())
        cur = {"t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
               "l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor,
               "tl": Qt.CursorShape.SizeFDiagCursor, "br": Qt.CursorShape.SizeFDiagCursor,
               "tr": Qt.CursorShape.SizeBDiagCursor, "bl": Qt.CursorShape.SizeBDiagCursor}.get(
            e, Qt.CursorShape.SizeAllCursor)
        self.setCursor(cur)
        super().hoverMoveEvent(ev)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.scene().clearSelection()
            self.setSelected(True)
            self._drag = (self._edges(ev.pos()) or "move", ev.pos(), QRectF(self.rect()))
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if not self._drag:
            return super().mouseMoveEvent(ev)
        mode, start, r0 = self._drag
        d = ev.pos() - start
        r = QRectF(r0)
        if mode == "move":
            r.translate(d)
        else:
            if "l" in mode:
                r.setLeft(min(r0.left() + d.x(), r0.right() - MIN_SIZE))
            if "r" in mode:
                r.setRight(max(r0.right() + d.x(), r0.left() + MIN_SIZE))
            if "t" in mode:
                r.setTop(min(r0.top() + d.y(), r0.bottom() - MIN_SIZE))
            if "b" in mode:
                r.setBottom(max(r0.bottom() + d.y(), r0.top() + MIN_SIZE))
        self.set_scene_rect(r)
        ev.accept()

    def mouseReleaseEvent(self, ev):
        if self._drag:
            self.mouseMoveEvent(ev)  # 놓은 자리까지 반영
            self._drag = None
            sc = self.scene()
            if hasattr(sc, "changed_item"):
                sc.changed_item.emit(self)
            ev.accept()
            return
        super().mouseReleaseEvent(ev)

    def set_scene_rect(self, r: QRectF) -> None:
        """쪽 이미지 밖으로 나가지 않게 맞춰 놓는다."""
        sc = self.scene()
        bounds = sc.page_rect if sc is not None and hasattr(sc, "page_rect") else None
        r = r.normalized()
        if bounds is not None and not bounds.isEmpty():
            if r.width() > bounds.width():
                r.setWidth(bounds.width())
            if r.height() > bounds.height():
                r.setHeight(bounds.height())
            dx = max(bounds.left() - r.left(), 0) - max(r.right() - bounds.right(), 0)
            dy = max(bounds.top() - r.top(), 0) - max(r.bottom() - bounds.bottom(), 0)
            r.translate(dx, dy)
        self.setRect(r)
        self.refresh()


class PageScene(QGraphicsScene):
    changed_item = Signal(object)  # 옮기거나 크기를 바꾼 영역
    created_item = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.page_rect = QRectF()
        self.add_kind: str | None = None  # 그리기 모드: text | tag_pills | protect | None
        self._new: RegionItem | None = None
        self._start = QPointF()

    def mousePressEvent(self, ev):
        item = self.itemAt(ev.scenePos(), self.views()[0].transform()) if self.views() else None
        target = item if isinstance(item, RegionItem) else (
            item.parentItem() if item is not None and isinstance(item.parentItem(), RegionItem) else None)
        if target is None and self.add_kind and ev.button() == Qt.MouseButton.LeftButton \
                and self.page_rect.contains(ev.scenePos()):
            self.clearSelection()
            self._start = ev.scenePos()
            kind = self.add_kind
            data = {"name": "", "kind": "text" if kind == "protect" else kind}
            self._new = RegionItem(QRectF(self._start, self._start), data, protect=kind == "protect")
            self.addItem(self._new)
            ev.accept()
            return
        if target is None:
            self.clearSelection()
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._new is not None:
            self._new.set_scene_rect(QRectF(self._start, ev.scenePos()))
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self._new is not None:
            self._new.set_scene_rect(QRectF(self._start, ev.scenePos()))
            item, self._new = self._new, None
            r = item.rect()
            if r.width() < MIN_SIZE * 2 or r.height() < MIN_SIZE * 2:
                self.removeItem(item)
            else:
                item.setSelected(True)
                self.created_item.emit(item)
            ev.accept()
            return
        super().mouseReleaseEvent(ev)


class PageView(QGraphicsView):
    nudged = Signal(object)
    delete_pressed = Signal()

    def __init__(self, scene: PageScene):
        super().__init__(scene)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, ev):
        if ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
            f = 1.15 if ev.angleDelta().y() > 0 else 1 / 1.15
            self.scale(f, f)
            ev.accept()
            return
        super().wheelEvent(ev)

    def fit(self):
        sc = self.scene()
        if isinstance(sc, PageScene) and not sc.page_rect.isEmpty():
            self.fitInView(sc.page_rect, Qt.AspectRatioMode.KeepAspectRatio)

    def keyPressEvent(self, ev):
        items = [i for i in self.scene().selectedItems() if isinstance(i, RegionItem)]
        k = ev.key()
        if items and k in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            step = 10.0 if ev.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1.0
            dx = {Qt.Key.Key_Left: -step, Qt.Key.Key_Right: step}.get(k, 0.0)
            dy = {Qt.Key.Key_Up: -step, Qt.Key.Key_Down: step}.get(k, 0.0)
            for it in items:
                r = QRectF(it.rect())
                if ev.modifiers() & Qt.KeyboardModifier.AltModifier:  # 크기 조절
                    r.setWidth(max(MIN_SIZE, r.width() + dx))
                    r.setHeight(max(MIN_SIZE, r.height() + dy))
                else:
                    r.translate(dx, dy)
                it.set_scene_rect(r)
                self.nudged.emit(it)
            ev.accept()
            return
        if items and k in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_pressed.emit()
            ev.accept()
            return
        super().keyPressEvent(ev)


class RegionEditor(QDialog):
    """그룹의 영역 템플릿 편집기. 저장하면 saved 신호."""

    saved = Signal()

    def __init__(self, group_dir: Path, file_dirs: list[Path], parent: QWidget | None = None,
                 groups: list[dict] | None = None):
        """groups: 프로젝트의 모든 그룹 [{"name", "dir", "files": [파일 폴더]}]. 주면 파일 목록에서 다른 그룹의
        파일을 골라 그 그룹의 영역으로 옮겨 갈 수 있다."""
        super().__init__(parent)
        self.group_dir = Path(group_dir)
        self.file_dirs = [Path(f) for f in file_dirs]
        self.groups = [{"name": g["name"], "dir": Path(g["dir"]), "files": [Path(f) for f in g["files"]]}
                       for g in (groups or []) if g["files"]] or [
            {"name": self.group_dir.name, "dir": self.group_dir, "files": self.file_dirs}]
        self.template = service.load_region_template(self.group_dir)
        self.fonts = service.font_choices()
        self.dirty = False
        self._loading = False
        self.setWindowTitle(f"영역 지정 — {self.group_dir.name}")
        self.resize(1400, 900)

        self.scene = PageScene(self)
        self.view = PageView(self.scene)
        self.pix_item = None
        self._build_ui()
        self._load_page()
        self._load_regions()
        self.scene.selectionChanged.connect(self._on_selection)
        self.scene.changed_item.connect(self._on_geometry)
        self.scene.created_item.connect(self._on_created)
        self.view.nudged.connect(self._on_geometry)
        self.view.delete_pressed.connect(self.delete_selected)
        QShortcut(QKeySequence.StandardKey.Save, self, activated=self.save)

    # ------------------------------------------------------------ 화면 구성
    def _build_ui(self):
        top = QHBoxLayout()
        top.addWidget(QLabel("그룹 / 파일"))
        self.file_combo = QComboBox()
        self.file_combo.setMinimumWidth(260)
        for g in self.groups:  # 모든 그룹의 파일. 다른 그룹 파일을 고르면 그 그룹 영역으로 옮겨 간다
            for fd in g["files"]:
                self.file_combo.addItem(f"{g['name']} / {fd.name}", (str(g["dir"]), str(fd)))
        cur = next((i for i in range(self.file_combo.count())
                    if Path(self.file_combo.itemData(i)[0]) == self.group_dir), 0)
        self.file_combo.setCurrentIndex(cur)
        self._combo_index = cur
        top.addWidget(self.file_combo)
        top.addWidget(QLabel("쪽"))
        self.page_spin = QSpinBox()
        self.page_spin.setMinimum(1)
        top.addWidget(self.page_spin)
        self.page_total = QLabel()
        top.addWidget(self.page_total)
        top.addSpacing(16)
        self.add_buttons = {}
        for kind, text in (("text", "+ 글자 영역"), ("tag_pills", "+ 태그 라벨 영역"), ("protect", "+ 보호 영역")):
            b = QToolButton()
            b.setText(text)
            b.setCheckable(True)
            b.clicked.connect(lambda checked, k=kind: self._set_add_kind(k if checked else None))
            self.add_buttons[kind] = b
            top.addWidget(b)
        top.addStretch(1)
        fit = QPushButton("화면에 맞춤")
        fit.clicked.connect(self.view.fit)
        top.addWidget(fit)
        self.save_btn = QPushButton("저장 (Ctrl+S)")
        self.save_btn.clicked.connect(self.save)
        top.addWidget(self.save_btn)
        close = QPushButton("닫기")
        close.clicked.connect(self.close)
        top.addWidget(close)

        side = QWidget()
        sl = QVBoxLayout(side)
        sl.addWidget(QLabel("영역 (위에서부터 처리·표시 순서)"))
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._on_list_row)
        sl.addWidget(self.list, 1)
        order = QHBoxLayout()
        for text, d in (("위로", -1), ("아래로", 1)):
            b = QPushButton(text)
            b.clicked.connect(lambda _c=False, dd=d: self.move_selected(dd))
            order.addWidget(b)
        dele = QPushButton("지우기")
        dele.clicked.connect(self.delete_selected)
        order.addWidget(dele)
        sl.addLayout(order)

        form = QFormLayout()
        self.f_name = QLineEdit()
        self.f_kind = QComboBox()
        for k, t in KIND_LABELS:
            self.f_kind.addItem(t, k)
        self.f_x, self.f_y, self.f_w, self.f_h = (self._ratio_spin() for _ in range(4))
        xy = QHBoxLayout()
        for lab, sp in (("x", self.f_x), ("y", self.f_y), ("너비", self.f_w), ("높이", self.f_h)):
            xy.addWidget(QLabel(lab))
            xy.addWidget(sp)
        self.f_rotate = QCheckBox("180도 돌려 인쇄된 영역 (카드 위쪽 반 등)")
        self.f_align = QComboBox()
        for k, t in ALIGN_LABELS:
            self.f_align.addItem(t, k)
        self.f_color = QLineEdit()
        self.f_color.setPlaceholderText("#000000")
        color_btn = QPushButton("고르기")
        color_btn.clicked.connect(self._pick_color)
        crow = QHBoxLayout()
        crow.addWidget(self.f_color)
        crow.addWidget(color_btn)
        self.f_font = QComboBox()
        self._fill_fonts()
        self.f_keep_tall = QDoubleSpinBox()
        self.f_keep_tall.setRange(0, 200)
        self.f_keep_tall.setDecimals(1)
        self.f_keep_tall.setSuffix(" pt")
        self.f_keep_tall.setSpecialValueText("끔")
        self.f_keep_above = QCheckBox("글자 줄 위의 그림(예: 이름 위 왕관)은 남기기")
        self.f_notes = QPlainTextEdit()
        self.f_notes.setFixedHeight(70)
        form.addRow("이름", self.f_name)
        form.addRow("종류", self.f_kind)
        form.addRow("위치(비율)", xy)
        form.addRow("회전", self.f_rotate)
        form.addRow("정렬", self.f_align)
        form.addRow("글자 색", crow)
        form.addRow("한글 글꼴", self.f_font)
        form.addRow("이보다 큰 잉크는 남기기", self.f_keep_tall)
        form.addRow("", self.f_keep_above)
        form.addRow("메모", self.f_notes)
        self.form_box = QWidget()
        self.form_box.setLayout(form)
        sl.addWidget(self.form_box)
        help_ = QLabel("빈 곳 끌기: 새 영역(위 + 버튼을 누른 뒤) · 안쪽 끌기: 옮기기 · 가장자리 끌기: 크기\n"
                       "화살표: 1px(Shift 10px) · Alt+화살표: 크기 · Delete: 지우기 · Ctrl+휠: 확대")
        help_.setStyleSheet("color: #666")
        sl.addWidget(help_)
        for w in (self.f_name,):
            w.textEdited.connect(self._from_form)
        for w in (self.f_kind, self.f_align, self.f_font):
            w.currentIndexChanged.connect(self._from_form)
        for w in (self.f_x, self.f_y, self.f_w, self.f_h, self.f_keep_tall):
            w.valueChanged.connect(self._from_form)
        for w in (self.f_rotate, self.f_keep_above):
            w.toggled.connect(self._from_form)
        self.f_color.editingFinished.connect(self._from_form)
        self.f_notes.textChanged.connect(self._from_form)

        split = QSplitter()
        split.addWidget(self.view)
        split.addWidget(side)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(split, 1)
        self.status = QLabel()
        lay.addWidget(self.status)

        self.page_spin.setValue(int(self.template.get("reference_page", 1) or 1))
        self.page_spin.valueChanged.connect(lambda _v: self._change_page())
        self.file_combo.currentIndexChanged.connect(self._on_file_combo)
        self._update_form_enabled()

    def used_fonts(self, exclude: RegionItem | None = None) -> list[tuple[str, str, list[str]]]:
        """다른 영역에서 이미 고른 글꼴 [(파일, 이름, [쓰는 곳])]: 이 창의 영역 + 프로젝트의 다른 그룹."""
        used: dict[str, tuple[str, list[str]]] = {}

        def add(font, where: str):
            f = (font or {}).get("file")
            if f:
                fam = font.get("family") or next((x["family"] for x in self.fonts if x["file"] == f), Path(f).stem)
                used.setdefault(f, (fam, []))[1].append(where)

        for it in self.items():
            if it is not exclude and it.kind != "protect":
                add((it.data.get("style") or {}).get("font"), it.name)
        for g in self.groups:
            if g["dir"] == self.group_dir:
                continue
            for r in service.load_region_template(g["dir"]).get("regions", []):
                add((r.get("style") or {}).get("font"), f"{g['name']}/{r.get('name')}")
        return [(f, fam, where) for f, (fam, where) in used.items()]

    def _fill_fonts(self, exclude: RegionItem | None = None):
        """글꼴 목록: 맨 위에 다른 영역에서 쓰는 글꼴(굵게), 그다음 기본값과 전체 목록."""
        self.f_font.blockSignals(True)
        self.f_font.clear()
        used = self.used_fonts(exclude)
        bold = QFont(self.f_font.font())
        bold.setBold(True)
        for f, fam, where in used:
            label = next((x["label"] for x in self.fonts if x["file"] == f), fam)
            more = " …" if len(where) > 3 else ""
            self.f_font.addItem(f"{label}  (쓰는 곳: {', '.join(where[:3])}{more})", f)
            self.f_font.setItemData(self.f_font.count() - 1, bold, Qt.ItemDataRole.FontRole)
        if used:
            self.f_font.insertSeparator(self.f_font.count())
        self.f_font.addItem("(기본: Noto Sans KR)", "")
        top = {f for f, _fam, _w in used}
        for f in self.fonts:
            if f["file"] not in top:
                self.f_font.addItem(f["label"], f["file"])
        self.f_font.blockSignals(False)

    def _ratio_spin(self):
        s = QDoubleSpinBox()
        s.setRange(0, 1)
        s.setDecimals(4)
        s.setSingleStep(0.002)
        return s

    # ------------------------------------------------------------ 쪽·영역 불러오기
    def current_file(self) -> Path:
        return Path(self.file_combo.currentData()[1])

    def _on_file_combo(self, idx: int):
        gdir = Path(self.file_combo.itemData(idx)[0])
        if gdir == self.group_dir:
            self._combo_index = idx
            self._change_page()
            return
        if not self._confirm_leave():  # 저장 안 한 변경이 있으면 묻는다. 취소면 원래 파일로
            self.file_combo.blockSignals(True)
            self.file_combo.setCurrentIndex(self._combo_index)
            self.file_combo.blockSignals(False)
            return
        self._combo_index = idx
        self.switch_group(gdir)

    def switch_group(self, gdir: Path):
        """다른 그룹의 영역 템플릿으로 바꾼다 (같은 창에서)."""
        for it in self.items():
            self.scene.removeItem(it)
        self._order = []
        self.group_dir = Path(gdir)
        g = next(g for g in self.groups if g["dir"] == self.group_dir)
        self.file_dirs = g["files"]
        self.template = service.load_region_template(self.group_dir)
        self.page_spin.blockSignals(True)
        self.page_spin.setMaximum(9999)
        self.page_spin.setValue(int(self.template.get("reference_page", 1) or 1))
        self.page_spin.blockSignals(False)
        self._load_page()
        self._load_regions()
        self.dirty = False
        self._update_title()
        self._update_form_enabled()
        self.view.fit()
        self.status.setText(f"그룹: {g['name']}")

    def _confirm_leave(self) -> bool:
        """바뀐 영역이 있으면 저장할지 묻는다. 계속해도 되면 True."""
        if not self.dirty:
            return True
        r = QMessageBox.question(self, "저장하지 않은 변경", f"‘{self.group_dir.name}’의 바뀐 영역을 저장할까요?",
                                 QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                                 | QMessageBox.StandardButton.Cancel)
        if r == QMessageBox.StandardButton.Cancel:
            return False
        if r == QMessageBox.StandardButton.Save:
            return self.save()
        self.dirty = False
        return True

    def _load_page(self):
        fd = self.current_file()
        n = service.page_count(fd)
        self.page_spin.blockSignals(True)
        self.page_spin.setMaximum(max(1, n))
        self.page_spin.blockSignals(False)
        self.page_total.setText(f"/ {n}")
        png, _size = service.page_image(fd, self.page_spin.value(), dpi=150)
        pm = QPixmap()
        pm.loadFromData(png, "PNG")
        if self.pix_item is None:
            self.pix_item = self.scene.addPixmap(pm)
            self.pix_item.setZValue(-1)
        else:
            self.pix_item.setPixmap(pm)
        self.scene.page_rect = QRectF(0, 0, pm.width(), pm.height())
        self.scene.setSceneRect(self.scene.page_rect.adjusted(-20, -20, 20, 20))

    def _change_page(self):
        ratios = [(it, self.ratio_of(it)) for it in self.items()]
        self._load_page()
        for it, r in ratios:  # 다른 카드에 같은 비율로 겹쳐 본다
            it.set_scene_rect(self.rect_from_ratio(r))
        self.view.fit()

    def _load_regions(self):
        self._order = []
        for r in self.template.get("regions", []):
            self._add_item(copy.deepcopy(r), protect=False)
        for r in self.template.get("protect_rects", []):
            self._add_item(copy.deepcopy(r), protect=True)
        self._rebuild_list()

    def _add_item(self, data: dict, protect: bool) -> RegionItem:
        it = RegionItem(self.rect_from_ratio(data.get("rect") or {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.1}),
                        data, protect)
        self.scene.addItem(it)
        it.refresh()
        self._order.append(it)
        return it

    def showEvent(self, ev):
        super().showEvent(ev)
        self.view.fit()

    # ------------------------------------------------------------ 좌표
    def ratio_of(self, it: RegionItem) -> dict:
        pr = self.scene.page_rect
        r = it.rect()
        return {"x": round(r.left() / pr.width(), 4), "y": round(r.top() / pr.height(), 4),
                "w": round(r.width() / pr.width(), 4), "h": round(r.height() / pr.height(), 4)}

    def rect_from_ratio(self, d: dict) -> QRectF:
        pr = self.scene.page_rect
        return QRectF(float(d["x"]) * pr.width(), float(d["y"]) * pr.height(),
                      float(d["w"]) * pr.width(), float(d["h"]) * pr.height())

    # ------------------------------------------------------------ 목록·선택
    def items(self) -> list[RegionItem]:
        return list(getattr(self, "_order", []))

    def _rebuild_list(self, keep: RegionItem | None = None):
        """목록은 템플릿 순서(_order)를 따른다. 새로 그린 영역은 끝에 붙는다."""
        order = self._order
        self.list.blockSignals(True)
        self.list.clear()
        for it in order:
            kind = dict(KIND_LABELS)[it.kind]
            self.list.addItem(f"{it.name or '(이름 없음)'}  ·  {kind}")
        if keep in order:
            self.list.setCurrentRow(order.index(keep))
        self.list.blockSignals(False)

    def selected(self) -> RegionItem | None:
        sel = [i for i in self.scene.selectedItems() if isinstance(i, RegionItem)]
        return sel[0] if sel else None

    def _on_list_row(self, row: int):
        if 0 <= row < len(self._order):
            self.scene.clearSelection()
            self._order[row].setSelected(True)
            self.view.centerOn(self._order[row])

    def _on_selection(self):
        try:
            it = self.selected()
        except RuntimeError:  # 창을 닫으며 장면이 지워질 때 선택 해제 신호가 온다
            return
        if it in self._order:
            self.list.blockSignals(True)
            self.list.setCurrentRow(self._order.index(it))
            self.list.blockSignals(False)
        self._to_form(it)
        self._update_form_enabled()

    def _update_form_enabled(self):
        self.form_box.setEnabled(self.selected() is not None)

    def _on_created(self, it: RegionItem):
        if it not in self._order:
            self._order.append(it)
        base = {"text": "본문", "tag_pills": "태그", "protect": "보호"}[it.kind]
        names = {i.name for i in self.items()}
        n, name = 1, base
        while name in names:
            n += 1
            name = f"{base}{n}"
        it.data["name"] = name
        for b in self.add_buttons.values():
            b.setChecked(False)
        self.scene.add_kind = None
        self._rebuild_list(it)
        self._to_form(it)
        self._mark_dirty()
        self.f_name.setFocus()
        self.f_name.selectAll()

    def _on_geometry(self, it: RegionItem):
        if it is self.selected():
            self._to_form(it)
        self._mark_dirty()

    def _set_add_kind(self, kind: str | None):
        for k, b in self.add_buttons.items():
            b.setChecked(k == kind)
        self.scene.add_kind = kind
        self.view.viewport().setCursor(Qt.CursorShape.CrossCursor if kind else Qt.CursorShape.ArrowCursor)

    def delete_selected(self):
        it = self.selected()
        if it is None:
            return
        self.scene.removeItem(it)
        self._order.remove(it)
        self._rebuild_list()
        self._mark_dirty()

    def move_selected(self, d: int):
        it = self.selected()
        if it is None:
            return
        i = self._order.index(it)
        j = max(0, min(len(self._order) - 1, i + d))
        self._order[i], self._order[j] = self._order[j], self._order[i]
        self._rebuild_list(it)
        self._mark_dirty()

    # ------------------------------------------------------------ 폼 <-> 영역
    def _to_form(self, it: RegionItem | None):
        self._loading = True
        try:
            if it is None:
                return
            st = it.data.get("style") or {}
            er = it.data.get("erase") or {}
            self.f_name.setText(it.name)
            self.f_kind.setCurrentIndex([k for k, _t in KIND_LABELS].index(it.kind))
            r = self.ratio_of(it)
            for sp, k in ((self.f_x, "x"), (self.f_y, "y"), (self.f_w, "w"), (self.f_h, "h")):
                sp.setValue(r[k])
            self.f_rotate.setChecked(int(st.get("rotate", 0) or 0) == 180)
            self.f_align.setCurrentIndex(max(0, [k for k, _t in ALIGN_LABELS].index(st.get("align", "left"))
                                             if st.get("align", "left") in dict(ALIGN_LABELS) else 0))
            self.f_color.setText(st.get("color", ""))
            ff = (st.get("font") or {}).get("file", "")
            self._fill_fonts(exclude=it)
            idx = self.f_font.findData(ff)
            if idx < 0 and ff:  # 목록에 없는 글꼴 파일도 보여 준다
                self.f_font.addItem(f"{(st.get('font') or {}).get('family') or Path(ff).stem} (목록 밖)", ff)
                idx = self.f_font.count() - 1
            self.f_font.setCurrentIndex(max(0, idx))
            self.f_keep_tall.setValue(float(er.get("keep_ink_taller_than_pt") or 0))
            self.f_keep_above.setChecked(bool(er.get("keep_ink_above_text")))
            self.f_notes.setPlainText(it.data.get("notes", ""))
            prot = it.kind == "protect"
            for w in (self.f_rotate, self.f_align, self.f_color, self.f_font, self.f_keep_tall, self.f_keep_above):
                w.setEnabled(not prot)
        finally:
            self._loading = False

    def _from_form(self, *_a):
        if self._loading:
            return
        it = self.selected()
        if it is None:
            return
        d = it.data
        d["name"] = self.f_name.text().strip()
        it.kind = self.f_kind.currentData()
        if it.kind != "protect":
            d["kind"] = it.kind
        r = QRectF(self.f_x.value(), self.f_y.value(), self.f_w.value(), self.f_h.value())
        if r.width() > 0 and r.height() > 0:
            new = self.rect_from_ratio({"x": r.x(), "y": r.y(), "w": r.width(), "h": r.height()})
            if (new.topLeft() - it.rect().topLeft()).manhattanLength() > 0.5 or \
                    abs(new.width() - it.rect().width()) > 0.5 or abs(new.height() - it.rect().height()) > 0.5:
                it.set_scene_rect(new)
        st = d.setdefault("style", {})
        if self.f_rotate.isChecked():
            st["rotate"] = 180
        else:
            st.pop("rotate", None)
        st["align"] = self.f_align.currentData()
        color = self.f_color.text().strip()
        if color:
            st["color"] = color
        else:
            st.pop("color", None)
        ff = self.f_font.currentData()
        old = st.get("font") or {}
        if ff and old.get("file") != ff:
            fam = next((f["family"] for f in self.fonts if f["file"] == ff), Path(ff).stem)
            font = {"family": fam, "file": ff}
            if old.get("fallback_file"):
                font["fallback_file"] = old["fallback_file"]
            font["chosen"] = "영역 편집기에서 고름"
            st["font"] = font
        elif not ff:
            st.pop("font", None)
        er = d.setdefault("erase", {})
        if self.f_keep_tall.value() > 0:
            er["keep_ink_taller_than_pt"] = round(self.f_keep_tall.value(), 1)
        else:
            er.pop("keep_ink_taller_than_pt", None)
        if self.f_keep_above.isChecked():
            er["keep_ink_above_text"] = True
        else:
            er.pop("keep_ink_above_text", None)
        if not er:
            d.pop("erase", None)
        notes = self.f_notes.toPlainText().strip()
        if notes:
            d["notes"] = notes
        else:
            d.pop("notes", None)
        if not st:
            d.pop("style", None)
        it.refresh()
        self._rebuild_list(it)
        self._mark_dirty()

    def _pick_color(self):
        c = QColorDialog.getColor(QColor(self.f_color.text() or "#000000"), self, "글자 색")
        if c.isValid():
            self.f_color.setText(c.name())
            self._from_form()

    # ------------------------------------------------------------ 저장
    def to_template(self) -> dict:
        data = copy.deepcopy(self.template)
        regions, protect = [], []
        for it in self.items():
            d = copy.deepcopy(it.data)
            d["rect"] = self.ratio_of(it)
            if it.kind == "protect":
                d.pop("kind", None)
                for k in ("style", "erase", "extract"):
                    d.pop(k, None)
                protect.append(d)
            else:
                d["kind"] = it.kind
                regions.append(d)
        data["regions"] = regions
        data["protect_rects"] = protect
        data.setdefault("reference_page", self.page_spin.value())
        data.setdefault("source_language", "ja")
        return data

    def save(self) -> bool:
        problems = service.save_region_template(self.group_dir, self.to_template())
        if problems:
            QMessageBox.warning(self, "저장할 수 없음", "\n".join(problems))
            return False
        self.template = service.load_region_template(self.group_dir)
        self.dirty = False
        self._update_title()
        self.status.setText(f"저장함: {self.group_dir / 'regions.json'}")
        self.saved.emit()
        return True

    def _mark_dirty(self):
        self.dirty = True
        self._update_title()

    def _update_title(self):
        self.setWindowTitle(f"영역 지정 — {self.group_dir.name}{' *' if self.dirty else ''}")

    def closeEvent(self, ev):
        if not self._confirm_leave():
            ev.ignore()
            return
        super().closeEvent(ev)
