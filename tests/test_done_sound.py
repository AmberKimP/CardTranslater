"""5초 넘게 걸린 작업이 끝나면 소리 (설정·작업 스레드·설정 창). 실제 소리는 내지 않는다."""

from __future__ import annotations

import pytest

from app import service, settings


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CARDTRANSLATER_SETTINGS", str(tmp_path / "settings.json"))


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_done_sound_setting_round_trip(tmp_path):
    assert settings.load().done_sound_on is True and settings.load().done_sound == ""
    snd = tmp_path / "jobs.mp3"
    snd.write_bytes(b"x")
    service.save_done_sound(True, str(snd))
    assert service.done_sound_file() == snd
    service.save_done_sound(True, str(tmp_path / "없음.mp3"))  # 파일이 없으면 Windows 기본 소리
    assert service.done_sound_file() == service.default_done_sound()
    service.save_done_sound(False, str(snd))
    assert service.done_sound_file() is None


def test_sound_only_after_long_successful_job(qapp, monkeypatch):
    from app.qt import sound, worker

    played = []
    monkeypatch.setattr(sound, "play_done", lambda: played.append(1) or True)
    worker._done_sound(service.DONE_SOUND_MIN_S - 0.1)
    assert played == []
    worker._done_sound(service.DONE_SOUND_MIN_S + 0.1)
    assert played == [1]


def test_job_done_signal_triggers_sound_check(qapp, monkeypatch):
    from PySide6.QtCore import QThreadPool

    from app.qt import worker

    seen = []
    monkeypatch.setattr(worker, "_done_sound", lambda elapsed: seen.append(elapsed))
    job = worker.start(worker.Job(lambda: 1, with_progress=False))
    QThreadPool.globalInstance().waitForDone(3000)
    qapp.processEvents()
    assert len(seen) == 1 and seen[0] >= 0
    fail = worker.start(worker.Job(lambda: 1 / 0, with_progress=False))  # 실패는 소리 없음
    QThreadPool.globalInstance().waitForDone(3000)
    qapp.processEvents()
    assert len(seen) == 1 and job and fail


def test_play_done_silent_in_tests():
    from app.qt import sound

    assert sound.play_done() is False


def test_settings_dialog_saves_sound(qapp, tmp_path, monkeypatch):
    from app.qt.dialogs import SettingsDialog

    snd = tmp_path / "jobs.mp3"
    snd.write_bytes(b"x")
    dlg = SettingsDialog()
    assert dlg.sound_on.isChecked() and dlg.b_listen.isEnabled()
    dlg.sound_on.setChecked(False)
    assert not dlg.sound_path.isEnabled()
    dlg.sound_on.setChecked(True)
    dlg.sound_path.setText(str(snd))
    monkeypatch.setattr(service, "save_settings", lambda *a, **k: None)  # 데이터 폴더는 건드리지 않음
    dlg.accept()
    s = settings.load()
    assert s.done_sound_on and s.done_sound == str(snd)
