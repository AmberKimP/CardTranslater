"""1.18.0 묶음: 아이콘 정의 뒤 ‘나중에’ 고른 칸 다시 읽기, 용어집 만들기 멈춤, 이름 겹침, 사전 값 지키기,
‘아이콘만 있음 · 원본 그대로’, 아이콘 정의 창의 사전 탭."""

from __future__ import annotations

import os

import pytest

from app import icons, service
from tests.test_batch_1170 import _confirm_all
from tests.test_icons import _add_run_partner, _project

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _changed_project(tmp_path, name="chg"):
    info, fd = _project(tmp_path, name, pages=2)
    service.find_icons(info.dir, suggest=False)
    scan = icons.load_scan(info.game_dir / "work" / "icons")
    o1 = next(o for o in scan.occurrences if o.page == 1)
    service.set_icon_box(info.dir, o1.id, o1.box)  # 1쪽: 사람이 자리를 다시 잡음
    assert _confirm_all(info).state == "confirmed"
    return info, fd


# ---------------------------------------------------------------- 1. ‘나중에 (원문 확인에서)’ → 원문 확인에서 다시 읽기
def test_changed_cells_wait_for_reread_until_read(tmp_path, monkeypatch):
    info, fd = _changed_project(tmp_path)
    pending = service.icon_reread_pending([fd])
    assert [(c["page"], c["region"]) for c in pending] == [(1, "본문")]

    def fake_read(client, proj, page, src_doc, model_id, out_dir, regs):
        return {regs[0].name: {"source": "AB[[lily]]CD", "uncertain": False}}, 0.0, 0.0

    monkeypatch.setattr("app.extract._read_regions_once", fake_read)
    r = service.reread_cells(pending, client=object())
    assert r["done"] == 1
    assert service.icon_reread_pending([fd]) == []  # 다시 읽었으니 원문 확인을 열 때 또 읽지 않는다
    c = next(c for c in service.extracted_cells(fd) if c["page"] == 1)
    # 보통·정밀 읽기가 같고 지금 원문과도 (아이콘 말고) 같으면 확인이 끝난다 (예전: 사람이 저장할 때까지 ‘확인 필요’)
    assert not c["icon_changed"] and r["settled"] == 1


# ---------------------------------------------------------------- 7. 용어집 만들기: 아이콘이 바뀐 칸이 남으면 원문 확인으로
def test_finish_lists_icon_changed_cells(tmp_path):
    info, fd = _changed_project(tmp_path, "fin")
    r = service.finish_extraction(info.dir, client=object())
    assert r.state == "needs_confirmation"
    assert any(i.startswith("아이콘이 바뀐 칸 (② 원문 확인 필요): ") and i.endswith(" 1칸") for i in r.issues)


# ---------------------------------------------------------------- 3. 제안 이름이 겹칠 때
def _scan_with_two(tmp_path):
    info, fd = _project(tmp_path, "nm", pages=1)
    host, extra = _add_run_partner(info, fd)
    work = info.game_dir / "work" / "icons"
    return info, work, host, extra


def test_suggested_name_same_dictionary_icon_is_registered_as_same(tmp_path):
    import shutil

    info, fd = _project(tmp_path, "same", pages=2)
    service.find_icons(info.dir, suggest=False)
    work = info.game_dir / "work" / "icons"
    shutil.copytree(work, tmp_path / "scan_copy")  # 확정하면 찾기 결과가 지워지므로 같은 그림의 결과를 남겨 둔다
    assert _confirm_all(info, "lily").state == "confirmed"  # 사전에 lily
    shutil.copytree(tmp_path / "scan_copy", work, dirs_exist_ok=True)
    res = icons.load_scan(work)
    g = next(iter(res.groups))
    res.groups[g]["suggest"] = "lily"
    res.groups[g].pop("dict", None)
    assert icons.resolve_suggested_name(info, res, g) == "same_dict"
    assert res.groups[g]["dict"] == "lily" and res.groups[g]["suggest"] == "lily"


