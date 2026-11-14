"""앱 전체 화면 스타일 (한 곳에서). main()이 QApplication에 apply()로 입힌다. 값과 이유는 DESIGN.md.

색 약속 (docs·README와 같음):
- 주 버튼·강조: 파랑 ACCENT
- 확인할 것: 노랑(#FFE066)·주황(#F4B183), 확인됨: 연두(#D9EAD3), 사람이 고침: 하늘(#CFE2F3)
바탕은 옅은 회색, 내용 칸(표·목록·입력 칸·카드)은 흰색에 둥근 모서리.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

ACCENT = "#0071E3"
ACCENT_HOVER = "#0062C4"
ACCENT_SOFT = "#E5F0FD"   # 고른 줄·눌린 도구 버튼
BG = "#F2F2F7"            # 창 바탕
CARD = "#FFFFFF"          # 내용 칸
LINE = "#D1D1D6"          # 입력 칸 테두리
LINE_SOFT = "#E5E5EA"     # 표·카드 테두리
TEXT = "#1D1D1F"
TEXT_SUB = "#6E6E73"
DISABLED = "#AEAEB2"
DANGER = "#D70015"
# 글자 (DESIGN.md 3장, 데스크톱에 맞춘 크기)
FONT_FAMILIES = ("Segoe UI", "Malgun Gothic")
BODY_PT = 10.0
RADIUS_CONTROL = 8   # 버튼·입력 칸
RADIUS_CARD = 12     # 카드·묶음 상자

_ARROWS = {
    "down": '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 12 12">'
            '<path d="M2.5 4.5 L6 8 L9.5 4.5" fill="none" stroke="{c}" stroke-width="1.6" '
            'stroke-linecap="round" stroke-linejoin="round"/></svg>',
    "up": '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 12 12">'
          '<path d="M2.5 7.5 L6 4 L9.5 7.5" fill="none" stroke="{c}" stroke-width="1.6" '
          'stroke-linecap="round" stroke-linejoin="round"/></svg>',
    "check": '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
             '<path d="M3.5 8.5 L6.5 11.5 L12.5 4.8" fill="none" stroke="{c}" stroke-width="2" '
             'stroke-linecap="round" stroke-linejoin="round"/></svg>',
    "dot": '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
           '<circle cx="8" cy="8" r="3.4" fill="{c}"/></svg>',
}


def _arrow_files() -> dict[str, str]:
    """콤보·숫자 칸 화살표 그림 (스타일시트는 파일 경로만 받는다). 임시 폴더에 한 번 쓴다."""
    d = Path(tempfile.gettempdir()) / "CardTranslater-theme"
    out = {}
    try:
        d.mkdir(parents=True, exist_ok=True)
        for name, svg in _ARROWS.items():
            colors = (("", "#FFFFFF"),) if name in ("check", "dot") else (("", TEXT_SUB), ("_off", DISABLED))
            for tag, color in colors:
                p = d / f"{name}{tag}.svg"
                body = svg.format(c=color)
                if not p.exists() or p.read_text(encoding="utf-8") != body:
                    p.write_text(body, encoding="utf-8")
                out[name + tag] = p.as_posix()
    except OSError:
        return {}
    return out


def stylesheet() -> str:
    a = _arrow_files()

    def arrow(name: str) -> str:
        return f"image: url({a[name]});" if name in a else ""

    return f"""
QWidget {{ color: {TEXT}; }}
QMainWindow, QDialog {{ background: {BG}; }}
QLabel {{ background: transparent; }}

