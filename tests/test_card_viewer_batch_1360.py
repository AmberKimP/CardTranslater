"""② 원문 확인 · 카드 보기 (1.36.0 묶음): 아이콘 그림 속 숫자만 다른 두 번 읽기, 영역 지정 중 버튼 흐리기,
아이콘 칸 두 줄 표시·띠 높이, 대문자 이름, 상자를 잡은 채 이름 적기, 겹치는 이름 (그림을 견주어 숫자 붙이기).
가짜 카드 PDF만 쓴다 (실제 Claude 호출 없음)."""

from __future__ import annotations

import shutil

import numpy as np
import pytest
from PySide6.QtWidgets import QAbstractButton

from app import icons, service, workspace
from tests.test_card_viewer_qt import _no_claude, _no_modal_boxes, _region, _type_name, _viewer, qapp  # noqa: F401


# ---------------------------------------------------------------- 두 번 읽기: 아이콘 그림 속 숫자만 다름
def test_readings_differing_only_by_icon_number_are_not_a_disagreement():
    icons_ = [{"state": "named", "name": "coin", "ti": 0, "num_image": {"text": "4", "side": "before"}}]
    cell = {"source": "効果\n[[?]] を得る", "label": "결론 못 냄",
            "readings": [{"source": "効果\n[[?]]を得る"}, {"source": "効果\n4[[?]] を得る"}]}
    status, reasons = service.region_status(cell, icons_)
    assert status != "fix" and "두 번 읽기 다름" not in reasons
    # 확정한 아이콘(줄의 strip 없음)도 그림 속 숫자로 견준다 (3·4쪽)
    status, _r = service.region_status({**cell, "source": "効果\n[[coin]] を得る"},
                                       [{**icons_[0], "state": "ok", "name": "coin"}])
    assert status != "fix"
    # 확정한 원문에 그림 속 숫자가 남아 있으면(4[[coin]]) 두 번 인쇄된다: 그대로 ‘두 번 읽기 다름’
    status, _r = service.region_status({**cell, "source": "効果\n4[[coin]] を得る"},
                                       [{**icons_[0], "state": "ok", "name": "coin"}])
    assert status == "fix"


def test_readings_with_real_difference_stay_fix():
    icons_ = [{"state": "unnamed", "ti": 0, "num_image": {"text": "4", "side": "before"}}]
    cell = {"source": "[[?]] を得る", "label": "결론 못 냄",
            "readings": [{"source": "効果\n[[?]]を得る"}, {"source": "4[[?]] を得る"}]}
    status, reasons = service.region_status(cell, icons_)
    assert status == "fix" and "두 번 읽기 다름" in reasons
    # 그림에 숫자가 없는 아이콘이면 숫자 차이는 진짜 차이다
    cell2 = {**cell, "readings": [{"source": "[[?]] を得る"}, {"source": "4[[?]] を得る"}]}
    assert service.region_status(cell2, [{"state": "unnamed", "ti": 0}])[0] == "fix"


# ---------------------------------------------------------------- 대문자 이름
def test_uppercase_icon_names_are_valid_everywhere():
    from app import glossary, translate, typeset, typeset_layout

    assert icons.NAME_RE.match("Coin_UP2")
    assert typeset._ICON_NAME.match("Coin")
    assert typeset_layout.ICON_RE.findall("A[[Coin]]B") == ["Coin"]
    assert translate._ICON.findall("[[Big_Heart]]") == ["Big_Heart"]
    assert glossary._ICON.search("x[[Coin]]")
    assert not icons.NAME_RE.match("코인") and not icons.NAME_RE.match("coin-up")


