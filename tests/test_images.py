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


def _composed_image_file(tmp_path):
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    _grid(tmp_path / "in" / "s.png")
    r = service.add_images(info.dir, [tmp_path / "in" / "s.png"], "이미지", cols=3, rows=2, new_group=True)
    g = next(g for g in service.project_overview(info.dir)["groups"] if g["name"] == "이미지")
    service.save_region_template(g["dir"], {"regions": [
        {"name": "이름", "rect": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.15}}]})
    assert service.erase(r.file_dir, "1-6").ok
    assert service._compose_with_cards(r.file_dir, {}, None, None).state == "done"
    return r.file_dir


def test_import_edited_pdf_makes_sheet_images_and_survives_recompose(tmp_path):
    """고친 한글 PDF를 넣으면 원래 배열 이미지로 합쳐 edited/에 둔다 (받은 PDF는 그대로, 다시 한글 넣기를 해도 남음)."""
    fd = _composed_image_file(tmp_path)
    assert service.can_import_edited(fd) and service.edited_sheet_images(fd) == []
    edited = _cards_pdf(tmp_path / "edited_by_me.pdf", [(0, 0, 250)] * 6,
                        size_pt=tuple(pymupdf.open(fd / "translated" / "translated.pdf")[0].rect[2:]))
    before = edited.read_bytes()
    r = service.import_edited_pdf(fd, edited)
    assert r.warnings == [] and r.pdf == fd / "edited" / "edited.pdf"
    assert [p.name for p in r.sheet_images] == ["s.png"] and service.edited_sheet_images(fd) == r.sheet_images
    assert edited.read_bytes() == before
    arr = np.asarray(Image.open(r.sheet_images[0]).convert("RGB"))
    lay = service.image_sheet_layouts(fd)[0]
    for b in lay["boxes"]:  # 모든 카드 자리가 고친 PDF의 색
        assert (arr[b[1] + 5:b[3] - 5, b[0] + 5:b[2] - 5] == (0, 0, 250)).all()
    assert Image.open(r.sheet_images[0]).size == Image.open(tmp_path / "in" / "s.png").size
    service._compose_with_cards(fd, {}, None, None)  # 한글 넣기를 다시 해도
    assert service.edited_sheet_images(fd) == r.sheet_images and (fd / "edited" / "edited.pdf").exists()


def test_import_edited_pdf_rejects_page_count_and_warns_on_shape(tmp_path):
    fd = _composed_image_file(tmp_path)
    w, h = pymupdf.open(fd / "translated" / "translated.pdf")[0].rect[2:]
    with pytest.raises(ValueError, match="쪽 수"):
        service.import_edited_pdf(fd, _cards_pdf(tmp_path / "five.pdf", [(0, 0, 0)] * 5, size_pt=(w, h)))
    assert service.edited_sheet_images(fd) == []
    r = service.import_edited_pdf(fd, _cards_pdf(tmp_path / "wide.pdf", [(0, 0, 0)] * 6, size_pt=(w * 2, h)))
    assert r.warnings and "비율" in r.warnings[0] and len(r.sheet_images) == 1


def test_can_import_edited_false_without_result_or_images(tmp_path):
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    _grid(tmp_path / "in" / "s.png")
    r = service.add_images(info.dir, [tmp_path / "in" / "s.png"], "이미지", cols=3, rows=2, new_group=True)
    assert not service.can_import_edited(r.file_dir)  # 한글 넣기 전
    with pytest.raises(ValueError, match="한글 넣기"):
        service.import_edited_pdf(r.file_dir, tmp_path / "x.pdf")


def test_viewer_import_edited_buttons(tmp_path, monkeypatch):
    """결과 보기: 배열 이미지 파일이면 ‘고친 PDF 넣기…’가 보이고, 넣은 뒤 ‘고친 배열 이미지’가 보여 그것을 연다."""
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

    QApplication.instance() or QApplication([])
    from app.qt import results, worker

    fd = _composed_image_file(tmp_path)
    v = results.ResultViewer([("f", fd / "translated")])
    assert v.import_btn.isVisibleTo(v) and not v.edited_btn.isVisibleTo(v)
    size = tuple(pymupdf.open(fd / "translated" / "translated.pdf")[0].rect[2:])
    pdf = _cards_pdf(tmp_path / "mine.pdf", [(0, 0, 250)] * 6, size_pt=size)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(pdf), "")))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(worker, "run_with_dialog",
                        lambda parent, title, job, done, failed=None, **k: done(job.fn(*job.args, **job.kwargs)))
    opened = []
    monkeypatch.setattr(results, "_open", opened.append)
    v.import_edited()
    assert v.edited_btn.isVisibleTo(v) and opened == [fd / "edited" / "images" / "s.png"]



