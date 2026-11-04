"""5단계 초벌 번역과 용어집 '원문 상태' 표시 (가짜 모델 응답, 실제 호출 없음)."""

from __future__ import annotations

import json
from pathlib import Path

from app import glossary, service, workspace
from app.extract_cc import ClaudeCodeClient
from app.translate import check, load_cache, terms_in
from tests.test_extract import FakeRunner
from tests.test_glossary import _assess_reply, _pdf_and_regions

GL = [{"source": "炎", "ko": "불꽃", "variants": []}, {"source": "判定", "ko": "판정", "variants": []},
      {"source": "魔", "ko": "마족", "variants": []}, {"source": "生命の雫", "ko": "생명의 물방울", "variants": []}]


def _reply(items):
    return (0, json.dumps({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.01,
                           "modelUsage": {"claude-sonnet-5-5": {}},
                           "structured_output": {"items": [{"id": str(i), "ko": k} for i, k in enumerate(items)]}}), "")


def _write_extract(fd: Path, page: int, text: str, uncertain=False, readings=None):
    d = fd / "extract" / "pages"
    d.mkdir(parents=True, exist_ok=True)
    reg = {"source": text, "uncertain": uncertain, "note": ""}
    if readings:
        reg["readings"] = readings
    (d / f"page-{page:03d}.json").write_text(json.dumps({"page": page, "model": "claude-sonnet-5-5",
                                                         "regions": {"이름": reg}}, ensure_ascii=False),
                                             encoding="utf-8")


def _project(tmp_path, texts, status="glossary_confirmed", terms=GL):
    pdf, regions = _pdf_and_regions(tmp_path)
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    workspace.add_files(info, [pdf], "카드", new_group=True, regions_from=regions)
    fd = workspace.file_dirs(info)[0]
    for i, t in enumerate(texts, start=1):
        _write_extract(fd, i, t)
    glossary.confirm(info, terms)
    workspace.set_status(info, status)
    return info, fd


def test_single_char_terms_only_match_whole_or_bracketed():
    assert [t["source"] for t in terms_in("悪魔の判定", GL)] == ["判定"]  # 悪魔의 魔는 용어가 아님
    assert "魔" in [t["source"] for t in terms_in("[魔]を持つ", GL)]
    assert check("[炎]の判定", "[불꽃]의 판정", GL) == ([], [])
    st, missing = check("[炎]の判定", "[화염]의 판정", GL)
    assert st == ["용어 불일치"] and missing == ["炎 → 불꽃"]
    assert check("[[coin]]を得る", "얻는다", [])[0] == ["아이콘 불일치"]


def test_translate_blocked_until_glossary_confirmed(tmp_path):
    info, _ = _project(tmp_path, ["[炎]の判定", "生命の雫"], status="glossary_review")
    r = service.translate_project(info.dir, client=ClaudeCodeClient(exe="claude", runner=FakeRunner([])))
    assert r.state == "blocked" and "용어집" in r.reason


def test_translate_uses_glossary_retries_mismatch_and_resumes(tmp_path):
    info, fd = _project(tmp_path, ["[炎]の判定", "生命の雫"])
    run = FakeRunner([
        _reply(["[화염]의 판정"]),        # 1차: 용어 어김
        _reply(["[불꽃]의 판정"]),        # 다시 요청 -> 고침
    ])
    r = service.translate_project(info.dir, client=ClaudeCodeClient(exe="claude", runner=run))
    assert r.state == "done" and r.from_glossary == 1 and r.translated == 1 and not r.term_mismatch
    assert "Previous attempt broke" in run.inputs[1] and "炎 → 불꽃" in run.inputs[1]
    assert '"炎": "불꽃"' in run.inputs[0]  # 그 묶음에 나오는 용어를 함께 줌
    cache = load_cache(info.dir)
    assert cache["生命の雫"]["ko"] == "생명의 물방울" and cache["生命の雫"]["by"] == "glossary"
    assert cache["[炎]の判定"]["ko"] == "[불꽃]의 판정" and cache["[炎]の判定"]["retried"]
    assert workspace.load_project_info(info.dir).status == "translated"

    # 다시 돌리면 아무것도 다시 번역하지 않는다
    run2 = FakeRunner([])
    r2 = service.translate_project(info.dir, client=ClaudeCodeClient(exe="claude", runner=run2))
    assert r2.cached == 2 and run2.calls == []

    # 용어집에서 炎의 번역이 바뀌면 그 용어가 든 칸만 다시
    glossary.confirm(info, [dict(t, ko="화염") if t["source"] == "炎" else t for t in GL])
    run3 = FakeRunner([_reply(["[화염]의 판정"])])
    r3 = service.translate_project(info.dir, client=ClaudeCodeClient(exe="claude", runner=run3))
    assert r3.translated == 1 and r3.cached == 1


