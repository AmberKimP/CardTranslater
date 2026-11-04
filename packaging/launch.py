"""exe 시작점 (PyInstaller). 개발 중에는 python -m app.qt 를 쓴다."""

import sys

from app.qt.main import main

if __name__ == "__main__":
    sys.exit(main())
