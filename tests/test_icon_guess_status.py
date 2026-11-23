"""② 원문 확인: 확정 전 아이콘 이름 중 아무도 보지 않은 이름(Claude 추측·사전과 비슷한 모양)만 ‘볼 것’이다.
사람이 고른 이름·사전과 모양이 같은 이름은 확정 전이어도 문제가 아니다 (‘모두 확정’으로 넣는다)."""

from __future__ import annotations

import json

import pytest

from app import service
from tests.test_icons import _project


def _set_group(info, **gi_update):
    path = info.game_dir / "work" / "icons" / "scan.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for gi in data["groups"].values():
        gi.update(gi_update)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _page1(info, fd):
    rows = service.source_check_rows(info.dir, [{"dir": fd, "key": "카드/cards"}])
    return next(r for c in rows if c["page"] == 1 for r in c["regions"] if r["region"] == "본문")


@pytest.mark.parametrize("gi, guess, status, reason", [
    ({"suggest": "lily"}, "suggest", "check", "이름 추측 1개"),
    ({"dict": "lily", "dict_how": "variant"}, "similar", "check", "사전과 비슷한 모양 이름 1개"),
    ({"dict": "lily", "dict_how": "legacy"}, "similar", "check", "사전과 비슷한 모양 이름 1개"),
    ({"dict": "lily", "dict_how": "same"}, "", "todo", None),
    ({"suggest": "rose", "staged": {"name": "lily"}}, "", "todo", None),  # 사람이 고른 이름
])
def test_unchecked_icon_names_need_a_look(tmp_path, gi, guess, status, reason):
    info, fd = _project(tmp_path, "guess", pages=1)
    service.find_icons(info.dir, suggest=False)
    _set_group(info, **gi)
    r = _page1(info, fd)
    assert r["pending"] and r["icons"][0]["state"] == "named"
    assert r["icons"][0]["guess"] == guess
    assert r["status"] == status
    if reason:
        assert reason in r["reasons"]


def test_guess_with_order_conflict_is_check():
    icons_ = [{"state": "named", "guess": "suggest", "order_conflict": True}]
    status, reasons = service.region_status({"source": "A [[?]]"}, icons_)
    assert status == "check" and any("순서" in x for x in reasons)


def test_guess_keeps_other_check_reasons():
    from app.extract import ICON_REVIEW_LABEL

    icons_ = [{"state": "named", "guess": "similar"}]
    status, reasons = service.region_status({"source": "A [[?]]", "label": ICON_REVIEW_LABEL}, icons_)
    assert status == "check" and "사전과 비슷한 모양 이름 1개" in reasons and any("아이콘 고친 뒤" in x for x in reasons)


def test_guess_ignored_in_kept_whole_region():
    icons_ = [{"state": "named", "guess": "suggest"}]
    assert service.region_status({"source": "[[?]]", "keep_mode": "whole"}, icons_)[0] == "keep"


def test_unnamed_icon_still_fix_over_guess():
    icons_ = [{"state": "unnamed"}, {"state": "named", "guess": "suggest"}]
    assert service.region_status({"source": "[[?]] [[?]]"}, icons_)[0] == "fix"
