"""앱 글꼴 파일이 있는 곳: 프로그램에 들어 있는 기본 글꼴 + 따로 설치하는 글꼴 묶음.

프로그램 설치 파일에는 기본 글꼴(catalog.json에서 "bundled": true인 계열, 본고딕)만 넣는다. 나머지 글꼴은
글꼴 설치 파일(CardTranslater-Fonts-<묶음 버전>.exe)이 %LOCALAPPDATA%\\CardTranslater\\fontpack\\fonts 에 설치한다.
프로그램을 업데이트해도 글꼴 묶음은 그대로 남는다.

프로젝트에는 글꼴을 'fonts/<계열>/<파일>' 상대 경로로 적는다. resolve()가 프로그램 폴더를 먼저, 없으면 글꼴 묶음을
본다 (개발 중에는 저장소 fonts/에 모두 있다).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 개발: 저장소, exe: 묶음 폴더 (typeset.ROOT와 같다)
PACK_ENV = "CARDTRANSLATER_FONT_PACK"  # 글꼴 묶음 폴더를 바꿀 때 (테스트)


def bundled_dir() -> Path:
    return ROOT / "fonts"


def pack_dir() -> Path:
    """글꼴 설치 파일이 글꼴을 두는 곳."""
    if os.environ.get(PACK_ENV):
        return Path(os.environ[PACK_ENV])
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "CardTranslater" / "fontpack" / "fonts"


def resolve(file: str | Path, base_dir: str | Path | None = None) -> Path:
    """글꼴 경로를 실제 파일로. 절대 경로는 그대로, 'fonts/…'는 프로그램 폴더(base_dir) → 글꼴 묶음 순으로 찾는다.
    어디에도 없으면 프로그램 폴더 경로를 돌려준다 (없는 글꼴은 부르는 쪽이 알린다)."""
    p = Path(file)
    if p.is_absolute():
        return p
    first = Path(base_dir or ROOT) / p
    if first.exists() or not p.parts or p.parts[0] != "fonts":
        return first
    alt = pack_dir().joinpath(*p.parts[1:])
    return alt if alt.exists() else first


def family_dirs(dir_name: str) -> list[Path]:
    """한 계열의 글꼴 폴더 (있는 것만): 프로그램 폴더, 글꼴 묶음."""
    return [d for d in (bundled_dir() / dir_name, pack_dir() / dir_name) if d.is_dir()]


def _read(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def catalog() -> dict:
    """글꼴 목록: 프로그램의 catalog.json + 글꼴 묶음에만 있는 계열 (더 새 글꼴 묶음을 설치한 경우)."""
    cat = _read(bundled_dir() / "catalog.json")
    fams = list(cat.get("families", []))
    known = {f["dir"] for f in fams}
    fams += [f for f in _read(pack_dir() / "catalog.json").get("families", []) if f.get("dir") not in known]
    return {**cat, "families": fams}


def pack_version_installed() -> int | None:
    v = _read(pack_dir() / "pack.json").get("pack_version")
    return int(v) if isinstance(v, (int, float, str)) and str(v).isdigit() else None


def pack_status() -> dict:
    """글꼴 묶음 상태 {"wanted": 이 프로그램이 아는 묶음 버전, "installed": 설치된 버전|None,
    "missing": 목록에는 있는데 파일이 없는 글꼴 수, "ok": 빠진 글꼴이 없나}."""
    cat = catalog()
    missing = 0
    for fam in cat.get("families", []):
        for f in fam.get("source_files", []):
            if "[" in f:  # 가변 글꼴 원본: 굵기별로 잘라 둔 파일을 본다
                stem = f.split("[")[0]
                missing += sum(1 for w in (fam.get("static_weights") or {})
                               if not any((d / f"{stem}-{w}.ttf").exists() for d in family_dirs(fam["dir"])))
            elif not any((d / f).exists() for d in family_dirs(fam["dir"])):
                missing += 1
    return {"wanted": cat.get("pack_version"), "installed": pack_version_installed(), "missing": missing,
            "ok": missing == 0}