def test_cut_sheet_follows_touching_card_edges_off_grid(tmp_path):
    """스캔처럼: 둥근 귀 카드 3×2가 회색 바탕 위에 거의 붙어 있고 고르게 놓이지 않음(줄·열마다 경계가 다름).
    네 카드가 만나는 점(귀 사이 바탕)을 따라 자른다. 카드 안 인쇄 선(테두리)에 끌려가지 않는다."""
    W, H = 900, 1200
    im = Image.new("RGB", (W, H), (130, 130, 130))  # 스캐너 덮개
    d = ImageDraw.Draw(im)
    vx = [[312, 606], [310, 604]]  # 줄마다 세로 경계 (같은 간격이면 300, 600)
    hy = [588, 593, 585]  # 열마다 가로 경계 (같은 간격이면 600)
    rng = np.random.default_rng(3)
    for r in range(2):
        for c in range(3):
            x0 = 0 if c == 0 else vx[r][c - 1]  # 틈 없이 붙어 있다 (바탕은 둥근 귀 사이로만 보인다)
            x1 = W if c == 2 else vx[r][c]
            y0 = 0 if r == 0 else hy[c]
            y1 = H if r == 1 else hy[c]
            d.rounded_rectangle([x0, y0, x1 - 1, y1 - 1], radius=14, fill=(205, 198, 175))
            d.rectangle([x0 + 8, y0 + 8, x1 - 9, y1 - 9], outline=(60, 50, 40), width=3)  # 인쇄 테두리
            for _ in range(40):  # 그림
                cx, cy = int(rng.integers(x0 + 60, x1 - 60)), int(rng.integers(y0 + 60, y1 - 60))
                d.ellipse([cx - 12, cy - 12, cx + 12, cy + 12], fill=tuple(int(v) for v in rng.integers(40, 200, 3)))
    grain = rng.normal(0, 9, (H, W, 1))  # 종이 결·스캔 잡음
    im = Image.fromarray(np.clip(np.asarray(im, np.float32) + grain, 0, 255).astype(np.uint8))
    rgb = np.asarray(im)
    # 네 카드가 만나는 점: 둥근 귀 사이 바탕
    jx, jy = images._junction(rgb, 300, 600, 22, 40)
    assert 304 <= jx <= 313 and 586 <= jy <= 594
    # 같은 간격(300·600)에서 시작해 이웃 카드 사이마다 따로 고친 경계
    vx, hy, moved, missing = images._refine_cuts(rgb, [0, 300, 300, 600, 600, 900], [0, 600, 600, 1200], 3, 2)
    assert missing == 0 and moved > 0
    assert all(abs(vx[r][1] - t) <= 7 for r, t in ((0, 312), (1, 310)))
    assert all(abs(vx[r][2] - t) <= 7 for r, t in ((0, 606), (1, 604)))
    assert all(abs(hy[c][1] - t) <= 7 for c, t in enumerate([588, 593, 585]))
    assert vx[0][0] == 0 and vx[0][3] == 900 and hy[0][0] == 0 and hy[0][2] == 1200  # 바깥 끝은 그대로

