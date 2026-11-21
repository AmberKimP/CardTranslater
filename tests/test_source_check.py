"""② 원문 확인 (아이콘 정의 + 원문 확인 한 창): 영역 상태, 확정 전 값, 영역마다 확정(부분 확정), 찾기 결과 맞추기."""

from __future__ import annotations

import json

from app import icons, icons_stage, service
from tests.test_icons import _project, _source


def _work(info):
    return info.game_dir / "work" / "icons"


def _files(fd):
    return [{"dir": fd, "key": "카드/cards"}]


def _region(cards, page, region="본문"):
    return next(r for c in cards if c["page"] == page for r in c["regions"] if r["region"] == region)


def _group_of(info, page):
    scan = icons.load_scan(_work(info))
    return next(o.group for o in scan.occurrences if o.page == page)


def test_rows_show_unnamed_icon_as_fix_and_staged_name_as_pending(tmp_path):
    info, fd = _project(tmp_path, "rows", pages=2)
    service.find_icons(info.dir, suggest=False)
    cards = service.source_check_rows(info.dir, _files(fd))
    assert [c["page"] for c in cards] == [1, 2]
    r1 = _region(cards, 1)
    assert r1["status"] == "fix" and r1["icons"][0]["state"] == "unnamed" and not r1["pending"]
    service.stage_icon_names(info.dir, [{"group": _group_of(info, 1), "name": "lily", "ko": "백합"}])
    r1 = _region(service.source_check_rows(info.dir, _files(fd)), 1)
    assert r1["status"] != "fix" and r1["pending"] and r1["icons"][0]["state"] == "named"
    assert _source(fd, 1) == "AB[[?]]CD"  # 확정 전: 원문은 그대로


def test_confirm_one_region_keeps_scan_and_other_regions(tmp_path):
    info, fd = _project(tmp_path, "part", pages=2)
    service.find_icons(info.dir, suggest=False)
    g1, g2 = _group_of(info, 1), _group_of(info, 2)
    service.stage_icon_names(info.dir, [{"group": g1, "name": "lily", "ko": "백합"},
                                        {"group": g2, "name": "rose", "ko": "장미"}])
    r = service.confirm_regions(info.dir, [(fd, 1, "본문")])
    assert r["state"] == "confirmed", r["blocking"]
    assert _source(fd, 1) == "AB[[lily]]CD" and _source(fd, 2) == "AB[[?]]CD"
    assert (_work(info) / "scan.json").exists()  # 찾기 결과가 남는다 (다른 칸의 확정 전 값이 남게)
    scan = icons.load_scan(_work(info))
    o1 = next(o for o in scan.occurrences if o.page == 1)
    o2 = next(o for o in scan.occurrences if o.page == 2)
    assert o1.named == "lily" and o1.index == -1 and o1.ti == 0
    assert not o2.named and o2.index == 0 and scan.groups[g2]["staged"]["name"] == "rose"
    assert icons_stage.stale_regions(info, scan) == set()  # 원문과 찾기 결과가 맞는다
    assert r["reread"] == [] and r["saved"] == 1  # 자리를 바꾸지 않았으니 다시 읽지 않고 ‘사람이 확인’
    cards = service.source_check_rows(info.dir, _files(fd))
    assert _region(cards, 1)["status"] == "ok" and _region(cards, 1)["icons"][0]["state"] == "ok"
    assert _region(cards, 2)["pending"]
    # 남은 영역도 확정
    r = service.confirm_regions(info.dir, [(fd, 2, "본문")])
    assert r["state"] == "confirmed" and _source(fd, 2) == "AB[[rose]]CD"


def test_moved_icon_region_is_reread_after_confirm(tmp_path):
    info, fd = _project(tmp_path, "mv", pages=1)
    service.find_icons(info.dir, suggest=False)
    scan = icons.load_scan(_work(info))
    o = scan.occurrences[0]
    service.set_icon_box(info.dir, o.id, o.box)  # 사람이 자리를 다시 잡음
    service.stage_icon_names(info.dir, [{"group": _group_of(info, 1), "name": "lily", "ko": "백합"}])
    r = service.confirm_regions(info.dir, [(fd, 1, "본문")])
    assert [(c["page"], c["region"]) for c in r["reread"]] == [(1, "본문")] and r["saved"] == 0


def test_unfinished_icons_stay_and_are_not_reread(tmp_path):
    info, fd = _project(tmp_path, "unf", pages=1)
    service.find_icons(info.dir, suggest=False)
    r = service.confirm_regions(info.dir, [(fd, 1, "본문")])  # 이름을 적지 않음
    assert r["state"] == "confirmed" and r["reread"] == [] and r["still"] == [(fd, 1, "본문")]
    assert _source(fd, 1) == "AB[[?]]CD"


def test_edited_text_is_saved_after_icons(tmp_path):
    info, fd = _project(tmp_path, "ed", pages=1)
    service.find_icons(info.dir, suggest=False)
    service.stage_icon_names(info.dir, [{"group": _group_of(info, 1), "name": "lily", "ko": "백합"}])
    r = service.confirm_regions(info.dir, [(fd, 1, "본문")],
                                {(fd, 1, "본문"): {"source": "AB[[lily]]CDE", "breaks": None}})
    assert r["state"] == "confirmed" and _source(fd, 1) == "AB[[lily]]CDE" and r["saved"] == 1


