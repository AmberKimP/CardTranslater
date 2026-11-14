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


def _assess_reply(flags=None):
    """번역 확인 판단 응답. flags: {용어: [다른 후보...]} (그 용어만 확인 필요)."""
    items = [{"term": k, "needs_check": True, "alternatives": v, "reason": "갈림"} for k, v in (flags or {}).items()]
    return (0, json.dumps({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.01,
                           "modelUsage": {}, "structured_output": {"items": items}}), "")


def test_new_project_creates_or_reuses_game(tmp_path):
    a, new_a = workspace.create_project("1차", "Dungeon", root=tmp_path)
    b, new_b = workspace.create_project("2차", "Dungeon", root=tmp_path)
    assert new_a and not new_b and a.game == b.game == "Dungeon"
    games = workspace.list_games(tmp_path)
    assert [g["name"] for g in games] == ["Dungeon"] and games[0]["projects"] == ["1차", "2차"]
    with pytest.raises(FileExistsError):  # 같은 게임 안에서는 같은 이름 안 됨
        workspace.create_project("1차", "Dungeon", root=tmp_path)
    c, _ = workspace.create_project("1차", "Other", root=tmp_path)  # 다른 게임이면 된다
    assert c.name == "1차" and c.dir != a.dir


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
    assert r.state == "needs_confirmation" and any(i.startswith("일부만 읽음:") and "장 중 1장" in i for i in r.issues)

    _extracted(fd, 2, "[炎]と[毒]の判定")
    run = FakeRunner([_terms_reply([
        {"term": "判定", "category": "키워드·능력", "suggested_ko": "판정", "reason": ""},
        {"term": "炎", "category": "분류어·태그", "suggested_ko": "불꽃", "reason": ""},
        {"term": "存在しない語", "category": "기타", "suggested_ko": "x", "reason": ""},  # 원문에 없음 -> 버림
    ]), _assess_reply({"炎": ["화염"]})])
    r = service.finish_extraction(info.dir, client=ClaudeCodeClient(exe="claude", runner=run))
    assert r.state == "review_ready" and r.review_xlsx.exists()
    assert "[炎]と[毒]の判定" in run.inputs[0]  # 모든 원문을 모아 보냄
    assert workspace.load_project_info(info.dir).status == "glossary_review"

    from openpyxl import load_workbook

    wb = load_workbook(r.review_xlsx)
    ws = wb["용어집"]
    rows = {ws.cell(i, 1).value: [c.value for c in ws[i]] for i in range(2, ws.max_row + 1) if ws.cell(i, 1).value}
    assert set(rows) == {"炎", "毒", "判定"}  # 코드 후보(대괄호) + 모델 후보, 지어낸 말은 없음
    h = [c.value for c in ws[1]]
    assert h[3:6] == ["원문 상태", "번역 확인", "다른 후보"]  # 확인할 정보는 추천 번역 바로 뒤
    assert rows["炎"][h.index("등장 횟수")] == 2 and rows["判定"][h.index("추천 번역")] == "판정"
    assert rows["炎"][h.index("번역 확인")] == "확인 필요" and "화염" in rows["炎"][h.index("다른 후보")]
    assert rows["判定"][h.index("번역 확인")] is None
    # 사람이 검증: 毒 번역 입력, 判定은 비워 용어로 쓰지 않음
    for i in range(2, ws.max_row + 1):
        if ws.cell(i, 1).value == "毒":
            ws.cell(i, 2).value = "독"
        if ws.cell(i, 1).value == "判定":
            ws.cell(i, 2).value = None
    wb.save(r.review_xlsx)
    c = service.import_glossary(info.dir, r.review_xlsx)
    assert c.state == "needs_confirmation" and any("判定" in i for i in c.issues)
    assert any("번역 확인" in i and "炎 = 불꽃" in i for i in c.issues)  # 확인 필요 용어를 추천 그대로
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
    run2 = FakeRunner([_terms_reply([]), _assess_reply()])
    r2 = service.finish_extraction(info2.dir, client=ClaudeCodeClient(exe="claude", runner=run2))
    assert r2.confirmed_terms == 2 and r2.new_terms == 1  # 闇만 새 후보
    # 이미 확정된 용어는 모델이 '번역 확인'이라 해도 확인 필요가 아니다 (2026-11-07 사용자 요청)
    run3 = FakeRunner([_terms_reply([]), _assess_reply({"毒": ["독성"]})])
    r3 = service.finish_extraction(info2.dir, accept_issues=True, client=ClaudeCodeClient(exe="claude", runner=run3))
    c3 = service.import_glossary(info2.dir, r3.review_xlsx)
    assert not any("毒 = 독" in i for i in c3.issues)
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


