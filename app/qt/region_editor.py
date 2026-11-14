"""영역 지정 화면 (PRD 2단계). 카드 위에 사각형을 그리고 이름·종류·서식을 정해 그룹의 regions.json에 저장한다.

- 빈 곳을 끌면 새 영역, 영역 안을 끌면 옮기기, 가장자리를 끌면 크기 조절.
- 화살표 키로 조금씩(Shift: 크게) 옮기기, Alt+화살표로 크기 조절, Delete로 지우기, Ctrl+휠로 확대.
- 좌표는 카드 대비 비율로 저장한다. 손으로 쓴 regions.json의 모르는 항목은 그대로 둔다.
- 미리보기(G): 오른쪽에 다른 카드 16장을 같은 영역으로 겹쳐 보여 준다 (app.qt.region_preview). 영역을 고치면 바로 반영.
- 이 카드만 따로 영역: 공통 영역을 쓰는 그룹에서 상자 크기가 다른 카드만 따로 영역을 둔다 (regions.json page_regions,
  엔진은 그 카드에서 공통 영역 대신 이것을 쓴다).
- 자동 정렬: 카드마다 조금씩 밀린 그룹에서 공통 영역을 카드마다 옮겨 맞춘다 (service.align_group, 파일 폴더의 align.json).
  기준이 아닌 카드를 보면 상자를 그 카드의 정렬만큼 옮겨 그리고, 상자를 고치면 기준 카드 좌표로 되돌려 공통 영역을 고친다.
  Ctrl+화살표(Shift: 크게)는 지금 카드의 정렬만 옮긴다 (바로 align.json에 저장).
"""

from __future__ import annotations

import copy
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QGuiApplication, QBrush, QColor, QFont, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QFrame, QGraphicsRectItem,
    QGraphicsScene, QGroupBox, QGraphicsSimpleTextItem, QGraphicsView, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QSplitter, QToolButton, QVBoxLayout, QWidget,
)

from app import service
from app.qt.dialogs import PRIMARY_QSS, ask_choice, erase_modes, fit_to_screen, grey_note, hint_bar, section_title, set_hint, show_problem
from app.qt.region_preview import PreviewPanel

KIND_LABELS = [("text", "글자"), ("tag_pills", "태그 라벨"), ("protect", "보호 영역(건드리지 않음)")]
ALIGN_LABELS = [("", "자동 (원문 줄마다 따라감)"), ("left", "왼쪽 정렬"), ("center", "가운데 정렬"),
                ("right", "오른쪽 정렬"), ("original", "정렬 안 함 (원래 위치 그대로)")]
