"""② 카드 보기 · 아이콘 자리 잡기 (2B): 겹친 아이콘 흡수, 둘로 나누기, 세트로 합친 뒤 창을 닫지 않고 새로 보여 주기."""
from __future__ import annotations

import os

import pytest

from app import icons, service
from tests.test_icons import _add_run_partner, _project, _source

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _located(info):
    rev = service.icon_review(info.dir)
    for g in rev["groups"]:
        for w in g["where"]:
            box = service.icon_place(info.dir, w["occ"])["box"]
            if box:
                return g, w["occ"], box
    raise AssertionError("찾은 자리가 없음")


def test_overlapping_occ_skips_named_self_and_not_icon():
    marks = [{"box": [0, 0, 10, 10], "occ": "o1"}, {"box": [2, 2, 12, 12], "occ": "o2"},
             {"box": [1, 1, 9, 9], "name": "crown"}, {"box": [0, 0, 10, 10], "occ": "o3", "not_icon": True},
             {"box": [50, 50, 60, 60], "occ": "o4"}]
    assert icons.overlapping_occ(marks, [0, 0, 10, 10], exclude="o1") == ["o2"]
    assert icons.overlapping_occ(marks, None) == []


def test_split_icon_adds_placeholder_and_cuts_two_boxes(tmp_path):
    info, fd = _project(tmp_path, "split", pages=1)
    service.find_icons(info.dir, suggest=False)
    _g, occ, (x0, y0, x1, y1) = _located(info)
    mid = (x0 + x1) // 2
    r = service.split_icon_box(info.dir, occ, [x0, y0, mid, y1])  # 왼쪽 반을 감쌈 -> 왼쪽이 앞 자리
    assert _source(fd, 1) == "AB[[?]][[?]]CD"
    scan = icons.load_scan(info.game_dir / "work" / "icons")
    by_id = {o.id: o for o in scan.occurrences}
    a, b = by_id[r["first"]], by_id[r["second"]]
    assert a.index == 0 and b.index == 1 and a.manual and b.manual
    assert a.box[2] <= b.box[0] + 1 and a.png and b.png
    with pytest.raises(ValueError):  # 상자 밖을 감싸 한쪽이 없어지면 못 나눈다
        service.split_icon_box(info.dir, r["first"], [a.box[0], a.box[1], a.box[2] + 500, a.box[3]])


def test_dialog_absorbs_overlapping_icon_without_closing(qapp, tmp_path):
    from PySide6.QtWidgets import QDialog

    from app.qt.icon_place import IconPlaceDialog

    info, fd = _project(tmp_path, "ov", pages=1)
    host, extra = _add_run_partner(info, fd)
    g = next(x for x in service.icon_review(info.dir)["groups"] if any(w["occ"] == host for w in x["where"]))
    saved = []
    place = IconPlaceDialog(info.dir, g, lambda grp: grp, saved.append)
    box = place.view.box
    place._place["marks"] = [{"box": list(box), "occ": extra, "group": "x"}]  # 다른 [[?]]가 같은 그림 자리
    place._update_tools()
    assert not place.overlap_btn.isHidden()
    place._absorb_overlaps()
    assert place.result() != QDialog.DialogCode.Accepted  # 닫지 않는다
    rev = service.icon_review(info.dir)
    by_occ = {w["occ"]: x for x in rev["groups"] for w in x["where"]}
    assert by_occ[extra]["not_icon"] and by_occ[extra]["absorbed_group"] == by_occ[host]["group"]
    assert place.outcome == ("saved", by_occ[host]["group"]) and saved