def test_icons_never_enter_the_glossary(tmp_path):
    """아이콘 자리표시([[coin]]·[[?]])는 용어 후보·확정·불러오기 어디서도 용어가 아니다."""
    from app import glossary, workspace

    assert glossary.is_icon_term("[[coin]]") and glossary.is_icon_term("[[?]]を払う")
    assert not glossary.is_icon_term("召喚石") and not glossary.is_icon_term("[炎]")
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    glossary.confirm(info, [{"source": "[[coin]]", "ko": "[[coin]]", "category": "자원·토큰"},
                            {"source": "召喚石", "ko": "소환석", "category": "자원·토큰"}])
    assert [t["source"] for t in glossary.load_glossary(info)["terms"]] == ["召喚石"]


# ---------------------------------------------------------------- 검증 엑셀 정렬·확정 용어·빈 열
def test_review_excel_sorted_by_check_then_source_and_confirmed_not_checked(tmp_path):
    """1차 번역 확인(확인 필요가 위), 2차 원문 용어. 이미 확정된 용어는 확인 필요가 아니고 추천 번역에 확정 번역,
    원문 상태가 빈 칸 대신 ‘확실’. 다시 읽으면 ‘확실’은 빈 판정으로 돌아온다."""
    from openpyxl import load_workbook

    info, _ = workspace.create_project("p", "G", root=tmp_path)
    terms = [glossary.Term("毒", suggested="독"),
             glossary.Term("炎", ko="불꽃", confirmed=True, check=True, alternatives=["화염"]),
             glossary.Term("闇", suggested="어둠", check=True, alternatives=["암흑"]),
             glossary.Term("光", suggested="빛", check=True, alternatives=["광"]),
             glossary.Term("判定", suggested="판정", verdict="확인 필요", verdict_keys=["1쪽/본문"])]
    out = glossary.export_review(info, terms, tmp_path / "r.xlsx")
    ws = load_workbook(out)["용어집"]
    h = [c.value for c in ws[1]]
    rows = [[c.value for c in r] for r in ws.iter_rows(min_row=2) if r[0].value]
    assert [r[0] for r in rows] == sorted(["光", "闇"]) + sorted(["判定", "毒", "炎"])
    by = {r[0]: r for r in rows}
    assert by["炎"][h.index("번역 확인")] is None and by["炎"][h.index("추천 번역")] == "불꽃"
    assert by["毒"][h.index("원문 상태")] == "확실" and by["判定"][h.index("원문 상태")] == "확인 필요"
    back = {t.source: t for t in glossary.terms_from_review(out)}
    assert back["毒"].verdict == "" and back["判定"].verdict == "확인 필요"