COLORS = {"text": QColor(30, 110, 230), "tag_pills": QColor(150, 60, 200), "protect": QColor(220, 40, 40)}
EDGE = 6.0  # 가장자리로 보는 거리 (화면 px)
MIN_SIZE = 4.0
SELECTED_Z = 10.0  # 고른 영역의 쌓임 순서 (다른 영역 위)


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
                self.setZValue(SELECTED_Z if self.isSelected() else 0)  # 고른 영역을 맨 위로: 겹친 영역에 가려 끌 수 없던 문제
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
        sc = self.scene()
        if hasattr(sc, "moving_item"):
            sc.moving_item.emit(self)
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
    moving_item = Signal(object)  # 끄는 중 (미리보기를 따라 그리기)
    created_item = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.page_rect = QRectF()
        self.add_kind: str | None = None  # 그리기 모드: text | tag_pills | protect | None
        self.item_class = RegionItem  # 새로 그릴 상자의 종류 (카드 자리 그리기 창은 CardBox)
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
            prot = kind in ("protect", "ocr_exclude")  # 읽기 제외 영역도 보호 영역 (글자 읽기에서도 뺌)
            data = {"name": "", "kind": "text" if prot else kind}
            if kind == "ocr_exclude":
                data["ocr_exclude"] = True
            self._new = self.item_class(QRectF(self._start, self._start), data, protect=prot)
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
    page_step = Signal(int)  # -1 이전 쪽, +1 다음 쪽
    page_jump = Signal(int)  # 0 첫 쪽, -1 마지막 쪽
    key_action = Signal(str)  # 한 글자 단축키: add_text | add_tag | add_protect | fit | rotate | next_region | prev_region
    align_nudge = Signal(float, float)  # Ctrl+화살표: 이 카드 정렬만 옮기기 (장면 px)

    def __init__(self, scene: PageScene):
        super().__init__(scene)
        self.auto_fit = True  # 사용자가 확대하기 전까지는 창 크기에 맞춰 늘 화면에 맞춘다
        self._wheel = 0
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def _set_bars(self):
        """화면에 맞춘 동안은 스크롤 막대를 숨긴다 (막대가 생기며 카드가 잘리지 않게). 확대하면 다시 보인다."""
        pol = Qt.ScrollBarPolicy.ScrollBarAlwaysOff if self.auto_fit else Qt.ScrollBarPolicy.ScrollBarAsNeeded
        self.setHorizontalScrollBarPolicy(pol)
        self.setVerticalScrollBarPolicy(pol)

    def wheelEvent(self, ev):
        if ev.modifiers() & Qt.KeyboardModifier.ControlModifier:  # Ctrl+휠: 확대·축소
            f = 1.15 if ev.angleDelta().y() > 0 else 1 / 1.15
            self.scale(f, f)
            self.auto_fit = False
            self._set_bars()
            ev.accept()
            return
        if ev.modifiers() & Qt.KeyboardModifier.ShiftModifier:  # Shift+휠: 확대한 화면 스크롤
            super().wheelEvent(ev)
            return
        # 휠: 이전·다음 쪽 (터치패드의 작은 움직임은 모아서 한 칸)
        self._wheel += ev.angleDelta().y()
        if abs(self._wheel) >= 120:
            self.page_step.emit(-1 if self._wheel > 0 else 1)
            self._wheel = 0
        ev.accept()

    def fit(self):
        """카드 한 장이 스크롤 없이 칸 안에 다 보이게 (확대하기 전까지)."""
        sc = self.scene()
        if isinstance(sc, PageScene) and not sc.page_rect.isEmpty():
            self._set_bars()
            m = 6.0  # 가장자리 여유 (영역 손잡이가 보이게)
            self.fitInView(sc.page_rect.adjusted(-m, -m, m, m), Qt.AspectRatioMode.KeepAspectRatio)
            if self.auto_fit:
                self.centerOn(sc.page_rect.center())

    def fit_again(self):
        """‘화면에 맞춤’: 다시 창 크기를 따라가게 한다."""
        self.auto_fit = True
        self.fit()

    def zoom(self, f: float):
        """확대·축소 (화면 가운데 기준). 확대하면 스크롤 막대와 끌어서 옮기기를 쓸 수 있다."""
        old = self.transformationAnchor()
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.scale(f, f)
        self.setTransformationAnchor(old)
        self.auto_fit = False
        self._set_bars()

    # 끌어서 옮기기: 가운데 버튼(휠 누르기) 또는 Space를 누른 채 왼쪽 버튼
    def _pan_start(self, ev) -> bool:
        if ev.button() == Qt.MouseButton.MiddleButton or (
                ev.button() == Qt.MouseButton.LeftButton and getattr(self, "_space", False)):
            self._pan = ev.position().toPoint()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            return True
        return False

    def mousePressEvent(self, ev):
        if self._pan_start(ev):
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if getattr(self, "_pan", None) is not None:
            p = ev.position().toPoint()
            d = p - self._pan
            self._pan = p
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - d.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - d.y())
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if getattr(self, "_pan", None) is not None:
            self._pan = None
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor if getattr(self, "_space", False)
                                      else Qt.CursorShape.ArrowCursor)
            ev.accept()
            return
        super().mouseReleaseEvent(ev)

    def keyReleaseEvent(self, ev):
        if ev.key() == Qt.Key.Key_Space and not ev.isAutoRepeat():
            self._space = False
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
        super().keyReleaseEvent(ev)

    def event(self, ev):
        # Tab은 포커스 이동 대신 영역 고르기 (Shift+Tab 거꾸로)
        from PySide6.QtCore import QEvent

        if ev.type() == QEvent.Type.KeyPress and ev.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            self.key_action.emit("prev_region" if ev.key() == Qt.Key.Key_Backtab else "next_region")
            return True
        return super().event(ev)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self.auto_fit:  # 처음 열 때 창이 커지는 동안에도 카드가 작게 남지 않게
            self.fit()

    def keyPressEvent(self, ev):
        items = [i for i in self.scene().selectedItems() if isinstance(i, RegionItem)]
        k = ev.key()
        arrows = (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down)
        if k in arrows and ev.modifiers() & Qt.KeyboardModifier.ControlModifier:  # 이 카드의 정렬만 옮기기
            step = 10.0 if ev.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1.0
            self.align_nudge.emit({Qt.Key.Key_Left: -step, Qt.Key.Key_Right: step}.get(k, 0.0),
                                  {Qt.Key.Key_Up: -step, Qt.Key.Key_Down: step}.get(k, 0.0))
            ev.accept()
            return
        # 쪽 넘기기: PageUp/PageDown은 언제나, 좌우 방향키는 고른 영역이 없을 때 (영역을 고르면 그 영역을 옮긴다)
        if k in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown) or (
                not items and k in (Qt.Key.Key_Left, Qt.Key.Key_Right)):
            self.page_step.emit(-1 if k in (Qt.Key.Key_PageUp, Qt.Key.Key_Left) else 1)
            ev.accept()
            return
        if k == Qt.Key.Key_Space:  # Space를 누른 채 끌면 옮기기
            if not ev.isAutoRepeat():
                self._space = True
                self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            ev.accept()
            return
        if k in (Qt.Key.Key_Home, Qt.Key.Key_End):
            self.page_jump.emit(0 if k == Qt.Key.Key_Home else -1)
            ev.accept()
            return
        if k == Qt.Key.Key_Escape and items:
            self.scene().clearSelection()
            ev.accept()
            return
        plain = not (ev.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier))
        letters = {Qt.Key.Key_T: "add_text", Qt.Key.Key_L: "add_tag", Qt.Key.Key_P: "add_protect",
                   Qt.Key.Key_O: "add_ocr_exclude", Qt.Key.Key_G: "toggle_preview",
                   Qt.Key.Key_F: "fit", Qt.Key.Key_R: "rotate"}
        if plain and k in letters:
            self.key_action.emit(letters[k])
            ev.accept()
            return
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
        self._ov_key: str | None = None  # 공통 영역 그룹에서 지금 보는 카드가 ‘따로 영역’이면 그 카드 키
        self._common: tuple[list, list] = ([], [])  # 따로 영역 카드를 보는 동안 넣어 둔 공통 영역
        self._view_t: dict | None = None  # 지금 카드에 그린 상자의 정렬 변환 (공통 영역 + 자동 정렬 카드일 때만)
        self._align_cache: dict = {}  # 파일 폴더 -> {쪽: 정렬 값} (service.align_info)
        self.setWindowTitle(f"영역 지정 — {self.group_dir.name}")
        fit_to_screen(self, 1400, 900, maximize_below=0)

        self.scene = PageScene(self)
        self.view = PageView(self.scene)
        self.pix_item = None
        self._build_ui()
        for b in self.findChildren(QPushButton):  # 이름 칸 Enter가 ‘화면에 맞춤’ 같은 버튼을 누르지 않게
            b.setAutoDefault(False)
            b.setDefault(False)
        self._load_page()
        self._load_regions()
        self.scene.selectionChanged.connect(self._on_selection)
        self.scene.changed_item.connect(self._on_geometry)
        self.scene.moving_item.connect(lambda _it: self.preview.schedule())
        self.scene.created_item.connect(self._on_created)
        self.view.nudged.connect(self._on_geometry)
        self.view.delete_pressed.connect(self.delete_selected)
        self.view.page_step.connect(self.step_page)
        self.view.page_jump.connect(self.jump_page)
        self.view.key_action.connect(self._key_action)
        self.view.align_nudge.connect(self.nudge_alignment)
        QShortcut(QKeySequence.StandardKey.Save, self, activated=self.save)
        for seq, fn in (("Ctrl+C", self.copy_regions), ("Ctrl+V", self.paste_regions),
                        ("Ctrl+Shift+D", self.duplicate_selected), ("Ctrl+Shift+V", self.copy_previous_card),
                        ("F1", lambda: self.help_btn.toggle())):
            sc = QShortcut(QKeySequence(seq), self, activated=fn)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._clip: list[dict] = []

    def _widen_file_popup(self):
        """긴 ‘그룹 / 파일’ 이름이 목록에서 잘리지 않게 펼친 목록을 가장 긴 이름 폭에 맞춘다 (화면 폭 안에서).
        칸에 다 안 들어가는 이름은 가운데를 줄이고, 마우스를 올리면 전체 이름이 나온다."""
        cb = self.file_combo
        view = cb.view()
        view.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        fm = cb.fontMetrics()
        widest = max((fm.horizontalAdvance(cb.itemText(i)) for i in range(cb.count())), default=0)
        scr = self.screen() or QGuiApplication.primaryScreen()
        cap = int(scr.availableGeometry().width() * 0.8) if scr else 900
        view.setMinimumWidth(min(widest + 60, cap))  # 글자 + 여백·스크롤 막대
        cb.setMinimumWidth(min(widest + 76, 480))  # 칸도 이름이 다 보이게 (너무 길면 480에서 멈춤, 전체는 말풍선)
        cb.setToolTip(cb.currentText())
        cb.currentIndexChanged.connect(lambda _i: cb.setToolTip(cb.currentText()))

    # ------------------------------------------------------------ 화면 구성
    def _build_ui(self):
        top = QHBoxLayout()
        top.addWidget(QLabel("그룹 / 파일"))
        self.file_combo = QComboBox()
        self.file_combo.setMinimumWidth(200)
        self.file_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.file_combo.setMinimumContentsLength(18)
        for g in self.groups:  # 모든 그룹의 파일. 다른 그룹 파일을 고르면 그 그룹 영역으로 옮겨 간다
            for fd in g["files"]:
                full = f"{g['name']} / {fd.name}"
                self.file_combo.addItem(full, (str(g["dir"]), str(fd)))
                self.file_combo.setItemData(self.file_combo.count() - 1, full, Qt.ItemDataRole.ToolTipRole)
        cur = next((i for i in range(self.file_combo.count())
                    if Path(self.file_combo.itemData(i)[0]) == self.group_dir), 0)
        self.file_combo.setCurrentIndex(cur)
        self._combo_index = cur
        self._widen_file_popup()
        top.addWidget(self.file_combo)
        top.addWidget(QLabel("쪽"))
        self.page_spin = QSpinBox()
        self.page_spin.setMinimum(1)
        top.addWidget(self.page_spin)
        self.page_total = QLabel()
        top.addWidget(self.page_total)
        tools = QHBoxLayout()  # 둘째 줄: 영역 추가·영역 방식
        tools.setSpacing(3)
        cap = QLabel("영역 그리기")
        cap.setStyleSheet("color: #3A3A3C; font-weight: bold;")
        tools.addWidget(cap)
        self.add_buttons = {}
        for kind, text, key in (("text", "+ 글자 영역", "T"), ("tag_pills", "+ 태그 라벨 영역", "L"),
                                ("protect", "+ 보호 영역", "P"), ("ocr_exclude", "+ 읽기 제외 영역", "O")):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(f"카드 위를 끌어서 새 영역을 그립니다 (단축키 {key})")
            b.setCheckable(True)
            b.clicked.connect(lambda checked, k=kind: self._set_add_kind(k if checked else None))
            self.add_buttons[kind] = b
            tools.addWidget(b)
        tools.addSpacing(6)
        self.per_card = QCheckBox("카드마다 영역 지정")
        self.per_card.setToolTip("카드마다 형식이 달라 공통 영역을 쓸 수 없는 그룹: 카드 한 장씩 영역을 정합니다")
        self.per_card.setChecked(self.template.get("layout") == "per_card")
        self.per_card.toggled.connect(self._toggle_per_card)
        tools.addWidget(self.per_card)
        self.override_btn = QPushButton("이 카드만 따로 영역")
        self.override_btn.setCheckable(True)
        self.override_btn.setToolTip("이 카드만 영역의 위치·크기를 다르게 씁니다 (글자 상자 크기가 다른 카드). 서식·글꼴은 공통 영역을 따르고,\n"
                                     "다른 카드는 공통 영역 그대로. 영역을 모두 지우면 이 카드는 처리하지 않습니다. 다시 누르면 공통 영역으로")
        self.override_btn.setStyleSheet("QPushButton:checked { background: #FFF4CC; border: 2px solid #E0A800; font-weight: bold; }")
        self.override_btn.toggled.connect(self._toggle_override)  # 아래 카드 정보 줄에 둔다 (창 최소 폭)
        self.align_btn = QPushButton("자동 정렬")
        self.align_btn.setToolTip("카드마다 조금씩 밀리거나 크기가 다른 그룹에서, 공통 영역을 카드마다 테두리·글 상자에 맞춰 옮깁니다.\n"
                                  "기준은 그룹 첫 파일의 기준 카드입니다. 맞춘 뒤 Ctrl+화살표로 카드마다 미세 조정할 수 있습니다.\n"
                                  "이미 읽은(추출한) 카드는 다시 읽지 않습니다")
        self.align_btn.clicked.connect(self.run_alignment)  # 아래 카드 정보 줄에 둔다 (창 최소 폭)
        self.align_reset_btn = QPushButton("이 카드 정렬 되돌리기")
        self.align_reset_btn.setToolTip("손으로 맞춘 정렬은 자동 정렬 값으로, 자동 정렬은 정렬 없음(공통 영역 그대로)으로 되돌립니다.\n"
                                        "바로 저장됩니다")
        self.align_reset_btn.clicked.connect(self.reset_alignment)
        self.copy_prev = QPushButton("이전 카드 영역 복사")
        self.copy_prev.setToolTip("앞 카드의 영역을 이 카드로 가져옵니다 (Ctrl+Shift+V)")
        self.copy_prev.clicked.connect(self.copy_previous_card)
        cards = QHBoxLayout()  # 셋째 줄: 카드마다 영역 도구 (카드마다일 때만 보인다)
        cards.setContentsMargins(0, 0, 0, 0)
        cards.addWidget(self.copy_prev)
        self.copy_rest = QPushButton("나머지 카드에 복사")
        self.copy_rest.setToolTip("지금 카드의 영역을 같은 그룹의 다른 카드에 복사합니다 (영역 없는 카드에만, 또는 모두 덮어쓰기)")
        self.copy_rest.clicked.connect(self.copy_to_rest)
        cards.addWidget(self.copy_rest)
        self.reset_btn = QPushButton("초기화…")
        self.reset_btn.setToolTip("이 카드(또는 모든 카드)의 영역을 비웁니다. 저장하기 전까지는 파일에 쓰지 않습니다")
        self.reset_btn.clicked.connect(self.reset_regions)
        self.sync_names = QCheckBox("설정은 같은 이름 영역에 함께")
        self.sync_names.setToolTip("카드마다 영역일 때: 영역의 이름·종류·회전·정렬·색·글꼴·지우기 방식을 바꾸면\n"
                                   "다른 카드의 같은 이름 영역에도 똑같이 적용합니다 (위치·크기는 카드마다 따로)")
        self.sync_names.setChecked(True)
        cards.addWidget(self.sync_names)
        cards.addStretch(1)
        self.cards_row = cards
        self.cards_bar = QWidget()
        self.cards_bar.setLayout(cards)
        self.preview_btn = QPushButton("미리보기 (G)")
        self.preview_btn.setCheckable(True)
        self.preview_btn.setStyleSheet("QPushButton:checked { background: #1E6FD9; color: white; font-weight: bold; }")
        self.preview_btn.setToolTip("오른쪽에 다른 카드 16장을 같은 영역으로 보여 줍니다 — 영역이 그림·테두리에 걸치는 카드를 찾고,\n"
                                    "카드를 빼거나 그 카드만 따로 영역을 정할 수 있습니다 (끄면 영역 설정 칸이 다시 보입니다)")
        self.preview_btn.toggled.connect(self._toggle_preview)
        tools.addStretch(1)
        tools.addWidget(self.reset_btn)
        top.addStretch(1)
        for text, f, tip in (("－", 1 / 1.25, "축소 (Ctrl+휠 아래)"), ("＋", 1.25, "확대 (Ctrl+휠 위)")):
            zb = QPushButton(text)
            zb.setFixedWidth(36)
            zb.setStyleSheet("font-size: 13pt; font-weight: bold; padding: 0 4px;")
            zb.setToolTip(tip + " · 확대한 뒤 가운데 버튼(휠)을 누른 채 끌거나 Space+끌기로 옮기기")
            zb.clicked.connect(lambda _c=False, ff=f: self.view.zoom(ff))
            top.addWidget(zb)
        fit = QPushButton("화면에 맞춤 (F)")
        fit.clicked.connect(self.view.fit_again)
        top.addWidget(fit)
        self.save_btn = QPushButton("저장 (Ctrl+S)")  # 저장 안 한 변경이 있을 때만 파랗게 (_update_title)
        self.save_btn.clicked.connect(self.save)
        top.addWidget(self.save_btn)
        close = QPushButton("닫기")
        close.clicked.connect(self.close)
        top.addWidget(close)

        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(4, 0, 4, 0)
        sl.setSpacing(8)
        lrow = QHBoxLayout()
        lrow.addWidget(section_title("이 카드의 영역"))
        lrow.addWidget(grey_note("위에서부터 처리"), 1)
        sl.addLayout(lrow)
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
        from PySide6.QtWidgets import QGridLayout

        xy = QGridLayout()  # 두 줄: x·y / 너비·높이 (좁은 칸에서도 숫자가 보이게)
        for i, (lab, sp) in enumerate((("왼쪽", self.f_x), ("위쪽", self.f_y), ("너비", self.f_w), ("높이", self.f_h))):
            # "0.0863" + 위/아래 버튼이 늘 들어갈 폭 (폼 라벨 열과 무관하게 한 줄 전체를 씀)
            sp.setMinimumWidth(sp.fontMetrics().horizontalAdvance("0.0000") + 44)
            xy.addWidget(QLabel(lab), i // 2, (i % 2) * 2)
            xy.addWidget(sp, i // 2, (i % 2) * 2 + 1)
        xy.setColumnStretch(1, 1)
        xy.setColumnStretch(3, 1)
        xy.setContentsMargins(0, 0, 0, 0)
        self.f_rotate = QCheckBox("180도 돌려 인쇄된 영역")
        self.f_rotate.setToolTip("카드 위쪽 반처럼 거꾸로 인쇄된 글자")
        self.f_align = QComboBox()
        self.f_align.setToolTip("한글을 놓는 방법\n"
                                "자동: 원문 줄마다 찾은 정렬을 따름 · 왼쪽/오른쪽: 글 칸의 왼쪽/오른쪽 끝에 맞춤\n"
                                "가운데: 영역 가운데 · 정렬 안 함: 줄마다 원문이 시작한 자리에서 시작")
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
        for cb in (self.f_font, self.f_kind, self.f_align):  # 긴 항목이 오른쪽 칸 폭을 늘리지 않게
            cb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            cb.setMinimumContentsLength(12)
        self.f_font_mode = QComboBox()
        self.f_font_mode.addItem("한 칸 모두 같은 글꼴", "cell")
        self.f_font_mode.addItem("문단마다 글꼴 지정", "sentence")
        self.f_font_mode.setToolTip("한글 넣기 전 글꼴 창에서 글꼴을 고르는 단위\n"
                                    "한 칸 모두 같은 글꼴: 카드의 이 칸 전체를 한 글꼴로 (기본)\n"
                                    "문단마다 글꼴 지정: 칸 안의 문단(효과·설명·말한 사람 등)마다 다른 글꼴을 고를 수 있음")
        self.f_keep_tall = QDoubleSpinBox()
        self.f_keep_tall.setRange(0, 200)
        self.f_keep_tall.setDecimals(1)
        self.f_keep_tall.setSuffix(" 포인트")
        self.f_keep_tall.setSpecialValueText("안 함")
        self.f_keep_tall.setToolTip("원래 카드의 글자·그림 중 이 크기보다 큰 것은 지우지 않고 남깁니다.\n"
                                    "예: 큰 장식 글자나 로고. 크기는 글자 크기 단위(포인트, 보통 본문 글자가 8~12 안팎)입니다.\n"
                                    "‘안 함’이면 크기와 상관없이 영역 안을 모두 지웁니다.")
        self.f_keep_tall.setMinimumWidth(self.f_keep_tall.fontMetrics().horizontalAdvance("200.0 포인트") + 44)
        self.f_erase_mode = QComboBox()
        for k, t in erase_modes():
            self.f_erase_mode.addItem(t, k)
        self.f_erase_mode.setToolTip("글자 뒤가 그림인 영역에서 원래 글자를 어떻게 지울지 정합니다. 결과 보기에서 카드마다 바꿀 수도 있습니다.\n"
                                     "글자가 따로 들어 있는 PDF는 ‘자동’이면 예전 방식으로 지웁니다.")
        self.f_ocr_exclude = QCheckBox("글자를 읽을 때도 건너뛰기")
        self.f_ocr_exclude.setToolTip("카드 번호·장식 글자처럼 번역하지 않을 글자: 글자를 읽을 때(추출) 가리고, 지우지도 않습니다")
        self.f_keep_above = QCheckBox("글자 위쪽 그림은 남기기")
        self.f_keep_above.setToolTip("글자 줄 바로 위에 붙은 그림은 지우지 않습니다. 예: 이름 위의 왕관 아이콘")
        self.f_notes = QPlainTextEdit()
        self.f_notes.setFixedHeight(70)
        def group(title: str) -> tuple[QGroupBox, QFormLayout]:
            box = QGroupBox(title)
            fl = QFormLayout(box)
            return box, fl

        self.g_basic, fb = group("기본")
        fb.addRow("이름", self.f_name)
        fb.addRow("종류", self.f_kind)
        fb.addRow(grey_note("위치·크기 (카드 전체를 1로 본 비율)"))  # 라벨 열을 빌리지 않고 폼 전체 폭을 쓴다
        fb.addRow(xy)
        fb.addRow("보호 영역", self.f_ocr_exclude)  # 보호 영역일 때만 보인다
        self.g_style, fs = group("한글 넣기")
        fs.addRow("회전", self.f_rotate)
        fs.addRow("정렬", self.f_align)
        fs.addRow("글자 색", crow)
        fs.addRow("한글 글꼴", self.f_font)
        fs.addRow("글꼴 단위", self.f_font_mode)
        self.g_erase, fe = group("글자 지우기")
        fe.addRow("지우는 방법", self.f_erase_mode)
        fe.addRow("큰 글자·그림은 남기기", self.f_keep_tall)
        fe.addRow("", self.f_keep_above)
        self.g_notes, fn = group("메모")
        fn.addRow(self.f_notes)
        self._basic_form = fb
        self.form_box = QWidget()
        fv = QVBoxLayout(self.form_box)
        fv.setContentsMargins(0, 0, 0, 0)
        for g in (self.g_basic, self.g_style, self.g_erase, self.g_notes):
            fv.addWidget(g)
        self.form_hint = grey_note("영역을 고르면 여기에서 이름·글꼴·지우기 방식을 바꿀 수 있습니다.\n"
                                   "카드 위의 영역을 누르거나 위 목록에서 고르세요.")
        sl.addWidget(self.form_box)
        sl.addWidget(self.form_hint)
        sl.addStretch(1)
        help_text = ("새 영역: T 글자 · L 태그 라벨 · P 보호 · O 읽기 제외 (누른 뒤 카드 위를 끌기) · 안쪽 끌기: 옮기기 · 가장자리 끌기: 크기\n"
                     "고른 영역: 화살표 조금씩 옮기기(Shift: 크게) · Alt+화살표 크기 · R 180도 회전 · Delete 지우기 · Esc 해제\n"
                     "Tab / Shift+Tab: 다음·이전 영역 · Ctrl+C / Ctrl+V: 영역 복사·붙여넣기(다른 카드로) · Ctrl+Shift+D: 복제 (Ctrl+D는 앱 전체 디버깅 모드)\n"
                     "Ctrl+화살표: 이 카드의 정렬만 옮기기 (모든 상자 함께, Shift: 크게) — 자동 정렬한 공통 영역 그룹\n"
                     "쪽: ←/→(영역을 안 골랐을 때) · PageUp/PageDown · Home/End 처음·끝 · 마우스 휠\n"
                     "F: 화면에 맞춤 · ＋/－ 또는 Ctrl+휠: 확대·축소 · 휠 누른 채 끌기 또는 Space+끌기: 옮기기 · Ctrl+Shift+V: 이전 카드 영역 · "
                     "Ctrl+S 저장 · F1 이 도움말")
        help_ = QLabel(help_text)
        help_.setStyleSheet("color: #6E6E73; background: white; border: 1px solid #E5E5EA; border-radius: 8px; padding: 8px;")
        self.help_label = help_
        self.help_btn = QToolButton()
        self.help_btn.setText("단축키 도움말 (F1)")
        self.help_btn.setCheckable(True)
        self.help_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.help_btn.setToolTip(help_text)
        self.help_btn.setAutoRaise(True)
        self.help_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.help_btn.toggled.connect(self._toggle_help)
        scr = self.screen() or QGuiApplication.primaryScreen()
        low = scr is not None and scr.availableGeometry().height() < 900  # 낮은 화면에서는 접어 둔다
        self.help_btn.setChecked(not low)
        self._toggle_help(not low)
        sl.addWidget(self.help_btn)
        sl.addWidget(help_)
        for w in (self.f_name,):
            w.textEdited.connect(self._from_form)
        self.f_name.returnPressed.connect(self.view.setFocus)  # Enter: 카드 화면으로 (화살표로 바로 옮기기)
        for w in (self.f_kind, self.f_align, self.f_font, self.f_font_mode, self.f_erase_mode):
            w.currentIndexChanged.connect(self._from_form)
        for w in (self.f_x, self.f_y, self.f_w, self.f_h, self.f_keep_tall):
            w.valueChanged.connect(self._from_form)
        for w in (self.f_rotate, self.f_keep_above, self.f_ocr_exclude):
            w.toggled.connect(self._from_form)
        self.f_color.editingFinished.connect(self._from_form)
        self.f_notes.textChanged.connect(self._from_form)

        # 이미지 좌우의 이전·다음 쪽 버튼
        self.prev_btn, self.next_btn = QToolButton(), QToolButton()
        for b, text, tip, d in ((self.prev_btn, "◀", "이전 쪽 (←, PageUp)", -1),
                                (self.next_btn, "▶", "다음 쪽 (→, PageDown)", 1)):
            b.setText(text)
            b.setToolTip(tip)
            b.setFixedWidth(34)
            b.setSizePolicy(b.sizePolicy().horizontalPolicy(), b.sizePolicy().Policy.Expanding)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda _c=False, dd=d: self.step_page(dd))
        center = QWidget()
        cl = QHBoxLayout(center)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(2)
        cl.addWidget(self.prev_btn)
        cl.addWidget(self.view, 1)
        cl.addWidget(self.next_btn)
        side.setMinimumWidth(340)
        # 낮은 화면(768)에서 폼이 찌그러지지 않게, 모자라면 오른쪽 칸 안에서 스크롤
        side_scroll = QScrollArea()
        side_scroll.setWidgetResizable(True)
        side_scroll.setFrameShape(QFrame.Shape.NoFrame)
        side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        side_scroll.setWidget(side)
        side_scroll.setMinimumWidth(360)
        self.list.setMinimumHeight(90)
        self.list.setMaximumHeight(max(90, (self.list.fontMetrics().height() + 8) * 6 + 8))  # 빈 공간은 폼에게
        help_.setWordWrap(True)
        split = QSplitter()
        split.addWidget(center)
        split.addWidget(side_scroll)
        self.side_scroll = side_scroll
        self.preview = PreviewPanel(self._preview_regions, self._preview_info)
        self.preview.open_page.connect(self._open_from_preview)
        self.preview.card_action.connect(self._preview_card_action)
        self.preview.set_excluded.connect(self.set_page_excluded)
        self.preview.setVisible(False)
        split.addWidget(self.preview)
        split.setSizes([1000, 420])  # 처음에는 카드 칸을 넓게
        self.split = split
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.addLayout(top)
        lay.addLayout(tools)
        lay.addWidget(self.cards_bar)
        lay.addWidget(split, 1)
        self.status = QLabel()
        self.card_info = QLabel()
        self.card_info.setStyleSheet("color: #0071E3; font-weight: bold")
        self.card_info.setMinimumWidth(60)  # 정렬 상태까지 붙어 길어져도 창 최소 폭을 늘리지 않게 (좁으면 잘림)
        self.guide = hint_bar("", "info")  # 다음 할 일 안내 띠
        srow = QHBoxLayout()  # 아래 줄: 미리보기·따로 영역 (위 두 줄은 파일 이름·도구로 이미 넓다)
        srow.addWidget(self.preview_btn)
        srow.addWidget(self.override_btn)
        srow.addWidget(self.align_btn)
        srow.addWidget(self.align_reset_btn)
        srow.addWidget(self.card_info)
        srow.addWidget(self.guide, 1)
        srow.addWidget(self.status)
        lay.addLayout(srow)

        self.page_spin.setValue(int(self.template.get("reference_page", 1) or 1))
        self.page_spin.valueChanged.connect(lambda _v: self._change_page())
        self.file_combo.currentIndexChanged.connect(self._on_file_combo)
        self._update_form_enabled()
        self._sync_guide()

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
        if hasattr(self, "_card_store"):
            del self._card_store
        self._ov_key = None
        self.__dict__.pop("_shown_cache", None)
        self.per_card.blockSignals(True)
        self.per_card.setChecked(self.template.get("layout") == "per_card")
        self.per_card.blockSignals(False)
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
        self._sync_preview(force=True)
        self.status.setText(f"그룹: {g['name']}")

    def _toggle_help(self, shown: bool):
        self.help_label.setVisible(shown)
        self.help_btn.setArrowType(Qt.ArrowType.DownArrow if shown else Qt.ArrowType.RightArrow)

    def _confirm_leave(self) -> bool:
        """바뀐 영역이 있으면 저장할지 묻는다. 계속해도 되면 True."""
        if not self.dirty:
            return True
        r = ask_choice(self, "저장하지 않은 변경", f"‘{self.group_dir.name}’의 바뀐 영역을 저장할까요?",
                       [("저장", "accept"), ("저장 안 함", "destructive"), ("취소", "reject")], default=0)
        if r is None or r == 2:
            return False
        if r == 0:
            return self.save()
        self.dirty = False
        return True

    def _shown(self, fd: Path | None = None) -> list[int]:
        """이 파일에서 보일 쪽 (뺄 쪽은 건너뛴다)."""
        fd = fd or self.current_file()
        cache = self.__dict__.setdefault("_shown_cache", {})
        if fd not in cache:
            cache[fd] = service.shown_pages(fd)
        return cache[fd]

    def _load_page(self):
        fd = self.current_file()
        n = service.page_count(fd)
        shown = self._shown(fd)
        self.page_spin.blockSignals(True)
        self.page_spin.setMaximum(max(1, n))
        if self.page_spin.value() not in shown:  # 뺀 쪽이면 가까운 보일 쪽으로
            v = self.page_spin.value()
            self.page_spin.setValue(next((p for p in shown if p >= v), shown[-1]))
        self.page_spin.blockSignals(False)
        hidden = n - len(shown)
        self.page_total.setText(f"/ {n}" + (f" (뺀 쪽 {hidden}개 건너뜀)" if hidden else ""))
        self._sync_nav()
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

    def step_page(self, d: int):
        """이전·다음 쪽 (뺄 쪽은 건너뛰고, 파일 끝에서는 멈춘다). 영역은 같은 비율로 다음 카드에 겹쳐 보인다."""
        v, shown = self.page_spin.value(), self._shown()
        p = next((q for q in (shown if d > 0 else reversed(shown)) if (q > v if d > 0 else q < v)), None)
        if p is not None:
            self.page_spin.setValue(p)  # valueChanged -> _change_page
        self._sync_nav()

    def _sync_nav(self):
        if hasattr(self, "prev_btn"):
            v, shown = self.page_spin.value(), self._shown()
            self.prev_btn.setEnabled(any(p < v for p in shown))
            self.next_btn.setEnabled(any(p > v for p in shown))

    def _change_page(self):
        shown = self._shown()
        v = self.page_spin.value()
        if v not in shown:  # 쪽 번호를 직접 넣어 뺀 쪽에 오면 움직인 쪽으로 가까운 보일 쪽에
            prev = getattr(self, "_last_page", v)
            nxt = (next((p for p in shown if p > v), None) if v >= prev
                   else next((p for p in reversed(shown) if p < v), None))
            nxt = nxt or min(shown, key=lambda p: abs(p - v))
            self.page_spin.blockSignals(True)
            self.page_spin.setValue(nxt)
            self.page_spin.blockSignals(False)
        self._last_page = self.page_spin.value()
        self._change_page_impl()

    def _change_page_impl(self):
        if self.is_per_card():  # 카드마다 영역: 보던 카드의 영역을 넣어 두고 새 카드의 영역을 꺼낸다
            self._stash_card()
            self._clear_items()
            self._load_page()
            self._current_key = self.card_key()
            self._load_card(self._current_key)
            self.view.fit()
            self._sync_preview()
            return
        leaving = self._ov_key
        cur = self._items_to_dicts()
        if leaving:  # 따로 영역을 보던 카드: 고친 것을 넣어 둔다
            self._cards()[leaving] = {"regions": cur[0], "protect_rects": cur[1]}
        else:
            self._common = cur
        ratios = [(it, self.ratio_of(it)) for it in self.items()]
        self._load_page()
        ov = self._cards().get(self.card_key())
        self._view_t = None if ov is not None else self._align_t()  # 새 카드의 자동 정렬 (따로 영역이면 없음)
        if ov is not None:  # 이 카드만 따로 영역 (영역을 다 지운 빈 것도: 그 카드는 처리하지 않는다)
            self._clear_items()
            self._load_dicts(ov.get("regions", []), ov.get("protect_rects", []))
            self._ov_key = self.card_key()
        elif leaving:  # 따로 영역 카드에서 공통 영역 카드로
            self._clear_items()
            self._load_dicts(*self._common)
            self._ov_key = None
        else:
            for it, r in ratios:  # 다른 카드에 같은 비율로 겹쳐 본다 (그 카드의 정렬만큼 옮겨)
                self._place(it, r)
        self._snap_shared()
        self._update_card_info()
        self.view.fit()
        self._sync_preview()

    # ------------------------------------------------------------ 카드마다 영역
    def is_per_card(self) -> bool:
        return hasattr(self, "per_card") and self.per_card.isChecked()

    def card_key(self) -> str:
        return f"{self.current_file().name}/{self.page_spin.value()}"

    def _cards(self) -> dict:
        """카드 키 -> {"regions": [...], "protect_rects": [...]} (편집 중인 값)."""
        if not hasattr(self, "_card_store"):
            self._card_store = {k: copy.deepcopy(v) for k, v in (self.template.get("page_regions") or {}).items()}
        return self._card_store

    def _items_to_dicts(self) -> tuple[list[dict], list[dict]]:
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
        return regions, protect

    def _stash_card(self):
        key = getattr(self, "_current_key", None)
        if key is None:
            return
        regions, protect = self._items_to_dicts()
        if regions or protect:
            self._cards()[key] = {"regions": regions, "protect_rects": protect}
        else:
            self._cards().pop(key, None)

    def _clear_items(self):
        for it in self.items():
            self.scene.removeItem(it)
        self._order = []

    def _load_card(self, key: str):
        ps = self._cards().get(key) or {}
        self._order = []
        for r in ps.get("regions", []):
            self._add_item(copy.deepcopy(r), protect=False)
        for r in ps.get("protect_rects", []):
            self._add_item(copy.deepcopy(r), protect=True)
        self._rebuild_list()
        self._update_card_info()

    def _sync_guide(self):
        """아래 안내 띠: 지금 무엇을 하면 되는지 (위에서 먼저 맞는 것)."""
        if not hasattr(self, "guide"):
            return
        has = any(i.kind != "protect" for i in self.items())
        left = 0
        if self.is_per_card():
            done = sum(1 for v in self._cards().values() if v.get("regions"))
            keys = set(self.all_card_keys())
            left = max(0, len(keys) - sum(1 for k, v in self._cards().items() if k in keys and v.get("regions")))
        if not has:
            set_hint(self.guide, "위의 ‘+ 글자 영역’(T)을 누르고, 번역할 글자 위를 끌어 영역을 그리세요.", "todo")
        elif self.dirty:
            set_hint(self.guide, "저장하지 않은 변경이 있습니다. Ctrl+S로 저장하세요.", "warn")
        elif left:
            set_hint(self.guide, f"영역이 없는 카드가 {left}장 남았습니다. ▶로 넘기며 지정하거나 ‘나머지 카드에 복사’를 쓰세요.", "todo")
        else:
            set_hint(self.guide, "저장했습니다. 다 지정했으면 창을 닫고 프로젝트 화면에서 ‘① 글자 추출’을 누르세요.", "done")

    def _update_card_info(self):
        try:
            self._update_card_info_impl()
        finally:
            self._sync_guide()

    def _update_card_info_impl(self):
        if hasattr(self, "cards_bar"):
            self.cards_bar.setVisible(self.is_per_card())
        if hasattr(self, "override_btn"):
            self.override_btn.setVisible(not self.is_per_card())
            self._syncing_override = True
            self.override_btn.setChecked(bool(getattr(self, "_ov_key", None)) and not self.is_per_card())
            self._syncing_override = False
        if hasattr(self, "align_btn"):
            self.align_btn.setVisible(not self.is_per_card())
            self.align_reset_btn.setVisible(not self.is_per_card())
        if not self.is_per_card():
            n_ov = len(self.overrides())
            al = self._align_label()
            self.card_info.setText(" · ".join(x for x in (
                "이 카드: 따로 영역" if getattr(self, "_ov_key", None) else "",
                f"따로 영역 카드 {n_ov}장" if n_ov else "", al) if x))
            e = None if getattr(self, "_ov_key", None) else self._align_pages().get(self.page_spin.value())
            self.align_reset_btn.setEnabled(bool(e) and e.get("status") != "ref")
            for w in (self.copy_prev, self.copy_rest, self.sync_names):
                w.setEnabled(False)
            return
        for w in (self.copy_rest, self.sync_names):
            w.setEnabled(True)
        n = len([i for i in self.items() if i.kind != "protect"])
        keys = set(self.all_card_keys())  # 뺄 쪽은 세지 않는다
        done = sum(1 for k, v in self._cards().items() if k in keys and v.get("regions"))
        total = len(keys)
        self.card_info.setText(f"이 카드: {'영역 ' + str(n) + '개' if n else '영역 없음'} · 지정한 카드 {done}/{total}장")
        self.copy_prev.setEnabled(True)

    def _align_label(self) -> str:
        """지금 카드의 정렬 상태 글 (공통 영역 그룹, 따로 영역이 아닌 카드)."""
        if self.is_per_card() or getattr(self, "_ov_key", None):
            return ""
        e = self._align_pages().get(self.page_spin.value())
        if not e:
            return ""
        st, score = e.get("status"), float(e.get("score") or 0.0)
        if st == "ref":
            return "정렬 기준 카드"
        if st == "manual":
            return "정렬: 손으로 맞춤"
        if st == "check":
            return f"정렬 확인 필요 (점수 {score:.2f}" + (")" if e.get("applied") else ", 적용 안 함)")
        return f"정렬: 자동 (점수 {score:.2f})"

    def copy_previous_card(self):
        """바로 앞(없으면 가장 가까운 앞) 카드의 영역을 이 카드로 복사한다 (비슷한 카드를 빨리 정하기)."""
        if not self.is_per_card():
            return
        self._stash_card()
        name, p = self.current_file().name, self.page_spin.value()
        src = next((self._cards()[f"{name}/{q}"] for q in range(p - 1, 0, -1)
                    if self._cards().get(f"{name}/{q}", {}).get("regions")), None)
        if src is None:
            QMessageBox.information(self, "이전 카드", "앞쪽에 영역을 정한 카드가 없습니다")
            return
        self._clear_items()
        self._cards()[self.card_key()] = copy.deepcopy(src)
        self._load_card(self.card_key())
        self._mark_dirty()

    def all_card_keys(self) -> list[str]:
        """이 그룹의 모든 카드 키 (파일 순서, 쪽 순서)."""
        return [f"{f.name}/{q}" for f in self.file_dirs for q in self._shown(f)]  # 뺄 쪽은 빼고

    def copy_to_rest(self):
        """지금 카드의 영역을 나머지 카드에 복사한다 (영역 없는 카드에만 / 모두 덮어쓰기)."""
        if not self.is_per_card():
            return
        self._stash_card()
        src = self._cards().get(self.card_key())
        if not src or not src.get("regions"):
            QMessageBox.information(self, "나머지 카드에 복사", "이 카드에 영역이 없습니다. 먼저 영역을 그리세요.")
            return
        others = [k for k in self.all_card_keys() if k != self.card_key()]
        empty = [k for k in others if not (self._cards().get(k) or {}).get("regions")]
        r = ask_choice(self, "나머지 카드에 복사",
                       f"이 카드의 영역 {len(src['regions'])}개를 다른 카드에 복사합니다.",
                       [(f"영역 없는 카드에만 ({len(empty)}장)", "accept"),
                        (f"모든 카드 덮어쓰기 ({len(others)}장)", "destructive"), ("취소", "reject")], default=0,
                       informative="저장하기 전까지는 파일에 쓰지 않습니다. 카드마다 위치가 다르면 복사한 뒤 쪽을 넘기며 고치세요.")
        if r not in (0, 1):
            return
        targets = empty if r == 0 else others
        for k in targets:
            self._cards()[k] = copy.deepcopy(src)
        self._mark_dirty()
        self.status.setText(f"영역을 {len(targets)}장에 복사했습니다")

    def reset_regions(self):
        """영역 비우기: 이 카드만 / 모든 카드 (카드마다 영역이 아니면 지금 영역 전부)."""
        if not self.is_per_card():
            if not self.items():
                return
            if QMessageBox.question(self, "초기화", "이 그룹의 영역을 모두 지울까요? (저장하기 전까지는 파일에 쓰지 않습니다)") \
                    != QMessageBox.StandardButton.Yes:
                return
            self._clear_items()
            self._rebuild_list()
            self._to_form(None)
            self._update_form_enabled()
            self._mark_dirty()
            self.status.setText("영역을 모두 지웠습니다 (저장해야 반영됩니다)")
            return
        self._stash_card()
        n_all = sum(1 for v in self._cards().values() if v.get("regions") or v.get("protect_rects"))
        r = ask_choice(self, "초기화", "어느 카드의 영역을 비울까요?",
                       [("이 카드만", "accept"), (f"모든 카드 ({n_all}장)", "destructive"), ("취소", "reject")],
                       default=0, informative="저장하기 전까지는 파일에 쓰지 않습니다 (닫을 때 ‘저장 안 함’이면 그대로).")
        if r not in (0, 1):
            return
        if r == 1:
            self._card_store = {}
        else:
            self._cards().pop(self.card_key(), None)
        self._clear_items()
        self._load_card(self.card_key())
        self._to_form(None)
        self._update_form_enabled()
        self._mark_dirty()
        self.status.setText("이 카드의 영역을 비웠습니다" if r == 0 else "모든 카드의 영역을 비웠습니다")

    def jump_page(self, where: int):
        shown = self._shown()
        self.page_spin.setValue(shown[0] if where == 0 else shown[-1])
        self._sync_nav()

    def _key_action(self, act: str):
        if act in ("add_text", "add_tag", "add_protect", "add_ocr_exclude"):
            kind = {"add_text": "text", "add_tag": "tag_pills", "add_protect": "protect",
                    "add_ocr_exclude": "ocr_exclude"}[act]
            self._set_add_kind(None if self.scene.add_kind == kind else kind)
        elif act == "fit":
            self.view.fit_again()
        elif act == "toggle_preview":
            self.preview_btn.toggle()
        elif act == "rotate":
            it = self.selected()
            if it is not None and it.kind != "protect":
                self.f_rotate.setChecked(not self.f_rotate.isChecked())  # 폼을 거쳐 저장·같은 이름 적용
        elif act in ("next_region", "prev_region") and self._order:
            cur = self.selected()
            i = self._order.index(cur) if cur in self._order else -1
            j = (i + (1 if act == "next_region" else -1)) % len(self._order)
            self.scene.clearSelection()
            self._order[j].setSelected(True)
            self.view.setFocus()

    def _unique_name(self, base: str) -> str:
        names = {i.name for i in self.items()}
        if base not in names:
            return base
        stem = base.rstrip("0123456789") or base
        n = 2
        while f"{stem}{n}" in names:
            n += 1
        return f"{stem}{n}"

    def copy_regions(self):
        """고른 영역(없으면 이 카드의 모든 영역)을 복사해 둔다. 다른 카드에서 Ctrl+V."""
        if self.focusWidget() in (self.f_name, self.f_color, self.f_notes):
            return
        sel = [i for i in self.scene.selectedItems() if isinstance(i, RegionItem)] or self.items()
        self._clip = []
        for it in sel:
            d = copy.deepcopy(it.data)
            d["rect"] = self.ratio_of(it)
            self._clip.append({"data": d, "protect": it.kind == "protect"})
        self.status.setText(f"영역 {len(self._clip)}개를 복사했습니다 (Ctrl+V로 붙여넣기)")

    def paste_regions(self):
        if self.focusWidget() in (self.f_name, self.f_color, self.f_notes) or not self._clip:
            return
        self.scene.clearSelection()
        last = None
        for c in self._clip:
            d = copy.deepcopy(c["data"])
            d["name"] = self._unique_name(d.get("name") or "본문")
            last = self._add_item(d, protect=c["protect"])
            last.setSelected(True)
        self._rebuild_list(last)
        self._mark_dirty()
        self.status.setText(f"영역 {len(self._clip)}개를 붙여 넣었습니다")

    def duplicate_selected(self):
        it = self.selected()
        if it is None:
            return
        d = copy.deepcopy(it.data)
        r = self.ratio_of(it)
        r["x"], r["y"] = min(1 - r["w"], r["x"] + 0.02), min(1 - r["h"], r["y"] + 0.02)
        d["rect"] = r
        d["name"] = self._unique_name(it.name or "본문")
        new = self._add_item(d, protect=it.kind == "protect")
        self.scene.clearSelection()
        new.setSelected(True)
        self._rebuild_list(new)
        self._mark_dirty()

    def _sync_same_name(self, old_name: str, it: RegionItem) -> int:
        """카드마다 영역: 이 영역의 설정(위치 빼고)을 다른 카드의 같은 이름 영역에 적용한다. 바꾼 카드 수."""
        if not (self.is_per_card() and self.sync_names.isChecked()) or not old_name:
            return 0
        n = 0
        cur = self.card_key()
        new_name = it.data.get("name") or old_name
        for key, card in self._cards().items():
            if key == cur:
                continue
            names = {r.get("name") for r in card.get("regions", [])} | {r.get("name") for r in card.get("protect_rects", [])}
            for lst in (card.get("regions", []), card.get("protect_rects", [])):
                for r in lst:
                    if r.get("name") != old_name:
                        continue
                    if new_name != old_name and new_name in names:
                        continue  # 그 카드에 이미 같은 새 이름이 있으면 이름은 그대로
                    for k in ("style", "erase", "notes", "extract"):
                        if k in it.data:
                            r[k] = copy.deepcopy(it.data[k])
                        else:
                            r.pop(k, None)
                    if it.kind != "protect" and lst is card.get("regions"):
                        r["kind"] = it.kind
                    r["name"] = new_name
                    n += 1
        return n

    def _toggle_per_card(self, on: bool):
        if on:
            msg = ("카드마다 영역을 정합니다. 지금 영역은 모든 카드에 그대로 남고,\n"
                   "카드마다 다른 곳만 쪽을 넘기며 고치면 됩니다.")
        else:
            msg = ("모든 카드가 같은 영역을 쓰도록 바꿉니다. 지금 카드의 영역이 공통 영역이 되고,\n"
                   "다른 카드에 정한 영역은 저장할 때 지워집니다.")
        if QMessageBox.question(self, "영역 방식 바꾸기", msg) != QMessageBox.StandardButton.Yes:
            self.per_card.blockSignals(True)
            self.per_card.setChecked(not on)
            self.per_card.blockSignals(False)
            return
        if on:
            items = self._items_to_dicts()  # 공통 영역이면 기준 카드 좌표
            common = self._common if self._ov_key else items
            self._view_t = None  # 카드마다 영역은 자동 정렬 없이 보이는 자리 그대로
            for it in self.items():
                it._placed = None
            shown = self._items_to_dicts()
            overrides = {k: v for k, v in self._cards().items() if v.get("regions") or v.get("protect_rects")}
            if self._ov_key:
                overrides[self._ov_key] = {"regions": items[0], "protect_rects": items[1]}
            self._card_store = {}
            self._current_key = self.card_key()
            self._ov_key = None
            # 공통 영역을 쓰던 모든 카드에 그대로 남기고(자동 정렬한 카드는 맞춘 자리로), 따로 영역을 둔 카드는 그 영역 그대로
            for k in self.all_card_keys():
                f, _, pg = k.rpartition("/")
                t = self._align_t(next((fd for fd in self.file_dirs if fd.name == f), None), int(pg))
                self._card_store[k] = copy.deepcopy(overrides.get(k) or {
                    "regions": [self._aligned_dict(r, t) for r in common[0]],
                    "protect_rects": [self._aligned_dict(r, t) for r in common[1]]})
            self._card_store[self._current_key] = {"regions": shown[0], "protect_rects": shown[1]}
        else:
            self._card_store = {}
            self._current_key = None
            self._ov_key = None
            self._view_t = self._align_t()  # 지금 카드의 영역이 공통 영역: 이 카드의 정렬을 거꾸로 해 기준 좌표로
            self._common = self._items_to_dicts()
        self._update_card_info()
        self._mark_dirty()

    def _load_regions(self):
        self._order = []
        self._align_cache = {}
        if self.template.get("layout") == "per_card":
            self._view_t = None
            self._card_store = {k: copy.deepcopy(v) for k, v in (self.template.get("page_regions") or {}).items()}
            self._current_key = self.card_key()
            self._load_card(self._current_key)
            return
        self._current_key = None
        self._ov_key = None
        self._common = (copy.deepcopy(self.template.get("regions", [])), copy.deepcopy(self.template.get("protect_rects", [])))
        ov = self._cards().get(self.card_key())
        if ov is not None:  # 처음 보이는 카드에 따로 영역이 있으면 그것
            self._view_t = None
            self._load_dicts(ov.get("regions", []), ov.get("protect_rects", []))
            self._ov_key = self.card_key()
        else:
            self._view_t = self._align_t()
            self._load_dicts(*self._common)
        self._snap_shared()
        self._update_card_info()

    def _add_item(self, data: dict, protect: bool) -> RegionItem:
        ratio = data.get("rect") or {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.1}
        it = RegionItem(self.rect_from_ratio(ratio), data, protect)
        self.scene.addItem(it)
        it._placed = (QRectF(it.rect()), dict(ratio), self._view_t)
        it.refresh()
        self._order.append(it)
        return it

    def _load_dicts(self, regions: list[dict], protect: list[dict]):
        for r in regions:
            self._add_item(copy.deepcopy(r), protect=False)
        for r in protect:
            self._add_item(copy.deepcopy(r), protect=True)
        self._rebuild_list()

    # ------------------------------------------------------------ 이 카드만 따로 영역 (공통 영역 그룹)
    def _snap_shared(self):
        """공통 영역 카드를 보여 줄 때(불러오기·쪽 넘기기·저장) 공통 영역의 모양(기준 카드 좌표)을 적어 둔다.
        ‘이 카드만 따로 영역’을 켜면 그 뒤에 이 카드에서 고친 것은 공통 영역이 아니라 이 카드 것이 되어야 한다."""
        if self.is_per_card() or self._ov_key:
            self._shared_snap = None
            return
        self._shared_snap = self._items_to_dicts()

    def _toggle_override(self, on: bool):
        if getattr(self, "_syncing_override", False) or self.is_per_card():
            return
        key = self.card_key()
        if on and self._ov_key != key:
            # 공통 영역은 이 카드를 불러올 때(또는 저장할 때) 모양 그대로 둔다 (기준 카드 좌표).
            # 지금 화면에서 고친 것은 이 카드의 따로 영역이 된다
            snap = getattr(self, "_shared_snap", None)
            self._common = copy.deepcopy(snap) if snap is not None else self._items_to_dicts()
            self._view_t = None  # 따로 영역은 이 카드에서 보이는 자리 그대로 (자동 정렬을 적용한 자리)
            for it in self.items():
                it._placed = None
            regions, protect = self._items_to_dicts()
            self._cards()[key] = {"regions": copy.deepcopy(regions), "protect_rects": copy.deepcopy(protect)}
            self._ov_key = key
            self.status.setText(f"{self.page_spin.value()}쪽만 따로 영역을 씁니다. 이 카드에서 고친 영역은 이 카드에만 씁니다 "
                                "(다른 카드는 그대로)")
            if hasattr(self, "preview"):
                self.preview.schedule()
        elif not on and self._ov_key == key:
            self._cards().pop(key, None)
            self._clear_items()
            self._view_t = self._align_t()
            self._load_dicts(*self._common)
            self._ov_key = None
            self.status.setText(f"{self.page_spin.value()}쪽을 공통 영역으로 되돌렸습니다")
        self._mark_dirty()

    @staticmethod
    def _override_with_common_style(ov: dict, common: list[dict]) -> dict:
        """따로 영역은 위치·크기만 따로: 같은 이름의 공통 영역이 있으면 서식·지우기 방식 등은 공통 영역 것을 쓴다
        (공통 영역의 글꼴을 나중에 바꿔도 따로 영역 카드만 옛 글꼴로 남지 않게)."""
        by = {r.get("name"): r for r in common}
        regions = []
        for r in ov.get("regions", []):
            c = by.get(r.get("name"))
            regions.append({**copy.deepcopy(c), "rect": r["rect"]} if c else r)
        return {**ov, "regions": regions}

    def overrides(self) -> list[str]:
        """따로 영역을 둔 카드 키 (공통 영역 그룹)."""
        if self.is_per_card():
            return []
        return list(self._cards())

    # ------------------------------------------------------------ 미리보기 (오른쪽 4×4)
    def _toggle_preview(self, on: bool):
        self.side_scroll.setVisible(not on)
        self.preview.setVisible(on)
        w = max(800, self.split.width())
        if on:
            self.split.setSizes([int(w * 0.47), 0, int(w * 0.53)])
            self._sync_preview(force=True)
        else:
            self.split.setSizes([int(w * 0.72), int(w * 0.28), 0])
        self.view.fit_again()

    def _sync_preview(self, force: bool = False):
        if not hasattr(self, "preview") or not self.preview.isVisible():
            return
        fd = self.current_file()
        if force or self.preview.file_dir != fd:
            self.preview.set_file(fd, list(range(1, self._page_count(fd) + 1)), self.page_spin.value())
        else:
            self.preview.show_page(self.page_spin.value())

    def _page_count(self, fd: Path) -> int:
        cache = self.__dict__.setdefault("_count_cache", {})
        if fd not in cache:
            cache[fd] = service.page_count(fd)  # PDF를 연다: 미리보기를 그릴 때마다 열지 않게
        return cache[fd]

    def _excluded(self, fd: Path | None = None) -> set[int]:
        fd = fd or self.current_file()
        shown = set(self._shown(fd))
        n = self._page_count(fd)
        return set() if len(shown) >= n else set(range(1, n + 1)) - shown

    def _preview_regions(self, page: int) -> list[dict]:
        """미리보기에 그릴 그 카드의 영역 (지금 카드는 편집 중인 값). protect는 kind="protect"."""
        t = None  # 공통 영역(기준 카드 좌표)이면 그 카드의 자동 정렬만큼 옮겨 그린다
        if page == self.page_spin.value():
            regions, protect = self._items_to_dicts()
            t = self._view_t
        else:
            key = f"{self.current_file().name}/{page}"
            ov = self._cards().get(key)
            if self.is_per_card() or ov is not None:
                regions, protect = (ov or {}).get("regions", []), (ov or {}).get("protect_rects", [])
            else:
                regions, protect = self._common if self._ov_key else self._items_to_dicts()
                t = self._align_t(page=page)
        if t:
            regions, protect = [self._aligned_dict(r, t) for r in regions], [self._aligned_dict(p, t) for p in protect]
        return list(regions) + [{**p, "kind": "protect"} for p in protect]

    def _preview_info(self, page: int) -> tuple[bool, bool, bool, str]:
        """(지금 카드?, 뺐나?, 따로 영역?, 정렬 상태: "" | ref | ok | check | manual)."""
        key = f"{self.current_file().name}/{page}"
        ov = not self.is_per_card() and key in self.overrides()
        st = "" if self.is_per_card() or ov else (self._align_pages().get(page) or {}).get("status", "")
        return page == self.page_spin.value(), page in self._excluded(), ov, st

    def _open_from_preview(self, page: int):
        if page in self._excluded():
            self.status.setText(f"{page}쪽은 뺀 쪽입니다. 되살리면(뺌 체크 해제) 편집할 수 있습니다")
            return
        self.page_spin.setValue(page)
        self.view.setFocus()

    def _preview_card_action(self, page: int, act: str):
        if self.is_per_card():
            self.status.setText("카드마다 영역 지정 중에는 카드마다 이미 따로 영역입니다")
            return
        if page in self._excluded():
            self.status.setText(f"{page}쪽은 뺀 쪽입니다")
            return
        if page != self.page_spin.value():
            self.page_spin.setValue(page)
        self.override_btn.setChecked(act == "override")

    def set_page_excluded(self, page: int, on: bool):
        """미리보기에서 뺄 쪽 바꾸기 (바로 file.json에 쓴다: 영역 저장과 따로)."""
        fd = self.current_file()
        excl = self._excluded(fd)
        new = sorted(excl | {page}) if on else sorted(excl - {page})
        if on and len(new) >= self._page_count(fd):
            QMessageBox.information(self, "뺄 쪽", "모든 쪽을 뺄 수는 없습니다")
            self.preview.refresh()
            return
        try:
            service.set_pages(fd, new)
        except ValueError as e:
            show_problem(self, "뺄 쪽을 바꾸지 못했습니다", str(e))
            return
        self.__dict__.setdefault("_shown_cache", {}).pop(fd, None)
        self.status.setText(f"{page}쪽을 뺐습니다" if on else f"{page}쪽을 되살렸습니다")
        if on and page == self.page_spin.value():
            self._change_page()  # 뺀 쪽에서는 가까운 보일 쪽으로
        else:
            self._load_page()  # ‘/ 20 (뺀 쪽 n개 건너뜀)’
        self.preview.refresh()

    def showEvent(self, ev):
        super().showEvent(ev)
        self.view.fit()
        from PySide6.QtCore import QTimer

        QTimer.singleShot(0, self.view.fit)  # 창 배치가 끝난 뒤 크기로 다시 맞춘다

    # ------------------------------------------------------------ 좌표
    def ratio_of(self, it: RegionItem) -> dict:
        """상자의 비율 좌표. 자동 정렬한 카드를 보는 중이면 기준 카드 좌표로 되돌린 값 (공통 영역은 기준 좌표로 저장)."""
        placed = getattr(it, "_placed", None)
        if placed is not None and placed[0] == it.rect() and placed[2] == self._view_t:
            return dict(placed[1])  # 놓은 뒤 움직이지 않았으면 놓을 때 값 그대로 (쪽 밖 자르기·반올림이 쌓이지 않게)
        pr = self.scene.page_rect
        r = it.rect()
        d = {"x": r.left() / pr.width(), "y": r.top() / pr.height(),
             "w": r.width() / pr.width(), "h": r.height() / pr.height()}
        if self._view_t:
            d = service.align_rect(d, self._view_t, inverse=True)
        return {k: round(v, 4) for k, v in d.items()}

    def rect_from_ratio(self, d: dict) -> QRectF:
        """비율 좌표 -> 장면 사각형 (자동 정렬한 카드면 그 카드의 정렬만큼 옮긴 자리)."""
        if self._view_t:
            d = service.align_rect(d, self._view_t)
        pr = self.scene.page_rect
        return QRectF(float(d["x"]) * pr.width(), float(d["y"]) * pr.height(),
                      float(d["w"]) * pr.width(), float(d["h"]) * pr.height())

    def _place(self, it: RegionItem, ratio: dict) -> None:
        """상자를 비율 좌표 자리에 놓고 그 값을 기억한다 (ratio_of가 그대로 돌려준다)."""
        it.set_scene_rect(self.rect_from_ratio(ratio))
        it._placed = (QRectF(it.rect()), dict(ratio), self._view_t)

    # ------------------------------------------------------------ 자동 정렬
    def _align_pages(self, fd: Path | None = None) -> dict:
        """{쪽: 정렬 값} (파일마다 한 번 읽어 둔다)."""
        fd = fd or self.current_file()
        if fd not in self._align_cache:
            try:
                self._align_cache[fd] = service.align_info(fd)["pages"]
            except Exception:  # noqa: BLE001 - 정렬 파일이 깨졌으면 정렬 없음으로
                self._align_cache[fd] = {}
        return self._align_cache[fd]

    def _align_t(self, fd: Path | None = None, page: int | None = None) -> dict | None:
        """그 카드(기본: 지금 카드)에 적용하는 정렬 변환 (공통 영역 그룹에서만. 없으면 None).
        따로 영역 카드인지는 부르는 쪽이 본다."""
        if self.is_per_card():
            return None
        fd = fd or self.current_file()
        e = self._align_pages(fd).get(self.page_spin.value() if page is None else int(page))
        if not e or not e.get("applied"):
            return None
        return {k: float(e.get(k, d)) for k, d in (("dx", 0.0), ("dy", 0.0), ("sx", 1.0), ("sy", 1.0))}

    @staticmethod
    def _aligned_dict(r: dict, t: dict | None) -> dict:
        """영역 dict의 rect만 정렬만큼 옮긴 사본."""
        return {**r, "rect": service.align_rect(r["rect"], t)} if t and r.get("rect") else dict(r)

    def _reapply_alignment(self):
        """정렬 값이 바뀐 뒤: 상자를 같은 기준 좌표의 새 자리로 옮긴다 (영역 자체는 바뀌지 않음)."""
        ratios = [(it, self.ratio_of(it)) for it in self.items()]
        self._view_t = None if (self.is_per_card() or self._ov_key) else self._align_t()
        for it, r in ratios:
            self._place(it, r)
        sel = self.selected()
        if sel is not None:
            self._to_form(sel)
        self._update_card_info()
        if hasattr(self, "preview"):
            self.preview.schedule()

    def _align_reason_blocked(self) -> str:
        """지금 카드의 정렬을 손으로 옮길 수 없는 까닭 (옮길 수 있으면 "")."""
        if self.is_per_card():
            return "카드마다 영역을 정하는 그룹은 정렬 대신 영역을 직접 옮기세요"
        if self._ov_key:
            return "따로 영역 카드는 정렬 대신 영역을 직접 옮기세요"
        if (self._align_pages().get(self.page_spin.value()) or {}).get("status") == "ref":
            return "정렬 기준 카드입니다. 기준 카드에서는 영역 자체를 옮기세요"
        return ""

    def nudge_alignment(self, dx_px: float, dy_px: float):
        """Ctrl+화살표: 이 카드의 정렬만 옮긴다 (모든 상자 함께, 바로 저장)."""
        why = self._align_reason_blocked()
        if why:
            self.status.setText(why)
            return
        pr = self.scene.page_rect
        if pr.isEmpty():
            return
        fd, page = self.current_file(), self.page_spin.value()
        try:
            e = service.nudge_alignment(fd, page, dx_px / pr.width(), dy_px / pr.height())
        except (OSError, ValueError) as ex:
            show_problem(self, "정렬을 옮기지 못했습니다", str(ex))
            return
        if e is None:
            self.status.setText("정렬 기준 카드입니다. 기준 카드에서는 영역 자체를 옮기세요")
            return
        self._align_cache.pop(fd, None)
        self._reapply_alignment()
        self.status.setText(f"{page}쪽 정렬을 옮겼습니다 (손으로 맞춤, 바로 저장)")

    def reset_alignment(self):
        """이 카드 정렬 되돌리기 (손으로 맞춤 -> 자동 값, 자동 -> 정렬 없음)."""
        if self.is_per_card() or self._ov_key:
            return
        fd, page = self.current_file(), self.page_spin.value()
        e = service.reset_alignment(fd, page)
        self._align_cache.pop(fd, None)
        self._reapply_alignment()
        self.status.setText(f"{page}쪽 정렬을 자동 정렬 값으로 되돌렸습니다" if e else f"{page}쪽 정렬을 없앴습니다 (공통 영역 그대로)")

    def run_alignment(self):
        """‘자동 정렬’: 그룹의 모든 카드를 기준 카드에 맞춘다 (작업 스레드, 진행률 창)."""
        if self.is_per_card():
            QMessageBox.information(self, "자동 정렬", "카드마다 영역을 정하는 그룹은 자동 정렬하지 않습니다")
            return
        if self.dirty and not self._confirm_leave():  # 정렬은 저장된 영역(기준 카드·따로 영역 카드)을 본다
            return
        from app.qt import worker

        job = worker.Job(service.align_group, list(self.file_dirs))
        worker.run_with_dialog(self, "카드 맞추는 중", job, self._alignment_done,
                               lambda m: show_problem(self, "자동 정렬을 하지 못했습니다", m))

    def _alignment_done(self, res):
        if res.state != "done":
            QMessageBox.information(self, "자동 정렬", res.reason)
            return
        self._align_cache = {}
        self._reapply_alignment()
        self._sync_preview(force=True)
        msg = f"맞춘 카드 {res.ok_count}장, 정렬 확인 필요 {res.check_count}장"
        self.status.setText(msg)
        more = ("\n\n‘정렬 확인 필요’ 카드는 미리보기(G)의 ‘정렬 확인 카드만’으로 모아 보고, Ctrl+화살표로 맞추거나 "
                "‘이 카드 정렬 되돌리기’를 누르세요." if res.check_count else "")
        QMessageBox.information(self, "자동 정렬", msg + more + "\n\n이미 글자를 읽은(추출한) 카드는 자동으로 다시 읽지 않습니다. "
                                "지운 카드는 다음 ‘글자 지우기’에서 맞춘 자리로 다시 지웁니다.")

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
        self._sync_guide()

    def selected(self) -> RegionItem | None:
        sel = [i for i in self.scene.selectedItems() if isinstance(i, RegionItem)]
        return sel[0] if sel else None

    def _on_list_row(self, row: int):
        if 0 <= row < len(self._order):
            self.scene.clearSelection()
            self._order[row].setSelected(True)
            if not self.view.auto_fit:  # 화면에 맞춰 보는 중이면 카드를 움직이지 않는다
                self.view.ensureVisible(self._order[row], 20, 20)

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
        sel = self.selected() is not None
        self.form_box.setEnabled(sel)
        self.form_box.setVisible(sel)
        self.form_hint.setVisible(not sel)

    def _on_created(self, it: RegionItem):
        if it not in self._order:
            self._order.append(it)
        if it.kind == "ocr_exclude":  # 읽기 제외 = 보호 영역 + 글자 읽기에서도 빼기
            it.kind = "protect"
            it.data["ocr_exclude"] = True
            it.data["kind"] = "protect"
            it.refresh()
        base = {"text": "본문", "tag_pills": "태그", "protect": "읽기 제외" if it.data.get("ocr_exclude") else "보호"}[it.kind]
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
        self._update_card_info()
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
            self.f_align.setCurrentIndex(max(0, [k for k, _t in ALIGN_LABELS].index(st.get("align", ""))
                                             if st.get("align", "") in dict(ALIGN_LABELS) else 0))
            self.f_color.setText(st.get("color", ""))
            ff = (st.get("font") or {}).get("file", "")
            self._fill_fonts(exclude=it)
            idx = self.f_font.findData(ff)
            if idx < 0 and ff:  # 목록에 없는 글꼴 파일도 보여 준다
                self.f_font.addItem(f"{(st.get('font') or {}).get('family') or Path(ff).stem} (목록 밖)", ff)
                idx = self.f_font.count() - 1
            self.f_font.setCurrentIndex(max(0, idx))
            self.f_font_mode.setCurrentIndex(max(0, self.f_font_mode.findData(st.get("font_mode") or "cell")))
            self.f_erase_mode.setCurrentIndex(max(0, self.f_erase_mode.findData(er.get("mode") or "auto")))
            self.f_keep_tall.setValue(float(er.get("keep_ink_taller_than_pt") or 0))
            self.f_keep_above.setChecked(bool(er.get("keep_ink_above_text")))
            self.f_notes.setPlainText(it.data.get("notes", ""))
            prot = it.kind == "protect"
            for w in (self.f_rotate, self.f_align, self.f_color, self.f_font, self.f_keep_tall, self.f_keep_above):
                w.setEnabled(not prot)
            self.f_erase_mode.setEnabled(it.kind == "text")
            self.f_font_mode.setEnabled(it.kind == "text")  # 태그 라벨은 라벨마다 짧은 글이라 칸 단위만
            self.f_ocr_exclude.setEnabled(prot)
            self.f_ocr_exclude.setChecked(bool(it.data.get("ocr_exclude")) and prot)
            self._sync_kind_fields(it.kind)
        finally:
            self._loading = False

    def _sync_kind_fields(self, kind: str):
        """종류에 맞는 칸만: 보호 영역은 한글 넣기·글자 지우기 묶음을 숨기고, 보호 영역 칸은 보호일 때만."""
        prot = kind == "protect"
        self.g_style.setVisible(not prot)
        self.g_erase.setVisible(not prot)
        self.f_ocr_exclude.setVisible(prot)
        lab = self._basic_form.labelForField(self.f_ocr_exclude)
        if lab is not None:
            lab.setVisible(prot)

    def _from_form(self, *_a):
        if self._loading:
            return
        it = self.selected()
        if it is None:
            return
        d = it.data
        old_name = d.get("name", "")
        old_settings = copy.deepcopy({k: d.get(k) for k in ("style", "erase", "notes", "kind")})
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
        if self.f_align.currentData():  # 자동이면 적지 않는다 (그림 카드는 원문 줄의 정렬을 따름)
            st["align"] = self.f_align.currentData()
        else:
            st.pop("align", None)
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
        if self.f_font_mode.currentData() == "sentence" and it.kind == "text":
            st["font_mode"] = "sentence"
        else:
            st.pop("font_mode", None)  # 기본(한 칸 모두 같은 글꼴)은 적지 않는다
        er = d.setdefault("erase", {})
        mode = self.f_erase_mode.currentData()
        if mode and mode != "auto" and it.kind == "text":
            er["mode"] = mode
        else:
            er.pop("mode", None)
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
        if it.kind == "protect":
            if self.f_ocr_exclude.isChecked():
                d["ocr_exclude"] = True
            else:
                d.pop("ocr_exclude", None)
        notes = self.f_notes.toPlainText().strip()
        if notes:
            d["notes"] = notes
        else:
            d.pop("notes", None)
        if not st:
            d.pop("style", None)
        new_settings = {k: d.get(k) for k in ("style", "erase", "notes", "kind")}
        if new_settings != old_settings or d["name"] != old_name:
            n = self._sync_same_name(old_name, it)
            if n:
                self.status.setText(f"다른 카드의 ‘{old_name}’ 영역 {n}개에도 적용했습니다")
        it.refresh()
        self._sync_kind_fields(it.kind)
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
        if self.is_per_card():
            self._stash_card()
            data["layout"] = "per_card"
            data["regions"], data["protect_rects"] = [], []
            data["page_regions"] = {k: v for k, v in self._cards().items() if v.get("regions") or v.get("protect_rects")}
            data.setdefault("reference_page", self.page_spin.value())
            data.setdefault("source_language", "ja")
            return data
        data.pop("layout", None)
        if self._ov_key:  # 따로 영역 카드를 보는 중: 화면의 영역은 그 카드 것, 공통 영역은 넣어 둔 것
            r, p = self._items_to_dicts()
            self._cards()[self._ov_key] = {"regions": r, "protect_rects": p}
            regions, protect = self._common
        else:
            regions, protect = self._items_to_dicts()
        data["regions"] = regions
        data["protect_rects"] = protect
        ov = {k: self._override_with_common_style(v, regions) for k, v in self._cards().items()}
        if ov:  # 이 카드만 따로 영역 (공통 영역 그룹). 빈 것도 남긴다: 그 카드는 처리하지 않는다
            data["page_regions"] = ov
        else:
            data.pop("page_regions", None)
        data.setdefault("reference_page", self.page_spin.value())
        data.setdefault("source_language", "ja")
        return data

    def save(self) -> bool:
        problems = service.save_region_template(self.group_dir, self.to_template())
        if problems:
            show_problem(self, "영역을 저장하지 못했습니다", "", "표시된 영역을 고친 뒤 다시 저장하세요.", items=problems)
            return False
        self.template = service.load_region_template(self.group_dir)
        if self.is_per_card():
            self._card_store = {k: copy.deepcopy(v) for k, v in (self.template.get("page_regions") or {}).items()}
        self._snap_shared()  # 저장한 모양이 새 공통 영역의 기준
        self.dirty = False
        self._update_title()
        self._update_card_info()
        self.status.setText("영역을 저장했습니다")
        self.saved.emit()
        return True

    def _mark_dirty(self):
        self.dirty = True
        self._update_title()
        if hasattr(self, "card_info"):
            self._update_card_info()
        if hasattr(self, "preview"):
            self.preview.schedule()

    def _update_title(self):
        self.setWindowTitle(f"영역 지정 — {self.group_dir.name}{' *' if self.dirty else ''}")
        if hasattr(self, "save_btn"):
            self.save_btn.setStyleSheet(PRIMARY_QSS if self.dirty else "")

    def closeEvent(self, ev):
        if not self._confirm_leave():
            ev.ignore()
            return
        super().closeEvent(ev)
