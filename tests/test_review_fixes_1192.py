"""1.16.0~1.18.0·1.19.1 코드 리뷰에서 나온 문제 (1.19.2)."""

from __future__ import annotations

import json
import os

import numpy as np
import pymupdf
import pytest

from app import erase_picture, icons, service
from app.service import _cells_icons
from tests.test_batch_1180 import _changed_project
from tests.test_icons import _add_run_partner, _project

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

FONT = "fonts/notosanskr/NotoSansKR-Medium.ttf"


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _cell(fd, page=1, region="본문") -> dict:
    p = fd / "extract" / "pages" / f"page-{page:03d}.json"
    return json.loads(p.read_text(encoding="utf-8"))["regions"][region]


def _set_cell(fd, page, region, **kw) -> None:
    p = fd / "extract" / "pages" / f"page-{page:03d}.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d["regions"][region].update(kw)
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- 1. 다시 표시해도 앞 메모를 남긴다
def test_mark_icon_changed_again_keeps_earlier_note(tmp_path):
    info, fd = _changed_project(tmp_path, "note")
    icon_note = _cell(fd)["note"]
    assert icon_note.startswith(icons.ICON_CHANGED_NOTE)
    _set_cell(fd, 1, "본문", note=f'사람이 고침 (모델: "AB") / {icon_note}')
    icons.mark_icon_changed(fd, 1, "본문", ["세트로 합침"])
    note = _cell(fd)["note"]
    assert note.startswith('사람이 고침 (모델: "AB") / ')
    assert note.count(icons.ICON_CHANGED_NOTE) == 1 and "세트로 합침" in note


# ---------------------------------------------------------------- 2. 원문 확인 앞 다시 읽기: 실패·취소해도 막히지 않는다
def test_reread_failed_cell_is_not_reread_automatically_again(tmp_path, monkeypatch):
    info, fd = _changed_project(tmp_path, "fail")
    pending = service.icon_reread_pending([fd])
    assert pending

    def fake(fd_, page, region, **kw):
        return service.RereadResult("blocked", reason=f"{_cells_icons.CELL_BLOCKED[0]}: 응답 없음")

    monkeypatch.setattr(_cells_icons, "reread_region", fake)
    r = service.reread_cells(pending, engine="api")
    assert r["failed"] and r["state"] == "done"
    assert service.icon_reread_pending([fd]) == []  # 다음에 원문 확인을 열 때 또 돈을 쓰지 않는다
    c = next(c for c in service.extracted_cells(fd) if c["page"] == 1)
    assert c["uncertain"] and c["icon_changed"]  # 원문 확인에는 여전히 ‘확인 필요’로 보인다


def test_cancelling_pending_reread_still_opens_review(qapp, tmp_path, monkeypatch):
    from app.qt import steps
    from app.qt.main import MainWindow

    info, fd = _changed_project(tmp_path, "cancel")
    win = MainWindow(root=tmp_path)
    win.open_project(info.dir)
    acts = win.project.steps
    opened = []
    monkeypatch.setattr(acts, "_engine", lambda: "api")

    def fake_run(parent, title, job, on_done, on_failed=None, cancellable=True, on_cancelled=None):
        assert on_cancelled is not None
        on_cancelled()

    monkeypatch.setattr(steps.worker, "run_with_dialog", fake_run)
    monkeypatch.setattr(acts, "_open_source_review", lambda files, *a: opened.append(a))
    acts.review_sources(only_check=True)
    assert opened == [(True, None, None)]
    assert service.icon_reread_pending([fd]) == []  # 다시 열 때 같은 다시 읽기를 또 시작하지 않는다


# ---------------------------------------------------------------- 3. 위아래로 붙은 [[?]]가 같은 자리: 첫 자리는 그대로
def test_vertical_placeholders_on_same_box_keep_first_box(tmp_path, monkeypatch):
    info, fd = _project(tmp_path, "vert", pages=1)
    _set_cell(fd, 1, "본문", source="AB[[?]]\n[[?]]CD")
    monkeypatch.setattr(icons, "locate", lambda img, text, ti: ((10, 10, 30, 30), ""))
    service.find_icons(info.dir, suggest=False)
    occ = sorted((o for o in icons.load_scan(info.game_dir / "work" / "icons").occurrences if o.page == 1),
                 key=lambda o: o.order)
    assert len(occ) == 2
    assert occ[0].box and occ[0].reason != icons.SHARED_BOX_REASON  # 예전(1.19.1): 둘 다 위치 못 찾음
    assert not occ[1].box and not occ[1].set_of  # 다른 줄은 자동으로 세트가 되지 않는다


# ---------------------------------------------------------------- 4. 아이콘 아님·합친 자리는 ‘위치 못 찾음’이 아니라 정한 자리로 보인다
def test_place_info_skips_not_icon_partner_guess(tmp_path):
    info, fd = _project(tmp_path, "skip", pages=1)
    host, extra = _add_run_partner(info, fd)
    service.set_icon_not_icon(info.dir, extra)
    p = service.icon_place(info.dir, host)
    assert not [m for m in p["marks"] if m.get("guess") and not m.get("settled")]


def test_place_info_skips_absorbed_partner_guess(tmp_path):
    info, fd = _project(tmp_path, "abs", pages=1)
    host, extra = _add_run_partner(info, fd)
    service.absorb_icon_run(info.dir, host, [extra])
    p = service.icon_place(info.dir, host)
    assert not [m for m in p["marks"] if m.get("guess") and not m.get("settled")]


