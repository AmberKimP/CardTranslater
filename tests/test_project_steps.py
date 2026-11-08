"""뺄 쪽 지정, 문구(긴 구절) 처리, 프로젝트 단위 지우기·한글 넣기."""

from __future__ import annotations

import json

from app import glossary, service, workspace
from app.extract_cc import ClaudeCodeClient
from app.translate import check
from tests.test_extract import FakeRunner
from tests.test_glossary import _pdf_and_regions
from tests.test_translate import _reply, _write_extract


def _project(tmp_path, pages=2):
    pdf, regions = _pdf_and_regions(tmp_path)
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    workspace.add_files(info, [pdf], "카드", new_group=True, regions_from=regions)
    return info, workspace.file_dirs(info)[0]


def test_excluded_pages_are_skipped_everywhere(tmp_path):
    info, fd = _project(tmp_path)
    assert workspace.included_pages(fd) == [1, 2]
    workspace.set_excluded_pages(fd, [1], "규칙 카드")
    assert workspace.included_pages(fd) == [2] and workspace.pages_spec([2]) == "2"
    # 추출: 2쪽만 읽는다
    run = FakeRunner([(0, json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                      "total_cost_usd": 0, "modelUsage": {}, "structured_output": {"regions": [
                                          {"name": "이름", "text": "HELLO", "uncertain": False, "note": ""}]}}), "")])
    r = service.extract(fd, client=ClaudeCodeClient(exe="claude", runner=run))
    assert r.done_pages == [2] and len(run.calls) == 1
    # 이미 읽힌 1쪽이 있어도 용어집 원문·추출 상태에서 빠진다
    _write_extract(fd, 1, "規則カードの文")
    keys = [s.key for s in glossary.collect_sources(info)]
    assert keys == ["카드/cards#p2#이름"]
    st = glossary.extraction_status(info)[0]
    assert st["pages"] == 1 and st["extracted"] == 1 and st["excluded"] == [1]


def test_phrase_terms_are_hints_not_checked():
    assert glossary.is_phrase("すべての判定にボーナスがもらえる", "키워드·능력")
    assert glossary.is_phrase("ダイスを振り", "행동")
    assert not glossary.is_phrase("スティクスの霊薬", "카드 이름") and not glossary.is_phrase("階層", "기타")
    gl = [{"source": "ダイスを振り", "ko": "주사위를 굴려", "category": "행동"},
          {"source": "判定", "ko": "판정", "category": "행동"}]
    assert check("もう一度ダイスを振り、判定", "주사위를 한 번 더 굴려, 판정", gl) == ([], [])
    assert check("判定", "심사", gl)[0] == ["용어 불일치"]  # 짧은 용어는 그대로 검사


def test_translate_prompt_separates_terms_and_phrases(tmp_path):
    info, fd = _project(tmp_path)
    _write_extract(fd, 1, "もう一度ダイスを振り、判定する")
    _write_extract(fd, 2, "判定")
    glossary.confirm(info, [{"source": "ダイスを振り", "ko": "주사위를 굴려", "category": "문구"},
                            {"source": "判定", "ko": "판정", "category": "행동"}])
    workspace.set_status(info, "glossary_confirmed")
    run = FakeRunner([_reply(["주사위를 한 번 더 굴려 판정한다"])])
    r = service.translate_project(info.dir, client=ClaudeCodeClient(exe="claude", runner=run))
    assert r.state == "done" and not r.term_mismatch and r.from_glossary == 1
    p = run.inputs[0]
    assert 'Glossary (must use): {"判定": "판정"}' in p and "Preferred phrasing" in p and "ダイスを振り" in p


