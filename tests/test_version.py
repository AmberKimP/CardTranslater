"""앱 버전: 한 곳(app/version.py)의 값을 엑셀·창 제목·설치 파일이 쓴다."""

import re
from pathlib import Path

from app import excel_io
from app.version import VERSION

ROOT = Path(__file__).resolve().parent.parent


def test_version_is_semver_and_used_everywhere():
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSION)
    assert excel_io.APP_VERSION == VERSION
    assert f"## {VERSION}" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    iss = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
    assert "OutputBaseFilename=CardTranslater-Setup-{#SetupName}" in iss
    assert "VERSION" in (ROOT / "packaging" / "make_installer.py").read_text(encoding="utf-8")
