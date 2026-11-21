"""1.24 ② 원문 확인: 위아래 아이콘 자동 합치기 금지(A1), 아이콘 아님 줄은 문제 아님(A3), 분홍은 문제 줄만(A4),
카드 칸 합치기·버튼 하나(A5), 문장만 원본 그대로(A6·A8), 자동으로 닫지 않음(A10), 칸 옮기기(A11), 뜻 열 없음(A13),
표시 크기 휠 무시(A14), 확정 실패 알림의 줄 번호가 먼저 + 그 줄로(A15)."""

from __future__ import annotations

import json
import os

import pytest

from app import icons, service
from tests.test_icons import _add_run_partner, _project, _source
from tests.test_keep_paragraphs import _cell, _set_src, sentence_mode  # noqa: F401 - fixture

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _scan_path(info):
    return info.game_dir / "work" / "icons" / "scan.json"


def _edit_scan(info, fn):
    sp = _scan_path(info)
    scan = json.loads(sp.read_text(encoding="utf-8"))
    fn(scan)
    sp.write_text(json.dumps(scan, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- A1: 위아래 아이콘을 자동으로 합치지 않는다
def test_crosses_other_line_catches_box_reaching_next_text_line():
    """찾은 상자 안에 다른 글 줄의 글자가 있으면(공주와기사 5쪽: 1줄 백합 문장 상자가 2줄 ‘加える’까지) 걸린다.
    상자 옆에만 글자가 있는 큰 아이콘, 아이콘 그림 속 작은 글씨는 걸리지 않는다."""
    em = 30.0
    line1 = [(x, 20, 24, 28, 400) for x in (10, 40, 70, 300, 330, 360)]  # 1줄 글자 (가운데 34)
    line2 = [(x, 80, 24, 28, 400) for x in (10, 40, 70, 300, 330, 360)]  # 2줄 글자 (가운데 94)
    comps = line1 + line2
    # 1줄 아이콘(x 100~200)인데 상자가 2줄까지 내려와 2줄 글자(x 120)를 감쌈
    reach = comps + [(120, 80, 24, 28, 400)]
    assert icons.crosses_other_line(reach, (100, 15, 200, 110), em, 34)
    # 한 줄 높이 상자: 괜찮음
    assert not icons.crosses_other_line(reach, (100, 15, 200, 52), em, 34)
    # 글 두 줄 왼쪽의 큰 아이콘: 상자 안에 글자가 없다
    assert not icons.crosses_other_line(comps, (100, 15, 200, 110), em, 34)
    # 아이콘 속 작은 글씨(상자 밖으로 이어지지 않는 높이 150): 다른 글 줄이 아니다
    label = comps + [(130, 140, 20, 20, 300), (155, 140, 20, 20, 300)]
    assert not icons.crosses_other_line(label, (100, 15, 200, 170), em, 34)


def test_named_tokens_sharing_one_box_are_both_unlocated():
    """공주와기사 6쪽: 2줄 crown_4·city 두 토큰이 1줄 사교 아이콘까지 감싼 같은 상자로 잡혔다. 같은 상자는 적어도 하나가
    틀렸으므로 둘 다 ‘위치 못 찾음’ (자리를 사람이 잡는다)."""
    a = icons.Occurrence("o1", "f", "k", 1, "본문", -1, "", png="a.png", box=[10, 0, 100, 90], named="crown_4", ti=1)
    b = icons.Occurrence("o2", "f", "k", 1, "본문", -1, "", png="b.png", box=[12, 2, 101, 90], named="city", ti=2)
    c = icons.Occurrence("o3", "f", "k", 1, "본문", 0, "", png="c.png", box=[200, 0, 260, 60], ti=0)
    icons._drop_shared_named([a, b, c])
    assert (a.box, a.png, b.box, b.png) == ([], "", [], "")
    assert a.reason == b.reason == icons.SHARED_BOX_REASON
    assert c.box == [200, 0, 260, 60] and c.png  # 다른 자리는 그대로


def _vertical_partner(info, fd, extra_box):
    """1쪽 원문을 ‘AB[[?]]\\n[[?]]CD’로: 둘째 자리(extra)는 아랫줄인데 그림·상자가 있다 (찾은 자리)."""
    host, extra = _add_run_partner(info, fd)
    p = fd / "extract" / "pages" / "page-001.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d["regions"]["본문"]["source"] = "AB[[?]]\n[[?]]CD"
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

    def fix(scan):
        h = next(o for o in scan["occurrences"] if o["id"] == host)
        for o in scan["occurrences"]:
            if o["id"] == extra:
                o.update(line=2, order=1, png=h["png"], box=list(extra_box), reason="")
                scan["groups"].pop(o["group"], None)
                o["group"] = "g90"
                scan["groups"]["g90"] = {"rep": extra, "count": 1}
    _edit_scan(info, fix)
    return host, extra


def test_vertical_neighbour_is_never_suggested_or_merged_by_found_path(tmp_path):
    info, fd = _project(tmp_path, "vert", pages=1)
    work = info.game_dir / "work" / "icons"
    service.find_icons(info.dir, suggest=False)
    hb = service.icon_place(info.dir, icons.load_scan(work).occurrences[0].id)["box"]  # [300, 54, 384, 139]
    eb = [hb[2] + 6, hb[1] + 6, hb[2] + 86, hb[3] + 1]  # 아랫줄 자리 (그림이 작아 옆에 둠: 줄 번호로만 아랫줄)
    host, extra = _vertical_partner(info, fd, eb)
    part = icons.run_partners(info, work, host)
    assert part["boxed"] == [extra] and part["boxed_line"] == [] and part["other_line"] == [extra]
    # 위치 못 찾은 자리에 ‘아랫줄 아이콘에 포함됨’을 제안하지 않는다 (같은 줄일 때만)
    _edit_scan(info, lambda s: [o.update(png="", box=[]) for o in s["occurrences"] if o["id"] == host])
    assert service.icon_place(info.dir, host)["run_host"] == []
    # 사람이 상자를 다시 잡을 때: 아랫줄 자리를 반쯤 덮은 것만으로는 ‘한 세트로’ 후보가 아니다 (같은 줄은 0.4면 후보)
    half = [hb[0], hb[1], eb[0] + (eb[2] - eb[0]) // 2, hb[3]]
    r = service.set_icon_box(info.dir, host, half)
    assert r["run_unfound"] == [] and not r["run_vertical"]
    # 거의 다 감싸면 묻기는 하지만(사람이 고름) 위아래가 섞였다고 알린다 (화면의 기본 답은 ‘따로 있는 아이콘’)
    both = [hb[0], hb[1], eb[2] + 2, max(hb[3], eb[3]) + 2]
    r = service.set_icon_box(info.dir, host, both)
    assert r["run_unfound"] == [extra] and r["run_vertical"]
    # 묻기만 하고 합치지는 않았다
    rev = service.icon_review(info.dir)
    assert not next(g for g in rev["groups"] if g["occ"] == extra)["not_icon"]


# ---------------------------------------------------------------- A6·A8: 문장만 원본 그대로
SRC3 = "Aを得る。\n[[?]]を払う。\n[[?]]Bを得る。"


def _three_sentences(tmp_path, name):
    info, fd = _project(tmp_path, name, pages=1)
    host, extra = _add_run_partner(info, fd)
    _set_src(fd, SRC3)
    # 1.24: 문장 끝으로는 문단을 나누지 않는다 — 사람이 ②에서 문장마다 [====]로 나눈 칸 (paragraph_breaks)
    for p in (fd / "extract").glob("**/page-001.json"):
        d = json.loads(p.read_text(encoding="utf-8"))
        d["regions"]["본문"]["paragraph_breaks"] = [1, 2]
        p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return info, fd, host, extra


def test_old_icon_paragraph_keep_became_whole_when_every_sentence_has_icon(tmp_path, sentence_mode):  # noqa: F811
    """A6 원인: 예전 ‘이 칸 원본 그대로’는 문단마다 다른 글꼴 영역에서 아이콘이 든 문단을 모두 남겼다 — 아이콘이 든
    문장이 여럿이면 거의 칸 전체가 남는다. 카드 보기는 이제 ‘칸 전체’(whole=True)와 ‘이 문장만’을 따로 쓴다."""
    info, fd, host, extra = _three_sentences(tmp_path, "a6")
    r = service.keep_cell_original(info.dir, host, True)
    assert r["mode"] == "partial" and _cell(fd)["keep_paragraphs"]["lines"] == [1, 2]  # 아이콘 문장 둘 다
    service.keep_cell_original(info.dir, host, False)
    r = service.keep_cell_original(info.dir, host, True, whole=True)
    assert r["mode"] == "whole" and _cell(fd)["keep_original"]


def test_keep_sentence_keeps_only_that_sentence_and_toggles_back(tmp_path, sentence_mode):  # noqa: F811
    info, fd, host, extra = _three_sentences(tmp_path, "a8")
    r = service.keep_sentence_original(info.dir, host, [0], True)  # host = 2줄의 [[?]] (토큰 0)
    assert r["mode"] == "partial" and r["lines"] == [1] and r["changed"] == [1] and r["not_icon"] == [host]
    cell = _cell(fd)
    assert cell["keep_paragraphs"]["lines"] == [1] and not cell.get("keep_original")
    boxes1 = cell["keep_paragraphs"].get("boxes_pt")
    assert boxes1 and len(boxes1) == 1  # 1문장(host)의 아이콘 자리: 글자 지우기가 남길 줄을 찾고 아이콘을 지킨다
    assert service.sentence_kept(info.dir, host, [0]) and not service.sentence_kept(info.dir, host, [1])
    states = {it["occ"]: it["state"] for it in service.icon_place(info.dir, extra)["items"]}
    assert states[host] == "not_icon" and states[extra] != "not_icon"  # 다른 문장의 아이콘은 그대로
    # 다른 문장도 더한다: 앞 문장(이미 아이콘 아님이 된 host)의 자리를 잃지 않는다
    r = service.keep_sentence_original(info.dir, extra, [1], True)
    assert r["lines"] == [1, 2]
    assert _cell(fd)["keep_paragraphs"].get("boxes_pt") == boxes1  # extra는 위치 못 찾음이라 상자 없음
    # 하나씩 되돌린다 (남은 문장이 없으면 칸의 원본 그대로가 꺼진다)
    r = service.keep_sentence_original(info.dir, extra, [1], False)
    assert r["lines"] == [1] and r["restored"] == [extra]
    assert _cell(fd)["keep_paragraphs"].get("boxes_pt") == boxes1
    r = service.keep_sentence_original(info.dir, host, [0], False)
    assert r["mode"] == "" and r["restored"] == [host]
    cell = _cell(fd)
    assert "keep_paragraphs" not in cell and not cell.get("keep_original")
    assert _source(fd, 1) == SRC3  # 원문은 그대로


def test_one_sentence_keep_still_rereads_changed_icons_in_other_sentences(tmp_path, sentence_mode):  # noqa: F811
    """문장 하나만 원본 그대로여도 나머지 문장(번역함)에 바뀐 아이콘이 있으면 ‘다시 읽기’ 표시가 남는다.
    아이콘 줄을 모두 남기면 표시를 지운다. 칸 전체 원본 그대로는 다시 읽지 않는다."""
    info, fd, host, extra = _three_sentences(tmp_path, "rr")
    p = fd / "extract" / "pages" / "page-001.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d["regions"]["본문"].update(icon_changed=["추가"], icon_reread=True)
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    service.keep_sentence_original(info.dir, host, [0], True)
    assert _cell(fd).get("icon_changed") and service.icon_reread_pending([fd])
    service.keep_sentence_original(info.dir, extra, [1], True)  # 아이콘 줄을 모두 남김
    assert not _cell(fd).get("icon_changed") and service.icon_reread_pending([fd]) == []


# ---------------------------------------------------------------- 2A 표
def test_overlap_absorb_skips_other_line_slots():
    marks = [{"box": [0, 0, 10, 10], "occ": "o2", "row": 0}, {"box": [1, 1, 11, 11], "occ": "o3", "row": 1}]
    assert icons.overlapping_occ(marks, [0, 0, 10, 10], exclude="o1", row=0) == ["o2"]
    assert icons.overlapping_occ(marks, [0, 0, 10, 10], exclude="o1") == ["o2", "o3"]  # 줄을 모르면 예전처럼


# ---------------------------------------------------------------- 2B: 닫지 않음·칸 옮기기