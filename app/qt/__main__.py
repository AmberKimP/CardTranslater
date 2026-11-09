import multiprocessing
import sys

if __name__ == "__main__":  # 작업 프로세스(⑦·⑧ 동시 처리)가 이 파일을 다시 읽을 때는 화면 모듈도 읽지 않게
    multiprocessing.freeze_support()
    from app.qt.main import main

    sys.exit(main())
