"""1.33.0 카드 보기 묶음: ‘이 자리로 저장’ 단축키(Ctrl+S), 아이콘 아님 줄 회색, 위치 못 찾음의 예상 위치를 좁게.
실제 Claude는 부르지 않는다."""

from __future__ import annotations

import os

import numpy as np
import pytest
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from app import icons, service
from tests.test_card_viewer_qt import _viewer

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _no_claude(monkeypatch):
    monkeypatch.setattr(service, "engine_status", lambda: {"message": "시험: 연결 안 함"})
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))


# ---------------------------------------------------------------- Ctrl+S = ‘이 자리로 저장’
def test_ctrl_s_saves_box_only_when_save_button_is_enabled(qapp, tmp_path, monkeypatch):
    info, fd, files, v = _viewer(qapp, tmp_path)
    calls = []
    monkeypatch.setattr(v, "_save_box", lambda: calls.append(1))
    v.show()
    v.activateWindow()
    QApplication.processEvents()
    assert not v.save_btn.isEnabled()  # 새 자리를 그리지 않음
    QTest.keyClick(v, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    assert calls == []
    v.view.new = (2, 2, 20, 20)
    v._icon_tools()
    assert v.save_btn.isEnabled()
    QTest.keyClick(v, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    assert calls == [1]
    assert "Ctrl+S" in v.save_btn.toolTip()
    v.close()


# ---------------------------------------------------------------- 아이콘 아님 줄은 회색
def _entry(state, pending=False):
    return {"occ": "o1", "group": "g1", "state": state, "name": "", "pending": pending, "image": ""}


def test_not_icon_rows_get_gray_background(qapp):
    from app.qt.card_viewer import GRAY_BG, PINK_BG, STAGED_BG, IconRow

    assert IconRow(_entry("not_icon"), [])._bg == GRAY_BG
    assert IconRow(_entry("not_icon", pending=True), [])._bg == GRAY_BG  # 확정 전이어도 (글에 ‘· 확정 전’)
    assert IconRow(_entry("absorbed", pending=True), [])._bg == GRAY_BG  # 그림의 회색 상자(아이콘 아님·포함)와 같게
    assert IconRow(_entry("unnamed"), [])._bg == PINK_BG
    assert IconRow(_entry("named", pending=True), [])._bg == STAGED_BG
    assert IconRow(_entry("ok"), [])._bg == "white"


def test_deleting_icon_turns_its_row_gray(qapp, tmp_path):
    from app.qt.card_viewer import GRAY_BG

    info, fd, files, v = _viewer(qapp, tmp_path)
    v.icon_list.item(0).setSelected(True)
    v.del_btn.click()
    assert v.icon_list.itemWidget(v.icon_list.item(0))._bg == GRAY_BG
    v.close()


# ---------------------------------------------------------------- 확대 칸: 위치 못 찾음이면 예상 위치
def test_magnifier_shows_guess_for_icon_without_box(qapp):
    from app.qt.icon_place import IconPlaceView

    view = IconPlaceView()
    pm = QPixmap(400, 400)
    pm.fill()
    size = QSize(192, 192)
    view.set_data(pm, None, None, [])
    assert view.focus_crop(size).isNull()  # 자리도 예상 위치도 없음
    view.set_data(pm, None, (100, 100, 180, 160), [])
    first = view.focus_crop(size)
    assert not first.isNull()  # 예전: 새 자리를 그리기 전에는 빈 칸
    view.new = (120, 110, 150, 140)  # 예상 위치 안에 그리면 배율은 그대로
    assert view.focus_crop(size).size() == first.size()
    view.close()


# ---------------------------------------------------------------- 예상 위치: 너무 넓지 않게
def test_band_share_splits_merged_panel_band_by_lines():
    bands = [(62, 390), (655, 845)]
    # 원문 5줄 중 0~2줄이 위 띠(색 판)에 든다: 셋으로 나눈 몫
    assert icons._band_share((62, 390), bands, 5, 1, 901) == (171, 280)
    assert icons._band_share((62, 390), bands, 5, 2, 901) == (280, 390)
    assert icons._band_share((655, 845), [(655, 845)], 1, 0, 901) == (655, 845)  # 한 줄뿐이면 그대로
    assert icons._band_share((1, 2), bands, 5, 1, 901) == (1, 2)  # 목록에 없는 띠는 그대로


def test_estimate_for_lone_icon_line_is_not_whole_width():
    """아이콘 하나뿐인 줄: 배경 조각이 줄 양 끝에 있어도 예상 자리가 영역 폭 전체가 되지 않는다 (7쪽 1201px 폭)."""
    img = np.full((300, 1200, 3), 245, np.uint8)
    img[75:225, 525:675] = (40, 40, 40)  # 아이콘 150x150
    for x in (30, 1160):  # 양 끝 배경 조각
        img[140:150, x:x + 10] = (40, 40, 40)
    g = icons.estimate(img, "[[?]]", 0)
    assert g is not None
    assert g[0] <= 600 <= g[2] and g[1] <= 150 <= g[3]  # 아이콘 가운데를 덮고
    assert g[2] - g[0] <= 2.4 * 150  # 예전: 0~1200 (폭 전체)


def _icon_row(xs, specks=()):
    img = np.full((300, 1200, 3), 245, np.uint8)
    for x in xs:
        img[75:225, x:x + 150] = (40, 40, 40)
    for x in specks:
        img[140:150, x:x + 10] = (40, 40, 40)
    return img


def test_estimate_lone_icon_with_speck_on_one_side_still_covers_icon():
    """좁힌 상자가 글자 칸 비율 자리(배경 조각까지 늘어난 줄의 가운데)에 놓여 아이콘을 벗어나지 않는다 (리뷰 재현)."""
    g = icons.estimate(_icon_row([525], specks=[30]), "[[?]]", 0)
    assert g is not None and g[0] <= 600 <= g[2]


def test_box_owner_tolerates_box_slightly_outside_narrow_guess(qapp, tmp_path):
    """예상 자리가 좁아져(1.33.0) 진짜 자리가 조금 벗어나도 옆 [[?]]의 것으로 묻지 않는다. 멀리(다른 줄) 잡으면 묻는다."""
    info, fd, files, v = _viewer(qapp, tmp_path)
    v._entries = [{"occ": "o1", "state": "lost"}, {"occ": "o2", "state": "lost"}]
    v._place = {"guess": (100, 100, 200, 200),
                "marks": [{"guess": True, "occ": "o2", "box": (150, 0, 600, 400)}]}
    e = v._entries[0]
    assert v._box_owner(e, (205, 140, 235, 160)) is None  # 가운데 220: 예상 자리(~200) 밖, 1.5배(~225) 안
    assert v._box_owner(e, (400, 300, 440, 340))["occ"] == "o2"
    v.close()


def test_magnifier_starts_from_new_box_when_drawn_away_from_guess(qapp):
    from app.qt.icon_place import IconPlaceView

    view = IconPlaceView()
    pm = QPixmap(1200, 400)
    pm.fill()
    size = QSize(192, 192)
    view.set_data(pm, None, (100, 100, 180, 160), [])
    view.new = (900, 100, 902, 102)  # 예상 위치 밖에서 끌기 시작
    near = view.focus_crop(size).size()
    view.set_data(pm, None, None, [])  # 예상 위치가 없을 때와 같은 배율 (둘을 다 담느라 줄지 않음)
    view.new = (900, 100, 902, 102)
    assert view.focus_crop(size).size() == near
    view.close()