def test_assess_skips_confirmed_and_code_candidates_are_asked(tmp_path):
    """확정 용어는 ‘번역 확인’ 판단에 보내지 않고, 코드로 뽑은 후보([炎])는 추천 번역을 받도록 모델에 알린다."""
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    glossary.confirm(info, [{"source": "毒", "ko": "독", "category": "기타", "variants": [], "note": "", "count": 1}])
    src = [glossary.Source(key="1쪽/본문", region="본문", kind="text", text="[炎]と[毒]の判定",
                           card_name="", verdict="")]
    run = FakeRunner([_terms_reply([{"term": "炎", "category": "분류어·태그", "suggested_ko": "불꽃", "reason": ""}]),
                      _assess_reply({"炎": ["화염"]})])
    terms, *_ = glossary.find_candidates(info, src, ClaudeCodeClient(exe="claude", runner=run), "m", tmp_path / "w")
    assert "MUST also list" in run.inputs[0] and "炎" in run.inputs[0].split("MUST also list")[1].split("\n")[0]
    assert "\"term\": \"毒\"" not in run.inputs[1] and "\"term\": \"炎\"" in run.inputs[1]
    by = {t.source: t for t in terms}
    assert by["炎"].suggested == "불꽃" and by["炎"].check and not by["毒"].check


def _review_ready(tmp_path):
    pdf, regions = _pdf_and_regions(tmp_path)
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    workspace.add_files(info, [pdf], "카드", new_group=True, regions_from=regions)
    fd = workspace.file_dirs(info)[0]
    _extracted(fd, 1, "[炎]を持つすべての判定にボーナス")
    _extracted(fd, 2, "[炎]と[毒]の判定")
    run = FakeRunner([_terms_reply([{"term": "判定", "category": "키워드·능력", "suggested_ko": "판정", "reason": ""},
                                    {"term": "炎", "category": "분류어·태그", "suggested_ko": "불꽃", "reason": ""}]),
                      _assess_reply({"炎": ["화염"]})])
    r = service.finish_extraction(info.dir, client=ClaudeCodeClient(exe="claude", runner=run))
    assert r.state == "review_ready"
    return info


def test_glossary_rows_confirm_in_app_without_excel(tmp_path):
    """⑤ 용어집 확정 (화면 표): 엑셀 없이 같은 검사로 확정하고, 고친 값은 검증 엑셀에도 남는다."""
    info = _review_ready(tmp_path)
    rows = service.glossary_review_rows(info.dir)
    by = {r["원문 용어"]: r for r in rows}
    assert set(by) == {"炎", "毒", "判定"} and by["炎"]["번역 확인"] == "확인 필요" and by["判定"]["추천 번역"] == "판정"
    by["毒"]["번역"] = "독"
    by["判定"]["번역"] = ""
    c = service.confirm_glossary_rows(info.dir, rows)
    assert c.state == "needs_confirmation" and any("判定" in i for i in c.issues)
    # 확정하지 않아도 고친 값은 엑셀에 남아 다시 열면 그대로
    again = {r["원문 용어"]: r for r in service.glossary_review_rows(info.dir)}
    assert again["毒"]["번역"] == "독" and again["判定"]["번역"] == ""
    c = service.confirm_glossary_rows(info.dir, rows, accept_issues=True)
    assert c.state == "confirmed" and c.terms == 2
    saved = json.loads((tmp_path / "games" / "G" / "glossary.json").read_text(encoding="utf-8"))
    assert {t["source"]: t["ko"] for t in saved["terms"]} == {"炎": "불꽃", "毒": "독"}
    # 같은 원문이 두 줄이면 막는다 (줄 번호는 화면 기준)
    rows.append(dict(rows[0]))
    c = service.confirm_glossary_rows(info.dir, rows)
    assert c.state == "blocked" and "두 번" in c.blocking[0]


