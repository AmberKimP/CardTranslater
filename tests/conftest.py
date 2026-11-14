"""테스트가 실제 Claude Code·API를 부르지 않게 막는다 (비용·요금제 사용량 방지)."""

import hashlib
import os
from pathlib import Path

import pytest

# 이 PC 사용자의 실제 파일: 테스트가 끝난 뒤 바뀌었으면 실패시킨다 (예전에 last_project가 pytest 임시 폴더로 바뀌었음)
_REAL_USER_FILES = [Path(os.environ.get("APPDATA") or Path.home() / ".cardtranslater") / "CardTranslater" / n
                    for n in ("settings.json", "api_key.bin")]


def _digest(p: Path) -> str | None:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def pytest_sessionstart(session):
    session.config._real_user_files = {p: _digest(p) for p in _REAL_USER_FILES}


def pytest_sessionfinish(session, exitstatus):
    before = getattr(session.config, "_real_user_files", {})
    changed = [str(p) for p, d in before.items() if _digest(p) != d]
    if changed:
        session.exitstatus = 1
        print("\n[conftest] 테스트가 실제 사용자 파일을 바꿨습니다: " + ", ".join(changed))


@pytest.fixture(autouse=True)
def _isolated_user_files(monkeypatch, tmp_path_factory):
    """설정(settings.json)·API 키·로그를 임시 폴더로: 테스트가 사용자의 실제 설정을 바꾸지 않게."""
    d = tmp_path_factory.mktemp("userfiles")
    monkeypatch.setenv("CARDTRANSLATER_SETTINGS", str(d / "settings.json"))
    monkeypatch.setenv("CARDTRANSLATER_LOG_DIR", str(d / "logs"))


@pytest.fixture(autouse=True)
def _no_installed_font_pack(monkeypatch, tmp_path_factory):
    """이 PC에 설치된 글꼴 묶음(%LOCALAPPDATA%)을 테스트가 보지 않게 (빈 폴더)."""
    monkeypatch.setenv("CARDTRANSLATER_FONT_PACK", str(tmp_path_factory.mktemp("fontpack")))


@pytest.fixture(autouse=True)
def _single_process(monkeypatch):
    """⑦·⑧ 동시 처리는 그것을 보는 테스트만 켠다 (프로세스를 띄우면 느리다)."""
    monkeypatch.setenv("CARDTRANSLATER_WORKERS", "1")
    monkeypatch.setenv("CARDTRANSLATER_EXTRACT_WORKERS", "1")  # ① 동시 요청은 그것을 보는 테스트만 (요청 순서를 정해 두려고)


@pytest.fixture(autouse=True)
def _no_background_erase(monkeypatch):
    """프로젝트 화면을 여는 테스트가 뒤에서 글자 지우기를 시작하지 않게 (그것을 보는 테스트만 켠다)."""
    monkeypatch.setenv("CARDTRANSLATER_NO_BG_ERASE", "1")


@pytest.fixture(autouse=True)
def _no_real_claude(monkeypatch):
    def refuse(*a, **k):
        raise RuntimeError("테스트에서 실제 claude 명령을 실행하려 했습니다. 가짜 runner를 넣으세요")

    monkeypatch.setattr("app.extract_cc._run_subprocess", refuse)
