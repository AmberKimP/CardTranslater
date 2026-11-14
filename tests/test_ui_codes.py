"""창 번호 [2B]·Ctrl+D 버튼 코드 디버깅 모드."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def ui():
    from PySide6.QtWidgets import QApplication

    from app.qt import ui_codes

    app = QApplication.instance() or QApplication([])
    u = ui_codes.install(app)
    yield u
    if u.debug:
        u.toggle()


def _key_ctrl_d(w):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    QApplication.sendEvent(w, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_D, Qt.KeyboardModifier.ControlModifier))


def test_every_window_code_is_digit_plus_letter_and_unique():
    from app.qt.ui_codes import WINDOW_CODES

    codes = [c for c, _n in WINDOW_CODES.values()]
    assert all(len(c) == 2 and c[0].isdigit() and c[1].isalpha() and c[1].isupper() for c in codes)
    assert len(codes) == len(set(codes))


def test_window_classes_in_table_exist():
    """표의 이름이 실제 창 클래스와 맞는지 (이름을 바꾸면 번호가 사라지므로)."""
    import importlib
    import inspect
    import pkgutil

    import app.qt as pkg
    from app.qt.ui_codes import WINDOW_CODES

    names = set()
    for m in pkgutil.iter_modules(pkg.__path__):
        mod = importlib.import_module(f"app.qt.{m.name}")
        names |= {n for n, o in inspect.getmembers(mod, inspect.isclass) if o.__module__ == mod.__name__}
    missing = set(WINDOW_CODES) - names - {"QMessageBox"}
    assert not missing


def test_title_gets_code_and_keeps_it_after_retitle(ui):
    from PySide6.QtWidgets import QDialog

    class TermChangesDialog(QDialog):  # 이름으로 번호를 찾는다
        pass

    d = TermChangesDialog()
    d.setWindowTitle("바뀐 용어 반영")
    d.show()
    assert d.windowTitle() == "[5B] 바뀐 용어 반영"
    d.setWindowTitle("바뀐 용어 반영 *")  # 화면이 제목을 다시 써도 번호가 남는다
    assert d.windowTitle() == "[5B] 바뀐 용어 반영 *"
    d.close()


def test_ctrl_d_shows_codes_copies_and_blocks_clicks(ui):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QDialog, QPushButton, QVBoxLayout

    class SourceReviewDialog(QDialog):
        pass

    d = SourceReviewDialog()
    lay = QVBoxLayout(d)
    a, b = QPushButton("저장"), QPushButton("다음 칸")
    clicks = []
    b.clicked.connect(lambda: clicks.append(1))
    lay.addWidget(a)
    lay.addWidget(b)
    d.show()
    _key_ctrl_d(d)
    assert ui.debug and a.text() == "3A-01" and b.text() == "3A-02"
    QTest.mouseClick(b, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))
    assert clicks == [] and QGuiApplication.clipboard().text() == "3A-02"
    _key_ctrl_d(d)
    assert not ui.debug and a.text() == "저장" and b.text() == "다음 칸"
    QTest.mouseClick(b, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))
    assert clicks == [1]
    d.close()


def test_buttons_shown_later_in_debug_mode_get_codes(ui):
    from PySide6.QtWidgets import QDialog, QPushButton, QVBoxLayout

    class ResultViewer(QDialog):
        pass

    d = ResultViewer()
    lay = QVBoxLayout(d)
    lay.addWidget(QPushButton("하나"))
    d.show()
    ui.toggle()
    late = QPushButton("나중")
    lay.addWidget(late)
    late.show()
    assert late.text() == "9A-02"
    ui.toggle()
    assert late.text() == "나중"
    d.close()


def test_debug_mode_blocks_shortcuts(ui):
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeySequence, QShortcut
    from PySide6.QtWidgets import QApplication, QDialog

    class RegionEditor(QDialog):
        pass

    d = RegionEditor()
    fired = []
    sc = QShortcut(QKeySequence("Ctrl+S"), d)
    sc.activated.connect(lambda: fired.append(1))
    d.show()
    ui.toggle()
    ev = QEvent(QEvent.Type.Shortcut)
    assert ui.eventFilter(sc, ev) is True  # 디버깅 중에는 단축키 객체로 가는 이벤트를 막는다
    ui.toggle()
    assert ui.eventFilter(sc, ev) is False
    d.close()
