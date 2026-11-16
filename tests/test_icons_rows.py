"""② 아이콘 정의: 카드의 아이콘 하나 = 한 줄, 사전 이름 미리 채우기(자동으로 바꾸지 않음), 확정된 아이콘도 표에,
카드 순서·문제 카드 먼저, 카드 보기 ‘아이콘 추가’. 가짜 카드 PDF만 쓴다 (실제 호출 없음)."""

from __future__ import annotations

import json
import os

import pytest

from app import icons, service
from tests.test_icons import _project, _source


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def _set_src(fd, page, src):
    p = fd / "extract" / "pages" / f"page-{page:03d}.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d["regions"]["본문"]["source"] = src
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def _cell(fd, page=1):
    p = fd / "extract" / "pages" / f"page-{page:03d}.json"
    return json.loads(p.read_text(encoding="utf-8"))["regions"]["본문"]


def _confirmed(tmp_path, pages=3, name="coin"):
    info, fd = _project(tmp_path, "1차", pages=pages)
    service.find_icons(info.dir, suggest=False)
    rows = [{"group": g["group"], "name": name, "ko": "금화"} for g in service.icon_review(info.dir)["groups"]]
    assert service.confirm_icons(info.dir, rows).state == "confirmed"
    return info, fd


# ---------------------------------------------------------------- 한 줄 = 카드의 아이콘 하나
def test_each_occurrence_is_its_own_row_and_same_shape_rows_share_shape(tmp_path):
    info, fd = _project(tmp_path, "1차", pages=3)
    r = service.find_icons(info.dir, suggest=False)
    assert r.groups == 3 and r.occurrences == 3
    rows = service.icon_review(info.dir)["groups"]
    assert [g["where"][0]["page"] for g in rows] == [1, 2, 3]  # 카드 순서
    assert all(g["count"] == 1 and len(g["where"]) == 1 for g in rows)
    assert len({g["shape"] for g in rows}) == 1 and all(g["shape_count"] == 3 for g in rows)
    assert all(g["card_label"] == str(g["where"][0]["page"]) for g in rows)


def test_rows_with_same_name_become_one_dictionary_entry_with_first_row_picture(tmp_path):
    info, fd = _project(tmp_path, "1차", pages=2)
    service.find_icons(info.dir, suggest=False)
    g1, g2 = service.icon_review(info.dir)["groups"]
    r = service.confirm_icons(info.dir, [{"group": g1["group"], "name": "coin", "ko": "금화", "meaning": "Gold"},
                                         {"group": g2["group"], "name": "coin", "ko": "동전"}])
    assert r.state == "confirmed" and r.added == ["coin"] and r.replaced == 2
    e = icons.load_dictionary(info)["icons"]["coin"]
    assert e["ko"] == "금화" and e["meaning"] == "Gold"  # 적힌 첫 값
    assert any("금화" in n and "동전" in n for n in r.notes)  # 서로 다르면 알린다 (막지는 않음)


# ---------------------------------------------------------------- 사전 이름은 미리 채우기만
def test_dictionary_match_prefills_name_without_changing_source(tmp_path):
    info, fd = _confirmed(tmp_path, pages=2)
    _set_src(fd, 2, "AB[[?]]CD")
    r = service.find_icons(info.dir, suggest=False)
    assert r.state == "icons_review" and not r.auto and r.prefilled == 1 and r.named == 1
    assert _source(fd, 2) == "AB[[?]]CD" and "자동" not in _cell(fd, 2).get("icon_note", "")
    row = next(g for g in service.icon_review(info.dir)["groups"] if g["where"][0]["page"] == 2)
    assert row["suggest"] == "coin" and row["dict_how"] == "same" and row["how_label"] == "사전과 모양 같음"
    assert row["ko"] == "금화" and not row["issue"]
    # 사람이 확정하면 그때 바뀐다
    rows = [{"group": g["group"], "name": g["suggest"], "keep_dict": True} for g in service.icon_review(info.dir)["groups"]]
    out = service.confirm_icons(info.dir, rows)
    assert out.state == "confirmed" and out.replaced == 1 and _source(fd, 2) == "AB[[coin]]CD"
    assert out.changed_cells == []  # 이름만 정함: 다시 읽을 칸 없음


