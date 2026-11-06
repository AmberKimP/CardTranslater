"""동작 로그 보강: 시작 시점·가리기·취소·오류 창·claude 호출 기록·도움말 메뉴. 실제 claude/API 없음."""

from __future__ import annotations

import os
import subprocess
import zipfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402

from app import applog, extract_cc, service  # noqa: E402
from tests.test_applog import _read, logdir  # noqa: E402,F401  (고정 장치 재사용)

_REAL_RUN = extract_cc._run_subprocess  # conftest가 막기 전의 진짜 함수 (subprocess.run은 가짜로 바꿔 쓴다)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("CARDTRANSLATER_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setenv("CARDTRANSLATER_LOG_DIR", str(tmp_path / "logs"))
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Ok)


def test_selftest와_테스트는_로그를_열지_않는다(qapp, tmp_path):
    """--selftest(와 일반 테스트)는 로그 파일·훅을 만들지 않는다: 실제 LOCALAPPDATA를 건드리지 않는다."""
    from app.qt import main as qmain

    applog._state.clear()
    old_font = qapp.font()  # main()이 앱 글꼴을 바꾸므로 다른 화면 테스트에 남지 않게 되돌린다
    try:
        assert qmain.main(["x", "--selftest", str(tmp_path / "st.json")]) in (0, 1)
    finally:
        qapp.setFont(old_font)
    assert not applog._state
    assert not (tmp_path / "logs").exists()


def test_일반_시작은_로그를_연다(qapp, monkeypatch):
    """일반 시작에서는 start_logging을 한 번 부르고, 실패해도 프로그램은 계속 시작한다."""
    from app.qt import main as qmain

    calls = []
    monkeypatch.setattr(service, "start_logging", lambda: calls.append(1) or (_ for _ in ()).throw(OSError("막힘")))
    monkeypatch.setattr(qmain, "MainWindow", lambda *a, **k: type("W", (), {
        "resume_last": lambda s: None, "show": lambda s: None})())
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    old_font = qapp.font()
    try:
        assert qmain.main(["x", "--no-login"]) == 0
    finally:
        qapp.setFont(old_font)
    assert calls == [1]


def test_예외_본문_안의_키도_가리고_export도_가린다(logdir, tmp_path):
    """예외 메시지·args 형식 인자 안의 sk-ant 키가 파일과 zip 둘 다에서 가려진다."""
    lg = applog.logging.getLogger("cardtranslater.t")
    try:
        raise RuntimeError("401 key=sk-ant-oat01-LEAKLEAKLEAK")
    except RuntimeError:
        lg.error("실패 %s", "sk-ant-api03-ARGLEAKARG", exc_info=True)
    out = applog.export(tmp_path / "e.zip")
    with zipfile.ZipFile(out) as z:
        blob = b"".join(z.read(n) for n in z.namelist()).decode("utf-8")
    assert "LEAKLEAKLEAK" not in blob and "ARGLEAKARG" not in blob
    assert "LEAKLEAKLEAK" not in _read(logdir)


def test_export는_회전된_로그도_담고_로그가_없어도_된다(tmp_path):
    """로그를 연 적 없어도 export가 system.json만 담아 실패하지 않는다."""
    applog._state.clear()
    out = applog.export(tmp_path / "none.zip")
    with zipfile.ZipFile(out) as z:
        assert z.namelist() == ["system.json"]


def test_작업_취소가_기록된다(logdir):
    """Cancelled로 끝난 작업은 '작업 취소'로 남고 실패로 남지 않는다."""
    from app.qt import worker

    def slow(progress=None, cancel=None):
        raise service.Cancelled()

    got = []
    j = worker.Job(slow)
    j.signals.cancelled.connect(lambda: got.append("c"))
    j.run()
    text = _read(logdir)
    assert "작업 취소: slow" in text and "작업 실패" not in text and got == ["c"]


def test_같은_진행_문구는_한번만_기록된다(logdir):
    """같은 진행 메시지가 반복돼도 로그에는 한 줄 (로그 폭주 방지)."""
    from app.qt import worker

    def fn(progress=None, cancel=None):
        for i in range(5):
            progress(service.ProgressEvent("erase", i, 5, "같은 문구"))

    worker.Job(fn).run()
    assert _read(logdir).count("같은 문구") == 1