def test_erase_and_compose_project_with_draft_and_excel(tmp_path):
    from tests.test_service import GAEGU

    if not GAEGU.exists():
        import pytest

        pytest.skip("fonts/ 없음")
    info, fd = _project(tmp_path)
    workspace.set_excluded_pages(fd, [2])
    _write_extract(fd, 1, "HELLO")
    glossary.confirm(info, [{"source": "HELLO", "ko": "안녕", "category": "기타"}])
    workspace.set_status(info, "glossary_confirmed")
    service.translate_project(info.dir, client=ClaudeCodeClient(exe="claude", runner=FakeRunner([])))

    r = service.compose_project(info.dir)
    assert r.state == "blocked" and "지운 결과" in r.reason  # 지우기 전에는 막힘
    r = service.erase_project(info.dir)
    assert r.state == "done" and r.files[0]["pages"] == 1  # 뺀 2쪽은 지우지 않음
    assert service.erase_project(info.dir).files[0]["state"] == "skipped"  # 이미 지움

    r = service.compose_project(info.dir)  # 검수 엑셀 없음 -> 초벌 번역
    assert r.state == "done" and r.files[0]["from"] == "초벌 번역"
    import pymupdf

    with pymupdf.open(fd / "translated" / "translated.pdf") as d:
        assert d.page_count == 1 and "안녕" in d[0].get_text()

    x = service.export_excel(fd)  # 검수 엑셀이 생기면 그것을 쓴다
    r = service.compose_project(info.dir, accept_issues=True)
    assert r.files[0]["from"] == "번역_검수.xlsx" and r.state == "done", r
    assert x.exists()


def test_card_limit_test_mode_limits_every_step(tmp_path):
    """시험 모드(앞 X장만): 추출·용어 원문·번역·검수 엑셀·지우기가 그 카드만. 끄면 남은 카드부터."""
    from openpyxl import load_workbook

    info, fd = _project(tmp_path)
    ok = (0, json.dumps({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0,
                         "modelUsage": {}, "structured_output": {"regions": [
                             {"name": "이름", "text": "HELLO", "uncertain": False, "note": ""}]}}), "")
    assert service.set_card_limit(info.dir, 1) == 1
    assert workspace.included_pages(fd) == [1] and workspace.included_pages(fd, limited=False) == [1, 2]
    ov = service.project_overview(info.dir)
    assert ov["card_limit"] == 1 and ov["groups"][0]["files"][0]["excluded"] == []  # 시험 밖 카드는 '뺀 쪽'이 아님
    run = FakeRunner([ok])
    r = service.extract_project(info.dir, client=ClaudeCodeClient(exe="claude", runner=run))
    assert r.state == "done" and r.files[0]["done"] == 1 and len(run.calls) == 1
    # 2쪽이 이미 읽혀 있어도 원문·번역·검수에서 빠진다
    _write_extract(fd, 2, "OTHER")
    assert [s.key for s in glossary.collect_sources(info)] == ["카드/cards#p1#이름"]
    st = glossary.extraction_status(info)[0]
    assert st["pages"] == 1 and st["extracted"] == 1
    x = service.export_excel(fd)
    keys = [row[0].value for row in load_workbook(x).active.iter_rows(min_row=2) if row[0].value]
    assert keys and all("#p1#" in k for k in keys), keys
    # 끄면 남은 카드부터 이어 읽는다
    assert service.set_card_limit(info.dir, 0) is None
    run2 = FakeRunner([ok])
    r = service.extract_project(info.dir, client=ClaudeCodeClient(exe="claude", runner=run2))
    assert r.state == "done" and len(run2.calls) == 0  # 2쪽은 위에서 이미 읽힘 -> 다시 읽지 않음
    assert len(glossary.collect_sources(info)) == 2


def test_card_limit_erase_redoes_when_limit_grows(tmp_path):
    info, fd = _project(tmp_path)
    service.set_card_limit(info.dir, 1)
    r = service.erase_project(info.dir)
    assert r.state == "done" and r.files[0]["pages"] == 1
    assert service.erase_project(info.dir).files[0]["state"] == "skipped"
    service.set_card_limit(info.dir, None)  # 장수를 늘리면 일부만 지운 결과를 다시 만든다
    r = service.erase_project(info.dir)
    assert r.files[0]["state"] == "done" and r.files[0]["pages"] == 2


