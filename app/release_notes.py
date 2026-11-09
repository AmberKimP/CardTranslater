"""릴리스 노트(CHANGELOG.md)를 사용자에게 보여 줄 글로. 화면 라이브러리를 쓰지 않는다 (빌드 스크립트도 쓴다)."""

from __future__ import annotations

from pathlib import Path

FILE_NAME = "CHANGELOG.md"
TITLE = "# CardTranslater 릴리스 노트"


def user_notes(text: str) -> str:
    """CHANGELOG 글에서 맨 위 개발용 안내(버전 규칙·태그)를 빼고, 첫 버전 제목(‘## ’로 시작하는 줄)부터."""
    lines = text.splitlines()
    first = next((i for i, ln in enumerate(lines) if ln.startswith("## ")), None)
    if first is None:
        return ""
    return "\n".join(lines[first:]).strip() + "\n"


def load(path: Path | None = None) -> str:
    """앱과 함께 묶은 CHANGELOG.md를 읽어 사용자용 글로. 없거나 읽지 못하면 ''."""
    if path is None:
        from app.settings import resource_root

        path = resource_root() / FILE_NAME
    try:
        return user_notes(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return ""


def license_text(root: Path | None = None) -> str:
    """도움말 > 라이선스에 보일 글: 이 프로그램의 라이선스(LICENSE) + 제3자 구성 요소 고지. 없으면 ''."""
    if root is None:
        from app.settings import resource_root

        root = resource_root()
    parts = []
    try:
        parts.append("# CardTranslater 라이선스\n\n```\n" + (Path(root) / "LICENSE").read_text(encoding="utf-8").strip()
                     + "\n```\n")
    except (OSError, UnicodeDecodeError):
        return ""
    try:
        parts.append((Path(root) / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        pass
    return "\n".join(parts)


def install_copy(changelog: Path) -> str:
    """설치 폴더에 넣을 ‘릴리스 노트.md’ 글 (앱 화면과 같은 내용)."""
    return f"{TITLE}\n\n" + user_notes(Path(changelog).read_text(encoding="utf-8"))