# ---------------------------------------------------------------- 확정된 아이콘도 표에
def test_confirmed_icons_are_listed_and_unchanged_rows_leave_source_alone(tmp_path):
    info, fd = _confirmed(tmp_path, pages=2)
    _set_src(fd, 1, "AB[[coin]]CD")  # 그대로
    r = service.find_icons(info.dir, suggest=False)
    assert r.state == "icons_review" and r.named == 2
    rows = service.icon_review(info.dir)["groups"]
    assert all(g["named"] == "coin" and g["dict_how"] == "named" and g["how_label"] == "확정됨" for g in rows)
    before = [_cell(fd, p) for p in (1, 2)]
    out = service.confirm_icons(info.dir, [{"group": g["group"], "name": "coin", "keep_dict": True} for g in rows])
    assert out.state == "confirmed" and out.replaced == 0 and out.removed == 0 and out.reused == []
    assert [_cell(fd, p) for p in (1, 2)] == before  # 아무것도 바뀌지 않음
    # 행이 없는 확정 아이콘(예전 엑셀 등)도 그대로 둔다
    service.find_icons(info.dir, suggest=False)
    assert service.confirm_icons(info.dir, []).state == "confirmed"
    assert [_cell(fd, p) for p in (1, 2)] == before


def test_confirmed_icon_rename_and_not_icon(tmp_path):
    info, fd = _confirmed(tmp_path, pages=3)
    service.find_icons(info.dir, suggest=False)
    rows = {g["where"][0]["page"]: g for g in service.icon_review(info.dir)["groups"]}
    out = service.confirm_icons(info.dir, [
        {"group": rows[1]["group"], "name": "gold", "ko": "금"},  # 이름 바꿈 -> 새 사전 항목 (그 줄 그림)
        {"group": rows[2]["group"], "not_icon": True},  # 아이콘 아님 -> 원문에서 뺌
        {"group": rows[3]["group"], "not_icon": True, "text": "●"}])  # 글자로
    assert out.state == "confirmed", out.blocking
    assert [_source(fd, p) for p in (1, 2, 3)] == ["AB[[gold]]CD", "ABCD", "AB●CD"]
    assert out.added == ["gold"] and out.removed == 1
    d = icons.load_dictionary(info)["icons"]
    assert d["gold"]["ko"] == "금" and (icons.icons_dir(info) / "gold.png").exists()
    # 아이콘 아님으로 뺀 칸은 ③ 원문 확인에서 다시 읽을 칸
    assert {c["page"] for c in out.changed_cells} == {2, 3}


def test_rebox_confirmed_icon_opens_token_and_confirm_puts_name_back(tmp_path):
    info, fd = _confirmed(tmp_path, pages=1)
    service.find_icons(info.dir, suggest=False)
    (row,) = service.icon_review(info.dir)["groups"]
    box = service.icon_place(info.dir, row["occ"])["box"]
    r = service.set_icon_box(info.dir, row["occ"], [box[0] - 2, box[1] - 2, box[2] + 2, box[3] + 2])
    assert r["group"] == row["group"] and _source(fd, 1) == "AB[[?]]CD"  # 토큰을 [[?]]로 열었다
    row2 = service.icon_review(info.dir)["groups"][0]
    assert row2["suggest"] == "coin" and row2["dict_how"] in ("prev", "same") and not row2["named"]
    out = service.confirm_icons(info.dir, [{"group": row2["group"], "name": "coin", "keep_dict": True}])
    assert out.state == "confirmed" and _source(fd, 1) == "AB[[coin]]CD"


def test_old_scan_json_with_shape_groups_loads_as_rows(tmp_path):
    (tmp_path / "scan.json").write_text(json.dumps({"occurrences": [
        {"id": f"o00{i}", "file": "f", "file_key": "k", "page": i, "region": "본문", "index": 0, "context": "A[[?]]",
         "png": "", "reason": "", "group": "g01", "line": 1, "order": 1, "box": []} for i in (1, 2)],
        "groups": {"g01": {"rep": "", "count": 2, "suggest": "coin", "ko": "금화"}}, "auto": {"coin": 1}}),
        encoding="utf-8")
    res = icons.load_scan(tmp_path)
    assert len({o.group for o in res.occurrences}) == 2
    assert all(res.groups[o.group]["suggest"] == "coin" and res.groups[o.group]["count"] == 1 for o in res.occurrences)
    assert all(o.named == "" and o.ti == -1 for o in res.occurrences)


