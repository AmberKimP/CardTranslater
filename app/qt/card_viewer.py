"""② 원문 확인: 카드 보기 (카드 한 장을 보고 영역 하나씩 고친다).

왼쪽에 카드 전체(영역 상자는 상태 색 테두리)와 영역 목록, 오른쪽에 그 영역의 그림(아이콘 상자·끌어 감싸기), 아래에
아이콘 목록(이름 칸)과 원문(읽은 결과·편집 칸)이 있다. 아이콘 이름·아이콘 아님은 ‘확정 전’으로 적어 두었다가
‘이 영역 확정’에서 원문에 넣는다 (service.confirm_regions). 자리 잡기·합치기 같은 아이콘 조작은 바로 저장한다.

목록 창(source_check)이 modeless로 띄우거나 exec()로 쓴다:
    CardViewer(project_dir, rows_fn, start, parent, engine, reading_fn)
    rows_fn() -> service.source_check_rows 모양의 카드 목록 (바꿀 때마다 다시 부른다)
    start = (파일 폴더, 쪽) 또는 (파일 폴더, 쪽, 영역)
    reading_fn() -> 뒤에서 다시 읽는 중인 영역 키 집합
    changed: 데이터를 바꾼 뒤 (목록을 새로 그리게), confirmed(dict): confirm_regions 결과 (목록이 result["reread"]를 읽게)
화면은 app.service만 부른다.
"""

from __future__ import annotations

import html as _html
import os
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QCompleter, QDialog, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit,
                               QPushButton, QScrollArea, QSplitter, QVBoxLayout, QWidget)

from app import service
from app.qt import worker
from app.qt.dialogs import (PRIMARY_QSS, ask_choice, file_label, fit_to_screen, hint_bar, page_title, set_hint,
                            show_error, show_problem)
from app.qt.icon_place import IconPlaceView, busy
from app.qt import source_text
from app.qt.step_common import (KEEP_LABEL, KEEP_UNDO_LABEL, REREAD_MODELS, UNKNOWN_ICON, _fail_as, as_text,
                                diff_html, fill_icon_names, precise_index, reader_name, source_font,
                                state_label)

# 영역 상태 색 (상자 테두리·점·이름표). 설계안 2.2: 빨강 고칠 것, 주황 볼 것, 파랑 읽는 중, 옅은 회색 확인 안 함, 초록 확인됨, 회색 원본 그대로
STATUS_COLOR = {"fix": "#D62828", "check": "#E67E22", "reading": "#1F6FEB", "todo": "#AEAEB2", "ok": "#2E8B57",
                "keep": "#8A8A8A"}
STAGED_BG = "#F6F0FD"  # 확정 전 줄 (연보라)
PINK_BG = "#FDECEC"  # 이름 없음·위치 못 찾음 줄
ENTRY_TEXT = {"ok": "확정됨", "named": "이름 정함", "unnamed": "이름 없음", "lost": "위치 못 찾음", "not_icon": "아이콘 아님",
              "absorbed": "옆 아이콘에 포함"}
SENTENCE_TEXT, SENTENCE_UNDO_TEXT = "이 문장만 원본 그대로", "이 문장 원본 그대로 · 되돌리기"  # 카드 보기·아이콘 자리 잡기와 같은 말


def _nk(key) -> tuple:
    """영역·카드 열쇠를 비교하기 좋게 (경로 표기 차이를 없앤다)."""
    key = tuple(key)
    return (os.path.normcase(os.path.abspath(str(key[0]))),) + key[1:]


def _dot(color: str, size: int = 12) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(1, 1, size - 2, size - 2)
    p.end()
    return QIcon(pm)


def _badge(text: str, color: str, solid: bool = True) -> str:
    fg, bg = ("white", color) if solid else (color, "white")
    return (f"color: {fg}; background: {bg}; border: 1px solid {color}; border-radius: 9px; padding: 2px 10px;"
            " font-weight: 600;")


