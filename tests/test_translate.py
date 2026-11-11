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
    ws = wb["카드"]  # 게임 검수 엑셀: 그룹마다 시트
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
    assert rows["盗賊フェレット"] == "결론 못 냄" and rows["判定"] == "확실" and rows["炎"] == "확실"
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


def test_terms_in_skips_term_covered_by_longer_term():
    from app.translate import terms_in

    gl = [{"source": "トラップ", "ko": "함정", "category": "분류어·태그"},
          {"source": "ガストラップドア", "ko": "가스 트랩도어", "category": "카드 이름"},
          {"source": "判定", "ko": "판정", "category": "기타"},
          {"source": "すべての判定にボーナスがもらえる", "ko": "모든 판정에 보너스를 받는다", "category": "문구"}]
    assert [t["source"] for t in terms_in("ガストラップドア", gl)] == ["ガストラップドア"]
    assert {t["source"] for t in terms_in("最悪のトラップだ", gl)} == {"トラップ"}
    # 문구는 검사하지 않으므로 그 안의 용어(判定)는 그대로 검사 대상
    assert {t["source"] for t in terms_in("すべての判定にボーナスがもらえる", gl)} == {
        "判定", "すべての判定にボーナスがもらえる"}


def test_leftover_japanese_is_flagged_and_retranslated_once(tmp_path):
    assert check("一挙手一投足が", "一挙手一投足이", [])[0] == ["원문 글자 남음"]
    assert check("[[coin]]を得る", "[[coin]]을 얻는다", [])[0] == []
    info, _ = _project(tmp_path, ["一挙手一投足が引き金"])
    run = FakeRunner([_reply(["一挙手一投足이 방아쇠"]), _reply(["일거수일투족이 방아쇠"])])
    r = service.translate_project(info.dir, client=ClaudeCodeClient(exe="claude", runner=run))
    assert r.state == "done" and not r.leftover
    assert "translate every Japanese word" in run.inputs[1]
    assert load_cache(info.dir)["一挙手一投足が引き金"]["ko"] == "일거수일투족이 방아쇠"

    # 예전 캐시에 일본어가 남아 있으면 한 번만 다시 번역한다
    cache = load_cache(info.dir)
    cache["一挙手一投足が引き金"].update(ko="一挙手이 방아쇠", status=[])
    cache["一挙手一投足が引き金"].pop("retried_leftover", None)
    from app.translate import save_cache
    save_cache(info.dir, cache)
    run2 = FakeRunner([_reply(["일거수일투족이 방아쇠"])])
    r2 = service.translate_project(info.dir, client=ClaudeCodeClient(exe="claude", runner=run2))
    assert r2.translated == 1 and len(run2.calls) == 1


def test_keep_brackets_restores_corner_brackets():
    from app.translate import SYSTEM_PROMPT, keep_brackets

    src = "プレイ時効果\n「5+生活[[house]]タグ+商売[[coin_bag]]タグ」の\n[[coin]]を得る"
    ko = "플레이 시 효과\n“5+생활[[house]]태그+상업[[coin_bag]]태그”의\n[[coin]]을 얻는다"
    assert keep_brackets(src, ko) == ko.replace("“", "「").replace("”", "」")
    assert keep_brackets("『本』を読む", "‘책’을 읽는다") == "『책』을 읽는다"
    # 원문에 괄호가 없으면 따옴표는 그대로, 이미 「」면 그대로, 수가 안 맞으면 건드리지 않는다
    assert keep_brackets("彼は言った", "“좋아”라고 했다") == "“좋아”라고 했다"
    assert keep_brackets("「A」", "「가」") == "「가」"
    assert keep_brackets("「A」「B」", "“가와 나”") == "“가와 나”"
    assert "「」" in SYSTEM_PROMPT and "become Korean" not in SYSTEM_PROMPT


def test_translate_batches_run_concurrently_and_pause_keeps_sent_batches(tmp_path, monkeypatch):
    """묶음 여러 개를 동시에 보내고, 한도에 걸리면 이미 보낸 묶음은 받아 저장한 뒤 멈춘다."""
    import threading
    import time as _t

    monkeypatch.setattr("app.translate.BATCH_CELLS", 1)
    monkeypatch.setenv("CARDTRANSLATER_EXTRACT_WORKERS", "3")
    from tests.test_service import make_project

    texts = ["一", "二", "三", "四", "五", "六"]
    src = make_project(tmp_path / "src", pages=len(texts))
    info, _ = workspace.create_project("p", "G", root=tmp_path)
    workspace.add_files(info, [src / "source" / "cards.pdf"], "카드", new_group=True, regions_from=src / "regions.json")
    fd = workspace.file_dirs(info)[0]
    for i, t in enumerate(texts, start=1):
        _write_extract(fd, i, t)
    glossary.confirm(info, [])
    workspace.set_status(info, "glossary_confirmed")
    lock, state = threading.Lock(), {"now": 0, "peak": 0}
    limit = json.dumps({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
                        "result": "rate limit"})

    def runner(args, cwd, timeout, input_text=None):
        src = json.loads(input_text.split("Items (JSON lines):\n")[1].splitlines()[0])["source"]
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        try:
            if src == "二":
                return 0, limit, ""  # 한도: 바로
            _t.sleep(0.3)
            return _reply([f"번역{src}"])
        finally:
            with lock:
                state["now"] -= 1

    r = service.translate_project(info.dir, client=ClaudeCodeClient(exe="claude", runner=runner))
    assert r.state == "paused"
    cache = load_cache(info.dir)
    assert set(cache) == {"一", "三"} and cache["三"]["ko"] == "번역三"  # 함께 보낸 1·3번은 저장, 그 뒤로는 보내지 않음
    assert state["peak"] >= 2