def test_translate_pause_keeps_finished_batches(tmp_path, monkeypatch):
    monkeypatch.setattr("app.translate.BATCH_CELLS", 1)
    info, _ = _project(tmp_path, ["[炎]の判定", "判定する"])
    limit = (1, json.dumps({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
                            "result": "You've hit your 5-hour limit · resets 3pm"}), "")
    r = service.translate_project(info.dir, client=ClaudeCodeClient(exe="claude", runner=FakeRunner(
        [_reply(["[불꽃]의 판정"]), limit])))
    assert r.state == "paused" and r.retry_after
    run = FakeRunner([_reply(["판정한다"])])
    r = service.translate_project(info.dir, client=ClaudeCodeClient(exe="claude", runner=run))
    assert r.state == "done" and len(run.calls) == 1


def test_excel_shows_draft_terms_and_checks_human_edits(tmp_path):
    from openpyxl import load_workbook

    from app.excel_io import import_translation
    from app.project import load_project

    info, fd = _project(tmp_path, ["[炎]の判定", "生命の雫"])
    service.translate_project(info.dir, client=ClaudeCodeClient(exe="claude", runner=FakeRunner(
        [_reply(["[불꽃]의 판정"])])))
    [x] = service.export_project_excel(info.dir)
    wb = load_workbook(x)
    ws = wb["번역"]
    head = [c.value for c in ws[1]]
    row = [c.value for c in ws[2]]
    assert row[head.index("초벌 번역")] == "[불꽃]의 판정" and row[head.index("번역")] == "[불꽃]의 판정"
    assert row[head.index("사용한 용어")] == "判定 → 판정, 炎 → 불꽃"
    ws.cell(2, head.index("번역") + 1).value = "[화염]의 판정"  # 사람이 용어를 어기게 고침
    wb.save(x)
    res = import_translation(load_project(fd), x)
    assert any("용어 불일치" in i and "炎 → 불꽃" in i for i in res.issues)


def test_glossary_review_marks_terms_only_from_unresolved_cells(tmp_path):
    from openpyxl import load_workbook

    pdf, regions = _pdf_and_regions(tmp_path)
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    workspace.add_files(info, [pdf], "카드", new_group=True, regions_from=regions)
    fd = workspace.file_dirs(info)[0]
    _write_extract(fd, 1, "盗賊フェレット", uncertain=True,
                   readings=[{"model": "a", "source": "盗賊フェレット"}, {"model": "b", "source": "盗賊フェレット"}])
    _write_extract(fd, 2, "[炎]の判定")
    terms_reply = (0, json.dumps({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0,
                                  "modelUsage": {}, "structured_output": {"terms": [
                                      {"term": "盗賊フェレット", "category": "카드 이름", "suggested_ko": "도적 페럿",
                                       "reason": ""},
                                      {"term": "判定", "category": "행동", "suggested_ko": "판정", "reason": ""}]}}), "")
    r = service.finish_extraction(info.dir, accept_issues=True,
                                  client=ClaudeCodeClient(exe="claude", runner=FakeRunner([terms_reply, _assess_reply()])))
    ws = load_workbook(r.review_xlsx)["용어집"]
    head = [c.value for c in ws[1]]
    col = head.index("원문 상태")
    rows = {ws.cell(i, 1).value: ws.cell(i, col + 1).value for i in range(2, ws.max_row + 1) if ws.cell(i, 1).value}
    assert rows["盗賊フェレット"] == "결론 못 냄" and rows["判定"] is None and rows["炎"] is None
    note = ws.cell([ws.cell(i, 1).value for i in range(1, ws.max_row + 1)].index("盗賊フェレット") + 1, 1).comment
    assert "틀렸을 수 있습니다" in note.text


def test_korean_inflection_counts_as_using_the_term():
    from app.translate import ko_term_used

    assert ko_term_used("버리기", "이 카드를 버린다.")
    assert ko_term_used("버리기", "카드를 버려야 한다")
    assert ko_term_used("회복하다", "HP를 2 회복한다")
    assert ko_term_used("판정", "모든 판정에")
    assert not ko_term_used("버리기", "이 카드를 파기한다")
    assert not ko_term_used("불꽃", "[화염] 카드")
    gl = [{"source": "破棄", "ko": "버리기", "variants": []}]
    assert check("このカードを破棄する", "이 카드를 버린다", gl) == ([], [])
