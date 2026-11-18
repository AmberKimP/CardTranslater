"""이미지 시트 자르기: 예상 카드 크기 보이기, 카드 크기를 정해 다시 찾기, 붙은 배열의 끝 카드가 그림 밖으로 밀리지 않기.
가짜 시트만 쓴다 (실제 호출 없음)."""

from __future__ import annotations

import json
import os

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app import images, service, workspace


def _touching(path, n=5, card=(260, 420), dpi=300, light_left=60):
    """틈 없이 붙은 카드 n장 (가로 한 줄). 카드마다 왼쪽 끝에 어두운 경계선, 첫 카드 왼쪽은 밝게 (여백처럼 보이게).
    카드 안 같은 자리(경계에서 light_left px)에 굵은 세로줄이 있어 반복 간격은 또렷하지만 경계로 잘못 잡기 쉽다."""
    w, h = n * card[0], card[1]
    im = Image.new("RGB", (w, h), (235, 225, 210))
    d = ImageDraw.Draw(im)
    for k in range(n):
        x = k * card[0]
        d.rectangle([x + 12, 30, x + card[0] - 12, h - 30], fill=(120 + 20 * k, 90, 150))
        d.rectangle([x + light_left, 0, x + light_left + 6, h], fill=(20, 20, 20))  # 카드 안 굵은 줄
        if k:
            d.rectangle([x - 1, 0, x + 1, h], fill=(250, 250, 250))  # 카드 사이 경계 (밝은 단면)
            d.rectangle([x + 2, 0, x + 3, h], fill=(40, 40, 40))  # 그림자
    d.rectangle([0, 0, light_left - 1, h], fill=(252, 252, 252))  # 첫 카드 왼쪽이 밝다
    im.save(path, dpi=(dpi, dpi))
    return [(k * card[0], 0, (k + 1) * card[0], h) for k in range(n)]


def test_place_axis_moves_clipped_touching_row_back_inside():
    """붙은 배열의 경계가 밀려 끝 카드가 그림 끝에 걸렸으면, 모든 카드가 들어가는 자리 중 경계가 뚜렷한 곳으로."""
    w, h, pitch, n = 1300, 300, 260, 5
    rgb = np.full((h, w, 3), 200, np.uint8)
    for k in range(1, n):
        rgb[:, k * pitch - 1:k * pitch + 1] = 250
        rgb[:, k * pitch + 1:k * pitch + 3] = 30
    shifted = [(40 + k * pitch, 0, min(w, 40 + (k + 1) * pitch), h) for k in range(n)]  # 40px 밀림, 끝 카드 잘림
    out, moved = images._place_axis(rgb, shifted, 0, float(pitch), inside_only=True)
    assert moved and [b[0] for b in out] == [0, 260, 520, 780, 1040] and out[-1][2] == 1300
    ok = [(k * pitch, 0, (k + 1) * pitch, h) for k in range(n)]
    assert images._place_axis(rgb, ok, 0, float(pitch), inside_only=True) == (ok, False)  # 잘리지 않았으면 그대로


def test_place_axis_leaves_cropped_scan_alone():
    """끝 카드가 그림 끝에 걸렸어도 반대쪽이 빈 종이면(스캔이 카드를 잘라 먹음) 옮기지 않는다 (공주와기사 3×3 스캔)."""
    w, h, pitch, n = 1240, 300, 375, 3
    rgb = np.full((h, w, 3), 255, np.uint8)
    rgb[:, 122:] = 170  # 왼쪽 122px는 흰 종이, 카드는 오른쪽 그림 끝 너머까지
    for k in range(1, n):
        rgb[:, 122 + k * pitch:122 + k * pitch + 3] = 30
    boxes = [(122 + k * pitch, 0, min(w, 122 + (k + 1) * pitch), h) for k in range(n)]
    assert images._place_axis(rgb, boxes, 0, float(pitch), inside_only=True) == (boxes, False)
    out, _m = images._place_axis(rgb, boxes, 0, float(pitch), inside_only=False)  # 크기를 정해도 그림 안으로 밀지 않는다
    assert [b[0] for b in out] == [122, 497, 872]


