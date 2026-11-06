"""동작 로그: 테스트하는 사람이 문제를 보내 줄 수 있게 파일로 남긴다.

위치: %LOCALAPPDATA%/CardTranslater/logs/ (환경변수 CARDTRANSLATER_LOG_DIR로 바꿀 수 있다)
- cardtranslater.log      작업 시작·끝·실패, 진행 메시지, Claude 호출, 보여 준 오류 창 (2MB × 5개 돌려 씀)
- crash.log               파이썬이 통째로 죽을 때(faulthandler)의 스택
API 키(sk-ant-…)는 기록하기 전에 가린다. 화면 코드 없음: 화면은 service를 통해 부른다.
"""

from __future__ import annotations

import faulthandler
import json
import logging
import logging.handlers
import os
import platform
import re
import sys
import threading
import time
import zipfile
from pathlib import Path

LOG_NAME = "cardtranslater.log"
_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{4,}")
_state: dict = {}

log = logging.getLogger("cardtranslater")


def log_dir() -> Path:
    env = os.environ.get("CARDTRANSLATER_LOG_DIR")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "CardTranslater" / "logs"


def mask(text: str) -> str:
    return _KEY_RE.sub(lambda m: m.group(0)[:7] + "…(가림)", text)


class _MaskFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        record.msg, record.args = mask(msg), None
        if record.exc_text:
            record.exc_text = mask(record.exc_text)
        return True


def build_info() -> dict:
    """빌드할 때 넣은 정보(build_info.json: 버전·커밋·시각). 개발 중에는 {"version": "dev"}."""
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    p = root / "build_info.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": "dev"}


def setup(directory: Path | None = None) -> Path:
    """로그 파일을 연다 (여러 번 불러도 한 번만). 로그 파일 경로를 돌려준다."""
    if _state.get("file"):
        return _state["file"]
    d = Path(directory) if directory else log_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / LOG_NAME
    h = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"))
    h.addFilter(_MaskFilter())
    log.setLevel(logging.DEBUG)
    log.addHandler(h)
    log.propagate = False
    _state.update(file=path, handler=h)

    try:  # 통째로 죽을 때(네이티브 오류)도 남게
        crash = open(d / "crash.log", "a", encoding="utf-8")  # noqa: SIM115 - 끝날 때까지 열어 둔다
        crash.write(f"\n=== 시작 {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        crash.flush()
        faulthandler.enable(crash)
        _state["crash"] = crash
    except (OSError, RuntimeError, ValueError):
        pass

    def excepthook(t, v, tb):
        log.critical("처리되지 않은 예외", exc_info=(t, v, tb))
        sys.__excepthook__(t, v, tb)

    def thread_hook(args):
        log.critical("작업 스레드에서 처리되지 않은 예외 (%s)", getattr(args.thread, "name", "?"),
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_hook
    return path


def log_startup(extra: dict | None = None) -> None:
    info = {"build": build_info(), "windows": platform.platform(), "python": sys.version.split()[0],
            "frozen": bool(getattr(sys, "frozen", False)), "exe": sys.executable}
    info.update(extra or {})
    log.info("=== CardTranslater 시작 === %s", json.dumps(info, ensure_ascii=False, default=str))


def export(dest: Path, extra: dict | None = None) -> Path:
    """로그 파일들과 환경 정보(system.json)를 zip 하나로 묶는다. 묶은 파일 경로를 돌려준다."""
    d = Path(_state["file"]).parent if _state.get("file") else log_dir()
    h = _state.get("handler")
    if h:
        h.flush()
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    info = {"build": build_info(), "windows": platform.platform(), "python": sys.version.split()[0],
            "exported": time.strftime("%Y-%m-%d %H:%M:%S")}
    info.update(extra or {})
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(d.glob("*.log*")) if d.exists() else []:
            z.writestr(f.name, mask(f.read_text(encoding="utf-8", errors="replace")))
        z.writestr("system.json", mask(json.dumps(info, ensure_ascii=False, indent=2, default=str)))
    log.info("로그를 묶었습니다: %s", dest)
    return dest


def shorten(args: list, limit: int = 160) -> str:
    """명령줄을 기록용으로 줄인다 (긴 지시문은 앞부분만)."""
    out = []
    for a in args:
        s = str(a)
        out.append(s if len(s) <= limit else s[:limit] + f"…(+{len(s) - limit}자)")
    return " ".join(out)
