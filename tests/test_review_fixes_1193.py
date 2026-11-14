"""1.16~1.19.2 코드 리뷰(2차)에서 나온 문제 (1.19.3)."""

from __future__ import annotations

import json
import os

import pymupdf
import pytest

from app import icons, service
from tests.test_batch_1180 import _changed_project
from tests.test_icons import _add_run_partner, _project

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

FONT = "fonts/notosanskr/NotoSansKR-Medium.ttf"


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _page(fd, page=1):
    return fd / "extract" / "pages" / f"page-{page:03d}.json"


def _set_cell(fd, page, region, **kw) -> None:
    p = _page(fd, page)
    d = json.loads(p.read_text(encoding="utf-8"))
    d["regions"][region].update(kw)
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- 1. 마지막이 보통 읽기여도 최근 정밀 읽기를 보여 준다
def test_extracted_cells_keeps_latest_opus_reread(tmp_path):
    info, fd = _project(tmp_path, "rr", pages=1)
    _set_cell(fd, 1, "본문", rereads=[
        {"model": "claude-opus-5-5", "source": "OLD-OPUS"},
        {"model": "claude-opus-5-5", "source": "NEW-OPUS"},
        {"model": "claude-sonnet-5-5", "source": "SONNET"},
    ])
    c = next(c for c in service.extracted_cells(fd) if c["page"] == 1 and c["region"] == "본문")
    re_src = [r["source"] for r in c["readings"] if r["reread"]]
    assert re_src == ["NEW-OPUS", "SONNET"]
    from app.qt.step_common import precise_index
    assert c["readings"][precise_index(c["readings"])]["source"] == "NEW-OPUS"


def test_extracted_cells_last_opus_shows_one(tmp_path):
    info, fd = _project(tmp_path, "rr1", pages=1)
    _set_cell(fd, 1, "본문", rereads=[{"model": "claude-sonnet-5-5", "source": "S"},
                                     {"model": "claude-opus-5-5", "source": "O"}])
    c = next(c for c in service.extracted_cells(fd) if c["page"] == 1 and c["region"] == "본문")
    assert [r["source"] for r in c["readings"] if r["reread"]] == ["O"]


# ---------------------------------------------------------------- 2. 위아래 [[?]]가 같은 자리: 첫 자리도 사전으로 자동으로 바꾸지 않는다
def test_vertical_shared_box_first_is_not_auto_matched(tmp_path, monkeypatch):
    info, fd = _project(tmp_path, "vauto", pages=1)
    _set_cell(fd, 1, "본문", source="AB[[?]]\n[[?]]CD")
    monkeypatch.setattr(icons, "locate", lambda img, text, ti: ((10, 10, 30, 30), ""))
    monkeypatch.setattr(icons, "best_known", lambda sh, known, num: ("coin", 0.99))
    r = service.find_icons(info.dir, suggest=False)
    assert "AB[[?]]\n[[?]]CD" == json.loads(_page(fd).read_text(encoding="utf-8"))["regions"]["본문"]["source"]
    occ = sorted((o for o in icons.load_scan(info.game_dir / "work" / "icons").occurrences if o.page == 1),
                 key=lambda o: o.order)
    assert len(occ) == 2
    assert occ[0].box and not occ[0].match and occ[0].hint == "coin"  # 첫 자리는 보이고, 이름은 제안만
    assert [o.index for o in occ] == [0, 1]
    assert not occ[1].box
    assert "coin" not in (getattr(r, "auto", None) or {})


# ---------------------------------------------------------------- 3. 이미 있는 이름: 숫자에 뜻이 있으면 떼지 않는다
@pytest.mark.parametrize("name,taken,want", [
    ("coin", {"coin", "coin_2"}, "coin_3"),
    ("coin_2", {"coin", "coin_2"}, "coin_3"),
    ("dice_6", {"dice", "dice_6"}, "dice_6_2"),
    ("dice_6", {"dice_6"}, "dice_6_2"),
    ("gold_2", {"gold_2"}, "gold_2_2"),
    ("coin_3", {"coin", "coin_2", "coin_3"}, "coin_4"),
])
def test_free_name(name, taken, want):
    assert icons.free_name(name, taken) == want


# ---------------------------------------------------------------- 6. 한도로 멈추면 남은 칸을 다음에 또 자동으로 읽지 않는다
def test_reread_paused_skips_remaining_cells(tmp_path, monkeypatch):
    info, fd = _changed_project(tmp_path, "pause")
    pending = service.icon_reread_pending([fd])
    assert pending
    from app.service import _cells_icons
    monkeypatch.setattr(_cells_icons, "reread_region",
                        lambda *a, **k: service.RereadResult("paused", reason="한도", retry_after="2026-11-14 20:00:00"))
    r = service.reread_cells(pending, client=object())
    assert r["state"] == "paused" and r["done"] == 0
    assert service.icon_reread_pending([fd]) == []
    cell = json.loads(_page(fd).read_text(encoding="utf-8"))["regions"]["본문"]
    assert cell.get("icon_changed")  # 원문 확인에는 ‘아이콘 자리가 바뀜’으로 보인다


