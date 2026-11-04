"""API 키 보관 (화면 라이브러리를 쓰지 않는다).

- Windows: DPAPI(CryptProtectData)로 이 컴퓨터의 이 사용자만 풀 수 있게 암호화해
  ``%APPDATA%/CardTranslater/api_key.bin``에 둔다. 소스 코드·데이터 폴더·.env에는 남지 않는다.
- 다른 OS: 같은 자리에 사용자만 읽을 수 있는 파일(0600)로 둔다.
- 키 값은 로그·화면에 그대로 내보내지 않는다 (``masked``로 앞뒤 몇 글자만).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ENTROPY = b"CardTranslater API key v1"


def key_path() -> Path:
    from app.settings import settings_path

    return settings_path().parent / "api_key.bin"


# ---------------------------------------------------------------- Windows DPAPI
def _blob(data: bytes):
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf, DATA_BLOB


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    inp, _keep, DATA_BLOB = _blob(data)
    ent, _keep2, _ = _blob(ENTROPY)
    out = DATA_BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    args = (ctypes.byref(inp), None, ctypes.byref(ent), None, None, 0x01, ctypes.byref(out)) if protect else (
        ctypes.byref(inp), None, ctypes.byref(ent), None, None, 0x01, ctypes.byref(out))
    if not fn(*args):
        raise OSError("API 키를 암호화/복호화하지 못했습니다 (다른 사용자·다른 컴퓨터에서 만든 파일?)")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


# ---------------------------------------------------------------- 저장·읽기
def save_api_key(key: str) -> Path:
    key = key.strip()
    if not key:
        raise ValueError("API 키가 비어 있습니다")
    p = key_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = key.encode("utf-8")
    if sys.platform == "win32":
        p.write_bytes(_dpapi(data, True))
    else:
        p.write_bytes(data)
        os.chmod(p, 0o600)
    return p


def load_api_key() -> str | None:
    p = key_path()
    if not p.exists():
        return None
    raw = p.read_bytes()
    try:
        data = _dpapi(raw, False) if sys.platform == "win32" else raw
    except OSError:
        return None
    return data.decode("utf-8").strip() or None


def delete_api_key() -> None:
    p = key_path()
    if p.exists():
        p.unlink()


def masked(key: str | None) -> str:
    if not key:
        return ""
    return f"{key[:7]}…{key[-4:]}" if len(key) > 14 else "…" * 3
