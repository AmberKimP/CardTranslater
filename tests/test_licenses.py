"""라이브러리 라이선스 지킴이: 새 런타임 라이브러리나 라이선스가 바뀐 업그레이드는 확인하기 전에는 통과하지 않는다."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packaging"))

import license_check  # noqa: E402


def test_runtime_libraries_have_approved_licenses():
    problems = license_check.check()
    assert not problems, "라이선스를 확인하세요:\n" + "\n".join(problems)


def test_app_does_not_import_unlicensed_pymupdf_extras():
    """라이선스 표기가 없는 PyMuPDF 부속(pymupdf_layout·pymupdf4llm)을 앱이 쓰면 설치 파일에 들어간다: 막는다."""
    names = "|".join(license_check.NOT_BUNDLED.values())
    pat = re.compile(rf"^\s*(import|from)\s+({names})\b|import_module\(['\"]({names})", re.M)
    used = [str(p.relative_to(ROOT)) for p in (ROOT / "app").rglob("*.py") if pat.search(p.read_text(encoding="utf-8"))]
    assert not used, f"라이선스 확인 전에는 쓰지 마세요: {used}"


def test_requirements_runtime_roots_match_build():
    """requirements.txt의 실행용 패키지가 build.RUNTIME_ROOTS(라이선스 모으기·점검 대상)에 모두 있다."""
    from build import RUNTIME_ROOTS, _norm

    reqs = [_norm(re.split(r"[=<>~!\[; ]", ln.strip())[0]) for ln in (ROOT / "requirements.txt").read_text(
        encoding="utf-8").splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    dev_only = {"pytest", "pyinstaller"}
    missing = [r for r in reqs if r not in dev_only and r not in {_norm(x) for x in RUNTIME_ROOTS}]
    assert not missing, f"requirements.txt에 새 실행용 패키지: {missing} — build.RUNTIME_ROOTS에 넣고 라이선스를 확인하세요"
