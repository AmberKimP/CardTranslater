"""5단계 초벌 번역 (PRD 5.3 나·다). 확정 게임 용어집을 반드시 지키며 프로젝트 전체 원문을 번역한다.

- 용어집이 확정된 프로젝트만 번역한다 (상태 glossary_confirmed 이상).
- 같은 원문은 한 번만 번역한다 (프로젝트 번역 캐시 translate/cache.json, 원문 문자열이 key).
- 칸 전체가 확정 용어와 같으면(카드 이름·태그 라벨) 모델을 부르지 않고 용어집 번역을 쓴다.
- 나머지는 여러 칸을 묶어 한 번에 보낸다. 그 묶음에 나오는 용어와 확정 번역을 함께 주고 반드시 쓰게 한다.
- 번역 뒤 검사(코드): 용어 불일치(원문에 용어가 있는데 번역에 확정 번역이 없음), 아이콘 불일치([[x]]).
  어긋난 칸은 한 번 더 고쳐 달라고 요청하고, 그래도 안 되면 상태로 표시한다.
- 끊겨도(한도·과부하) 묶음마다 저장하므로 다시 부르면 남은 칸부터 이어 간다.
- 용어집이 바뀌면 바뀐 용어가 든 칸만 다시 번역한다.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

STATUS_TERM = "용어 불일치"
STATUS_ICON = "아이콘 불일치"
STATUS_LEFTOVER = "원문 글자 남음"  # 번역문에 일본어(한자·가나)가 그대로 남음
_JA_CHARS = re.compile("[぀-ヿ㐀-䶿一-鿿]")
BATCH_CELLS = 30
_ICON = re.compile(r"\[\[([a-z0-9_]+)\]\]")

SYSTEM_PROMPT = """You translate board-game card text into natural Korean for a published Korean edition.

Rules:
- Use the glossary translation every time the source term appears. You may conjugate or inflect it to fit
  the sentence (e.g. 버리다 -> 버린다 / 버려진다 / 버린 뒤), but keep its stem, meaning and nuance; never swap it
  for a synonym.
- Rule/effect text: concise rulebook style ending in the plain "~한다/~할 수 있다" form.
  Flavor text and quotes: keep the tone. Keep the corner brackets 「」 and 『』 exactly as in the source
  (do not change them to “ ” or ‘ ’). Do not add quotation marks or brackets that are not in the source.
- Keep the same number of lines: the source line breaks (\\n) mark where the card breaks lines.
- Keep [[icon]] placeholders unchanged (move them only to fit Korean word order).
  Keep card brackets such as [炎] as [ ] around the translated glossary word, e.g. [불꽃].
  Keep formatting tags <b> </b> <i> </i> <c1> </c1> around the corresponding Korean words.
