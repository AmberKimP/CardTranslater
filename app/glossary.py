"""용어집 관리 (PRD 5.3 가, 4장 `용어집.xlsx`).

게임 단위 용어집(games/<게임>/glossary.json)을 만들고, 검증하고, 확정한다.

    sources = collect_sources(info)                      프로젝트 모든 그룹·파일의 추출 원문
    cands, cost = find_candidates(info, sources, client, model)   용어 후보 (코드 + 모델)
    export_review(info, cands, out)                      용어집 검증 엑셀
    res = import_review(info, xlsx)                      검사 (멈춤·확인 목록)
    confirm(info, res.terms)                             게임 용어집 확정 (버전 +1)
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.workspace import ProjectInfo, _read, _write, file_dirs, file_key

CATEGORIES = ["카드 이름", "분류어·태그", "키워드·능력", "자원·토큰", "행동", "문구", "기타"]
# '문구': 조사·어미가 붙은 긴 구절(예: すべての判定にボーナスがもらえる). 번역 때 권장 표현으로만 주고
# 용어 불일치 검사는 하지 않는다 (글자가 조금만 달라도 불일치가 나서 오히려 방해가 된다).
_PHRASE_MARKS = set("をにがはでへとも。、！？!?")
PHRASE_MIN_LEN = 11


def is_phrase(source: str, category: str = "") -> bool:
    """용어가 아니라 '문구'(권장 표현)로 볼 것인가. 카드 이름·태그는 길어도 용어다."""
    if category == "문구":
        return True
    if category in ("카드 이름", "분류어·태그"):
        return False
    return len(source) >= PHRASE_MIN_LEN or any(ch in _PHRASE_MARKS for ch in source)
REVIEW_SHEET = "용어집"
INFO_SHEET = "정보"
HEADERS = ["원문 용어", "번역", "추천 번역", "원문 상태", "번역 확인", "다른 후보", "분류", "등장 횟수", "예문", "변형",
           "메모"]  # 확인할 정보(원문 상태·번역 확인·다른 후보)를 번역 바로 옆에 둔다 (2026-11-04 사용자 요청)
# '번역 확인': 모델이 번역이 갈릴 수 있다고 본 용어 ("확인 필요", 보라색). '다른 후보': 대안 번역과 이유
# '원문 상태': 이 용어가 나온 원문 칸의 추출 확인 결과 중 가장 나쁜 것 (결론 못 냄 > 확인 필요 > 빈 칸=확실)
COL = {h: i + 1 for i, h in enumerate(HEADERS)}
_BRACKET = re.compile(r"(?<!\[)\[([^\[\]\n]{1,24})\](?!\])")  # 카드에 인쇄된 [炎]·[人族] (아이콘 [[x]]는 제외)
_ICON = re.compile(r"\[\[[a-z0-9_]+\]\]")
# 「召喚石」·『魔冠』·［炎］처럼 괄호로 묶어 인쇄한 말은 무조건 용어 후보 (2026-11-05 사용자 요청)
_QUOTED = re.compile(r"「([^「」\n]{1,40})」|『([^『』\n]{1,40})』|［([^［］\n]{1,24})］")
_ICON_TOKEN = re.compile(r"\[\[[^\[\]]*\]\]")  # [[coin]]·[[?]] 등 모든 아이콘 자리표시


def is_icon_term(source: str) -> bool:
    """아이콘 자리표시가 든 용어. 아이콘은 아이콘 사전이 맡으므로 용어집에 넣지 않는다."""
    return bool(_ICON_TOKEN.search(source or ""))


# ---------------------------------------------------------------- 원문 모으기
@dataclass
class Source:
    key: str  # 그룹/파일#p쪽#영역
    region: str
    kind: str  # text | tag_pills
    text: str | list
    card_name: str | None = None  # 같은 카드의 이름 영역 (예문 표시용)
    verdict: str = ""  # 추출 확인: "" (확실) | "확인 필요" | "결론 못 냄"


def collect_sources(info: ProjectInfo, game: bool = False) -> list[Source]:
    """프로젝트(game=True면 같은 게임의 모든 프로젝트) 모든 그룹·파일의 추출 결과를 모은다 (처리할 쪽만)."""
    from app.extract import load_extracted, verification_label
    from app.project import load_project
    from app.workspace import game_file_dirs, game_file_key, included_pages

    out: list[Source] = []
    for fd in (game_file_dirs(info) if game else file_dirs(info)):
        fkey = game_file_key(fd) if game else file_key(fd)
        included = set(included_pages(fd))  # 뺀 쪽 제외, 시험 모드면 앞 card_limit장만
        try:
            proj = load_project(fd)
        except FileNotFoundError:
            continue  # 영역 템플릿이 아직 없는 그룹
        kinds = {r.name: r.kind for r in proj.template.regions}
        for pno, regs in sorted(load_extracted(fd).items()):
            if pno not in included:  # 그룹 형식과 다른 쪽(예: 규칙 카드) 또는 시험 모드 밖
                continue
            name_reg = next((n for n in regs if "이름" in n or "name" in n.lower()), None)
            card_name = regs[name_reg]["source"] if name_reg and isinstance(regs[name_reg]["source"], str) else None
            for rname, r in regs.items():
                src = r.get("source")
                if not src:
                    continue
                v = verification_label(r)
                verdict = "결론 못 냄" if v == "결론 못 냄" else ("확인 필요" if r.get("uncertain") else "")
                out.append(Source(f"{fkey}#p{pno}#{rname}", rname, kinds.get(rname, "text"), src, card_name,
                                  verdict))
    return out


def extraction_status(info: ProjectInfo, game: bool = False) -> list[dict]:
    """파일마다 추출 진행: 전체 쪽, 읽은 쪽, 실패, '결론 못 냄'·확인 필요 칸."""
    import pymupdf

    from app.extract import ExtractState, extract_dir, load_extracted, verification_label

    from app.workspace import game_file_dirs, game_file_key

    rows = []
    for fd in (game_file_dirs(info) if game else file_dirs(info)):
        pdfs = sorted((fd / "source").glob("*.pdf"))
        if not pdfs:
            continue
        from app.workspace import file_settings, included_pages

        pages = set(included_pages(fd))
        total = len(pages)
        has_regions = (fd / "regions.json").exists() or (fd.parent.parent / "regions.json").exists()
        ext = {p: r for p, r in load_extracted(fd).items() if p in pages}
        st = ExtractState.load(extract_dir(fd) / "state.json")
        cells = [r for regs in ext.values() for r in regs.values()]
        rows.append({
            "file": game_file_key(fd) if game else file_key(fd), "dir": fd, "pages": total, "extracted": len(ext), "failed": dict(st.failed),
            "status": st.status, "has_regions": has_regions,
            "excluded": file_settings(fd).get("exclude_pages", []),
            "no_verdict": sum(verification_label(r) == "결론 못 냄" for r in cells),
            "uncertain": sum(bool(r.get("uncertain")) for r in cells),
        })
    return rows


# ---------------------------------------------------------------- 용어 후보
@dataclass
class Term:
    source: str
    ko: str = ""  # 확정 번역 (검증 엑셀 B열)
    suggested: str = ""  # 추천 번역 (C열)
    category: str = "기타"
    count: int = 0
    examples: list[str] = field(default_factory=list)
    variants: list[str] = field(default_factory=list)
    note: str = ""
    confirmed: bool = False  # 이미 게임 용어집에 확정된 용어
    verdict: str = ""  # 나온 원문 칸 중 가장 불확실한 것 ("결론 못 냄" | "확인 필요" | "")
    verdict_keys: list[str] = field(default_factory=list)  # 그 원문 칸들
    check: bool = False  # 번역이 갈릴 수 있어 사람이 골라야 하는 용어 (모델 판단)
    alternatives: list[str] = field(default_factory=list)
    check_reason: str = ""
    chosen: str | None = None  # 사람이 이미 고른 번역 (다시 내보낼 때 B열에 그대로)

    def to_dict(self) -> dict:
        return {"source": self.source, "ko": self.ko, "category": self.category, "variants": self.variants,
                "note": self.note, "count": self.count}


_FMT_TAG = re.compile(r"</?(?:b|i|c\d+)>")


def _texts(s: Source) -> list[str]:
    """원문 글자 (서식 태그 <b><i><cN>는 빼고: 용어는 태그와 상관없다)."""
    return [_FMT_TAG.sub("", t) for t in (s.text if isinstance(s.text, list) else [s.text])]


_RANK = {"": 0, "확인 필요": 1, "결론 못 냄": 2}


def occurrences(form: str, text: str) -> int:
    """text 안에 용어가 나온 횟수. 한 글자 용어(예: 魔)는 다른 단어(悪魔) 안에서 세지 않도록
    칸 전체가 그 글자이거나 카드 대괄호 [魔] 안에 있을 때만 센다."""
    if not form or not text:
        return 0
    if len(form) == 1:
        return 1 if text == form else text.count(f"[{form}]")
    return text.count(form)


def term_verdict(term: str, sources: list[Source], variants: list[str] = ()) -> tuple[str, list[str]]:
    """용어가 나온 원문 칸 중 가장 불확실한 추출 결과와 그 칸들. 확실한 칸에도 나오면 그쪽이 근거가 된다."""
    forms = [term, *[v for v in variants if v]]
    worst, keys, any_sure = "", [], False
    for s in sources:
        hit = any((t == f) if s.kind == "tag_pills" else occurrences(f, t) > 0 for t in _texts(s) for f in forms)
        if not hit:
            continue
        if not s.verdict:
            any_sure = True
        elif _RANK[s.verdict] >= _RANK[worst]:
            if _RANK[s.verdict] > _RANK[worst]:
                keys = []
            worst = s.verdict
            keys.append(s.key)
    if any_sure:  # 확실하게 읽은 칸에도 같은 용어가 있으면 용어 자체는 믿을 만하다
        return "", []
    return worst, keys


def count_and_examples(term: str, sources: list[Source], variants: list[str] = (), n_examples: int = 2):
    forms = [term, *[v for v in variants if v]]
    count, ex = 0, []
    for s in sources:
        hits = 0
        for t in _texts(s):
            if s.kind == "tag_pills":
                hits += sum(t == f for f in forms)
            else:
                hits += sum(occurrences(f, t) for f in forms)
        if hits:
            count += hits
            if len(ex) < n_examples:
                where = f" ({s.card_name})" if s.card_name and s.region != s.card_name else ""
                ex.append(f"{s.key}{where}: {' / '.join(_texts(s))}".replace("\n", " "))
    return count, ex


def local_candidates(sources: list[Source]) -> dict[str, str]:
    """코드로 확실히 뽑는 후보: 태그 라벨 글자, 카드에 [ ]·「 」로 묶어 인쇄된 말. {용어: 분류}
    괄호 안 말은 모델이 고르지 않아도 무조건 넣는다."""
    out: dict[str, str] = {}
    for s in sources:
        if s.kind == "tag_pills":
            for t in _texts(s):
                out.setdefault(t, "분류어·태그")
        else:
            for t in _texts(s):
                plain = _ICON_TOKEN.sub("", re.sub(r"</?(?:b|i|c\d+)>", "", t))
                for m in _QUOTED.finditer(plain):
                    w = next(g for g in m.groups() if g is not None).strip()
                    if w:
                        out.setdefault(w, "분류어·태그" if m.group(3) else "문구" if is_phrase(w) else "키워드·능력")
                for m in _BRACKET.finditer(plain):
                    w = m.group(1).strip()
                    if w and not re.fullmatch(r"\d+.*", w):  # [4階層] 같은 숫자로 시작하는 것은 '階層'만
                        out.setdefault(w, "분류어·태그")
                    elif w:
                        rest = re.sub(r"^\d+", "", w).strip()
                        if rest:
                            out.setdefault(rest, "키워드·능력")
    return out


ASSESS_SYSTEM = """You review a Korean glossary for a board game localization before a human confirms it.
For each term and its proposed Korean, decide whether a human should choose the translation:
needs_check=true when several Korean renderings are common and the choice changes how the game reads
(e.g. 炎 -> 화염 / 불꽃, 人族 -> 인간족 / 인간 / 인족), when it is a category or keyword players will see on many
cards, when a name could be transliterated or translated, or when an action term is in a form that will read
awkwardly inside sentences (e.g. a noun form like 버리기 where sentences need 버린다).
Give 1-3 alternatives (different from the proposal) and a short reason in Korean.
Otherwise needs_check=false with no alternatives."""


def _assess_schema() -> dict:
    return {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "term": {"type": "string"},
                "needs_check": {"type": "boolean"},
                "alternatives": {"type": "array", "items": {"type": "string"}},
                "reason": {"type": "string"},
            },
            "required": ["term", "needs_check", "alternatives", "reason"],
            "additionalProperties": False,
        }}},
        "required": ["items"],
        "additionalProperties": False,
    }


def assess_terms(info: ProjectInfo, terms: list["Term"], client, model: str, work_dir: Path) -> tuple[float, float]:
    """확정 용어와 새 후보 모두에 대해 '번역 확인 필요'를 모델이 판단한다 (t.check·alternatives를 채움)."""
    from app.llm import complete_json

    if not terms:
        return 0.0, 0.0
    lines = []
    for t in terms:
        ko = t.ko if t.confirmed else t.suggested
        ex = t.examples[0].split(": ", 1)[-1] if t.examples else ""
        lines.append(json.dumps({"term": t.source, "proposed_ko": ko, "category": t.category,
                                 "example": ex[:80]}, ensure_ascii=False))
    prompt = f"Game: {info.game}. Terms (JSON lines):\n" + "\n".join(lines)
    data, api, eq = complete_json(client, model, ASSESS_SYSTEM, prompt, _assess_schema(), work_dir)
    by = {t.source: t for t in terms}
    for it in data.get("items", []):
        t = by.get(it["term"])
        if t and it["needs_check"]:
            t.check = True
            t.alternatives = [a for a in it["alternatives"] if a and a != (t.ko if t.confirmed else t.suggested)][:3]
            t.check_reason = it["reason"]
    return api, eq


CANDIDATE_SYSTEM = """You build a translation glossary for a board game being localized into Korean.
From the card texts, list terms that must be translated the same way on every card: card/item names,
category words and tags, keywords and abilities, resources and tokens, repeated rule phrases
(e.g. "判定にボーナスがもらえる" style fixed phrases), and game-specific nouns (e.g. 階層, ダイス目, バトル判定).
Do not list ordinary words or whole flavor sentences.
Prefer single words and short noun phrases. A fixed multi-word rule phrase with particles or a verb
(e.g. すべての判定にボーナスがもらえる, ダイスを振り) goes under category 문구: it is a style hint, not a strict term.
Never list icon placeholders such as [[coin]] or [[?]], or terms containing them: icons are handled separately.
Write each term exactly as it appears in the texts (same characters). Suggest a natural Korean translation
that fits a board game rulebook. Use the given category names."""


def _candidate_schema() -> dict:
    return {
        "type": "object",
        "properties": {"terms": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "term": {"type": "string"},
                "category": {"type": "string", "enum": CATEGORIES},
                "suggested_ko": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["term", "category", "suggested_ko", "reason"],
            "additionalProperties": False,
        }}},
        "required": ["terms"],
        "additionalProperties": False,
    }


def find_candidates(info: ProjectInfo, sources: list[Source], client, model: str,
                    work_dir: Path) -> tuple[list[Term], float, float]:
    """(후보 목록, API 비용, 요금제 환산). 확정 용어는 번역을 채운 채로 함께 돌려준다(새 후보만 모델에 묻는다)."""
    from app.llm import complete_json

    existing = {t["source"]: t for t in load_glossary(info).get("terms", [])}
    local = local_candidates(sources)
    uniq, seen = [], set()
    for s in sources:
        line = " / ".join(_texts(s)).replace("\n", " ")
        if line not in seen:
            seen.add(line)
            uniq.append(f"[{s.region}] {line}")
    prompt = (f"Game: {info.game}. Source language: {info.source_language}.\n"
              f"Already confirmed terms (do not list again): {', '.join(existing) or '(none)'}\n"
              f"Card texts ({len(uniq)} unique lines, region name in brackets):\n" + "\n".join(uniq))
    data, api_usd, equiv = complete_json(client, model, CANDIDATE_SYSTEM, prompt, _candidate_schema(), work_dir)

    terms: dict[str, Term] = {}
    for src, t in existing.items():
        if is_icon_term(src):
            continue
        c, ex = count_and_examples(src, sources, t.get("variants", []))
        terms[src] = Term(src, ko=t.get("ko", ""), category=t.get("category", "기타"), count=c, examples=ex,
                          variants=t.get("variants", []), note=t.get("note", ""), confirmed=True)
    for w, cat in local.items():
        if w not in terms and not is_icon_term(w):
            c, ex = count_and_examples(w, sources)
            terms[w] = Term(w, category=cat, count=c, examples=ex)
    for item in data.get("terms", []):
        w = re.sub(r"</?(?:b|i|c\d+)>", "", item["term"]).strip()  # 서식 태그는 용어가 아니다
        if not w or is_icon_term(w):
            continue
        if w in terms:
            if not terms[w].confirmed and not terms[w].suggested:
                terms[w].suggested = item["suggested_ko"]
            continue
        c, ex = count_and_examples(w, sources)
        if c == 0:
            continue  # 원문에 없는 용어(모델이 지어낸 것)는 버린다
        cat = "문구" if is_phrase(w, item["category"]) else item["category"]
        terms[w] = Term(w, suggested=item["suggested_ko"], category=cat, count=c, examples=ex,
                        note="")
    for t in terms.values():
        t.verdict, t.verdict_keys = term_verdict(t.source, sources, t.variants)
    a2, e2 = assess_terms(info, list(terms.values()), client, model, work_dir)
    api_usd += a2
    equiv += e2
    order = sorted(terms.values(), key=lambda t: (not t.confirmed, CATEGORIES.index(t.category)
                                                  if t.category in CATEGORIES else 99, -t.count, t.source))
    return order, api_usd, equiv


# ---------------------------------------------------------------- 게임 용어집 저장
def glossary_path(info: ProjectInfo) -> Path:
    return info.game_dir / "glossary.json"


def load_glossary(info: ProjectInfo) -> dict:
    p = glossary_path(info)
    data = _read(p) if p.exists() else {"game": info.game, "version": 0, "terms": []}
    data["terms"] = [t for t in data.get("terms", []) if not is_icon_term(t.get("source", ""))]  # 예전에 들어간 아이콘
    return data


def confirm(info: ProjectInfo, terms: list[dict]) -> dict:
    """게임 용어집을 확정한다 (버전 +1, 바뀐 용어 기록)."""
    terms = [t for t in terms if not is_icon_term(t.get("source", ""))]
    old = load_glossary(info)
    before = {t["source"]: t.get("ko") for t in old.get("terms", [])}
    changed = [t["source"] for t in terms if t["source"] in before and before[t["source"]] != t["ko"]]
    data = {"game": info.game, "version": int(old.get("version", 0)) + 1,
            "confirmed_at": time.strftime("%Y-%m-%d %H:%M:%S"), "confirmed_in_project": info.name,
            "terms": terms}
    _write(glossary_path(info), data)
    return {"version": data["version"], "terms": len(terms), "changed": changed}


# ---------------------------------------------------------------- 검증 엑셀
def export_review(info: ProjectInfo, terms: list[Term], out: Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment, Font, PatternFill, Protection

    wb = Workbook()
    ws = wb.active
    ws.title = REVIEW_SHEET
    ws.append(HEADERS)
    for c in ws[1]:
        c.font = Font(bold=True)
    for h, w in {"원문 용어": 22, "번역": 22, "추천 번역": 22, "분류": 12, "등장 횟수": 9, "예문": 70, "변형": 16,
                 "메모": 24, "원문 상태": 14, "번역 확인": 12, "다른 후보": 40}.items():
        ws.column_dimensions[ws.cell(1, COL[h]).column_letter].width = w
    ws.freeze_panes = "B2"
    edit = PatternFill("solid", fgColor="FFF8DC")
    done = PatternFill("solid", fgColor="D9EAD3")
    no_verdict = PatternFill("solid", fgColor="F4B183")  # 주황: 결론 못 낸 원문에서만 나온 용어
    unsure = PatternFill("solid", fgColor="FFE066")  # 노랑: 확인 필요 원문에서만 나온 용어
    to_check = PatternFill("solid", fgColor="D9D2E9")  # 보라: 번역이 갈릴 수 있어 사람이 골라야 하는 용어
    for i, t in enumerate(terms, start=2):
        b = t.chosen if t.chosen is not None else (t.ko if t.confirmed else t.suggested)
        vals = {"원문 용어": t.source, "번역": b, "추천 번역": t.suggested,
                "분류": t.category, "등장 횟수": t.count, "예문": "\n".join(t.examples),
                "변형": ", ".join(t.variants), "메모": ("이미 확정된 용어. " if t.confirmed else "") + t.note,
                "원문 상태": t.verdict or None, "번역 확인": "확인 필요" if t.check else None,
                "다른 후보": (", ".join(t.alternatives) + (f" — {t.check_reason}" if t.check_reason else ""))
                if t.check else None}
        for h, v in vals.items():
            c = ws.cell(i, COL[h], v)
            c.alignment = Alignment(wrap_text=True, vertical="top")
        for h in ("원문 용어", "번역", "분류", "변형", "메모"):
            ws.cell(i, COL[h]).protection = Protection(locked=False)
        ws.cell(i, COL["번역"]).fill = done if t.confirmed else edit
        if t.check:  # 번역이 갈릴 수 있는 용어: 보라색
            ws.cell(i, COL["번역"]).fill = to_check
            ws.cell(i, COL["번역 확인"]).fill = to_check
        if t.verdict:
            fill = no_verdict if t.verdict == "결론 못 냄" else unsure
            ws.cell(i, COL["원문 용어"]).fill = fill
            ws.cell(i, COL["원문 상태"]).fill = fill
            ws.cell(i, COL["원문 용어"]).comment = Comment(
                f"이 용어는 추출이 불확실한 칸({t.verdict})에서만 나왔습니다. 원문 글자가 틀렸을 수 있습니다.\n"
                + "\n".join(t.verdict_keys[:5]), "CardTranslater")
    # 사람이 아래에 용어를 직접 더할 수 있게 빈 행 몇 줄을 열어 둔다
    for i in range(len(terms) + 2, len(terms) + 52):
        for h in ("원문 용어", "번역", "분류", "변형", "메모"):
            ws.cell(i, COL[h]).protection = Protection(locked=False)
    ws.protection.sheet = True
    ws.protection.formatColumns = False
    ws.protection.formatRows = False

    info_ws = wb.create_sheet(INFO_SHEET)
    for k, v in [("종류", "용어집 검증"), ("게임", info.game), ("프로젝트", info.name),
                 ("용어집 버전", load_glossary(info).get("version", 0)),
                 ("내보낸 시각", time.strftime("%Y-%m-%d %H:%M:%S")),
                 ("안내", "B열(번역)을 확인·수정하세요. 비워 두면 용어로 쓰지 않습니다. 맨 아래 빈 행에 용어를 더할 수 있습니다.")]:
        info_ws.append([k, v])
    info_ws.column_dimensions["A"].width = 12
    info_ws.column_dimensions["B"].width = 80
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


@dataclass
class ReviewImport:
    terms: list[dict]  # 확정할 용어 (번역이 있는 것)
    blocking: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)  # 확인하고 계속할 항목
    unused: list[str] = field(default_factory=list)  # 번역이 비어 용어로 쓰지 않는 원문


def import_review(info: ProjectInfo, xlsx: Path) -> ReviewImport:
    from openpyxl import load_workbook

    wb = load_workbook(xlsx, data_only=True)
    res = ReviewImport([])
    if REVIEW_SHEET not in wb.sheetnames or INFO_SHEET not in wb.sheetnames:
        res.blocking.append("용어집 검증 엑셀이 아닙니다 (`용어집`·`정보` 시트 없음)")
        return res
    meta = {str(r[0].value): r[1].value for r in wb[INFO_SHEET].iter_rows() if r[0].value}
    if meta.get("게임") != info.game:
        res.blocking.append(f"다른 게임의 용어집입니다 (엑셀: {meta.get('게임')}, 지금: {info.game})")
        return res
    ws = wb[REVIEW_SHEET]
    head = [c.value for c in ws[1]]
    if any(h not in head for h in ("원문 용어", "번역")):
        res.blocking.append("`원문 용어`·`번역` 열이 없습니다")
        return res
    ix = {h: head.index(h) for h in head if h}

    def cell(row, h):
        v = row[ix[h]] if h in ix else None
        return "" if v is None else str(v).strip()

    seen: dict[str, int] = {}
    by_ko: dict[str, list[str]] = {}
    unchanged_checks: list[str] = []
    icon_rows: list[str] = []
    current = {t["source"]: t.get("ko") for t in load_glossary(info).get("terms", [])}  # 이미 확정된 번역
    for n, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        src = cell(row, "원문 용어")
        if not src:
            continue
        if src in seen:
            res.blocking.append(f"원문 용어가 두 번 있습니다: '{src}' ({seen[src]}행, {n}행)")
            continue
        seen[src] = n
        if is_icon_term(src):
            icon_rows.append(src)
            continue
        ko = cell(row, "번역")
        if not ko:
            res.unused.append(src)
            continue
        if "\n" in ko or " / " in ko:
            res.blocking.append(f"'{src}'의 번역이 여러 개입니다 ({n}행): {ko!r}. 하나만 남기세요")
            continue
        variants = [v.strip() for v in re.split(r"[,、]", cell(row, "변형")) if v.strip()]
        cnt = row[ix["등장 횟수"]] if "등장 횟수" in ix else 0
        res.terms.append({"source": src, "ko": ko, "category": cell(row, "분류") or "기타", "variants": variants,
                          "note": cell(row, "메모").replace("이미 확정된 용어. ", ""),
                          "count": int(cnt) if isinstance(cnt, (int, float)) else 0})
        by_ko.setdefault(ko, []).append(src)
        if cell(row, "번역 확인") and (ko == cell(row, "추천 번역") or ko == current.get(src)):
            unchanged_checks.append(f"{src} = {ko}")
    for ko, srcs in by_ko.items():
        if len(srcs) > 1:
            res.issues.append(f"번역 '{ko}'을(를) 여러 원문이 함께 씀: {', '.join(srcs)}")
    if unchanged_checks:
        res.issues.append(f"‘번역 확인’ 용어를 추천 번역 그대로 확정 {len(unchanged_checks)}개 (다른 후보를 봤는지 확인): "
                          + ", ".join(unchanged_checks[:20]) + (" …" if len(unchanged_checks) > 20 else ""))
    if icon_rows:
        res.issues.append(f"아이콘은 용어집에 넣지 않음 (아이콘 사전이 맡음): {', '.join(icon_rows)}")
    if res.unused:
        res.issues.append(f"번역이 비어 용어로 쓰지 않음 {len(res.unused)}개: {', '.join(res.unused[:20])}"
                          + (" …" if len(res.unused) > 20 else ""))
    return res


def terms_from_review(xlsx: Path) -> list[Term]:
    """검증 엑셀(옛 열 순서도 됨)을 다시 Term 목록으로 읽는다. 사람이 고른 B열은 chosen으로 보존한다."""
    from openpyxl import load_workbook

    ws = load_workbook(xlsx, data_only=True)[REVIEW_SHEET]
    head = [c.value for c in ws[1]]
    ix = {h: head.index(h) for h in head if h}

    def get(row, h):
        v = row[ix[h]] if h in ix else None
        return "" if v is None else str(v).strip()

    out = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        src = get(row, "원문 용어")
        if not src:
            continue
        note = get(row, "메모")
        alt = get(row, "다른 후보")
        alts, reason = (alt.split(" — ", 1) + [""])[:2] if alt else ("", "")
        cnt = row[ix["등장 횟수"]] if "등장 횟수" in ix else 0
        out.append(Term(src, ko=get(row, "번역"), suggested=get(row, "추천 번역"), category=get(row, "분류") or "기타",
                        count=int(cnt) if isinstance(cnt, (int, float)) else 0,
                        examples=[e for e in get(row, "예문").split("\n") if e],
                        variants=[v.strip() for v in get(row, "변형").split(",") if v.strip()],
                        note=note.replace("이미 확정된 용어. ", ""), confirmed=note.startswith("이미 확정된 용어"),
                        verdict=get(row, "원문 상태"), check=bool(get(row, "번역 확인")),
                        alternatives=[a.strip() for a in alts.split(",") if a.strip()], check_reason=reason,
                        chosen=get(row, "번역")))
    return out
