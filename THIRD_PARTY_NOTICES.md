# 제3자 구성 요소 고지 (Third-Party Notices)

CardTranslater 자체 코드는 Beerware 라이선스입니다 (`LICENSE`).
Windows 설치 파일에는 아래의 제3자 구성 요소가 함께 들어 있으며, 각 구성 요소는 자기 라이선스를 따릅니다.
라이선스 전문은 설치 폴더의 `licenses\` 폴더에 있습니다 (패키지별 원문은 `licenses\packages\`).

The CardTranslater source code is licensed under the Beer-Ware License (`LICENSE`).
The Windows installer also contains the third-party components below, each under its own license.
Full license texts are in the `licenses\` folder of the install directory (per-package originals in `licenses\packages\`).

---

## 소스 코드 (Source code) — AGPL-3.0

설치 파일에 PyMuPDF / MuPDF (GNU Affero General Public License v3.0)가 들어 있으므로, 설치 파일 전체는
AGPL-3.0 조건으로 배포됩니다. 배포한 모든 버전의 전체 소스 코드는 아래에서 받을 수 있습니다.

Because the installer contains PyMuPDF / MuPDF (AGPL-3.0), the installer as a whole is distributed under the
terms of the AGPL-3.0. The complete corresponding source code of every released version is available at:

- https://github.com/AmberKimP/CardTranslater — 버전마다 태그 `v<버전>` (tag `v<version>`, e.g. `v1.6.4`)

---

## 설치 파일에 들어 있는 구성 요소 (Bundled components)

| 구성 요소 (Component) | 라이선스 (License) | 전문 (Text) | 출처 (Source) |
|---|---|---|---|
| PyMuPDF, MuPDF | AGPL-3.0 (or Artifex commercial) | `licenses/agpl-3.0.txt` | https://github.com/pymupdf/PyMuPDF |
| Qt 6, PySide6, Shiboken6 | LGPL-3.0 | `licenses/lgpl-3.0.txt`, `licenses/gpl-3.0.txt` | https://code.qt.io , https://download.qt.io/official_releases/QtForPython/ |
| OpenCV (opencv-python-headless) | Apache-2.0 | `licenses/apache-2.0.txt` | https://github.com/opencv/opencv-python |
| FFmpeg (OpenCV 안의 `opencv_videoio_ffmpeg*.dll`) | LGPL-2.1 | `licenses/lgpl-2.1.txt` | https://ffmpeg.org |
| Python 3.12 | PSF License | `licenses/packages/` | https://www.python.org |
| OpenSSL 3 | Apache-2.0 | `licenses/apache-2.0.txt` | https://www.openssl.org |
| libffi | MIT | — | https://github.com/libffi/libffi |
| Microsoft Visual C++ Runtime (`VCRUNTIME140*.dll`) | Microsoft 재배포 허용 (redistributable) | — | Microsoft |
| NumPy | BSD-3-Clause 등 | `licenses/packages/` | https://numpy.org |
| Pillow | MIT-CMU (HPND) | `licenses/packages/` | https://python-pillow.org |
| fontTools | MIT | `licenses/packages/` | https://github.com/fonttools/fonttools |
| openpyxl, et-xmlfile | MIT | `licenses/packages/` | https://foss.heptapod.net/openpyxl/openpyxl |
| Anthropic Python SDK | Apache-2.0 / MIT | `licenses/packages/` | https://github.com/anthropics/anthropic-sdk-python |
| httpx, httpcore, h11, anyio, idna, sniffio, pydantic, jiter, typing-extensions 등 SDK 의존성 | MIT / BSD / Apache-2.0 / PSF | `licenses/packages/` | PyPI |
| psutil, PyYAML, setuptools, opentelemetry-api, truststore | BSD / MIT / Apache-2.0 | `licenses/packages/` | PyPI |

`licenses/packages/`에는 빌드할 때 설치 파일에 실제로 들어간 Python 패키지의 라이선스 원문을 모아 둡니다.
`licenses/packages/` collects, at build time, the license files of the Python packages actually bundled.

### Qt / PySide6 (LGPL-3.0)

이 프로그램은 Qt와 PySide6를 바꾸지 않고 별도 파일(DLL·.pyd, 설치 폴더의 `_internal\PySide6`)로 씁니다.
같은 버전의 Qt·PySide6로 바꿔 끼워 실행할 수 있습니다. Qt의 소스는 https://code.qt.io 에서 받을 수 있습니다.

This program uses unmodified Qt and PySide6 as separate files (DLLs/.pyd in `_internal\PySide6`); you may
replace them with your own compatible build. Qt source code is available at https://code.qt.io .

---

## 글꼴 (Fonts) — SIL Open Font License 1.1

프로그램 설치 파일의 기본 글꼴(Noto Sans KR)과 글꼴 설치 파일의 한글 글꼴(36종)은 모두 SIL Open Font License 1.1
글꼴입니다 (Google Fonts). 각 글꼴 폴더에 라이선스 원문 `OFL.txt`가 함께 들어 있습니다.
Noto Sans KR, Noto Serif KR, Hahmlet은 가변 글꼴에서 굵기별 파일로 잘라 썼습니다 (예약된 글꼴 이름은 쓰지 않음).

The bundled Noto Sans KR and the 36 Korean font families in the separate font installer are licensed under the
SIL Open Font License 1.1 (from Google Fonts); each font folder includes its `OFL.txt`. Noto Sans KR, Noto Serif KR
and Hahmlet were instanced from variable fonts into static weights (no Reserved Font Names are used).

---

## 설치 파일에 들어 있지 않은 것 (Not bundled)

- **Claude Code** — Anthropic 공식 사이트에서 사용자가 직접 받습니다 (Anthropic 약관). / Downloaded by the user from Anthropic.
- **Node.js** — Claude Code 공식 설치가 안 될 때만 nodejs.org에서 받습니다 (MIT). / Downloaded only as a fallback.
- **번역하는 카드 PDF와 결과물** — 원작 저작권자의 권리입니다. / Card files and translated outputs belong to their rights holders.
