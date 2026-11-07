"""앱 설정 (데이터 폴더·기본 엔진). 화면 라이브러리를 쓰지 않는다.

- 설정 파일: ``%APPDATA%/CardTranslater/settings.json`` (다른 OS는 ``~/.cardtranslater/settings.json``).
  ``CARDTRANSLATER_SETTINGS`` 환경 변수로 바꿀 수 있다 (테스트).
- 데이터 폴더(``games/``·``projects/``가 들어가는 곳): 설정에 있으면 그것, 없으면
  exe로 실행할 때는 ``문서/CardTranslater``, 개발 중에는 저장소 폴더.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENGINES = ("claude-code", "api")


@dataclass
class Settings:
    data_dir: str = ""
    engine: str = "claude-code"
    last_project: str = ""
    done_sound_on: bool = True  # 5초 넘게 걸린 작업이 끝나면 소리
    done_sound: str = ""  # 소리 파일 (mp3·wav). 비우면 Windows 기본 알림 소리


def settings_path() -> Path:
    env = os.environ.get("CARDTRANSLATER_SETTINGS")
    if env:
        return Path(env)
    base = os.environ.get("APPDATA")
    return (Path(base) / "CardTranslater" if base else Path.home() / ".cardtranslater") / "settings.json"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def default_data_dir() -> Path:
    if is_frozen():
        return Path.home() / "Documents" / "CardTranslater"
    return REPO_ROOT


def load() -> Settings:
    p = settings_path()
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return Settings(**{k: v for k, v in d.items() if k in Settings.__dataclass_fields__})
        except (OSError, ValueError, TypeError):
            pass
    return Settings()


def save(s: Settings) -> Path:
    if s.engine not in ENGINES:
        raise ValueError(f"모르는 엔진: {s.engine}")
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(s), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


def data_dir(s: Settings | None = None) -> Path:
    s = s or load()
    return Path(s.data_dir) if s.data_dir else default_data_dir()


def env_files() -> list[Path]:
    """API 키를 찾을 .env 후보: 데이터 폴더, 저장소(개발) 폴더. 키 값은 이 모듈에서 읽지 않는다."""
    out = []
    for p in (data_dir() / ".env", REPO_ROOT / ".env"):
        if p not in out:
            out.append(p)
    return out