/* ---- 버튼: 보조 = 흰 바탕·회색 테두리, 주 버튼은 dialogs.PRIMARY_QSS(파랑) */
QPushButton {{
    background: {CARD}; border: 1px solid {LINE}; border-radius: 8px;
    padding: 6px 16px; min-height: 22px;
}}
QPushButton:hover {{ border-color: {ACCENT}; background: #F0F6FE; }}
QPushButton:pressed {{ background: {ACCENT_SOFT}; }}
QPushButton:disabled {{ color: {DISABLED}; background: #F2F2F7; border-color: {LINE_SOFT}; }}
QPushButton:flat {{ border: none; background: transparent; color: {ACCENT}; }}
QPushButton:flat:hover {{ background: {ACCENT_SOFT}; }}
QPushButton[danger="true"] {{ color: {DANGER}; }}
QPushButton[danger="true"]:hover {{ border-color: {DANGER}; background: #FEF3F2; }}
QPushButton[danger="true"]:disabled {{ color: {DISABLED}; }}
QToolButton {{
    background: {CARD}; border: 1px solid {LINE}; border-radius: 8px; padding: 4px 10px;
}}
QToolButton:hover {{ border-color: {ACCENT}; background: #F0F6FE; }}
QToolButton:checked {{ background: {ACCENT_SOFT}; border-color: {ACCENT}; color: {ACCENT}; font-weight: bold; }}
QToolButton:disabled {{ color: {DISABLED}; background: #F2F2F7; border-color: {LINE_SOFT}; }}
QToolButton[autoRaise="true"] {{ border: none; background: transparent; color: {ACCENT}; }}
QToolButton[autoRaise="true"]:hover {{ background: {ACCENT_SOFT}; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}

/* ---- 입력 칸 */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {CARD}; border: 1px solid {LINE}; border-radius: 8px; padding: 4px 8px;
    selection-background-color: #CFE2F3; selection-color: {TEXT};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border: 1px solid {ACCENT};
}}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled, QPlainTextEdit:disabled {{
    background: #F2F2F7; color: {DISABLED}; border-color: {LINE_SOFT};
}}
QLineEdit:read-only {{ background: #F7F7FA; }}
QAbstractSpinBox QLineEdit {{ border: none; padding: 0; background: transparent; }}
QComboBox QLineEdit {{ border: none; padding: 0; background: transparent; }}
QComboBox {{ padding-right: 26px; }}
QComboBox:hover {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 24px; border: none; }}
QComboBox::down-arrow {{ {arrow("down")} width: 12px; height: 12px; }}
QComboBox::down-arrow:disabled {{ {arrow("down_off")} }}
QComboBox QAbstractItemView {{
    background: {CARD}; border: 1px solid {LINE}; selection-background-color: {ACCENT_SOFT};
    selection-color: {TEXT}; outline: 0; padding: 2px;
}}
QSpinBox, QDoubleSpinBox {{ padding: 4px 22px 4px 4px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border; subcontrol-position: top right; width: 20px; border: none;
    border-top-right-radius: 8px; background: transparent;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border; subcontrol-position: bottom right; width: 20px; border: none;
    border-bottom-right-radius: 8px; background: transparent;
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{ background: {ACCENT_SOFT}; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ {arrow("up")} width: 10px; height: 10px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ {arrow("down")} width: 10px; height: 10px; }}
QSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:disabled {{ {arrow("up_off")} }}
QSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:disabled {{ {arrow("down_off")} }}

QCheckBox, QRadioButton {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator, QRadioButton::indicator, QAbstractItemView::indicator {{
    width: 15px; height: 15px; border: 1px solid #AEAEB2; background: {CARD}; border-radius: 4px;
}}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover, QAbstractItemView::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked, QAbstractItemView::indicator:checked {{
    background: {ACCENT}; border-color: {ACCENT}; {arrow("check")}
}}
QRadioButton::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; {arrow("dot")} }}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{ background: #F2F2F7; border-color: {LINE}; }}
QCheckBox::indicator:checked:disabled, QRadioButton::indicator:checked:disabled {{
    background: #9CC3EF; border-color: #9CC3EF;
}}

/* ---- 표·목록·트리: 흰 칸, 넉넉한 줄 높이, 옅은 선 */
QTreeView, QTableView, QListView {{
    background: {CARD}; border: 1px solid {LINE_SOFT}; border-radius: 8px;
    selection-background-color: {ACCENT_SOFT}; selection-color: {TEXT}; outline: 0;
    gridline-color: #EFEFF4;
}}
QTreeView::item, QListView::item {{ padding: 5px 4px; }}
QTreeView::item:selected, QListView::item:selected, QTableView::item:selected {{
    background: {ACCENT_SOFT}; color: {TEXT};
}}
QTreeView::item:hover, QListView::item:hover {{ background: #F2F7FE; }}
QHeaderView {{ background: transparent; border: none; }}
QHeaderView::section {{
    background: #F7F7FA; color: {TEXT_SUB}; font-weight: bold; border: none;
    border-bottom: 1px solid {LINE_SOFT}; padding: 6px 8px;
}}
QTableCornerButton::section {{ background: #F7F7FA; border: none; }}

/* ---- 묶음 상자: 흰 카드 + 제목 */
QGroupBox {{
    background: {CARD}; border: 1px solid {LINE_SOFT}; border-radius: 12px;
    margin-top: 0; padding: 34px 12px 12px 12px; font-weight: bold; font-size: 10.5pt;
}}
QGroupBox::title {{
    subcontrol-origin: padding; subcontrol-position: top left; left: 14px; top: 9px; padding: 0;
    color: #3A3A3C; background: transparent;
}}
QGroupBox QLabel, QGroupBox QCheckBox, QGroupBox QRadioButton, QGroupBox QLineEdit,
QGroupBox QComboBox, QGroupBox QPushButton, QGroupBox QSpinBox, QGroupBox QDoubleSpinBox,
QGroupBox QPlainTextEdit {{ font-weight: normal; }}
QFrame#card {{ background: {CARD}; border: 1px solid {LINE_SOFT}; border-radius: 12px; }}

/* ---- 스크롤 막대: 가늘고 둥글게 */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #C7C7CC; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:horizontal {{ background: #C7C7CC; border-radius: 3px; min-width: 30px; }}
QScrollBar::handle:hover {{ background: #AEAEB2; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; background: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QSplitter::handle {{ background: transparent; }}
QSplitter::handle:hover {{ background: {ACCENT_SOFT}; }}

/* ---- 메뉴·상태 줄·말풍선 */
QMenuBar {{ background: {CARD}; border-bottom: 1px solid {LINE_SOFT}; padding: 2px 4px; }}
QMenuBar::item {{ padding: 4px 10px; border-radius: 4px; background: transparent; }}
QMenuBar::item:selected {{ background: {ACCENT_SOFT}; }}
QMenu {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 22px 6px 14px; border-radius: 4px; }}
QMenu::item:selected {{ background: {ACCENT_SOFT}; color: {TEXT}; }}
QMenu::item:disabled {{ color: {DISABLED}; }}
QMenu::separator {{ height: 1px; background: {LINE_SOFT}; margin: 4px 8px; }}
QStatusBar {{ background: {CARD}; border-top: 1px solid {LINE_SOFT}; color: {TEXT_SUB}; }}
QStatusBar QLabel {{ color: {TEXT_SUB}; }}
QToolTip {{ background: #2C2C2E; color: white; border: none; padding: 6px 8px; border-radius: 8px; }}

QProgressBar {{
    background: #E5E5EA; border: none; border-radius: 8px; height: 12px; text-align: center; color: {TEXT};
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 8px; }}
QMessageBox {{ background: {BG}; }}
"""


def _spin_polisher():
    """숫자 칸 글자가 왼쪽 테두리에 붙지 않게 (스타일시트 padding이 숫자 칸 안쪽에는 먹지 않아 직접 여백을 준다)."""
    from PySide6.QtCore import QEvent, QObject
    from PySide6.QtWidgets import QAbstractSpinBox

    class Polisher(QObject):
        def eventFilter(self, obj, ev):  # noqa: N802 - Qt 이름
            if ev.type() == QEvent.Type.Polish and isinstance(obj, QAbstractSpinBox):
                le = obj.lineEdit()
                if le is not None:
                    le.setTextMargins(6, 0, 0, 0)
            return False

    return Polisher()


def apply(app) -> None:
    """앱 전체에 스타일을 입힌다 (Fusion 바탕 + 스타일시트). 어느 Windows 테마에서도 같은 모양이 되게."""
    from PySide6.QtGui import QColor, QPalette

    app.setStyle("Fusion")
    from PySide6.QtGui import QFont

    font = QFont()  # 본문 글꼴 (DESIGN.md 3장: Body 10pt ≈ 13px, 한글은 맑은 고딕)
    font.setFamilies(list(FONT_FAMILIES))
    font.setPointSizeF(BODY_PT)
    app.setFont(font)
    pal = app.palette()
    for role, color in ((QPalette.ColorRole.Window, BG), (QPalette.ColorRole.Base, CARD),
                        (QPalette.ColorRole.AlternateBase, "#F7F7FA"), (QPalette.ColorRole.Button, CARD),
                        (QPalette.ColorRole.WindowText, TEXT), (QPalette.ColorRole.Text, TEXT),
                        (QPalette.ColorRole.ButtonText, TEXT), (QPalette.ColorRole.Highlight, ACCENT_SOFT),
                        (QPalette.ColorRole.HighlightedText, TEXT), (QPalette.ColorRole.ToolTipBase, "#2C2C2E"),
                        (QPalette.ColorRole.ToolTipText, "#FFFFFF"), (QPalette.ColorRole.PlaceholderText, DISABLED),
                        (QPalette.ColorRole.Link, ACCENT)):
        pal.setColor(role, QColor(color))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(DISABLED))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(DISABLED))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(DISABLED))
    app.setPalette(pal)
    app.setStyleSheet(stylesheet())
    if getattr(app, "_ct_spin_polisher", None) is None:
        app._ct_spin_polisher = _spin_polisher()  # 지워지지 않게 붙들어 둔다
        app.installEventFilter(app._ct_spin_polisher)