# ---------------------------------------------------------------- 7. 아이콘 아님·합친 자리는 회색 ‘정한 자리’로 보인다
def test_place_info_marks_settled_not_icon(tmp_path):
    info, fd = _project(tmp_path, "set", pages=1)
    host, extra = _add_run_partner(info, fd)
    service.set_icon_not_icon(info.dir, extra)
    p = service.icon_place(info.dir, host)
    settled = [m for m in p["marks"] if m.get("settled")]
    assert [m["occ"] for m in settled] == [extra] and settled[0]["settled"] == "not_icon"
    assert icons.overlapping_occ(p["marks"], settled[0]["box"]) == []  # 흡수 후보는 아니다


def test_place_info_marks_settled_absorbed(tmp_path):
    info, fd = _project(tmp_path, "sab", pages=1)
    host, extra = _add_run_partner(info, fd)
    service.absorb_icon_run(info.dir, host, [extra])
    p = service.icon_place(info.dir, host)
    assert all(m.get("settled") == "absorbed" for m in p["marks"] if m.get("guess"))


# ---------------------------------------------------------------- 4·5. 영역 지정: 그리기 모드에서 옆 영역 가장자리를 잡지 않음, 커서
def _scene(qapp):
    from PySide6.QtCore import QRectF
    from PySide6.QtWidgets import QGraphicsView

    from app.qt.region_editor import PageScene, RegionItem

    sc = PageScene()
    sc.page_rect = QRectF(0, 0, 400, 300)
    sc.setSceneRect(sc.page_rect)
    v = QGraphicsView(sc)
    v.resize(800, 600)
    it = RegionItem(QRectF(50, 20, 40, 30), {"name": "본문", "kind": "text"})
    sc.addItem(it)
    v.show()
    qapp.processEvents()
    return sc, v, it


def test_add_mode_draws_new_region_beside_unselected(qapp):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest

    from app.qt.region_editor import RegionItem

    sc, v, it = _scene(qapp)
    sc.add_kind = "text"
    near = QPointF(92, 25)  # 오른쪽 변 바로 밖 (화면 EDGE px 안)
    assert sc.region_at(near) is it and sc.edge_outside(near) is None
    start = v.mapFromScene(near)
    end = v.mapFromScene(QPointF(150, 80))
    QTest.mousePress(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(v.viewport(), end)
    QTest.mouseRelease(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
    qapp.processEvents()
    assert len([x for x in sc.items() if isinstance(x, RegionItem)]) == 2  # 새 영역을 그렸다
    assert abs(it.rect().width() - 40) < 0.5  # 옆 영역은 그대로
    v.close()


def test_edge_outside_shows_resize_cursor(qapp):
    from PySide6.QtCore import QPointF, Qt

    sc, v, it = _scene(qapp)
    # 마우스 이동 이벤트는 다른 테스트 창이 남아 있으면 전달되지 않을 수 있어, 장면이 이동마다 부르는 함수를 직접 부른다
    sc._update_edge_cursor(QPointF(92, 35))
    assert v.viewport().cursor().shape() == Qt.CursorShape.SizeHorCursor
    sc._update_edge_cursor(QPointF(200, 200))
    assert v.viewport().cursor().shape() == Qt.CursorShape.ArrowCursor
    sc.add_kind = "text"  # 그리기 모드: 고르지 않은 영역 옆에서는 크기 조절 커서를 보이지 않는다
    sc._update_edge_cursor(QPointF(92, 35))
    assert v.viewport().cursor().shape() == Qt.CursorShape.ArrowCursor
    v.close()


# ---------------------------------------------------------------- 8. 외곽선 줄: 아이콘 왼쪽 글자는 아이콘보다 먼저 그린다
def test_stroked_fill_before_inline_icon(tmp_path):
    from app.typeset import TextStyle, typeset_region

    icdir = tmp_path / "icons"
    icdir.mkdir()
    pm = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 16, 16), False)
    pm.set_rect(pm.irect, (200, 30, 30))
    pm.save(icdir / "coin.png")
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=60)
    st = TextStyle(font_file=FONT, size_pt=10, color="#545358", stroke_color="#f6eff3", stroke_width=0.3,
                   icons_dir=str(icdir))
    res = typeset_region(page, pymupdf.Rect(10, 10, 290, 50), "가나 [[coin]] 다라", st)
    assert not res.overflow_text
    streams = [doc.xref_stream(x) for x in page.get_contents()]
    fills = [i for i, s in enumerate(streams) if b"BT" in s and b"ActualText ()" not in s]
    imgs = [i for i, s in enumerate(streams) if b" Do" in s]
    assert fills and imgs
    assert fills[0] < imgs[0] < fills[-1]  # 왼쪽 글자 채움 → 아이콘 → 오른쪽 글자 채움 (읽는 순서)
