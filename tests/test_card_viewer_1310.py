"""1.31.0 카드 보기 묶음: 그림 속 숫자 보이기, 다시 찾은 줄 이름 제안, 확대 칸 고정 자리, 카드 단위 영역 고치기.
실제 Claude는 부르지 않는다."""

from __future__ import annotations

import os

import pytest
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication, QMessageBox

from app import service
from app.service import _extract, _source_check as sc

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _no_claude(monkeypatch):
    monkeypatch.setattr(service, "engine_status", lambda: {"message": "시험: 연결 안 함"})
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))


# ---------------------------------------------------------------- 그림 속 숫자 (3쪽 ‘5 [[?]] [[?]]’)
def _row(source, icons):
    return {"source": source, "icons": icons}


def _icon(state, ti, index=None, name="", num_image=None, strip=None, pending=False):
    return {"occ": f"o{ti}", "group": f"g{ti}", "state": state, "ti": ti, "index": ti if index is None else index,
            "name": name, "num_image": num_image, "strip": strip, "pending": pending, "text": ""}


def test_unnamed_icon_number_in_picture_is_not_shown_as_text():
    row = _row("効果\n5 [[?]] [[?]]", [_icon("unnamed", 0, num_image={"text": "5", "side": "before"}),
                                     _icon("named", 1, name="card_2", pending=True)])
    assert service.strip_icon_numbers("効果\n5 [[?]] [[?]]", row) == "効果\n[[?]] [[?]]"  # 읽은 결과 표 (보이기만)
    # 저장되는 글(편집 칸·‘이것 쓰기’)에는 이름 없는 자리의 숫자를 남긴다: 고쳐서 확정해도 숫자가 사라지지 않게
    assert service.preview_source(row) == "効果\n5 [[?]] [[card_2]]"
    assert service.strip_icon_numbers("効果\n5 [[?]] [[?]]", row, named_only=True) == "効果\n5 [[?]] [[?]]"
    # 전각 숫자로 읽은 결과도
    assert service.strip_icon_numbers("効果\n５ [[?]] [[?]]", row) == "効果\n[[?]] [[?]]"


def test_named_icon_strips_its_number_in_readings_but_not_when_dictionary_number_differs():
    named = _row("5 [[?]]", [_icon("named", 0, name="coin_crate_5", strip={"text": "5", "side": "before"},
                                   num_image={"text": "5", "side": "before"}, pending=True)])
    assert service.strip_icon_numbers("5 [[coin_crate_5]]", named) == "[[coin_crate_5]]"
    # 이름 정한 자리는 ‘이것 쓰기’에서도 뺀다 (확정 때 confirm_icons가 같은 숫자를 빼고 icon_numbers에 남긴다)
    assert service.strip_icon_numbers("5 [[coin_crate_5]]", named, named_only=True) == "[[coin_crate_5]]"
    assert service.preview_source(named) == "[[coin_crate_5]]"
    # 사전 그림의 숫자가 다른 이름(2coinup = 2+상자)을 골랐으면 strip이 없다: 원문의 5는 남는다 (확정과 같게)
    other = _row("5 [[?]]", [_icon("named", 0, name="2coinup", strip=None,
                                   num_image={"text": "5", "side": "before"}, pending=True)])
    assert service.strip_icon_numbers("5 [[2coinup]]", other) == "5 [[2coinup]]"


def test_number_strip_leaves_text_alone_when_it_does_not_line_up():
    row = _row("5 [[?]] [[?]]", [_icon("unnamed", 0, num_image={"text": "5", "side": "before"})])
    assert service.strip_icon_numbers("5 [[?]]", row) == "5 [[?]]"  # 아이콘 수가 원문과 다름
    assert service.strip_icon_numbers("3 [[?]] [[?]]", row) == "3 [[?]] [[?]]"  # 다른 숫자
    assert service.strip_icon_numbers("[[?]] 5 [[?]]", row) == "[[?]] 5 [[?]]"  # 숫자가 다른 아이콘 옆


