"""대화상자도 최대화(전체 화면)할 수 있다: 제목줄에 최대화 버튼."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_fit_to_screen_adds_maximize_button(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog

    from app.qt.dialogs import allow_maximize, fit_to_screen

    d = QDialog()
    fit_to_screen(d, 800, 600)
    assert d.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint
    d.show()
    allow_maximize(d)  # 이미 떠 있어도 숨겨지지 않는다
    assert d.isVisible()
    d.close()
