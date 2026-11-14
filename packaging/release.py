"""릴리스 준비: 검사 → 전체 테스트 → exe 빌드 → 설치 파일을 ‘하나씩 차례로’ 한다.

    .venv/Scripts/python packaging/release.py            # 전부
    .venv/Scripts/python packaging/release.py --check    # 검사만 (테스트·빌드 없이)

되풀이된 실수를 막으려고 만들었다 (.omc/notepads/autopilot/problems.md):
- 테스트와 빌드를 동시에 돌려 메모리 부족으로 둘 다 죽음 → 여기서는 항상 차례로.
- autopilot 상태가 남아 있으면 git push가 막힘 → 상태 파일이 있으면 먼저 알린다 (/oh-my-claudecode:cancel).
- CHANGELOG 설치 파일 이름의 <날짜>를 채우지 않고 커밋함 → 설치 파일을 만든 뒤 그 이름으로 채운다.
- 이미 태그가 있는 버전에 덧붙임 → 태그가 있으면 멈춘다.
- Samples/(사용자 스캔)가 커밋에 들어감 → 스테이지에 있으면 멈춘다.

커밋·태그·push는 하지 않는다 (사람이 확인한 뒤에).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def version() -> str:
    m = re.search(r'VERSION\s*=\s*"([^"]+)"', (ROOT / "app" / "version.py").read_text(encoding="utf-8"))
    return m.group(1) if m else ""


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8").stdout


def checks() -> list[str]:
    """멈춰야 할 문제 목록 (비면 통과)."""
    v, bad = version(), []
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if not v:
        bad.append("app/version.py에서 VERSION을 찾지 못함")
    elif f"## {v} " not in changelog:
        bad.append(f"CHANGELOG.md에 ‘## {v} — 날짜’ 항목이 없음")
    if v and git("tag", "--list", f"v{v}").strip():
        bad.append(f"태그 v{v}가 이미 있음: 버전을 올리세요 (태그 붙은 버전에는 덧붙이지 않는다)")
    if any(line[3:].startswith("Samples/") for line in git("status", "--porcelain").splitlines() if line[0] not in " ?"):
        bad.append("Samples/(사용자 스캔)가 스테이지에 있음: git restore --staged Samples")
    states = list((ROOT / ".omc" / "state").glob("**/autopilot-state.json"))
    if states:
        bad.append("autopilot 상태가 남아 있어 git push가 막힘: /oh-my-claudecode:cancel 먼저 ("
                   + ", ".join(str(p.relative_to(ROOT)) for p in states) + ")")
    return bad


def run(step: str, args: list[str]) -> None:
    print(f"\n== {step}: {' '.join(args)}", flush=True)
    r = subprocess.run(args, cwd=ROOT)
    if r.returncode:
        sys.exit(f"{step} 실패 (종료 코드 {r.returncode})")


def run_tests() -> None:
    """전체 테스트. 끝날 때 프로세스가 죽어도(종료 코드가 0~5 밖) 실패로 본다: 다 통과해도 Qt 객체를 잘못 다루면
    끝날 때 힙이 깨져 죽는다 (1.19.0까지 영역 지정 창의 parentItem() 호출이 그랬다 — tests/test_region_scene_exit.py)."""
    run("전체 테스트", [PY, "-m", "pytest", "-q"])


def fill_changelog_date() -> str | None:
    """dist/에 방금 만든 설치 파일 이름으로 CHANGELOG의 <날짜>를 채운다."""
    v = version()
    built = sorted((ROOT / "dist").glob(f"CardTranslater-Setup-{v}-*.exe"), key=lambda p: p.stat().st_mtime)
    if not built:
        return None
    p = ROOT / "CHANGELOG.md"
    s = p.read_text(encoding="utf-8")
    placeholder = f"CardTranslater-Setup-{v}-<날짜>.exe"
    if placeholder in s:
        p.write_text(s.replace(placeholder, built[-1].name), encoding="utf-8")
    return built[-1].name


def main(argv: list[str]) -> int:
    bad = checks()
    for b in bad:
        print("멈춤:", b)
    if bad:
        return 1
    print(f"검사 통과: {version()}")
    if "--check" in argv:
        return 0
    run_tests()  # 빌드와 동시에 돌리지 않는다 (메모리)
    run("exe 빌드", [PY, "packaging/build.py"])
    run("설치 파일", [PY, "packaging/make_installer.py"])
    name = fill_changelog_date()
    print(f"\n완료: {name or '(설치 파일을 찾지 못함)'} — 이제 커밋·태그 v{version()}·push")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