def test_icon_entries_carry_number_in_picture_for_unnamed_rows(monkeypatch):
    from app import icons

    class O:
        id, group, named, manual, added, reason, line, index, ti, box = "o1", "g1", "", False, False, "", 1, 0, 0, []
        file, page, region, png = "g/files/f", 1, "본문", ""
        number = {"text": "5", "side": "before"}
        num_in_image = True
        num = "5"

    res = icons.ScanResult([O()], {"g1": {"rep": "o1", "count": 1, "shape": "s1"}})

    class Info:
        pass

    monkeypatch.setattr(icons, "_occ_dir", lambda info, f: f)
    out = sc._icon_entries(Info(), res, {})
    (e,) = next(iter(out.values()))
    assert e["state"] == "unnamed" and e["num_image"] == {"text": "5", "side": "before"} and e["strip"] is None


# ---------------------------------------------------------------- 다시 찾은 줄의 이름 제안
def test_unasked_groups_are_unnamed_rows_never_asked():
    from app import icons

    class O:
        def __init__(self, i, png="x.png"):
            self.id, self.png = i, png

    occ = [O(f"o{i}") for i in range(1, 8)] + [O("o8", png="")]
    res = icons.ScanResult(occ, {
        "g1": {"rep": "o1"},  # 그림 있음·이름 없음·물은 적 없음 -> 묻는다
        "g2": {"rep": "o2", "suggest": "coin"},  # 이미 제안 받음
        "g3": {"rep": "o3", "suggest_tried": True},  # 물었지만 이름을 못 받음
        "g4": {"rep": "o4", "dict": "card_2", "dict_how": "same"},  # 사전과 같음
        "g5": {"rep": "o5", "staged": {"name": "crate"}},  # 사람이 적음
        "g6": {"rep": ""},  # 위치 못 찾음 (그림 없음)
        "g7": {"rep": "o7", "not_icon": True},
        "g8": {"rep": "o8"},  # 그림 파일이 없는 자리: 물으면 한 묶음 전체가 실패해 창을 열 때마다 다시 묻게 된다
    })
    assert sc._unasked_groups(res) == {"g1"}


def test_same_shape_row_with_a_name_is_copied_not_asked(monkeypatch, tmp_path):
    """다시 찾은 줄이 이미 이름이 있는 줄과 모양이 같으면 그 이름을 옮겨 적고 모델에게 묻지 않는다
    (같은 아이콘에 서로 다른 제안 이름이 붙지 않게)."""
    from app import icons

    class O:
        def __init__(self, i):
            self.id, self.png = i, "x.png"

    res = icons.ScanResult([O("o1"), O("o2"), O("o3")], {
        "g1": {"rep": "o1", "shape": "s1", "staged": {"name": "coin_crate_5", "ko": "금화 상자"}},
        "g2": {"rep": "o2", "shape": "s1"},  # 새 줄, 모양 같음 -> 옮겨 적기
        "g3": {"rep": "o3", "shape": "s9"},  # 새 줄, 모양 다름 -> 묻는다
    })
    monkeypatch.setattr(icons, "_save_scan", lambda work, r: None)
    got = []
    monkeypatch.setattr(_extract, "suggest_missing_names",
                        lambda info, work, r, engine="", client=None, groups=None: got.append(groups) or "")
    sc._suggest_unasked(object(), tmp_path, res, "api")
    assert res.groups["g2"]["suggest"] == "coin_crate_5" and res.groups["g2"]["ko"] == "금화 상자"
    assert res.groups["g2"]["suggest_tried"] and got == [{"g3"}]


def test_suggest_missing_names_asks_only_given_groups_and_marks_them(monkeypatch, tmp_path):
    from app import icons

    class O:
        def __init__(self, i):
            self.id, self.png = i, ""

    res = icons.ScanResult([O("o1"), O("o2")], {"g1": {"rep": "o1", "shape": "s1"}, "g2": {"rep": "o2", "shape": "s2"}})
    asked = []

    def fake_suggest(target, client, model, work_dir):
        asked.append(sorted(target.groups))
        for g, gi in target.groups.items():
            gi.update(suggest="coin_crate", meaning="", ko="")
        return 0.0, 0.0

    monkeypatch.setattr(icons, "suggest_names", fake_suggest)
    monkeypatch.setattr(icons, "resolve_suggested_name", lambda info, r, g: "")
    monkeypatch.setattr(icons, "_save_scan", lambda work, r: None)
    note = _extract.suggest_missing_names(object(), tmp_path, res, client=object(), groups={"g1"})
    assert note == "" and asked == [["g1"]]
    assert res.groups["g1"]["suggest"] == "coin_crate" and res.groups["g1"]["suggest_tried"]
    assert "suggest" not in res.groups["g2"] and "suggest_tried" not in res.groups["g2"]


