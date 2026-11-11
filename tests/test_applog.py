"""동작 로그 (applog): 파일에 남는지, API 키를 가리는지, 묶어서 저장되는지."""

from __future__ import annotations

import json
import logging
import zipfile

import pytest

from app import applog


@pytest.fixture
def logdir(tmp_path, monkeypatch):
    """임시 폴더에 로그를 열고, 끝나면 원래대로 (다른 테스트에 남지 않게)."""
    import faulthandler
    import sys
    import threading

    old_hooks = (sys.excepthook, threading.excepthook)
    applog._state.clear()
    monkeypatch.setenv("CARDTRANSLATER_LOG_DIR", str(tmp_path / "logs"))
    path = applog.setup()
    yield path
    h = applog._state.get("handler")
    if h:
        applog.log.removeHandler(h)
        h.close()
    faulthandler.disable()
    if applog._state.get("crash"):
        applog._state["crash"].close()
    applog._state.clear()
    sys.excepthook, threading.excepthook = old_hooks


def _read(path):
    applog._state["handler"].flush()
    return path.read_text(encoding="utf-8")


def test_setup_writes_and_masks_key(logdir):
    assert logdir.parent.name == "logs" and logdir.name == applog.LOG_NAME
    assert applog.setup() == logdir  # 두 번 불러도 같은 파일
    logging.getLogger("cardtranslater.test").info("키 sk-ant-api03-SECRETSECRET 사용")
    try:
        raise ValueError("sk-ant-api03-INTRACEBACK")
    except ValueError:
        logging.getLogger("cardtranslater.test").exception("실패")
    text = _read(logdir)
    assert "SECRETSECRET" not in text and "INTRACEBACK" not in text
    assert "sk-ant-…(가림)" in text and "ValueError" in text


def test_startup_and_export(logdir, tmp_path):
    applog.log_startup({"engine": "claude-code"})
    out = applog.export(tmp_path / "out" / "logs.zip", {"note": "sk-ant-api03-ZZZZZZZZ"})
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        assert applog.LOG_NAME in names and "system.json" in names
        info = json.loads(z.read("system.json"))
        assert "ZZZZZZZZ" not in z.read("system.json").decode("utf-8")
        assert "CardTranslater 시작" in z.read(applog.LOG_NAME).decode("utf-8")
    assert info["build"]["version"]  # 개발 중에는 "dev"


def test_job_logs_start_end_and_failure(logdir):
    from app import service
    from app.qt import worker

    def ok_fn(n, progress=None, cancel=None):
        progress(service.ProgressEvent("erase", 1, 2, "1쪽 지우는 중"))
        return n * 2

    def bad_fn(progress=None, cancel=None):
        raise RuntimeError("일부러 낸 오류")

    worker.Job(ok_fn, 21).run()
    worker.Job(bad_fn).run()
    text = _read(logdir)
    assert "작업 시작: ok_fn (21)" in text and "1쪽 지우는 중" in text and "작업 끝: ok_fn" in text and "→ 42" in text
    assert "작업 실패: bad_fn" in text and "일부러 낸 오류" in text


def test_service_export_logs(logdir, tmp_path, monkeypatch):
    from app import service

    monkeypatch.setattr("app.service.find_claude", lambda: r"C:\x\claude.exe")
    assert service.log_folder() == logdir.parent
    out = service.export_logs(tmp_path / "r.zip")
    with zipfile.ZipFile(out) as z:
        info = json.loads(z.read("system.json"))
    assert info["claude"] == r"C:\x\claude.exe" and "engine" in info


# ---------------------------------------------------------------- 문제 위치·단계별 시간
def test_context_adds_location_to_warnings_only(logdir):
    lg = logging.getLogger("cardtranslater.test")
    with applog.context(프로젝트="엘프", 쪽=10):
        with applog.context(영역="본문"):
            lg.warning("지우기 경고")
        lg.info("보통 줄")
    lg.warning("밖의 경고")
    text = _read(logdir)
    assert "지우기 경고 | 위치: 프로젝트=엘프, 쪽=10, 영역=본문" in text
    assert "보통 줄 | 위치" not in text and "밖의 경고 | 위치" not in text


def test_timings_summary_and_report(logdir):
    import threading

    tm = applog.Timings("① 글자 추출 [엘프]")
    with tm.part("원본 자르기"):
        pass
    ths = [threading.Thread(target=tm.add, args=("Claude 요청", 2.0)) for _ in range(4)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    line = tm.log(카드="4장 읽음", 실패=None)
    assert line.startswith("[시간] ① 글자 추출 [엘프]") and "Claude 요청 8.0초 (4회, 평균 2.00초, 최대 2.00초)" in line
    assert "카드 4장 읽음" in line and "실패" not in line
    applog.log_job_time("extract_project", 19.2, "done")
    rep = applog.timing_report()
    assert "작업 ① 글자 추출 (extract_project) 19.2초 done" in rep and "    ① 글자 추출 [엘프]" in rep
    out = applog.export(logdir.parent / "x.zip")
    with zipfile.ZipFile(out) as z:
        assert "Claude 요청" in z.read("timings.txt").decode("utf-8")


def test_job_runner_writes_cycle_line_with_state(logdir):
    from types import SimpleNamespace as NS

    from app.qt import worker

    def extract_project(progress=None, cancel=None):
        return NS(state="paused")

    worker.Job(extract_project).run()
    assert "[시간] 작업 ① 글자 추출 (extract_project)" in _read(logdir) and "paused" in _read(logdir)


def test_cli_log_times_reads_a_sent_log(tmp_path, capsys):
    from app import cli

    f = tmp_path / "sent.log"
    f.write_text("2026-11-11 10:00:00,000 INFO    [Dummy-1] cardtranslater.time: [시간] ⑦ 글자 지우기 [엘프] 16.0초 | 쪽 20\n"
                 "2026-11-11 10:00:01,000 INFO    [Dummy-1] cardtranslater.job: 작업 끝: erase_project\n"
                 "2026-11-11 10:00:01,000 INFO    [Dummy-1] cardtranslater.time: [시간] 작업 ⑦ 글자 지우기 (erase_project) 16.2초 끝\n",
                 encoding="utf-8")
    assert cli.main(["log-times", "--file", str(f)]) == 0
    out = capsys.readouterr().out
    assert "2026-11-11 10:00:00      ⑦ 글자 지우기 [엘프] 16.0초 | 쪽 20" in out
    assert "2026-11-11 10:00:01  작업 ⑦ 글자 지우기 (erase_project) 16.2초 끝" in out and "작업 끝: erase" not in out


def test_log_summary_errors_never_fail_the_job(logdir):
    from app import runlog

    applog.safe(runlog.log_compose, "p", [{"page": 1, "regions": {"이름": {"status": ["넘침"], "errors": ("x",),
                                                                          "warnings": ["y"]}}}], 1.0, 1)
    applog.safe(runlog.log_erase, "p", [{"source_page": 1, "regions": [{"erase": "이상한 값"}]}], 1.0, 1)  # 오류는 로그로만
    assert "로그 요약을 적지 못함" in _read(logdir) or "[시간] ⑦ 글자 지우기" in _read(logdir)
    two = "2026-11-11 10:00:00,000 I t: [시간] 첫줄\n2026-11-11 10:00:01,000 I t: [시간] 둘째줄"
    assert "첫줄" in applog.timing_report(two, last=0) and "둘째줄" in applog.timing_report(two, last=0)  # 0 = 전부
    one = applog.timing_report(two, last=1)
    assert "둘째줄" in one and "첫줄" not in one