# ---------------------------------------------------------------- 카드 보기 ‘아이콘 추가’
def test_insert_at_rules():
    # 같은 줄 토큰 오른쪽: 줄 끝 쪽 비율로 (CD 뒤)
    t = "AB[[?]]CD"
    boxes = {0: [100, 10, 120, 30]}
    assert icons.insert_at(t, (200, 10, 220, 30), boxes, (300, 40), span=(20, 180)) == len(t)
    # 같은 줄 토큰 왼쪽 가까이: 토큰 바로 앞 (AB 뒤)
    assert icons.insert_at(t, (80, 10, 96, 30), boxes, (300, 40), span=(20, 180)) == 2
    # 줄 맨 앞 (토큰 없는 둘째 줄, 띠로 줄을 고름)
    t2 = "AB[[?]]CD\nあいう"
    bands = [(10, 30), (50, 70)]
    assert icons.insert_at(t2, (0, 52, 15, 68), boxes, (300, 80), bands=bands, span=(20, 180)) == len("AB[[?]]CD\n")
    # 그 줄 가운데 (あ|いう 사이쯤)
    at = icons.insert_at(t2, (60, 52, 70, 68), boxes, (300, 80), bands=bands, span=(20, 140))
    assert t2[:at].endswith("あ") or t2[:at].endswith("い")
    # 토큰 안에는 넣지 않는다
    t3 = "[[a]][[b]]"
    at = icons.insert_at(t3, (48, 10, 52, 30), {0: [0, 10, 40, 30], 1: [60, 10, 100, 30]}, (100, 40))
    assert at == 5


def test_add_icon_inserts_placeholder_and_new_row(tmp_path):
    info, fd = _project(tmp_path, "1차", pages=1)
    service.find_icons(info.dir, suggest=False)
    (row,) = service.icon_review(info.dir)["groups"]
    box = service.icon_place(info.dir, row["occ"])["box"]
    w = box[2] - box[0]
    with pytest.raises(ValueError):
        service.add_icon_box(info.dir, row["occ"], (1, 1, 3, 3))
    r = service.add_icon_box(info.dir, row["occ"], (box[0] + 4 * w, box[1], box[0] + 5 * w, box[3]))  # CD 오른쪽
    assert _source(fd, 1) == "AB[[?]]CD[[?]]" and r["line"] == 1 and r["order"] == 2
    rows = service.icon_review(info.dir)["groups"]
    assert len(rows) == 2 and rows[1]["occ"] == r["occ"] and rows[1]["added"] and rows[1]["image"]
    items = service.icon_place(info.dir, r["occ"])["items"]
    assert [it["occ"] for it in items] == [row["occ"], r["occ"]]
    out = service.confirm_icons(info.dir, [{"group": rows[0]["group"], "name": "coin", "ko": "금화"},
                                           {"group": rows[1]["group"], "name": "blank", "ko": "빈칸"}])
    assert out.state == "confirmed", out.blocking
    assert _source(fd, 1) == "AB[[coin]]CD[[blank]]"
    assert _cell(fd)["icon_changed"] == ["아이콘을 추가함"]  # ③ 원문 확인에서 다시 읽는다


def test_add_icon_shifts_named_token_index(tmp_path):
    """확정된 아이콘 앞에 새 아이콘을 넣으면 그 확정 아이콘 줄의 토큰 순번(ti)도 밀린다."""
    info, fd = _confirmed(tmp_path, pages=1)
    _set_src(fd, 1, "AB[[coin]]CD")
    service.find_icons(info.dir, suggest=False)
    (row,) = service.icon_review(info.dir)["groups"]
    box = service.icon_place(info.dir, row["occ"])["box"]
    w = box[2] - box[0]
    r = service.add_icon_box(info.dir, row["occ"], (box[0] - 3 * w, box[1], box[0] - 2 * w, box[3]))  # AB 쪽
    src = _source(fd, 1)
    assert src.index("[[?]]") < src.index("[[coin]]")
    scan = icons.load_scan(info.game_dir / "work" / "icons")
    named = next(o for o in scan.occurrences if o.named)
    assert named.ti == 1 and next(o for o in scan.occurrences if o.id == r["occ"]).ti == 0
    items = service.icon_place(info.dir, row["occ"])["items"]
    assert [it["state"] for it in items] == ["manual", "named"]


