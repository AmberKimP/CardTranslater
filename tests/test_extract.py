"""글자 추출(app.extract): 가짜 client로 이어 하기·일시 정지·일괄 처리를 확인한다 (API 호출·비용 없음)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace as NS

import anthropic
import httpx2
import pytest

from app import service
from app.extract import ExtractState, evaluate, extract_dir, load_extracted
from tests.test_service import make_project


def _resp(status: int, headers=None):
    return httpx2.Response(status, headers=headers or {},
                           request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))


def _msg(page: int, text: str = "HELLO", uncertain=False):
    body = json.dumps({"regions": [{"name": "이름", "text": text, "uncertain": uncertain, "note": ""}]})
    return NS(content=[NS(type="text", text=body)], stop_reason="end_turn", model="claude-opus-5-5",
              usage=NS(input_tokens=2000, output_tokens=600, cache_creation_input_tokens=0, cache_read_input_tokens=0))


class FakeClient:
    """messages 요청을 순서대로 처리한다. fail_at: {호출 번호: 던질 예외}."""

    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at or {}
        self.batch_store = {}
        outer = self

        class _Beta:
            class messages:  # noqa: N801
                @staticmethod
                def create(**kw):
                    n = len(outer.calls)
                    outer.calls.append(kw)
                    if n in outer.fail_at:
                        raise outer.fail_at[n]
                    page = int(kw["messages"][0]["content"][0]["text"].split("page ")[1].split(".")[0])
                    return _msg(page)

        class _Batches:
            @staticmethod
            def create(requests):
                bid = f"msgbatch_{len(outer.batch_store) + 1}"
                outer.batch_store[bid] = {"requests": requests, "status": "in_progress"}
                return NS(id=bid, processing_status="in_progress")

            @staticmethod
            def retrieve(bid):
                return NS(id=bid, processing_status=outer.batch_store[bid]["status"])

            @staticmethod
            def results(bid):
                for r in outer.batch_store[bid]["requests"]:
                    pno = int(r["custom_id"].split("-")[1])
                    if pno == 2 and outer.batch_store[bid].get("expire_page_2"):
                        yield NS(custom_id=r["custom_id"], result=NS(type="expired"))
                    else:
                        yield NS(custom_id=r["custom_id"], result=NS(type="succeeded", message=_msg(pno)))

        self.beta = _Beta()
        self.messages = NS(batches=_Batches(), create=_Beta.messages.create)


def test_rate_limit_pauses_and_rerun_resumes_without_rereading(tmp_path):
    proj = make_project(tmp_path, pages=3)
    rl = anthropic.RateLimitError("rate limited", response=_resp(429, {"retry-after": "30"}), body=None)
    c1 = FakeClient(fail_at={1: rl})
    r = service.extract(proj, client=c1, model="opus", engine="api")
    assert r.state == "paused" and "429" in r.reason and r.retry_after
    assert r.done_pages == [1] and r.remaining_pages == [2, 3]
    assert ExtractState.load(extract_dir(proj) / "state.json").status == "paused"

    c2 = FakeClient()
    r = service.extract(proj, client=c2, model="opus", engine="api")
    assert r.state == "done" and r.done_pages == [1, 2, 3]
    assert len(c2.calls) == 2  # 1쪽은 다시 읽지 않는다
    # 요청 모양: Opus 5.5 + 구조화 출력 + 거절 시 대체 모델
    kw = c2.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert any(b["type"] == "image" for b in kw["messages"][0]["content"])
    assert load_extracted(proj)[2]["이름"]["source"] == "HELLO"
    assert r.spent_usd == pytest.approx(3 * (2000 * 4 + 600 * 20) / 1e6)


def test_credit_exhausted_pauses(tmp_path):
    proj = make_project(tmp_path, pages=2)
    err = anthropic.BadRequestError("Your credit balance is too low to access the Anthropic API.",
                                    response=_resp(400), body=None)
    r = service.extract(proj, client=FakeClient(fail_at={0: err}))
    assert r.state == "paused" and "크레딧" in r.reason and r.done_pages == []


def test_bad_request_for_one_card_marks_it_failed_and_continues(tmp_path):
    proj = make_project(tmp_path, pages=2)
    err = anthropic.BadRequestError("image too large", response=_resp(400), body=None)
    r = service.extract(proj, client=FakeClient(fail_at={0: err}))
    assert r.state == "done" and r.done_pages == [2] and 1 in r.failed
    r = service.extract(proj, client=FakeClient(), retry_failed=True)
    assert r.done_pages == [1, 2] and not r.failed


def test_budget_pauses_and_can_continue_with_bigger_budget(tmp_path):
    proj = make_project(tmp_path, pages=3)
    r = service.extract(proj, client=FakeClient(), budget_usd=0.03)  # 1장 ≈ $0.02
    assert r.state == "paused" and "예산" in r.reason and r.done_pages == [1, 2]
    r = service.extract(proj, client=FakeClient(), budget_usd=1.0)
    assert r.state == "done" and r.done_pages == [1, 2, 3]


def test_batch_mode_survives_restart_and_resubmits_expired(tmp_path):
    proj = make_project(tmp_path, pages=3)
    client = FakeClient()
    r = service.extract(proj, client=client, batch=True, engine="api")
    assert r.state == "waiting_batch" and r.batch_id == "msgbatch_1" and r.done_pages == []
    # 앱을 껐다 켬 = 새 호출. 아직 처리 중이면 다시 보내지 않는다
    r = service.extract(proj, client=client, batch=True, engine="api")
    assert r.state == "waiting_batch" and len(client.batch_store) == 1
    # 끝남. 2쪽은 만료 -> 받아 온 뒤 2쪽만 새 작업으로
    client.batch_store["msgbatch_1"].update(status="ended", expire_page_2=True)
    r = service.extract(proj, client=client, batch=True, engine="api")
    assert r.done_pages == [1, 3] and r.state == "waiting_batch" and r.batch_id == "msgbatch_2"
    assert [q["custom_id"] for q in client.batch_store["msgbatch_2"]["requests"]] == ["page-2"]
    assert "fallbacks" not in client.batch_store["msgbatch_2"]["requests"][0]["params"]  # 일괄 처리는 지원 안 함
    client.batch_store["msgbatch_2"]["status"] = "ended"
    r = service.extract(proj, client=client, batch=True, engine="api")
    assert r.state == "done" and r.done_pages == [1, 2, 3]
    # 일괄 처리 가격은 절반
    rec = json.loads((extract_dir(proj) / "pages" / "page-001.json").read_text(encoding="utf-8"))
    assert rec["batch"] and rec["cost_usd"] == pytest.approx((2000 * 4 + 600 * 20) / 1e6 / 2)


def test_missing_credentials_is_blocked_not_a_crash(tmp_path, monkeypatch):
    proj = make_project(tmp_path, pages=1)

    def boom():
        raise anthropic.AnthropicError("no API key")

    monkeypatch.setattr("app.extract.make_client", boom)
    r = service.extract(proj, engine="api")
    assert r.state == "blocked" and "API" in r.reason


def test_excel_uses_extracted_source_and_flags_uncertain(tmp_path):
    from openpyxl import load_workbook

    proj = make_project(tmp_path, pages=1)
    client = FakeClient()
    client.beta.messages.create = lambda **kw: _msg(1, "HELL0", uncertain=True)
    service.extract(proj, client=client)
    x = service.export_excel(proj)
    ws = load_workbook(x)["번역"]
    head = [c.value for c in ws[1]]
    row = [c.value for c in ws[2]]
    assert row[head.index("원문")] == "HELL0" and "확인 필요" in row[head.index("상태")]


def test_evaluate_ignores_width_and_spaces():
    ev = evaluate({1: {"본문": {"source": "ＡＢ　ｃ！\n", "uncertain": False}}},
                  {1: {"본문": {"source": "AB c!"}}})
    assert ev["exact_rate"] == 1.0


@pytest.mark.parametrize("name,model_id,fallback,price", [
    ("opus", "claude-opus-5-5", True, (4.0, 20.0)),
    ("sonnet", "claude-sonnet-5-5", True, (2.0, 10.0)),
    ("haiku", "claude-haiku-5-5", False, (0.10, 0.50)),
])
def test_model_option_sets_model_fallback_and_price(tmp_path, name, model_id, fallback, price):
    proj = make_project(tmp_path, pages=1)
    client = FakeClient()

    def create(**kw):  # 응답 모델도 요청 모델로
        client.calls.append(kw)
        m = _msg(1)
        m.model = kw["model"]
        return m

    client.beta.messages.create = create
    client.messages.create = create
    r = service.extract(proj, client=client, model=name)
    assert r.state == "done" and r.model == model_id
    kw = client.calls[0]
    assert kw["model"] == model_id
    assert ("fallbacks" in kw) == fallback  # Haiku는 서버 대체 모델이 없다
    assert r.spent_usd == pytest.approx((2000 * price[0] + 600 * price[1]) / 1e6, abs=1e-4)


def test_test_mode_keeps_models_apart_and_eval_reads_each(tmp_path):
    proj = make_project(tmp_path, pages=2)
    out = tmp_path / "runs"
    r1 = service.extract(proj, client=FakeClient(), model="haiku", test=True, out_root=out)
    r2 = service.extract(proj, client=FakeClient(), model="opus", test=True, out_root=out)
    assert r1.output_dir != r2.output_dir and r1.output_dir.name == "extract-proj-haiku"
    assert not (proj / "extract").exists()  # 프로젝트 결과는 건드리지 않는다
    ev = service.evaluate_extract(proj, r1.output_dir)
    assert ev["cells"] == 2 and ev["exact_rate"] == 1.0  # 샘플 원문 HELLO와 같다
    # 다시 부르면 이어 간다 (이미 읽은 쪽은 건너뜀)
    c = FakeClient()
    service.extract(proj, client=c, model="haiku", test=True, out_root=out)
    assert c.calls == []


def test_unknown_model_is_blocked(tmp_path):
    proj = make_project(tmp_path, pages=1)
    assert service.extract(proj, client=FakeClient(), model="gpt").state == "blocked"



# ---------------------------------------------------------------- Claude Code 엔진 (가짜 실행기)
from app.extract_cc import ClaudeCodeClient  # noqa: E402


def _cc_ok(text="HELLO"):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.0123,
                       "modelUsage": {"claude-opus-5-5": {}}, "usage": {"input_tokens": 5},
                       "structured_output": {"regions": [{"name": "이름", "text": text, "uncertain": False,
                                                          "note": ""}]}})


class FakeRunner:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, args, cwd, timeout):
        self.calls.append((args, Path(cwd)))
        return self.outputs.pop(0)


def test_claude_code_engine_reads_with_crops_and_schema(tmp_path):
    proj = make_project(tmp_path, pages=2)
    run = FakeRunner([(0, _cc_ok(), ""), (0, _cc_ok(), "")])
    r = service.extract(proj, client=ClaudeCodeClient(exe="claude", runner=run))
    assert r.state == "done" and r.engine == "claude-code" and r.done_pages == [1, 2]
    assert r.model == "claude-sonnet-5-5"  # 기본 모델
    assert r.spent_usd == 0 and r.equiv_usd == pytest.approx(0.0246)
    args, cwd = run.calls[0]
    assert args[:2] == ["claude", "-p"] and "--bare" not in args
    assert args[args.index("--model") + 1] == "sonnet"
    assert args[args.index("--tools") + 1] == "Read"
    assert json.loads(args[args.index("--json-schema") + 1])["required"] == ["regions"]
    assert (cwd / "region01.png").exists()  # 영역 이미지를 파일로 만들어 읽게 한다
    rec = json.loads((extract_dir(proj) / "pages" / "page-001.json").read_text(encoding="utf-8"))
    assert rec["engine"] == "claude-code" and rec["cost_usd"] == 0


def test_claude_code_usage_limit_pauses_then_resumes(tmp_path):
    proj = make_project(tmp_path, pages=3)
    limit = json.dumps({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
                        "result": "Claude AI usage limit reached|4102444800"})
    r = service.extract(proj, client=ClaudeCodeClient(exe="claude", runner=FakeRunner([(0, _cc_ok(), ""),
                                                                                      (1, limit, "")])))
    assert r.state == "paused" and "한도" in r.reason and r.retry_after and r.done_pages == [1]
    run = FakeRunner([(0, _cc_ok(), ""), (0, _cc_ok(), "")])
    r = service.extract(proj, client=ClaudeCodeClient(exe="claude", runner=run))
    assert r.state == "done" and r.done_pages == [1, 2, 3] and len(run.calls) == 2


def test_claude_code_not_logged_in_is_blocked(tmp_path):
    proj = make_project(tmp_path, pages=1)
    run = FakeRunner([(1, "", "Not logged in · Please run /login")])
    r = service.extract(proj, client=ClaudeCodeClient(exe="claude", runner=run))
    assert r.state == "blocked" and "로그인" in r.reason


def test_claude_code_bad_output_fails_only_that_card(tmp_path):
    proj = make_project(tmp_path, pages=2)
    run = FakeRunner([(0, "not json", ""), (0, _cc_ok(), "")])
    r = service.extract(proj, client=ClaudeCodeClient(exe="claude", runner=run))
    assert r.state == "done" and r.done_pages == [2] and 1 in r.failed


def test_batch_needs_api_engine(tmp_path):
    proj = make_project(tmp_path, pages=1)
    r = service.extract(proj, batch=True)
    assert r.state == "blocked" and "api" in r.reason


def test_reset_time_parsing():
    import time as _t

    from app.extract_cc import _reset_seconds

    now = _t.mktime((2026, 11, 4, 12, 3, 0, 0, 0, -1))
    s = _reset_seconds("You've hit your 5-hour limit · resets 1:10pm (Asia/Seoul)", now)
    assert s == pytest.approx(67 * 60 + 60)
    assert _reset_seconds("limit · resets 11am", now) == pytest.approx((23 * 60 - 3) * 60 + 60)  # 다음 날
    assert _reset_seconds("Claude AI usage limit reached|4102444800", now) > 0
    assert _reset_seconds("no time here", now) is None


# ---------------------------------------------------------------- 두 번 확인 (기본 모드)
def _cc_reply(model, text, uncertain):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.01,
                       "modelUsage": {model: {}}, "usage": {},
                       "structured_output": {"regions": [{"name": "이름", "text": text, "uncertain": uncertain,
                                                          "note": "흐림" if uncertain else ""}]}})


def test_double_check_rereads_only_uncertain_cells_with_opus(tmp_path):
    proj = make_project(tmp_path, pages=3)
    run = FakeRunner([
        (0, _cc_reply("claude-sonnet-5-5", "HELLO", False), ""),   # 1쪽: 확실 -> 다시 안 읽음
        (0, _cc_reply("claude-sonnet-5-5", "HELL0", True), ""),    # 2쪽: 불확실
        (0, _cc_reply("claude-sonnet-5-5", "HELIO", True), ""),    # 3쪽: 불확실
        (0, _cc_reply("claude-opus-5-5", "ＨＥＬＬ0", False), ""),  # 2쪽 Opus: 같게 읽음 (전각 무시)
        (0, _cc_reply("claude-opus-5-5", "HELLO", False), ""),     # 3쪽 Opus: 다르게 읽음
    ])
    r = service.extract(proj, client=ClaudeCodeClient(exe="claude", runner=run))
    assert r.state == "done" and r.mode == "double-check" and r.model == "claude-sonnet-5-5"
    models = [a[a.index("--model") + 1] for a, _ in run.calls]
    assert models == ["sonnet", "sonnet", "sonnet", "opus", "opus"]
    assert (r.recheck_cells, r.recheck_agreed, r.recheck_disagreed) == (2, 1, 1)
    got = load_extracted(proj)
    assert got[2]["이름"]["uncertain"] is False and got[2]["이름"]["source"] == "HELL0"
    assert got[3]["이름"]["uncertain"] is True and got[3]["이름"]["source"] == "HELLO"  # Opus 쪽을 원문으로
    assert "HELIO" in got[3]["이름"]["note"] and len(got[3]["이름"]["readings"]) == 2
    # 다시 불러도 이미 확인한 칸은 다시 읽지 않는다
    run2 = FakeRunner([])
    assert service.extract(proj, client=ClaudeCodeClient(exe="claude", runner=run2)).state == "done"
    assert run2.calls == []


def test_double_check_pause_during_recheck_resumes(tmp_path):
    proj = make_project(tmp_path, pages=2)
    limit = json.dumps({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
                        "result": "You've hit your 5-hour limit · resets 1:10pm (Asia/Seoul)"})
    run = FakeRunner([(0, _cc_reply("claude-sonnet-5-5", "X", True), ""),
                      (0, _cc_reply("claude-sonnet-5-5", "Y", True), ""),
                      (0, _cc_reply("claude-opus-5-5", "X", False), ""),
                      (1, limit, "")])
    r = service.extract(proj, client=ClaudeCodeClient(exe="claude", runner=run))
    assert r.state == "paused" and "두 번째 확인" in r.reason and r.retry_after
    run2 = FakeRunner([(0, _cc_reply("claude-opus-5-5", "Y", False), "")])  # 2쪽만 남음
    r = service.extract(proj, client=ClaudeCodeClient(exe="claude", runner=run2))
    assert r.state == "done" and len(run2.calls) == 1
    assert all(not v["이름"]["uncertain"] for v in load_extracted(proj).values())


def test_opus_mode_reads_once_with_opus(tmp_path):
    proj = make_project(tmp_path, pages=1)
    run = FakeRunner([(0, _cc_reply("claude-opus-5-5", "HELLO", True), "")])
    r = service.extract(proj, mode="opus", client=ClaudeCodeClient(exe="claude", runner=run))
    assert r.state == "done" and r.model == "claude-opus-5-5" and len(run.calls) == 1 and r.recheck_cells == 0


def test_unknown_mode_is_blocked(tmp_path):
    proj = make_project(tmp_path, pages=1)
    assert service.extract(proj, mode="haiku", client=ClaudeCodeClient(exe="claude", runner=FakeRunner([]))).state == "blocked"


def test_excel_shows_how_each_source_was_verified(tmp_path):
    from openpyxl import load_workbook

    proj = make_project(tmp_path, pages=4)
    run = FakeRunner([
        (0, _cc_reply("claude-sonnet-5-5", "HELLO", False), ""),   # 1쪽: Sonnet 1회
        (0, _cc_reply("claude-sonnet-5-5", "HELL0", True), ""),    # 2쪽: 두 번 확인 (같음)
        (0, _cc_reply("claude-sonnet-5-5", "HELIO", True), ""),    # 3쪽: 결론 못 냄 (다름)
        (0, _cc_reply("claude-opus-5-5", "HELL0", False), ""),
        (0, _cc_reply("claude-opus-5-5", "HELLO", False), ""),
    ])
    service.extract(proj, pages="1-3", client=ClaudeCodeClient(exe="claude", runner=run))
    x = service.export_excel(proj)  # 4쪽은 추출 안 함 -> 손 입력
    ws = load_workbook(x)["번역"]
    head = [c.value for c in ws[1]]
    col = head.index("원문 확인")
    assert head[col - 1] == "원문"
    got = [ws.cell(r, col + 1).value for r in range(2, 6)]
    assert got == ["Sonnet 1회", "두 번 확인", "결론 못 냄", "손 입력"]
    note = ws.cell(4, head.index("원문") + 1).comment.text
    assert "Sonnet: HELIO" in note and "Opus: HELLO" in note
    # 새 열이 있어도 가져오기는 그대로 된다
    from app.excel_io import import_translation
    from app.project import load_project

    res = import_translation(load_project(proj), x)
    assert not res.blocking and res.rows == 4