def test_not_icon_removes_token_and_shifts_remaining_index(tmp_path):
    info, fd = _project(tmp_path, "two", pages=1)
    f = fd / "extract" / "pages" / "page-001.json"
    rec = json.loads(f.read_text(encoding="utf-8"))
    rec["regions"]["본문"]["source"] = "A[[?]]B[[?]]C"
    f.write_text(json.dumps(rec), encoding="utf-8")
    service.find_icons(info.dir, suggest=False)
    scan = icons.load_scan(_work(info))
    first = next(o for o in scan.occurrences if o.index == 0)
    service.stage_icon_names(info.dir, [{"group": first.group, "not_icon": True}])
    r = service.confirm_regions(info.dir, [(fd, 1, "본문")])
    assert r["state"] == "confirmed", r["blocking"]
    assert _source(fd, 1) == "AB[[?]]C"
    scan = icons.load_scan(_work(info))
    assert [(o.index, o.ti) for o in scan.occurrences] == [(0, 0)]
    assert icons_stage.stale_regions(info, scan) == set()


def test_changed_source_is_rescanned_only_there(tmp_path):
    info, fd = _project(tmp_path, "sync", pages=2)
    service.find_icons(info.dir, suggest=False)
    g1 = _group_of(info, 1)
    service.stage_icon_names(info.dir, [{"group": g1, "name": "lily", "ko": "백합"}])
    service.save_source(fd, 2, "본문", "AB[[?]]C[[?]]D")  # 사람이 원문에 아이콘 하나를 더 적음
    st = service.ensure_icon_scan(info.dir)
    assert st["state"] == "synced" and st["regions"] == 1
    scan = icons.load_scan(_work(info))
    assert scan.groups[g1]["staged"]["name"] == "lily"  # 다른 칸의 확정 전 값은 그대로
    p2 = sorted((o.index for o in scan.occurrences if o.page == 2))
    assert p2 == [0, 1] and len({o.id for o in scan.occurrences}) == len(scan.occurrences)
    assert service.ensure_icon_scan(info.dir)["state"] == "ok"


def test_icon_kinds_groups_same_shape(tmp_path):
    info, fd = _project(tmp_path, "kinds", pages=3)
    service.find_icons(info.dir, suggest=False)
    kinds = service.icon_kinds(info.dir)
    assert len(kinds) == 1 and kinds[0]["uses"] == 3 and kinds[0]["state"] == "unnamed"
    service.stage_icon_names(info.dir, [{"group": g, "name": "lily", "ko": "백합"} for g in kinds[0]["groups"]])
    k = service.icon_kinds(info.dir)[0]
    assert k["name"] == "lily" and k["state"] == "pending"


def _set_scan(info, fd):
    """1쪽 원문 ‘AB[[?]][[?]]CD’: 둘째 자리는 찾기가 스스로 첫 자리 세트로 합친 자리(auto)."""
    from tests.test_icons import _add_run_partner

    host, extra = _add_run_partner(info, fd)
    sp = _work(info) / "scan.json"
    scan = json.loads(sp.read_text(encoding="utf-8"))
    for o in scan["occurrences"]:
        if o["id"] == extra:
            o["set_of"], o["group"] = host, f"세트 {extra}"
    scan["groups"].pop(f"위치 못 찾음 {extra}")
    scan["groups"][f"세트 {extra}"] = {"rep": "", "count": 1, "not_icon": True, "absorbed": host, "auto": True}
    sp.write_text(json.dumps(scan, ensure_ascii=False), encoding="utf-8")
    return host


def test_preview_matches_what_confirm_writes_for_auto_sets(tmp_path):
    info, fd = _project(tmp_path, "set", pages=1)
    _set_scan(info, fd)
    service.stage_icon_names(info.dir, [{"group": _group_of(info, 1), "name": "lily", "ko": "백합"}])
    row = _region(service.source_check_rows(info.dir, _files(fd)), 1)
    assert [i["state"] for i in row["icons"]] == ["named"]  # 세트의 나머지 자리는 줄로 보이지 않는다
    preview = service.preview_source(row)
    assert preview == "AB[[lily]]CD"
    r = service.confirm_regions(info.dir, [(fd, 1, "본문")])
    assert r["state"] == "confirmed" and _source(fd, 1) == preview


def test_confirm_all_does_not_mark_unseen_regions_checked(tmp_path):
    """목록 창 ‘모두 확정’(mark_checked=False): 아이콘만 넣고 ‘결론 못 냄’·확실하지 않음은 그대로."""
    info, fd = _project(tmp_path, "nochk", pages=1)
    f = fd / "extract" / "pages" / "page-001.json"
    rec = json.loads(f.read_text(encoding="utf-8"))
    rec["regions"]["본문"]["uncertain"] = True
    f.write_text(json.dumps(rec), encoding="utf-8")
    service.find_icons(info.dir, suggest=False)
    service.stage_icon_names(info.dir, [{"group": _group_of(info, 1), "name": "lily", "ko": "백합"}])
    r = service.confirm_regions(info.dir, [(fd, 1, "본문")], mark_checked=False)
    assert r["state"] == "confirmed" and r["saved"] == 0 and _source(fd, 1) == "AB[[lily]]CD"
    row = _region(service.source_check_rows(info.dir, _files(fd)), 1)
    assert row["uncertain"] and row["status"] == "check"