def test_move_pages_of_one_pdf_to_another_group(tmp_path):
    """한 PDF에 형식이 다른 카드가 섞여 있으면 쪽 일부를 다른 그룹에서 처리한다."""
    import pytest

    info, fd = _project(tmp_path)
    with pytest.raises(ValueError):
        service.move_pages_to_group(fd, [9], "정예", new_group=True)  # 없는 쪽
    t = service.move_pages_to_group(fd, [2], "정예", new_group=True)
    assert workspace.included_pages(fd) == [1] and workspace.included_pages(t) == [2]
    assert t.parent.parent.name == "정예" and (t / "source" / "cards.pdf").exists()
    assert workspace.file_settings(fd)["pages_in_groups"] == {"정예": [2]}
    assert workspace.file_settings(t)["pages_in_groups"] == {"카드": [1]}
    # 다시 보내면 같은 파일 폴더를 쓴다 (새로 넣지 않음)
    service.set_pages(t, [1, 2])
    assert service.move_pages_to_group(fd, [2], "정예") == t
    assert workspace.included_pages(t) == [2]
    with pytest.raises(ValueError):
        service.move_pages_to_group(fd, [1], "카드")  # 같은 그룹
    keys = [workspace.file_key(d) for d in workspace.file_dirs(info)]
    assert sorted(keys) == ["정예/cards", "카드/cards"]


def test_game_review_excel_one_file_sheet_per_group(tmp_path):
    """검수 엑셀은 게임에 하나, 그룹마다 시트. 같은 그룹 이름이 여러 프로젝트에 있으면 ‘프로젝트·그룹’."""
    from openpyxl import load_workbook

    from app.excel_io import game_files, import_translation
    from app.project import load_project
    from tests.test_glossary import _pdf_and_regions

    info, fd = _project(tmp_path)
    t = service.move_pages_to_group(fd, [2], "정예", new_group=True)
    _pdf, regions = _pdf_and_regions(tmp_path / "x")
    import shutil

    shutil.copyfile(regions, t.parent.parent / "regions.json")
    info2, _ = workspace.create_project("q", "G", root=tmp_path)
    workspace.add_files(info2, [_pdf], "카드", new_group=True, regions_from=regions)
    for d in workspace.game_file_dirs(info):
        for p in workspace.included_pages(d):
            _write_extract(d, p, f"T{p}")
    [x] = service.export_project_excel(info.dir)
    assert x == info.game_dir / "번역_검수.xlsx"
    files = game_files(x)
    assert sorted((f["시트"], f["프로젝트"], f["그룹"], f["쪽"]) for f in files) == [
        ("p·카드", "p", "카드", "1"), ("q·카드", "q", "카드", "1,2"), ("정예", "p", "정예", "2")]
    wb = load_workbook(x)
    ws = wb["정예"]
    head = [c.value for c in ws[1]]
    ws.cell(2, head.index("번역") + 1).value = "고친 번역"
    wb.save(x)
    assert service.review_xlsx_for(t) == x and service.review_xlsx_for(fd) == x
    res = import_translation(load_project(t), x)
    assert not res.blocking and list(res.cards) == [2] and res.cards[2]["이름"]["ko"] == "고친 번역"
    res = import_translation(load_project(fd), x)
    assert not res.blocking and list(res.cards) == [1]  # 이 파일의 쪽만
    import pytest

    with pytest.raises(FileExistsError):
        service.export_project_excel(info2.dir)  # 같은 게임: 같은 엑셀
    service.export_project_excel(info2.dir, force=True)  # 다시 만들어도 사람이 고친 번역은 남는다
    ws = load_workbook(x)["정예"]
    assert ws.cell(2, head.index("번역") + 1).value == "고친 번역"


