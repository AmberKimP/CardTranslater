"""② 아이콘 정의: 카드 보기·자리 잡기 (아이콘 자리를 그림 위에 보여 주고 다시 잡기)."""

from __future__ import annotations

import functools
import inspect
from pathlib import Path

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDialog, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QVBoxLayout, QWidget)
from app import service
from app.qt import worker
from app.qt.dialogs import ask_choice, fit_to_screen, hint_bar, show_error, show_problem


def busy(fn):
    """카드 보기 조작: 끝날 때까지 창 전체를 잠근다 (다른 버튼·목록·그림을 누르지 못하게).
    조작이 화면을 멈춘 채 도는 동안 누른 클릭은 쌓였다가 끝난 뒤 그대로 실행되므로(예: ‘되돌리기’ 바로 아래
    ‘원본 그대로’가 눌림), 잠근 채로 쌓인 입력을 먼저 처리해 꺼진 버튼에서 버려지게 한 뒤 푼다.
    안에서 다시 부르면(조작 안의 조작) 그대로 실행한다. 신호가 넘기는 여분 인자(clicked의 checked)는 버린다."""
    params = list(inspect.signature(fn).parameters.values())
    if any(p.kind == p.VAR_POSITIONAL for p in params):
        n_args = None
    else:
        n_args = len([p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]) - 1

    @functools.wraps(fn)
    def run(self, *args, **kw):
        if n_args is not None:
            args = args[:n_args]
        if self._busy_depth:
            return fn(self, *args, **kw)
        self._busy_depth += 1
        focus = QApplication.focusWidget()  # 잠그면 포커스가 빠진다: 끝나면 돌려준다 (목록 화살표 키 등)
        self.setEnabled(False)
        self.setCursor(Qt.CursorShape.WaitCursor)  # 이 창에만 (묻는 창은 보통 화살표)
        try:
            return fn(self, *args, **kw)
        finally:
            try:
                QApplication.processEvents()  # 잠근 채로: 그동안 쌓인 클릭은 꺼진 위젯에 가서 버려진다
            finally:
                self._busy_depth -= 1
                self.unsetCursor()
                self.setEnabled(True)
                if focus is not None and self.isVisible() and self.isAncestorOf(focus) and focus.isEnabled() \
                        and focus.isVisible():
                    focus.setFocus()

    return run


# ================================================================ 아이콘 이름
class IconPlaceView(QWidget):
    """카드 영역 그림 위에 아이콘 자리를 보여 주고, 마우스로 끌어 새 자리를 잡는다.

    초록 = 같은 칸에서 이미 찾은 아이콘, 파랑 = 이 아이콘의 지금 자리, 주황 점선 = 예상 위치(못 찾았을 때),
    빨강 = 사람이 새로 잡은 자리. 좌표는 모두 그림 px."""

    drawn = Signal()

    def __init__(self):
        super().__init__()
        self._pm = QPixmap()
        self.marks: list[tuple[list, str]] = []
        self.box = self.guess = self.new = None
        self._drag = None
        self.setMinimumSize(300, 200)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(False)

    def set_data(self, pm: QPixmap, box, guess, marks: list[tuple[list, str]]):
        self._pm, self.box, self.guess, self.marks, self.new, self._drag = pm, box, guess, marks, None, None
        self.update()

    def _geom(self):
        w, h = self._pm.width(), self._pm.height()
        if not w or not h:
            return 1.0, 0.0, 0.0
        s = min((self.width() - 16) / w, (self.height() - 16) / h, 3.0)
        return s, (self.width() - w * s) / 2, (self.height() - h * s) / 2

    def _to_img(self, p) -> tuple[float, float]:
        s, ox, oy = self._geom()
        return (min(max((p.x() - ox) / s, 0), self._pm.width()), min(max((p.y() - oy) / s, 0), self._pm.height()))

    def _rect(self, b) -> QRectF:
        s, ox, oy = self._geom()
        return QRectF(ox + b[0] * s, oy + b[1] * s, (b[2] - b[0]) * s, (b[3] - b[1]) * s)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#FAFAFA"))
        if self._pm.isNull():
            return
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        s, ox, oy = self._geom()
        p.drawPixmap(QRectF(ox, oy, self._pm.width() * s, self._pm.height() * s), self._pm, QRectF(self._pm.rect()))
        f = p.font()
        f.setBold(True)
        p.setFont(f)

        def frame(b, color, label="", dash=False, width=2):
            r = self._rect(b)
            pen = QPen(QColor(color), width)
            if dash:
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r)
            if label:
                fm = p.fontMetrics()
                tw, th = fm.horizontalAdvance(label) + 8, fm.height() + 2
                ty = r.top() - th if r.top() - th >= 0 else r.bottom()
                p.fillRect(QRectF(r.left(), ty, tw, th), QColor(color))
                p.setPen(QColor("white"))
                p.drawText(QRectF(r.left(), ty, tw, th), Qt.AlignmentFlag.AlignCenter, label)

        for b, label, *rest in self.marks:  # (상자, 이름[, 예상 위치인가[, 아이콘 아님·합침으로 정했나[, 목록에서 고름]]])
            if len(rest) > 2 and rest[2]:  # 목록에서 고른 자리: 보라 굵은 테두리
                frame(b, "#7B2CBF", label, dash=bool(rest[0] or rest[1]), width=3)
            elif len(rest) > 1 and rest[1]:  # 아이콘 아님·옆에 합친 자리: 정해진 것이라 회색 점선 (어디였는지만)
                frame(b, "#8A8A8A", label, dash=True, width=1)
            elif rest and rest[0]:  # 위치를 못 찾은 다른 아이콘: 예상 자리에 점선
                frame(b, "#9A6A00", label, dash=True)
            else:
                frame(b, "#2E8B57", label)
        if self.guess and not self.new:
            frame(self.guess, "#E67E22", "예상 위치", dash=True)
        if self.box:
            frame(self.box, "#1F6FEB", "지금 자리", width=3 if not self.new else 2)
        if self.new:
            frame(self.new, "#D62828", "새 자리", width=3)
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and not self._pm.isNull():
            self._drag = self._to_img(e.position())
            self.new = None
            self.update()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            x, y = self._to_img(e.position())
            x0, y0 = self._drag
            self.new = [min(x0, x), min(y0, y), max(x0, x), max(y0, y)]
            self.update()

    def mouseReleaseEvent(self, e):
        if self._drag is None:
            return
        self._drag = None
        if self.new and (self.new[2] - self.new[0] < 4 or self.new[3] - self.new[1] < 4):
            self.new = None  # 그냥 누른 것
        self.update()
        self.drawn.emit()


