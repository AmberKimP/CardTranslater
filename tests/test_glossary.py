"""게임·프로젝트·그룹과 용어집 관리 (가짜 모델 응답, 실제 호출 없음)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import glossary, service, workspace
from app.extract_cc import ClaudeCodeClient
from tests.test_extract import FakeRunner, _cc_reply
from tests.test_service import make_project


def _pdf_and_regions(tmp: Path) -> tuple[Path, Path]:
    p = make_project(tmp / "src", pages=2)  # 카드 PDF 2장 + regions.json (영역 '이름' 하나)
    return p / "source" / "cards.pdf", p / "regions.json"


def _extracted(file_dir: Path, page: int, text: str):
    d = file_dir / "extract" / "pages"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"page-{page:03d}.json").write_text(json.dumps({
        "page": page, "model": "claude-sonnet-5-5",
        "regions": {"이름": {"source": text, "uncertain": False, "note": ""}}}, ensure_ascii=False), encoding="utf-8")


def _terms_reply(terms):
    return (0, json.dumps({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.05,
                           "modelUsage": {"claude-opus-5-5": {}}, "structured_output": {"terms": terms}}), "")


def test_new_project_creates_or_reuses_game(tmp_path):
    a, new_a = workspace.create_project("1차", "Dungeon", root=tmp_path)
    b, new_b = workspace.create_project("2차", "Dungeon", root=tmp_path)
    assert new_a and not new_b and a.game == b.game == "Dungeon"
    games = workspace.list_games(tmp_path)
    assert [g["name"] for g in games] == ["Dungeon"] and games[0]["projects"] == ["1차", "2차"]
    with pytest.raises(FileExistsError):
        workspace.create_project("1차", "Other", root=tmp_path)


def test_add_files_to_new_or_existing_group(tmp_path):
    pdf, regions = _pdf_and_regions(tmp_path)
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    with pytest.raises(KeyError):  # 없는 그룹에 넣으려 하면 목록을 보여 주고 다시 고르게
        workspace.add_files(info, [pdf], "아이템")
    r = workspace.add_files(info, [pdf], "아이템", new_group=True, regions_from=regions)
    assert r["has_regions"] and len(r["added"]) == 1
    with pytest.raises(FileExistsError):
        workspace.add_files(info, [pdf], "아이템", new_group=True)
    pdf2 = tmp_path / "more.pdf"
    pdf2.write_bytes(pdf.read_bytes())
    workspace.add_files(info, [pdf2], "아이템")  # 기존 그룹에 포함
    groups = workspace.list_groups(info)
    assert groups[0]["name"] == "아이템" and groups[0]["files"] == ["cards", "more"] and groups[0]["regions"] == 1
    # 그룹 안 파일 폴더는 그룹의 영역 템플릿으로 그대로 열린다
    from app.project import load_project

    fd = workspace.file_dirs(info)[0]
    assert load_project(fd).template.regions[0].name == "이름"


def test_finish_extraction_builds_review_and_import_confirms_game_glossary(tmp_path):
    pdf, regions = _pdf_and_regions(tmp_path)
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    workspace.add_files(info, [pdf], "카드", new_group=True, regions_from=regions)
    fd = workspace.file_dirs(info)[0]
    _extracted(fd, 1, "[炎]を持つすべての判定にボーナス")
    # 2쪽은 아직 안 읽음 -> 확인 필요
    r = service.finish_extraction(info.dir, client=ClaudeCodeClient(exe="claude", runner=FakeRunner([])))
    assert r.state == "needs_confirmation" and any("1장만 읽음" in i for i in r.issues)

    _extracted(fd, 2, "[炎]と[毒]の判定")
    run = FakeRunner([_terms_reply([
        {"term": "判定", "category": "키워드·능력", "suggested_ko": "판정", "reason": ""},
        {"term": "炎", "category": "분류어·태그", "suggested_ko": "불꽃", "reason": ""},
        {"term": "存在しない語", "category": "기타", "suggested_ko": "x", "reason": ""},  # 원문에 없음 -> 버림
    ])])
    r = service.finish_extraction(info.dir, client=ClaudeCodeClient(exe="claude", runner=run))
    assert r.state == "review_ready" and r.review_xlsx.exists()
    assert "[炎]と[毒]の判定" in run.inputs[0]  # 모든 원문을 모아 보냄
    assert workspace.load_project_info(info.dir).status == "glossary_review"

    from openpyxl import load_workbook

    wb = load_workbook(r.review_xlsx)
    ws = wb["용어집"]
    rows = {ws.cell(i, 1).value: [c.value for c in ws[i]] for i in range(2, ws.max_row + 1) if ws.cell(i, 1).value}
    assert set(rows) == {"炎", "毒", "判定"}  # 코드 후보(대괄호) + 모델 후보, 지어낸 말은 없음
    assert rows["炎"][4] == 2 and rows["判定"][2] == "판정"
    # 사람이 검증: 毒 번역 입력, 判定은 비워 용어로 쓰지 않음
    for i in range(2, ws.max_row + 1):
        if ws.cell(i, 1).value == "毒":
            ws.cell(i, 2).value = "독"
        if ws.cell(i, 1).value == "判定":
            ws.cell(i, 2).value = None
    wb.save(r.review_xlsx)
    c = service.import_glossary(info.dir, r.review_xlsx)
    assert c.state == "needs_confirmation" and any("判定" in i for i in c.issues)
    c = service.import_glossary(info.dir, r.review_xlsx, accept_issues=True)
    assert c.state == "confirmed" and c.version == 1 and c.terms == 2
    saved = json.loads((tmp_path / "games" / "G" / "glossary.json").read_text(encoding="utf-8"))
    assert {t["source"]: t["ko"] for t in saved["terms"]} == {"炎": "불꽃", "毒": "독"}
    assert workspace.load_project_info(info.dir).status == "glossary_confirmed"

    # 같은 게임의 다음 프로젝트: 확정 용어는 번역이 채워진 채로, 모델에는 확정 용어를 알려 새 것만 묻는다
    info2, new_game = workspace.create_project("p2", "G", root=tmp_path)
    assert not new_game
    workspace.add_files(info2, [pdf], "카드", new_group=True, regions_from=regions)
    fd2 = workspace.file_dirs(info2)[0]
    _extracted(fd2, 1, "[炎]と[闇]")
    _extracted(fd2, 2, "[闇]")
    run2 = FakeRunner([_terms_reply([])])
    r2 = service.finish_extraction(info2.dir, client=ClaudeCodeClient(exe="claude", runner=run2))
    assert r2.confirmed_terms == 2 and r2.new_terms == 1  # 闇만 새 후보
    assert "炎" in run2.inputs[0].split("Already confirmed terms")[1].split("\n")[0]


def test_import_blocks_duplicates_and_other_game(tmp_path):
    from openpyxl import Workbook

    info, _ = workspace.create_project("p", "G", root=tmp_path)
    wb = Workbook()
    ws = wb.active
    ws.title = "용어집"
    ws.append(glossary.HEADERS)
    ws.append(["炎", "불꽃"])
    ws.append(["炎", "화염"])
    meta = wb.create_sheet("정보")
    meta.append(["게임", "G"])
    x = tmp_path / "g.xlsx"
    wb.save(x)
    r = service.import_glossary(info.dir, x)
    assert r.state == "blocked" and "두 번" in r.blocking[0]
    meta["B1"] = "Other"
    wb.save(x)
    assert "다른 게임" in service.import_glossary(info.dir, x).blocking[0]
