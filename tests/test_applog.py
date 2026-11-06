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
