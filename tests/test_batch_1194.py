"""1.20.0: 원문 확인에서 처음 정밀 읽기 숨기기, ‘아이콘 아님’ 자리가 다시 읽은 결과로 되살아나지 않기."""

from __future__ import annotations

import json

from app import service
from app.qt.step_common import fill_icon_names, precise_index
from tests.test_icons import _project


def _set_cell(fd, page, region, **kw) -> None:
    p = fd / "extract" / "pages" / f"page-{page:03d}.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d["regions"][region].update(kw)
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def _cell(fd):
    return next(c for c in service.extracted_cells(fd) if c["page"] == 1 and c["region"] == "본문")


# ---------------------------------------------------------------- 정밀 읽기로 다시 읽었으면 처음 정밀 읽기는 숨긴다
def test_first_opus_hidden_when_opus_reread(tmp_path):
    info, fd = _project(tmp_path, "hide", pages=1)
    _set_cell(fd, 1, "본문", readings=[{"model": "claude-sonnet-5-5", "source": "S"},
                                      {"model": "claude-opus-5-5", "source": "O1"}],
              rereads=[{"model": "claude-opus-5-5", "source": "O2"}])
    c = _cell(fd)
    assert [(r["source"], r["reread"]) for r in c["readings"]] == [("S", False), ("O2", True)]
    assert c["readings"][precise_index(c["readings"])]["source"] == "O2"
    assert c["label"] in ("두 번 확인", "결론 못 냄")  # 칸 상태는 두 번 읽기 결과 그대로


def test_first_opus_kept_when_only_sonnet_reread(tmp_path):
    info, fd = _project(tmp_path, "keep", pages=1)
    _set_cell(fd, 1, "본문", readings=[{"model": "claude-sonnet-5-5", "source": "S"},
                                      {"model": "claude-opus-5-5", "source": "O1"}],
              rereads=[{"model": "claude-sonnet-5-5", "source": "S2"}])
    assert [r["source"] for r in _cell(fd)["readings"]] == ["S", "O1", "S2"]


# ---------------------------------------------------------------- ‘아이콘 아님’으로 뺀 자리는 [[?]]로 되살아나지 않는다
def test_removed_icon_slots_are_not_revived():
    src = "いずれかを2つまで選択し実行\n[[workshop]] / / \n工房 馬車 商館"
    rd = "いずれかを2つまで選択し実行\n[[?]] / [[?]] / [[?]]\n工房 馬車 商館"
    assert fill_icon_names(rd, src) == "いずれかを2つまで選択し実行\n[[workshop]] /  / \n工房 馬車 商館"
    assert fill_icon_names("×[[?]]-1\n[[?]] [[?]] [[?]]", "×[[gray]]-1\n[[star]] [[star]] ") == \
        "×[[gray]]-1\n[[star]] [[star]] "
    assert fill_icon_names("実行\n[[?]] / [[?]][[?]]", "実行\n / [[w5]] [[w2]]") == "実行\n / [[w5]] [[w2]]"  # 원문 빈칸을 따른다


def test_not_icon_with_text_follows_source_text():
    # ‘아이콘 아님’에 글자를 적은 자리 (예: 最): 원문 글자를 따른다
    assert fill_icon_names("[[?]]大[[?]]を得る", "最大[[coin]]を得る") == "最大[[coin]]を得る"


def test_source_still_has_unknown_keeps_old_behavior():
    # 아직 ② 아이콘 정의 전(원문에 [[?]]가 있음): 읽은 [[?]]를 빼지 않는다
    assert fill_icon_names("A[[?]]B[[?]]", "A[[?]]B") == "A[[?]]B[[?]]"


def test_new_icon_found_by_reread_is_kept_when_no_icons_decided():
    # 아이콘을 정한 적 없는 칸: 다시 읽기가 새로 찾은 [[?]]는 그대로 (원문 확인에서 보이고 ② 아이콘 정의로 간다)
    assert fill_icon_names("盗掘[[?]]フェレット", "盗賊フェレット") == "盗掘[[?]]フェレット"
    assert fill_icon_names("盗掘[[?]]フェレット", "盗賊フェレット", removed=True) == "盗賊フェレット"


def test_apply_region_records_removed_icons(tmp_path):
    from app import icons

    info, fd = _project(tmp_path, "rm", pages=1)
    _set_cell(fd, 1, "본문", source="A[[?]]B[[?]]C[[?]]")
    icons.apply_region(fd, 1, "본문", {0: "coin", 1: "", 2: "=最"})
    c = _cell(fd)
    assert c["source"] == "A[[coin]]BC最" and c["icon_removed"] == 2
    service.save_source(fd, 1, "본문", c["source"])  # 사람이 저장해도 남는다
    assert _cell(fd)["icon_removed"] == 2


def test_text_typed_for_removed_icon_is_kept():
    # ‘아이콘 아님’에 적은 글자(+1)가 다시 읽은 아이콘 자리와 짝지어져 사라지지 않는다 (리뷰)
    assert fill_icon_names("[[?]][[?]]", "+1[[coin]]", removed=True) == "+1[[coin]]"


def test_icon_removed_zero_is_known_not_guessed(tmp_path):
    from app import icons

    info, fd = _project(tmp_path, "zero", pages=1)
    _set_cell(fd, 1, "본문", source="A[[?]]B")
    icons.apply_region(fd, 1, "본문", {0: "coin"})  # 뺀 것 없음: 0으로 적는다
    assert _cell(fd)["icon_removed"] == 0
    from app.qt.source_review import _removed
    assert _removed(_cell(fd)) is False and _removed({"icon_removed": None}) is None
