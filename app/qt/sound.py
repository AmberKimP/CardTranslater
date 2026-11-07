"""작업이 끝났을 때 소리 (Windows MCI: mp3·wav, 따로 설치할 것 없음). 화면 스레드에서 부른다.

소리를 못 내도 작업에는 영향이 없다 (조용히 넘어간다).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger("cardtranslater.sound")
_ALIAS = "cardtranslater_done"


def _mci(cmd: str) -> int:
    import ctypes

    return ctypes.windll.winmm.mciSendStringW(cmd, None, 0, None)


def play(path: Path | str | None) -> bool:
    """소리 파일을 재생한다 (기다리지 않음). 재생을 시작하면 True."""
    if not path or sys.platform != "win32" or not Path(path).exists():
        return False
    try:
        _mci(f"close {_ALIAS}")  # 앞 소리가 아직 열려 있으면 닫는다
        if _mci(f'open "{Path(path)}" type mpegvideo alias {_ALIAS}') != 0:
            return False
        return _mci(f"play {_ALIAS}") == 0
    except Exception:  # noqa: BLE001 - 소리는 덤이다
        log.debug("소리 재생 실패: %s", path, exc_info=True)
        return False


def play_done() -> bool:
    """설정한 ‘작업 끝’ 소리 (꺼 두었으면 내지 않는다)."""
    import os

    from app import service

    if os.environ.get("PYTEST_CURRENT_TEST"):  # 테스트 중에는 소리를 내지 않는다
        return False
    try:
        return play(service.done_sound_file())
    except Exception:  # noqa: BLE001
        return False
