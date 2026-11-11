"""3단계 텍스트 추출 (PRD 5.2): Claude 비전으로 영역별 원문을 읽는다.

카드 한 장 = 요청 한 번. 영역마다 원본 PDF를 잘라 이미지로 보내고(180도 영역은 바로 세워서),
영역별 원문을 JSON(구조화 출력)으로 받는다.

이어 하기 (토큰·크레딧·속도 제한에 걸려도):
- 결과는 카드마다 projects/<p>/extract/pages/page-NNN.json 에 바로 쓴다. 다시 실행하면 이미 읽은
  카드는 건너뛴다.
- 속도 제한(429)·과부하(529)·서버 오류·연결 오류·크레딧 부족·예산 도달이면 그 자리에서 멈추고
  state.json에 '일시 정지'와 이유를 적는다. 같은 명령을 다시 실행하면 남은 카드부터 이어 간다.
- 일괄 처리(--batch, 50% 할인): 보낸 일괄 작업 ID를 state.json에 저장한다. 앱을 껐다 켜도 그 작업의
  결과를 받아 오고, 실패·만료된 카드만 다시 보낸다.

요청·응답 처리는 client 객체로만 한다 (테스트는 가짜 client를 넣는다).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

log = logging.getLogger("cardtranslater.extract")

MODELS = {  # 옵션 이름 -> 모델 ID
    "opus": "claude-opus-5-5",  # 가장 정확 (기본)
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",  # 가장 저렴
}
MODEL = MODELS["opus"]
PRICE_PER_MTOK = {  # (입력, 출력) $/100만 토큰. 일괄 처리는 절반
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),  # 요청 10만 토큰 이하 (카드 한 장은 훨씬 작다)
}
# 거절 시 서버 쪽 대체 모델(fallbacks: "default")을 쓸 수 있는 모델. Haiku는 지원하지 않는다
SERVER_FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}


def resolve_model(name: str | None) -> str:
    """'opus'·'sonnet'·'haiku' 또는 모델 ID -> 모델 ID."""
    if not name:
        return MODEL
    if name in MODELS:
        return MODELS[name]
    if name in PRICE_PER_MTOK:
        return name
    raise ValueError(f"모르는 모델: {name} (opus, sonnet, haiku 중 하나)")
CROP_DPI = 400
MAX_CROP_PX = 1500  # 긴 변 상한 (API는 큰 이미지를 줄여서 본다)
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """You transcribe printed text from cropped regions of scanned board-game cards.
The source language is given in the request. Each image is one named region of the same card,
already rotated upright.

Rules:
- Copy exactly the characters printed. Do not translate, correct, normalize or complete text.
- Keep the card's line breaks: one output line per printed line, joined with \\n.
- Keep punctuation as printed (full-width 、。！？「」（）, brackets such as [4階層] or [炎], dashes such as ——).
- Region kind "tag_pills": colored rounded labels with short words. Output one label per line, left to right.
- A small picture inside a sentence (an icon standing for a word) is written [[?]] at its position.
  Never drop an icon: every picture between words must appear as [[?]] (e.g. "ごとに[[?]]を得る", not "ごとにを得る").
  A number printed on the icon itself (a coin showing 3, a tile showing 2) belongs to the icon: write only [[?]].
  A line inside the region made only of icons, signs and numbers (e.g. a coin icon followed by +2, written
  [[?]] +2) IS text: keep it as its own line. Skip only decorative art, big stat numbers printed outside the
  region's text (card corners, badges), and small icons above a title.
- Word formatting: when a word or phrase inside a line looks different from the rest of that line, wrap it:
  <b>..</b> bolder, <i>..</i> slanted, <c1>..</c1> a different color (<c2> for a second different color, in
  order of first appearance on the card). Do not tag a whole line or a whole region: only the words that
  differ from their line. If nothing differs, use no tags.