def _scan_with_extras(path: Path, n_cards: int = 9, card=(300, 420)) -> list[tuple[int, int, int, int]]:
    """스캔처럼: 흰 바탕 왼쪽에 자(눈금·숫자), 아래쪽은 빈 종이, 카드는 3×3 자리에 n장(왼쪽 위부터, 틈 없이)."""
    rng = np.random.default_rng(5)
    left, top = 160, 0
    W, H = left + 3 * card[0] + 20, 3 * card[1] + 600
    im = Image.new("RGB", (W, H), (214, 222, 220))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 200, 110, 900], fill=(200, 205, 203))  # 자
    for y in range(210, 900, 8):
        d.line([(0, y), (40 if y % 40 else 80, y)], fill=(20, 20, 20), width=3)
    for y in range(240, 900, 80):
        d.rectangle([85, y, 105, y + 25], fill=(20, 20, 20))  # 숫자
    for y in range(950, H, 20):  # 모눈종이
        d.line([(0, y), (W, y)], fill=(60, 60, 60), width=2)
    for x in range(0, W, 20):
        d.line([(x, 950), (x, H)], fill=(60, 60, 60), width=2)
    boxes = []
    for i in range(n_cards):
        r, c = divmod(i, 3)
        x0, y0 = left + c * card[0], top + r * card[1]
        d.rounded_rectangle([x0, y0, x0 + card[0] - 1, y0 + card[1] - 1], radius=14,
                            fill=(170 + 10 * c, 70, 80 + 10 * r))
        d.rectangle([x0 + 30, y0 + 40, x0 + card[0] - 31, y0 + 120], fill=(235, 220, 200))
        for _ in range(12):
            cx, cy = int(rng.integers(x0 + 50, x0 + card[0] - 50)), int(rng.integers(y0 + 150, y0 + card[1] - 40))
            d.ellipse([cx - 10, cy - 10, cx + 10, cy + 10], fill=tuple(int(v) for v in rng.integers(30, 220, 3)))
        boxes.append((x0, y0, x0 + card[0], y0 + card[1]))
    im.save(path)
    return boxes


def test_cut_sheet_ignores_ruler_and_paper_around_cards(tmp_path):
    """자·모눈종이·빈 종이가 같이 찍힌 스캔: 카드가 있는 곳만 잘라 카드 9장."""
    truth = _scan_with_extras(tmp_path / "ruler.png")
    cut = images.cut_sheet(tmp_path / "ruler.png", 3, 3)
    assert len(cut.boxes) == 9
    assert max(abs(a - b) for bx, tb in zip(cut.boxes, truth) for a, b in zip(bx, tb)) <= 12
    assert any("카드가 아닌 부분" in w for w in cut.warnings)


def test_cut_sheet_skips_empty_slots(tmp_path):
    """3×3 자리에 카드 7장만(마지막 줄 2칸 빔): 빈 자리는 쪽을 만들지 않는다."""
    truth = _scan_with_extras(tmp_path / "seven.png", n_cards=7)
    cut = images.cut_sheet(tmp_path / "seven.png", 3, 3)
    assert len(cut.boxes) == 7
    assert max(abs(a - b) for bx, tb in zip(cut.boxes, truth) for a, b in zip(bx, tb)) <= 12
    assert any("7장" in w for w in cut.warnings)


# ---------------------------------------------------------------- 수동으로 영역 지정
def test_manual_cut_reading_order_and_clipping(tmp_path):
    p = tmp_path / "in" / "free.png"
    boxes = _grid(p, cols=3, rows=2)
    drawn = [boxes[4], boxes[0], boxes[5], boxes[2], boxes[1], boxes[3],  # 그린 순서는 뒤죽박죽
             (-50, -50, 3, 3)]  # 이미지 밖·너무 작은 상자는 뺀다
    cut = images.manual_cut(p, drawn)
    assert cut.boxes == boxes and "1개는 뺐습니다" in cut.warnings[0]
    # 줄이 조금 비뚤어도 같은 줄로 본다
    assert images.reading_order([(200, 12, 300, 112), (10, 0, 110, 100), (10, 200, 110, 300)]) == \
        [(10, 0, 110, 100), (200, 12, 300, 112), (10, 200, 110, 300)]


def test_add_images_with_manual_boxes(tmp_path):
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    a, b = tmp_path / "in" / "a.png", tmp_path / "in" / "b.png"
    ba = _grid(a, cols=3, rows=2)
    _grid(b, cols=3, rows=2)
    manual = {str(a): [ba[0], ba[4]], str(b): [ba[1]]}
    r = service.add_images(info.dir, [a, b], "카드", name="시트", new_group=True, boxes=manual)
    assert r.pages == 3
    fi = json.loads((r.file_dir / "file.json").read_text(encoding="utf-8"))["from_images"]
    assert fi["manual"] is True and [tuple(x) for x in fi["sheets"][0]["boxes"]] == [ba[0], ba[4]]
    doc = pymupdf.open(next((r.file_dir / "source").glob("*.pdf")))
    w0 = doc[0].rect.width
    assert abs(w0 - (ba[0][2] - ba[0][0]) * 72 / 300) < 0.5  # 그린 상자 크기 그대로 한 쪽


