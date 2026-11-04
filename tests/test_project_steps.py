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