def test_glossary_review_dialog_edit_sort_add_and_confirm(tmp_path, monkeypatch):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from app.qt import steps
    from app.qt.glossary_review import C, GlossaryReviewDialog

    QApplication.instance() or QApplication([])
    info = _review_ready(tmp_path)
    dlg = GlossaryReviewDialog(info.dir, service.glossary_review_rows(info.dir))
    assert dlg.table.rowCount() == 3
    assert dlg.table.item(0, C["원문 용어"]).text() == "炎"  # 번역 확인(보라)이 위
    from PySide6.QtCore import Qt

    dlg.table.sortItems(C["원문 용어"])  # 머리글 정렬 (다시 누르면 반대)
    asc = [dlg.table.item(i, C["원문 용어"]).text() for i in range(3)]
    dlg.table.sortItems(C["원문 용어"], Qt.SortOrder.DescendingOrder)
    assert [dlg.table.item(i, C["원문 용어"]).text() for i in range(3)] == asc[::-1]
    dlg.table.sortItems(C["등장 횟수"], Qt.SortOrder.DescendingOrder)  # 숫자로 정렬
    counts = [int(dlg.table.item(i, C["등장 횟수"]).text()) for i in range(3)]
    assert counts == sorted(counts, reverse=True)
    for i in range(3):
        if dlg.table.item(i, C["원문 용어"]).text() == "毒":
            dlg.table.item(i, C["번역"]).setText("독")
    dlg.add_row()
    new = next(i for i in range(dlg.table.rowCount()) if not dlg.table.item(i, C["원문 용어"]).text())
    dlg.table.item(new, C["원문 용어"]).setText("闇")
    dlg.table.item(new, C["번역"]).setText("어둠")
    dlg.search.setText("독")
    assert sum(not dlg.table.isRowHidden(i) for i in range(dlg.table.rowCount())) == 1
    dlg.search.clear()
    monkeypatch.setattr("app.qt.step_common._ask_list", lambda *a, **k: True)
    dlg.confirm()
    assert dlg.result is not None and dlg.result.state == "confirmed"
    saved = json.loads((tmp_path / "games" / "G" / "glossary.json").read_text(encoding="utf-8"))
    assert {t["source"]: t["ko"] for t in saved["terms"]} == {"炎": "불꽃", "毒": "독", "判定": "판정", "闇": "어둠"}


def test_glossary_review_dialog_remove_sorted_and_close_rules(tmp_path, monkeypatch):
    """정렬한 채 여러 줄을 빼도 고른 용어만 비우고, 닫을 때는 고친 것이 있을 때만·엑셀 가져오기 전일 때만 엑셀에 쓴다."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QItemSelectionModel, Qt
    from PySide6.QtWidgets import QApplication

    from app.qt import glossary_review
    from app.qt.glossary_review import C, GlossaryReviewDialog

    QApplication.instance() or QApplication([])
    info = _review_ready(tmp_path)
    saves = []
    real_save = service.save_glossary_rows
    monkeypatch.setattr(service, "save_glossary_rows", lambda d, rows: saves.append(rows) or real_save(d, rows))

    # 고친 것 없이 닫으면 엑셀을 건드리지 않는다
    dlg = GlossaryReviewDialog(info.dir, service.glossary_review_rows(info.dir))
    dlg.reject()
    assert saves == []

    dlg = GlossaryReviewDialog(info.dir, service.glossary_review_rows(info.dir))
    for i in range(3):
        dlg.table.item(i, C["번역"]).setText(f"번역{i}")
    dlg.table.sortItems(C["번역"], Qt.SortOrder.DescendingOrder)
    picked = {dlg.table.item(i, C["원문 용어"]).text() for i in (0, 1)}
    sm = dlg.table.selectionModel()
    sm.clearSelection()
    for i in (0, 1):
        sm.select(dlg.table.model().index(i, 0), QItemSelectionModel.SelectionFlag.Select
                  | QItemSelectionModel.SelectionFlag.Rows)
    dlg.remove_rows()
    emptied = {r["원문 용어"] for r in dlg.rows() if not r["번역"]}
    assert emptied == picked

    # 엑셀에서 가져오기를 해 본 뒤 닫으면 (막혔어도) 표로 엑셀을 덮어쓰지 않는다
    monkeypatch.setattr(glossary_review.QFileDialog, "getOpenFileName", lambda *a, **k: (str(tmp_path / "x.xlsx"), ""))
    monkeypatch.setattr(dlg, "done_with", lambda run, fix_where="표": None)
    dlg.import_excel()
    dlg.reject()
    assert saves == []