def test_suggest_failure_returns_note_and_does_not_mark(monkeypatch, tmp_path):
    from app import icons

    class O:
        id, png = "o1", ""

    res = icons.ScanResult([O()], {"g1": {"rep": "o1"}})

    def boom(*a, **k):
        raise RuntimeError("한도")

    monkeypatch.setattr(icons, "suggest_names", boom)
    note = _extract.suggest_missing_names(object(), tmp_path, res, client=object(), groups={"g1"})
    assert "이름 제안을 받지 못함" in note and not res.groups["g1"].get("suggest_tried")  # 다음에 창을 열 때 다시 묻는다


def test_ensure_icon_scan_suggests_after_region_rescan(monkeypatch, tmp_path):
    """칸만 다시 찾은 뒤에도 새 줄의 이름 제안을 받는다 (3쪽을 다시 읽은 뒤 ‘5+상자’ 이름 칸이 비어 있던 문제)."""
    from app import icons, icons_stage

    class Info:
        game_dir = tmp_path

    class O:
        id, png = "o9", "x.png"

    res = icons.ScanResult([O()], {"g9": {"rep": "o9"}})
    monkeypatch.setattr(sc, "_scan", lambda pd: (Info(), tmp_path, res))
    monkeypatch.setattr(icons, "token_count", lambda info: 1)
    monkeypatch.setattr(icons_stage, "stale_regions", lambda info, r: {("f", 3, "본문2")})
    monkeypatch.setattr(icons_stage, "rescan_regions", lambda info, work, keys: res)
    monkeypatch.setattr(icons, "recheck_boxed_numbers", lambda info, work: 0)
    got = []
    monkeypatch.setattr(_extract, "suggest_missing_names",
                        lambda info, work, r, engine="", client=None, groups=None: got.append(groups) or "")
    out = sc.ensure_icon_scan(tmp_path, engine="api", suggest=True)
    assert out["state"] == "synced" and got == [{"g9"}]
    got.clear()
    sc.ensure_icon_scan(tmp_path, engine="api", suggest=False)  # 사전 저장 뒤 다시 찾기 (suggest=False)는 묻지 않는다
    assert got == []


# ---------------------------------------------------------------- 확대 칸: 보이는 자리는 고정
def test_magnifier_area_stays_fixed_while_resizing(qapp):
    from PySide6.QtCore import QSize

    from app.qt.icon_place import IconPlaceView

    view = IconPlaceView()
    pm = QPixmap(400, 400)
    pm.fill()
    view.set_data(pm, (100, 100, 140, 140), None, [])
    size = QSize(192, 192)
    base = view.focus_crop(size).size()
    for new in ((100, 100, 150, 120), (110, 105, 130, 145), (95, 98, 145, 142)):  # 지금 자리 둘레 안의 새 상자
        view.new = new
        assert view.focus_crop(size).size() == base  # 배율이 바뀌지 않는다 (예전: 새 상자 모양을 따라 바뀜)
    view.new = (20, 100, 160, 140)  # 둘레를 벗어나면 넓힌다
    wide = view.focus_crop(size).size()
    assert wide != base and wide.width() == 192
    view.new = None  # 새 상자를 지우면 지금 자리 기준으로 되돌린다
    assert view.focus_crop(size).size() == base
    view.set_data(pm, (200, 200, 220, 260), None, [])  # 다른 아이콘: 새로 정한다
    assert view.focus_crop(size).height() == 192
    # 지금 자리가 없는 아이콘(위치 못 찾음): 끌기 시작한 1~2px 상자 둘레가 아니라 넉넉한 자리로 시작해 배율이 그대로
    view.set_data(pm, None, None, [])
    view.new = (100, 100, 102, 102)
    first = view.focus_crop(size).size()
    view.new = (100, 100, 130, 125)
    assert view.focus_crop(size).size() == first
    view.close()


