"""런타임 라이브러리 라이선스 점검 (새 라이브러리·버전 업그레이드 때 라이선스를 놓치지 않게).

    .venv/Scripts/python packaging/license_check.py      # 표로 보기, 승인 목록과 다르면 종료 코드 1

tests/test_licenses.py가 같은 점검을 한다: 앱이 실행할 때 쓰는 패키지(build.RUNTIME_ROOTS와 그 의존성)가
APPROVED에 없거나, 설치된 버전의 라이선스 표기가 APPROVED와 다르면 테스트가 실패한다.

새 라이브러리를 넣거나 올리면:
1. 그 라이선스가 배포(설치 파일)와 맞는지 확인한다 — MIT·BSD·Apache·PSF·ISC·Zlib 등은 고지문만 넣으면 된다.
   LGPL은 따로 바꿔 끼울 수 있게(별도 DLL·폴더 배포), GPL·AGPL은 설치 파일 전체가 그 조건이 된다 (지금은 PyMuPDF로 이미 AGPL).
   상업적 사용 금지(NonCommercial)·라이선스 표기 없음·독점 라이선스는 넣지 않는다 (넣어야 하면 사용자에게 먼저 묻는다).
2. THIRD_PARTY_NOTICES.md 표에 넣는다 (설치 파일에 들어가면).
3. 아래 APPROVED에 이름과 라이선스 표기를 적는다.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packaging"))

# 이름(정규화) -> 설치된 패키지의 라이선스 표기 (license_of()가 돌려주는 글 그대로)
APPROVED: dict[str, str] = {
    "annotated-types": "MIT",
    "anthropic": "Apache-2.0",
    "anyio": "MIT",
    "docstring-parser": "MIT",
    "et-xmlfile": "MIT",
    "flatbuffers": "Apache 2.0",
    "fonttools": "MIT",
    "h11": "MIT",
    "httpcore2": "BSD-3-Clause",
    "httpx2": "BSD-3-Clause",
    "idna": "BSD-3-Clause",
    "jiter": "MIT",
    "networkx": "BSD-3-Clause",
    "numpy": "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0",
    "onnxruntime": "MIT License",
    "opencv-python-headless": "Apache 2.0",
    "openpyxl": "MIT",
    "opentelemetry-api": "Apache-2.0",
    "packaging": "Apache-2.0 OR BSD-2-Clause",
    "pillow": "MIT-CMU",
    "protobuf": "3-Clause BSD License",
    "psutil": "BSD-3-Clause",
    "pydantic": "MIT",
    "pydantic-core": "MIT",
    "pymupdf": "Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License",  # 설치 파일 전체가 AGPL
    # PyMuPDF 2.x가 함께 설치하지만 앱이 import하지 않아 설치 파일에 들어가지 않는다 (라이선스 표기 없음).
    # import하게 되면(설치 파일에 들어가면) 먼저 라이선스를 확인해야 한다 — tests/test_licenses.py가 막는다.
    "pymupdf-layout": "(표기 없음 · 설치 파일에 넣지 않음)",
    "pymupdf4llm": "(표기 없음 · 설치 파일에 넣지 않음)",
    "pyside6": "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only",
    "pyside6-addons": "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only",
    "pyside6-essentials": "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only",
    "pyside6-pdf": "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only",
    "pyside6-webengine": "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only",
    "pyyaml": "MIT",
    "shiboken6": "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only",
    "sniffio": "MIT OR Apache-2.0",
    "tabulate": "MIT",
    "truststore": "MIT",
    "typing-extensions": "PSF-2.0",
    "typing-inspection": "MIT",
}
NOT_BUNDLED = {"pymupdf-layout": "pymupdf_layout", "pymupdf4llm": "pymupdf4llm"}  # 이름 -> import 이름 (앱이 쓰면 안 됨)


def license_of(dist) -> str:
    """패키지의 라이선스 표기: License-Expression → License 첫 줄(짧을 때) → 분류(Classifier). 없으면 ''."""
    md = dist.metadata
    lic = (md.get("License-Expression") or "").strip()
    if not lic:
        raw = (md.get("License") or "").strip()
        lic = raw.splitlines()[0].strip() if raw and len(raw) < 200 else ""
    if not lic:
        cls = [c.split("::")[-1].strip() for c in (md.get_all("Classifier") or []) if c.startswith("License ::")]
        lic = "; ".join(cls)
    return lic


def check() -> list[str]:
    """승인 목록과 다른 것: ["이름: 설명", ...] (비면 통과)."""
    from importlib.metadata import distribution

    from build import _norm, runtime_distributions

    problems = []
    for name in sorted(runtime_distributions()):
        lic = license_of(distribution(name))
        want = APPROVED.get(_norm(name))
        if want is None:
            problems.append(f"{name}: 새 라이브러리 (라이선스 ‘{lic or '표기 없음'}’) — 확인 후 THIRD_PARTY_NOTICES.md와 "
                            "packaging/license_check.py APPROVED에 넣으세요")
        elif _norm(name) in NOT_BUNDLED:
            if lic:
                problems.append(f"{name}: 라이선스 표기가 생김 ‘{lic}’ — 확인 후 APPROVED를 고치세요")
        elif lic != want:
            problems.append(f"{name}: 라이선스가 바뀜 ‘{want}’ → ‘{lic}’ — 확인 후 APPROVED·THIRD_PARTY_NOTICES.md를 고치세요")
    return problems


if __name__ == "__main__":
    from importlib.metadata import distribution

    from build import runtime_distributions

    for n in sorted(runtime_distributions()):
        print(f"{n:26s} {license_of(distribution(n))}")
    errs = check()
    print("\n".join(["", "문제:"] + errs) if errs else "\n승인 목록과 같음")
    sys.exit(1 if errs else 0)