def test_card_boxes_dialog_draw_inherit_and_add(tmp_path):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from app.qt.card_boxes import CardBoxesDialog
    from app.qt.dialogs import AddFilesDialog

    QApplication.instance() or QApplication([])
    a, b = tmp_path / "in" / "a.png", tmp_path / "in" / "b.png"
    ba = _grid(a, cols=3, rows=2)
    _grid(b, cols=3, rows=2)
    dlg = CardBoxesDialog([str(a), str(b)])
    for box in (ba[3], ba[0]):  # 화면에서 그린 것처럼
        dlg._add_item(box)
        dlg._changed()
    assert [it.data["name"] for it in sorted(dlg._items(), key=lambda i: i.rect().top())] == ["1", "2"]
    dlg.list.setCurrentRow(1)  # 두 번째 이미지: 자리를 안 그렸으면 앞 이미지 자리(점선)
    assert len(dlg._items()) == 2 and all(getattr(i, "inherited", False) for i in dlg._items())
    assert "앞 이미지와 같은 자리" in dlg.info.text()
    res = dlg.result()
    assert res[str(a)] == [ba[0], ba[3]] and res[str(b)] == [ba[0], ba[3]]
    dlg.clear_current()
    dlg._add_item(ba[5])
    dlg._changed()
    assert dlg.result()[str(b)] == [ba[5]] and dlg.result()[str(a)] == [ba[0], ba[3]]

    info, _ = workspace.create_project("p", "G", root=tmp_path)
    add = AddFilesDialog(info.dir, files=[str(a), str(b)])
    add.r_manual.setChecked(True)
    assert add.b_draw.isEnabled() and add.b_preview.isHidden() and "카드 자리 그리기" in add.img_info.text()
    add.manual_boxes = dlg.result()
    add._sync()
    assert "카드 3장" in add.img_info.text()
    add.new_name.setText("카드")
    import PySide6.QtWidgets as W

    orig = W.QMessageBox.information
    W.QMessageBox.information = staticmethod(lambda *a, **k: W.QMessageBox.StandardButton.Ok)
    try:
        add.accept()
    finally:
        W.QMessageBox.information = orig
    assert add.result_info["added"] and service.project_overview(info.dir)["groups"][0]["files"][0]["pages"] == 3


def test_manual_boxes_with_forward_slash_paths_and_empty_image(tmp_path):
    """파일 고르기 창의 경로(C:/x/a.png)로 그린 자리도 쓰고, 빈 목록 이미지에서는 카드를 만들지 않는다."""
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    a, b = tmp_path / "in" / "a.png", tmp_path / "in" / "b.png"
    ba = _grid(a, cols=3, rows=2)
    _grid(b, cols=3, rows=2)
    fwd = {a.as_posix(): [ba[0], ba[1]], b.as_posix(): []}
    r = service.add_images(info.dir, [str(a), str(b)], "카드", name="시트", new_group=True, boxes=fwd)
    assert r.pages == 2  # a에서 2장, b에서는 0장 (통째로 한 장이 아님)


def test_card_boxes_dialog_delete_inherited_sticks_and_click_keeps_inheriting(tmp_path):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from app.qt.card_boxes import CardBoxesDialog

    QApplication.instance() or QApplication([])
    a, b, c = (tmp_path / "in" / f"{n}.png" for n in "abc")
    ba = _grid(a, cols=3, rows=2)
    _grid(b, cols=3, rows=2)
    _grid(c, cols=3, rows=2)
    dlg = CardBoxesDialog([str(a), str(b), str(c)])
    dlg._add_item(ba[0])
    dlg._changed()
    dlg.list.setCurrentRow(1)
    dlg._changed()  # 누르기만 함 (옮기지 않음): 앞 이미지 자리를 계속 따른다
    assert str(b) not in dlg.boxes and all(i.inherited for i in dlg._items())
    for it in dlg._items():
        it.setSelected(True)
    dlg.delete_selected()  # 앞 이미지에서 빌려 온 자리를 지움: 이 이미지에는 카드 없음
    assert dlg.boxes[str(b)] == [] and "카드를 만들지 않음" in dlg.info.text()
    dlg.list.setCurrentRow(2)
    dlg.list.setCurrentRow(1)  # 돌아와도 점선이 다시 생기지 않는다
    assert dlg._items() == []
    res = dlg.result()
    assert res[str(b)] == [] and res[str(c)] == [ba[0]]  # c는 b를 건너뛰고 a 자리를 따른다


