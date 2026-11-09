"""테스트가 실제 Claude Code·API를 부르지 않게 막는다 (비용·요금제 사용량 방지)."""

import pytest


@pytest.fixture(autouse=True)
def _no_installed_font_pack(monkeypatch, tmp_path_factory):
    """이 PC에 설치된 글꼴 묶음(%LOCALAPPDATA%)을 테스트가 보지 않게 (빈 폴더)."""
    monkeypatch.setenv("CARDTRANSLATER_FONT_PACK", str(tmp_path_factory.mktemp("fontpack")))


@pytest.fixture(autouse=True)
def _single_process(monkeypatch):
    """⑦·⑧ 동시 처리는 그것을 보는 테스트만 켠다 (프로세스를 띄우면 느리다)."""
    monkeypatch.setenv("CARDTRANSLATER_WORKERS", "1")


@pytest.fixture(autouse=True)
def _no_background_erase(monkeypatch):
    """프로젝트 화면을 여는 테스트가 뒤에서 글자 지우기를 시작하지 않게 (그것을 보는 테스트만 켠다)."""
    monkeypatch.setenv("CARDTRANSLATER_NO_BG_ERASE", "1")


@pytest.fixture(autouse=True)
def _no_real_claude(monkeypatch):
    def refuse(*a, **k):
        raise RuntimeError("테스트에서 실제 claude 명령을 실행하려 했습니다. 가짜 runner를 넣으세요")

    monkeypatch.setattr("app.extract_cc._run_subprocess", refuse)