def test_suggested_name_different_icon_gets_new_name(tmp_path, monkeypatch):
    info, fd = _project(tmp_path, "diff", pages=1)
    service.find_icons(info.dir, suggest=False)
    work = info.game_dir / "work" / "icons"
    res = icons.load_scan(work)
    g = next(iter(res.groups))
    res.groups[g]["suggest"] = "coin"
    monkeypatch.setattr(icons, "load_dictionary", lambda _i: {"icons": {"coin": {}, "coin_2": {}}})
    monkeypatch.setattr(icons, "_known_shapes", lambda _i: {})  # 사전 coin은 그림이 다름 (모양 없음)
    assert icons.resolve_suggested_name(info, res, g) == "renamed"
    assert res.groups[g]["suggest"] == "coin_3"


# ---------------------------------------------------------------- 사전과 같다고 고른 줄은 사전 값을 덮어쓰지 않는다
def test_keep_dict_row_does_not_overwrite_dictionary(tmp_path):
    info, fd = _project(tmp_path, "kd", pages=2)
    service.find_icons(info.dir, suggest=False)
    assert _confirm_all(info, "lily").state == "confirmed"
    service.update_icon_dictionary(info.dir, [{"name": "lily", "ko": "백합꽃", "meaning": "百合", "scale": 1.6,
                                               "reset": False}])
    for p in (1, 2):
        f = fd / "extract" / "pages" / f"page-{p:03d}.json"
        f.write_text(f.read_text(encoding="utf-8").replace("[[lily]]", "[[?]]"), encoding="utf-8")
    service.find_icons(info.dir, suggest=False)
    rev = service.icon_review(info.dir)
    rows = [{"group": g["group"], "name": "lily", "meaning": "", "ko": "", "not_icon": False, "text": "",
             "scale": None, "keep_dict": True} for g in rev["groups"]]
    assert service.confirm_icons(info.dir, rows).state == "confirmed"
    e = icons.load_dictionary(info)["icons"]["lily"]
    assert e["ko"] == "백합꽃" and float(e["scale"]) == 1.6


# ---------------------------------------------------------------- 5. 아이콘만 있음 · 원본 그대로
def test_keep_original_in_result_view_info_and_review_rows(tmp_path):
    info, fd = _project(tmp_path, "keep", pages=1)
    assert service.card_edit_info(fd, 1, "본문")["keep_original"] is False
    service.set_keep_original(fd, 1, "본문", True)
    assert service.card_edit_info(fd, 1, "본문")["keep_original"] is True
    service.set_keep_original(fd, 1, "본문", False)
    assert service.card_edit_info(fd, 1, "본문")["keep_original"] is False


def test_review_dialog_keep_button_sets_keep_original(qapp, tmp_path, monkeypatch):
    from app.qt import review as review_mod

    info, fd = _project(tmp_path, "rk", pages=1)
    calls = []
    monkeypatch.setattr(review_mod.service, "set_keep_original", lambda *a: calls.append(a))
    dlg = review_mod.TranslationReviewDialog.__new__(review_mod.TranslationReviewDialog)
    row = {"id": "x#p1#본문", "file_dir": fd, "page": 1, "region": "본문"}
    loaded = []
    dlg.current_row = lambda: row
    dlg.view_ids = ["x#p1#본문", "y"]
    dlg.edit = type("E", (), {"toPlainText": lambda self: ""})()
    dlg.load = lambda keep_id=None: loaded.append(keep_id)
    review_mod.TranslationReviewDialog.keep_original(dlg)
    assert calls == [(fd, 1, "본문", True)] and loaded == ["y"]


# ---------------------------------------------------------------- 4. 아이콘 정의 창: 사전 전체 탭
def test_keep_original_clears_icon_changed_flags(tmp_path):
    """원본 그대로 둘 칸은 ‘아이콘이 바뀐 칸’ 확인·다시 읽기에서 빠진다 (용어집 만들기 경고가 풀리지 않던 문제)."""
    info, fd = _changed_project(tmp_path, "kc")
    service.set_keep_original(fd, 1, "본문", True)
    assert service.icon_reread_pending([fd]) == []
    c = next(c for c in service.extracted_cells(fd) if c["page"] == 1)
    assert not c["icon_changed"]