# ================================================================ 카드 전체 그림
class CardMap(QWidget):
    """카드 전체 그림 위에 영역 상자를 상태 색 테두리로 그린다. 고른 영역은 굵게 + 옅은 칠. 상자를 누르면 picked(영역 이름)."""

    picked = Signal(str)

    def __init__(self):
        super().__init__()
        self._pm = QPixmap()
        self._rects: dict[str, tuple] = {}
        self._colors: dict[str, str] = {}
        self._sel = ""
        self.setMinimumSize(220, 220)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("영역 상자를 누르면 그 영역으로 갑니다")

    def set_data(self, pm: QPixmap, rects: dict, colors: dict, selected: str) -> None:
        self._pm, self._rects, self._colors, self._sel = pm, dict(rects), dict(colors), selected
        self.update()

    def select(self, name: str, colors: dict | None = None) -> None:
        self._sel = name
        if colors is not None:
            self._colors = dict(colors)
        self.update()

    def _geom(self) -> tuple[float, float, float]:
        w, h = self._pm.width(), self._pm.height()
        if not w or not h:
            return 1.0, 0.0, 0.0
        s = min((self.width() - 8) / w, (self.height() - 8) / h)
        return s, (self.width() - w * s) / 2, (self.height() - h * s) / 2

    def _rect(self, r) -> QRectF:
        s, ox, oy = self._geom()
        return QRectF(ox + r[0] * s, oy + r[1] * s, (r[2] - r[0]) * s, (r[3] - r[1]) * s)

    def hit(self, pos) -> str:
        """누른 자리의 영역 이름 (겹치면 가장 작은 상자)."""
        best, area = "", 0.0
        for name, r in self._rects.items():
            qr = self._rect(r)
            if qr.contains(pos) and (not best or qr.width() * qr.height() < area):
                best, area = name, qr.width() * qr.height()
        return best

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#FAFAFA"))
        if self._pm.isNull():
            return
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        s, ox, oy = self._geom()
        p.drawPixmap(QRectF(ox, oy, self._pm.width() * s, self._pm.height() * s), self._pm, QRectF(self._pm.rect()))
        for name, r in self._rects.items():
            color = QColor(self._colors.get(name, STATUS_COLOR["todo"]))
            mine = name == self._sel
            qr = self._rect(r)
            if mine:
                fill = QColor(color)
                fill.setAlpha(45)
                p.fillRect(qr, fill)
            p.setPen(QPen(color, 4 if mine else 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(qr)
        p.end()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            name = self.hit(e.position())
            if name:
                self.picked.emit(name)


# ================================================================ 아이콘 목록의 한 줄
class IconRow(QFrame):
    """아이콘 한 줄: 그림 · 이름 칸 · 한국어 이름 · 상태. 이름·한국어 이름은 Enter·포커스를 옮길 때 edited(자리 id, 이름, 한국어 이름)."""

    edited = Signal(str, str, str)
    focused = Signal(str)

    def __init__(self, e: dict, names: list[str]):
        super().__init__()
        self.occ = e["occ"]
        self.entry = e
        self.setObjectName("iconrow")
        bg = PINK_BG if e["state"] in ("unnamed", "lost") else STAGED_BG if e["pending"] else "white"
        self._bg = bg
        self.set_current(False)
        self.img = QLabel()
        self.img.setFixedSize(36, 36)
        self.img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img.setStyleSheet("background: white; border: 1px solid #E5E5EA; border-radius: 4px;")
        pm = QPixmap(e["image"]) if e.get("image") else QPixmap()
        if pm.isNull():
            self.img.setText("?")
        else:
            self.img.setPixmap(pm.scaled(34, 34, Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation))
        editable = e["state"] not in ("ok", "not_icon", "absorbed")
        self.name = QLineEdit(e["name"])
        self.name.setPlaceholderText("이름 (영문 소문자·숫자·_)")
        self.name.setEnabled(editable)
        self.name.setMinimumWidth(110)
        self.name.setMinimumHeight(28)
        comp = QCompleter(names, self.name)
        comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.name.setCompleter(comp)
        self.ko = QLineEdit(e["ko"])
        self.ko.setPlaceholderText("한국어 이름")
        self.ko.setEnabled(editable)
        self.ko.setMinimumWidth(80)
        self.ko.setMinimumHeight(28)
        tag = ENTRY_TEXT.get(e["state"], e["state"]) + (" · 확정 전" if e["pending"] else "")
        self.tag = QLabel(tag)
        self.tag.setStyleSheet("color: #6E6E73;")
        lay = QGridLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setHorizontalSpacing(8)
        lay.setVerticalSpacing(4)
        lay.addWidget(self.img, 0, 0, 2, 1)
        lay.addWidget(self.name, 0, 1)
        lay.addWidget(self.ko, 0, 2)
        lay.addWidget(self.tag, 1, 1, 1, 2)
        lay.setColumnStretch(1, 3)
        lay.setColumnStretch(2, 2)
        for w in (self.name, self.ko):
            w.editingFinished.connect(self._done)
            w.installEventFilter(self)

    def set_current(self, on: bool) -> None:
        self.setStyleSheet(f"QFrame#iconrow {{ background: {self._bg}; border-radius: 6px;"
                           f" border: {'2px solid #7B2CBF' if on else '1px solid #E5E5EA'}; }}")

    def eventFilter(self, obj, ev):  # noqa: N802 - Qt 이름
        if ev.type() == QEvent.Type.FocusIn:
            self.focused.emit(self.occ)
        return super().eventFilter(obj, ev)

    def _done(self) -> None:
        self.edited.emit(self.occ, self.name.text().strip(), self.ko.text().strip())


# ================================================================ 읽은 결과 표
class ReadingsTable(QWidget):
    """읽은 결과 표: 왼쪽 ‘보통 읽기’·‘정밀 읽기’, 오른쪽 그 결과 (정밀 읽기와 다른 글자는 노랑·빨간 굵은 글자)."""

    def __init__(self):
        super().__init__()
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(0)
        self.grid.setColumnStretch(1, 1)
        self._font = source_font(self.font(), 1)
        self._rows: list[tuple[QLabel, QLabel]] = []

    def fill(self, texts: list[tuple[str, str]], pi: int | None) -> None:
        for h, v in self._rows:
            for w in (h, v):
                self.grid.removeWidget(w)
                w.hide()
                w.deleteLater()
        self._rows = []
        self.setVisible(bool(texts))
        if not texts:
            return
        htmls = diff_html([t for _n, t in texts], pi if pi is not None else len(texts) - 1)
        last = len(texts) - 1
        for i, ((name, _t), body) in enumerate(zip(texts, htmls)):
            edge = "border: 1px solid #D1D1D6;" + ("" if i == last else " border-bottom: none;")
            h = QLabel(name)
            h.setStyleSheet(f"background: #F2F2F7; color: #3A3A3C; font-weight: bold; padding: 5px 10px; {edge}"
                            " border-right: none;")
            h.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            v = QLabel(body or '<span style="color:#888">(빈 칸)</span>')
            v.setTextFormat(Qt.TextFormat.RichText)
            v.setWordWrap(True)
            v.setFont(self._font)
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            v.setStyleSheet(f"background: white; padding: 5px 10px; {edge}")
            self.grid.addWidget(h, i, 0)
            self.grid.addWidget(v, i, 1)
            self._rows.append((h, v))


def _panel(title: str, body: QWidget) -> QFrame:
    fr = QFrame()
    fr.setObjectName("panel")
    fr.setStyleSheet("QFrame#panel { background: white; border: 1px solid #E5E5EA; border-radius: 10px; }")
    lay = QVBoxLayout(fr)
    lay.setContentsMargins(10, 8, 10, 8)
    lay.setSpacing(6)
    t = QLabel(title)
    t.setStyleSheet("font-weight: 600; color: #3A3A3C; border: none;")
    lay.addWidget(t)
    lay.addWidget(body, 1)
    return fr


# ================================================================ 카드 보기 창
class CardViewer(QDialog):
    changed = Signal()
    confirmed = Signal(dict)

    def __init__(self, project_dir: Path, rows_fn: Callable[[], list[dict]], start: tuple, parent=None,
                 engine: str | None = None, reading_fn: Callable[[], set] | None = None):
        super().__init__(parent)
        self.project_dir = Path(project_dir)
        self.rows_fn = rows_fn
        self.reading_fn = reading_fn
        self.engine = engine
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        fit_to_screen(self, 1400, 880, maximize_below=0)
        self._busy_depth = 0
        self._rereading = False
        self._loading = False
        self._card_cache: dict[tuple, dict] = {}
        self._known: list[str] = []
        self.cards: list[dict] = []
        self.flat: list[tuple[dict, dict]] = []
        self.key: tuple | None = None
        self._entries: list[dict] = []
        self._cur_occ: str | None = None
        self._place: dict = {}
        self._orig_text = ""
        self._overlaps: list[str] = []
        self._run_host: str | None = None
        self._region_mode = "cell"
        self._pending_reload = False
        self._build()
        self._fetch()
        self._show_region(self._initial_key(start))

    # ------------------------------------------------------------ 화면 만들기
    def _build(self) -> None:
        self.title = page_title("카드 보기", 14)
        # 왼쪽: 카드 전체 + 영역 목록
        self.card_map = CardMap()
        self.card_map.picked.connect(self._pick_name)
        self.region_list = QListWidget()
        self.region_list.setMinimumHeight(110)
        self.region_list.setToolTip("영역 목록 (↑/↓로 고르고, Alt+↑/↓로도 옮깁니다)")
        self.region_list.currentRowChanged.connect(self._region_row_changed)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)
        ll.addWidget(QLabel("<b>카드 전체</b>"))
        ll.addWidget(self.card_map, 3)
        ll.addWidget(QLabel("<b>영역 목록</b>"))
        ll.addWidget(self.region_list, 2)
        left.setMinimumWidth(280)
        left.setMaximumWidth(380)
        # 오른쪽 머리
        self.region_title = QLabel()
        self.region_title.setStyleSheet("font-size: 15px; font-weight: 600; color: #1D1D1F;")
        self.status_badge = QLabel()
        self.pending_badge = QLabel("확정 전")
        self.pending_badge.setStyleSheet(_badge("확정 전", "#7B2CBF", solid=False))
        self.flash_label = QLabel()
        self.flash_label.setTextFormat(Qt.TextFormat.RichText)
        self.flash_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.flash_timer = QTimer(self)
        self.flash_timer.setSingleShot(True)
        self.flash_timer.timeout.connect(lambda: self.flash_label.setText(""))
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(self.region_title)
        head.addWidget(self.status_badge)
        head.addWidget(self.pending_badge)
        head.addStretch(1)
        head.addWidget(self.flash_label, 1)
        # 영역 그림
        self.view = IconPlaceView()
        self.view.drawn.connect(self._drawn)
        self.view_hint = QLabel('<span style="color:#2E8B57">■</span> 찾은 아이콘 &nbsp; '
                                '<span style="color:#1F6FEB">■</span> 고른 아이콘 &nbsp; '
                                '<span style="color:#E67E22">■</span> 예상 위치 &nbsp; '
                                '<span style="color:#D62828">■</span> 새로 잡은 자리 &nbsp; '
                                '<span style="color:#8A8A8A">■</span> 아이콘 아님·포함 &nbsp;&nbsp; '
                                '아이콘을 끌어 감싸면 새 자리가 됩니다')
        self.view_hint.setStyleSheet("color: #6E6E73;")
        view_box = QWidget()
        vl = QVBoxLayout(view_box)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(4)
        vl.addWidget(self.view, 1)
        vl.addWidget(self.view_hint)
        # 아이콘 칸
        self.icon_list = QListWidget()
        self.icon_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.icon_list.setSpacing(2)
        self.icon_list.setMinimumHeight(110)
        self.icon_list.setStyleSheet("QListWidget { border: none; background: white; }"
                                     "QListWidget::item { background: transparent; }"
                                     "QListWidget::item:selected { background: transparent; }")
        self.icon_list.itemSelectionChanged.connect(self._icon_selection_changed)
        self.same_shape = QCheckBox("같은 모양에 모두 적용")
        self.same_shape.setChecked(True)
        self.same_shape.setToolTip("이름을 정하면 프로젝트 안의 모양이 같은 이름 없는 아이콘에도 같은 이름을 적습니다 (확정 전)")
        self.save_btn = QPushButton("이 자리로 저장")
        self.add_btn = QPushButton("아이콘 추가")
        self.del_btn = QPushButton("삭제 (아이콘 아님)")
        self.merge_btn = QPushButton("합치기")
        self.restore_btn = QPushButton("되돌리기")
        self.sentence_btn = QPushButton(SENTENCE_TEXT)
        self.absorb_btn = QPushButton("옆 아이콘에 포함됨")
        self.overlap_btn = QPushButton("겹친 아이콘 흡수")
        self.save_btn.setToolTip("고른 아이콘을 그림에서 끌어 감싼 자리로 다시 자릅니다 (바로 저장)")
        self.add_btn.setToolTip("원문에 빠진 아이콘: 그림에서 끌어 감싼 뒤 누르세요. 원문에 [[?]]가 하나 늘어납니다")
        self.del_btn.setToolTip("고른 아이콘을 ‘아이콘 아님’으로 합니다 (확정하면 원문에서 빠짐)")
        self.merge_btn.setToolTip("2개 이상 고르면 하나의 아이콘으로 합칩니다 (나머지는 원문에서 빠짐)")
        self.restore_btn.setToolTip("‘아이콘 아님’·포함으로 정한 자리를 다시 아이콘으로 되돌립니다")
        self.save_btn.clicked.connect(self._save_box)
        self.add_btn.clicked.connect(self._add_box)
        self.del_btn.clicked.connect(self._delete)
        self.merge_btn.clicked.connect(self._merge)
        self.restore_btn.clicked.connect(self._restore)
        self.sentence_btn.clicked.connect(self._toggle_sentence)
        self.absorb_btn.clicked.connect(self._absorb)
        self.overlap_btn.clicked.connect(self._absorb_overlaps)
        for b in (self.absorb_btn, self.overlap_btn):
            b.hide()
        btn_grid = QGridLayout()
        btn_grid.setSpacing(4)
        for k, b in enumerate((self.save_btn, self.add_btn, self.del_btn, self.merge_btn, self.restore_btn,
                               self.sentence_btn)):
            btn_grid.addWidget(b, k // 2, k % 2)
        icons_body = QWidget()
        il = QVBoxLayout(icons_body)
        il.setContentsMargins(0, 0, 0, 0)
        il.setSpacing(4)
        il.addWidget(self.icon_list, 1)
        il.addWidget(self.same_shape)
        il.addLayout(btn_grid)
        il.addWidget(self.absorb_btn)
        il.addWidget(self.overlap_btn)
        self.icon_panel = _panel("이 영역의 아이콘", icons_body)
        # 원문 칸
        self.info = hint_bar("", "info")
        self.info.setTextFormat(Qt.TextFormat.RichText)
        self.readings = ReadingsTable()
        self.pick_row = QHBoxLayout()
        self.pick_label = QLabel("이것 쓰기:")
        self.pick_row.addWidget(self.pick_label)
        self.pick_row.addStretch(1)
        self.pick_buttons: list[QPushButton] = []
        self.edit = QPlainTextEdit()
        self.edit.setMinimumHeight(80)
        self.edit.setFont(source_font(self.edit.font(), 3))
        self.edit.setPlaceholderText("원문 (줄바꿈 그대로. 태그 칸은 한 줄에 하나. 아이콘은 [[이름]]). "
                                     "맞으면 고치지 않고 확정해도 ‘사람이 확인’이 됩니다")
        self.reread_btn = QPushButton("이 영역 다시 읽기")
        self.reread_btn.clicked.connect(self._reread)
        self.reread_model = QComboBox()
        for k, t in REREAD_MODELS:
            self.reread_model.addItem(t, k)
        self.reread_model.setToolTip("다시 읽을 방식 (정밀 읽기가 더 정확하지만 느리고 사용량이 많음)")
        self.keep_btn = QPushButton(KEEP_LABEL)
        self.keep_btn.setCheckable(True)
        self.keep_btn.setToolTip("이 영역은 글자 없이 아이콘(그림)만 있습니다.\n번역·글자 지우기·한글 넣기를 하지 않고 원본 그대로 둡니다 "
                                 "(다시 누르면 풀림).")
        self.keep_btn.clicked.connect(self._toggle_keep)
        tools = QHBoxLayout()
        tools.setSpacing(8)
        tools.addWidget(self.reread_btn)
        tools.addWidget(self.reread_model, 1)
        tools.addStretch(1)
        src_body = QWidget()
        sl = QVBoxLayout(src_body)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(6)
        sl.addWidget(self.info)
        sl.addWidget(self.readings)
        sl.addLayout(self.pick_row)
        sl.addWidget(self.edit, 1)
        sl.addLayout(tools)
        sl.addWidget(self.keep_btn)
        src_scroll = QScrollArea()
        src_scroll.setWidgetResizable(True)
        src_scroll.setFrameShape(QFrame.Shape.NoFrame)
        src_scroll.setWidget(src_body)
        self.source_panel = _panel("원문", src_scroll)
        panels = QWidget()
        pl = QHBoxLayout(panels)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(8)
        pl.addWidget(self.icon_panel, 1)
        pl.addWidget(self.source_panel, 1)
        self.icon_panel.setMinimumWidth(320)
        self.source_panel.setMinimumWidth(340)
        split = QSplitter(Qt.Orientation.Vertical)
        split.setChildrenCollapsible(False)
        split.addWidget(view_box)
        split.addWidget(panels)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setSizes([250, 480])
        self.split = split
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)
        rl.addLayout(head)
        rl.addWidget(split, 1)
        # 아래 줄
        self.close_btn = QPushButton("목록으로")
        self.close_btn.setAutoDefault(False)
        self.close_btn.clicked.connect(self.close)
        hint = QLabel("Ctrl+Enter 확정 · Alt+↑/↓ 영역 이동")
        hint.setStyleSheet("color: #8E8E93")
        self.nav_btns: dict[str, QPushButton] = {}
        for kind, text, tip in (
                ("prev_bad", "◀◀ 이전 문제", "앞쪽에서 고칠 것·볼 것·확정 전이 있는 영역으로"),
                ("prev", "◀ 이전 영역", "목록 순서로 앞 영역으로 (Alt+↑)"),
                ("next", "다음 영역 ▶", "목록 순서로 다음 영역으로 (Alt+↓)"),
                ("next_card", "다음 카드 ▶▶", "다음 카드의 첫 문제 영역으로")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setAutoDefault(False)
            b.clicked.connect(lambda _c=False, k=kind: self._go(k))
            self.nav_btns[kind] = b
        self.confirm_btn = QPushButton("이 영역 확정 → 다음 문제")
        self.confirm_btn.setStyleSheet(PRIMARY_QSS)
        self.confirm_btn.setAutoDefault(False)
        self.confirm_btn.setToolTip("확정 전 아이콘 이름을 원문에 넣고, 원문을 ‘사람이 확인’으로 저장합니다 (Ctrl+Enter)")
        self.confirm_btn.clicked.connect(self._confirm_next)
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        bottom.addWidget(self.close_btn)
        bottom.addWidget(hint, 1)
        for k in ("prev_bad", "prev", "next", "next_card"):
            bottom.addWidget(self.nav_btns[k])
        bottom.addWidget(self.confirm_btn)
        for b in (*self.nav_btns.values(), self.confirm_btn, self.close_btn, self.reread_btn, self.keep_btn):
            b.setMinimumWidth(b.sizeHint().width())
        body = QWidget()
        self.body = body
        main = QHBoxLayout()
        main.setSpacing(12)
        main.addWidget(left)
        main.addWidget(right, 1)
        bl = QVBoxLayout(body)
        bl.setContentsMargins(14, 10, 14, 10)
        bl.setSpacing(8)
        bl.addWidget(self.title)
        bl.addLayout(main, 1)
        bl.addLayout(bottom)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(body)
        for b in (self.save_btn, self.add_btn, self.del_btn, self.merge_btn, self.restore_btn, self.sentence_btn,
                  self.absorb_btn, self.overlap_btn):
            b.setAutoDefault(False)
        for keys, fn in (("Ctrl+Return", self._confirm_next), ("Ctrl+Enter", self._confirm_next),
                         ("Alt+Down", lambda: self._go("next")), ("Alt+Up", lambda: self._go("prev"))):
            QShortcut(QKeySequence(keys), self, activated=fn)

    # ------------------------------------------------------------ 데이터
    def _fetch(self) -> None:
        try:
            self.cards = list(self.rows_fn() or [])
        except Exception as e:  # noqa: BLE001 - 목록을 못 받으면 창은 빈 채로
            self.cards = []
            self._flash(f"목록을 읽지 못했습니다: {e}", "warn")
        self.flat = [(c, r) for c in self.cards for r in c["regions"]]
        self._by_key = {_nk(r["key"]): (c, r) for c, r in self.flat}
        try:
            self._reading = {_nk(k) for k in (self.reading_fn() if self.reading_fn else set())}
        except Exception:  # noqa: BLE001
            self._reading = set()

    @staticmethod
    def is_problem(r: dict) -> bool:
        return r["status"] in ("fix", "check") or bool(r.get("pending"))

    def _eff_status(self, r: dict) -> str:
        return "reading" if _nk(r["key"]) in self._reading else r["status"]

    def _initial_key(self, start: tuple) -> tuple | None:
        fd, page = start[0], start[1]
        card = next((c for c in self.cards if _nk(c["key"]) == _nk((fd, page))), None)
        if card is None:
            return self.flat[0][1]["key"] if self.flat else None
        if len(start) > 2:
            r = next((r for r in card["regions"] if r["region"] == start[2]), None)
            if r is not None:
                return r["key"]
        r = next((r for r in card["regions"] if self.is_problem(r)), None) or card["regions"][0]
        return r["key"]

    def _region(self) -> dict | None:
        if self.key is None:
            return None
        hit = self._by_key.get(_nk(self.key))
        return hit[1] if hit else None

    def _card(self) -> dict | None:
        hit = self._by_key.get(_nk(self.key)) if self.key is not None else None
        return hit[0] if hit else None

    def _dirty(self) -> bool:
        return self.key is not None and self.edit.toPlainText() != self._orig_text

    # ------------------------------------------------------------ 보이기
    def _show_region(self, key: tuple | None, prefill: str | None = None, keep_occ: str | None = None) -> None:
        self._loading = True
        try:
            self._show(key, prefill, keep_occ)
        finally:
            self._loading = False

    def _show(self, key, prefill, keep_occ) -> None:
        if key is not None and _nk(key) not in self._by_key:
            key = self.flat[0][1]["key"] if self.flat else None
        self.key = key
        reg, card = self._region(), self._card()
        if reg is None or card is None:
            self.title.setText("카드 보기")
            self.region_title.setText("보여 줄 영역이 없습니다")
            self.status_badge.hide()
            self.pending_badge.hide()
            self.view.set_data(QPixmap(), None, None, [])
            self.icon_list.clear()
            self.region_list.clear()
            self.edit.clear()
            self._orig_text = ""
            self.card_map.set_data(QPixmap(), {}, {}, "")
            self._update_buttons()
            return
        fd, page = card["file_dir"], card["page"]
        self.setWindowTitle(f"카드 보기 — {file_label(card['file_key'])} · {page}쪽")
        self.title.setText(f"{file_label(card['file_key'])} · {page}쪽")
        self._known = self._known_names(fd)
        self._fill_header(reg)
        self._fill_card(card, reg)
        self._fill_icons(reg, keep_occ)
        self._fill_view(reg)
        self._fill_source(reg, prefill)
        self._update_buttons()

    def _known_names(self, fd) -> list[str]:
        try:
            return list(service.known_icon_names(fd))
        except Exception:  # noqa: BLE001 - 자동 완성만 못 한다
            return []

    def _fill_header(self, reg: dict) -> None:
        st = self._eff_status(reg)
        self.region_title.setText(f"영역: {reg['region']}")
        self.status_badge.setText(service.STATUS_LABEL[st])
        self.status_badge.setStyleSheet(_badge(st, STATUS_COLOR[st]))
        self.status_badge.show()
        self.pending_badge.setVisible(bool(reg.get("pending")))

    def _card_info(self, card: dict) -> dict:
        k = _nk(card["key"])
        if k not in self._card_cache:
            info = {"pm": QPixmap(), "rects": {}}
            try:
                fn = getattr(service, "card_regions", None)
                if fn is not None:
                    d = fn(card["file_dir"], card["page"])
                    info["pm"].loadFromData(d["png"], "PNG")
                    info["rects"] = {n: tuple(r) for n, r in d["regions"].items()}
                else:  # 영역 상자를 모르면 쪽 그림만 (영역 목록으로 옮긴다)
                    png, _sz = service.page_image(card["file_dir"], card["page"], dpi=60)
                    info["pm"].loadFromData(png, "PNG")
            except Exception:  # noqa: BLE001 - 카드 그림은 보조
                pass
            self._card_cache[k] = info
        return self._card_cache[k]

    def _region_colors(self, card: dict) -> dict[str, str]:
        return {r["region"]: STATUS_COLOR[self._eff_status(r)] for r in card["regions"]}

    def _fill_card(self, card: dict, reg: dict) -> None:
        info = self._card_info(card)
        self.card_map.set_data(info["pm"], info["rects"], self._region_colors(card), reg["region"])
        self.region_list.blockSignals(True)
        self.region_list.clear()
        for r in card["regions"]:
            st = self._eff_status(r)
            it = QListWidgetItem(_dot(STATUS_COLOR[st]), f"{r['region']}   {service.STATUS_LABEL[st]}"
                                 + (" · 확정 전" if r.get("pending") else ""))
            it.setData(Qt.ItemDataRole.UserRole, r["region"])
            self.region_list.addItem(it)
            if r["region"] == reg["region"]:
                self.region_list.setCurrentItem(it)
        self.region_list.blockSignals(False)

    def _fill_icons(self, reg: dict, keep_occ: str | None) -> None:
        self._entries = list(reg.get("icons") or [])
        occs = [e["occ"] for e in self._entries]
        if keep_occ not in occs and self._cur_occ not in occs:
            bad = next((e for e in self._entries if e["state"] in ("lost", "unnamed")), None)
            self._cur_occ = (bad or (self._entries[0] if self._entries else {})).get("occ")
        elif keep_occ in occs:
            self._cur_occ = keep_occ
        self.icon_list.blockSignals(True)
        self.icon_list.clear()
        for e in self._entries:
            row = IconRow(e, self._known)
            row.edited.connect(self._name_edited)
            row.focused.connect(self._row_focused)
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, e["occ"])
            hint = row.sizeHint().expandedTo(row.minimumSizeHint())
            it.setSizeHint(hint.expandedTo(QSize(0, 72)))
            self.icon_list.addItem(it)
            self.icon_list.setItemWidget(it, row)
            if e["occ"] == self._cur_occ:
                it.setSelected(True)
                self.icon_list.setCurrentItem(it)
        self.icon_list.blockSignals(False)
        self._mark_rows()

    def _mark_rows(self) -> None:
        for i in range(self.icon_list.count()):
            it = self.icon_list.item(i)
            row = self.icon_list.itemWidget(it)
            if row is not None:
                row.set_current(it.isSelected())

    def _selected(self) -> list[dict]:
        occs = [self.icon_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.icon_list.count())
                if self.icon_list.item(i).isSelected()]
        return [e for e in self._entries if e["occ"] in occs]

    def _cur_entry(self) -> dict | None:
        return next((e for e in self._entries if e["occ"] == self._cur_occ), None)

    def _fill_view(self, reg: dict) -> None:
        """영역 그림: 고른 아이콘의 자리·같은 영역의 다른 아이콘. 아이콘이 없으면 영역 그림만."""
        self._place, self._overlaps, self._run_host = {}, [], None
        e = self._cur_entry()
        pm = QPixmap()
        if e is not None:
            try:
                self._place = service.icon_place(self.project_dir, e["occ"])
            except Exception:  # noqa: BLE001 - 찾기 결과가 없으면 영역 그림만
                self._place = {}
        if self._place:
            pm.loadFromData(self._place["png"], "PNG")
            by_occ = {x["occ"]: x for x in self._entries}
            sel = {x["occ"] for x in self._selected()}
            marks = []
            for m in self._place.get("marks") or []:
                x = by_occ.get(m.get("occ") or "")
                name = (x or {}).get("name") or m.get("name") or ""
                label = (f"[[{name}]]" if name else "?") + (" · 위치 못 찾음" if m.get("guess") else "")
                marks.append((m["box"], label, bool(m.get("guess")), bool(m.get("settled")),
                              bool(m.get("occ")) and m["occ"] in sel, m.get("occ") or f"#ti{m.get('ti')}"))
            self.view.set_data(pm, self._place.get("box"), self._place.get("guess"), marks)
            self._run_host = (self._place.get("run_host") or [None])[0]
        else:
            try:  # 아이콘 자리를 잡는 그림과 같은 좌표 (빈 영역에 ‘아이콘 추가’할 때 감싼 상자를 그대로 넘긴다)
                pm.loadFromData(service.region_icon_image(reg["file_dir"], reg["page"], reg["region"]), "PNG")
            except Exception:  # noqa: BLE001 - 그림만 못 보인다
                try:
                    pm.loadFromData(service.region_crop(reg["file_dir"], reg["page"], reg["region"]), "PNG")
                except Exception:  # noqa: BLE001
                    pm = QPixmap()
            self.view.set_data(pm, None, None, [])
        try:
            self._region_mode = service.region_keep_mode(reg["file_dir"], int(reg["page"]), reg["region"])
        except Exception:  # noqa: BLE001 - 표시만
            self._region_mode = "cell"

    def _fill_source(self, reg: dict, prefill: str | None) -> None:
        c = reg
        src = as_text(c["source"])
        removed = c.get("icon_removed")
        known = set(self._known)
        texts = [(reader_name(rd["model"]), fill_icon_names(as_text(rd["source"]), src, known,
                                                            None if removed is None else bool(removed)))
                 for rd in c["readings"]]
        pi = precise_index(c["readings"])
        differ = len({t for _n, t in texts}) >= 2
        reason, memo = source_text.reason_text(c, differ)
        bits = [f"<b>{_html.escape(' · '.join(c['reasons']))}</b>"] if c.get("reasons") else []
        bits.append(_html.escape(reason))
        if memo:
            bits.append(f'<span style="color:#555">읽은 모델의 메모: {_html.escape(memo)}</span>')
        if c.get("paragraph_mode") and c["source"] is not None:
            bits.append('<span style="color:#555">편집 칸의 <b>[====]</b> 줄은 문단 나눔입니다 (문단마다 다른 글꼴).</span>')
        kind = "todo" if reg["status"] in ("fix", "check") else "info"
        set_hint(self.info, "<br>".join(bits), kind)
        self.readings.fill(texts, pi)
        self._fill_picks(texts)
        base = source_text.for_edit(c, service.preview_source(c))
        self._orig_text = base
        self.edit.setPlainText(base if prefill is None else prefill)
        keep = bool(c.get("keep_original") or c.get("keep_mode"))
        self.keep_btn.setChecked(keep)
        self.keep_btn.setText(KEEP_UNDO_LABEL if keep else KEEP_LABEL)

    def _fill_picks(self, texts: list[tuple[str, str]]) -> None:
        for b in self.pick_buttons:
            self.pick_row.removeWidget(b)
            b.hide()
            b.deleteLater()
        self.pick_buttons = []
        uniq: list[tuple[str, str]] = []
        for name, t in texts:
            if t not in [x for _n, x in uniq]:
                uniq.append((name, t))
        self.pick_label.setVisible(len(uniq) >= 2)
        if len(uniq) < 2:
            return
        for i, (name, t) in enumerate(uniq):
            b = QPushButton(f"{name} 결과")
            b.setAutoDefault(False)
            b.setToolTip(t)
            b.clicked.connect(lambda _c=False, tt=t: self._use_reading(tt))
            self.pick_row.insertWidget(1 + i, b)
            self.pick_buttons.append(b)

    def _use_reading(self, text: str) -> None:
        reg = self._region() or {}
        self.edit.setPlainText(source_text.for_edit(reg, text))
        self.edit.setFocus()

    def _update_buttons(self) -> None:
        reg = self._region()
        reading = reg is not None and self._eff_status(reg) == "reading"
        ok = reg is not None and not reading and not self._rereading
        self.confirm_btn.setEnabled(ok)
        self.reread_btn.setEnabled(ok)
        self.keep_btn.setEnabled(ok and reg["source"] is not None)
        self.edit.setReadOnly(reading)
        self.confirm_btn.setToolTip("뒤에서 다시 읽는 중인 영역입니다" if reading else
                                    "확정 전 아이콘 이름을 원문에 넣고, 원문을 ‘사람이 확인’으로 저장합니다 (Ctrl+Enter)")
        self._icon_tools()
        for kind in self.nav_btns:
            self.nav_btns[kind].setEnabled(self._target(kind) is not None)

    def _icon_tools(self) -> None:
        reg = self._region()
        if reg is not None and self._eff_status(reg) == "reading":  # 뒤에서 다시 읽는 중: 같은 파일을 쓰므로 잠근다
            for b in (self.save_btn, self.add_btn, self.del_btn, self.merge_btn, self.restore_btn, self.sentence_btn):
                b.setEnabled(False)
            for b in (getattr(self, "absorb_btn", None), getattr(self, "overlap_btn", None)):
                if b is not None:
                    b.setEnabled(False)
            return
        sel = self._selected()
        has_new = self.view.new is not None
        cur = self._cur_entry()
        self.save_btn.setEnabled(has_new and cur is not None)
        reg = self._region()
        can_add = bool(self._entries) or (reg is not None and reg.get("kind") == "text"
                                           and isinstance(reg.get("source"), str))
        self.add_btn.setEnabled(has_new and can_add)
        self.del_btn.setEnabled(any(e["state"] not in ("not_icon", "absorbed") for e in sel))
        host, others = self._merge_plan()
        self.merge_btn.setEnabled(bool(host and others))
        self.merge_btn.setText(f"합치기 ({len(others) + 1}개)" if host and others else "합치기")
        self.restore_btn.setEnabled(any(e["state"] in ("not_icon", "absorbed") for e in sel))
        tis = self._sentence_tis() if self._region_mode == "sentence" else []
        anchor = self._entries[0]["occ"] if self._entries else None
        kept = None
        if tis and anchor:
            try:
                kept = service.sentence_kept(self.project_dir, anchor, tis)
            except Exception:  # noqa: BLE001 - 표시만
                kept = None
        whole = bool(self.keep_btn.isChecked())
        self.sentence_btn.setEnabled(kept is not None and not whole)
        self.sentence_btn.setText(SENTENCE_UNDO_TEXT if kept and not whole
                                  else SENTENCE_TEXT)
        self.sentence_btn.setToolTip("고른 아이콘이 든 문장(문단)만 원본 그대로 둡니다. 다른 문장은 한글화합니다.\n"
                                     "영역 설정이 ‘문단마다 글꼴 지정’인 영역에서만 쓸 수 있습니다.")
        box = self.view.new or self._place.get("box")
        cur_occ = (cur or {}).get("occ", "")
        try:
            self._overlaps = service.overlapping_icons(self._place.get("marks") or [], box, exclude=cur_occ) \
                if box and self._place else []
        except Exception:  # noqa: BLE001
            self._overlaps = []
        n = len(self._overlaps)
        self.overlap_btn.setText(f"겹친 아이콘 {n}개 흡수" if n > 1 else "겹친 아이콘 흡수")
        self.overlap_btn.setVisible(bool(n))
        host_label = "옆 아이콘"
        if self._run_host:
            hm = next((m for m in self._place.get("marks") or [] if m.get("occ") == self._run_host), None)
            hx = next((x for x in self._entries if x["occ"] == self._run_host), None)
            nm = (hx or {}).get("name") or (hm or {}).get("name") or ""
            host_label = f"[[{nm}]]" if nm else "옆 아이콘"
        self.absorb_btn.setText(f"{host_label}에 포함됨")
        self.absorb_btn.setVisible(bool(self._run_host))

    # ------------------------------------------------------------ 아이콘 목록 조작
    def _row_focused(self, occ: str) -> None:
        if occ == self._cur_occ:
            return
        self.icon_list.clearSelection()
        for i in range(self.icon_list.count()):
            if self.icon_list.item(i).data(Qt.ItemDataRole.UserRole) == occ:
                self.icon_list.item(i).setSelected(True)
                self.icon_list.setCurrentRow(i)
                break

    def _icon_selection_changed(self) -> None:
        if self._loading:
            return
        self._mark_rows()
        cur = self.icon_list.currentItem()
        occ = cur.data(Qt.ItemDataRole.UserRole) if cur is not None and cur.isSelected() else None
        if occ is None:
            sel = self._selected()
            occ = sel[0]["occ"] if sel else None
        if occ is not None and occ != self._cur_occ:
            self._cur_occ = occ
            reg = self._region()
            if reg is not None:
                self._fill_view(reg)
        self._icon_tools()
        self._repaint_marks()

    def _repaint_marks(self) -> None:
        sel = {e["occ"] for e in self._selected()}
        self.view.marks = [(*m[:4], bool(m[5]) and m[5] in sel, m[5]) if len(m) > 5 else m for m in self.view.marks]
        self.view.update()

    def _drawn(self) -> None:
        self._icon_tools()

    def _guard(self) -> bool:
        """원문 칸을 고쳐 둔 채 아이콘을 바꾸면 원문 칸을 새로 채우므로 먼저 묻는다."""
        if not self._dirty():
            return True
        r = ask_choice(self, "고친 원문이 있습니다", "아이콘을 바꾸면 원문 칸을 새로 채웁니다. 고친 원문은 사라집니다.",
                       [("계속", "destructive"), ("취소", "reject")], default=1)
        return r == 0

    def _name_edited(self, occ: str, name: str, ko: str) -> None:
        """이름·한국어 이름을 적었다: ‘확정 전’으로 적어 둔다 (원문은 확정할 때 바뀜). 같은 모양 모두 적용이 켜져 있으면 모두."""
        e = next((x for x in self._entries if x["occ"] == occ), None)
        if e is None or self._loading or self._rereading:
            return
        if name == e["name"] and (not ko or ko == e["ko"]):
            return
        if not self._guard():
            self._schedule_reload()  # 적던 값을 되돌린다
            return
        targets = [e]
        if self.same_shape.isChecked() and e.get("shape"):
            for _c, r in self.flat:
                for x in r.get("icons") or []:
                    if x["occ"] != occ and x.get("shape") == e["shape"] and x["state"] in ("unnamed", "named") \
                            and (x["state"] == "unnamed" or x["name"] == e["name"]):
                        targets.append(x)
        rows = []
        for x in targets:
            if not name:
                rows.append({"group": x["group"], "clear": True})
            else:
                v = {"group": x["group"], "name": name}
                if ko:
                    v["ko"] = ko
                rows.append(v)
        try:
            service.stage_icon_names(self.project_dir, rows)
        except Exception as ex:  # noqa: BLE001
            show_error(self, "이름을 적지 못했습니다", ex)
            return
        self.changed.emit()
        self._cur_occ = occ
        self._schedule_reload(discard=True)  # 고친 원문은 버린다고 했다: 확정 전 이름이 든 미리 보기로 새로 채운다
        n = len(targets) - 1
        self._flash(f"이름을 적었습니다 (확정 전)" + (f" · 같은 모양 {n}개에도" if n else ""))

    def _schedule_reload(self, discard: bool = False) -> None:
        """입력 칸의 신호 안에서 줄을 지우지 않게 이벤트 루프에서 다시 그린다. discard: 고친 원문을 버리고 새로 채운다."""
        self._discard_edit = getattr(self, "_discard_edit", False) or discard
        if self._pending_reload:
            return
        self._pending_reload = True

        def run():
            self._pending_reload = False
            discard_now, self._discard_edit = self._discard_edit, False
            self._reload(discard=discard_now)
        QTimer.singleShot(0, self, run)

    def _reload(self, keep_occ: str | None = None, discard: bool = False) -> None:
        """목록을 다시 받아 지금 영역을 다시 그린다 (고친 원문은 그대로, discard면 버림)."""
        text = self.edit.toPlainText() if self._dirty() and not discard else None
        self._fetch()
        self._show_region(self.key, prefill=text, keep_occ=keep_occ or self._cur_occ)

    def refresh(self) -> None:
        """목록 창이 바뀐 뒤 (뒤에서 다시 읽기 끝남 등) 다시 그린다."""
        self._card_cache.clear()
        self._reload()

    def _after_op(self, occ: str | None = None, msg: str = "") -> None:
        self.changed.emit()
        self._fetch()
        self._show_region(self.key, keep_occ=occ or self._cur_occ)
        if msg:
            self._flash(msg)

    def _open_entries(self, entries: list[dict]) -> dict[str, str]:
        """확정된 아이콘은 [[?]] 자리로 연 뒤 자리 id를 돌려준다 (그 영역 토큰 수는 그대로)."""
        anchor = self._entries[0]["occ"] if self._entries else None
        out = {}
        for e in entries:
            if e["state"] == "ok" and e.get("ti") is not None and anchor:
                out[e["occ"]] = service.reopen_icon(self.project_dir, anchor, int(e["ti"]))["occ"]
            else:
                out[e["occ"]] = e["occ"]
        return out

    @busy
    def _delete(self) -> None:
        sel = [e for e in self._selected() if e["state"] not in ("not_icon", "absorbed")]
        if not sel or not self._guard():
            return
        try:
            ids = self._open_entries(sel)
            for e in sel:
                service.set_icon_not_icon(self.project_dir, ids[e["occ"]])
        except Exception as ex:  # noqa: BLE001
            show_error(self, "‘아이콘 아님’으로 정하지 못했습니다", ex)
        self._after_op(msg="‘아이콘 아님’으로 했습니다 (확정하면 원문에서 빠집니다)")

    def _merge_plan(self) -> tuple[str | None, list[str]]:
        sel = self._selected()
        if len(sel) < 2:
            return None, []
        boxed = [e["occ"] for e in sel if e["state"] != "lost" and e.get("image")]
        host = self._cur_occ if self._cur_occ in boxed else (boxed[0] if boxed else None)
        return host, [e["occ"] for e in sel if e["occ"] != host] if host else []

    @busy
    def _merge(self) -> None:
        host, others = self._merge_plan()
        if not host or not others or not self._guard():
            return
        by = {e["occ"]: e for e in self._entries}
        try:
            ids = self._open_entries([by[host]] + [by[o] for o in others])
            service.absorb_icon_run(self.project_dir, ids[host], [ids[o] for o in others])
        except Exception as ex:  # noqa: BLE001
            show_error(self, "아이콘을 합치지 못했습니다", ex)
            return
        self._after_op(host, "아이콘을 합쳤습니다")

    @busy
    def _restore(self) -> None:
        sel = [e for e in self._selected() if e["state"] in ("not_icon", "absorbed")]
        if not sel or not self._guard():
            return
        try:
            for e in sel:
                service.restore_icon(self.project_dir, e["occ"])
        except Exception as ex:  # noqa: BLE001
            show_error(self, "되돌리지 못했습니다", ex)
        self._after_op(msg="아이콘으로 되돌렸습니다")

    @busy
    def _absorb(self) -> None:
        e = self._cur_entry()
        if e is None or not self._run_host or not self._guard():
            return
        try:
            service.absorb_icon_run(self.project_dir, self._run_host, [e["occ"]])
        except Exception as ex:  # noqa: BLE001
            show_error(self, "아이콘을 합치지 못했습니다", ex)
            return
        self._after_op(self._run_host, "옆 아이콘에 포함했습니다")

    @busy
    def _absorb_overlaps(self) -> None:
        e = self._cur_entry()
        if e is None or not self._overlaps or not self._guard():
            return
        if self.view.new is not None:
            try:
                service.set_icon_box(self.project_dir, e["occ"], self.view.new)
            except ValueError as ex:
                show_problem(self, "자리를 저장하지 못했습니다", str(ex), "아이콘을 조금 더 크게 감싸 주세요.")
                return
            except Exception as ex:  # noqa: BLE001
                show_error(self, "자리를 저장하지 못했습니다", ex)
                return
        try:
            service.absorb_icon_run(self.project_dir, e["occ"], list(self._overlaps))
        except Exception as ex:  # noqa: BLE001
            show_error(self, "아이콘을 흡수하지 못했습니다", ex)
            return
        self._after_op(e["occ"], "겹친 아이콘을 흡수했습니다")

    def _sentence_tis(self) -> list[int]:
        sel = self._selected() or ([self._cur_entry()] if self._cur_entry() else [])
        return [e["ti"] for e in sel if e.get("ti") is not None]

    @busy
    def _toggle_sentence(self) -> None:
        tis = self._sentence_tis()
        anchor = self._entries[0]["occ"] if self._entries else None
        if not tis or anchor is None or not self._guard():
            return
        try:
            keep = not service.sentence_kept(self.project_dir, anchor, tis)
            service.keep_sentence_original(self.project_dir, anchor, tis, keep)
        except Exception as ex:  # noqa: BLE001
            show_error(self, "문장 원본 그대로를 바꾸지 못했습니다", ex)
            return
        self._after_op(msg="그 문장을 원본 그대로 둡니다" if keep else "그 문장을 다시 한글화합니다")

    @busy
    def _save_box(self) -> None:
        box, e = self.view.new, self._cur_entry()
        if not box or e is None or not self._guard():
            return
        try:
            prob = service.icon_box_problem(self.project_dir, e["occ"], box)
        except Exception:  # noqa: BLE001 - 검사는 없어도 된다
            prob = ""
        if prob and ask_choice(self, "아이콘 상자 확인", prob,
                               [("다시 잡기", "reject"), ("그래도 아이콘 하나로 저장", "accept")], default=0,
                               informative="큰 아이콘은 모든 글자의 왼쪽·오른쪽에 있거나 한 줄을 아이콘이 다 차지할 때만 있습니다.") != 1:
            return
        try:
            r = service.set_icon_box(self.project_dir, e["occ"], box)
        except ValueError as ex:
            show_problem(self, "자리를 저장하지 못했습니다", str(ex), "아이콘을 조금 더 크게 감싸 주세요.")
            return
        except Exception as ex:  # noqa: BLE001
            show_error(self, "자리를 저장하지 못했습니다", ex)
            return
        if r.get("run_unfound"):  # 바로 옆에 붙은 [[?]]를 못 찾았음: 이 상자가 그것까지 감쌌을 수 있다
            n = len(r["run_unfound"])
            c = ask_choice(self, "붙어 있는 아이콘",
                           f"이 자리 바로 옆에 아이콘([[?]]) {n}개가 붙어 있습니다.\n방금 잡은 상자가 아이콘 {n + 1}개를 한 세트로 감싸고 있나요?",
                           [(f"아이콘 {n + 1}개를 한 세트로", "accept"), ("따로 있는 아이콘임", "reject")],
                           default=1 if r.get("run_vertical") else 0)
            if c == 0:
                try:
                    service.absorb_icon_run(self.project_dir, e["occ"], r["run_unfound"])
                except Exception as ex:  # noqa: BLE001
                    show_error(self, "아이콘을 합치지 못했습니다", ex)
        self._after_op(e["occ"], "자리를 저장했습니다. 확정하면 이 영역을 다시 읽습니다")

    @busy
    def _add_box(self) -> None:
        box = self.view.new
        reg = self._region()
        if not box or reg is None or not self._guard():
            return
        try:
            if self._entries:
                r = service.add_icon_box(self.project_dir, self._cur_occ or self._entries[0]["occ"], box)
            else:  # 아이콘이 하나도 없던 영역
                r = service.add_icon_box_region(self.project_dir, reg["file_dir"], reg["page"], reg["region"], box)
        except ValueError as ex:
            show_problem(self, "아이콘을 넣지 못했습니다", str(ex),
                         "" if "글자만" in str(ex) else "아이콘을 조금 더 크게 감싸 주세요.")
            return
        except Exception as ex:  # noqa: BLE001
            show_error(self, "아이콘을 넣지 못했습니다", ex)
            return
        self._after_op(r["occ"], f"원문 {r['line']}줄에 아이콘을 넣었습니다 — 이름을 적으세요")

    # ------------------------------------------------------------ 원문 칸 조작
    def _edit_value(self, reg: dict) -> tuple:
        """편집 칸 -> (저장할 값, 문단 나눔)."""
        text = self.edit.toPlainText().strip("\n")
        if reg["kind"] == "tag_pills":
            return [t.strip() for t in text.split("\n") if t.strip()], None
        if reg.get("paragraph_mode"):
            text, breaks = service.unmark_paragraphs(text)
            return text.strip("\n"), breaks
        return text, None

    @busy
    def _toggle_keep(self) -> None:
        reg = self._region()
        if reg is None:
            return
        keep = self.keep_btn.isChecked()
        if not self._guard():
            self.keep_btn.setChecked(not keep)
            return
        try:
            service.set_cell_keep_original(reg["file_dir"], reg["page"], reg["region"], keep)
        except Exception as ex:  # noqa: BLE001
            show_error(self, "정하지 못했습니다", ex)
            self.keep_btn.setChecked(not keep)
            return
        self._after_op(msg="이 영역은 원본 그대로 둡니다" if keep else "이 영역을 다시 한글화합니다")

    def _reread(self) -> None:
        reg = self._region()
        if reg is None or self._rereading or not self._guard():
            return
        eng = self.engine or service.get_settings().engine
        st = service.engine_status()
        if not st.get(eng):
            show_error(self, "Claude에 연결할 수 없습니다", st["message"],
                       "‘파일 > 로그인 방식’에서 로그인하거나 다른 방식을 고른 뒤 다시 하세요.")
            return
        key = reg["key"]
        job = worker.Job(service.reread_region, reg["file_dir"], reg["page"], reg["region"],
                         model=self.reread_model.currentData(), engine=eng, with_progress=False)
        self._lock(True)
        worker.run_with_dialog(self, f"{reg['page']}쪽 {reg['region']} 다시 읽는 중", job,
                               lambda r: self._reread_done(r, key), self._reread_failed, cancellable=False,
                               on_cancelled=lambda: self._lock(False))

    def _lock(self, on: bool) -> None:
        self._rereading = on
        try:
            self.body.setEnabled(not on)
        except RuntimeError:  # 창이 이미 지워짐
            pass

    def _reread_failed(self, msg: str) -> None:
        self._lock(False)
        _fail_as(self, "다시 읽지 못했습니다")(msg)

    def _reread_done(self, r, key: tuple) -> None:
        try:
            self._lock(False)
            if r.state != "done":
                show_problem(self, f"다시 읽지 못했습니다 ({state_label(r.state)})", r.reason or "",
                             retry_after=r.retry_after)
                return
            self.changed.emit()
            self._fetch()
            reg = self._by_key.get(_nk(key), (None, None))[1]
            if reg is None or self.key is None or _nk(self.key) != _nk(key):
                return
            removed = reg.get("icon_removed")
            text = fill_icon_names(as_text(r.source), as_text(reg["source"]), set(self._known),
                                   None if removed is None else bool(removed))
            self._show_region(key, prefill=source_text.for_edit(reg, text))
            msg = "다시 읽은 결과를 원문 칸에 넣었습니다. 맞으면 확정하세요."
            if UNKNOWN_ICON in text:
                msg += " 이름 없는 아이콘 [[?]]는 이름을 적어 주세요."
            if r.uncertain:
                msg += " (다시 읽은 결과도 확실하지 않다고 했습니다)"
            self._flash(msg, "warn")
        except RuntimeError:  # 읽는 동안 창이 닫혀 C++ 객체가 지워짐
            return

    # ------------------------------------------------------------ 확정
    def _confirm(self) -> dict | None:
        """지금 영역을 확정한다. 성공하면 confirm_regions 결과, 막히거나 실패하면 None."""
        reg = self._region()
        if reg is None or self._eff_status(reg) == "reading" or self._rereading:
            return None
        edits = {}
        if self._dirty():
            value, breaks = self._edit_value(reg)
            edits = {tuple(reg["key"]): {"source": value, "breaks": breaks}}
        try:
            r = service.confirm_regions(self.project_dir, [tuple(reg["key"])], edits)
        except Exception as ex:  # noqa: BLE001
            show_error(self, "확정하지 못했습니다", ex)
            return None
        if r["state"] != "confirmed":
            show_problem(self, "확정하지 못했습니다", "", "아이콘 이름을 적거나 ‘아이콘 아님’으로 정한 뒤 다시 확정하세요.",
                         items=[str(x) for x in r["blocking"]])
            return None
        key = _nk(reg["key"])
        in_reread = any(_nk((c["file_dir"], c["page"], c["region"])) == key for c in r["reread"])
        in_still = any(_nk(k) == key for k in r["still"])
        self.changed.emit()
        self.confirmed.emit(r)
        self._flash("확정 → 다시 읽는 중" if in_reread else
                    "아이콘이 덜 끝나 다시 읽지 않았습니다" if in_still else "확인됨", "warn" if in_still else "ok")
        return r

    def _confirm_next(self) -> None:
        if self._confirm() is None:
            return
        cur = self.key
        self._fetch()
        nxt = self._neighbor(cur, +1, only_problem=True, wrap=True)
        self._show_region(nxt or cur)

    # ------------------------------------------------------------ 옮기기
    def _index(self, key) -> int | None:
        k = _nk(key) if key is not None else None
        return next((i for i, (_c, r) in enumerate(self.flat) if _nk(r["key"]) == k), None)

    def _neighbor(self, key, step: int, only_problem: bool, wrap: bool = False) -> tuple | None:
        i = self._index(key)
        n = len(self.flat)
        if i is None or not n:
            return None
        order = range(i + 1, n) if step > 0 else range(i - 1, -1, -1)
        cand = list(order)
        if wrap:
            cand += list(range(0, i)) if step > 0 else list(range(n - 1, i, -1))
        for j in cand:
            r = self.flat[j][1]
            if not only_problem or self.is_problem(r):
                return r["key"]
        return None

    def _target(self, kind: str) -> tuple | None:
        if self.key is None:
            return None
        if kind == "prev":
            return self._neighbor(self.key, -1, False)
        if kind == "next":
            return self._neighbor(self.key, +1, False)
        if kind == "prev_bad":
            return self._neighbor(self.key, -1, True)
        if kind == "next_card":
            card = self._card()
            i = self._index(self.key)
            if card is None or i is None:
                return None
            for _c, r in self.flat[i + 1:]:
                if _nk(r["key"])[:2] != _nk(card["key"]):
                    first = next((x for x in next(c for c in self.cards if _nk(c["key"]) == _nk(r["key"])[:2])
                                  ["regions"] if self.is_problem(x)), None)
                    return (first or r)["key"]
        return None

    def _go(self, kind: str) -> None:
        t = self._target(kind)
        if t is not None:
            self._select(t)

    def _select(self, key: tuple) -> bool:
        """다른 영역으로. 고친 원문이 있으면 묻는다 (확정 / 버리기 / 취소)."""
        if self.key is not None and _nk(key) == _nk(self.key):
            return True
        if not self._leave_ok():
            self._sync_list()
            return False
        self._cur_occ = None
        self._fetch()
        self._show_region(key)
        return True

    def _sync_list(self) -> None:
        """영역 목록·카드 그림의 고른 줄을 지금 영역으로 되돌린다."""
        reg = self._region()
        if reg is None:
            return
        self.region_list.blockSignals(True)
        for i in range(self.region_list.count()):
            if self.region_list.item(i).data(Qt.ItemDataRole.UserRole) == reg["region"]:
                self.region_list.setCurrentRow(i)
        self.region_list.blockSignals(False)

    def _pick_name(self, name: str) -> None:
        card = self._card()
        r = next((x for x in card["regions"] if x["region"] == name), None) if card else None
        if r is not None:
            self._select(r["key"])

    def _region_row_changed(self, row: int) -> None:
        if self._loading or row < 0:
            return
        self._pick_name(self.region_list.item(row).data(Qt.ItemDataRole.UserRole))

    def _leave_ok(self) -> bool:
        if not self._dirty():
            return True
        reg = self._region() or {}
        r = ask_choice(self, "고친 원문이 있습니다", f"{reg.get('page', '')}쪽 {reg.get('region', '')}의 고친 원문을 확정할까요?",
                       [("확정", "accept"), ("버리기", "destructive"), ("취소", "reject")], default=0)
        if r == 0:
            return self._confirm() is not None
        return r == 1

    def done(self, r):  # noqa: D102 - QDialog
        if self._rereading:
            return
        if self._leave_ok():
            super().done(r)

    # ------------------------------------------------------------ 알림
    def _flash(self, text: str, kind: str = "info") -> None:
        color = {"ok": "#2E8B57", "warn": "#B35C00", "info": "#6E6E73"}.get(kind, "#6E6E73")
        self.flash_label.setText(f'<span style="color:{color}; font-weight:600">{_html.escape(text)}</span>')
        self.flash_timer.start(8000)