# ---------------------------------------------------------------- 6·9. 외곽선: 줄마다 스트림 하나, q/Q는 감싸지 않는다
def test_halo_is_one_stream_and_does_not_wrap_page_q(tmp_path):
    from app.typeset import TextStyle, typeset_region

    doc = pymupdf.open()
    page = doc.new_page(width=200, height=60)
    page.insert_text((5, 55), "orig", fontsize=6)  # 감싸지 않은 원래 내용
    doc.save(tmp_path / "a.pdf")
    doc = pymupdf.open(tmp_path / "a.pdf")
    page = doc[0]
    n0 = len(page.get_contents())
    st = TextStyle(font_file=FONT, size_pt=8, color="#545358", stroke_color="#f6eff3", stroke_width=0.3)
    res = typeset_region(page, pymupdf.Rect(10, 10, 190, 40), "민심 보드에 있는", st)
    assert not res.overflow_text
    streams = [doc.xref_stream(x) for x in page.get_contents()]
    halo = [s for s in streams if b"ActualText ()" in s]
    assert len(halo) == 1  # 줄 하나 = 외곽선 스트림 하나 (예전: 글자마다)
    for s in streams:  # q/Q만 있는 감싸기 스트림을 BDC로 감싸지 않는다
        if s.strip() in (b"q", b"Q"):
            assert b"BDC" not in s
    assert len(streams) <= n0 + 4  # 감싸기 q·Q + 외곽선 1 + 채움 1 (예전: 글자마다 두 개씩)
    text = "".join(page.get_text().split())
    assert "orig" in text and text.count("민심보드에있는") == 1


# ---------------------------------------------------------------- 7. 글자 없는 칸 판단은 문구가 아니라 함수로
def test_source_has_no_text_flag():
    assert erase_picture.source_has_no_text("[[coin]]", True)
    assert erase_picture.source_has_no_text("", True)
    assert not erase_picture.source_has_no_text("[[coin]] 2장", True)
    assert not erase_picture.source_has_no_text("[[coin]]", False)
    assert not erase_picture.source_has_no_text(None, True)


def test_region_text_mask_no_text_matches_flag():
    from app.erase_text import TextMask

    m = np.ones((20, 20), bool)
    tm = TextMask(m.copy(), m.copy(), False, None, 3.0, [], [])
    pic, tm2 = erase_picture.region_text_mask(np.zeros((20, 20, 3), np.uint8), (0, 0, 20, 20), None, tm,
                                              "[[coin]]", True)
    assert pic is None and not tm2.mask.any()


# ---------------------------------------------------------------- 8. 확대한 채 상자 바로 밖 가장자리를 끌면 크기가 바뀐다
def test_region_edge_just_outside_rect_resizes_when_zoomed(qapp):
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QGraphicsView

    from app.qt.region_editor import PageScene, RegionItem

    sc = PageScene()
    sc.page_rect = QRectF(0, 0, 400, 300)
    sc.setSceneRect(sc.page_rect)
    v = QGraphicsView(sc)
    v.resize(800, 600)
    v.scale(2, 2)  # 고른 영역은 굵은 테두리(3)가 상자 밖 1.5까지 잡히므로, 그보다 넓은 띠가 생기는 배율
    it = RegionItem(QRectF(50, 20, 40, 30), {"name": "본문", "kind": "text"})
    sc.addItem(it)
    sc.add_kind = "text"  # 그리기 모드여도 고른 영역은 새 영역을 그리지 않고 가장자리를 잡는다
    it.setSelected(True)
    v.show()
    qapp.processEvents()
    v.centerOn(it)
    qapp.processEvents()
    start = v.mapFromScene(QPointF(90, 35)) + type(v.mapFromScene(QPointF(0, 0)))(5, 0)  # 오른쪽 변 바로 밖 5px
    assert not it.contains(it.mapFromScene(v.mapToScene(start)))
    assert sc.edge_outside(v.mapToScene(start)) is it
    end = start + type(start)(40, 0)
    QTest.mousePress(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(v.viewport(), end)
    QTest.mouseRelease(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
    qapp.processEvents()
    assert len([x for x in sc.items() if isinstance(x, RegionItem)]) == 1  # 새 영역을 그리지 않았다
    assert it.rect().width() > 45 and abs(it.rect().left() - 50) < 0.5  # 오른쪽으로 늘었다
    assert sc.mouseGrabberItem() is None  # 마우스를 풀어 주었다
    v.close()


# ---------------------------------------------------------------- 10. 숫자에 뜻이 있는 이름은 번호를 떼지 않는다
def test_suggested_name_keeps_meaningful_number(tmp_path, monkeypatch):
    info, fd = _project(tmp_path, "dice", pages=1)
    service.find_icons(info.dir, suggest=False)
    res = icons.load_scan(info.game_dir / "work" / "icons")
    g = next(iter(res.groups))
    res.groups[g]["suggest"] = "dice_6"
    monkeypatch.setattr(icons, "load_dictionary", lambda _i: {"icons": {"dice_6": {}}})
    monkeypatch.setattr(icons, "_known_shapes", lambda _i: {})
    assert icons.resolve_suggested_name(info, res, g) == "renamed"
    assert res.groups[g]["suggest"] == "dice_6_2"


# ---------------------------------------------------------------- 원문 확인: 다시 읽은 결과는 마지막 것만 표에
def test_source_review_shows_only_last_reread(tmp_path):
    info, fd = _project(tmp_path, "rr", pages=1)
    rr = [{"model": "claude-opus-5-5", "source": f"read {i}", "at": f"2026-11-14 10:0{i}:00"} for i in range(4)]
    _set_cell(fd, 1, "본문", rereads=rr)
    c = next(c for c in service.extracted_cells(fd) if (c["page"], c["region"]) == (1, "본문"))
    again = [x for x in c["readings"] if x["reread"]]
    assert [x["source"] for x in again] == ["read 3"]
    assert len(_cell(fd)["rereads"]) == 4  # 파일의 기록은 그대로