def test_noise_line_in_bottom_margin_is_not_card_area(tmp_path):
    """카드가 틈 없이 붙은 3×3 + 아래 빈 여백에 스캔 잡음(가는 세로줄·점): 여백까지 카드로 보고 줄이 밀리면 안 된다
    (공주와 기사 시트: 둘째·셋째 줄 카드가 아래로 밀려 잘림)."""
    cw, ch, top, left = 300, 420, 0, 90
    W, H = left + 3 * cw, top + 3 * ch + 230
    im = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(im)
    truth = []
    for r in range(3):
        for c in range(3):
            x, y = left + c * cw, top + r * ch
            d.rectangle([x, y, x + cw - 1, y + ch - 1], fill=COLORS[(r * 3 + c) % len(COLORS)])
            d.rectangle([x + 2, y + 2, x + cw - 3, y + ch - 3], outline=(250, 250, 250), width=2)  # 카드 끝 밝은 선
            truth.append((x, y, x + cw, y + ch))
    d.rectangle([W - 8, 3 * ch - 60, W - 6, H - 1], fill=(20, 20, 20))  # 여백까지 내려오는 가는 스캔 줄
    d.rectangle([W // 2, 3 * ch + 120, W // 2 + 2, 3 * ch + 122], fill=(60, 60, 60))  # 작은 점
    p = tmp_path / "packed.png"
    im.save(p)
    cut = images.cut_sheet(p, 3, 3)
    assert len(cut.boxes) == 9
    for b, t in zip(cut.boxes, truth):
        assert abs(b[1] - t[1]) <= 12 and abs(b[3] - t[3]) <= 12, (b, t)  # 줄이 아래 여백 쪽으로 밀리지 않는다
    assert max(b[3] for b in cut.boxes) <= 3 * ch + 12


def test_refined_cut_that_breaks_card_size_is_reverted():
    """만나는 점을 카드 안 흰 그림에서 잘못 잡아 경계가 크게 밀리면(이웃 칸 크기 어긋남) 같은 간격 자리로 되돌린다."""
    nominal = [0, 100, 200, 300]
    lines = [[0, 101, 199, 300],  # 조금 옮김: 그대로
             [0, 100, 170, 300],  # 30 밀림: 되돌림
             [0, 125, 225, 300]]  # 둘 다 밀림: 둘 다 되돌림
    n = images._revert_odd_cuts(lines, nominal, 100.0)
    assert lines[0] == [0, 101, 199, 300]
    assert lines[1] == [0, 100, 200, 300]
    assert lines[2] == [0, 100, 200, 300]
    assert n == 3


def _grid_pdf(path: Path, pages: int = 2, cols=3, rows=2) -> Path:
    """쪽마다 카드 cols×rows 배열 스캔(그림 한 장, 300 dpi)이 들어 있는 PDF."""
    doc = pymupdf.open()
    for i in range(pages):
        png = path.parent / f"_src{i}.png"
        _grid(png, cols, rows)
        w, h = Image.open(png).size
        page = doc.new_page(width=w * 72 / 300, height=h * 72 / 300)
        page.insert_image(page.rect, filename=str(png))
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path


def test_pdf_to_images_keeps_scan_resolution(tmp_path):
    pdf = _grid_pdf(tmp_path / "in" / "sheets.pdf", pages=2)
    out = images.pdf_to_images(pdf, tmp_path / "pages")
    assert [p.name for p in out] == ["sheets_p001.png", "sheets_p002.png"]
    w, _h = Image.open(out[0]).size
    assert abs(w - Image.open(tmp_path / "in" / "_src0.png").size[0]) <= 2  # 스캔 해상도 그대로
    assert abs(images.image_dpi(out[0]) - 300) < 1
    assert len(images.cut_sheet(out[0], 3, 2).boxes) == 6


def test_add_files_dialog_cuts_pdf_pages_into_cards(tmp_path):
    """PDF 한 쪽에 카드가 여러 장: ‘배열’을 고르면 쪽마다 잘라 카드 한 장이 한 쪽인 파일 하나로 넣는다."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from app.qt.dialogs import AddFilesDialog

    QApplication.instance() or QApplication([])
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    pdf = _grid_pdf(tmp_path / "in" / "sheets.pdf", pages=2)
    dlg = AddFilesDialog(info.dir, files=[str(pdf)])
    assert not dlg.img_box.isHidden()  # PDF만 넣어도 ‘한 장에 카드 몇 장’을 고를 수 있다
    dlg.r_grid.setChecked(True)
    dlg.cols.setValue(3)
    dlg.rows.setValue(2)
    assert "PDF 1개(2쪽)" in dlg.img_info.text() and "카드 약 12장" in dlg.img_info.text()
    dlg.new_name.setText("카드")
    import PySide6.QtWidgets as W

    orig = W.QMessageBox.information
    W.QMessageBox.information = staticmethod(lambda *a, **k: W.QMessageBox.StandardButton.Ok)
    try:
        dlg.accept()
    finally:
        W.QMessageBox.information = orig
    assert dlg.result_info["added"] and "2쪽 → 카드 12장" in dlg.result_info["warnings"][0]
    f = service.project_overview(info.dir)["groups"][0]["files"][0]
    assert f["pages"] == 12
    fd = Path(f["dir"])
    assert (fd / "source" / "images" / "sheets.pdf").exists()  # 원래 PDF도 보관
    assert len(list((fd / "source").glob("*.pdf"))) == 1  # source/ 바로 아래에는 카드 PDF 하나
    st = workspace.file_settings(fd)["from_images"]
    assert st["pdf"] == "sheets.pdf" and len(st["sheets"]) == 2 and st["cols"] == 3
    with pymupdf.open(next((fd / "source").glob("*.pdf"))) as doc:  # 카드 크기 = 원래 PDF의 카드 크기
        assert abs(doc[0].rect.width - 380 * 72 / 300) < 2
    dlg.finished.emit(0)
    assert dlg._tmp is None


def test_add_files_dialog_single_keeps_pdf_as_is(tmp_path):
    """‘한 장에 카드 한 장’이면 PDF는 예전처럼 그대로 넣는다 (그림으로 바꾸지 않음)."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from app.qt.dialogs import AddFilesDialog

    QApplication.instance() or QApplication([])
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    pdf = _grid_pdf(tmp_path / "in" / "sheets.pdf", pages=2)
    dlg = AddFilesDialog(info.dir, files=[str(pdf)])
    dlg.new_name.setText("카드")
    dlg.accept()
    f = service.project_overview(info.dir)["groups"][0]["files"][0]
    assert f["pages"] == 2 and dlg._tmp is None


def test_add_files_dialog_manual_boxes_on_pdf_pages(tmp_path):
    """수동으로 영역 지정도 PDF 쪽 위에 그린 자리대로 자른다."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from app.qt.dialogs import AddFilesDialog

    QApplication.instance() or QApplication([])
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    pdf = _grid_pdf(tmp_path / "in" / "sheets.pdf", pages=2)
    dlg = AddFilesDialog(info.dir, files=[str(pdf)])
    dlg.r_manual.setChecked(True)
    pages = dlg.sheet_files()
    assert len(pages) == 2 and all(p.endswith(".png") for p in pages)
    boxes = _grid(tmp_path / "x.png")  # 같은 배치의 카드 자리
    dlg.manual_boxes = {pages[0]: boxes[:2], pages[1]: boxes[:3]}
    dlg._sync()
    assert "카드 5장" in dlg.img_info.text()
    dlg.new_name.setText("카드")
    import PySide6.QtWidgets as W

    orig = W.QMessageBox.information
    W.QMessageBox.information = staticmethod(lambda *a, **k: W.QMessageBox.StandardButton.Ok)
    try:
        dlg.accept()
    finally:
        W.QMessageBox.information = orig
    f = service.project_overview(info.dir)["groups"][0]["files"][0]
    assert f["pages"] == 5
    assert workspace.file_settings(Path(f["dir"]))["from_images"]["manual"]
