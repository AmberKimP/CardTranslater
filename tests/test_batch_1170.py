"""1.17.0 묶음: 초기화·삭제 뒤 진행 상태, 아이콘 정의 변경 → 원문 확인, 카드 보기의 모든 아이콘 표시."""

from __future__ import annotations

import json

from app import icons, service, workspace
from tests.test_icons import _add_run_partner, _project


# ---------------------------------------------------------------- A. 진행 상태가 데이터와 맞게
def test_adding_files_returns_project_to_extracting(tmp_path):
    """번역까지 끝난 프로젝트에 새 카드를 넣으면 ‘추출 중’으로 돌아간다 (아이콘 정의~전체 번역이 끝난 것처럼 보이지 않게)."""
    info, fd = _project(tmp_path, "st", pages=1)
    workspace.set_status(info, "translated", glossary_version=3)
    pdf = next((fd / "source").glob("*.pdf"))
    r = service.add_files(info.dir, [pdf], "새 그룹", new_group=True)
    assert r["added"]
    again = workspace.load_project_info(info.dir)
    assert again.status == "extracting" and "glossary_version" not in again.extra


def test_deleting_last_file_returns_project_to_extracting(tmp_path):
    info, fd = _project(tmp_path, "del", pages=1)
    workspace.set_status(info, "glossary_confirmed", glossary_version=1)
    service.delete_file(fd)
    assert workspace.load_project_info(info.dir).status == "extracting"


def test_deleting_one_of_two_files_keeps_status(tmp_path):
    info, fd = _project(tmp_path, "keep", pages=1)
    pdf = next((fd / "source").glob("*.pdf"))
    service.add_files(info.dir, [pdf], "둘째", new_group=True)
    workspace.set_status(info, "translated")
    service.delete_file(workspace.file_dirs(info)[-1])
    assert workspace.load_project_info(info.dir).status == "translated"


def test_deleting_game_resets_its_remaining_projects(tmp_path):
    """게임(용어집·아이콘 사전)만 지우고 프로젝트를 남기면 그 프로젝트는 ‘추출 중’으로."""
    info, _fd = _project(tmp_path, "game", pages=1)
    workspace.set_status(info, "translated", glossary_version=2)
    service.delete_game(info.game, root=tmp_path)
    assert workspace.load_project_info(info.dir).status == "extracting"


# ---------------------------------------------------------------- B. 아이콘 정의 변경 → 원문 확인
def _confirm_all(info, name="lily"):
    rev = service.icon_review(info.dir)
    rows = [{"group": g["group"], "name": "" if g["not_icon"] else name, "meaning": "", "ko": "백합",
             "not_icon": g["not_icon"], "text": "", "scale": 1.1} for g in rev["groups"]]
    return service.confirm_icons(info.dir, rows)


def test_confirm_marks_cells_with_changed_icon_spots(tmp_path):
    """손으로 다시 잡은 자리가 있는 칸은 ‘확인 필요’가 되고 다시 읽을 칸으로 돌려준다. 그대로인 칸은 그대로."""
    info, fd = _project(tmp_path, "chg", pages=2)
    service.find_icons(info.dir, suggest=False)
    scan = icons.load_scan(info.game_dir / "work" / "icons")
    o1 = next(o for o in scan.occurrences if o.page == 1)
    service.set_icon_box(info.dir, o1.id, o1.box)  # 1쪽: 사람이 자리를 다시 잡음
    r = _confirm_all(info)
    assert r.state == "confirmed", r.blocking
    assert [(c["page"], c["region"]) for c in r.changed_cells] == [(1, "본문")]
    cells = {(c["page"], c["region"]): c for c in service.extracted_cells(fd)}
    assert cells[(1, "본문")]["uncertain"] and cells[(1, "본문")]["icon_changed"] == ["손으로 자리를 다시 잡음"]
    assert not cells[(2, "본문")]["uncertain"] and not cells[(2, "본문")]["icon_changed"]
    # 원문 확인에서 사람이 보고 저장하면 풀린다
    service.save_source(fd, 1, "본문", cells[(1, "본문")]["source"])
    c1 = next(c for c in service.extracted_cells(fd) if (c["page"], c["region"]) == (1, "본문"))
    assert not c1["uncertain"] and not c1["icon_changed"]


def test_confirm_marks_not_icon_and_absorbed_spots(tmp_path):
    """‘아이콘 아님’으로 정한 자리(그 자리에 글자가 있었을 수 있음)와 세트로 합친 자리의 칸도 다시 읽을 칸."""
    info, fd = _project(tmp_path, "nic", pages=1)
    host, extra = _add_run_partner(info, fd)
    service.absorb_icon_run(info.dir, host, [extra])
    r = _confirm_all(info)
    assert r.state == "confirmed", r.blocking
    assert r.changed_cells and r.changed_cells[0]["page"] == 1
    c = next(c for c in service.extracted_cells(fd) if c["page"] == 1)
    assert "세트로 합침" in c["icon_changed"]


def test_reread_cells_runs_each_and_stops_on_pause(tmp_path, monkeypatch):
    calls = []

    def fake(fd, page, region, **kw):
        calls.append((page, region))
        st = "paused" if page == 2 else "done"
        return service.RereadResult(st, source="x", reason="한도" if st == "paused" else "")

    monkeypatch.setattr(service, "reread_region", fake)
    cells = [{"file_dir": tmp_path, "page": p, "region": "본문"} for p in (1, 2, 3)]
    r = service.reread_cells(cells, engine="api")
    assert calls == [(1, "본문"), (2, "본문")] and r["done"] == 1 and r["state"] == "paused"


# ---------------------------------------------------------------- E. 카드 보기: 아이콘을 빠뜨리지 않고 보여 준다
def test_place_info_shows_unfound_neighbours_as_guesses(tmp_path):
    """위치를 못 찾은 다른 자리도 예상 자리에 보여 준다 (guess=True)."""
    info, fd = _project(tmp_path, "pl", pages=1)
    host, extra = _add_run_partner(info, fd)
    p = service.icon_place(info.dir, host)
    guesses = [m for m in p["marks"] if m.get("guess")]
    assert len(guesses) == 1 and guesses[0]["occ"] == extra and len(guesses[0]["box"]) == 4


def test_match_known_finds_dictionary_icon(tmp_path):
    """줄 배치로 못 찾은 이름 있는 아이콘은 사전 그림으로 칸 안을 찾는다."""
    info, fd = _project(tmp_path, "mk", pages=1)
    service.find_icons(info.dir, suggest=False)
    work = info.game_dir / "work" / "icons"
    scan = icons.load_scan(work)
    o = scan.occurrences[0]
    _fd, img, text = icons._occ_region(info, o)
    assert _confirm_all(info).state == "confirmed"
    box = icons.match_known(info, img, "lily", "AB[[lily]]CD")
    assert box is not None
    x0, y0, x1, y1 = box
    ox0, oy0, ox1, oy1 = o.box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    assert ox0 - 4 <= cx <= ox1 + 4 and oy0 - 4 <= cy <= oy1 + 4  # 처음 찾은 아이콘 자리와 같은 곳
    assert icons.match_known(info, img, "no_such_icon", text) is None