def test_dialog_save_as_set_keeps_window_open_and_shows_merged_icon(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QDialog

    import app.qt.icon_place as ip
    from app.qt.icon_place import IconPlaceDialog

    info, fd = _project(tmp_path, "set", pages=1)
    host, extra = _add_run_partner(info, fd)
    g = next(x for x in service.icon_review(info.dir)["groups"] if any(w["occ"] == host for w in x["where"]))
    monkeypatch.setattr(ip, "ask_choice", lambda *a, **k: 0)  # ‘아이콘 2개를 한 세트로’
    monkeypatch.setattr(service, "engine_status", lambda: {})  # 이름 제안 없이
    place = IconPlaceDialog(info.dir, g, lambda grp: grp, lambda r: None)
    x0, y0, x1, y1 = place.view.box
    wide = [x0, y0, x0 + (y1 - y0) * 2 + 4, y1]
    place.view.new = wide
    place._drawn()
    place._save()
    assert place.result() != QDialog.DialogCode.Accepted  # 합쳤으면 창을 닫지 않는다
    assert place.view.box == [int(v) for v in wide] and place.view.new is None  # 합친 자리를 새로 보여 줌
    cur = place.where[place.combo.currentIndex()]
    assert cur["occ"] == host and cur["png"] and not place.crop.pixmap().isNull()


def test_dialog_split_adds_new_spot_to_list(qapp, tmp_path):
    from app.qt.icon_place import IconPlaceDialog

    info, fd = _project(tmp_path, "sp", pages=1)
    service.find_icons(info.dir, suggest=False)
    g, occ, (x0, y0, x1, y1) = _located(info)
    place = IconPlaceDialog(info.dir, g, lambda grp: grp, lambda r: None)
    assert not place.split_btn.isEnabled()  # 감싸기 전에는 못 누름
    place.view.new = [x0, y0, (x0 + x1) // 2, y1]
    place._drawn()
    assert place.split_btn.isEnabled()
    n = place.combo.count()
    place._split()
    assert place.combo.count() == n + 1 and _source(fd, 1) == "AB[[?]][[?]]CD"


def test_split_shifts_icon_number_positions_and_skips_guesses(tmp_path):
    """나누면 뒤 아이콘에 넣어 둔 숫자의 순번(icon_numbers)도 하나 밀린다. 예상 위치(점선)는 흡수 후보가 아니다."""
    import json

    info, fd = _project(tmp_path, "num", pages=1)
    service.find_icons(info.dir, suggest=False)
    _g, occ, (x0, y0, x1, y1) = _located(info)
    for f in (fd / "extract" / "pages" / "page-001.json",):
        rec = json.loads(f.read_text(encoding="utf-8"))
        rec["regions"]["본문"]["source"] = "AB[[?]]CD[[coin]]"
        rec["regions"]["본문"]["icon_numbers"] = [{"index": 1, "name": "coin", "number": "3", "side": "right"}]
        f.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    service.split_icon_box(info.dir, occ, [x0, y0, (x0 + x1) // 2, y1])
    r = json.loads((fd / "extract" / "pages" / "page-001.json").read_text(encoding="utf-8"))["regions"]["본문"]
    assert r["source"] == "AB[[?]][[?]]CD[[coin]]" and r["icon_numbers"][0]["index"] == 2
    assert icons.overlapping_occ([{"box": [0, 0, 10, 10], "occ": "o9", "guess": True}], [0, 0, 10, 10]) == []


def test_placeholder_runs_same_line_splits_vertical_neighbours():
    text = "[[?]][[?]] A\n[[?]]\n[[?]] B"
    assert icons.placeholder_runs(text) == [[0, 1], [2, 3]]  # 사람이 묶을 때는 줄을 넘어 이어 본다
    assert icons.placeholder_runs(text, same_line=True) == [[0, 1], [2], [3]]  # 자동 세트는 좌우로 붙은 것만


def test_unfound_partner_on_next_line_is_not_offered_for_merge(tmp_path):
    """위아래로 이어진 [[?]]: 위치를 못 찾은 아랫줄 자리는 ‘한 세트로 합칠까’에 넣지 않는다 (사람이 감쌀 때만)."""
    import json

    info, fd = _project(tmp_path, "vert", pages=1)
    host, extra = _add_run_partner(info, fd)
    work = info.game_dir / "work" / "icons"
    assert icons.run_partners(info, work, host)["unfound"] == [extra]  # 같은 줄: 묻는다
    p = fd / "extract" / "pages" / "page-001.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d["regions"]["본문"]["source"] = "AB[[?]]\n[[?]]CD"
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    sp = work / "scan.json"
    scan = json.loads(sp.read_text(encoding="utf-8"))
    for o in scan["occurrences"]:
        if o["id"] == extra:
            o["line"], o["order"] = 2, 1
    sp.write_text(json.dumps(scan, ensure_ascii=False), encoding="utf-8")
    assert icons.run_partners(info, work, host)["unfound"] == []  # 아랫줄: 묻지 않는다
