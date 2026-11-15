"""영역 지정 창의 그림판: 영역 상자(RegionItem)·쪽 장면(PageScene)·확대/이동 보기(PageView) (region_editor에서 나눔)."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QGraphicsRectItem, QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView


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
        self._grabbed = False  # 장면이 grabMouse로 넘겨준 끌기 (놓을 때 풀어 준다)
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
        tol = self.edge_tolerance()
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

    def cursor_at(self, p: QPointF) -> Qt.CursorShape:
        """p(이 영역 좌표)에서 누르면 할 일의 커서: 가장자리면 크기 조절 방향, 아니면 옮기기."""
        return {"t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
                "l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor,
                "tl": Qt.CursorShape.SizeFDiagCursor, "br": Qt.CursorShape.SizeFDiagCursor,
                "tr": Qt.CursorShape.SizeBDiagCursor, "bl": Qt.CursorShape.SizeBDiagCursor}.get(
            self._edges(p), Qt.CursorShape.SizeAllCursor)

    def hoverMoveEvent(self, ev):
        self.setCursor(self.cursor_at(ev.pos()))
        super().hoverMoveEvent(ev)

    def edge_tolerance(self) -> float:
        """가장자리로 보는 거리 (장면 단위 = 화면 EDGE px)."""
        view = self.scene().views()[0] if self.scene() and self.scene().views() else None
        return EDGE / (view.transform().m11() if view else 1.0)

    def start_drag(self, p: QPointF) -> None:
        """p(이 영역 좌표)에서 끌기 시작: 가장자리면 크기 조절, 아니면 옮기기."""
        self.scene().clearSelection()
        self.setSelected(True)
        self._drag = (self._edges(p) or "move", p, QRectF(self.rect()))

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.start_drag(ev.pos())
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
            if self._grabbed:  # 영역 바로 밖 가장자리를 눌러 장면이 넘겨준 끌기
                self._grabbed = False
                self.ungrabMouse()
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
        self._edge_cursor = False  # 상자 바로 밖 가장자리에 크기 조절 커서를 보이는 중

    def region_at(self, pos: QPointF, outside: bool = True) -> "RegionItem | None":
        """그 자리의 맨 위 영역 (상자 또는 이름표를 누름). items()는 위(z가 큰 것)부터 준다.
        쪽 그림(QGraphicsPixmapItem)에 parentItem()을 부르지 않는다: PySide6 6.11~6.12에서 그 호출이 힙을 깨뜨려
        창을 닫거나 앱을 끝낼 때 죽었다 (0xC0000374·0xC0000005). 우리 영역만 골라 본다."""
        regs = [it for it in self.items() if isinstance(it, RegionItem) and it.isVisible()]
        for it in regs:
            lab = it.label
            if it.sceneBoundingRect().contains(pos) or (lab.isVisible() and lab.sceneBoundingRect().contains(pos)):
                return it
        if not outside:
            return None
        for it in regs:  # 어느 영역 안도 아니면: 가장자리를 잡을 수 있는 거리(화면 EDGE px)만큼 상자 밖도 그 영역으로 본다
            tol = it.edge_tolerance()
            if it.rect().adjusted(-tol, -tol, tol, tol).contains(it.mapFromScene(pos)):
                return it
        return None

    def edge_outside(self, pos: QPointF) -> "RegionItem | None":
        """상자 바로 밖 가장자리(화면 EDGE px 안)여서 누르면 크기를 바꿀 영역. 그리기 모드에서는 지금 고른 영역만:
        붙어 있는 다른 영역 바로 옆에서 새 영역을 그리려는 누름이 그 영역 크기 조절로 바뀌지 않게."""
        if self.region_at(pos, outside=False) is not None:
            return None
        it = self.region_at(pos)
        if it is not None and self.add_kind and not it.isSelected():
            return None
        return it

    def mousePressEvent(self, ev):
        target = self.region_at(ev.scenePos(), outside=False) or self.edge_outside(ev.scenePos())
        if target is not None and ev.button() == Qt.MouseButton.LeftButton:
            p = target.mapFromScene(ev.scenePos())
            lab = target.label
            on_label = lab.isVisible() and lab.sceneBoundingRect().contains(ev.scenePos())
            if not on_label and not target.contains(p):
                # 상자 바로 밖 가장자리: Qt는 이 영역에 누름을 보내지 않으므로 직접 끌기를 시작하고 마우스를 넘긴다
                target.start_drag(p)
                target._grabbed = True
                target.grabMouse()
                ev.accept()
                return
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
        if ev.buttons() == Qt.MouseButton.NoButton:
            self._update_edge_cursor(ev.scenePos())
        super().mouseMoveEvent(ev)

    def _update_edge_cursor(self, pos: QPointF) -> None:
        """상자 바로 밖 가장자리에서도 크기 조절 커서를 보인다 (영역의 hover는 상자 안에서만 온다)."""
        views = self.views()
        if not views or getattr(views[0], "_space", False):  # 스페이스로 화면 옮기는 중: 손 커서 그대로
            return
        vp = views[0].viewport()
        it = self.edge_outside(pos)
        if it is not None:
            vp.setCursor(it.cursor_at(it.mapFromScene(pos)))
            self._edge_cursor = True
        elif self._edge_cursor:
            vp.setCursor(Qt.CursorShape.ArrowCursor)
            self._edge_cursor = False

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