# ---------------------------------------------------------------- 카드 단위 영역 고치기
def test_edit_card_regions_opens_editor_on_card_and_rescans_changed_card(qapp, tmp_path, monkeypatch):
    from tests.test_card_viewer_qt import _viewer

    from app.qt import region_editor

    info, fd, files, v = _viewer(qapp, tmp_path, name="cr", pages=2, start_page=2)
    assert v.card_regions_btn.isEnabled()
    calls = {}

    def fake_exec(ed):
        calls["at"] = (str(ed.current_file()), ed.page_spin.value())
        return 0

    rects = iter([{(str(fd), 1): {"본문": (0, 0, 1, 1)}, (str(fd), 2): {"본문": (0, 0, 1, 1)}},
                  {(str(fd), 1): {"본문": (0, 0, 1, 1)}, (str(fd), 2): {"본문": (0, 0.1, 1, 0.9)}}])
    monkeypatch.setattr(region_editor.RegionEditor, "exec", fake_exec)
    monkeypatch.setattr(service, "card_region_rects",
                        lambda f, pages, _it=rects: (lambda d: {p: d[(str(f), p)] for p in pages})(next(_it)))
    monkeypatch.setattr(service, "rescan_card_icons", lambda pd, cards: calls.setdefault("rescan", cards) and 1)
    v._edit_card_regions()
    assert calls["at"] == (str(fd), 2)  # 영역 지정 창이 이 카드로 열린다
    assert calls["rescan"] == [(str(fd), 2)]  # 상자가 바뀐 카드만 아이콘을 다시 찾는다
    assert "다시 찾았습니다" in v.flash_label.text()
    assert v.key[1] == 2
    v.close()


def test_card_region_rects_and_rescan_card_icons(tmp_path):
    from tests.test_icons import _project

    info, fd = _project(tmp_path, "rr", pages=2)
    service.find_icons(info.dir, suggest=False)
    r = service.card_region_rects(fd, [1, 2])
    assert set(r) == {1, 2} and "본문" in r[1]
    assert service.rescan_card_icons(info.dir, [(fd, 2)]) == 1  # 그 카드의 아이콘 칸만
    assert service.rescan_card_icons(info.dir, []) == 0


# ---------------------------------------------------------------- 형 검사(pyright)로 찾은 실제 오류
def test_erase_page_with_extra_small_image_does_not_crash():
    """배경 말고 작은 그림이 더 있는 쪽: erase_page 안의 지역 이름 area(라벨 띠 계산)가 위쪽 area 호출을
    UnboundLocalError로 만들었다 (pyright reportUnboundVariable)."""
    import numpy as np
    import pymupdf

    from app.erase import erase_page
    from tests.test_erase_pdf import make_card_pdf, template

    doc = pymupdf.open(stream=make_card_pdf(), filetype="pdf")
    page = doc[0]
    small = np.full((10, 10, 3), 40, np.uint8)
    page.insert_image(pymupdf.Rect(200, 20, 210, 30), pixmap=pymupdf.Pixmap(pymupdf.csRGB, 10, 10, small.tobytes(), 0))
    r = erase_page(page, template(), seed=0)
    assert r.meta["patches"] is not None


def test_show_error_accepts_exception(qapp, monkeypatch):
    """show_error(…, ex): QMessageBox.setText는 str만 받아 예외를 넘기면 오류 창 대신 TypeError가 났다."""
    from app.qt import dialogs

    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    box = dialogs.show_error(None, "제목", RuntimeError("까닭"))
    assert box.text() == "까닭"


# ---------------------------------------------------------------- 숫자만 다른 사전 아이콘: ‘숫자_이름’
def test_variant_name_puts_number_and_underscore_before_base():
    from app import icons

    assert icons.variant_name("2coinup", "2", "5") == "5_coinup"
    assert icons.variant_name("coin_crate_2", "2", "5") == "5_coin_crate"
    assert icons.variant_name("2_coin", "2", "10") == "10_coin"
    assert icons.variant_name("coin_up", "", "5") == "5_coin_up"  # 사전 그림에 숫자가 없는 아이콘
    assert icons.variant_name("coin", "2", "５") == "5_coin"  # 전각 숫자
    assert icons.variant_name("coin", "2", "") == ""


def test_number_variant_prefers_numbered_entry_and_needs_same_shape(monkeypatch):
    from app import icons

    known = {"2coinup": ("A", "2"), "coin_up": ("A", ""), "sword": ("B", ""), "old": ("A", None), "5_x": ("A", "5")}
    monkeypatch.setattr(icons, "same_icon", lambda a, b: 0.9 if b == "A" else 0.0)
    assert icons.number_variant("A", known, "5") == "5_coinup"  # 숫자 든 사전 아이콘 먼저 (숫자가 같은 5_x는 best_known 몫)
    del known["2coinup"]
    assert icons.number_variant("A", known, "5") == "5_coin_up"  # 없으면 숫자 없는 아이콘
    assert icons.number_variant("A", known, "") == ""  # 옆 숫자가 없는 자리는 아님
    monkeypatch.setattr(icons, "same_icon", lambda a, b: 0.5)
    assert icons.number_variant("A", known, "5") == ""  # 모양이 다르면 없음


