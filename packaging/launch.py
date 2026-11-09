"""exe 시작점 (PyInstaller). 개발 중에는 python -m app.qt 를 쓴다."""

import multiprocessing
import sys

if __name__ == "__main__":
    # ⑦·⑧ 동시 처리의 작업 프로세스로 띄워졌으면 여기서 작업만 하고 끝난다 (창을 띄우지 않음)
    multiprocessing.freeze_support()
    from app.qt.main import main

    sys.exit(main())