# ---------------------------------------------------------------- 화면
def test_review_dialog_card_order_problem_cards_first_and_bands(qapp, tmp_path):
    from app.qt.icon_review import IconReviewDialog

    info, fd = _confirmed(tmp_path, pages=3)
    _set_src(fd, 3, "AB[[?]]CD")
    service.find_icons(info.dir, suggest=False)
    rv = service.icon_review(info.dir)
    # 3쪽 줄의 이름을 지워 문제로 만든다
    for g in rv["groups"]:
        if g["where"][0]["page"] == 3:
            g["suggest"], g["ko"] = "", ""
    dlg = IconReviewDialog(info.dir, rv)
    pages = [g["where"][0]["page"] for g in dlg.review["groups"]]
    assert pages == [3, 1, 2]  # 문제 카드 먼저, 그다음 카드 순서
    assert dlg._card_of[dlg.widgets[0][0]][1] and not dlg._card_of[dlg.widgets[1][0]][1]
    assert dlg.table.item(0, dlg.C_PAGES).font().bold()
    # 1.24: 카드 칸은 옅은 띠(구분만), 분홍은 그 줄 자체가 문제일 때만 (상태·예문 칸)
    assert dlg.table.item(0, dlg.C_PAGES).background().color().name().lower() in dlg.BAND_COLORS_LOWER
    assert dlg.table.item(0, dlg.C_STATE).background().color().name().lower() == dlg.PROBLEM_COLOR.lower()
    assert dlg.table.item(1, dlg.C_STATE).background().color().name().lower() != dlg.PROBLEM_COLOR.lower()
    dlg.widgets[0][1].setText("coin")  # 사전 이름: 한국어 이름이 채워져 문제가 아니게
    assert dlg.widgets[0][3].text() == "금화"
    assert dlg.table.item(0, dlg.C_STATE).background().color().name().lower() != dlg.PROBLEM_COLOR.lower()
    dlg._sort_by(dlg.C_PAGES)  # 다시 누르면 카드 순서 반대 (문제 카드 없음)
    assert [g["where"][0]["page"] for g in dlg.review["groups"]] == [3, 2, 1]
    dlg.close()


def test_place_dialog_add_icon_and_rename(qapp, tmp_path, monkeypatch):
    from app.qt.icon_place import IconPlaceDialog
    from app.qt.icon_review import IconReviewDialog

    info, fd = _project(tmp_path, "1차", pages=1)
    service.find_icons(info.dir, suggest=False)
    dlg = IconReviewDialog(info.dir, service.icon_review(info.dir))
    g = dlg.review["groups"][0]
    place = IconPlaceDialog(info.dir, g, dlg._row_label, dlg._after_place, dlg, on_rename=dlg.rename_row)
    assert place.card_view.pixmap() is not None and not place.card_view.pixmap().isNull()  # 카드 전체
    assert not place.add_btn.isEnabled()
    # 이름 고치기: 표의 그 줄에 적힌다
    place.name_edit.setText("coin")
    place._rename()
    assert dlg.widgets[0][1].text() == "coin" and place.items.item(0).text().endswith("찾음")
    assert "1번 coin" in place.items.item(0).text()
    # 아이콘 추가: CD 오른쪽 빈 곳을 감싸 넣는다
    box = place._place["box"]
    w = box[2] - box[0]
    place.view.new = [box[0] + 4 * w, box[1], box[0] + 5 * w, box[3]]
    place._drawn()
    assert place.add_btn.isEnabled()
    place._add()
    assert _source(fd, 1) == "AB[[?]]CD[[?]]"
    assert place.items.count() == 2 and len(dlg.widgets) == 2  # 표에 새 줄
    assert dlg.widgets[0][1].text() == "coin"  # 적어 둔 값은 그대로
    assert place.outcome[0] == "saved"
    place.close()
    dlg.close()


def test_confirm_stops_without_half_changes_when_named_token_is_gone(tmp_path):
    info, fd = _confirmed(tmp_path, pages=2)
    service.find_icons(info.dir, suggest=False)
    rows = {g["where"][0]["page"]: g for g in service.icon_review(info.dir)["groups"]}
    _set_src(fd, 2, "ABCD")  # 스캔 뒤 2쪽 원문이 바뀜 (토큰이 없어짐)
    before_dict = json.dumps(icons.load_dictionary(info), sort_keys=True)
    out = service.confirm_icons(info.dir, [{"group": rows[1]["group"], "name": "gold", "ko": "금"},
                                           {"group": rows[2]["group"], "name": "gold2", "ko": "금2"}])
    assert out.state != "confirmed" and out.blocking
    assert json.dumps(icons.load_dictionary(info), sort_keys=True) == before_dict  # 사전도
    assert _source(fd, 1) == "AB[[coin]]CD"  # 앞 토큰도 그대로


def test_suggest_sentence_marks_this_icon_only():
    from app.icons import Occurrence
    from app.icons_review import marked_context

    o = Occurrence("o1", "f", "f", 3, "본문", 1, "[[?]]を戻した数だけ[[?]]を軽減 / [[?]]")
    o.ti = 2
    assert marked_context(o) == "[[?]]を戻した数だけ[[?]]を軽減 / [[THIS]]"
    o.ti = -1  # 예전 스캔: 몇 번째 [[?]]인지로
    assert marked_context(o) == "[[?]]を戻した数だけ[[THIS]]を軽減 / [[?]]"
    o2 = Occurrence("o2", "f", "f", 3, "본문", 0, "[[crown]]を[[?]]")
    o2.ti = 1
    assert marked_context(o2) == "[[crown]]を[[THIS]]"
