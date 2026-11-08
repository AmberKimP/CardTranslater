"""릴리스 노트 글 (app.release_notes): 개발용 안내 빼기, 읽기 실패에 견디기, 설치 폴더 사본."""

from __future__ import annotations

from app import release_notes as rn


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