def test_fill_variants_prefills_unnamed_numbered_rows(monkeypatch, tmp_path):
    from app import icons, icons_cells

    class O:
        def __init__(self, i, num, png="x.png", named=""):
            self.id, self.num, self.png, self.named, self.unique, self.variant = i, num, png, named, False, ""

    res = icons.ScanResult([O("o1", "5"), O("o2", ""), O("o3", "5")], {
        "g1": {"rep": "o1"},  # 숫자 있는 이름 없는 줄 -> 채움
        "g2": {"rep": "o2"},  # 옆 숫자 없음 -> 그대로
        "g3": {"rep": "o3", "staged": {"name": "mine"}},  # 사람이 적은 이름 -> 그대로
    })
    monkeypatch.setattr(icons, "_known_shapes", lambda info: {"2coinup": ("A", "2")})
    monkeypatch.setattr(icons_cells, "load_png", lambda p: None)
    monkeypatch.setattr(icons, "shape_pair", lambda a: "A")
    monkeypatch.setattr(icons, "same_icon", lambda a, b: 0.95)
    monkeypatch.setattr(icons, "_save_scan", lambda work, r: None)
    assert sc._fill_variants(object(), tmp_path, res) == 1
    assert res.groups["g1"]["dict"] == "5_coinup" and res.groups["g1"]["dict_how"] == "variant"
    assert "dict" not in res.groups["g2"] and "dict" not in res.groups["g3"]
    assert icons.prefill_name(res.groups["g1"]) == "5_coinup"
    assert not icons.needs_suggest(res.groups["g1"])  # 이름이 채워진 줄은 모델에게 묻지 않는다
    assert sc._unasked_groups(res) == {"g2"}  # 이름이 채워진 g1은 묻지 않고, 숫자 없는 g2만 묻는다


def test_regrow_erased_edges_restores_crate_border_but_not_wide_lines():
    """긴 선을 지운 칸(나무 상자의 위·아래 테두리가 지워짐)을 원래 그림으로 다시 넓힌다. 칸보다 넓은 띠 선은 넣지 않는다."""
    import numpy as np

    from app.icons_cells import _regrow_erased_edges

    ink = np.zeros((300, 400), bool)
    ink[100:104, 100:200] = True  # 상자 위 테두리 (칸 폭과 같음)
    ink[196:200, 100:200] = True  # 아래 테두리
    ink[100:200, 100:104] = True  # 왼쪽 세로
    ink[100:200, 196:200] = True  # 오른쪽 세로
    ink[60:63, 0:400] = True  # 칸보다 훨씬 넓은 띠 선
    ink[63:110, 300:303] = True  # 띠 선에 붙은 다른 그림 (상자와 안 겹침)
    cell = (104, 112, 196, 188)  # 긴 선을 지운 뒤 찾은 칸: 위·아래 테두리가 빠져 있다
    assert _regrow_erased_edges(cell, ink, (110, 190), 80) == (100, 100, 200, 200)
    # 상자 테두리가 칸보다 훨씬 넓은 선에 이어져 있으면 넓히지 않는다
    ink[100:104, 0:400] = True
    assert _regrow_erased_edges(cell, ink, (110, 190), 80) == cell


# ---------------------------------------------------------------- 1쪽: 줄마다 하나씩 적힌 아래 아이콘 줄
def test_merge_icon_lines_joins_consecutive_icon_only_lines():
    from app import icons

    src = "常時効果\n迷宮タイルを得るたび\n[[?]]を得る\nプレイ時効果\n[[?]]\n[[?]]\n[[?]]\n[[?]]"
    assert icons.merge_icon_lines(src) == "常時効果\n迷宮タイルを得るたび\n[[?]]を得る\nプレイ時効果\n[[?]] [[?]] [[?]] [[?]]"
    assert icons.merge_icon_lines("A[[?]]\n[[?]]B") == "A[[?]]\n[[?]]B"  # 글이 있는 줄은 합치지 않는다
    assert icons.merge_icon_lines("[[a]]\nX\n[[b]]") == "[[a]]\nX\n[[b]]"  # 이어지지 않은 아이콘 줄