def test_cut_with_card_size_refinds_touching_cards_and_reports_mm(tmp_path):
    truth = _touching(tmp_path / "t.png")
    auto = images.cut_sheet(tmp_path / "t.png", 5, 1)
    mm = images.card_mm(auto)
    assert mm is not None and len(auto.boxes) == 5
    size_px = (260, 420)
    cut = images.cut_sheet(tmp_path / "t.png", 5, 1, card_size=size_px)
    assert [b[0] for b in cut.boxes] == [b[0] for b in truth] and all(b[2] - b[0] == 260 for b in cut.boxes)
    assert any("정한 카드 크기" in w and "mm" in w for w in cut.warnings)
    assert images.card_mm(cut) == (round(260 * 25.4 / 300, 1), round(420 * 25.4 / 300, 1))


def test_cut_with_card_size_keeps_centers_when_cards_have_gaps(tmp_path):
    from tests.test_images import _grid

    boxes = _grid(tmp_path / "g.png", 3, 2)  # 카드 380×430, 틈 30
    cut = images.cut_sheet(tmp_path / "g.png", 3, 2, card_size=(360, 410))
    for (x0, y0, x1, y1), (a0, b0, a1, b1) in zip(cut.boxes, boxes):
        assert (x1 - x0, y1 - y0) == (360, 410)
        assert abs((x0 + x1) / 2 - (a0 + a1) / 2) <= 1 and abs((y0 + y1) / 2 - (b0 + b1) / 2) <= 1


def test_service_preview_size_add_images_saves_size_and_recut_uses_it(tmp_path):
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    (tmp_path / "in").mkdir()
    _touching(tmp_path / "in" / "t.png")
    png, pv = service.image_preview(tmp_path / "in" / "t.png", 5, 1)
    assert png[:4] == b"\x89PNG" and pv["card_mm"] and pv["dpi"] == pytest.approx(300, abs=1)
    mm = (round(260 * 25.4 / 300, 2), round(420 * 25.4 / 300, 2))
    _png, pv2 = service.image_preview(tmp_path / "in" / "t.png", 5, 1, card_mm=mm)
    assert any("정한 카드 크기" in w for w in pv2["warnings"])
    r = service.add_images(info.dir, [tmp_path / "in" / "t.png"], "카드", cols=5, rows=1, new_group=True, card_mm=mm)
    fi = json.loads((r.file_dir / "file.json").read_text(encoding="utf-8"))["from_images"]
    assert fi["card_mm"] == [mm[0], mm[1]]
    chk = service.recut_check(r.file_dir)  # 넣을 때 정한 크기로 다시 잘라 본다: 같다
    assert chk.can and not chk.changed and chk.card_mm == mm and chk.cols == 5 and chk.image.endswith("t.png")
    chk2 = service.recut_check(r.file_dir, card_mm=(mm[0] - 3, mm[1]))  # 다른 크기: 달라진다
    assert chk2.changed and chk2.card_mm == (mm[0] - 3, mm[1])


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def test_cut_preview_dialog_shows_size_and_refinds(qapp, tmp_path):
    from app.qt.cut_preview import CutPreviewDialog

    _touching(tmp_path / "t.png")
    dlg = CutPreviewDialog(tmp_path / "t.png", 5, 1)
    assert dlg.chosen_mm() is None and dlg.ok_cards == 5 and dlg.w_mm.value() > 5 and "px" in dlg.px.text()
    assert not dlg.b_auto.isEnabled() and "실제 카드 크기" in dlg.note.text()
    dlg.w_mm.setValue(22.0)
    dlg.h_mm.setValue(35.6)
    dlg.find_with_size()
    assert dlg.chosen_mm() == (22.0, 35.6) and "정한 크기 22.0×35.6mm" in dlg.note.text() and dlg.b_auto.isEnabled()
    dlg.find_auto()
    assert dlg.chosen_mm() is None
    assert dlg.windowTitle().startswith("자를 자리")


def test_add_files_preview_keeps_chosen_size_and_resets_on_layout_change(qapp, tmp_path, monkeypatch):
    from app.qt import cut_preview
    from app.qt.dialogs import AddFilesDialog

    info, _ = workspace.create_project("p", "G", root=tmp_path)
    (tmp_path / "in").mkdir()
    _touching(tmp_path / "in" / "t.png")
    dlg = AddFilesDialog(info.dir)
    dlg._add_path(str(tmp_path / "in" / "t.png"))
    dlg.r_grid.setChecked(True)
    dlg.cols.setValue(5)
    dlg.rows.setValue(1)

    class Fake:
        def __init__(self, *a, **k):
            pass

        def exec(self):
            return True

        def chosen_mm(self):
            return (22.0, 35.6)
    monkeypatch.setattr(cut_preview, "CutPreviewDialog", Fake)
    dlg.preview()
    assert dlg.card_mm == (22.0, 35.6) and "22.0×35.6mm" in dlg.img_info.text()
    assert dlg._card_mm_for(5, 1) == (22.0, 35.6)
    dlg.cols.setValue(4)  # 장수를 바꾸면 정한 크기는 버린다
    assert dlg.card_mm is None