- Card names: short and natural. Do not add explanations.
- Keep numbers, + signs, and HP/POW style abbreviations as they are."""


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "ko": {"type": "string"}},
            "required": ["id", "ko"],
            "additionalProperties": False,
        }}},
        "required": ["items"],
        "additionalProperties": False,
    }


# ---------------------------------------------------------------- 용어 검사
def terms_in(text: str, glossary: list[dict]) -> list[dict]:
    """원문에 들어 있는 확정 용어 (긴 용어 우선). 짧은 용어가 더 긴 용어 안에만 들어 있으면 뺀다
    (예: ガストラップドア 안의 トラップ — 이름 용어 '가스 트랩도어'를 따르므로 '함정'을 요구하지 않는다)."""
    from app.glossary import is_phrase, occurrences

    out = []
    rest = text
    for t in sorted(glossary, key=lambda t: -len(t["source"])):
        forms = sorted([t["source"], *t.get("variants", [])], key=len, reverse=True)
        if is_phrase(t["source"], t.get("category", "")):
            if any(occurrences(f, text) for f in forms):
                out.append(t)  # 문구는 검사하지 않으므로 안의 용어를 가리지 않는다
            continue
        if any(occurrences(f, rest) for f in forms):
            out.append(t)
            for f in forms:  # 이 용어가 차지한 자리는 더 짧은 용어 검사에서 가린다
                rest = rest.replace(f, "　" * len(f))
    return out


_KO_ENDINGS = ("하기", "하다", "한다", "기", "다")  # 용어집 번역이 동사·명사형일 때 떼고 어간만 본다


def _jamo(s: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFD", s)


def ko_term_used(term_ko: str, ko: str) -> bool:
    """확정 번역이 번역문에 쓰였는가. 한국어는 활용하므로(버리기 -> 버린다·버려·버리고) 끝의 -기·-다를
    떼어 어간으로 보고, 자모 단위로 비교한다(버리 ⊂ 버린: ㅂㅓㄹㅣ ⊂ ㅂㅓㄹㅣㄴ). 두 글자 이상 어간만 뗀다."""
    if not term_ko:
        return True
    if term_ko in ko:
        return True
    stem = term_ko
    for e in _KO_ENDINGS:
        if stem.endswith(e) and len(stem) - len(e) >= 2:
            stem = stem[: -len(e)]
            break
    js, jk = _jamo(stem), _jamo(ko)
    if js in jk:
        return True
    # 어간 끝 모음이 어미와 줄어드는 경우 (버리+어 -> 버려, 되+어 -> 돼): 끝 모음만 빼고 비교.
    # NFD는 초성 ㄹ(U+1105)과 받침 ㄹ(U+11AF)을 구분하므로 '버ㄹ(초성)'은 버려·버릴에는 맞고 벌에는 안 맞는다.
    if len(stem) >= 2 and "ᅡ" <= js[-1] <= "ᅵ":
        return js[:-1] in jk
    return False


def check(source: str, ko: str, glossary: list[dict]) -> tuple[list[str], list[str]]:
    """(상태 목록, 어긴 용어 설명). 용어 불일치·아이콘 불일치."""
    status, missing = [], []
    from app.glossary import is_phrase

    for t in terms_in(source, glossary):
        if is_phrase(t["source"], t.get("category", "")):
            continue  # 문구는 권장 표현일 뿐 검사하지 않는다
        if not ko_term_used(t["ko"], ko):
            missing.append(f"{t['source']} → {t['ko']}")
    if missing:
        status.append(STATUS_TERM)
    if sorted(_ICON.findall(source)) != sorted(_ICON.findall(ko)):
        status.append(STATUS_ICON)
    if _JA_CHARS.search(_ICON.sub("", ko)):
        status.append(STATUS_LEFTOVER)
    return status, missing


# ---------------------------------------------------------------- 캐시
def cache_path(project_dir: Path) -> Path:
    return Path(project_dir) / "translate" / "cache.json"


def load_cache(project_dir: Path) -> dict:
    p = cache_path(project_dir)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_cache(project_dir: Path, cache: dict) -> None:
    p = cache_path(project_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _stale(entry: dict, source: str, glossary: list[dict]) -> bool:
    """캐시된 번역을 만든 뒤 이 원문에 든 용어의 확정 번역이 바뀌었으면 다시 번역한다."""
    used = entry.get("terms_used", {})
    return any(used.get(t["source"]) != t["ko"] for t in terms_in(source, glossary))


_BRACKETS = (("「", "」", "“", "”"), ("『", "』", "‘", "’"))


def keep_brackets(source: str, ko: str) -> str:
    """원문의 「」·『』를 번역이 “ ”·‘ ’로 바꿨으면 원래 괄호로 되돌린다 (원문에 없는 따옴표는 그대로)."""
    for o, c, qo, qc in _BRACKETS:
        n = source.count(o)
        if (n and o not in ko and qo not in source and ko.count(qo) == n
                and ko.count(qc) == source.count(c)):
            ko = ko.replace(qo, o).replace(qc, c)
    return ko


def lookup(cache: dict, source) -> dict | None:
    """원문(문자열 또는 태그 목록) -> 캐시 항목."""
    if isinstance(source, list):
        items = [cache.get(s) for s in source]
        if any(i is None for i in items):
            return None
        status = sorted({x for i in items for x in i.get("status", [])})
        return {"ko": [i["ko"] for i in items], "status": status,
                "terms_used": {k: v for i in items for k, v in i.get("terms_used", {}).items()}}
    return cache.get(source)


# ---------------------------------------------------------------- 실행
@dataclass
class TranslateResult:
    state: str  # done | paused | blocked
    cells: int = 0  # 번역할 서로 다른 원문 수
    from_glossary: int = 0  # 용어집으로 바로 정한 칸
    translated: int = 0  # 이번에 모델로 번역한 칸
    cached: int = 0  # 이미 번역되어 있던 칸
    term_mismatch: list[str] = field(default_factory=list)  # 고쳐 달라고 해도 남은 용어 불일치 원문
    icon_mismatch: list[str] = field(default_factory=list)
    leftover: list[str] = field(default_factory=list)  # 다시 번역해도 일본어가 남은 원문
    reason: str = ""
    retry_after: str | None = None
    equiv_usd: float = 0.0
    spent_usd: float = 0.0
    excel_note: str = ""  # 이미 있던 검수 엑셀을 새 번역으로 다시 만들지 못했을 때 (열려 있음 등)


def unique_sources(sources) -> list[tuple[str, str, str]]:
    """[(원문, 영역 이름, 종류)] 서로 다른 원문만. 태그 목록은 라벨 하나하나를 원문으로."""
    out, seen = [], set()
    for s in sources:
        texts = s.text if isinstance(s.text, list) else [s.text]
        for t in texts:
            if t and t not in seen:
                seen.add(t)
                out.append((t, s.region, s.kind))
    return out


def _icon_hint(batch, icons: dict | None) -> str:
    """묶음에 나오는 아이콘의 뜻·한국어 이름 (조사 고르기와 보조형/대체형 판단에 쓴다)."""
    names = sorted({n for text, _r, _k in batch for n in _ICON.findall(text)})
    if not names or not icons:
        return ""
    info = {n: {"meaning": icons.get(n, {}).get("meaning", ""), "ko": icons.get(n, {}).get("ko", "")} for n in names}
    return ("\nIcons (keep each [[name]] exactly as written; when an icon stands alone for a noun, pick the "
            "Korean particle after it as if it were its ko name): " + json.dumps(info, ensure_ascii=False))


def run_translate(project_dir: Path, sources, glossary: list[dict], client, model: str, *,
                  game: str, work_dir: Path, progress=None, cancel=None,
                  icons: dict | None = None) -> TranslateResult:
    """icons: 게임 아이콘 사전 {이름: {"meaning", "ko", ...}}."""
    from app.extract import Paused, _iso_after, _pipelined, extract_workers
    from app.extract_cc import CardError, NotAvailable
    from app.llm import complete_json
    from app.service import ProgressEvent

    cache = load_cache(project_dir)
    by_source = {t["source"]: t for t in glossary}
    items = unique_sources(sources)
    res = TranslateResult("done", cells=len(items))
    todo = []
    for text, region, kind in items:
        if text in cache and not _stale(cache[text], text, glossary):
            if cache[text].get("by") != "glossary":  # 검사 규칙이 바뀌었을 수 있으니 상태만 다시 매긴다 (모델 호출 없음)
                cache[text]["ko"] = keep_brackets(text, cache[text]["ko"])  # 예전 번역의 “ ”도 「」로
                st, missing = check(text, cache[text]["ko"], glossary)
                cache[text].update(status=st, missing_terms=missing)
                if STATUS_LEFTOVER in st and not cache[text].get("retried_leftover"):
                    cache[text]["retried_leftover"] = True
                    todo.append((text, region, kind))  # 일본어가 남은 번역은 한 번 다시 번역한다
                    continue
            res.cached += 1
            continue
        t = by_source.get(text) or next((g for g in glossary if text in g.get("variants", [])), None)
        if t and t["ko"]:  # 칸 전체가 확정 용어 (카드 이름·태그 라벨)
            cache[text] = {"ko": t["ko"], "status": [], "terms_used": {t["source"]: t["ko"]},
                           "by": "glossary", "region": region}
            res.from_glossary += 1
            continue
        todo.append((text, region, kind))
    save_cache(project_dir, cache)

    def ask(batch, extra=""):
        from app.glossary import is_phrase

        lines, gl, phrases = [], {}, {}
        for i, (text, region, _kind) in enumerate(batch):
            lines.append(json.dumps({"id": str(i), "region": region, "source": text}, ensure_ascii=False))
            for t in terms_in(text, glossary):
                (phrases if is_phrase(t["source"], t.get("category", "")) else gl)[t["source"]] = t["ko"]
        prompt = (f"Game: {game}.\nGlossary (must use): " + json.dumps(gl, ensure_ascii=False)
                  + ("\nPreferred phrasing (follow when it reads naturally): " + json.dumps(phrases, ensure_ascii=False)
                     if phrases else "")
                  + _icon_hint(batch, icons)
                  + f"\n{extra}\nItems (JSON lines):\n" + "\n".join(lines) + "\nReturn one item per id.")
        return complete_json(client, model, SYSTEM_PROMPT, prompt, _schema(), work_dir)

    def work(batch):
        """묶음 하나 번역 (작업 스레드: 모델 요청과 검사만, 캐시는 건드리지 않는다).
        돌려줌: (칸별 결과, API 비용, 요금제 환산, 다시 묻다가 난 예외 또는 None)."""
        data, api, eq = ask(batch)
        got = {it["id"]: it["ko"] for it in data.get("items", [])}
        entries, retry = {}, []
        for i, (text, region, _k) in enumerate(batch):
            ko = got.get(str(i))
            if ko is None:
                continue  # 빠진 칸은 다음 실행에서 다시
            ko = keep_brackets(text, ko)
            status, missing = check(text, ko, glossary)
            entries[text] = {"ko": ko, "status": status, "missing_terms": missing, "by": model, "region": region,
                             "terms_used": {t["source"]: t["ko"] for t in terms_in(text, glossary)},
                             "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            if status:
                retry.append((text, region, _k))
        if retry:  # 한 번 더: 어긴 용어를 짚어 고쳐 달라고 한다
            notes = "; ".join(f"{t}: {', '.join(entries[t]['missing_terms']) or ('translate every Japanese word into Korean' if STATUS_LEFTOVER in entries[t]['status'] else 'keep [[icons]]')}" for t, _r, _k in retry)
            try:
                data2, api2, eq2 = ask(retry, extra=f"Previous attempt broke these required terms/icons: {notes}")
            except Exception as e:  # noqa: BLE001 - 첫 번역은 저장하고 나서 멈춘다
                return entries, api, eq, e
            api, eq = api + api2, eq + eq2
            for it in data2.get("items", []):
                text, region, _k = retry[int(it["id"])]
                ko2 = keep_brackets(text, it["ko"])
                status, missing = check(text, ko2, glossary)
                if len(status) <= len(entries[text]["status"]):
                    entries[text].update(ko=ko2, status=status, missing_terms=missing, retried=True)
        return entries, api, eq, None

    batches = [todo[i:i + BATCH_CELLS] for i in range(0, len(todo), BATCH_CELLS)]
    index = {id(b): i for i, b in enumerate(batches)}

    def before(_i, batch):
        if progress:
            progress(ProgressEvent("translate", index[id(batch)], len(batches), f"{len(batch)}칸 번역 중"))

    def finish(batch, out, exc):  # 주 스레드: 캐시 저장·비용
        if exc is not None:
            raise exc
        entries, api, eq, late = out
        res.spent_usd += api
        res.equiv_usd += eq
        cache.update(entries)
        res.translated += sum(1 for t, _r, _k in batch if t in cache)
        save_cache(project_dir, cache)
        if late is not None:
            raise late

    try:
        # 묶음 여러 개를 동시에 보낸다 (글자 추출과 같은 수: API 4, Claude Code 2)
        _pipelined(batches, lambda b: (lambda: work(b)), finish,
                   extract_workers(getattr(client, "engine", "api")), cancel, before)
    except Paused as p:
        res.state, res.reason, res.retry_after = "paused", p.reason, _iso_after(p.retry_after_s)
    except (CardError, NotAvailable) as e:
        res.state, res.reason = "blocked", str(e)
    for text, *_ in items:
        e = cache.get(text)
        if e and STATUS_TERM in e.get("status", []):
            res.term_mismatch.append(text)
        if e and STATUS_ICON in e.get("status", []):
            res.icon_mismatch.append(text)
        if e and STATUS_LEFTOVER in e.get("status", []):
            res.leftover.append(text)
    return res
