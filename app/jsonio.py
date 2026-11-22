"""JSON 파일을 안전하게 쓰기 (쓰다가 멈춰도 반쪽짜리 파일이 남지 않게 임시 파일에 쓰고 바꿔 넣는다)."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


def write_json(p: Path | str, data, *, mkdir: bool = True) -> None:
    """data를 p에 JSON(UTF-8, 들여쓰기 2)으로 쓴다. mkdir이면 폴더가 없을 때 만든다 (False면 폴더가 지워졌을 때
    오류: 뒤에서 도는 작업이 사용자가 지운 그룹·파일 폴더를 다시 만들지 않게)."""
    p = Path(p)
    if mkdir:
        p.parent.mkdir(parents=True, exist_ok=True)
    # 임시 파일 이름은 쓰는 스레드마다 다르게 (두 스레드가 같은 임시 파일을 섞어 쓰지 않게)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    for i in range(20):  # Windows: 다른 스레드(뒤에서 글자 지우기 등)가 마침 읽는 중이면 바꿔 쓰기를 거절한다
        try:
            tmp.replace(p)
            return
        except PermissionError:
            if i == 19:
                tmp.unlink(missing_ok=True)  # 임시 파일 이름이 스레드마다 달라 남기면 쌓인다
                raise
            time.sleep(0.05)