def test_locate_prefers_merged_icon_lines_only_when_line_count_mismatches(monkeypatch):
    from app import icons

    calls = []

    def once(img, text, index):
        calls.append(text.count("\n"))
        return ((1, 2, 3, 4), "") if "[[?]] [[?]]" in text else ((9, 9, 9, 9), "")

    monkeypatch.setattr(icons, "_locate_once", once)
    src = "X[[?]]\n[[?]]\n[[?]]"
    monkeypatch.setattr(icons, "_bands_from", lambda *a, **k: [(0, 1), (2, 3)])  # 그림 2줄, 원문 3줄 -> 합친 원문 먼저
    assert icons.locate(None if False else __import__("numpy").zeros((40, 40, 3), "uint8"), src, 1)[0] == (1, 2, 3, 4)
    monkeypatch.setattr(icons, "_bands_from", lambda *a, **k: [(0, 1), (2, 3), (4, 5)])  # 줄 수가 꼭 맞음 -> 원문 그대로
    assert icons.locate(__import__("numpy").zeros((40, 40, 3), "uint8"), src, 1)[0] == (9, 9, 9, 9)


def test_reread_with_icon_moved_off_a_text_line_is_not_hidden():
    from app.qt.source_text import same_but_icon_layout

    reading = "迷宮タイルを得るたび\n[[?]]を得る\nプレイ時効果\n[[?]] [[?]]"
    wrong = "迷宮タイルを得るたび\nを得る\nプレイ時効果\n[[coin]]\n[[potion]]"
    assert not same_but_icon_layout(reading, wrong, True)  # 글 줄의 아이콘이 다른 줄로: 다시 읽은 결과를 보인다
    # 아이콘만 있는 줄의 배치(한 줄 ↔ 여러 줄)만 다르면 예전처럼 카드 보기에서 정한 대로
    assert same_but_icon_layout("A[[?]]B\n[[?]] [[?]]", "A[[x]]B\n[[y]]\n[[z]]", True)


def test_save_box_asks_when_box_is_in_another_icons_guess(qapp, tmp_path, monkeypatch):
    from tests.test_card_viewer_qt import _viewer

    from app.qt import card_viewer as cvm

    info, fd, files, v = _viewer(qapp, tmp_path, name="own")
    e = v._entries[0]
    other = {**e, "occ": "oX", "state": "lost", "line": 2, "ti": 1}
    v._entries = [e, other]
    v._place = {"guess": (0, 0, 10, 10), "box": None,
                "marks": [{"box": [50, 50, 90, 90], "occ": "oX", "guess": True}]}
    assert v._box_owner(e, (55, 55, 80, 80))["occ"] == "oX"  # 다른 아이콘의 예상 자리 안
    assert v._box_owner(e, (1, 1, 9, 9)) is None  # 제 예상 자리 안
    v._place["marks"][0]["guess"] = False
    assert v._box_owner(e, (55, 55, 80, 80)) is None  # 자리가 있는 아이콘이면 묻지 않는다
    v.close()


def test_icon_rows_show_which_line_and_token(qapp, tmp_path):
    from tests.test_card_viewer_qt import _viewer

    info, fd, files, v = _viewer(qapp, tmp_path, name="where")
    row = v.icon_list.itemWidget(v.icon_list.item(0))
    assert "째 줄 · AB■CD" in row.tag.text()  # 원문 ‘AB[[?]]CD’의 그 아이콘
    v.close()


# ---------------------------------------------------------------- 5쪽: 이름표 줄 + 원문에 없는 아이콘 줄, 글 줄 아이콘 높이
def _synthetic_lines():
    """원문: 이름표(常時効果) · 글+아이콘 줄 · [[?]] 줄 · 이름표(プレイ時効果). 그림: 글 줄 두 개 + 아래 큰 아이콘 줄 (이름표는 안 잡힘)."""
    lines = ["常時効果", "ジャンク[[?]]タグを得るたび", "[[?]]を得る", "プレイ時効果"]
    bands = [(170, 264), (300, 350), (658, 849)]
    comps = []
    for x in range(230, 960, 60):  # 1째 글 줄: 글자 12개
        comps.append((x, 190, 50, 50, 900))
    comps += [(450, 270, 100, 100, 5000), (570, 300, 50, 50, 900), (624, 300, 50, 50, 900), (687, 300, 47, 49, 900)]
    comps.append((526, 658, 133, 191, 9000))  # 아래 포션 (원문에 없음)
    return lines, bands, comps