class IconPlaceDialog(QDialog):
    """아이콘이 나온 카드 자리를 보며, 못 찾았거나 잘못 잘린 아이콘을 마우스로 감싸 직접 잡는다."""

    def __init__(self, project_dir: Path, g: dict, row_label, on_saved, parent=None):
        super().__init__(parent)
        self.project_dir = Path(project_dir)
        self.row_label = row_label  # 묶음 -> ‘3번’ 같은 화면 이름
        self.on_saved = on_saved
        self.outcome: tuple[str, str | None] = ("", None)  # 닫은 뒤 표가 고를 줄: ("saved"|"not_icon", 묶음)
        self.setWindowTitle("카드 보기 · 아이콘 자리 잡기")
        fit_to_screen(self, 1100, 720, maximize_below=0)
        self.where = [dict(w) for w in g.get("where", [])]
        self.combo = QComboBox()
        for w in self.where:
            self.combo.addItem(self._item_text(w), w["occ"])
        self.view = IconPlaceView()
        self.view.drawn.connect(self._drawn)
        self.crop = QLabel()
        self.crop.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.crop.setMinimumWidth(110)
        self.status = QLabel()
        self.status.setWordWrap(True)
        legend = QLabel('<span style="color:#2E8B57">■</span> 이미 찾은 아이콘 &nbsp; '
                        '<span style="color:#1F6FEB">■</span> 이 아이콘의 지금 자리 &nbsp; '
                        '<span style="color:#E67E22">■</span> 예상 위치(못 찾았을 때, 점선) &nbsp; '
                        '<span style="color:#D62828">■</span> 새로 잡은 자리 &nbsp; '
                        '<span style="color:#7B2CBF">■</span> 목록에서 고름 &nbsp; '
                        '<span style="color:#8A8A8A">■</span> 아이콘 아님·포함')
        hint = hint_bar("아이콘을 마우스로 끌어 감싸세요 (테두리까지 조금 넉넉하게). 다시 끌면 새로 잡습니다.\n"
                        "‘이 자리로 저장’을 누르면 그 자리로 그림을 다시 잘라 같은 모양끼리 다시 묶습니다. "
                        "한 상자에 아이콘이 둘이면 하나만 감싸 ‘둘로 나누기’.", "info")
        self.save_btn = QPushButton("이 자리로 저장")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._save)
        self.absorb_btn = QPushButton("옆 아이콘에 포함됨")  # 못 찾은 자리인데 바로 옆 자리의 상자가 이 자리까지 감쌌을 때
        self.absorb_btn.setToolTip("바로 옆에 붙은 아이콘을 잡은 상자가 이 자리까지 감싸고 있으면 누르세요.\n"
                                   "둘을 하나의 아이콘으로 보고 이 자리는 원문에서 뺍니다 (예: 문양 셋이 붙은 아이콘).")
        self.absorb_btn.clicked.connect(self._absorb)
        self.absorb_btn.hide()
        self.overlap_btn = QPushButton("겹친 아이콘 흡수")  # 다른 [[?]] 자리가 이 아이콘 자리와 겹침: 같은 그림을 둘로 읽은 것
        self.overlap_btn.setToolTip("이 아이콘 자리와 겹친 다른 아이콘([[?]])을 이 아이콘에 포함합니다.\n"
                                    "같은 그림을 원문에서 두 번 읽은 경우: 겹친 자리는 원문에서 빠집니다.\n"
                                    "새로 끌어 감쌌으면 그 자리로 저장한 뒤 흡수합니다.")
        self.overlap_btn.clicked.connect(self._absorb_overlaps)
        self.overlap_btn.hide()
        self._overlaps: list[str] = []
        self.split_btn = QPushButton("둘로 나누기")
        self.split_btn.setToolTip("지금 자리(파랑) 안에 아이콘이 둘 있을 때: 그중 하나를 끌어 감싼 뒤 누르세요.\n"
                                  "감싼 것과 나머지가 서로 다른 아이콘이 되고, 원문에 [[?]]가 하나 늘어납니다.")
        self.split_btn.clicked.connect(self._split)
        self.split_btn.setEnabled(False)
        self._place: dict = {}
        self._run_host: str | None = None
        self._cur_occ: str | None = None  # 목록에서 고른 지금 자리 (None이면 위 콤보의 자리)
        self._cell_where: dict[str, dict] = {}  # 이 칸 자리들의 정보 (그림 조각·직접 잡음·카드 이름)
        self._cell_key: frozenset | None = None  # _cell_where를 읽은 자리들 (같으면 다시 읽지 않는다)
        self._named: dict | None = None  # 목록에서 누른 확정 아이콘 줄 (그 아이콘을 다시 잡거나 합칠 때 연다)
        self._items_by_key: dict[str, dict] = {}
        self._kept = False
        self._busy_depth = 0  # 조작 중 (busy): 창 전체를 잠근다
        # ---- 이 칸의 아이콘 목록: 고르고 수정(끌어 감싸 저장)·삭제(아이콘 아님)·합치기·되돌리기
        self.items = QListWidget()
        self.items.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.items.setMinimumWidth(230)
        self.items.setToolTip("이 카드 칸의 아이콘 자리 (원문 순서). 누르면 그 자리를 그림에서 고칩니다.\n"
                              "Ctrl·Shift로 여러 개를 고르면 삭제·합치기를 한꺼번에 합니다.")
        self.items.itemClicked.connect(self._item_clicked)
        self.items.itemSelectionChanged.connect(self._selection_changed)
        self.del_btn = QPushButton("삭제 (아이콘 아님)")
        self.del_btn.setToolTip("고른 자리들을 ‘아이콘 아님’으로 합니다 (확정하면 원문에서 빠짐). 창은 닫지 않습니다.")
        self.del_btn.clicked.connect(self._delete_selected)
        self.merge_btn = QPushButton("합치기")
        self.merge_btn.setToolTip("목록에서 2개 이상 고르면 하나의 아이콘으로 합칩니다 (나머지는 원문에서 빠짐).\n"
                                  "문양 여러 개가 붙은 한 아이콘을 따로 읽었을 때.")
        self.merge_btn.clicked.connect(self._merge_selected)
        self.restore_btn = QPushButton("되돌리기")
        self.restore_btn.setToolTip("‘아이콘 아님’·포함으로 정한 자리를 다시 아이콘 자리로 되돌립니다.")
        self.restore_btn.clicked.connect(self._restore_selected)
        for b in (self.del_btn, self.merge_btn, self.restore_btn):
            b.setEnabled(False)
        self.keep_btn = QPushButton(self.KEEP_TEXT)
        self.keep_btn.setToolTip("이 칸 전체가 아이콘이면: 이 칸은 글자 지우기·한글 넣기를 하지 않고 원본 그대로 둡니다\n"
                                 "(③ 원문 확인의 ‘아이콘만 있음 · 원본 그대로’와 같음). 이 칸의 [[?]] 자리는 ‘아이콘 아님’이 됩니다.")
        self.keep_btn.clicked.connect(self._toggle_keep)
        close = QPushButton("닫기")
        close.clicked.connect(self.accept)
        side = QVBoxLayout()
        side.addWidget(QLabel("잘라 낸 그림:"))
        side.addWidget(self.crop)
        side.addWidget(QLabel("<b>이 칸의 아이콘</b>"))
        side.addWidget(self.items, 1)
        edit_hint = QLabel("수정: 목록에서 고르고 그림에서 끌어 감싼 뒤 ‘이 자리로 저장’")
        edit_hint.setWordWrap(True)
        edit_hint.setStyleSheet("color: #666")
        side.addWidget(edit_hint)
        for b in (self.save_btn, self.del_btn, self.merge_btn, self.restore_btn, self.keep_btn):
            side.addWidget(b)
        side_w = QWidget()
        side_w.setLayout(side)
        side_w.setFixedWidth(260)
        mid = QHBoxLayout()
        mid.addWidget(self.view, 1)
        mid.addWidget(side_w)
        btns = QHBoxLayout()
        btns.addWidget(self.status, 1)
        btns.addWidget(self.absorb_btn)
        btns.addWidget(self.overlap_btn)
        btns.addWidget(self.split_btn)
        btns.addWidget(close)
        lay = QVBoxLayout(self)
        lay.addWidget(hint)
        lay.addWidget(self.combo)
        lay.addLayout(mid, 1)
        lay.addWidget(legend)
        lay.addLayout(btns)
        self.combo.currentIndexChanged.connect(self._combo_changed)
        # 처음 볼 자리: 못 찾은 것 -> 직접 잡은 것(다시 고치러 왔을 때) -> 첫째
        first = next((i for i, w in enumerate(self.where) if not w.get("png")),
                     next((i for i, w in enumerate(self.where) if w.get("manual")), 0))
        if first:
            self.combo.setCurrentIndex(first)  # 신호가 _load를 부른다
        else:
            self._load()

    @staticmethod
    def _item_text(w: dict) -> str:
        tag = " (직접 잡음)" if w.get("manual") else ("" if w.get("png") else " (위치 못 찾음)")
        return w["label"] + tag

    def _combo_changed(self, _i: int) -> None:
        self._cur_occ = None  # 다른 카드: 그 묶음의 자리부터
        self._load()

    def _cur(self) -> dict | None:
        """지금 자리 (목록에서 고른 자리, 없으면 위 콤보의 자리): {"occ", "png", "manual", "label", …}."""
        if self._cur_occ:
            w = next((w for w in self.where if w["occ"] == self._cur_occ), None) or self._cell_where.get(self._cur_occ)
            if w is not None:
                return w
            self._cur_occ = None  # 없어진 자리: 콤보의 자리로
        i = self.combo.currentIndex()
        return self.where[i] if 0 <= i < len(self.where) else None

    STATE_TEXT = {"found": "찾음", "manual": "직접 잡음", "lost": "위치 못 찾음", "not_icon": "아이콘 아님",
                  "named": "확정됨"}
    ACTIONABLE = ("found", "manual", "lost", "not_icon", "absorbed", "named")  # 목록에서 합칠 수 있는 자리
    DELETABLE = ("found", "manual", "lost", "named")
    KEEP_TEXT, KEEP_UNDO_TEXT = "이 칸 원본 그대로 (한글화 안 함)", "원본 그대로 두는 중 · 되돌리기"
    KEEP_PARTIAL_UNDO_TEXT = "아이콘 문단만 원본 그대로 · 되돌리기"  # 문단마다 다른 글꼴 영역: 다른 문단은 한글화

    @staticmethod
    def _key(it: dict) -> str:
        """목록 줄·그림 표시의 열쇠: 자리 id, 확정 아이콘은 ‘#ti<그 칸 토큰 순번>’."""
        return it.get("occ") or (f"#ti{it['ti']}" if it.get("ti") is not None else "")

    def _item_label(self, it: dict) -> str:
        return f"[[{it['name']}]]" if it.get("name") else self.row_label(it.get("group", ""))

    def _fill_items(self, p: dict, cur: str) -> None:
        """이 칸의 아이콘 목록을 다시 채운다 (고른 줄은 그대로 고른 채, 지금 자리는 늘 고름)."""
        keep = {o for o, _st in self._selected()}
        label_of = {it["occ"]: self._item_label(it) for it in p.get("items", []) if it.get("occ")}
        self._items_by_key = {self._key(it): it for it in p.get("items", []) if self._key(it)}
        self.items.blockSignals(True)
        self.items.clear()
        for k, it in enumerate(p.get("items", []), 1):
            st = it["state"]
            if st in ("absorbed", "set"):
                host = label_of.get(it.get("host"), "옆 아이콘")
                txt = f"{host}에 포함" if st == "absorbed" else f"{host} 세트 (자동)"
            else:
                txt = self.STATE_TEXT.get(st, st)
            mine = bool(it.get("occ")) and it["occ"] == cur
            row = QListWidgetItem(f"{'▶ ' if mine else ''}{k}. {self._item_label(it)} — {txt}")
            row.setData(Qt.ItemDataRole.UserRole, self._key(it))
            row.setData(Qt.ItemDataRole.UserRole + 1, st)
            if st in ("not_icon", "absorbed", "set", "named"):
                row.setForeground(QColor("#888"))
            elif st == "lost":
                row.setForeground(QColor("#B35C00"))
            if mine:
                f = row.font()
                f.setBold(True)
                row.setFont(f)
            if st == "set":  # 찾기가 합친 세트의 나머지 자리: 볼 수만 있다 (첫 자리를 고친다)
                row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.items.addItem(row)
            if self._key(it) and self._key(it) in keep:  # 지금 자리는 굵게만 (고르지 않으면 삭제에 들어가지 않는다)
                row.setSelected(True)
        self.items.blockSignals(False)
        self._list_tools()

    def _selected(self) -> list[tuple[str, str]]:
        """목록에서 고른 자리 [(occ, 상태)] (원문 순서)."""
        return [(it.data(Qt.ItemDataRole.UserRole), it.data(Qt.ItemDataRole.UserRole + 1))
                for it in sorted(self.items.selectedItems(), key=self.items.row) if it.data(Qt.ItemDataRole.UserRole)]

    def _item_clicked(self, item: QListWidgetItem) -> None:
        key = item.data(Qt.ItemDataRole.UserRole)
        mods = QApplication.keyboardModifiers()
        if not key or not item.isSelected() or \
                mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            return  # Ctrl·Shift로 여러 개를 고르는 중에는 지금 자리를 바꾸지 않는다
        if key.startswith("#ti"):
            self._busy_call(self.focus_named, key)
            return
        if key == (self._cur() or {}).get("occ") and self._named is None:
            return
        self._busy_call(self.select_item, key)

    def focus_named(self, key: str) -> None:
        """확정 아이콘 줄을 눌렀음: 그 아이콘(사전 그림)을 보여 준다. 원문은 바꾸지 않는다 — 끌어 감싸 저장하거나
        합치기·삭제에 넣을 때 그 아이콘을 [[?]] 자리로 연다."""
        it = self._items_by_key.get(key)
        if it is None:
            return
        self._named = it
        if it.get("png"):
            self.crop.setPixmap(QPixmap(it["png"]).scaledToHeight(64, Qt.TransformationMode.SmoothTransformation))
        else:
            self.crop.setText("사전 그림 없음")
        self.view.box, self.view.guess = (it.get("box") or None), None
        self.view.update()
        self.status.setText(f"확정된 아이콘 [[{it['name']}]]: 자리가 틀리면 끌어 감싸 ‘이 자리로 저장’ (다시 정할 자리로 엽니다). "
                            "합치기·삭제에도 넣을 수 있습니다.")
        self._fill_items(self._place, (self._cur() or {}).get("occ", ""))
        self._list_tools()

    def _open_keys(self, keys: list[str]) -> dict[str, str]:
        """목록 열쇠 -> 자리 id. 확정 아이콘(#ti…)은 [[?]] 자리로 연다 (토큰 수는 그대로라 다른 #ti는 그대로 맞다)."""
        anchor = next((it["occ"] for it in self._place.get("items", []) if it.get("occ")), None) \
            or (self._cur() or {}).get("occ")
        out = {}
        for k in keys:
            if k.startswith("#ti"):
                out[k] = service.reopen_icon(self.project_dir, anchor, int(k[3:]))["occ"]
            else:
                out[k] = k
        return out

    def select_item(self, occ: str) -> None:
        """목록의 그 자리를 지금 자리로 (그림에서 고친다). 위 콤보에 있는 자리면 콤보도 그 자리로."""
        idx = next((k for k, w in enumerate(self.where) if w["occ"] == occ), None)
        if idx is not None:
            self.combo.blockSignals(True)
            self.combo.setCurrentIndex(idx)
            self.combo.blockSignals(False)
            self._cur_occ = None
        else:
            self._cur_occ = occ
        self._load()

    def _selection_changed(self) -> None:
        self._list_tools()
        self._repaint_selection()

    def _repaint_selection(self) -> None:
        sel = {o for o, _st in self._selected()}
        self.view.marks = [(*m[:4], bool(m[5]) and m[5] in sel, m[5]) if len(m) > 5 else m for m in self.view.marks]
        # (확정 아이콘 표시의 열쇠도 #ti…라 같은 방법으로 강조된다)
        self.view.update()

    def _list_tools(self) -> None:
        sel = self._selected()
        self.del_btn.setEnabled(any(st in self.DELETABLE for _o, st in sel))
        host, others = self._merge_plan()
        self.merge_btn.setEnabled(bool(host and others))
        if host and others:
            k = next((i + 1 for i in range(self.items.count())
                      if self.items.item(i).data(Qt.ItemDataRole.UserRole) == host), 0)
            self.merge_btn.setText(f"합치기 ({len(others) + 1}개 → {k}번)")
        else:
            self.merge_btn.setText("합치기")
        n_sel = sum(1 for _o, st in sel if st in self.ACTIONABLE)
        self.merge_btn.setToolTip(
            "고른 자리들을 하나의 아이콘으로 합칩니다 (나머지는 원문에서 빠짐).\n"
            "합쳐질 자리: 고른 것 중 지금 자리(파랑), 아니면 상자가 있는 첫 자리."
            + ("\n고른 자리에 상자가 있는 것(찾음·직접 잡음)이 없습니다: 하나를 먼저 끌어 감싸 저장하세요."
               if n_sel >= 2 and not host else ""))
        self.restore_btn.setEnabled(any(st in ("not_icon", "absorbed") for _o, st in sel))
        ui = getattr(QApplication.instance(), "_ui_codes", None)
        if ui is not None:
            ui.relabel(self.merge_btn)

    def _after_list_action(self, cur: str | None) -> None:
        self.on_saved({})  # 이름 정하기 표를 새로 채운다 (줄 번호가 바뀔 수 있음)
        self.outcome = ("saved", self._refresh(cur))

    @busy
    def _delete_selected(self) -> None:
        keys = [o for o, st in self._selected() if st in self.DELETABLE]
        if not keys:
            return
        occs = []
        try:
            occs = list(self._open_keys(keys).values())
            for o in occs:
                service.set_icon_not_icon(self.project_dir, o)
        except Exception as e:  # noqa: BLE001
            show_error(self, "‘아이콘 아님’으로 정하지 못했습니다", e)
        self._named = None
        self._after_list_action((self._cur() or {}).get("occ") or (occs[0] if occs else None))

    def _merge_plan(self) -> tuple[str | None, list[str]]:
        """목록에서 고른 자리들을 합칠 계획 (합쳐질 자리, 그 자리에 포함할 자리들).
        합쳐질 자리: 고른 것 중 지금 자리(상자가 있을 때), 아니면 상자가 있는 첫 자리. 2개 이상 골라야 한다."""
        sel = [(o, st) for o, st in self._selected() if st in self.ACTIONABLE]
        if len(sel) < 2:
            return None, []
        cur = (self._cur() or {}).get("occ")
        boxed = [o for o, st in sel if st in ("found", "manual")]
        # 확정 아이콘은 합칠 곳이 되지 않는다 (사전 그림이 그대로 쓰여 합친 조각이 결과에서 빠진다): 넣는 쪽으로만
        host = cur if cur in boxed else (boxed[0] if boxed else None)
        return host, [o for o, _st in sel if o != host] if host else []

    @busy
    def _merge_selected(self) -> None:
        host, others = self._merge_plan()
        if not host or not others:
            return
        try:
            ids = self._open_keys([host] + others)  # 확정 아이콘은 먼저 [[?]] 자리로 연다
            host = ids[host]
            service.absorb_icon_run(self.project_dir, host, [ids[o] for o in others])
        except Exception as e:  # noqa: BLE001
            show_error(self, "아이콘을 합치지 못했습니다", e)
        self._named = None
        self._after_list_action(host if not host.startswith("#ti") else (self._cur() or {}).get("occ"))

    @busy
    def _restore_selected(self) -> None:
        occs = [o for o, st in self._selected() if st in ("not_icon", "absorbed")]
        if not occs:
            return
        try:
            for o in occs:
                service.restore_icon(self.project_dir, o)
        except Exception as e:  # noqa: BLE001
            show_error(self, "되돌리지 못했습니다", e)
        self._after_list_action((self._cur() or {}).get("occ") or occs[0])

    def _load(self):
        w = self._cur()
        if w is None:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            p = service.icon_place(self.project_dir, w["occ"])
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            show_error(self, "카드를 보여 주지 못했습니다", e)
            return
        QApplication.restoreOverrideCursor()
        pm = QPixmap()
        pm.loadFromData(p["png"], "PNG")
        settled = {"not_icon": " · 아이콘 아님", "absorbed": " · 옆 아이콘에 합침"}
        self._place = p
        self._named = None
        self._cell_info(p)
        self._fill_items(p, w["occ"])
        try:
            mode = service.cell_keep_mode(self.project_dir, w["occ"])
        except Exception:  # noqa: BLE001 - 표시만
            mode = ""
        kept = bool(mode)
        self.keep_btn.setText(self.KEEP_PARTIAL_UNDO_TEXT if mode == "partial" else
                              self.KEEP_UNDO_TEXT if kept else self.KEEP_TEXT)
        self._kept = kept
        sel = {o for o, _st in self._selected()}
        # (상자, 이름, 예상 위치, 정한 자리, 목록에서 고름, 자리 id)
        marks = [(m["box"], (f"[[{m['name']}]]" if m.get("name") else self.row_label(m.get("group", "")))
                  + (settled.get(m.get("settled"), "") or (" · 위치 못 찾음" if m.get("guess") else "")),
                  bool(m.get("guess")), bool(m.get("settled")), self._key(m) in sel, self._key(m))
                 for m in p["marks"]]
        self.view.set_data(pm, p["box"], p["guess"], marks)
        self._update_tools()
        if w.get("png"):
            self.crop.setPixmap(QPixmap(w["png"]).scaledToHeight(64, Qt.TransformationMode.SmoothTransformation))
        else:
            self.crop.setText("아직 없음")
        host = (p.get("run_host") or [None])[0]
        self._run_host = host
        host_mark = next((m for m in p["marks"] if m.get("occ") == host), None) if host else None
        host_label = (f"[[{host_mark['name']}]]" if host_mark.get("name") else self.row_label(host_mark.get("group", ""))) \
            if host_mark else "옆 아이콘"
        self.absorb_btn.setText(f"{host_label}에 포함됨")
        self.absorb_btn.setVisible(bool(host))
        if p["box"]:
            self.status.setText("자리가 맞지 않으면 아이콘을 끌어 다시 잡으세요.")
        elif host:
            self.status.setText(f"바로 옆 아이콘({host_label})의 상자가 이 자리까지 감싸고 있으면 ‘{host_label}에 포함됨’을 "
                                "누르세요. 따로 있는 아이콘이면 끌어 감싸세요.")
        elif p["guess"]:
            self.status.setText("자동으로 찾지 못했습니다. 주황 점선(예상 위치) 근처의 아이콘을 끌어 감싸세요.")
        else:
            self.status.setText("자동으로 찾지 못했습니다. 아이콘을 끌어 감싸세요.")
        self.save_btn.setEnabled(False)

    def _cell_info(self, p: dict) -> None:
        """목록의 다른 자리(다른 묶음)를 고를 때 쓸 그림 조각·직접 잡음 정보."""
        occs = {it["occ"] for it in p.get("items", []) if it.get("occ")} - {w["occ"] for w in self.where}
        if not occs:
            self._cell_where = {}
            return
        if frozenset(occs) == self._cell_key:  # 같은 칸: 다시 읽지 않는다 (_refresh가 비운다)
            return
        self._cell_key = frozenset(occs)
        rev = service.icon_review(self.project_dir) or {"groups": []}
        self._cell_where = {w["occ"]: dict(w) for g in rev["groups"] for w in g.get("where", []) if w["occ"] in occs}

    def _drawn(self):
        self.save_btn.setEnabled(self.view.new is not None)
        self._update_tools()

    def _update_tools(self):
        """겹친 아이콘 흡수·둘로 나누기 버튼: 지금 자리(또는 새로 감싼 자리)를 보고 켠다."""
        p = self._place
        occ = (self._cur() or {}).get("occ", "")
        box = self.view.new or p.get("box")
        self._overlaps = service.overlapping_icons(p.get("marks") or [], box, exclude=occ) if box else []
        n = len(self._overlaps)
        self.overlap_btn.setText(f"겹친 아이콘 {n}개 흡수" if n > 1 else "겹친 아이콘 흡수")
        self.overlap_btn.setVisible(bool(n))
        ui = getattr(QApplication.instance(), "_ui_codes", None)
        if ui is not None:
            ui.relabel(self.overlap_btn)  # Ctrl+D 디버깅 중이면 버튼 코드를 그대로
        self.split_btn.setEnabled(bool(p.get("box")) and self.view.new is not None)

    @busy
    def _save(self):
        box = self.view.new
        w = self._cur()
        if not box or w is None:
            return
        target = w["occ"]
        try:
            if self._named is not None:
                target = None  # 확정 아이콘: 묻기를 통과하면 연다
            prob = service.icon_box_problem(self.project_dir, target or w["occ"], box)
        except Exception:  # noqa: BLE001 - 검사는 없어도 된다
            prob = ""
        if prob and ask_choice(self, "아이콘 상자 확인", prob,
                               [("다시 잡기", "reject"), ("그래도 아이콘 하나로 저장", "accept")], default=0,
                               informative="큰 아이콘은 모든 글자의 왼쪽·오른쪽에 있거나 한 줄을 아이콘이 다 차지할 때만 있습니다.\n"
                                           "위아래로 아이콘이 두 개면 하나만 감싸 저장한 뒤, 다른 하나는 목록에서 골라 잡으세요.") != 1:
            return
        try:
            if target is None:
                target = self._open_keys([self._key(self._named)])[self._key(self._named)]
                self._cur_occ, self._named = target, None
                self._cell_where = {}
                self._cell_key = None
            w = {**w, "occ": target}
            r = service.set_icon_box(self.project_dir, target, box)
        except ValueError as e:
            show_problem(self, "자리를 저장하지 못했습니다", str(e), "아이콘을 조금 더 크게 감싸 주세요.")
            self._after_failed(target)
            return
        except Exception as e:  # noqa: BLE001
            show_error(self, "자리를 저장하지 못했습니다", e)
            self._after_failed(target)
            return
        if r.get("run_unfound"):  # 바로 옆에 붙은 [[?]]를 못 찾았음: 이 상자가 그것까지 감쌌을 수 있다 (문양 셋이 붙은 아이콘)
            n = len(r["run_unfound"])
            c = ask_choice(self, "붙어 있는 아이콘",
                           f"이 자리 바로 옆에 아이콘([[?]]) {n}개가 붙어 있습니다 (위치를 못 찾았거나 이 상자 안에 있음).\n"
                           f"방금 잡은 상자가 아이콘 {n + 1}개를 한 세트로 감싸고 있나요?",
                           [(f"아이콘 {n + 1}개를 한 세트로", "accept"), ("따로 있는 아이콘임", "reject")], default=0,
                           informative="합치면 붙어 있는 [[?]]가 이 아이콘 하나가 되고, 나머지 자리는 원문에서 뺍니다.\n"
                                       "따로 있는 아이콘이면 그 줄에서 ‘카드 보기 · 자리 잡기’로 하나씩 잡으세요.")
            merged = False
            if c == 0:
                try:
                    service.absorb_icon_run(self.project_dir, w["occ"], r["run_unfound"])
                    merged = True
                except Exception as e:  # noqa: BLE001
                    show_error(self, "아이콘을 합치지 못했습니다", e)
        else:
            merged = False
        self.outcome = ("saved", r["group"])
        named = self.on_saved(r)  # 이름 정하기 표를 새로 채운다 (줄 번호가 바뀔 수 있음). 예전 줄 이름을 이어받으면 True
        rev = service.icon_review(self.project_dir) or {"groups": []}
        gg = next((x for x in rev["groups"] if x["group"] == r["group"]), {})
        # 여러 아이콘을 한 세트로 합쳤으면 그림이 바뀌었으므로 (예전 한 개짜리 제안은 맞지 않음) 다시 제안 받는다.
        # 같은 아이콘을 다시 잡아 예전 줄의 이름을 이어받았으면 제안으로 덮지 않는다
        again = (merged and gg.get("count", 1) <= 1 and not r.get("dict")) or \
            (r.get("new_group") and not r.get("dict") and not gg.get("suggest") and not named)
        if merged:  # 세트로 합쳤으면 닫지 않는다: 합친 그림을 이 창에서 바로 보여 준다
            if again:
                self._suggest(r["group"], close=False)
            else:
                self.outcome = ("saved", self._refresh(w["occ"]) or r["group"])
        elif again:
            self._suggest(r["group"])  # 제안을 받은 뒤 닫는다 (위치 못 찾음이 남으면 닫지 않음)
        else:
            self._finish(w["occ"])  # 저장했으면 닫고 이름 정하기 표로 돌아간다 (위치 못 찾음이 남으면 닫지 않음)

    def _after_failed(self, occ: str | None) -> None:
        """실패했어도 확정 아이콘을 이미 [[?]] 자리로 열었으면 목록·표를 새로 (예전 #ti 줄을 다시 누르지 않게)."""
        if occ and self._cur_occ == occ:  # 열면 그 자리를 지금 자리로 해 두었다
            self._after_list_action(occ)

    def _finish(self, occ: str | None) -> None:
        """작업을 마친 뒤: 이 칸에 ‘위치 못 찾음’이 하나라도 남았으면 창을 닫지 않고 바뀐 모습을 보여 준다."""
        try:
            items = service.icon_place(self.project_dir, occ)["items"] if occ else []
        except Exception:  # noqa: BLE001
            items = []
        if any(it["state"] == "lost" for it in items):
            g = self._refresh(occ)
            self.outcome = (self.outcome[0] or "saved", g or self.outcome[1])
            self.status.setText("이 칸에 아직 위치를 못 찾은 아이콘이 있어 창을 닫지 않았습니다. 목록에서 골라 잡으세요.")
            return
        self.accept()

    @busy
    def _toggle_keep(self) -> None:
        """이 칸 원본 그대로 (한글화 안 함) 켜기·끄기. 켤 때는 한 번 묻는다 (이 칸의 아이콘 자리가 모두 ‘아이콘 아님’이 됨)."""
        w = self._cur()
        if w is None:
            return
        keep = not getattr(self, "_kept", False)
        if keep:
            n = sum(1 for it in self._place.get("items", []) if it.get("state") in ("found", "manual", "lost"))
            if ask_choice(self, "이 칸 원본 그대로", "이 칸을 한글화하지 않고 원본 그대로 둘까요?",
                          [("원본 그대로 두기", "accept"), ("취소", "reject")], default=1,
                          informative=(f"이 칸의 아이콘 자리 {n}개가 ‘아이콘 아님’이 됩니다. " if n else "")
                          + "영역이 ‘문단마다 다른 글꼴’이면 아이콘이 있는 문단만 원본 그대로 두고 다른 문단은 한글화합니다. "
                          + "다시 누르면 원래대로 되돌립니다.") != 0:
                return
        try:
            r = service.keep_cell_original(self.project_dir, w["occ"], keep)
        except Exception as e:  # noqa: BLE001
            show_error(self, "원본 그대로 두기를 바꾸지 못했습니다", e)
            return
        if not keep and not r.get("restored") and r.get("left"):  # 예전 버전에서 켠 칸: 어느 자리를 원본 그대로로 지웠는지 모름
            if ask_choice(self, "원본 그대로 끄기", f"이 칸에 ‘아이콘 아님’ 자리가 {len(r['left'])}개 남아 있습니다. 모두 아이콘 자리로 되돌릴까요?",
                          [("모두 되돌리기", "accept"), ("그대로 두기", "reject")], default=0,
                          informative="원본 그대로를 켜서 아이콘 아님이 된 자리라면 되돌리세요. 일부러 지운 자리가 섞여 있으면 "
                                      "‘그대로 두기’를 고르고 목록에서 골라 ‘되돌리기’를 누르세요.") == 0:
                restored = []
                for o in r["left"]:
                    try:
                        service.restore_icon(self.project_dir, o)
                        restored.append(o)
                    except Exception as e:  # noqa: BLE001
                        show_error(self, "되돌리지 못했습니다", e)
                        break
                r = {**r, "restored": restored, "left": [o for o in r["left"] if o not in restored]}
        self._after_list_action(w["occ"])
        if r["keep"] and r.get("mode") == "partial":
            self.status.setText("이 칸에서 아이콘이 있는 문단만 원본 그대로 둡니다 (그 문단은 글자 지우기·번역·한글 넣기 안 함). "
                                "다른 문단은 한글화합니다. 이 칸의 아이콘 자리는 ‘아이콘 아님’이 되었습니다.")
        elif r["keep"]:
            self.status.setText("이 칸은 원본 그대로 둡니다 (글자 지우기·한글 넣기 안 함). 이 칸의 아이콘 자리는 ‘아이콘 아님’이 되었습니다.")
        else:
            self.status.setText("이 칸을 다시 한글화합니다."
                                + (f" ‘아이콘 아님’ 자리 {len(r['restored'])}개를 되돌렸습니다." if r.get("restored") else "")
                                + (" 남은 ‘아이콘 아님’ 자리는 목록에서 골라 ‘되돌리기’로 풀 수 있습니다." if r.get("left") else ""))

    @busy
    def _absorb(self):
        """지금 자리를 바로 옆 아이콘(상자가 이 자리까지 감쌈)에 포함: 이 자리는 원문에서 뺀다."""
        w = self._cur()
        if w is None or not self._run_host:
            return
        try:
            service.absorb_icon_run(self.project_dir, self._run_host, [w["occ"]])
        except Exception as e:  # noqa: BLE001
            show_error(self, "아이콘을 합치지 못했습니다", e)
            return
        self.outcome = ("not_icon", None)  # 이 줄은 끝: 다음 줄로
        self.on_saved({})
        # 합친 세트(앞 아이콘)의 이름을 새로 제안 받는다: 혼자 있는 줄이고 사전 아이콘이 아닐 때
        rev = service.icon_review(self.project_dir) or {"groups": []}
        host = next((x for x in rev["groups"] if any(w.get("occ") == self._run_host for w in x["where"])), None)
        if host is not None and host.get("count", 1) <= 1 and not host.get("dict"):
            self._suggest(host["group"])
        else:
            self._finish(self._run_host)

    def _suggest(self, group: str, close: bool = True):
        """직접 잡아 새로 생긴 줄의 이름·뜻·한국어 이름을 Claude에게 제안 받는다 (못 받아도 직접 적으면 된다).
        close=False: 제안을 받은 뒤 창을 닫지 않고 바뀐 그림을 다시 보여 준다 (세트로 합침·나눔·흡수)."""
        occ = (self._cur() or {}).get("occ")
        after = (lambda: self._busy_call(self._finish, occ)) if close else (lambda: self._busy_call(self._refresh, occ))
        eng = service.get_settings().engine
        if not service.engine_status().get(eng):  # 연결할 수 없으면 제안 없이 (이름은 표에서 직접 적는다)
            after()
            return

        def done(res):
            self.on_saved({"suggested": group, **(res or {})})  # 표를 다시 채워 제안을 넣는다 (적어 둔 값은 그대로)
            after()

        job = worker.Job(service.suggest_icon_name, self.project_dir, group, engine=eng, with_progress=False)
        worker.run_with_dialog(self, "아이콘 이름 제안 받는 중", job, done, lambda _m: after(),
                               cancellable=False)

    @busy
    def _busy_call(self, fn, *args):
        return fn(*args)

    def _refresh(self, select_occ: str | None = None, extra: list[str] | None = None) -> str | None:
        """자리 목록·그림을 다시 읽어 이 창에서 바로 보여 준다 (닫지 않음). extra: 새로 생긴 자리 id.
        돌려줌: select_occ가 지금 들어 있는 묶음 (닫은 뒤 표가 고를 줄)."""
        rev = service.icon_review(self.project_dir) or {"groups": []}
        self._cell_where, self._cell_key = {}, None  # 묶음이 바뀌었을 수 있다
        group = next((g["group"] for g in rev["groups"] if any(w["occ"] == select_occ for w in g.get("where", []))), None)
        by_occ = {w["occ"]: w for g in rev["groups"] for w in g.get("where", [])}
        ids = [w["occ"] for w in self.where] + [o for o in (extra or []) if o not in {w["occ"] for w in self.where}]
        self.where = [dict(by_occ[o]) for o in ids if o in by_occ]
        self.combo.blockSignals(True)
        self.combo.clear()
        for w in self.where:
            self.combo.addItem(self._item_text(w), w["occ"])
        idx = next((k for k, w in enumerate(self.where) if w["occ"] == select_occ), None)
        # 목록에서 고른 다른 묶음의 자리면 그 자리를 그대로 지금 자리로 (콤보는 그 묶음의 자리만 있다)
        self._cur_occ = select_occ if idx is None and select_occ and select_occ in by_occ else None
        if self._cur_occ:  # 다른 묶음의 자리 정보는 방금 읽은 것으로 (비워 두면 _cur()가 콤보 자리로 돌아간다)
            self._cell_where = {self._cur_occ: dict(by_occ[self._cur_occ])}
        self.combo.setCurrentIndex(idx or 0)
        self.combo.blockSignals(False)
        if not self.where:  # 보여 줄 자리가 없음: 예전 그림·버튼을 남기지 않는다
            self._place = {}
            self.view.set_data(QPixmap(), None, None, [])
            self._update_tools()
            return group
        self._load()
        return group

    @busy
    def _absorb_overlaps(self):
        """겹친 [[?]] 자리들을 지금 아이콘에 포함한다 (같은 그림을 두 번 읽음: 그 자리들은 원문에서 빠진다)."""
        w = self._cur()
        if w is None or not self._overlaps:
            return
        if self.view.new is not None:  # 새로 감쌌으면 그 자리로 먼저 저장
            try:
                service.set_icon_box(self.project_dir, w["occ"], self.view.new)
            except ValueError as e:
                show_problem(self, "자리를 저장하지 못했습니다", str(e), "아이콘을 조금 더 크게 감싸 주세요.")
                return
            except Exception as e:  # noqa: BLE001
                show_error(self, "자리를 저장하지 못했습니다", e)
                return
        try:
            service.absorb_icon_run(self.project_dir, w["occ"], list(self._overlaps))
        except Exception as e:  # noqa: BLE001
            show_error(self, "아이콘을 흡수하지 못했습니다", e)
            return
        self.on_saved({})
        self.outcome = ("saved", self._refresh(w["occ"]))

    @busy
    def _split(self):
        """지금 자리 상자 안의 아이콘 둘을 나눈다: 새로 감싼 것 하나 + 나머지."""
        w = self._cur()
        if w is None or self.view.new is None:
            return
        try:
            r = service.split_icon_box(self.project_dir, w["occ"], self.view.new)
        except ValueError as e:
            show_problem(self, "둘로 나누지 못했습니다", str(e), "지금 자리(파랑) 안에서 아이콘 하나만 감싸 주세요.")
            return
        except Exception as e:  # noqa: BLE001
            show_error(self, "둘로 나누지 못했습니다", e)
            return
        self.outcome = ("saved", r["groups"][0])
        self.on_saved({})
        self._refresh(w["occ"], extra=[r["second"]])