# ---------------------------------------------------------------- 아이콘 칸: 상태·줄 번호 / 그 줄 모양 두 줄, 띠는 4칸 높이
def test_icon_strip_height_fits_four_cards_regardless_of_count(qapp, tmp_path):
    from app.qt.card_viewer import ICON_CARD_W, ICON_STRIP_CARDS

    info, fd, files, v = _viewer(qapp, tmp_path, name="strip")
    v.resize(1400, 900)
    v.show()
    qapp.processEvents()
    per = max(1, (v.icon_list.viewport().width() - 4) // (ICON_CARD_W + 4))
    rows = min(-(-ICON_STRIP_CARDS // per), 2)
    row_h = v.icon_list.item(0).sizeHint().height()
    assert v.icon_list.count() == 1
    assert v.icon_list.height() >= rows * row_h  # 아이콘이 하나여도 4칸 높이 (빈칸)
    row = v.icon_list.itemWidget(v.icon_list.item(0))
    assert row.where.isVisible() and row.where.text() == "AB■CD"
    assert row.where.geometry().top() >= row.tag.geometry().bottom()  # 모양은 상태 아래 줄
    v.close()


# ---------------------------------------------------------------- 영역 지정 창을 띄운 동안 뒤 창 버튼 흐리기
def test_edit_card_regions_disables_background_buttons_while_open(qapp, tmp_path, monkeypatch):
    from app.qt import region_editor

    info, fd, files, v = _viewer(qapp, tmp_path, name="freeze")
    seen = {}

    class FakeEditor:
        def __init__(self, *a, **k):
            pass

        def go_to(self, *a):
            pass

        def windowState(self):
            from PySide6.QtCore import Qt
            return Qt.WindowState.WindowNoState

        def setWindowState(self, *_a):
            pass

        def exec(self):
            btns = [b for b in v.findChildren(QAbstractButton) if b.window() is v]
            seen["enabled"] = [b.text() for b in btns if b.isEnabled()]
            seen["n"] = len(btns)

        def deleteLater(self):
            pass

    monkeypatch.setattr(region_editor, "RegionEditor", FakeEditor)
    monkeypatch.setattr(service, "list_groups", lambda *_a, **_k: [])
    before = v.nav_btns["next"].isEnabled()
    v._edit_card_regions()
    assert seen["n"] > 10 and seen["enabled"] == []  # 띄운 동안 모두 흐림
    assert v.nav_btns["next"].isEnabled() == before and v.card_regions_btn.isEnabled()  # 닫으면 원래대로
    v.close()


# ---------------------------------------------------------------- 상자를 잡은 채 이름 적기: 자리도 함께 저장
def test_typing_name_with_unsaved_box_saves_box_too(qapp, tmp_path, monkeypatch):
    info, fd, files, v = _viewer(qapp, tmp_path, name="boxname")
    v.same_shape.setChecked(False)
    occ = v._entries[0]["occ"]
    v.view.new = (2, 2, 14, 14)
    calls = []
    real = service.set_icon_box
    monkeypatch.setattr(service, "set_icon_box", lambda pd, o, box: calls.append((o, tuple(box))) or real(pd, o, box))
    monkeypatch.setattr(service, "icon_box_problem", lambda *_a: "")
    _type_name(qapp, v, "lily")
    assert calls == [(occ, (2, 2, 14, 14))]  # 이름보다 먼저 자리를 저장
    qapp.processEvents()
    r = _region(info, files, 1)
    assert r["icons"][0]["name"] == "lily" and r["icons"][0]["pending"]
    assert "자리도 저장함" in v.flash_label.text()
    v.close()


# ---------------------------------------------------------------- 겹치는 이름: 그림을 견준다
def _dict_icon(info, name: str, png_src=None, different=False):
    """사전에 아이콘 하나를 넣는다: png_src를 복사하거나(같은 그림) 전혀 다른 그림(가는 십자)을 그린다."""
    import cv2

    d = icons.load_dictionary(info)
    folder = icons.icons_dir(info)
    folder.mkdir(parents=True, exist_ok=True)
    dst = folder / f"{name}.png"
    if different:
        img = np.zeros((60, 60, 4), np.uint8)
        img[28:32, 5:55] = (200, 120, 10, 255)
        img[5:55, 28:32] = (200, 120, 10, 255)
        cv2.imwrite(str(dst), img)
    else:
        shutil.copy(png_src, dst)
    d["icons"][name] = {"file": dst.name, "ko": name}
    icons.save_dictionary(info, d)


def _rep_png(info, group):
    res = icons.load_scan(info.game_dir / "work" / "icons")
    gi = res.groups[group]
    return next(o.png for o in res.occurrences if o.id == gi["rep"])


def _first_group(info, fd, files):
    return _region(info, files, 1)["icons"][0]["group"]


@pytest.fixture()
def named_project(tmp_path):
    from tests.test_icons import _project

    info, fd = _project(tmp_path, "names", pages=1)
    service.find_icons(info.dir, suggest=False)
    files = [{"dir": fd, "key": "카드/cards"}]
    return info, fd, files


def test_name_of_same_picture_is_kept_with_its_casing(named_project):
    info, fd, files = named_project
    g = _first_group(info, fd, files)
    _dict_icon(info, "coin", png_src=_rep_png(info, g))
    assert service.resolve_icon_name(info.dir, g, "coin") == {"name": "coin", "renamed": False, "same_as": "coin"}
    assert service.resolve_icon_name(info.dir, g, "Coin")["name"] == "coin"  # 대소문자만 다르면 같은 이름


def test_name_of_different_picture_gets_free_number(named_project):
    info, fd, files = named_project
    g = _first_group(info, fd, files)
    _dict_icon(info, "coin", different=True)
    _dict_icon(info, "coin_2", different=True)
    r = service.resolve_icon_name(info.dir, g, "coin")
    assert r == {"name": "coin_3", "renamed": True, "same_as": ""}
    assert service.resolve_icon_name(info.dir, g, "COIN")["name"] == "COIN_3"  # coin_2와 겹치지 않게 (대소문자 무시)


def test_new_name_unchanged(named_project):
    info, fd, files = named_project
    g = _first_group(info, fd, files)
    assert service.resolve_icon_name(info.dir, g, "lily") == {"name": "lily", "renamed": False, "same_as": ""}


def test_card_viewer_renames_clashing_name(qapp, tmp_path):
    info, fd, files, v = _viewer(qapp, tmp_path, name="clash")
    _dict_icon(workspace.load_project_info(info.dir), "lily", different=True)
    v._fetch()
    _type_name(qapp, v, "lily")
    qapp.processEvents()
    assert _region(info, files, 1)["icons"][0]["name"] == "lily_2"
    assert "lily_2" in v.flash_label.text()
    v.close()


def test_suggested_name_of_other_group_counts_as_taken(named_project):
    """다른 줄에 미리 채운 Claude 제안 이름(적지 않음)도 이미 쓰는 이름이다: 숫자를 붙일 때 그 이름을 건너뛴다."""
    info, fd, files = named_project
    g = _first_group(info, fd, files)
    _dict_icon(info, "lily", different=True)
    work = info.game_dir / "work" / "icons"
    res = icons.load_scan(work)
    res.groups["gx"] = {"rep": "", "count": 1, "shape": "s-x", "suggest": "lily_2"}
    icons._save_scan(work, res)
    assert service.resolve_icon_name(info.dir, g, "lily") == {"name": "lily_3", "renamed": True, "same_as": ""}


def test_case_only_rename_of_group_being_named_is_kept(named_project):
    info, fd, files = named_project
    g = _first_group(info, fd, files)
    service.stage_icon_names(info.dir, [{"group": g, "name": "coin"}])
    # 이 줄 자신(과 함께 적는 줄)의 예전 이름으로 되돌아가지 않는다
    assert service.resolve_icon_name(info.dir, g, "Coin", exclude_groups=[g])["name"] == "Coin"


def test_excel_import_uses_dictionary_spelling_for_case_variant(tmp_path):
    """엑셀에 ‘Coin’을 적어도 사전의 ‘coin’으로 넣는다 (Windows에서 Coin.png가 coin.png를 덮어쓰지 않게)."""
    from tests.test_icons import _project, _source

    info, fd = _project(tmp_path, "case", pages=1)
    service.find_icons(info.dir, suggest=False)
    res = icons.load_scan(info.game_dir / "work" / "icons")
    g = next(iter(res.groups))
    _dict_icon(info, "coin", png_src=_rep_png(info, g))
    before = (icons.icons_dir(info) / "coin.png").read_bytes()
    out = icons.import_rows(info, [{"묶음": g, "이름": "Coin", "처리": "", "글자": ""}],
                            info.game_dir / "work" / "icons")
    assert not out.blocking
    d = icons.load_dictionary(info)["icons"]
    assert "Coin" not in d and "coin" in d
    assert (icons.icons_dir(info) / "coin.png").read_bytes() == before
    assert "[[coin]]" in _source(fd, 1)