def test_line_pairing_drops_label_lines_and_unsourced_icon_band():
    from app import icons

    lines, bands, comps = _synthetic_lines()
    pairing, cost = icons.line_pairing(lines, bands, comps)
    assert pairing.get(1) == 0 and pairing.get(2) == 1 and cost < 0.35
    assert icons._match_line(lines, 1, bands, comps) == 0 and icons._match_line(lines, 2, bands, comps) == 1


def test_insert_at_uses_paired_line_band_for_new_icon():
    """아이콘 추가: 같은 줄에 자리를 아는 아이콘이 없으면 상자가 든 글자 높이 띠의 원문 줄에 넣는다
    (예전: 높이 비율로 위 톱니 줄 ‘ジャンク[[?]][[?]]タグ’에 들어갔다)."""
    from app import icons_place

    src = "常時効果\nジャンク[[?]]タグを得るたび\nを得る\nプレイ時効果"
    box = (448, 260, 565, 376)
    lb = {1: (169, 264, 230, 960), 2: (300, 350, 570, 734), 3: (658, 849)}
    at = icons_place.insert_at(src, box, {0: [459, 163, 557, 254]}, (1201, 900), None, (0.0, 1201.0), lb)
    assert src[:at] + "[[?]]" + src[at:] == "常時効果\nジャンク[[?]]タグを得るたび\n[[?]]を得る\nプレイ時効果"
    # 짝 정보가 없으면 예전처럼 높이 비율 (회귀 확인용)
    at0 = icons_place.insert_at(src, box, {0: [459, 163, 557, 254]}, (1201, 900), None, (0.0, 1201.0))
    assert "ジャンク" in (src[:at0] + "[[?]]" + src[at0:]).split("\n")[1]


def test_locate_does_not_prefer_merged_text_when_original_is_closer(monkeypatch):
    import numpy as np

    from app import icons

    monkeypatch.setattr(icons, "_locate_once", lambda img, text, i: ((9, 9, 9, 9), "") if "\n[[b]]" in text
                        else ((1, 1, 1, 1), ""))
    # 정말 줄마다 하나씩 쌓인 아이콘 + 그림에 띠가 하나 더 (원문 4줄·합치면 3줄·그림 5줄): 원문 그대로 먼저
    monkeypatch.setattr(icons, "_bands_from", lambda *a, **k: [(0, 1)] * 5)
    assert icons.locate(np.zeros((40, 40, 3), np.uint8), "A\nB\n[[a]]\n[[b]]", 1)[0] == (9, 9, 9, 9)


def test_variant_name_keeps_other_numbers_and_skips_existing_names(monkeypatch):
    from app import icons

    assert icons.variant_name("2", "2", "5") == ""  # 숫자뿐인 이름
    assert icons.variant_name("x12y", "2", "5") == "5_x12y"  # 12 속의 2는 건드리지 않는다
    monkeypatch.setattr(icons, "same_icon", lambda a, b: 0.9)
    assert icons.number_variant("A", {"2coinup": ("A", "2"), "5_coinup": ("B", "")}, "5") == ""  # 이미 있는 이름


def test_hand_merged_set_on_text_line_stays_hidden():
    from app.qt.source_text import same_but_icon_layout

    assert same_but_icon_layout("効果[[?]] [[?]] [[?]]を得る", "効果[[set]]を得る", True)


def test_reread_shows_when_added_icon_went_to_the_wrong_text_line():
    from app.qt.source_text import same_but_icon_layout

    reading = "常時効果\nジャンク[[?]]タグを得るたび\n[[?]]を得る\nプレイ時効果\n[[?]]"
    wrong = "常時効果\nジャンク[[gear_3]][[coin]]タグを得るたび\n[[potion]]を得る\nプレイ時効果"
    assert not same_but_icon_layout(reading, wrong, True)  # 5쪽: 동전이 위 줄에 들어감 -> 다시 읽은 결과를 보인다


