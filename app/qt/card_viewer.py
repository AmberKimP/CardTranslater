"""② 원문 확인: 카드 보기 (카드 한 장을 보고 영역 하나씩 고친다).

한 영역을 왼쪽 → 오른쪽으로 한 번에 처리하게 놓는다: 왼쪽에 카드 전체(영역 상자는 상태 색 테두리)·영역 목록·이동 단추,
가운데에 그 영역의 그림(아이콘 상자·끌어 감싸기, 가장 크게)과 그 아래 아이콘 띠(이름 칸)·고른 아이콘 조작,
오른쪽에 고른 아이콘 확대·원문(읽은 결과·편집 칸)·확정 단추가 있다. 아이콘 이름·아이콘 아님은 ‘확정 전’으로 적어 두었다가
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
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QCompleter, QDialog, QFrame,
                               QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem,
                               QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from app import service
from app.qt import worker
from app.qt.dialogs import (PRIMARY_QSS, ask_choice, file_label, fit_to_screen, hint_bar, page_title, set_hint,
                            show_error, show_problem)
from app.qt.icon_place import IconPlaceView, busy
from app.qt import source_text, ui_codes
from app.qt.step_common import (ICON_TOKEN_RE, KEEP_LABEL, KEEP_UNDO_LABEL, REREAD_MODELS, UNKNOWN_ICON, _fail_as,
                                as_text, diff_html, fill_icon_names, precise_index, reader_name, source_font,
                                state_label)

# 영역 상태 색 (상자 테두리·점·이름표). 설계안 2.2: 빨강 고칠 것, 주황 볼 것, 파랑 읽는 중, 옅은 회색 확인 안 함, 초록 확인됨, 회색 원본 그대로
STATUS_COLOR = {"fix": "#D62828", "check": "#E67E22", "reading": "#1F6FEB", "todo": "#AEAEB2", "ok": "#2E8B57",
                "keep": "#8A8A8A"}
STAGED_BG = "#F6F0FD"  # 확정 전 줄 (연보라)
PINK_BG = "#FDECEC"  # 이름 없음·위치 못 찾음 줄
GRAY_BG = "#E9E9EC"  # 아이콘 아님·옆 아이콘에 포함 줄 (그림의 회색 상자와 같은 뜻: 확정 전이어도 회색)
ENTRY_TEXT = {"ok": "확정됨", "named": "이름 정함", "unnamed": "이름 없음", "lost": "위치 못 찾음", "not_icon": "아이콘 아님",
              "absorbed": "옆 아이콘에 포함"}
MAG_SIZE = 110  # 고른 아이콘 확대 칸 (px, 오른쪽 칸 위)
LEFT_W, RIGHT_W, RIGHT_MIN_W = 240, 400, 380  # 왼쪽(이동) 칸 폭, 오른쪽(글·확정) 칸 폭·최소 폭 (px)
ICON_CARD_W, ICON_ROW_H = 250, 92  # 아이콘 띠의 아이콘 한 칸 폭·높이 (px). 이름 · 상태(몇 번 줄) · 그 줄 모양 세 줄
ICON_STRIP_CARDS = 4  # 아이콘 띠는 아이콘 4칸이 보이는 높이 (더 많으면 띠 안에서 스크롤, 적으면 빈칸)
PREV_CONFIRM_TEXT, NEXT_CONFIRM_TEXT, CONFIRM_ONLY_TEXT = "◀◀ 확정 → 이전 문제", "이 영역 확정 → 다음 문제", "이 영역 확정"
NAV_HINT = "Alt+←/→ 영역 · Alt+Shift+←/→ 문제 카드"
NO_TARGET = {"prev": "다른 영역이 없습니다", "next": "다른 영역이 없습니다",
             "next_card": "다음 카드가 없습니다", "prev_card_bad": "앞쪽에 문제 카드가 없습니다",
             "next_card_bad": "뒤쪽에 문제 카드가 없습니다"}
SENTENCE_TEXT, SENTENCE_UNDO_TEXT ="이 문장만 원본 그대로", "이 문장 원본 그대로 · 되돌리기"  # 카드 보기·아이콘 자리 잡기와 같은 말


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
    """아이콘 한 줄: 그림 · 이름 칸 · 상태. 이름은 Enter·포커스를 옮길 때 edited(자리 id, 이름, 한국어 이름).
    한국어 이름은 카드 보기에서 적지 않는다 (사용자 요청): 있던 값을 그대로 넘긴다."""

    edited = Signal(str, str, str)
    focused = Signal(str)

    def __init__(self, e: dict, names: list[str]):
        super().__init__()
        self.occ = e["occ"]
        self.entry = e
        self.setObjectName("iconrow")
        bg = (GRAY_BG if e["state"] in ("not_icon", "absorbed") else PINK_BG if e["state"] in ("unnamed", "lost")
              else STAGED_BG if e["pending"] else "white")
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
        self.name.setPlaceholderText("이름 (영문·숫자·_)")
        self.name.setEnabled(editable)
        self.name.setMinimumWidth(110)
        self.name.setMinimumHeight(28)
        comp = QCompleter(names, self.name)
        comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.name.setCompleter(comp)
        tag = ENTRY_TEXT.get(e["state"], e["state"]) + (" · 확정 전" if e["pending"] else "")
        # 원문 몇 번 줄의 어느 [[…]]인가 (줄마다 [[?]]가 여럿이면 어느 것을 고르는지 헷갈렸다).
        # 상태·줄 번호 한 줄, 그 줄의 모양(2■ □ □)은 다음 줄 (한 줄에 넣으면 괄호 안 아무 데서나 줄이 바뀌었다)
        line_no, snippet = e.get("where") or ("", "")
        if line_no:
            tag += f" ({line_no})"
        self.tag = QLabel(tag)
        self.tag.setStyleSheet("color: #6E6E73;")
        self.tag.setWordWrap(True)  # 아이콘 띠의 칸 폭이 정해져 있다: 긴 상태 글은 줄을 바꾼다
        self.where = QLabel(snippet)
        self.where.setStyleSheet("color: #3A3A3C;")
        self.where.setFont(source_font(self.where.font(), 0))
        self.where.setVisible(bool(snippet))
        lay = QGridLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setHorizontalSpacing(8)
        lay.setVerticalSpacing(3)
        lay.addWidget(self.img, 0, 0, 3, 1, Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self.name, 0, 1)
        lay.addWidget(self.tag, 1, 1)
        lay.addWidget(self.where, 2, 1)
        lay.setColumnStretch(1, 1)
        self.name.editingFinished.connect(self._done)
        self.name.installEventFilter(self)

    def set_current(self, on: bool) -> None:
        self.setStyleSheet(f"QFrame#iconrow {{ background: {self._bg}; border-radius: 6px;"
                           f" border: {'2px solid #7B2CBF' if on else '1px solid #E5E5EA'}; }}")

    def eventFilter(self, obj, ev):  # noqa: N802 - Qt 이름
        if ev.type() == QEvent.Type.FocusIn:
            self.focused.emit(self.occ)
        return super().eventFilter(obj, ev)

    def _done(self) -> None:
        self.name.setModified(False)  # 적기를 마침 (카드 보기가 ‘이름을 적는 중’으로 보지 않게)
        self.edited.emit(self.occ, self.name.text().strip(), "")


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


def _adopt(parent: QWidget, widgets) -> None:
    """위젯들을 이 순서로 parent의 자식으로 붙인다 (배치에 넣기 전에). 버튼 코드(ui_codes)는 위젯 나무의 순서라
    화면 자리를 옮겨도 예전 번호가 그대로이게 한다. 배치에 넣을 때는 부모가 같아 순서가 바뀌지 않는다."""
    for w in widgets:
        w.setParent(parent)


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
        self._refresh_pending = False  # 뒤에서 다시 읽기가 이 카드를 바꿨는데 사람이 조작 중이라 아직 다시 그리지 않음
        self._rereading = False
        self._loading = False
        self._card_cache: dict[tuple, dict] = {}
        self._known: list[str] = []
        self.cards: list[dict] = []
        self.flat: list[tuple[dict, dict]] = []
        self._by_key: dict = {}
        self._reading: set = set()
        self.key: tuple | None = None
        self._entries: list[dict] = []
        self._cur_occ: str | None = None
        self._place: dict = {}
        self._orig_text = ""
        self._overlaps: list[str] = []
        self._run_host: str | None = None
        self._region_mode = "cell"
        self._pending_reload = False
        self._shown_key: tuple | None = None
        self._fresh_reread: set = set()  # 아이콘을 모두 정한 뒤 보통·정밀 읽기로 다시 읽은 영역 (확정 때 또 읽지 않음)
        self._build()
        self._fetch()
        self._show_region(self._initial_key(start))

    # ------------------------------------------------------------ 화면 만들기
    # 배치(시안 A): 한 영역을 왼쪽 → 오른쪽, 위 → 아래로 한 번에 처리한다.
    #   머리: 쪽 제목 · 이 카드 진행 · (이 카드 영역 고치기 · 목록으로)
    #   왼쪽(이동): 카드 전체 · 영역 목록 · 이전/다음 영역 · 다음 카드
    #   가운데(그림): 영역 머리 · 영역 그림(가장 크게, 오른쪽 위 확대·축소) · 색 설명 · 아이콘 띠 · 고른 아이콘 조작 한 줄
    #   오른쪽(글·확정): 고른 아이콘 확대 · 원문(읽은 결과·이것 쓰기·편집 칸·도구) · 확정 단추
    # 버튼 코드(ui_codes)는 창 안 위젯 나무의 순서다: 위치가 바뀌어도 예전 번호가 그대로이게 부모를 정해 둔 순서로 붙인다
    # (_adopt). 새 버튼은 맨 뒤에 붙인다.
    def _build(self) -> None:
        self.title = page_title("카드 보기", 14)
        self.progress_label = QLabel()
        self.progress_label.setStyleSheet("color: #6E6E73;")
        self.progress_label.setToolTip("이 카드의 영역 중 확인됨·원본 그대로인 영역 수 / 전체 영역 수")
        left = self._build_left_panel()
        head = self._build_region_head()
        view_frame = self._build_view_box()
        self._build_icon_panel()
        self._build_source_panel()
        self._build_bottom_row()
        body = QWidget()
        self.body = body
        # 버튼 코드 순서: 아이콘 칸(01~09) → 원문 칸(10~12) → 확대·축소(13~15) → 목록으로(16) → 이동·확정(17~21)
        _adopt(body, (self.icon_panel, self.source_panel, view_frame, self.close_btn,
                      *(self.nav_btns[k] for k in ("prev_bad", "prev", "next", "next_card")), self.confirm_btn))
        header = QHBoxLayout()
        header.setSpacing(10)
        header.addWidget(self.title)
        header.addWidget(self.progress_label)
        header.addStretch(1)
        header.addWidget(self.close_btn)
        center = self._build_center(head, view_frame)
        right = self._build_right_panel()
        main = QHBoxLayout()
        main.setSpacing(12)
        main.addLayout(left)
        main.addLayout(center, 1)
        main.addLayout(right)
        bl = QVBoxLayout(body)
        bl.setContentsMargins(14, 10, 14, 10)
        bl.setSpacing(8)
        bl.addLayout(header)
        bl.addLayout(main, 1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(body)
        self._build_late_widgets(header)

    def _build_left_panel(self) -> QVBoxLayout:
        """왼쪽(이동): 카드 전체 + 영역 목록 (이전·다음 단추는 _build_right_panel 앞에서 만든 뒤 _left에 붙인다)."""
        self.card_map = CardMap()
        self.card_map.setMinimumSize(200, 150)
        self.card_map.picked.connect(self._pick_name)
        self.region_list = QListWidget()
        self.region_list.setFixedHeight(90)  # 영역 수에 맞춰 _fit_region_list가 줄인다 (남는 높이는 카드 전체로)
        self.region_list.setToolTip("영역 목록 (↑/↓로 고르고, Alt+←/→·Alt+↑/↓로도 옮깁니다)")
        self.region_list.currentRowChanged.connect(self._region_row_changed)
        for w in (self.card_map, self.region_list):
            w.setFixedWidth(LEFT_W)
        ll = QVBoxLayout()
        ll.setSpacing(6)
        ll.addWidget(QLabel("<b>카드 전체</b>"))
        ll.addWidget(self.card_map, 1)
        ll.addWidget(QLabel("<b>영역 목록</b>"))
        ll.addWidget(self.region_list)
        self._left = ll
        return ll

    def _build_region_head(self) -> QHBoxLayout:
        """가운데 머리: 영역 이름·상태 표시·알림 글."""
        self.region_title = QLabel()
        self.region_title.setStyleSheet("font-size: 15px; font-weight: 600; color: #1D1D1F;")
        self.status_badge = QLabel()
        self.pending_badge = QLabel("확정 전")
        self.pending_badge.setStyleSheet(_badge("확정 전", "#7B2CBF", solid=False))
        self.flash_label = QLabel()
        self.flash_label.setTextFormat(Qt.TextFormat.RichText)
        self.flash_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.flash_label.setWordWrap(True)  # 긴 알림이 가운데 칸(과 창 최소 폭)을 넓히지 않게 줄을 바꾼다
        self.flash_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.flash_timer = QTimer(self)
        self.flash_timer.setSingleShot(True)
        self.flash_timer.timeout.connect(lambda: self.flash_label.setText(""))
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(self.region_title)
        head.addWidget(self.status_badge)
        head.addWidget(self.pending_badge)
        head.addWidget(self.flash_label, 1)
        return head

    def _build_view_box(self) -> QFrame:
        """영역 그림 틀: 오른쪽 위 얇은 줄에 확대·축소, 그 아래 그림. 색 설명(view_hint)·고른 아이콘 확대 칸(mag)도 만든다."""
        self.view = IconPlaceView()
        self.view.drawn.connect(self._drawn)
        self.view.picked.connect(self._pick_icon)
        self.view.zoomed.connect(self._zoomed)
        self.view_hint = QLabel('<span style="color:#2E8B57">■</span> 찾은 아이콘 &nbsp; '
                                '<span style="color:#1F6FEB">■</span> 고른 아이콘 &nbsp; '
                                '<span style="color:#E67E22">■</span> 예상 위치 &nbsp; '
                                '<span style="color:#D62828">■</span> 새로 잡은 자리 &nbsp; '
                                '<span style="color:#8A8A8A">■</span> 아이콘 아님·포함')
        self.view_hint.setStyleSheet("color: #6E6E73;")
        self.view_hint.setToolTip("빈 곳을 끌어 감싸면 새 자리 · 상자 테두리·모서리를 끌면 크기 바꾸기 · 아이콘 상자를 누르면 그 아이콘 고르기\n"
                                  "휠: 확대·축소 · 확대한 뒤 오른쪽(가운데) 단추로 끌면 옮기기")
        # 좁은 창에서 한 줄 설명이 창 최소 폭을 늘리지 않게 (잘리면 마우스를 올려 봄)
        self.view_hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.zoom_out_btn = QPushButton("축소")  # 전각 ‘－’·‘＋’는 좁은 버튼에서 글자가 보이지 않았다
        self.zoom_in_btn = QPushButton("확대")
        self.fit_btn = QPushButton("화면 맞춤")
        self.zoom_label = QLabel("100%")
        self.zoom_label.setStyleSheet("color: #6E6E73; border: none;")
        self.zoom_label.setMinimumWidth(44)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.zoom_out_btn.setToolTip("축소 (휠을 아래로)")
        self.zoom_in_btn.setToolTip("확대 (휠을 위로: 마우스가 있는 곳을 크게)")
        self.fit_btn.setToolTip("그림 전체가 보이게 (영역을 옮기면 늘 화면 맞춤으로 엽니다)")
        self.zoom_out_btn.clicked.connect(lambda: self.view.zoom_by(0.8))
        self.zoom_in_btn.clicked.connect(lambda: self.view.zoom_by(1.25))
        self.fit_btn.clicked.connect(self.view.fit)
        view_tools = QHBoxLayout()
        view_tools.setContentsMargins(0, 0, 0, 0)
        view_tools.setSpacing(4)
        view_tools.addStretch(1)
        view_tools.addWidget(self.zoom_out_btn)
        view_tools.addWidget(self.zoom_label)
        view_tools.addWidget(self.zoom_in_btn)
        view_tools.addWidget(self.fit_btn)
        # 고른 아이콘 확대 칸 (고정 크기, 오른쪽 칸 위): 지금 자리, 새 자리를 그리거나 크기를 바꾸면 따라 그린다
        self.mag = QLabel()
        self.mag.setFixedSize(MAG_SIZE, MAG_SIZE)
        self.mag.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.mag.setWordWrap(True)
        self.mag.setStyleSheet("background: #FAFAFA; border: 1px solid #E5E5EA; border-radius: 6px; color: #8E8E93;")
        self.mag.setToolTip("고른 아이콘을 크게 봅니다 (파랑 = 지금 자리, 빨강 = 새로 잡는 자리)")
        self.view.box_moved.connect(self._update_mag)
        frame = QFrame()
        frame.setObjectName("viewframe")
        frame.setStyleSheet("QFrame#viewframe { background: white; border: 1px solid #E5E5EA; border-radius: 10px; }")
        vl = QVBoxLayout(frame)
        vl.setContentsMargins(6, 4, 6, 6)
        vl.setSpacing(2)
        _adopt(frame, (self.view, self.zoom_out_btn, self.zoom_label, self.zoom_in_btn, self.fit_btn))
        vl.addLayout(view_tools)
        vl.addWidget(self.view, 1)
        self.view_frame = frame
        return frame

    def _build_icon_panel(self) -> None:
        """‘이 영역의 아이콘’ 칸 (self.icon_panel): 아이콘 띠(가로로 놓고 넘치면 다음 줄) + 고른 아이콘 조작 한 줄."""
        self.icon_list = QListWidget()
        self.icon_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.icon_list.setFlow(QListView.Flow.LeftToRight)
        self.icon_list.setWrapping(True)
        self.icon_list.setResizeMode(QListView.ResizeMode.Adjust)
        self.icon_list.setSpacing(2)
        self.icon_list.setMinimumHeight(ICON_ROW_H + 8)
        self.icon_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.icon_list.setStyleSheet("QListWidget { border: none; background: white; }"
                                     "QListWidget::item { background: transparent; }"
                                     "QListWidget::item:selected { background: transparent; }")
        self.icon_list.itemSelectionChanged.connect(self._icon_selection_changed)
        self.icon_list.installEventFilter(self)  # 띠 폭이 바뀌면(창 크기와 따로 배치가 바뀔 때도) 4칸 높이를 다시 맞춘다
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
        self.save_btn.setToolTip("고른 아이콘을 그림에서 끌어 감싼 자리로 다시 자릅니다 (바로 저장, Ctrl+S)")
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
        icons_body = QWidget()
        # 버튼 코드 순서 (01~09): 같은 모양 · 저장 · 추가 · 삭제 · 합치기 · 되돌리기 · 문장 · 포함 · 흡수
        _adopt(icons_body, (self.icon_list, self.same_shape, self.save_btn, self.add_btn, self.del_btn, self.merge_btn,
                            self.restore_btn, self.sentence_btn, self.absorb_btn, self.overlap_btn))
        # 띠 오른쪽: 같은 모양에 모두 적용 · 아이콘 추가 (고른 아이콘이 아니라 새 아이콘을 넣는 일. 조작 줄에 두면
        # 작은 노트북에서 창 최소 폭이 화면보다 넓어졌다)
        side = QVBoxLayout()
        side.setSpacing(4)
        side.addWidget(self.same_shape)
        side.addWidget(self.add_btn)
        side.addStretch(1)
        strip = QHBoxLayout()
        strip.setSpacing(8)
        strip.addWidget(self.icon_list, 1)
        strip.addLayout(side)
        acts = QHBoxLayout()
        acts.setSpacing(4)
        for b in (self.save_btn, self.del_btn, self.merge_btn, self.restore_btn, self.sentence_btn,
                  self.absorb_btn, self.overlap_btn):
            acts.addWidget(b)
        acts.addStretch(1)
        il = QVBoxLayout(icons_body)
        il.setContentsMargins(0, 0, 0, 0)
        il.setSpacing(4)
        il.addLayout(strip)
        il.addLayout(acts)
        self.icon_panel = _panel("이 영역의 아이콘", icons_body)

    def _build_source_panel(self) -> None:
        """‘원문’ 칸 (self.source_panel): 안내 · 읽은 결과(줄마다 ‘이것 쓰기’) · 편집 칸 · 도구."""
        self.info = hint_bar("", "info")
        self.info.setTextFormat(Qt.TextFormat.RichText)
        self.readings = ReadingsTable()
        # ‘이것 쓰기’ 단추는 읽은 결과 표의 각 줄 왼쪽 칸(읽은 방식 이름 아래)에 붙는다 (_fill_picks). 이 줄은 그 안내
        self.pick_row = QHBoxLayout()
        self.pick_label = QLabel("이것 쓰기:")
        self.pick_label.setStyleSheet("color: #6E6E73;")
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
        self.reread_model.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.keep_btn = QPushButton(KEEP_LABEL)
        self.keep_btn.setCheckable(True)
        self.keep_btn.setToolTip("이 영역은 글자 없이 아이콘(그림)만 있습니다.\n번역·글자 지우기·한글 넣기를 하지 않고 원본 그대로 둡니다 "
                                 "(다시 누르면 풀림).")
        self.keep_btn.clicked.connect(self._toggle_keep)
        # 문단 나눔 [====]: ‘문단마다 다른 글꼴’ 영역에서 커서가 있는 줄 위에 나눔 줄을 넣는다 (지우려면 그 줄을 지움)
        self.para_btn = QPushButton("[====] 문단 나눔 넣기")
        self.para_btn.clicked.connect(self._insert_paragraph_mark)
        # 도구: 좁은 오른쪽 칸에 맞게 두 줄 (다시 읽기 + 방식 / 문단 나눔 · 원본 그대로)
        tools = QGridLayout()
        tools.setHorizontalSpacing(6)
        tools.setVerticalSpacing(4)
        tools.addWidget(self.reread_btn, 0, 0)
        tools.addWidget(self.reread_model, 0, 1)
        tools.addWidget(self.para_btn, 1, 0)
        tools.addWidget(self.keep_btn, 1, 1)
        tools.setColumnStretch(1, 1)
        src_body = QWidget()
        # 버튼 코드 순서 (10~12): 다시 읽기 · 문단 나눔 · 원본 그대로
        _adopt(src_body, (self.info, self.readings, self.pick_label, self.edit, self.reread_btn, self.reread_model,
                          self.para_btn, self.keep_btn))
        sl = QVBoxLayout(src_body)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(6)
        sl.addWidget(self.info)
        sl.addLayout(self.pick_row)
        sl.addWidget(self.readings)
        sl.addWidget(self.edit, 1)
        sl.addLayout(tools)
        src_scroll = QScrollArea()
        src_scroll.setWidgetResizable(True)
        src_scroll.setFrameShape(QFrame.Shape.NoFrame)
        src_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        src_scroll.setWidget(src_body)
        self.source_panel = _panel("원문", src_scroll)

    def _build_center(self, head: QHBoxLayout, view_frame: QFrame) -> QVBoxLayout:
        """가운데(그림 작업): 영역 머리 · 영역 그림(가장 크게) · 색 설명 · 아이콘 칸."""
        cl = QVBoxLayout()
        cl.setSpacing(6)
        cl.addLayout(head)
        cl.addWidget(view_frame, 1)
        cl.addWidget(self.view_hint)
        cl.addWidget(self.icon_panel)
        return cl

    def _build_right_panel(self) -> QVBoxLayout:
        """오른쪽(글·확정): 고른 아이콘 확대 + 어느 [[…]]인지 · 원문 칸 · 큰 확정 단추 · ◀◀ 확정 → 이전 문제 · 단축키 안내.
        왼쪽 칸 아래에 이전·다음 영역·다음 카드 단추와 안내도 붙인다."""
        # 왼쪽 칸 아래: ◀ 이전 영역 | 다음 영역 ▶, 다음 카드 ▶▶, 안내
        pn = QHBoxLayout()
        pn.setSpacing(4)
        pn.addWidget(self.nav_btns["prev"])
        pn.addWidget(self.nav_btns["next"])
        self._left.addLayout(pn)
        self._left.addWidget(self.nav_btns["next_card"])
        nav_hint = QLabel(NAV_HINT)
        nav_hint.setWordWrap(True)
        nav_hint.setStyleSheet("color: #8E8E93;")
        nav_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._left.addWidget(nav_hint)
        # 오른쪽 위: 고른 아이콘 확대 + 원문의 어느 [[…]]인가
        self.mag_title = QLabel("<b>고른 아이콘</b>")
        self.mag_where = QLabel()
        self.mag_where.setWordWrap(True)
        self.mag_where.setStyleSheet("color: #3A3A3C;")
        self.mag_where.setFont(source_font(self.mag_where.font(), 1))
        self.mag_where.setToolTip("고른 아이콘이 원문 몇 번 줄의 어느 [[…]]인가 (■ = 이 아이콘, □ = 같은 줄의 다른 아이콘)")
        mt = QVBoxLayout()
        mt.setSpacing(4)
        mt.addWidget(self.mag_title)
        mt.addWidget(self.mag_where)
        mt.addStretch(1)
        mr = QHBoxLayout()
        mr.setSpacing(10)
        mr.addWidget(self.mag)
        mr.addLayout(mt, 1)
        self.source_panel.setMinimumWidth(RIGHT_MIN_W)
        self.source_panel.setMaximumWidth(RIGHT_W)
        self.confirm_btn.setStyleSheet(PRIMARY_QSS.replace("min-height: 22px;", "min-height: 30px;"))
        hint = QLabel("Ctrl+Enter 확정 · Ctrl+S 이 자리로 저장 · " + NAV_HINT)
        hint.setStyleSheet("color: #8E8E93")
        hint.setToolTip(hint.text())
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        # 글이 길어져도 창 최소 폭을 늘리지 않는다 (좁으면 잘려 보이고, 마우스를 올리면 다 보인다)
        hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        rl = QVBoxLayout()
        rl.setSpacing(6)
        rl.addLayout(mr)
        rl.addWidget(self.source_panel, 1)
        rl.addWidget(self.confirm_btn)
        rl.addWidget(self.nav_btns["prev_bad"])
        rl.addWidget(hint)
        return rl

    def _build_bottom_row(self) -> None:
        """이동·확정·목록으로 단추 (예전 아래 줄에 있던 것. 자리는 머리·왼쪽·오른쪽 칸으로 나뉘었다)."""
        self.close_btn = QPushButton("목록으로")
        self.close_btn.setAutoDefault(False)
        self.close_btn.clicked.connect(self.close)
        self.nav_btns: dict[str, QPushButton] = {}
        for kind, text, tip in (
                ("prev_bad", PREV_CONFIRM_TEXT, "이 영역을 확정하고 앞쪽에서 고칠 것·볼 것(추측한 아이콘 이름 포함)이 있는 영역으로"),
                ("prev", "◀ 이전 영역", "목록 순서로 앞 영역으로 (Alt+← 또는 Alt+↑, 첫 영역에서는 마지막 영역으로)"),
                ("next", "다음 영역 ▶", "목록 순서로 다음 영역으로 (Alt+→ 또는 Alt+↓, 마지막 영역에서는 첫 영역으로)"),
                ("next_card", "다음 카드 ▶▶", "다음 카드의 첫 문제 영역으로")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setAutoDefault(False)
            b.clicked.connect(self._confirm_prev if kind == "prev_bad" else (lambda _c=False, k=kind: self._go(k)))
            self.nav_btns[kind] = b
        self.confirm_btn = QPushButton(NEXT_CONFIRM_TEXT)
        self.confirm_btn.setStyleSheet(PRIMARY_QSS)
        self.confirm_btn.setAutoDefault(False)
        self.confirm_btn.setToolTip("확정 전 아이콘 이름을 원문에 넣고, 원문을 ‘사람이 확인’으로 저장합니다 (Ctrl+Enter)")
        self.confirm_btn.clicked.connect(self._confirm_next)
        for b in (*self.nav_btns.values(), self.confirm_btn, self.close_btn, self.reread_btn, self.keep_btn):
            b.setMinimumWidth(b.sizeHint().width())

    def _build_late_widgets(self, header: QHBoxLayout) -> None:
        """맨 나중에 만드는 단추(버튼 코드가 밀리지 않게)·기본 단추 해제·단축키."""
        # 카드 단위로 영역 고치기 (영역 지정 창을 이 카드로 연다). 버튼 코드가 밀리지 않게 맨 나중에 만든다
        self.card_regions_btn = QPushButton("이 카드 영역 고치기")
        self.card_regions_btn.setAutoDefault(False)
        self.card_regions_btn.setToolTip("영역 지정 창을 이 카드로 엽니다. ‘이 카드만 따로 영역’으로 이 카드의 영역 상자만 "
                                         "옮기거나 크기를 바꿀 수 있습니다.\n상자가 바뀐 카드는 아이콘을 다시 찾습니다 "
                                         "(글은 ‘이 영역 다시 읽기’로 다시 읽으세요).")
        self.card_regions_btn.clicked.connect(self._edit_card_regions)
        header.insertWidget(header.indexOf(self.close_btn), self.card_regions_btn)
        # 이전·다음 문제 카드 (고칠 것·볼 것 영역이 있는 카드의 첫 문제 영역). 버튼 코드가 밀리지 않게 맨 뒤(2B-23·24)
        row = QHBoxLayout()
        row.setSpacing(4)
        for kind, text, tip in (("prev_card_bad", "◀◀ 문제 카드", "이전 문제 카드: 앞쪽에서 고칠 것·볼 것 영역이 있는 "
                                                              "가장 가까운 카드의 첫 문제 영역으로 (Alt+Shift+←)"),
                                ("next_card_bad", "문제 카드 ▶▶", "다음 문제 카드: 뒤쪽에서 고칠 것·볼 것 영역이 있는 "
                                                              "가장 가까운 카드의 첫 문제 영역으로 (Alt+Shift+→)")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setAutoDefault(False)
            b.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)  # 왼쪽 칸(LEFT_W)을 넓히지 않게
            b.setMinimumWidth(0)
            b.clicked.connect(lambda _c=False, k=kind: self._go(k))
            self.nav_btns[kind] = b
            row.addWidget(b)
        self._left.insertLayout(self._left.indexOf(self.nav_btns["next_card"]), row)
        for b in (self.save_btn, self.add_btn, self.del_btn, self.merge_btn, self.restore_btn, self.sentence_btn,
                  self.absorb_btn, self.overlap_btn, self.zoom_in_btn, self.zoom_out_btn, self.fit_btn):
            b.setAutoDefault(False)
        for keys, fn in (("Ctrl+Return", self._confirm_next), ("Ctrl+Enter", self._confirm_next),
                         ("Ctrl+S", self._save_shortcut),
                         ("Alt+Down", lambda: self._go("next")), ("Alt+Up", lambda: self._go("prev")),
                         ("Alt+Right", lambda: self._go("next")), ("Alt+Left", lambda: self._go("prev")),
                         ("Alt+Shift+Right", lambda: self._go("next_card_bad")),
                         ("Alt+Shift+Left", lambda: self._go("prev_card_bad"))):
            QShortcut(QKeySequence(keys), self, activated=fn)

    def resizeEvent(self, e):  # noqa: N802 - Qt 이름
        super().resizeEvent(e)
        if hasattr(self, "icon_list"):
            self._fit_icon_strip()

    def eventFilter(self, obj, ev):  # noqa: N802 - Qt 이름
        if obj is getattr(self, "icon_list", None) and ev.type() == QEvent.Type.Resize                 and ev.size().width() != ev.oldSize().width():
            QTimer.singleShot(0, self, self._fit_icon_strip)  # 크기를 바꾸는 신호 안에서 다시 크기를 바꾸지 않게
        return super().eventFilter(obj, ev)

    def _fit_icon_strip(self) -> None:
        """아이콘 띠 높이: 아이콘 수와 상관없이 아이콘 4칸이 보이는 높이 (한 줄에 4칸이 안 들어가면 줄을 늘림, 2줄까지).
        더 많으면 띠 안에서 스크롤, 적으면 빈칸 (영역마다 띠 높이가 바뀌어 그림이 움직이지 않게). 남는 높이는 그림에 준다."""
        per = max(1, (self.icon_list.viewport().width() - 4) // (ICON_CARD_W + 4))
        # 좁아서 한 줄에 1칸이면 4줄이 되어 영역 그림보다 커진다: 2줄까지만 (나머지는 띠 안에서 스크롤)
        rows = min(-(-ICON_STRIP_CARDS // per), 2)
        row_h = max([ICON_ROW_H] + [self.icon_list.item(i).sizeHint().height() for i in range(self.icon_list.count())])
        self.icon_list.setFixedHeight(rows * (row_h + 4) + 8)

    def _save_shortcut(self) -> None:
        """Ctrl+S = ‘이 자리로 저장’ (버튼을 누를 수 있을 때만: 새 자리를 그리지 않았으면 아무것도 하지 않는다)."""
        if self.save_btn.isEnabled() and self.save_btn.isVisible():
            self._save_box()

    # ------------------------------------------------------------ 데이터
    def _fetch(self) -> None:
        self._refresh_pending = False
        try:
            self.cards = list(self.rows_fn() or [])
        except Exception as e:  # noqa: BLE001
            # 뒤에서 다시 읽는 스레드가 추출 결과 파일을 바꿔 쓰는 순간에 읽으면 잠깐 실패할 수 있다: 목록을 비우면 ‘보여 줄
            # 영역이 없습니다’가 떠 창을 다시 열어야 했다 (8쪽). 예전 목록을 그대로 두고 조금 뒤 다시 읽는다
            import logging

            logging.getLogger("cardtranslater.ui").warning("카드 보기 목록을 읽지 못함 (예전 목록 유지)", exc_info=True)
            if not self.cards:
                self._flash(f"목록을 읽지 못했습니다: {e}", "warn")
            QTimer.singleShot(1500, self, self._retry_fetch)
            return
        self.flat = [(c, r) for c in self.cards for r in c["regions"]]
        self._by_key = {_nk(r["key"]): (c, r) for c, r in self.flat}
        try:
            self._reading = {_nk(k) for k in (self.reading_fn() if self.reading_fn else set())}
        except Exception:  # noqa: BLE001
            self._reading = set()

    def _retry_fetch(self) -> None:
        """목록을 잠깐 못 읽었을 때 조금 뒤 다시 (사람이 조작 중이면 쉴 때)."""
        try:
            if not self.isVisible():
                return
            if self._idle():
                self.refresh()
            else:
                QTimer.singleShot(1500, self, self._retry_fetch)
        except RuntimeError:  # 창이 이미 지워짐
            pass

    @staticmethod
    def is_problem(r: dict) -> bool:
        # 확정 전만으로는 문제가 아니다. 추측한 아이콘 이름은 region_status가 ‘볼 것’으로 만든다 (원문 확인 목록과 같은 기준)
        return r["status"] in ("fix", "check")

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
            self.progress_label.setText("")
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
        if self._shown_key is None or _nk(key) != self._shown_key:  # 다른 영역: 그림은 화면 맞춤으로 연다
            self._shown_key = _nk(key)
            self.view.fit()
        fd, page = card["file_dir"], card["page"]
        self.setWindowTitle(f"카드 보기 — {file_label(card['file_key'])} · {page}쪽")
        self.title.setText(f"{file_label(card['file_key'])} · {page}쪽")
        self._known = self._known_names(fd)
        self._fill_header(reg)
        self._fill_progress(card)
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

    def _fill_progress(self, card: dict) -> None:
        """머리의 ‘이 카드 1 / 3 영역 확인’ (확인됨·원본 그대로인 영역 / 이 카드의 영역)."""
        regs = card["regions"]
        done = sum(1 for r in regs if r["status"] in ("ok", "keep"))
        self.progress_label.setText(f"이 카드 {done} / {len(regs)} 영역 확인")

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
        self._fit_region_list()

    def _fit_region_list(self) -> None:
        """영역 목록 높이를 영역 수에 맞춘다 (2~8줄, 넘으면 스크롤). 남는 높이는 카드 전체 그림이 쓴다."""
        lst = self.region_list
        row = lst.sizeHintForRow(0) if lst.count() else 0
        row = max(row, lst.fontMetrics().height() + 8)
        n = min(max(lst.count(), 2), 8)
        lst.setFixedHeight(n * row + 2 * lst.frameWidth() + 4)

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
            row = IconRow({**e, "where": self._where_parts(e)}, self._known)
            row.edited.connect(self._name_edited)
            row.focused.connect(self._row_focused)
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, e["occ"])
            row.setFixedWidth(ICON_CARD_W)  # 띠에 가로로 놓으므로 칸 폭을 같게 (긴 상태 글은 줄을 바꾼다)
            h = row.heightForWidth(ICON_CARD_W) if row.hasHeightForWidth() else row.sizeHint().height()
            it.setSizeHint(QSize(ICON_CARD_W, max(ICON_ROW_H, h)))
            self.icon_list.addItem(it)
            self.icon_list.setItemWidget(it, row)
            if e["occ"] == self._cur_occ:
                it.setSelected(True)
                self.icon_list.setCurrentItem(it)
        self.icon_list.blockSignals(False)
        self._mark_rows()
        self._fit_icon_strip()

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
            # 확대해 둔 채 다른 아이콘을 고르면 그 아이콘(지금 자리, 없으면 예상 위치)이 보이게 옮긴다
            self.view.center_on(self._place.get("box") or self._place.get("guess"))
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
        src, removed = self._icon_ref(c)
        known = self._names_for(c)
        # 같은 방식으로 다시 읽으면 처음 결과는 숨기므로(service.extracted_cells) 이름은 ‘보통 읽기’·‘정밀 읽기’ 그대로
        filled = [(reader_name(rd["model"]), fill_icon_names(as_text(rd["source"]), src, known, removed))
                  for rd in c["readings"]]
        # 읽은 결과 표: 아이콘 그림에 든 옆 숫자(‘5 [[?]]’의 5)는 이름을 정하기 전에도 뺀다 (보이기만).
        # ‘이것 쓰기’는 편집 칸(저장되는 글)에 들어가므로 이름 정한 자리의 숫자만 뺀다 (확정 때 빼는 숫자와 같게)
        texts = [(n, service.strip_icon_numbers(t, c)) for n, t in filled]
        picks = [(n, service.strip_icon_numbers(t, c, named_only=True)) for n, t in filled]
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
        self._fill_picks(picks)
        base = source_text.for_edit(c, service.preview_source(c))
        self._orig_text = base
        self.edit.setPlainText(base if prefill is None else prefill)
        keep = bool(c.get("keep_original") or c.get("keep_mode"))
        self.keep_btn.setChecked(keep)
        self.keep_btn.setText(KEEP_UNDO_LABEL if keep else KEEP_LABEL)

    @staticmethod
    def _strip_boxed(text: str, c: dict) -> str:
        """다시 읽은 글에서 아이콘 그림에 든 옆 숫자를 뺀다 (확정할 때 원문에서 빼는 숫자: 아이콘의 strip).
        그대로 원문 칸에 넣으면 확정 때 숫자가 원문과 아이콘에 두 번 들어갔다."""
        import re

        for e in c.get("icons") or []:
            st, name = e.get("strip"), e.get("name")
            if not st or not name or not st.get("text"):
                continue
            num, tok = re.escape(str(st["text"])), re.escape(f"[[{name}]]")
            pat = rf"{num}[ 　]*(?={tok})" if st.get("side", "before") == "before" else rf"(?<={tok})[ 　]*{num}"
            text = re.sub(pat, "", text, count=1)
        return text

    def _names_for(self, c: dict) -> set:
        """읽은 결과에 붙일 수 있는 아이콘 이름: 사전 이름 + 이 영역에 정해 둔 확정 전 이름."""
        return set(self._known) | {e["name"] for e in c.get("icons") or [] if e.get("name")}

    def _icon_ref(self, c: dict) -> tuple[str, bool | None]:
        """읽은 결과의 [[?]]를 맞출 기준 원문과 ‘뺀 아이콘이 있나’. 확정 전 이름·합치기·아이콘 아님을 넣은 미리 보기를 쓴다
        (원문 그대로 쓰면 확정 전에는 이름이 안 붙고, 넷을 하나로 합쳐도 [[?]]가 넷으로 보였다: 1쪽)."""
        raw = as_text(c.get("source"))
        try:
            ref = service.preview_source(c)
        except Exception:  # noqa: BLE001 - 미리 보기를 못 만들면 원문 그대로
            ref = raw
        removed = c.get("icon_removed")
        removed = None if removed is None else bool(removed)
        if len(ICON_TOKEN_RE.findall(ref)) < len(ICON_TOKEN_RE.findall(raw)):
            removed = True  # 확정 전 합치기·아이콘 아님으로 줄어든 자리
        return ref, removed

    def _fill_picks(self, texts: list[tuple[str, str]]) -> None:
        for b in self.pick_buttons:
            self.readings.grid.removeWidget(b)
            b.hide()
            b.deleteLater()
        self.pick_buttons = []
        uniq: list[tuple[int, str, str]] = []  # (읽은 결과 표의 줄, 이름, 글): 같은 글은 처음 줄에만 단추
        for row, (name, t) in enumerate(texts):
            if t not in [x for _r, _n, x in uniq]:
                uniq.append((row, name, t))
        self.pick_label.setVisible(len(uniq) >= 2)
        if len(uniq) < 2:
            return
        for row, name, t in uniq:
            b = QPushButton(f"{name} 결과")
            b.setProperty(ui_codes.LATE, True)  # 버튼 코드는 고정 버튼 뒤에 (있다 없다 해도 다른 버튼 번호가 밀리지 않게)
            b.setAutoDefault(False)
            b.setToolTip(t)
            b.clicked.connect(lambda _c=False, tt=t: self._use_reading(tt))
            # 그 줄의 왼쪽 칸(‘보통 읽기’ 이름 아래)에 놓는다: 오른쪽에 열을 더 두면 좁은 오른쪽 칸에서 표가 잘렸다
            b.setStyleSheet("margin: 0 6px 6px 6px;")
            h = self.readings._rows[row][0]
            h.setMinimumHeight(h.sizeHint().height() + b.sizeHint().height() + 4)
            self.readings.grid.addWidget(b, row, 0, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter)
            self.pick_buttons.append(b)

    def _insert_paragraph_mark(self) -> None:
        """커서가 있는 줄 앞에 문단 나눔 줄 [====]를 넣는다 (문단마다 다른 글꼴 영역만)."""
        reg = self._region()
        if reg is None or not reg.get("paragraph_mode"):
            return
        cur = self.edit.textCursor()
        cur.movePosition(cur.MoveOperation.StartOfBlock)
        cur.insertText(service.PARAGRAPH_MARK + "\n")
        self.edit.setTextCursor(cur)
        self.edit.setFocus()

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
        para = reg is not None and bool(reg.get("paragraph_mode")) and isinstance(reg.get("source"), str)
        self.para_btn.setEnabled(ok and para)
        self.para_btn.setToolTip(
            "커서가 있는 줄 앞에 문단 나눔 줄 [====]를 넣습니다. 나눈 문단마다 한글 글꼴·서식을 따로 정합니다 (지우려면 그 줄을 지움)."
            if para else "이 영역은 ‘문단마다 다른 글꼴’이 아니라 문단 나눔을 쓰지 않습니다.\n"
                         "‘영역 지정’에서 이 영역의 글꼴 단위를 ‘문단마다 다른 글꼴’로 바꾸면 쓸 수 있습니다.")
        self.edit.setReadOnly(reading)
        self.confirm_btn.setToolTip("뒤에서 다시 읽는 중인 영역입니다" if reading else
                                    "확정 전 아이콘 이름을 원문에 넣고, 원문을 ‘사람이 확인’으로 저장합니다 (Ctrl+Enter)")
        self._icon_tools()
        for kind in self.nav_btns:
            if kind != "prev_bad":
                self.nav_btns[kind].setEnabled(self._target(kind) is not None)
        # 확정 버튼 둘(◀◀ 확정 → 이전 문제 · 이 영역 확정 → 다음 문제): 이 영역의 아이콘이 모두 끝나야 켜진다.
        # 이전·다음 문제 영역이 없으면 ‘이 영역 확정’ (확정하고 이 영역에 머문다)
        blocking = reg is not None and any(e.get("blocking") for e in reg.get("icons") or [])
        can = ok and not blocking
        has_prev = reg is not None and self._neighbor(self.key, -1, True) is not None
        has_next = reg is not None and self._neighbor(self.key, +1, True, wrap=True) is not None
        pb = self.nav_btns["prev_bad"]
        pb.setText(PREV_CONFIRM_TEXT if has_prev else CONFIRM_ONLY_TEXT)
        pb.setEnabled(can)
        self.confirm_btn.setText(NEXT_CONFIRM_TEXT if has_next else CONFIRM_ONLY_TEXT)
        self.confirm_btn.setEnabled(can)
        # 앞·뒤 문제가 모두 없으면 두 단추가 모두 ‘이 영역 확정’이 된다: 같은 일을 하는 아래 단추는 숨긴다
        pb.setVisible(pb.text() != self.confirm_btn.text())
        if blocking and ok:
            why = "이 영역에 이름 없는·위치 못 찾은 아이콘이 있습니다. 아이콘을 모두 정한 뒤 확정하세요"
            pb.setToolTip(why)
            self.confirm_btn.setToolTip(why)
        else:
            pb.setToolTip("이 영역을 확정하고 앞쪽에서 고칠 것·볼 것(추측한 아이콘 이름 포함)이 있는 영역으로")

    def _icon_tools(self) -> None:
        self.mag_where.setText(self._where(self._cur_entry()) or ("아이콘이 없습니다" if not self._entries else ""))
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
        if self.view.new is not None and self.save_btn.isEnabled():
            self._flash("새 자리를 잡았습니다. ‘이 자리로 저장’(Ctrl+S)을 누르면 저장됩니다"
                        + (" (원문에 빠진 아이콘이면 ‘아이콘 추가’)" if self.add_btn.isEnabled() else ""))

    def _pick_icon(self, occ: str) -> None:
        """그림에서 다른 아이콘 상자를 눌렀다: 목록에서 그 아이콘을 고른다 (그 상자의 테두리를 끌어 크기를 바꿀 수 있게)."""
        for i in range(self.icon_list.count()):
            it = self.icon_list.item(i)
            if it.data(Qt.ItemDataRole.UserRole) == occ:
                self.icon_list.clearSelection()
                it.setSelected(True)
                self.icon_list.setCurrentItem(it)
                self.icon_list.scrollToItem(it)
                return

    def _update_mag(self) -> None:
        pm = self.view.focus_crop(self.mag.size() - QSize(8, 8))
        if pm.isNull():
            self.mag.setPixmap(QPixmap())
            self.mag.setText("아이콘을 고르면\n크게 보입니다")
        else:
            self.mag.setText("")
            self.mag.setPixmap(pm)

    def _zoomed(self, z: float) -> None:
        self.zoom_label.setText(f"{round(z * 100)}%")

    def _guard(self) -> bool:
        """원문 칸을 고쳐 둔 채 아이콘을 바꾸면 원문 칸을 새로 채우므로 먼저 묻는다."""
        if not self._dirty():
            return True
        r = ask_choice(self, "고친 원문이 있습니다", "아이콘을 바꾸면 원문 칸을 새로 채웁니다. 고친 원문은 사라집니다.",
                       [("계속", "destructive"), ("취소", "reject")], default=1)
        return r == 0

    def _name_edited(self, occ: str, name: str, ko: str) -> None:
        """이름·한국어 이름을 적었다: ‘확정 전’으로 적어 둔다 (원문은 확정할 때 바뀜). 같은 모양 모두 적용이 켜져 있으면 모두.
        자리를 먼저 저장할 때 묻는 창이 뜨면 입력 칸이 포커스를 잃어 같은 신호가 또 올 수 있다: 한 번만 처리한다."""
        if getattr(self, "_naming", False):
            return
        self._naming = True
        try:
            self._name_edited_now(occ, name, ko)
        finally:
            self._naming = False

    def _name_edited_now(self, occ: str, name: str, ko: str) -> None:
        e = next((x for x in self._entries if x["occ"] == occ), None)
        if e is None or self._loading or self._rereading:
            return
        if name == e["name"] and (not ko or ko == e["ko"]):
            return
        if not self._guard():
            self._schedule_reload()  # 적던 값을 되돌린다
            return
        notes = []
        if name and self.view.new and self._cur_entry() is not None and self._cur_entry()["occ"] == occ:
            # 이 아이콘의 자리를 새로 잡고 저장하지 않은 채 이름을 적었다: 다시 그리면 잡은 상자가 사라졌다. 자리를 먼저 저장
            stored = self._store_box(self.view.new, e)
            if stored is None:  # 취소했거나 저장하지 못함: 상자와 적은 이름을 그대로 둔다 (다시 Enter로 적는다)
                return
            saved_occ, box_msg = stored
            notes.append("자리도 저장함")
            if box_msg:
                notes.append(box_msg)
            self._fetch()
            e = next((x for _c, r in self.flat for x in r.get("icons") or [] if x["occ"] == occ), e)
            if saved_occ != occ:  # 상자는 다른 [[?]]의 자리로 저장했다: 이름은 적은 줄(occ)에 그대로
                self._cur_occ = occ
        targets = [e]
        if self.same_shape.isChecked() and e.get("shape"):
            for _c, r in self.flat:
                for x in r.get("icons") or []:
                    if x["occ"] != occ and x.get("shape") == e["shape"] and x["state"] in ("unnamed", "named") \
                            and (x["state"] == "unnamed" or x["name"] == e["name"]):
                        targets.append(x)
        if name:
            try:
                rn = service.resolve_icon_name(self.project_dir, e["group"], name,
                                                   exclude_groups=[x["group"] for x in targets])
            except Exception:  # noqa: BLE001 - 견주지 못하면 적은 이름 그대로
                rn = {"name": name, "renamed": False, "same_as": ""}
            if rn["renamed"]:
                notes.append(f"‘{name}’은(는) 그림이 다른 아이콘의 이름이라 ‘{rn['name']}’(으)로 적음")
            elif rn["same_as"] and rn["same_as"] != name:
                notes.append(f"같은 아이콘의 이름 ‘{rn['same_as']}’(으)로 적음")
            name = rn["name"]
        rows = []
        for x in targets:
            if not name:
                rows.append({"group": x["group"], "clear": True})
            else:
                v = {"group": x["group"], "name": name}
                if ko:
                    v["ko"] = ko
                elif name not in self._known:
                    v["ko"] = ""  # 새 이름: 예전 이름의 한국어 이름을 끌고 가지 않는다 (카드 보기에는 한국어 이름 칸이 없다)
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
        warn = any("다른 아이콘" in x for x in notes)
        self._flash(f"이름을 적었습니다 (확정 전)" + (f" · 같은 모양 {n}개에도" if n else "")
                    + "".join(f" · {x}" for x in notes), "warn" if warn else "info")

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

    def background_done(self, cells: list[dict]) -> None:
        """뒤에서 다시 읽기가 끝났다 (목록 창이 부른다). 지금 카드의 영역이면 다시 그리되, 사람이 상자를 그리거나 이름을
        적는 중이면 끝날 때까지 미룬다 (그린 상자·적던 이름이 사라지지 않게). 다른 카드면 ‘읽는 중’ 표시만 고친다."""
        card = self._card()
        mine = card is not None and any(_nk((c["file_dir"], c["page"])) == _nk(card["key"]) for c in cells)
        if not mine:
            try:
                self._reading = {_nk(k) for k in (self.reading_fn() if self.reading_fn else set())}
            except Exception:  # noqa: BLE001
                pass
            self._update_buttons()
            return
        self._refresh_pending = True
        self._refresh_when_idle()

    def _idle(self) -> bool:
        from PySide6.QtWidgets import QApplication, QLineEdit

        fw = QApplication.focusWidget()
        # 이름 칸에 커서만 있는 것은 적는 중이 아니다 (Enter 뒤에도 커서가 남아 다시 그리기가 끝없이 미뤄졌다)
        typing = isinstance(fw, QLineEdit) and self.icon_list.isAncestorOf(fw) and fw.isModified()
        editing = self.edit.hasFocus() and self._dirty()  # 원문 칸을 고치는 중 (다시 그리면 커서가 맨 앞으로 간다)
        return not (self._busy_depth or self._rereading or self._loading or self.view.new is not None
                    or self.view.drawing() or typing or editing)

    def _refresh_when_idle(self) -> None:
        try:
            if not self._refresh_pending or not self.isVisible():
                return
            if self._idle():
                self.refresh()
                self._flash("뒤에서 다시 읽기가 끝나 읽은 결과를 새로 보입니다")
            else:
                QTimer.singleShot(1500, self, self._refresh_when_idle)
        except RuntimeError:  # 창이 지워짐
            return

    def refresh(self) -> None:
        """목록 창이 바뀐 뒤 (뒤에서 다시 읽기 끝남 등) 다시 그린다."""
        self._card_cache.clear()
        self._reload()

    def _after_op(self, occ: str | None = None, msg: str = "", advance: str | None = None) -> None:
        """조작 뒤 다시 그린다. advance(조작 전 고른 자리 id)를 주면 다음 문제 아이콘으로 옮기고, 남은 문제가 없으면
        이 영역을 보통·정밀 읽기로 다시 읽는다 (이 자리로 저장·아이콘 추가·삭제·합치기 뒤)."""
        if self.key is not None:  # 아이콘을 바꿨다: 앞서 다시 읽은 결과는 지금 아이콘 자리와 맞지 않을 수 있다
            self._fresh_reread.discard(_nk(self.key))
        self.changed.emit()
        self._fetch()
        self._show_region(self.key, keep_occ=occ or self._cur_occ)
        if msg:
            self._flash(msg)
        if advance is not None:
            self._advance_icons(advance)

    def _advance_icons(self, from_occ: str) -> None:
        """다음 문제(이름 없음·위치 못 찾음) 아이콘으로: 목록에서 from_occ 뒤의 것 → 없으면 첫 문제 아이콘.
        문제가 하나도 없으면 새 아이콘 자리로 보통·정밀 읽기를 다시 해 ‘이것 쓰기’·원문 칸을 고친다."""
        probs = [e for e in self._entries if e.get("blocking")]
        if probs:
            order = [e["occ"] for e in self._entries]
            i = order.index(from_occ) if from_occ in order else -1
            nxt = next((e for e in self._entries[i + 1:] if e.get("blocking")), None) or probs[0]
            self._pick_icon(nxt["occ"])
            return
        reg = self._region()
        if reg is not None and self._entries and reg.get("kind") == "text" and isinstance(reg.get("source"), str):
            self._reread(double=True)

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
        was = self._cur_occ or sel[0]["occ"]
        try:
            ids = self._open_entries(sel)
            for e in sel:
                service.set_icon_not_icon(self.project_dir, ids[e["occ"]])
        except Exception as ex:  # noqa: BLE001
            show_error(self, "‘아이콘 아님’으로 정하지 못했습니다", ex)
            self._after_op()  # 새로 그리기만 (다음 아이콘으로 옮기거나 다시 읽지 않는다)
            return
        self._after_op(msg="‘아이콘 아님’으로 했습니다 (확정하면 원문에서 빠집니다)", advance=was)

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
        self._after_op(host, "아이콘을 합쳤습니다", advance=host)

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

    def _where(self, e: dict | None) -> str:
        """아이콘이 원문 몇 번 줄의 어느 [[…]]인가: ‘3번 줄 · ■を得る’ (이 아이콘 = ■, 같은 줄의 다른 아이콘 = □)."""
        line_no, snippet = self._where_parts(e)
        return f"{line_no} · {snippet}" if line_no else ""

    def _where_parts(self, e: dict | None) -> tuple[str, str]:
        """(‘3번 줄’, ‘■を得る’). 모르면 ("", "")."""
        reg = self._region() or {}
        if not e or not isinstance(reg.get("source"), str):
            return "", ""
        lines = reg["source"].split("\n")
        li = int(e.get("line") or 0) - 1
        if not 0 <= li < len(lines):
            return "", ""
        before = sum(len(ICON_TOKEN_RE.findall(ln)) for ln in lines[:li])
        k = int(e.get("ti", -1)) - before
        n = iter(range(10_000))
        snippet = ICON_TOKEN_RE.sub(lambda _m: "■" if next(n) == k else "□", lines[li]).strip()
        if len(snippet) > 18:
            snippet = snippet[:17] + "…"
        return f"{li + 1}번 줄", snippet

    def _box_owner(self, e: dict, box) -> dict | None:
        """방금 잡은 상자가 고른 아이콘의 예상 자리 밖이고 다른 ‘위치 못 찾음·이름 없음’ 아이콘의 예상 자리 안이면 그 아이콘.
        (1쪽: 아래 아이콘 줄의 [[?]]를 고른 채 위 글 줄의 동전을 잡아 저장해, 동전이 아래 줄 자리로 들어갔다)"""
        if not self._place:
            return None
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2

        def inside(b) -> bool:
            return bool(b) and b[0] <= cx <= b[2] and b[1] <= cy <= b[3]
        own = self._place.get("guess") or self._place.get("box")  # 예상 자리, 자리가 있던 아이콘은 지금 자리
        if own is not None and self._place.get("guess"):
            # 예상 자리는 짐작이다 (1.33.0부터 아이콘 크기 근처로 좁음): 조금 벗어난 진짜 자리를 옆 [[?]]의 것으로 묻지 않게
            # 가운데 기준 1.5배로 넓혀 본다 (다른 줄의 아이콘을 잡은 경우는 여전히 밖이다)
            mx, my = (own[2] - own[0]) * 0.25, (own[3] - own[1]) * 0.25
            own = (own[0] - mx, own[1] - my, own[2] + mx, own[3] + my)
        if own is None or inside(own):  # 제 자리·예상 자리 안이면 그대로 (자리를 조금 고쳐 잡음)
            return None
        by_occ = {x["occ"]: x for x in self._entries}
        cands = [m for m in self._place.get("marks") or []
                 if m.get("guess") and m.get("occ") and m["occ"] != e["occ"] and inside(m.get("box"))
                 and (by_occ.get(m["occ"]) or {}).get("state") in ("lost", "unnamed")]
        if not cands:
            return None
        best = min(cands, key=lambda m: abs((m["box"][0] + m["box"][2]) / 2 - cx) + abs((m["box"][1] + m["box"][3]) / 2 - cy))
        return by_occ[best["occ"]]

    @busy
    def _save_box(self) -> None:
        box, e = self.view.new, self._cur_entry()
        if not box or e is None or not self._guard():
            return
        stored = self._store_box(box, e)
        if stored is not None:
            occ, extra = stored
            msg = "자리를 저장했습니다. 확정하면 이 영역을 다시 읽습니다"
            if extra:
                msg = extra if extra.startswith("두 아이콘") else f"{msg} ({extra})"
            self._suggest_then(occ, msg)

    def _store_box(self, box, e: dict) -> tuple[str, str] | None:
        """잡은 상자를 아이콘 e의 자리로 저장한다 (다른 [[?]]의 예상 자리 안이면 어느 것인지 묻고, 상자 크기·붙은 아이콘·
        순서도 묻는다). 돌려줌 (저장한 자리 id, 덧붙일 알림 글) — 취소했거나 저장하지 못했으면 None.
        ‘이 자리로 저장’과 상자를 잡은 채 이름을 적었을 때(_name_edited)가 함께 쓴다."""
        other = self._box_owner(e, box)
        if other is not None:
            c = ask_choice(self, "어느 아이콘의 자리인가요",
                           f"방금 잡은 자리는 고른 아이콘({self._where(e)})의 예상 자리 밖이고,\n"
                           f"원문의 다른 아이콘({self._where(other)})의 예상 자리 안에 있습니다.\n"
                           "어느 아이콘의 자리로 저장할까요?",
                           [("그 아이콘에 저장", "accept"), ("고른 아이콘에 저장", "destructive"), ("취소", "reject")],
                           default=0,
                           informative="다른 [[?]]에 저장하면 한글 넣기에서 아이콘이 원문의 다른 줄·자리에 들어갑니다.")
            if c == 0:
                e = other  # 고른 줄(_cur_occ)은 저장에 성공한 뒤에 바꾼다 (실패하면 목록과 어긋나지 않게)
            elif c != 1:
                return None
        try:
            prob = service.icon_box_problem(self.project_dir, e["occ"], box)
        except Exception:  # noqa: BLE001 - 검사는 없어도 된다
            prob = ""
        if prob and ask_choice(self, "아이콘 상자 확인", prob,
                               [("다시 잡기", "reject"), ("그래도 아이콘 하나로 저장", "accept")], default=0,
                               informative="큰 아이콘은 모든 글자의 왼쪽·오른쪽에 있거나 한 줄을 아이콘이 다 차지할 때만 있습니다.") != 1:
            return None
        try:
            r = service.set_icon_box(self.project_dir, e["occ"], box)
        except ValueError as ex:
            show_problem(self, "자리를 저장하지 못했습니다", str(ex), "아이콘을 조금 더 크게 감싸 주세요.")
            return None
        except Exception as ex:  # noqa: BLE001
            show_error(self, "자리를 저장하지 못했습니다", ex)
            return None
        self._cur_occ = e["occ"]
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
        extra = []
        if r.get("order_conflict"):
            swapped = self._ask_swap(e, r["order_conflict"][0])
            if swapped:
                extra.append(swapped)
        if r.get("boxed_number"):
            extra.append(f"상자 안의 숫자 ‘{r['boxed_number']}’는 아이콘 그림이라 원문에서 뺍니다")
        return e["occ"], " · ".join(extra)

    def _ask_swap(self, e: dict, other: str) -> str:
        """방금 잡은 상자가 원문 순서와 어긋난다 (원문에서 앞 아이콘인데 상자가 뒤 줄·오른쪽): 두 자리를 바꿀지 묻는다.
        바꿨으면 알림 글, 아니면 ""."""
        x = next((y for y in self._entries if y["occ"] == other), None)
        nm = lambda y: f"[[{y['name']}]]" if y and y.get("name") else "다른 아이콘"  # noqa: E731
        c = ask_choice(self, "아이콘 순서 확인",
                       f"방금 잡은 자리와 {nm(x)}의 자리가 원문 순서와 반대입니다.\n"
                       "원문에서 앞(위 줄·왼쪽)에 있는 아이콘의 상자가 뒤쪽 아이콘의 상자보다 아래(오른쪽)에 있습니다.\n"
                       "두 아이콘의 자리(그림)를 서로 바꿀까요?",
                       [("자리 바꾸기", "accept"), ("그대로 두기", "reject")], default=0,
                       informative="바꾸지 않으면 한글 넣기에서 아이콘 그림이 서로 엇갈려 들어갈 수 있습니다. "
                                   "확정 전 이름도 그림을 따라 바뀝니다.")
        if c != 0:
            return ""
        try:
            service.swap_icon_boxes(self.project_dir, e["occ"], other)
        except Exception as ex:  # noqa: BLE001
            show_error(self, "자리를 바꾸지 못했습니다", ex)
            return ""
        return "두 아이콘의 자리를 원문 순서대로 바꿨습니다. 이름이 그림과 맞는지 확인하세요"

    def _suggest_then(self, occ: str, msg: str) -> None:
        """손으로 잡은 자리에 이름이 없으면 Claude에게 이름·한국어 이름을 제안 받아 이름 칸에 채운 뒤 다시 그린다
        (사전과 모양이 같아 이름이 정해졌거나 연결할 수 없으면 바로). 제안은 ‘확정 전’ 값이 아니라 줄의 제안으로 남는다."""
        def after(note: str = ""):
            try:
                self._after_op(occ, msg + (f" — {note}" if note else ""), advance=occ)
            except RuntimeError:  # 제안을 받는 동안 창이 지워짐
                return
        # 자리를 저장한 뒤의 줄(새 줄로 옮겨졌을 수 있다)의 이름으로 정한다: 이름이 있으면(사전·확정·적어 둔 값) 묻지 않는다
        self._fetch()
        reg = self._region() or {}
        entry = next((x for x in reg.get("icons") or [] if x["occ"] == occ), None)
        group = entry["group"] if entry else None
        if entry is None or entry.get("name") or entry["state"] in ("not_icon", "absorbed"):
            after()
            return
        eng = self.engine or service.get_settings().engine
        if not service.engine_status().get(eng):
            after("이름을 적으세요 (Claude에 연결할 수 없어 이름 제안 없음)")
            return

        def done(res):
            res = res or {}
            after(f"이름 제안: {res['suggest']}" if res.get("suggest") else (res.get("note") or "이름을 적으세요"))
        job = worker.Job(service.suggest_icon_name, self.project_dir, group, engine=eng, with_progress=False)
        worker.run_with_dialog(self, "아이콘 이름 제안 받는 중", job, done, lambda _m: after("이름을 적으세요"),
                               cancellable=False)

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
        self._suggest_then(r["occ"], f"원문 {r['line']}줄에 아이콘을 넣었습니다")

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

    def _freeze_buttons(self) -> Callable[[], None]:
        """영역 지정 창을 띄운 동안 뒤에 보이는 카드 보기·원문 확인 목록의 버튼·입력 칸을 흐리게 (누를 수 없음을 보인다:
        영역 지정 창이 모달이라 눌러도 동작하지 않는데 눌리는 것처럼 보였다). 돌려준 함수를 부르면 원래대로."""
        from PySide6.QtWidgets import QAbstractButton, QComboBox, QLineEdit

        wins = [self] + ([self.parentWidget().window()] if self.parentWidget() is not None else [])
        frozen = []
        for w in wins:
            for cls in (QAbstractButton, QComboBox, QLineEdit):
                for x in w.findChildren(cls):
                    if x.isEnabled() and x.window() is w:  # 다른 창(영역 지정 창 등) 것은 건드리지 않는다
                        x.setEnabled(False)
                        frozen.append(x)

        def thaw() -> None:
            for x in frozen:
                try:
                    x.setEnabled(True)
                except RuntimeError:  # 그 사이 지워진 위젯
                    pass
            self._update_buttons()  # 버튼마다 할 수 있는지 다시 정한다
            owner = self.parentWidget().window() if self.parentWidget() is not None else None
            if owner is not None and hasattr(owner, "_update_chips"):  # 원문 확인 목록도 (그 사이 뒤 작업이 바꿨을 수 있다)
                try:
                    owner._update_chips()
                except Exception:  # noqa: BLE001
                    pass
        return thaw

    def _edit_card_regions(self) -> None:
        """이 카드 영역 고치기: 영역 지정 창(그 카드의 그룹)을 이 카드로 연다. 닫은 뒤 영역 상자가 바뀐 카드는 아이콘을
        다시 찾고(아이콘 자리는 영역 그림 기준), 카드 그림·영역 목록을 새로 그린다."""
        def reading_now(c) -> bool:
            if any(self._eff_status(r) == "reading" for r in c["regions"]):
                self._flash("이 카드는 뒤에서 다시 읽는 중입니다. 다 읽은 뒤에 영역을 고치세요", "warn")
                return True
            return False

        card = self._card()
        if card is None or self._rereading or reading_now(card):
            return
        if not self._leave_ok():
            return
        # 고친 원문을 ‘확정’하고 왔으면 그 영역이 뒤에서 다시 읽기에 들어갔을 수 있다: 읽는 동안 상자를 바꾸면 안 된다
        self._fetch()
        card = self._card()
        if card is None or reading_now(card):
            return
        from app.qt.region_editor import RegionEditor

        fd, page = Path(card["file_dir"]), int(card["page"])
        gdir = fd.parent.parent  # <그룹>/files/<파일>
        try:
            groups = service.list_groups(gdir.parent.parent)
        except Exception:  # noqa: BLE001 - 그룹 목록을 못 읽으면 이 파일만
            groups = []
        g = next((x for x in groups if _nk((x["dir"],)) == _nk((gdir,))), None)
        files = [Path(g["dir"]) / "files" / f for f in g["files"]] if g else [fd]
        # 공통 영역을 바꾸면 그룹의 모든 카드가 바뀐다: 이 창에 보이는 그 그룹 카드를 모두 견준다
        mine = [c for c in self.cards if _nk((Path(c["file_dir"]).parent.parent,)) == _nk((gdir,))]

        def rects() -> dict:
            out = {}
            for f in {str(c["file_dir"]) for c in mine}:
                try:
                    pages = [int(c["page"]) for c in mine if str(c["file_dir"]) == f]
                    out.update({(f, p): v for p, v in service.card_region_rects(f, pages).items()})
                except Exception:  # noqa: BLE001 - 못 읽은 파일은 견주지 않는다
                    pass
            return out
        before = rects()
        ed = RegionEditor(gdir, files, self)
        thaw = self._freeze_buttons()
        try:
            ed.go_to(fd, page)
            ed.setWindowState(ed.windowState() | Qt.WindowState.WindowMaximized)
            ed.exec()
        finally:
            thaw()
            ed.deleteLater()  # 카드 보기의 자식으로 남아 그림·장면이 쌓이지 않게
        after = rects()
        changed = [k for k in after if after[k] != before.get(k)]
        self._card_cache.clear()
        msg = ""
        if changed:
            try:
                with_busy = QApplication.overrideCursor() is None
                if with_busy:
                    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
                try:
                    service.rescan_card_icons(self.project_dir, changed)
                finally:
                    if with_busy:
                        QApplication.restoreOverrideCursor()
                msg = (f"영역이 바뀐 카드 {len(changed)}장의 아이콘을 다시 찾았습니다. "
                       "바뀐 영역의 글은 ‘이 영역 다시 읽기’로 다시 읽으세요")
            except Exception as ex:  # noqa: BLE001
                show_error(self, "아이콘을 다시 찾지 못했습니다", ex)
            self.changed.emit()
        self._fetch()
        key = self.key
        if key is not None and _nk(key) not in self._by_key:  # 영역 이름이 바뀌었거나 빠짐: 같은 카드의 첫 영역
            c = next((c for c in self.cards if _nk(c["key"]) == _nk((fd, page))), None)
            key = c["regions"][0]["key"] if c and c["regions"] else None
        self._shown_key = None
        self._show_region(key)
        if msg:
            self._flash(msg, "warn")

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

    def _reread(self, double: bool = False) -> None:
        """이 영역 다시 읽기. double: 아이콘을 모두 정한 뒤 자동으로 — 보통·정밀 읽기 둘 다 (새 아이콘 자리로)."""
        reg = self._region()
        if reg is None or self._rereading or not self._guard():
            return
        eng = self.engine or service.get_settings().engine
        st = service.engine_status()
        if not st.get(eng):
            if double:
                self._flash("아이콘을 모두 정했습니다. Claude에 연결할 수 없어 다시 읽지 못했습니다 (확정하면 뒤에서 다시 읽습니다)",
                            "warn")
                return
            show_error(self, "Claude에 연결할 수 없습니다", st["message"],
                       "‘파일 > 로그인 방식’에서 로그인하거나 다른 방식을 고른 뒤 다시 하세요.")
            return
        key = reg["key"]
        self._fresh_reread.discard(_nk(key))  # 성공한 두 번 읽기 뒤에만 다시 넣는다 (_reread_done)
        if double:
            job = worker.Job(service.reread_region, reg["file_dir"], reg["page"], reg["region"], double=True,
                             engine=eng, with_progress=False)
            title = f"아이콘을 모두 정했습니다 — {reg['page']}쪽 {reg['region']} 보통·정밀 읽기로 다시 읽는 중"
        else:
            job = worker.Job(service.reread_region, reg["file_dir"], reg["page"], reg["region"],
                             model=self.reread_model.currentData(), engine=eng, with_progress=False)
            title = f"{reg['page']}쪽 {reg['region']} 다시 읽는 중"
        self._lock(True)
        worker.run_with_dialog(self, title, job, lambda r: self._reread_done(r, key, double), self._reread_failed,
                               cancellable=False, on_cancelled=lambda: self._lock(False))

    def _lock(self, on: bool) -> None:
        self._rereading = on
        try:
            self.body.setEnabled(not on)
        except RuntimeError:  # 창이 이미 지워짐
            pass

    def _reread_failed(self, msg: str) -> None:
        self._lock(False)
        _fail_as(self, "다시 읽지 못했습니다")(msg)

    def _reread_done(self, r, key: tuple, double: bool = False) -> None:
        try:
            self._lock(False)
            if r.state != "done":
                show_problem(self, f"다시 읽지 못했습니다 ({state_label(r.state)})", r.reason or "",
                             retry_after=r.retry_after)
                return
            if double:  # 확정할 때 또 읽지 않게 (원문 칸 글을 사람이 확인한 원문으로 저장한다: _confirm)
                self._fresh_reread.add(_nk(key))
            self.changed.emit()
            self._fetch()
            reg = self._by_key.get(_nk(key), (None, None))[1]
            if reg is None or self.key is None or _nk(self.key) != _nk(key):
                return
            ref, removed = self._icon_ref(reg)
            text = fill_icon_names(as_text(r.source), ref, self._names_for(reg), removed)
            current = service.preview_source(reg)
            human = bool(reg.get("set_members")) or any(
                e.get("manual") or e["state"] == "absorbed" or (e["state"] == "not_icon" and e.get("staged"))
                for e in reg.get("icons") or [])
            if source_text.same_but_icon_layout(text, current, human):
                # 글은 같고 아이콘 자리·합친 아이콘 수만 다르다: 카드 보기에서 정한 아이콘(자리·합치기)을 따른다
                self._show_region(key)
                n_r, n_c = text.count("[["), current.count("[[")
                self._flash("다시 읽은 글이 지금 원문과 같습니다. 아이콘 자리·합친 아이콘은 카드 보기에서 정한 대로 둡니다"
                            + (f" (다시 읽은 아이콘 {n_r}개 · 원문 {n_c}개)" if n_r != n_c else "")
                            + ". 다르게 쓰려면 위 ‘이것 쓰기’"
                            + (" (다시 읽은 결과도 확실하지 않다고 했습니다)" if r.uncertain else ""), "info")
                return
            text = self._strip_boxed(text, reg)
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
        if self._dirty() or _nk(reg["key"]) in self._fresh_reread:
            # 고친 원문, 또는 아이콘을 모두 정한 뒤 이미 보통·정밀 읽기로 다시 읽은 영역: 원문 칸 글을 사람이 확인한 원문으로
            # 저장한다 (확정 뒤 뒤에서 또 읽지 않게)
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
        if not self.confirm_btn.isEnabled():  # Ctrl+Enter도 버튼이 꺼져 있으면 하지 않는다
            return
        cur, order = self.key, [r["key"] for _c, r in self.flat]  # 확정 전 순서 (확정하면 목록에서 빠질 수 있다)
        if self._confirm() is None:
            return
        self._fetch()
        self._show_region(self._after_confirm(cur, order, +1, wrap=True))

    def _confirm_prev(self) -> None:
        """◀◀ 확정 → 이전 문제: 이 영역을 확정하고 앞쪽 문제 영역으로 (없으면 확정만 하고 머문다)."""
        cur, order = self.key, [r["key"] for _c, r in self.flat]
        if self._confirm() is None:
            return
        self._fetch()
        self._show_region(self._after_confirm(cur, order, -1, wrap=False))

    def _after_confirm(self, cur, order: list, step: int, wrap: bool):
        """확정한 뒤 갈 영역: 확정 전 순서(order)에서 cur 다음(step 방향)의 문제 영역 중 지금 목록에 있는 첫 영역.
        목록 창이 ‘문제만’이면 문제를 다 고친 카드는 목록에서 빠져 cur를 찾지 못했다: 그때 첫 카드의 첫 영역(3쪽 본문)으로
        가던 문제. 문제 영역이 없으면 cur (목록에서 빠졌으면 cur 다음의 아무 영역, 그것도 없으면 첫 영역)."""
        keys = [_nk(k) for k in order]
        i = keys.index(_nk(cur)) if cur is not None and _nk(cur) in keys else -1
        if i < 0:  # 예전 순서에도 없음: 지금 목록 기준
            return self._neighbor(cur, step, only_problem=True, wrap=wrap) or cur
        n = len(order)
        if step > 0:
            rng = list(range(i + 1, n)) + (list(range(0, i)) if wrap else [])
        else:
            rng = list(range(i - 1, -1, -1)) + (list(range(n - 1, i, -1)) if wrap else [])
        for j in rng:
            hit = self._by_key.get(keys[j])
            if hit is not None and self.is_problem(hit[1]):
                return hit[1]["key"]
        if _nk(cur) in self._by_key:
            return cur
        for j in rng:  # cur가 목록에서 빠짐: 순서상 가까운, 아직 목록에 있는 영역
            if keys[j] in self._by_key:
                return self._by_key[keys[j]][1]["key"]
        return cur

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
        # 이전·다음 영역은 끝에서 돌아간다 (첫 영역의 ◀ 이전 영역 = 마지막 영역): 끝에서 버튼이 꺼져 있었다
        if kind == "prev":
            return self._neighbor(self.key, -1, False, wrap=True)
        if kind == "next":
            return self._neighbor(self.key, +1, False, wrap=True)
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
        if kind in ("prev_card_bad", "next_card_bad"):
            card = self._card()
            idx = next((i for i, c in enumerate(self.cards) if card is not None and _nk(c["key"]) == _nk(card["key"])),
                       None)
            if idx is None:
                return None
            order = self.cards[idx + 1:] if kind == "next_card_bad" else self.cards[idx - 1::-1] if idx else []
            for c in order:
                first = next((x for x in c["regions"] if self.is_problem(x)), None)
                if first is not None:
                    return first["key"]
        return None

    def _go(self, kind: str) -> None:
        """이동 (단추·단축키). 갈 곳이 없으면 알린다 (단축키는 꺼진 단추와 달리 눌러도 아무 표시가 없었다)."""
        t = self._target(kind)
        if t is None:
            self._flash(NO_TARGET.get(kind, "갈 곳이 없습니다"), "warn")
            QApplication.beep()
            return
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

