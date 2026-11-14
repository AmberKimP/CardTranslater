"""설정·로그인 방식 상태·완료 소리·데이터 폴더."""

from __future__ import annotations

from pathlib import Path

from app.claude_setup import find_claude


def engine_status() -> dict:
    """쓸 수 있는 엔진: {"claude-code": bool, "api": bool, "message"}. 키 값은 돌려주지 않는다."""
    from app.extract import api_key_source

    cc = bool(find_claude())
    api = bool(api_key_source()[0])
    eng = get_settings().engine
    if eng == "api":
        msg = "API 키로 실행 (사용한 만큼 요금)" if api else "API 키가 없습니다 — 파일 > 로그인 방식에서 키를 넣으세요"
    else:
        msg = ("Claude 구독으로 실행 (Claude Code)" if cc else
               "Claude Code가 없습니다 — 파일 > 로그인 방식에서 ‘Claude Code 설치’를 누르거나 API 키 방식을 쓰세요")
    return {"claude-code": cc, "api": api, "message": msg}


def get_settings():
    """앱 설정 (app.settings.Settings)."""
    from app import settings

    return settings.load()


def save_settings(data_dir: str | Path, engine: str = "claude-code") -> Path:
    from app import settings

    s = settings.load()
    s.data_dir, s.engine = str(data_dir), engine
    return settings.save(s)


DONE_SOUND_MIN_S = 5.0  # 이만큼 넘게 걸린 작업이 끝나면 소리를 낸다


def save_done_sound(enabled: bool, path: str = "") -> Path:
    """작업 끝 소리 설정 (path가 비면 Windows 기본 알림 소리)."""
    from app import settings

    s = settings.load()
    s.done_sound_on, s.done_sound = bool(enabled), str(path or "")
    return settings.save(s)


def done_sound_file() -> Path | None:
    """작업이 끝날 때 낼 소리 파일. 꺼 두었으면 None. 고른 파일이 없으면 Windows 기본 알림 소리."""
    s = get_settings()
    if not s.done_sound_on:
        return None
    if s.done_sound and Path(s.done_sound).exists():
        return Path(s.done_sound)
    return default_done_sound()


def default_done_sound() -> Path | None:
    """Windows 기본 알림 소리 파일 (없으면 None)."""
    import os

    win = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Media"
    for name in ("Windows Notify System Generic.wav", "Windows Notify.wav", "tada.wav"):
        if (win / name).exists():
            return win / name
    return None


def data_dir() -> Path:
    """games/·projects/가 들어가는 데이터 폴더."""
    from app import settings

    return settings.data_dir()