def test_show_error가_기록된다(qapp, logdir):
    """오류 창을 보여 줄 때 제목·이유·자세히가 로그에 남고 키는 가려진다."""
    from app.qt.dialogs import show_error

    show_error(None, "추출 실패", "연결 안 됨", "다시 시도", detail="Traceback sk-ant-api03-DETAILKEYKEY")
    text = _read(logdir)
    assert "오류 창: 추출 실패 | 연결 안 됨 | 다시 시도" in text and "DETAILKEYKEY" not in text


def _fake_run(monkeypatch, rc=0, out="ok", err="", raise_=None):
    seen = {}

    def run(args, **kw):
        seen["args"], seen["kw"] = args, kw
        if raise_:
            raise raise_
        return subprocess.CompletedProcess(args, rc, out, err)

    monkeypatch.setattr(subprocess, "run", run)
    return seen


def test_claude_호출_성공이_기록되고_결과는_그대로(logdir, tmp_path, monkeypatch):
    """긴 지시문은 줄여 기록하고, 반환값(코드·stdout·stderr)은 바뀌지 않는다."""
    seen = _fake_run(monkeypatch, out="결과")
    r = _REAL_RUN(["claude", "-p", "x" * 500], tmp_path, 5, "입력")
    assert r == (0, "결과", "") and seen["kw"]["input"] == "입력"
    text = _read(logdir)
    assert "claude 실행: claude -p " in text and "(+340자)" in text and "x" * 200 not in text
    assert "claude 끝: 코드 0" in text and "stdout 끝" not in text


def test_claude_호출_실패는_끝부분과_함께_기록되고_키는_가려진다(logdir, tmp_path, monkeypatch):
    _fake_run(monkeypatch, rc=1, out="o" * 3000, err="auth sk-ant-api03-STDERRKEYKEY")
    assert _REAL_RUN(["claude"], tmp_path, 5)[0] == 1
    text = _read(logdir)
    assert "코드 1" in text and "stderr 끝: auth" in text and "STDERRKEYKEY" not in text
    assert text.count("o" * 1500) == 1 and "o" * 1501 not in text  # 끝 1500자만


def test_claude_호출이_예외로_끝나도_기록하고_다시_던진다(logdir, tmp_path, monkeypatch):
    _fake_run(monkeypatch, raise_=subprocess.TimeoutExpired("claude", 5))
    with pytest.raises(subprocess.TimeoutExpired):
        _REAL_RUN(["claude"], tmp_path, 5)
    assert "claude 실행 실패" in _read(logdir) and "TimeoutExpired" in _read(logdir)


def test_도움말_메뉴와_로그_저장(qapp, tmp_path, monkeypatch, logdir):
    """도움말 메뉴에 두 동작이 있고, 저장 창을 고르면 zip이 만들어진다. 취소하면 아무것도 안 만든다."""
    from app.qt.main import MainWindow

    monkeypatch.setattr("app.service.find_claude", lambda: None)
    win = MainWindow(root=tmp_path)
    menus = {a.text(): a.menu() for a in win.menuBar().actions()}
    assert "도움말" in menus
    assert [a.text() for a in menus["도움말"].actions()] == ["로그 묶어서 저장… (문제 보고용)", "로그 폴더 열기"]

    dest = tmp_path / "out" / "r.zip"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: ("", "")))
    win.save_logs()
    assert not dest.exists()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(dest), "")))
    win.save_logs()
    assert zipfile.is_zipfile(dest)


def test_로그_저장_실패는_오류_창으로(qapp, tmp_path, monkeypatch, logdir):
    """저장 위치에 쓸 수 없으면(OSError) 앱이 죽지 않고 오류 창을 보여 준다."""
    from app.qt import main as qmain

    shown = []
    monkeypatch.setattr(qmain, "show_error", lambda *a, **k: shown.append(a[1]))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(tmp_path), "")))  # 폴더에 쓰기
    win = qmain.MainWindow(root=tmp_path)
    win.save_logs()
    assert shown == ["로그를 저장하지 못했습니다"]


def test_로그_폴더_열기(qapp, tmp_path, monkeypatch, logdir):
    """폴더가 없어도 만들고 QDesktopServices로 연다 (실제로 탐색기는 열지 않는다)."""
    from PySide6.QtGui import QDesktopServices

    from app.qt.main import MainWindow

    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda u: opened.append(u.toLocalFile()) or True))
    MainWindow(root=tmp_path).open_log_folder()
    assert opened and Path(opened[0]) == logdir.parent
