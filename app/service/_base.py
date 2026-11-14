"""작업 흐름 공용: 진행률 알림·취소 토큰 (다른 하위 모듈이 불러오는 동안 바로 쓰이므로 의존하는 것이 없다)."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable


SCHEMA = "cardtranslater.erase-page/2"  # 2: 원본 이미지 유지 + patches


# ---------------------------------------------------------------- 진행률·취소
@dataclass
class ProgressEvent:
    stage: str  # "erase" | "verify" | "compose" | "render" | "done"
    done: int  # 끝난 수
    total: int
    message: str = ""
    page: int | None = None


ProgressFn = Callable[[ProgressEvent], None]


class Cancelled(Exception):
    """사용자가 취소했다. 작업 폴더는 지우고, 최종 폴더(cleared/·translated/)는 그대로 둔다."""


class CancelToken:
    """다른 스레드(화면)에서 cancel()하면 작업 스레드가 다음 확인 지점에서 멈춘다."""

    def __init__(self) -> None:
        self._ev = threading.Event()

    def cancel(self) -> None:
        self._ev.set()

    @property
    def cancelled(self) -> bool:
        return self._ev.is_set()

    def check(self) -> None:
        if self._ev.is_set():
            raise Cancelled()


def _noop(_: ProgressEvent) -> None:
    pass