- If the region has no text, return an empty string.
- Set uncertain=true when any character is hard to read or you are guessing, and say which in note.
  Otherwise uncertain=false and note is an empty string."""


# ---------------------------------------------------------------- 결과·상태
@dataclass
class ExtractState:
    status: str = "idle"  # idle | running | paused | waiting_batch | done
    reason: str = ""  # 멈춘 이유 (사람이 읽는 말)
    retry_after: str | None = None  # 이 시각 뒤에 다시 실행 (ISO)
    spent_usd: float = 0.0  # API로 실제 쓴 비용
    equiv_usd: float = 0.0  # Claude Code 요금제로 읽은 분량을 API 정가로 환산 (청구되지 않음)
    batch: dict | None = None  # {"id", "pages", "submitted_at"}
    failed: dict = field(default_factory=dict)  # 쪽 -> 이유 (다시 보내지 않음; --retry-failed로 다시)

    @classmethod
    def load(cls, path: Path) -> "ExtractState":
        if path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            d["failed"] = {int(k): v for k, v in d.get("failed", {}).items()}
            return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


class Paused(Exception):
    """API 쪽 한도(속도·크레딧)나 예산 때문에 멈춘다. 다시 실행하면 이어 간다."""

    def __init__(self, reason: str, retry_after_s: float | None = None):
        super().__init__(reason)
        self.reason = reason
        self.retry_after_s = retry_after_s


def cost_usd(usage, model: str = MODEL, batch: bool = False) -> float:
    pin, pout = PRICE_PER_MTOK.get(model, PRICE_PER_MTOK[MODEL])
    inp = (getattr(usage, "input_tokens", 0) or 0)
    inp += (getattr(usage, "cache_creation_input_tokens", 0) or 0) * 1.25
    inp += (getattr(usage, "cache_read_input_tokens", 0) or 0) * 0.1
    out = getattr(usage, "output_tokens", 0) or 0
    c = (inp * pin + out * pout) / 1_000_000
    return c * 0.5 if batch else c


# ---------------------------------------------------------------- 요청 만들기
def ocr_excludes(template, page_no: int) -> list:
    """그 쪽의 ‘글자 읽기에서 빼기’ 보호 영역."""
    try:
        prot = template.protect_for(page_no)
    except Exception:  # noqa: BLE001
        prot = getattr(template, "protect", [])
    return [p for p in prot if (p.extract or {}).get("ocr_exclude")]


def region_crop_png(page, region, max_px: int = MAX_CROP_PX, exclude=None) -> bytes:
    """원본 쪽에서 영역을 잘라 PNG로 (180도 영역은 바로 세워서).
    exclude: 가릴 영역(보호 영역 중 ‘글자 읽기에서 빼기’). 그 자리는 그림의 바탕색으로 덮어 모델이 읽지 않게 한다."""
    import pymupdf

    from app.regions import ratio_to_pt

    rect = pymupdf.Rect(ratio_to_pt(region.rect, tuple(page.rect))) & page.rect
    dpi = CROP_DPI
    long_pt = max(rect.width, rect.height)
    if long_pt * dpi / 72 > max_px:
        dpi = int(max_px * 72 / long_pt)
    rot = int((region.style or {}).get("rotate", 0)) % 360
    mat = pymupdf.Matrix(dpi / 72, dpi / 72).prerotate(rot)
    pix = page.get_pixmap(matrix=mat, clip=rect, alpha=False)
    boxes = []
    for ex in exclude or []:
        r = pymupdf.Rect(ratio_to_pt(ex.rect, tuple(page.rect))) & rect
        if r.is_empty:
            continue
        s = dpi / 72
        x0, y0, x1, y1 = (r.x0 - rect.x0) * s, (r.y0 - rect.y0) * s, (r.x1 - rect.x0) * s, (r.y1 - rect.y0) * s
        if rot == 180:
            x0, x1 = pix.width - x1, pix.width - x0
            y0, y1 = pix.height - y1, pix.height - y0
        boxes.append((int(x0), int(y0), int(x1 + 0.999), int(y1 + 0.999)))
    if not boxes:
        return pix.tobytes("png")
    import io

    import numpy as np
    from PIL import Image

    a = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3].copy()
    bgc = np.median(a.reshape(-1, 3), axis=0).astype(np.uint8)  # 그림 바탕색
    for x0, y0, x1, y1 in boxes:
        a[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = bgc
    buf = io.BytesIO()
    Image.fromarray(a).save(buf, format="PNG")
    return buf.getvalue()


def region_hint(reg) -> str:
    hint = (reg.notes or "").split(".")[0][:120]
    words = [v["text"] for v in (reg.extract or {}).get("tag_colors", [])]
    if words:
        hint += " Known label words in this game: " + ", ".join(words) + "."
    return hint


NO_REGIONS = "영역 없음 (카드마다 영역을 정하는 그룹: 이 카드의 영역을 지정하세요)"


def build_params(project, page_no: int, src_doc, model: str = MODEL, regions=None) -> dict:
    """카드 한 장의 Messages API 요청 본문 (fallbacks·betas 제외). regions: 읽을 영역 (기본: 전부)."""
    page = src_doc[page_no - 1]
    regions = project.template.regions_for(page_no) if regions is None else regions
    lang = {"ja": "Japanese", "en": "English"}.get(project.template.source_language, project.template.source_language)
    content: list = [{"type": "text", "text": f"Source language: {lang}. Card page {page_no}. Regions follow."}]
    for reg in regions:
        content.append({"type": "text", "text": f"Region name: {reg.name} (kind: {reg.kind}). {region_hint(reg)}"})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/png",
            "data": base64.standard_b64encode(
                region_crop_png(page, reg, exclude=ocr_excludes(project.template, page_no))).decode("ascii"),
        }})
    content.append({"type": "text", "text": "Return one entry per region, in the order given."})
    return {
        "model": model,
        "max_tokens": 16000,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": content}],
        "output_config": {"format": {"type": "json_schema", "schema": output_schema(regions)}},
    }


# ---------------------------------------------------------------- 태그: 라벨 색 -> 글자 사전
TAG_COLOR_TOL = 45.0  # RGB 거리. 같은 종류 라벨의 실측 차이 ≈ 10, 다른 종류끼리 ≥ 60


def _hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def match_tag_color(color: str, vocab: list[dict], tol: float = TAG_COLOR_TOL) -> str | None:
    """라벨 색과 가장 가까운 사전 항목의 글자 (tol 밖이면 None)."""
    c = _hex_rgb(color)
    best, bd = None, tol
    for v in vocab:
        d = sum((a - b) ** 2 for a, b in zip(c, _hex_rgb(v["color"]))) ** 0.5
        if d <= bd:
            best, bd = v["text"], d
    return best


def tags_from_colors(project, page_no: int, src_doc) -> dict:
    """색 사전이 있는 태그 영역을 색만으로 읽는다. {영역: {"source", "uncertain", "note"}} (모두 맞춘 영역만).

    스캔·ClearScan 과정에서 라벨 안 작은 흰 글자는 뭉개져 비전으로도 읽을 수 없다(2026-11-04 실측).
    대신 같은 게임에서 라벨 색과 글자는 1:1이므로 색으로 정한다. 비용이 들지 않는다.
    """
    from app.erase import detect_tag_labels

    out = {}
    page = src_doc[page_no - 1]
    for reg in project.template.regions_for(page_no):
        vocab = (reg.extract or {}).get("tag_colors")
        if reg.kind != "tag_pills" or not vocab:
            continue
        labels = detect_tag_labels(page, project.template.for_page(page_no), reg)
        words = [match_tag_color(l["color"], vocab) for l in labels]
        if labels and all(words):
            out[reg.name] = {"source": words, "uncertain": False,
                             "note": "라벨 색으로 정함: " + ", ".join(l["color"] for l in labels)}
    return out


def output_schema(regions) -> dict:
    return {
        "type": "object",
        "properties": {
            "regions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": [r.name for r in regions]},
                        "text": {"type": "string"},
                        "uncertain": {"type": "boolean"},
                        "note": {"type": "string"},
                    },
                    "required": ["name", "text", "uncertain", "note"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["regions"],
        "additionalProperties": False,
    }


def parse_response(msg, region_kinds: dict) -> dict:
    """응답 -> {영역: {"source", "uncertain", "note"}}. 태그 영역은 줄마다 하나씩 목록으로."""
    if getattr(msg, "stop_reason", None) == "refusal":
        raise ValueError("모델이 응답을 거절함")
    if getattr(msg, "stop_reason", None) == "max_tokens":
        raise ValueError("응답이 max_tokens에서 잘림")
    text = next(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return regions_from_json(json.loads(text), region_kinds)


def regions_from_json(data: dict, region_kinds: dict) -> dict:
    out = {}
    for r in data["regions"]:
        src = r["text"]
        if region_kinds.get(r["name"]) == "tag_pills":
            src = [t.strip() for t in src.split("\n") if t.strip()]
        out[r["name"]] = {"source": src, "uncertain": bool(r["uncertain"]), "note": r["note"]}
    return out


# ---------------------------------------------------------------- 저장
def extract_dir(project_root: Path) -> Path:
    return Path(project_root) / "extract"


def page_file(base: Path, page_no: int) -> Path:
    """base = 추출 폴더 (프로젝트 extract/ 또는 시험 폴더)."""
    return Path(base) / "pages" / f"page-{page_no:03d}.json"


def load_extracted(project_root: Path, base: Path | None = None) -> dict:
    """{쪽: {영역: {"source", "uncertain", "note"}}} (추출한 쪽만). base가 없으면 프로젝트 extract/."""
    out = {}
    d = Path(base or extract_dir(project_root)) / "pages"
    for f in sorted(d.glob("page-*.json")) if d.exists() else []:
        rec = json.loads(f.read_text(encoding="utf-8"))
        regions = {}
        for name, r in rec["regions"].items():  # 칸마다 1차로 읽은 모델을 붙인다 (엑셀 '원문 확인' 열)
            regions[name] = {**r, "read_by": r.get("read_by") or rec.get("model"), "engine": rec.get("engine")}
        out[int(rec["page"])] = regions
    return out


MODEL_LABEL = {"claude-sonnet-5-5": "Sonnet", "claude-opus-5-5": "Opus", "claude-haiku-5-5": "Haiku"}


def verification_label(r: dict | None) -> str:
    """엑셀 '원문 확인' 열 값. r = load_extracted의 칸 하나 (없으면 손 입력)."""
    if r is None:
        return "손 입력"
    if r.get("edited_by"):  # 화면에서 사람이 고친 칸
        return "사람이 고침"
    if r.get("checked_by"):  # 사람이 보고 읽은 그대로 맞다고 저장한 칸
        return "사람이 확인"
    if r.get("readings"):  # 두 번째 모델로 다시 읽은 칸
        return "결론 못 냄" if r.get("uncertain") else "두 번 확인"
    if (r.get("note") or "").startswith("라벨 색"):
        return "라벨 색"
    name = MODEL_LABEL.get(r.get("read_by") or "", r.get("read_by") or "모델")
    return f"{name} 1회" + (" (확인 필요)" if r.get("uncertain") else "")


def _write_page(base: Path, page_no: int, regions: dict, msg, batch: bool) -> float:
    usage = getattr(msg, "usage", None)
    c = cost_usd(usage, getattr(msg, "model", MODEL), batch) if usage else 0.0
    rec = {
        "page": page_no,
        "model": getattr(msg, "model", MODEL),
        "batch": batch,
        "regions": regions,
        "usage": {k: getattr(usage, k, None) for k in ("input_tokens", "output_tokens")} if usage else None,
        "cost_usd": round(c, 6),
        "extracted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    f = page_file(base, page_no)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(f)  # 쓰다가 멈춰도 반쪽짜리 결과가 남지 않게
    return c


# ---------------------------------------------------------------- API 오류 분류
def send_message(client, params: dict, fallback: bool = False):
    """Messages API 요청 한 번. 응답이 길 수 있어(max_tokens가 크면 SDK가 스트리밍을 요구함) 스트리밍으로 받아
    완성된 메시지를 돌려준다. fallback이면 거절 시 서버가 대체 모델로 다시 하는 베타를 쓴다.
    stream이 없는 클라이언트(테스트의 가짜 등)는 create로."""
    api = client.beta.messages if fallback else client.messages
    kw = {"betas": [FALLBACK_BETA], "fallbacks": "default"} if fallback else {}
    stream = getattr(api, "stream", None)
    if stream is None:
        return api.create(**params, **kw)
    with stream(**params, **kw) as s:
        return s.get_final_message()


def classify_error(e: Exception) -> Paused | None:
    """멈추고 나중에 이어 할 오류면 Paused, 그 카드만 실패로 볼 오류면 None. 그 밖은 그대로 던진다."""
    import anthropic

    def retry_after(err) -> float | None:
        try:
            v = err.response.headers.get("retry-after")
            return float(v) if v else None
        except Exception:  # noqa: BLE001
            return None

    if isinstance(e, anthropic.RateLimitError):
        return Paused("API 속도 제한(429)에 걸림", retry_after(e) or 60)
    if isinstance(e, (anthropic.OverloadedError, anthropic.InternalServerError, anthropic.ServiceUnavailableError)):
        return Paused(f"API 서버가 바쁨·오류({getattr(e, 'status_code', '')})", retry_after(e) or 120)
    if isinstance(e, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return Paused("네트워크 연결 오류", 60)
    if isinstance(e, anthropic.AuthenticationError):
        raise e  # 키 문제: 기다려도 안 풀린다
    if isinstance(e, (anthropic.BadRequestError, anthropic.PermissionDeniedError)):
        msg = str(getattr(e, "message", e)).lower()
        if "credit" in msg or "billing" in msg or "balance" in msg or "spend limit" in msg:
            return Paused("API 크레딧·사용 한도 부족 (충전하거나 한도를 올린 뒤 다시 실행)")
        return None  # 이 카드의 요청 문제: 그 카드만 실패로 표시하고 계속
    raise e


# 동시에 보내는 요청 수: 카드마다 Claude 응답을 기다리는 시간이 대부분이라 여러 장을 함께 보낸다.
# Claude Code(요금제)는 사용 한도·CLI 시작 비용 때문에 적게. 환경 변수 CARDTRANSLATER_EXTRACT_WORKERS로 바꾼다
EXTRACT_WORKERS = {"api": 4, "claude-code": 2}


def extract_workers(engine: str) -> int:
    v = os.environ.get("CARDTRANSLATER_EXTRACT_WORKERS", "").strip()
    if v.isdigit() and int(v) > 0:
        return int(v)
    return EXTRACT_WORKERS.get(engine, 1)


def _pipelined(todo: list, prepare, finish, workers: int, cancel=None, before=None) -> None:
    """todo의 항목을 차례로 prepare(item)(주 스레드: PDF 자르기 등)한 뒤, 돌려준 함수를 최대 workers개 스레드에서
    동시에 실행하고, 끝난 것부터 finish(item, 결과, 예외)(주 스레드: 저장·상태)를 부른다.

    - prepare가 None을 돌려주면 그 항목은 이미 처리된 것 (요청 없음).
    - before(i, item)는 보내기 전에 부른다 (예산 확인·진행 표시). 예외를 던지면 새로 보내지 않는다.
    - 작업 함수의 예외는 finish에 넘긴다. finish가 예외를 던지면(Paused 등) 새로 보내지 않고, 이미 보낸 요청은
      끝까지 받아 저장한 뒤(돈을 낸 결과를 버리지 않게) 처음 예외를 던진다.
    - cancel.check()가 던지면 기다리지 않고 바로 던진다 (보낸 요청의 결과는 버린다).
    """
    from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait

    workers = max(1, workers)
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="extract")
    running: dict[Future, object] = {}
    stop: BaseException | None = None
    it = iter(enumerate(todo))
    wait_pool = True

    def settle(fut: Future) -> None:
        nonlocal stop
        item = running.pop(fut)
        exc = fut.exception()
        try:
            finish(item, None if exc else fut.result(), exc)
        except BaseException as e:  # noqa: BLE001
            if stop is None:
                stop = e

    try:
        while True:
            while stop is None and len(running) < workers:
                if cancel:
                    cancel.check()
                nxt = next(it, None)
                if nxt is None:
                    break
                i, item = nxt
                try:
                    if before:
                        before(i, item)
                    job = prepare(item)
                except BaseException as e:  # noqa: BLE001
                    stop = e
                    break
                if job is not None:
                    running[pool.submit(job)] = item
            if not running:
                break
            finished, _ = wait(list(running), timeout=0.2, return_when=FIRST_COMPLETED)
            if cancel and stop is None:
                cancel.check()
            for fut in finished:
                settle(fut)
        if stop is not None:
            raise stop
    except BaseException as e:
        wait_pool = e is stop  # 취소 등 밖에서 온 예외: 보낸 요청을 기다리지 않는다
        raise
    finally:
        pool.shutdown(wait=wait_pool, cancel_futures=True)


def _iso_after(seconds: float | None) -> str | None:
    if not seconds:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + seconds))


# ---------------------------------------------------------------- 실행
@dataclass
class ExtractRunResult:
    state: str  # done | paused | waiting_batch
    done_pages: list[int]
    remaining_pages: list[int]
    failed: dict
    spent_usd: float
    reason: str = ""
    retry_after: str | None = None
    batch_id: str | None = None
    model: str = MODEL
    out_dir: Path | None = None
    engine: str = "api"
    equiv_usd: float = 0.0


def run_extract(project, pages: list[int], client, *, batch: bool = False, budget_usd: float | None = None,
                retry_failed: bool = False, model: str = MODEL, out_dir: Path | None = None,
                progress=None, cancel=None) -> ExtractRunResult:
    import pymupdf

    from app.service import ProgressEvent

    engine = getattr(client, "engine", "api")
    base = Path(out_dir or extract_dir(project.root))
    spath = base / "state.json"
    st = ExtractState.load(spath)
    if retry_failed:
        st.failed = {}
    # 영역이 없어 건너뛴 카드는 그 사이 영역을 정했으면 다시 읽는다
    st.failed = {p: r for p, r in st.failed.items() if not (r == NO_REGIONS and project.template.has_regions(p))}
    kinds = {r.name: r.kind for r in project.template.regions}
    done = set(load_extracted(project.root, base))
    src_doc = pymupdf.open(stream=project.source_pdf.read_bytes(), filetype="pdf")

    def remaining() -> list[int]:
        busy = set((st.batch or {}).get("pages", []))
        return [p for p in pages if p not in done and p not in st.failed and p not in busy]

    def result(state: str) -> ExtractRunResult:
        st.status = state
        st.save(spath)
        return ExtractRunResult(state, sorted(p for p in pages if p in done), remaining(), dict(st.failed),
                                round(st.spent_usd, 4), st.reason, st.retry_after, (st.batch or {}).get("id"),
                                model, base, engine, round(st.equiv_usd, 4))

    st.status, st.reason, st.retry_after = "running", "", None
    st.save(spath)
    try:
        if engine == "claude-code":
            return _run_claude_code(project, pages, client, st, spath, done, kinds, src_doc, remaining, result,
                                    progress, cancel, model, base)
        if batch:
            return _run_batch(project, pages, client, st, spath, done, kinds, src_doc, budget_usd, remaining, result,
                              progress, cancel, model, base)
        todo = remaining()
        tags_of: dict[int, dict] = {}

        def before(i, pno):
            # 예산은 보내기 전에 본다 (이미 동시에 보낸 요청 몫만큼은 넘을 수 있다)
            if budget_usd is not None and st.spent_usd >= budget_usd:
                raise Paused(f"정한 예산(${budget_usd:.2f})에 도달")
            if progress:
                progress(ProgressEvent("extract", i, len(todo), f"{pno}쪽 글자 읽는 중", pno))

        def prepare(pno):  # 주 스레드: PDF를 잘라 요청을 만든다 (PyMuPDF는 여러 스레드에서 함께 쓰면 안 된다)
            if not project.template.has_regions(pno):  # 카드별 그룹에서 아직 영역을 정하지 않은 카드
                st.failed[pno] = NO_REGIONS
                st.save(spath)
                return None
            color_tags = tags_from_colors(project, pno, src_doc)
            to_read = [r for r in project.template.regions_for(pno) if r.name not in color_tags]
            if not to_read:
                _write_plain(base, pno, color_tags, model, "colors")
                done.add(pno)
                st.save(spath)
                return None
            tags_of[pno] = color_tags
            params = build_params(project, pno, src_doc, model, to_read)
            # 거절되면 서버가 대체 모델로 다시 읽는다 (SERVER_FALLBACK_MODELS)
            return lambda: send_message(client, params, model in SERVER_FALLBACK_MODELS)

        def finish(pno, msg, exc):  # 주 스레드: 결과 저장·상태
            color_tags = tags_of.pop(pno, {})
            if exc is not None:
                if not isinstance(exc, Exception):
                    raise exc
                p = classify_error(exc)
                if p is None:
                    st.failed[pno] = f"요청 오류: {getattr(exc, 'message', exc)}"
                    st.save(spath)
                    return
                raise p
            try:
                regions = {**parse_response(msg, kinds), **color_tags}
            except (ValueError, KeyError, StopIteration, json.JSONDecodeError) as e:
                st.failed[pno] = f"응답 오류: {e}"
                st.spent_usd += cost_usd(msg.usage, model) if getattr(msg, "usage", None) else 0
                st.save(spath)
                return
            st.spent_usd += _write_page(base, pno, regions, msg, batch=False)
            done.add(pno)
            st.save(spath)

        _pipelined(todo, prepare, finish, extract_workers("api"), cancel, before)
        return result("done")
    except Paused as p:
        st.reason, st.retry_after = p.reason, _iso_after(p.retry_after_s)
        return result("paused")
    except Exception as e:  # noqa: BLE001
        from app.extract_cc import NotAvailable

        if not isinstance(e, NotAvailable):
            st.status = "paused"
            st.reason = f"예상하지 못한 오류: {e}"
            st.save(spath)
            raise
        st.reason = str(e)
        return result("blocked")
    finally:
        src_doc.close()


def _run_batch(project, pages, client, st, spath, done, kinds, src_doc, budget_usd, remaining, result,
               progress, cancel, model, base):
    """일괄 처리. 진행 중인 작업이 있으면 결과를 받아 오고, 남은 카드는 새 작업으로 보낸다."""
    from app.service import ProgressEvent

    if st.batch:
        try:
            b = client.messages.batches.retrieve(st.batch["id"])
        except Exception as e:  # noqa: BLE001
            p = classify_error(e)
            raise p if p else e
        if b.processing_status != "ended":
            st.reason = f"일괄 작업 {st.batch['id']} 처리 중 (보통 1시간 안, 최대 24시간). 나중에 다시 실행하면 결과를 받아 온다"
            return result("waiting_batch")
        for r in client.messages.batches.results(st.batch["id"]):
            pno = int(r.custom_id.split("-")[1])
            kind = r.result.type
            if kind == "succeeded":
                try:
                    regions = parse_response(r.result.message, kinds)
                    ct = base / "color_tags" / f"page-{pno:03d}.json"
                    if ct.exists():
                        regions.update(json.loads(ct.read_text(encoding="utf-8")))
                except (ValueError, KeyError, StopIteration, json.JSONDecodeError) as e:
                    st.failed[pno] = f"응답 오류: {e}"
                    continue
                st.spent_usd += _write_page(base, pno, regions, r.result.message, batch=True)
                done.add(pno)
            elif kind == "errored" and getattr(r.result.error, "type", "") == "invalid_request":
                st.failed[pno] = "요청 오류(일괄)"
            # 그 밖(서버 오류·취소·만료)은 남은 카드로 두어 다시 보낸다
        st.batch = None
        st.save(spath)

    todo = remaining()
    if not todo:
        return result("done")
    if budget_usd is not None:
        est = _avg_cost(base, model) or _default_cost(model) * 0.5
        can = int(max(0.0, budget_usd - st.spent_usd) // est)
        if can <= 0:
            raise Paused(f"정한 예산(${budget_usd:.2f})에 도달")
        todo = todo[:can]
    reqs = []
    for i, pno in enumerate(todo):
        if cancel:
            cancel.check()
        if progress:
            progress(ProgressEvent("extract", i, len(todo), f"{pno}쪽 일괄 요청 준비", pno))
        if not project.template.has_regions(pno):
            st.failed[pno] = NO_REGIONS
            continue
        color_tags = tags_from_colors(project, pno, src_doc)
        if color_tags:
            (base / "color_tags").mkdir(parents=True, exist_ok=True)
            (base / "color_tags" / f"page-{pno:03d}.json").write_text(
                json.dumps(color_tags, ensure_ascii=False), encoding="utf-8")
        to_read = [r for r in project.template.regions_for(pno) if r.name not in color_tags]
        reqs.append({"custom_id": f"page-{pno}", "params": build_params(project, pno, src_doc, model, to_read)})
    try:
        b = client.messages.batches.create(requests=reqs)
    except Exception as e:  # noqa: BLE001
        p = classify_error(e)
        raise p if p else e
    st.batch = {"id": b.id, "pages": todo, "submitted_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    st.reason = f"일괄 작업 {b.id}로 {len(todo)}장을 보냄. 나중에 다시 실행하면 결과를 받아 온다"
    return result("waiting_batch")


def _run_claude_code(project, pages, client, st, spath, done, kinds, src_doc, remaining, result,
                     progress, cancel, model, base):
    """Claude Code 요금제 엔진. 한 장씩 `claude -p`로 읽는다."""
    from app.extract_cc import CardError
    from app.service import ProgressEvent

    todo = remaining()
    tags_of: dict[int, dict] = {}
    can_split = hasattr(client, "prepare_card") and hasattr(client, "run_card")

    def before(i, pno):
        if progress:
            progress(ProgressEvent("extract", i, len(todo), f"{pno}쪽 글자 읽는 중 (Claude Code)", pno))

    def prepare(pno):  # 주 스레드: 영역 이미지 만들기 (PyMuPDF)
        if not project.template.has_regions(pno):
            st.failed[pno] = NO_REGIONS
            st.save(spath)
            return None
        color_tags = tags_from_colors(project, pno, src_doc)
        to_read = [r for r in project.template.regions_for(pno) if r.name not in color_tags]
        if not to_read:
            _write_plain(base, pno, color_tags, model, "colors")
            done.add(pno)
            st.save(spath)
            return None
        tags_of[pno] = color_tags
        args = (project, pno, src_doc, model, base, output_schema(to_read), to_read)
        if can_split:
            prepared = client.prepare_card(*args)
            return lambda: client.run_card(prepared)
        try:  # 나눠 부를 수 없는 클라이언트: 여기(주 스레드)서 읽는다
            reply = client.read_card(*args)
        except CardError as e:
            def fail(e=e):
                raise e
            return fail
        return lambda: reply

    def finish(pno, reply, exc):  # 주 스레드: 결과 저장·상태
        color_tags = tags_of.pop(pno, {})
        if exc is not None:
            if isinstance(exc, CardError):
                st.failed[pno] = str(exc)
                st.save(spath)
                return
            raise exc  # Paused·NotAvailable 등: 멈춘다
        try:
            regions = {**regions_from_json(reply.regions_json, kinds), **color_tags}
        except (KeyError, TypeError, ValueError) as e:
            st.failed[pno] = f"응답 오류: {e}"
            st.save(spath)
            return
        rec = {
            "page": pno, "engine": "claude-code", "model": reply.model, "batch": False, "regions": regions,
            "usage": reply.usage, "cost_usd": 0.0, "cost_equiv_usd": round(reply.cost_equiv_usd, 6),
            "extracted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        f = page_file(base, pno)
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(f)
        st.equiv_usd += reply.cost_equiv_usd
        done.add(pno)
        st.save(spath)

    _pipelined(todo, prepare, finish, extract_workers("claude-code"), cancel, before)
    return result("done")


def _write_plain(base: Path, page_no: int, regions: dict, model: str, engine: str) -> None:
    """모델을 부르지 않고 정한 결과(예: 태그만 있는 카드를 색으로) 저장."""
    rec = {"page": page_no, "engine": engine, "model": None, "batch": False, "regions": regions,
           "usage": None, "cost_usd": 0.0, "extracted_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    f = page_file(base, page_no)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")


def learn_tag_colors(project, src_doc, known: dict, tol: float = TAG_COLOR_TOL) -> tuple[dict, list[str]]:
    """글자를 아는 카드(known: 쪽 -> {영역: [글자...]})에서 '라벨 색 -> 글자' 사전을 배운다.

    라벨 수와 글자 수가 같은 카드만 쓴다. 돌려주는 값: ({영역: [{"color", "text", "samples"}]}, 경고).
    같은 글자의 색은 평균을 낸다. 서로 다른 글자의 색이 tol 안으로 가까우면 경고한다(사전이 헷갈린다).
    """
    from app.erase import detect_tag_labels

    acc: dict = {}
    warns: list[str] = []
    for reg in project.template.regions:
        if reg.kind != "tag_pills":
            continue
        for pno, regs in sorted(known.items()):
            words = regs.get(reg.name)
            if not isinstance(words, list) or not words:
                continue
            labels = detect_tag_labels(src_doc[pno - 1], project.template.for_page(pno), reg)
            if len(labels) != len(words):
                warns.append(f"p{pno} {reg.name}: 라벨 {len(labels)}개 / 글자 {len(words)}개 -> 건너뜀")
                continue
            for lab, w in zip(labels, words):
                acc.setdefault(reg.name, {}).setdefault(w, []).append(_hex_rgb(lab["color"]))
    out: dict = {}
    for name, by_word in acc.items():
        items = []
        for w, cols in by_word.items():
            avg = tuple(round(sum(c[i] for c in cols) / len(cols)) for i in range(3))
            spread = max(sum((a - b) ** 2 for a, b in zip(c, avg)) ** 0.5 for c in cols)
            if spread > tol:
                warns.append(f"{name} '{w}': 색이 카드마다 많이 다름 (최대 차이 {spread:.0f})")
            items.append({"color": "#%02x%02x%02x" % avg, "text": w, "samples": len(cols)})
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                d = sum((x - y) ** 2 for x, y in zip(_hex_rgb(a["color"]), _hex_rgb(b["color"]))) ** 0.5
                if d <= tol:
                    warns.append(f"{name}: '{a['text']}'와 '{b['text']}'의 라벨 색이 비슷함 (차이 {d:.0f})")
        out[name] = sorted(items, key=lambda v: -v["samples"])
    return out, warns


def _default_cost(model: str) -> float:
    """카드 한 장 추정 비용 (입력 2,000 + 출력 600 토큰)."""
    pin, pout = PRICE_PER_MTOK[model]
    return (2000 * pin + 600 * pout) / 1_000_000


def _avg_cost(base: Path, model: str) -> float | None:
    """같은 모델로 읽은 카드의 평균 비용 (일괄 처리 가격으로 환산)."""
    costs = []
    d = Path(base) / "pages"
    for f in d.glob("page-*.json") if d.exists() else []:
        rec = json.loads(f.read_text(encoding="utf-8"))
        if rec.get("cost_usd") and rec.get("model") == model:
            costs.append(rec["cost_usd"] * (1 if rec.get("batch") else 0.5))
    return sum(costs) / len(costs) if costs else None


def api_key_source() -> tuple[str | None, str]:
    """(API 키, 어디서 찾았나). 순서: 환경 변수 -> 화면에서 저장한 키(암호화) -> .env. 키는 코드에 적지 않는다."""
    from app.credentials import load_api_key
    from app.settings import env_files

    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"], "환경 변수"
    k = load_api_key()
    if k:
        return k, "저장한 키"
    for env in env_files():
        if env.exists():
            for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("ANTHROPIC_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"'), ".env"
    return None, ""


def make_client(api_key: str | None = None):
    """Anthropic client. api_key가 없으면 api_key_source()에서, 그래도 없으면 `ant auth login` 프로필을 쓴다."""
    import anthropic

    key = api_key or api_key_source()[0]
    return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()


# ---------------------------------------------------------------- 정확도 (손으로 옮긴 정답과 비교)
def _norm(s) -> str:
    if isinstance(s, list):
        s = "\n".join(s)
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("〜", "~").replace("～", "~")
    return "".join(s.split())


def evaluate(extracted: dict, truth: dict) -> dict:
    """칸별 비교. truth는 samples.json의 cards (쪽 -> 카드). 공백·전각/반각 차이는 무시한다."""
    rows = []
    for pno, regs in sorted(extracted.items()):
        card = truth.get(pno) or {}
        for name, r in regs.items():
            if name not in card or card[name].get("source") is None:
                continue
            a, b = _norm(r["source"]), _norm(card[name]["source"])
            rows.append({"key": f"p{pno}#{name}", "exact": a == b,
                         "char_acc": round(SequenceMatcher(None, a, b).ratio(), 4),
                         "uncertain": r.get("uncertain", False), "got": r["source"], "truth": card[name]["source"]})
    n = len(rows)
    wrong = [r for r in rows if not r["exact"]]
    return {
        "cells": n,
        "exact_rate": round(sum(r["exact"] for r in rows) / n, 4) if n else None,
        "char_acc_mean": round(sum(r["char_acc"] for r in rows) / n, 4) if n else None,
        "wrong": len(wrong),
        "wrong_flagged_uncertain": sum(r["uncertain"] for r in wrong),
        "rows": rows,
    }


# ---------------------------------------------------------------- 두 번 확인 (불확실한 칸만 더 정확한 모델로)
@dataclass
class RecheckResult:
    state: str  # done | paused | blocked
    checked_cells: int = 0
    agreed: int = 0  # 두 모델이 같게 읽어 '확인 필요'를 지운 칸
    disagreed: int = 0  # 다르게 읽어 '확인 필요'를 남긴 칸
    reason: str = ""
    retry_after: str | None = None
    spent_usd: float = 0.0
    equiv_usd: float = 0.0


def _read_regions_once(client, project, pno, src_doc, model, base, regions) -> tuple[dict, float, float]:
    """영역 일부만 한 번 읽는다. (영역 결과, API 비용, 요금제 환산). 이 카드만의 문제면 CardError."""
    return _prepare_read(client, project, pno, src_doc, model, base, regions)()


def _prepare_read(client, project, pno, src_doc, model, base, regions):
    """영역 일부를 읽을 요청을 만든다 (PDF를 다루므로 주 스레드에서). 돌려준 함수는 다른 스레드에서 불러도 되고,
    (영역 결과, API 비용, 요금제 환산)을 돌려준다. 이 카드만의 문제면 CardError."""
    from app.extract_cc import CardError

    kinds = {r.name: r.kind for r in project.template.regions}
    if getattr(client, "engine", "api") == "claude-code":
        args = (project, pno, src_doc, model, base, output_schema(regions), regions)
        if hasattr(client, "prepare_card") and hasattr(client, "run_card"):
            prepared = client.prepare_card(*args)
            call = lambda: client.run_card(prepared)  # noqa: E731
        else:
            reply0 = client.read_card(*args)  # 나눠 부를 수 없는 클라이언트: 여기서 읽는다
            call = lambda: reply0  # noqa: E731

        def run_cc():
            reply = call()
            try:
                return regions_from_json(reply.regions_json, kinds), 0.0, reply.cost_equiv_usd
            except (KeyError, TypeError, ValueError) as e:
                raise CardError(f"응답 오류: {e}")
        return run_cc
    params = build_params(project, pno, src_doc, model, regions)

    def run_api():
        try:
            msg = send_message(client, params, model in SERVER_FALLBACK_MODELS)
        except Exception as e:  # noqa: BLE001
            p = classify_error(e)
            if p is None:
                raise CardError(f"요청 오류: {getattr(e, 'message', e)}")
            raise p
        c = cost_usd(msg.usage, model) if getattr(msg, "usage", None) else 0.0
        try:
            return parse_response(msg, kinds), c, 0.0
        except (ValueError, KeyError, StopIteration, json.JSONDecodeError) as e:
            raise CardError(f"응답 오류: {e}")
    return run_api


# ---------------------------------------------------------------- 닮은 한자로 잘못 읽은 것 찾기
LOOKALIKE_SIM = 0.9  # 두 한자를 그려 겹쳐 본 닮은 정도 (復·後 0.95, 未·末 0.97 / 霊·鋭 0.85, 始·拓 0.83)
LOOKALIKE_MIN_COMMON = 3  # 다른 카드에서 이만큼 나온 한자 낱말과
_KANJI = "[一-鿿々]"
_JA_FONTS = ("C:/Windows/Fonts/msgothic.ttc", "C:/Windows/Fonts/YuGothM.ttc", "C:/Windows/Fonts/meiryo.ttc")


def _kanji_font():
    from PIL import ImageFont

    if not hasattr(_kanji_font, "font"):
        _kanji_font.font = None
        for f in _JA_FONTS:  # 일본어 한자(新字体)가 모두 있는 글꼴 (한국어 글꼴에는 없는 글자가 있다)
            try:
                _kanji_font.font = ImageFont.truetype(f, 64)
                break
            except OSError:
                continue
    return _kanji_font.font


def _glyph(ch: str):
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw

    cache = _glyph.__dict__.setdefault("cache", {})
    if ch not in cache:
        im = Image.new("L", (80, 80), 0)
        ImageDraw.Draw(im).text((8, 8), ch, font=_kanji_font(), fill=255)
        ink = np.asarray(im) > 128
        cache[ch] = (ink, cv2.dilate(ink.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0)
    return cache[ch]


def look_alike(a: str, b: str) -> bool:
    """두 한자가 닮았나 (그려서 서로의 획이 상대 획 근처에 얼마나 들어오는지). 글꼴이 없으면 False."""
    if a == b or _kanji_font() is None:
        return False
    ia, da = _glyph(a)
    ib, db = _glyph(b)
    if not ia.any() or not ib.any():
        return False
    return min((ia & db).sum() / ia.sum(), (ib & da).sum() / ib.sum()) >= LOOKALIKE_SIM


def suspicious_readings(cells: dict) -> dict:
    """cells {(쪽, 영역): 원문}. 한 번만 나온 한자 두 글자 낱말이, 다른 카드에서 LOOKALIKE_MIN_COMMON번 넘게 나온
    낱말과 닮은 한 글자만 다르면 잘못 읽었을 수 있다 (예: 回後 ← 回復). {(쪽, 영역): [(읽은 것, 아마 맞는 것, 횟수)]}."""
    import re
    from collections import Counter

    pat = re.compile(f"(?=({_KANJI}{_KANJI}))")
    found = {k: [m.group(1) for m in pat.finditer(t or "")] for k, t in cells.items()}
    count = Counter(w for ws in found.values() for w in ws)
    common = [w for w, c in count.items() if c >= LOOKALIKE_MIN_COMMON]
    out: dict = {}
    for key, ws in found.items():
        for w in ws:
            if count[w] != 1:
                continue
            for o in common:
                diff = [(x, y) for x, y in zip(w, o) if x != y]
                if len(diff) == 1 and look_alike(*diff[0]):
                    out.setdefault(key, []).append((w, o, count[o]))
                    break
    return out


def flag_lookalikes(base: Path, pages: list[int] | None = None) -> int:
    """추출 결과(base)에서 닮은 한자로 잘못 읽었을 법한 칸을 ‘확인 필요’로 바꾸고 이유를 남긴다 (두 번 읽기가
    다시 읽게). 한 칸은 한 번만 본다 ("lookalike" 기록). 바꾼 칸 수."""
    recs = {}
    for f in sorted((Path(base) / "pages").glob("page-*.json")):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        recs[int(rec["page"])] = (f, rec)
    cells = {}
    for pno, (_f, rec) in recs.items():
        for name, r in (rec.get("regions") or {}).items():
            src = r.get("source")
            cells[(pno, name)] = "\n".join(src) if isinstance(src, list) else (src or "")
    n = 0
    for (pno, name), hits in suspicious_readings(cells).items():
        if pages is not None and pno not in pages:
            continue
        f, rec = recs[pno]
        r = rec["regions"][name]
        if r.get("lookalike") is not None or r.get("readings"):
            continue  # 이미 봤거나 두 모델이 읽은 칸
        r["lookalike"] = [{"read": w, "likely": o, "seen": c} for w, o, c in hits]
        why = ", ".join(f"«{w}»(다른 카드에서는 «{o}» {c}번)" for w, o, c in hits)
        r["uncertain"] = True
        r["note"] = (f"닮은 한자로 잘못 읽었을 수 있음: {why}. " + (r.get("note") or "")).strip()
        if rec.get("recheck"):  # 이미 두 번 읽은 카드: 이 칸만 다시 읽게 (다른 칸의 두 모델 기록은 그대로)
            pending = rec.setdefault("recheck_pending", [])
            if name not in pending:
                pending.append(name)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(f)
        n += 1
    if n:
        log.info("닮은 한자 의심 칸 %d개를 확인 필요로 표시", n)
    return n


def run_recheck(project, pages: list[int], client, *, base: Path, check_model: str,
                progress=None, cancel=None) -> RecheckResult:
    """base의 추출 결과에서 '확인 필요' 칸만 check_model로 다시 읽는다. 끊겨도 다시 부르면 이어 간다.

    - 같게 읽으면(공백·전각/반각 무시) 확인 필요를 지운다.
    - 다르게 읽으면 check_model의 결과를 원문으로 쓰고, 두 결과를 note·readings에 남기고 확인 필요를 유지한다.
    - 다시 읽은 카드는 page 파일의 "recheck"에 기록해 두 번 하지 않는다.
    """
    import pymupdf

    from app.extract_cc import CardError, NotAvailable
    from app.service import ProgressEvent

    out = RecheckResult("done")
    todo = []
    for pno in pages:
        f = page_file(base, pno)
        if not f.exists():
            continue
        rec = json.loads(f.read_text(encoding="utf-8"))
        if rec.get("recheck"):  # 이미 다시 읽은 카드: 나중에 ‘닮은 한자’로 표시된 칸만
            names = [n for n in rec.get("recheck_pending") or [] if n in rec["regions"]]
        else:
            names = [n for n, r in rec["regions"].items() if r.get("uncertain")]
        if names:
            todo.append((pno, f, rec, names))
    if not todo:
        return out
    by_name_all = {r.name: r for r in project.template.regions}
    src_doc = pymupdf.open(stream=project.source_pdf.read_bytes(), filetype="pdf")

    def before(i, item):
        pno, _f, _rec, names = item
        if progress:
            progress(ProgressEvent("recheck", i, len(todo), f"{pno}쪽 불확실한 {len(names)}칸 다시 읽는 중", pno))

    def prepare(item):  # 주 스레드: PDF 자르기
        pno, _f, _rec, names = item
        by_name = {**by_name_all, **{r.name: r for r in project.template.regions_for(pno)}}
        regs = [by_name[n] for n in names if n in by_name]
        try:
            return _prepare_read(client, project, pno, src_doc, check_model, base, regs)
        except CardError as e:
            def fail(e=e):
                raise e
            return fail

    def finish(item, got, exc):  # 주 스레드: 비교·저장
        pno, f, rec, names = item
        if isinstance(exc, CardError):
            rec["recheck"] = {"model": check_model, "error": str(exc), "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            rec.pop("recheck_pending", None)
            f.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
            return
        if exc is not None:
            raise exc
        second, c, eq = got
        out.spent_usd += c
        out.equiv_usd += eq
        first_model = rec.get("model") or "1차"
        for n in names:
            if n not in second:
                continue
            a, b = rec["regions"][n], second[n]
            out.checked_cells += 1
            readings = [{"model": first_model, "source": a["source"]}, {"model": check_model, "source": b["source"]}]
            if _norm(a["source"]) == _norm(b["source"]) and not b.get("uncertain"):
                rec["regions"][n] = {**a, "uncertain": False, "note": f"{check_model}로 다시 읽어 같음",
                                     "readings": readings}
                out.agreed += 1
            else:
                same = _norm(a["source"]) == _norm(b["source"])
                head = ("두 모델이 같게 읽었지만 다시 읽은 모델도 확실하지 않음" if same
                        else "두 모델이 다르게 읽음")
                rec["regions"][n] = {"source": b["source"], "uncertain": True,
                                     "note": f"{head} ({first_model}: {a['source']} / "
                                             f"{check_model}: {b['source']}). {b.get('note', '')}".strip(),
                                     "readings": readings}
                out.disagreed += 1
        rec.pop("recheck_pending", None)
        rec["recheck"] = {"model": check_model, "regions": names, "cost_usd": round(c, 6),
                          "cost_equiv_usd": round(eq, 6), "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(f)

    try:
        _pipelined(todo, prepare, finish, extract_workers(getattr(client, "engine", "api")), cancel, before)
        return out
    except Paused as p:
        out.state, out.reason, out.retry_after = "paused", p.reason, _iso_after(p.retry_after_s)
        return out
    except NotAvailable as e:
        out.state, out.reason = "blocked", str(e)
        return out
    finally:
        src_doc.close()