def test_glossary_review_is_game_wide(tmp_path):
    """‘추출 완료’는 같은 게임의 모든 프로젝트 원문을 모아 게임 폴더에 용어집 검증 엑셀을 만든다."""
    from app.extract_cc import ClaudeCodeClient
    from tests.test_glossary import _pdf_and_regions

    info, fd = _project(tmp_path)
    _write_extract(fd, 1, "炎の判定")
    _pdf, regions = _pdf_and_regions(tmp_path / "x")
    info2, _ = workspace.create_project("q", "G", root=tmp_path)
    workspace.add_files(info2, [_pdf], "카드", new_group=True, regions_from=regions)
    fd2 = workspace.file_dirs(info2)[0]
    _write_extract(fd2, 1, "生命の雫")
    keys = sorted(s.key for s in glossary.collect_sources(info, game=True))
    assert keys == ["p/카드/cards#p1#이름", "q/카드/cards#p1#이름"]
    assert [s.key for s in glossary.collect_sources(info)] == ["카드/cards#p1#이름"]  # 번역은 프로젝트 단위
    reply = json.dumps({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0,
                        "modelUsage": {}, "structured_output": {"terms": []}})
    assess = json.dumps({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0,
                         "modelUsage": {}, "structured_output": {"terms": []}})
    r = service.finish_extraction(info.dir, accept_issues=True,
                                  client=ClaudeCodeClient(exe="claude", runner=FakeRunner([(0, reply, ""), (0, assess, "")])))
    assert r.state == "review_ready" and r.review_xlsx == info.game_dir / "용어집_검증.xlsx"
    assert {p.status for p in workspace.game_projects(info)} == {"glossary_review"}


def test_font_preview_draws_like_compose(tmp_path):
    """글꼴 창 미리 보기는 한글 넣기와 똑같이 그린다 (예전: 화면이 줄바꿈·크기 맞춤 없이 따로 그려 결과와 많이 달랐다)."""
    import numpy as np
    import pymupdf

    from tests.test_service import GAEGU

    if not GAEGU.exists():
        import pytest

        pytest.skip("fonts/ 없음")
    info, fd = _project(tmp_path)
    workspace.set_excluded_pages(fd, [2])
    _write_extract(fd, 1, "HELLO")
    glossary.confirm(info, [{"source": "HELLO", "ko": "안녕", "category": "기타"}])
    workspace.set_status(info, "glossary_confirmed")
    service.translate_project(info.dir, client=ClaudeCodeClient(exe="claude", runner=FakeRunner([])))
    assert service.erase_project(info.dir).state == "done"
    assert service.compose_project(info.dir).state == "done"
    p = service.font_preview(fd, 1, "이름", None)
    assert p is not None and p["composed_png"]
    prev = pymupdf.Pixmap(p["composed_png"])
    meta = json.loads((fd / "cleared" / "meta" / "page-001.json").read_text(encoding="utf-8"))
    rect = pymupdf.Rect(next(r for r in meta["regions"] if r["name"] == "이름")["rect_pt"])
    with pymupdf.open(fd / "translated" / "translated.pdf") as d:
        real = d[0].get_pixmap(matrix=pymupdf.Matrix(p["zoom"], p["zoom"]), clip=rect, alpha=False)
    a = np.frombuffer(prev.samples, np.uint8).reshape(prev.h, prev.w, prev.n)[..., :3].astype(int)
    b = np.frombuffer(real.samples, np.uint8).reshape(real.h, real.w, real.n)[..., :3].astype(int)
    assert a.shape == b.shape and (np.abs(a - b).max(axis=2) > 40).mean() < 0.01  # 결과와 같은 그림
    # 번역이 없는 칸은 그리지 않는다 (화면이 예전처럼 간단히 그린다)
    from app import font_plan

    assert font_plan.preview_render(fd, 1, "없는 영역", None) is None
