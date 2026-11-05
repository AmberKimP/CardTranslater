"""게임 단위 자료(번역 검수 엑셀·아이콘 검증)가 같은 게임의 여러 프로젝트에 걸쳐 동작하는지."""

from __future__ import annotations

import pytest
from openpyxl import load_workbook

from app import icons, service, workspace
from app.excel_io import import_translation
from app.extract_cc import ClaudeCodeClient
from app.project import load_project
from tests.test_extract import FakeRunner
from tests.test_icons import _project as icon_project
from tests.test_icons import _save, _source, _suggest
from tests.test_project_steps import _project as step_project
from tests.test_translate import _write_extract


def test_review_xlsx_for_picks_legacy_per_file_excel_when_no_game_excel(tmp_path):
    """게임 엑셀이 없고 예전 파일별 번역_검수.xlsx만 있으면 그것을 고르고, 둘 다 없으면 None."""
    info, fd = step_project(tmp_path)
    assert service.review_xlsx_for(fd) is None
    legacy = fd / "번역_검수.xlsx"
    legacy.write_bytes(b"x")
    assert service.review_xlsx_for(fd) == legacy


def test_review_xlsx_for_ignores_game_excel_without_this_file(tmp_path):
    """게임 엑셀이 있어도 이 파일이 거기 없으면(그 뒤에 넣은 파일) 예전 엑셀로, 없으면 None으로 돌아간다."""
    info, fd = step_project(tmp_path)
    for p in workspace.included_pages(fd):
        _write_extract(fd, p, f"T{p}")
    [x] = service.export_project_excel(info.dir)
    assert service.review_xlsx_for(fd) == x
    from tests.test_glossary import _pdf_and_regions
    src, _r = _pdf_and_regions(tmp_path / "again")
    pdf2 = src.with_name("other.pdf")
    src.rename(pdf2)
    workspace.add_files(info, [pdf2], "카드")
    fd2 = next(d for d in workspace.file_dirs(info) if d != fd)
    assert service.review_xlsx_for(fd2) is None
    res = import_translation(load_project(fd2), x)
    assert res.blocking and "이 파일이 없습니다" in res.blocking[0]


def test_icons_find_and_import_cover_every_project_of_the_game(tmp_path):
    """같은 게임의 두 프로젝트에 [[?]]가 있으면 한 번 찾고 가져와도 둘 다 바뀐다."""
    info, fd = icon_project(tmp_path, "1차")
    info2, fd2 = icon_project(tmp_path, "2차", pages=1)
    assert icons.pending_count(info) == icons.pending_count(info2) == 3
    run = FakeRunner([_suggest([{"group": "g01", "name": "Gold Coin", "meaning": "Gold", "ko": "금화"}])])
    r = service.find_icons(info.dir, client=ClaudeCodeClient(exe="claude", runner=run))
    assert r.state == "icons_review" and r.groups == 1 and r.occurrences == 3
    ws = load_workbook(r.review_xlsx)["아이콘"]
    head = [c.value for c in ws[1]]
    ws.cell(2, head.index("이름") + 1).value = "coin"
    _save(ws, r.review_xlsx)
    ok = service.import_icons(info.dir, r.review_xlsx)
    assert ok.state == "confirmed" and ok.replaced == 3
    assert _source(fd, 1) == _source(fd, 2) == _source(fd2, 1) == "AB[[coin]]CD"
    assert icons.pending_count(info) == 0


def test_move_pages_splits_pages_between_groups(tmp_path):
    """다른 그룹으로 보낸 쪽은 두 그룹에서 겹치지 않고, 빈 쪽 목록은 거부한다."""
    info, fd = step_project(tmp_path)
    t = service.move_pages_to_group(fd, [2], "정예", new_group=True)
    assert set(workspace.included_pages(fd)).isdisjoint(workspace.included_pages(t))
    with pytest.raises(ValueError):
        service.move_pages_to_group(fd, [], "정예")
