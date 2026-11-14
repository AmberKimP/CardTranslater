"""Claude Code PostToolUse 훅: 바뀐 .py 파일을 바로 검사한다 (되풀이된 실수를 막으려고).

1. 문법 오류: Bash heredoc 속 파이썬이 문자열 안의 "\\n"을 진짜 줄바꿈으로 바꿔 깨진 파일 (11-13, 11-14 되풀이).
2. 같은 이름의 최상위 def/class 두 번: 잘라 붙이기 편집이 블록을 복제해 뒤의 정의가 새 코드를 덮음 (11-13 images.py).

문제가 있으면 종료 코드 2와 stderr로 알린다 (Claude가 바로 보고 고친다). 문제 없으면 아무것도 출력하지 않는다.
검사 대상: git이 바뀌었다고 보는(수정·새) app/·tests/·packaging/·.claude/hooks/ 의 .py 파일.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIRS = ("app/", "tests/", "packaging/", ".claude/hooks/")


def changed_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "-c", "core.quotepath=off", "status", "--porcelain", "-uall", "--", *DIRS], cwd=ROOT,
                             capture_output=True, text=True, encoding="utf-8", timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    files = []
    for line in out.splitlines():
        path = line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path.endswith(".py") and line[:2].strip() != "D":
            p = ROOT / path
            if p.is_file():
                files.append(p)
    return files


def problems(p: Path) -> list[str]:
    try:
        src = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return [f"{p.relative_to(ROOT)}: 읽지 못함 ({e})"]
    try:
        tree = ast.parse(src, filename=str(p))
    except SyntaxError as e:
        return [f"{p.relative_to(ROOT)}:{e.lineno}: 문법 오류 - {e.msg}"
                " (heredoc이 문자열 속 \\n을 줄바꿈으로 바꿨는지 확인; 문자열에 \\n이 있으면 Edit/Write 도구로)"]
    seen: dict[str, int] = {}
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in seen and not node.decorator_list:  # @overload·@property.setter 등은 뺀다
                out.append(f"{p.relative_to(ROOT)}:{node.lineno}: 최상위 '{node.name}'가 {seen[node.name]}줄에도 있음"
                           " (잘라 붙이기 편집이 블록을 복제했는지 확인)")
            seen.setdefault(node.name, node.lineno)
    return out


def main() -> int:
    sys.stderr.reconfigure(encoding="utf-8")  # Windows 콘솔 기본(cp949)이면 한글이 깨져 전달된다
    found = [m for p in changed_files() for m in problems(p)]
    if found:
        print("파이썬 파일 검사에서 문제를 찾았습니다:\n" + "\n".join(found), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