def test_too_large_card_size_keeps_auto_cut_and_warns(tmp_path):
    """정한 카드 크기가 시트에 비해 너무 크면(가로·세로를 바꿔 넣음 등) 자동으로 찾은 자리 그대로 + 알림.
    뒤집히거나 겹친 상자를 만들지 않는다 (예전: 미리 보기에서 Pillow 오류)."""
    from tests.test_images import _grid

    _touching(tmp_path / "t.png")
    auto = images.cut_sheet(tmp_path / "t.png", 5, 1)
    for size in ((400, 420), (520, 420), (2000, 2000)):
        cut = images.cut_sheet(tmp_path / "t.png", 5, 1, card_size=size)
        assert cut.boxes == auto.boxes and any("너무 커서" in w for w in cut.warnings)
        assert all(b[2] > b[0] and b[3] > b[1] for b in cut.boxes)
    _grid(tmp_path / "g.png", 3, 2)
    cut = images.cut_sheet(tmp_path / "g.png", 3, 2, card_size=(800, 800))
    assert all(b[2] > b[0] and b[3] > b[1] for b in cut.boxes) and any("너무 커서" in w for w in cut.warnings)
    png, _info = service.image_preview(tmp_path / "t.png", 5, 1, card_mm=(60.0, 40.0))  # 미리 보기도 오류 없이
    assert png[:4] == b"\x89PNG"


def test_narrow_gutters_keep_their_gaps_with_card_size(tmp_path):
    """틈이 좁은 시트(1.5mm 등)는 붙은 배열로 보지 않는다: 카드마다 가운데를 그대로 두고 크기만 바꾼다."""
    from tests.test_images import _grid

    boxes = _grid(tmp_path / "n.png", 3, 1, gap=12)
    cut = images.cut_sheet(tmp_path / "n.png", 3, 1, card_size=(390, 440))
    for (x0, _y0, x1, _y1), (a0, _b0, a1, _b1) in zip(cut.boxes, boxes):
        assert x1 - x0 == 390 and abs((x0 + x1) / 2 - (a0 + a1) / 2) <= 1


def test_cut_preview_ok_with_typed_size_refinds_first(qapp, tmp_path):
    """크기 칸만 고치고 ‘이대로 자르기’를 누르면 그 크기로 먼저 다시 찾고 창에 남는다 (보지 않은 자리로 자르지 않게)."""
    from app.qt.cut_preview import CutPreviewDialog

    _touching(tmp_path / "t.png")
    dlg = CutPreviewDialog(tmp_path / "t.png", 5, 1)
    dlg.show()
    dlg.w_mm.setValue(22.0)
    dlg.h_mm.setValue(35.6)
    dlg.accept()
    assert dlg.isVisible() and dlg.chosen_mm() == (22.0, 35.6)
    dlg.accept()  # 다시 누르면 닫힌다
    assert not dlg.isVisible() and dlg.result() == 1
    one = CutPreviewDialog(tmp_path / "t.png", 1, 1, ok_text="닫기")
    one.size_row_visible(False)
    assert one.cancel.isHidden() and one.w_mm.isHidden()


def test_add_files_drops_chosen_size_when_files_change(qapp, tmp_path):
    from app.qt.dialogs import AddFilesDialog

    info, _ = workspace.create_project("p", "G", root=tmp_path)
    (tmp_path / "in").mkdir()
    _touching(tmp_path / "in" / "t.png")
    _touching(tmp_path / "in" / "u.png")
    dlg = AddFilesDialog(info.dir)
    dlg._add_path(str(tmp_path / "in" / "t.png"))
    dlg.card_mm = (22.0, 35.6)
    dlg._add_path(str(tmp_path / "in" / "u.png"))
    assert dlg.card_mm is None
