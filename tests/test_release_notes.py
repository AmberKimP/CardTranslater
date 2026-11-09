"""릴리스 노트 글 (app.release_notes): 개발용 안내 빼기, 읽기 실패에 견디기, 설치 폴더 사본."""

from __future__ import annotations

from pathlib import Path

from app import release_notes as rn

ROOT = Path(__file__).resolve().parent.parent


def test_user_notes_starts_at_first_version_heading():
    text = "# 릴리스 노트\n\n버전은 app/version.py ...\n\n---\n\n## 1.2.0 — 날짜\n\n- 새 기능\n\n## 1.1.0\n- 예전\n"
    out = rn.user_notes(text)
    assert out.startswith("## 1.2.0") and "app/version.py" not in out and "## 1.1.0" in out
    assert rn.user_notes("## 1.0.0\n- 처음\n").startswith("## 1.0.0")  # 맨 앞이 바로 버전 제목이어도
    assert rn.user_notes("# 제목만\n안내\n") == ""
    # 안내 속 ‘### ’ 소제목은 버전 제목이 아니다
    assert rn.user_notes("# 노트\n### 규칙\n- x\n## 2.0.0\n- y\n").startswith("## 2.0.0")


def test_load_survives_missing_or_unreadable_file(tmp_path):
    assert rn.load(tmp_path / "없음.md") == ""
    bad = tmp_path / "CHANGELOG.md"
    bad.write_bytes("## 1.0.0\n- 한글".encode("cp949"))  # UTF-8이 아님
    assert rn.load(bad) == ""
    bad.write_text("# 노트\n안내\n## 1.0.0\n- 한글\n", encoding="utf-8")
    assert rn.load(bad) == "## 1.0.0\n- 한글\n"


def test_install_copy_matches_app_text(tmp_path):
    f = tmp_path / "CHANGELOG.md"
    f.write_text("# 노트\n버전 규칙·태그 안내\n---\n## 1.0.0\n- 기능\n", encoding="utf-8")
    out = rn.install_copy(f)
    assert out.startswith(rn.TITLE) and "태그 안내" not in out and out.endswith(rn.load(f))


def test_license_text_has_beerware_agpl_and_source_link():
    """도움말 > 라이선스: Beerware(AmberKimP) + 제3자 고지(AGPL 소스 받는 곳)."""
    from app import service

    t = service.license_text()
    assert "BEER-WARE" in t and "AmberKimP" in t
    assert "AGPL-3.0" in t and "https://github.com/AmberKimP/CardTranslater" in t and "LGPL-3.0" in t
    for name in ("agpl-3.0.txt", "lgpl-3.0.txt", "gpl-3.0.txt", "lgpl-2.1.txt", "apache-2.0.txt"):
        assert (ROOT / "licenses" / name).stat().st_size > 5000, name


def test_build_copies_licenses_for_bundled_packages(tmp_path):
    import sys

    sys.path.insert(0, str(ROOT / "packaging"))
    from build import copy_licenses

    out = tmp_path / "CardTranslater"
    (out / "_internal" / "pymupdf").mkdir(parents=True)  # 설치 파일에 들어간 패키지 흉내
    (out / "_internal" / "numpy").mkdir()
    n = copy_licenses(out)
    assert (out / "LICENSE").exists() and (out / "THIRD_PARTY_NOTICES.md").exists()
    assert (out / "licenses" / "agpl-3.0.txt").exists() and n >= 2
    pk = {p.name.lower() for p in (out / "licenses" / "packages").iterdir()}
    assert {"pymupdf", "numpy", "python", "openpyxl", "anthropic"} <= pk and "pytest" not in pk  # 실행에 쓰는 것만