def test_text_height_uses_upper_percentile_not_median():
    """글자 높이는 덩어리 높이의 90번째 백분위: 획 조각(작은 덩어리)이 많아도 실제 글자 높이에 가깝다
    (중간값을 쓰니 이세길 9쪽에서 글자 높이가 17px로 잡혀 멀쩡한 글 줄 아이콘을 ‘2배보다 큼’으로 버렸다)."""
    from app import icons

    comps = [(0, 0, 10, 15, 60)] * 30 + [(0, 0, 40, 50, 900)] * 10  # 획 조각 30개 + 온전한 글자 10개
    assert icons.text_height(comps) == 50
    assert icons.text_height([]) == 0.0


def test_zoomed_view_centers_on_selected_icon(qapp):
    """확대해 둔 채 다른 아이콘을 고르면 그 아이콘 자리(없으면 예상 위치)가 화면 가운데로 온다. 보이는 상자면 그대로."""
    from app.qt.icon_place import IconPlaceView

    view = IconPlaceView()
    view.resize(400, 300)
    pm = QPixmap(800, 600)
    pm.fill()
    view.set_data(pm, (10, 10, 50, 50), None, [])
    assert not view.center_on((700, 500, 760, 560))  # 확대하지 않았으면 옮기지 않는다
    view.zoom_by(4.0)
    assert view.center_on((700, 500, 760, 560))  # 화면 밖 상자 -> 가운데로
    r = view._rect((700, 500, 760, 560))
    assert abs(r.center().x() - 200) < 1 and abs(r.center().y() - 150) < 1
    pan = list(view._pan)
    assert not view.center_on((720, 520, 740, 540))  # 이미 다 보이는 상자: 그대로
    assert view._pan == pan
    view.close()


def test_failed_list_read_keeps_regions_instead_of_showing_nothing(qapp, tmp_path, monkeypatch):
    """뒤에서 다시 읽는 순간 목록 읽기가 잠깐 실패해도 ‘보여 줄 영역이 없습니다’가 되지 않는다 (8쪽): 예전 목록을 두고 다시 읽는다."""
    from PySide6.QtCore import QTimer

    from tests.test_card_viewer_qt import _viewer

    info, fd, files, v = _viewer(qapp, tmp_path, name="keep")
    key = v.key
    good = v.rows_fn
    retries = []
    monkeypatch.setattr(QTimer, "singleShot", staticmethod(lambda ms, *a: retries.append(ms)))

    def boom():
        raise OSError("파일을 쓰는 중")
    v.rows_fn = boom
    v._fetch()
    v._show_region(key)
    assert v.key == key and "보여 줄 영역이 없습니다" not in v.region_title.text()
    assert v.flat and retries  # 예전 목록 그대로 + 조금 뒤 다시 읽기
    v.rows_fn = good
    v.close()


def test_confirm_next_goes_to_next_problem_even_when_card_leaves_problem_only_list(qapp, tmp_path):
    """목록 창이 ‘문제만’일 때 카드의 마지막 문제를 확정하면 그 카드가 목록에서 빠진다: 예전에는 지금 영역을 못 찾아
    첫 카드의 첫 영역(3쪽 본문, 문제 없음)으로 갔다. 확정 전 순서로 다음 문제(3쪽 본문2)를 찾는다."""
    from tests.test_card_viewer_qt import _viewer

    from app.qt.card_viewer import _nk

    info, fd, files, v = _viewer(qapp, tmp_path, name="po")

    def card(page, regs):
        rows = [{"key": (str(fd), page, name), "region": name, "status": st, "pending": False} for name, st in regs]
        return {"key": (str(fd), page), "regions": rows}

    before = [card(2, [("본문", "fix")]), card(3, [("본문", "todo"), ("본문2", "fix")])]
    order = [r["key"] for c in before for r in c["regions"]]
    after = [before[1]]  # 2쪽은 문제를 다 고쳐 ‘문제만’ 목록에서 빠짐
    v.flat = [(c, r) for c in after for r in c["regions"]]
    v._by_key = {_nk(r["key"]): (c, r) for c, r in v.flat}
    assert v._after_confirm((str(fd), 2, "본문"), order, +1, wrap=True) == (str(fd), 3, "본문2")
    # 남은 문제가 없으면 순서상 가까운 영역 (첫 카드 첫 영역으로 튀지 않게)
    after[0]["regions"][1]["status"] = "ok"
    assert v._after_confirm((str(fd), 2, "본문"), order, +1, wrap=True) == (str(fd), 3, "본문")
    v.close()
