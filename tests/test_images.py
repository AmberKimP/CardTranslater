"""이미지(JPG·PNG) 카드 넣기 (PRD F27): 한 장에 한 장 / 한 장에 가로 X × 세로 Y 배열."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pymupdf
import pytest
from PIL import Image, ImageDraw

from app import images, service, workspace

COLORS = [(200, 60, 60), (60, 160, 60), (60, 60, 200), (200, 160, 40), (150, 60, 160), (40, 160, 160)]


def _grid(path: Path, cols=3, rows=2, gap=30, margin=(40, 60), card=(380, 430), dpi=300, fmt="PNG"):
    """흰 바탕에 색이 다른 카드 cols×rows (틈 gap). 카드마다 (x0, y0, x1, y1)."""
    w = margin[0] * 2 + cols * card[0] + (cols - 1) * gap + 17  # 오른쪽 여백을 일부러 다르게
    h = margin[1] * 2 + rows * card[1] + (rows - 1) * gap
    im = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(im)
    boxes = []
    for r in range(rows):
        for c in range(cols):
            x = margin[0] + c * (card[0] + gap)
            y = margin[1] + r * (card[1] + gap)
            d.rectangle([x, y, x + card[0] - 1, y + card[1] - 1], fill=COLORS[(r * cols + c) % len(COLORS)])
            boxes.append((x, y, x + card[0], y + card[1]))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, format=fmt, dpi=(dpi, dpi))
    return boxes


def test_cut_sheet_finds_gutters_and_trims_margins(tmp_path):
    boxes = _grid(tmp_path / "sheet.png")
    cut = images.cut_sheet(tmp_path / "sheet.png", 3, 2)
    assert cut.gutters_found and cut.boxes == boxes and not cut.warnings


def test_cut_sheet_without_gutters_splits_evenly_and_warns(tmp_path):
    im = Image.new("RGB", (900, 600), (90, 140, 200))  # 카드끼리 틈 없이 붙은 배열
    im.save(tmp_path / "tight.jpg", quality=92)
    cut = images.cut_sheet(tmp_path / "tight.jpg", 3, 2)
    assert not cut.gutters_found and len(cut.boxes) == 6 and cut.warnings
    assert cut.boxes[0] == (0, 0, 300, 300) and cut.boxes[-1] == (600, 300, 900, 600)
    assert images.cut_sheet(tmp_path / "tight.jpg").boxes == [(0, 0, 900, 600)]  # 한 장에 한 장


def test_images_to_pdf_page_order_size_and_rotation(tmp_path):
    _grid(tmp_path / "a.png", 3, 2, dpi=150)
    # 사진 회전 정보(EXIF 6 = 시계 방향 90도)가 있는 세로 사진 한 장
    single = Image.new("RGB", (400, 300), (10, 120, 10))
    exif = single.getexif()
    exif[0x0112] = 6
    single.save(tmp_path / "b.jpg", exif=exif, quality=92)
    # 투명 배경 PNG
    tr = Image.new("RGBA", (200, 280), (0, 0, 0, 0))
    ImageDraw.Draw(tr).rectangle([20, 20, 180, 260], fill=(220, 30, 30, 255))
    tr.save(tmp_path / "c.png")
    r = images.images_to_pdf([tmp_path / "a.png", tmp_path / "b.jpg", tmp_path / "c.png"], tmp_path / "o.pdf", 3, 2)
    assert r.pages == 6 + 6 + 6  # 같은 배열 설정이 모든 이미지에 적용된다
    r1 = images.images_to_pdf([tmp_path / "a.png"], tmp_path / "o1.pdf", 3, 2)
    doc = pymupdf.open(r1.pdf)
    assert doc.page_count == 6
    assert doc[0].rect.width == pytest.approx(380 * 72 / 150, abs=0.5)  # 쪽 크기 = 이미지 DPI 기준
    for i in range(6):  # 왼쪽 위부터 가로 먼저
        pix = doc[i].get_pixmap(dpi=30)
        c = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[pix.height // 2, pix.width // 2]
        assert np.abs(c[:3].astype(int) - COLORS[i]).max() < 25
    r2 = images.images_to_pdf([tmp_path / "b.jpg"], tmp_path / "o2.pdf")
    d2 = pymupdf.open(r2.pdf)
    assert d2[0].rect.width < d2[0].rect.height  # 회전 정보를 반영해 세로
    r3 = images.images_to_pdf([tmp_path / "c.png"], tmp_path / "o3.pdf")
    pix = pymupdf.open(r3.pdf)[0].get_pixmap(dpi=72)
    corner = pix.pixel(1, 1)
    assert min(corner) > 240  # 투명한 곳은 흰색


def test_preview_marks_cards(tmp_path):
    _grid(tmp_path / "s.png")
    png, info = service.image_preview(tmp_path / "s.png", 3, 2)
    assert png[:4] == b"\x89PNG" and info["cards"] == 6 and info["gutters_found"]


def test_add_images_to_group_and_erase(tmp_path):
    from tests.test_qt import _pdf

    info, _ = workspace.create_project("p", "G", root=tmp_path)
    service.add_files(info.dir, [_pdf(tmp_path / "in" / "x.pdf", 1)], "PDF", new_group=True)
    _grid(tmp_path / "in" / "sheet1.png")
    _grid(tmp_path / "in" / "sheet2.png")
    r = service.add_images(info.dir, [tmp_path / "in" / "sheet1.png", tmp_path / "in" / "sheet2.png",
                                      tmp_path / "in" / "note.txt"], "이미지 카드", cols=3, rows=2,
                           name="시트", new_group=True)
    assert r.file_dir.name == "시트" and r.pages == 12 and r.skipped == ["note.txt: 이미지가 아님"]
    assert (r.file_dir / "source" / "시트.pdf").exists()
    assert sorted(p.name for p in (r.file_dir / "source" / "images").iterdir()) == ["sheet1.png", "sheet2.png"]
    meta = json.loads((r.file_dir / "file.json").read_text(encoding="utf-8"))
    fi = meta["from_images"]
    assert {k: fi[k] for k in ("files", "cols", "rows")} == {"files": ["sheet1.png", "sheet2.png"], "cols": 3, "rows": 2}
    assert [sh["file"] for sh in fi["sheets"]] == ["sheet1.png", "sheet2.png"]
    assert [tuple(b) for b in fi["sheets"][0]["boxes"]] == images.cut_sheet(tmp_path / "in" / "sheet1.png", 3, 2).boxes
    g = next(g for g in service.project_overview(info.dir)["groups"] if g["name"] == "이미지 카드")
    assert g["files"][0]["pages"] == 12
    # 이미지에서 온 쪽도 PDF와 같은 흐름: 영역을 정하면 지우기가 된다
    service.save_region_template(g["dir"], {"regions": [
        {"name": "이름", "rect": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.15}}]})
    er = service.erase(r.file_dir, "1-2")
    assert er.ok and (r.file_dir / "cleared" / "cleared.pdf").exists()
    with pytest.raises(ValueError):
        service.add_images(info.dir, [tmp_path / "in" / "note.txt"], "이미지 카드")


def test_add_files_dialog_with_images(tmp_path):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from app.qt.dialogs import AddFilesDialog

    QApplication.instance() or QApplication([])
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    _grid(tmp_path / "in" / "s.png")
    dlg = AddFilesDialog(info.dir, files=[str(tmp_path / "in" / "s.png")])
    assert not dlg.img_box.isHidden()
    dlg.r_grid.setChecked(True)
    dlg.cols.setValue(3)
    dlg.rows.setValue(2)
    assert dlg.layout_choice() == (3, 2) and "카드 약 6장" in dlg.img_info.text()
    dlg.new_name.setText("카드")
    dlg.img_name.setText("시트")
    import PySide6.QtWidgets as W

    orig = W.QMessageBox.information
    W.QMessageBox.information = staticmethod(lambda *a, **k: W.QMessageBox.StandardButton.Ok)
    try:
        dlg.accept()
    finally:
        W.QMessageBox.information = orig
    assert dlg.result_info["added"] and "카드 6장" in dlg.result_info["warnings"][0]
    assert service.project_overview(info.dir)["groups"][0]["files"][0]["pages"] == 6


def test_cut_sheet_white_edged_cards_on_gray_scan_background(tmp_path):
    """가장자리가 흰 카드(255)가 잡음 있는 회색 바탕(232) 위에 있어도 카드 경계로 자른다 (Items 카드 같은 경우)."""
    rng = np.random.default_rng(1)
    W, H, cw, ch, gap, m = 50 * 2 + 3 * 240 + 2 * 14, 50 * 2 + 2 * 340 + 14, 240, 340, 14, 50
    bg = (np.full((H, W, 3), 232) + rng.integers(-6, 7, (H, W, 3))).clip(0, 255).astype(np.uint8)
    im = Image.fromarray(bg)
    d = ImageDraw.Draw(im)
    truth = []
    for i in range(6):
        x, y = m + (i % 3) * (cw + gap), m + (i // 3) * (ch + gap)
        d.rectangle([x, y, x + cw - 1, y + ch - 1], fill=(255, 255, 255))  # 흰 카드
        d.rectangle([x + 60, y + 120, x + 180, y + 220], fill=COLORS[i])  # 가운데 그림만 색
        truth.append((x, y, x + cw, y + ch))
    im.save(tmp_path / "scan.jpg", quality=90)
    cut = images.cut_sheet(tmp_path / "scan.jpg", 3, 2)
    assert cut.gutters_found
    assert max(abs(a - b) for bx, tb in zip(cut.boxes, truth) for a, b in zip(bx, tb)) <= 3


# ---------------------------------------------------------------- 결과를 원래 배열로 다시 합치기
def _cards_pdf(path: Path, colors: list[tuple[int, int, int]], size_pt=(91.2, 103.2)) -> Path:
    """쪽마다 한 색으로 칠한 PDF (한글 넣기 결과 대신)."""
    doc = pymupdf.open()
    for c in colors:
        pg = doc.new_page(width=size_pt[0], height=size_pt[1])
        pg.draw_rect(pg.rect, color=None, fill=[v / 255 for v in c])
    doc.save(path)
    doc.close()
    return path


def test_compose_sheets_keeps_size_layout_and_untouched_cards(tmp_path):
    boxes = _grid(tmp_path / "img" / "sheet.png")
    orig = np.asarray(Image.open(tmp_path / "img" / "sheet.png").convert("RGB"))
    lay = images.cut_sheet(tmp_path / "img" / "sheet.png", 3, 2).layout()
    pdf = _cards_pdf(tmp_path / "t.pdf", [(0, 0, 0), (250, 250, 0)])
    outs = images.compose_sheets([lay], tmp_path / "img", pdf, [1, 3], tmp_path / "out")  # 2·4·5·6쪽은 뺀 쪽
    assert [o.name for o in outs] == ["sheet.png"]
    res = Image.open(outs[0])
    assert res.size == (orig.shape[1], orig.shape[0]) and round(res.info["dpi"][0]) == 300
    arr = np.asarray(res.convert("RGB"))

    def inside(b):
        return arr[b[1] + 5:b[3] - 5, b[0] + 5:b[2] - 5].reshape(-1, 3)

    assert (inside(boxes[0]) == 0).all()  # 1쪽 → 1번째 칸
    assert (inside(boxes[2]) == (250, 250, 0)).all()  # 3쪽 → 3번째 칸
    for k in (1, 3, 4, 5):  # 결과가 없는 칸은 원본 그대로
        b = boxes[k]
        assert (arr[b[1]:b[3], b[0]:b[2]] == orig[b[1]:b[3], b[0]:b[2]]).all()
    assert (arr[:boxes[0][1]] == orig[:boxes[0][1]]).all()  # 바깥 여백·틈도 그대로
    gap = slice(boxes[0][2], boxes[1][0])
    assert (arr[:, gap] == orig[:, gap]).all()


def test_compose_sheets_jpg_and_multi_sheet_order(tmp_path):
    _grid(tmp_path / "img" / "a.jpg", cols=2, rows=1, fmt="JPEG")
    _grid(tmp_path / "img" / "b.png", cols=2, rows=1)
    lays = [images.cut_sheet(tmp_path / "img" / n, 2, 1).layout() for n in ("a.jpg", "b.png")]
    pdf = _cards_pdf(tmp_path / "t.pdf", [(0, 0, 0)] * 4)
    outs = images.compose_sheets(lays, tmp_path / "img", pdf, [1, 2, 3, 4], tmp_path / "out")
    assert [o.name for o in outs] == ["a.jpg", "b.png"]
    b = lays[1]["boxes"][1]  # 4쪽 = 두 번째 시트의 두 번째 칸
    arr = np.asarray(Image.open(outs[1]).convert("RGB"))
    assert (arr[b[1] + 5:b[3] - 5, b[0] + 5:b[2] - 5] == 0).all()


def test_compose_sheets_rejects_changed_source(tmp_path):
    _grid(tmp_path / "img" / "s.png", cols=2, rows=1)
    lay = images.cut_sheet(tmp_path / "img" / "s.png", 2, 1).layout()
    lay["size"] = [10, 10]
    with pytest.raises(ValueError, match="크기"):
        images.compose_sheets([lay], tmp_path / "img", _cards_pdf(tmp_path / "t.pdf", [(0, 0, 0)]), [1],
                              tmp_path / "out")


def test_image_sheet_layouts_old_project_recut(tmp_path):
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    _grid(tmp_path / "in" / "s.png")
    r = service.add_images(info.dir, [tmp_path / "in" / "s.png"], "이미지", cols=3, rows=2, new_group=True)
    saved = service.image_sheet_layouts(r.file_dir)
    meta = workspace.file_settings(r.file_dir)
    del meta["from_images"]["sheets"]  # 예전 프로젝트: 가로×세로만 남아 있다
    workspace._write(r.file_dir / "file.json", meta)
    assert service.image_sheet_layouts(r.file_dir) == saved
    assert service.image_sheet_layouts(tmp_path) == []  # 이미지로 넣지 않은 파일


def test_compose_writes_sheet_images_for_image_input(tmp_path):
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    _grid(tmp_path / "in" / "s.png")
    r = service.add_images(info.dir, [tmp_path / "in" / "s.png"], "이미지", cols=3, rows=2, new_group=True)
    g = next(g for g in service.project_overview(info.dir)["groups"] if g["name"] == "이미지")
    service.save_region_template(g["dir"], {"regions": [
        {"name": "이름", "rect": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.15}}]})
    assert service.erase(r.file_dir, "1-6").ok
    res = service._compose_with_cards(r.file_dir, {}, None, None)
    assert res.state == "done" and res.sheet_error is None
    assert [p.name for p in res.sheet_images] == ["s.png"]
    assert res.sheet_images[0] == r.file_dir / "translated" / "images" / "s.png"
    assert Image.open(res.sheet_images[0]).size == Image.open(tmp_path / "in" / "s.png").size

