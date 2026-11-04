"""테스트가 실제 Claude Code·API를 부르지 않게 막는다 (비용·요금제 사용량 방지)."""

import pytest


@pytest.fixture(autouse=True)
def _no_real_claude(monkeypatch):
    def refuse(*a, **k):
        raise RuntimeError("테스트에서 실제 claude 명령을 실행하려 했습니다. 가짜 runner를 넣으세요")

    monkeypatch.setattr("app.extract_cc._run_subprocess", refuse)
