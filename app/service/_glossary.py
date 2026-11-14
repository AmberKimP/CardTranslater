"""④ 용어집 만들기·확정과 확정한 용어 바꾸기를 칸마다 적용하기."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.project import load_project
from app.service import _extract
from app.service import _review
from app.service import _translate_erase


# ---------------------------------------------------------------- 추출 완료 -> 용어집 검증 -> 확정 (PRD 5.3 가)
@dataclass
class GlossaryReviewResult:
    state: str  # review_ready | needs_confirmation | blocked | paused
    review_xlsx: Path | None = None
    new_terms: int = 0
    confirmed_terms: int = 0
    issues: list[str] = field(default_factory=list)
    reason: str = ""
    retry_after: str | None = None
    equiv_usd: float = 0.0
    spent_usd: float = 0.0


GLOSSARY_MODEL = "claude-opus-5-5"  # 용어 후보는 한 번만 묻고 전체 번역의 기준이 되므로 Opus


def finish_extraction(project_dir: Path | str, *, accept_issues: bool = False, engine: str = "claude-code",
                      client=None) -> GlossaryReviewResult:
    """‘추출 완료’: 모든 그룹·파일의 원문을 모아 용어 후보를 뽑고 용어집 검증 엑셀을 만든다."""
    from app import glossary, workspace
    from app.extract import Paused, make_client
    from app.extract_cc import CardError, ClaudeCodeClient, NotAvailable

    info = workspace.load_project_info(project_dir)
    status = glossary.extraction_status(info, game=True)  # 용어집 검증은 게임 단위 (모든 프로젝트)
    if not status:
        return GlossaryReviewResult("blocked", reason="프로젝트에 파일이 없습니다 (add-files로 넣으세요)")
    issues = []
    for s in status:
        if not s["has_regions"]:
            issues.append(f"영역 템플릿이 없어 추출하지 못함: {s['file']}")
        elif s["extracted"] < s["pages"] - len(s["failed"]):
            issues.append(f"일부만 읽음: {s['file']} ({s['pages']}장 중 {s['extracted']}장)")
        if s["failed"]:
            issues.append(f"실패한 카드 (용어 후보에서 빠짐): {s['file']} {len(s['failed'])}장")
        if s.get("icon_changed"):
            issues.append(f"아이콘이 바뀐 칸 (③ 원문 확인 필요): {s['file']} {s['icon_changed']}칸")
        if s["no_verdict"]:
            issues.append(f"‘결론 못 냄’ 칸 (원문이 틀렸을 수 있음): {s['file']} {s['no_verdict']}칸")
    if issues and not accept_issues:
        return GlossaryReviewResult("needs_confirmation", issues=issues)
    # 이름 없는 아이콘이 남아 있으면 용어집으로 넘어가지 않는다 (PRD 2장 3단계 뒤 안전장치)
    ic = _extract.find_icons(info.dir, engine=engine, client=client)
    if ic.state == "icons_review":
        return GlossaryReviewResult("icons_review", review_xlsx=ic.review_xlsx, issues=issues,
                                    reason=f"이름을 정할 아이콘 묶음 {ic.groups}개 ({ic.occurrences}곳)"
                                           + (f", 위치 못 찾음 {ic.not_found}곳" if ic.not_found else "")
                                           + (f". {ic.reason}" if ic.reason else ""))
    sources = glossary.collect_sources(info, game=True)
    if not sources:
        return GlossaryReviewResult("blocked", issues=issues, reason="추출한 원문이 없습니다")
    if client is None:
        try:
            client = ClaudeCodeClient() if engine == "claude-code" else make_client()
        except Exception as e:  # noqa: BLE001
            return GlossaryReviewResult("blocked", issues=issues, reason=f"{engine} 엔진을 쓸 수 없습니다: {e}")
    try:
        terms, api_usd, equiv = glossary.find_candidates(info, sources, client, GLOSSARY_MODEL,
                                                         info.game_dir / "work" / "glossary")
    except Paused as p:
        from app.extract import _iso_after

        return GlossaryReviewResult("paused", issues=issues, reason=p.reason, retry_after=_iso_after(p.retry_after_s))
    except (CardError, NotAvailable) as e:
        return GlossaryReviewResult("blocked", issues=issues, reason=str(e))
    out = glossary.export_review(info, terms, info.game_dir / "용어집_검증.xlsx")
    _translate_erase._retire_legacy(info, ["용어집_검증.xlsx"])
    for p_ in workspace.game_projects(info):  # 같은 게임의 프로젝트가 함께 용어집 검증 단계로
        if p_.dir == info.dir or p_.status in ("extracting", "glossary_review"):
            workspace.set_status(p_, "glossary_review")
    return GlossaryReviewResult("review_ready", out, sum(not t.confirmed for t in terms),
                                sum(t.confirmed for t in terms), issues, equiv_usd=equiv, spent_usd=api_usd)


@dataclass
class GlossaryConfirmResult:
    state: str  # confirmed | needs_confirmation | blocked
    version: int = 0
    terms: int = 0
    changed: list[str] = field(default_factory=list)  # 번역이 바뀐 확정 용어 (그 용어가 든 번역은 다시 해야 함)
    blocking: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    # 바뀐 용어마다 {"source", "old", "new", "variants", "category"} (term_change_candidates·apply_term_changes에 넘김)
    changed_detail: list[dict] = field(default_factory=list)
    prev_status: str = ""  # 가져오기 전 프로젝트 상태 (바뀐 용어를 모두 반영하면 이 상태로 되돌린다)
    # 이번 가져오기로 상태가 바뀐 프로젝트마다 그 전 상태 {프로젝트 폴더: 상태} (apply_term_changes에 넘김)
    prev_statuses: dict = field(default_factory=dict)


def import_glossary(project_dir: Path | str, xlsx: Path | str, *, accept_issues: bool = False) -> GlossaryConfirmResult:
    """검증한 용어집 엑셀을 검사하고, 문제가 없거나 확인하면 게임 용어집으로 확정한다."""
    from app import glossary, workspace

    info = workspace.load_project_info(project_dir)
    return _confirm_review(info, glossary.import_review(info, Path(xlsx)), accept_issues)


def glossary_review_rows(project_dir: Path | str) -> list[dict] | None:
    """④ 용어집 만들기로 만든 용어 후보 (화면 표용). 없으면 None. 행마다 엑셀과 같은 열 이름:
    {"원문 용어", "번역", "추천 번역", "원문 상태", "번역 확인", "다른 후보", "분류", "등장 횟수", "예문", "변형", "메모",
    "confirmed": 이미 확정된 용어, "verdict_keys"}."""
    from app import glossary, workspace

    info = workspace.load_project_info(project_dir)
    xlsx = info.game_dir / "용어집_검증.xlsx"
    if not xlsx.exists():
        return None
    out = []
    for t in glossary.terms_from_review(xlsx):
        out.append({"원문 용어": t.source, "번역": t.chosen if t.chosen is not None else t.ko,
                    "추천 번역": t.suggested, "원문 상태": t.verdict or glossary.VERDICT_SURE,
                    "번역 확인": "확인 필요" if t.check else "",
                    "다른 후보": (", ".join(t.alternatives) + (f" — {t.check_reason}" if t.check_reason else ""))
                    if t.check else "",
                    "분류": t.category, "등장 횟수": t.count, "예문": "\n".join(t.examples),
                    "변형": ", ".join(t.variants), "메모": t.note, "confirmed": t.confirmed})
    return out


def save_glossary_rows(project_dir: Path | str, rows: list[dict]) -> Path:
    """화면 표에서 고친 용어 후보를 검증 엑셀에 다시 쓴다 (확정하지 않고 닫을 때·엑셀로 내보낼 때)."""
    from app import glossary, workspace

    info = workspace.load_project_info(project_dir)
    terms = []
    for r in rows:
        src = str(r.get("원문 용어") or "").strip()
        if not src:
            continue
        alt = str(r.get("다른 후보") or "")
        alts, reason = (alt.split(" — ", 1) + [""])[:2] if alt else ("", "")
        verdict = str(r.get("원문 상태") or "")
        terms.append(glossary.Term(
            src, ko=str(r.get("번역") or ""), suggested=str(r.get("추천 번역") or ""),
            category=str(r.get("분류") or "기타"), count=int(r.get("등장 횟수") or 0),
            examples=[e for e in str(r.get("예문") or "").split("\n") if e],
            variants=[v.strip() for v in str(r.get("변형") or "").split(",") if v.strip()],
            note=str(r.get("메모") or ""), confirmed=bool(r.get("confirmed")),
            verdict="" if verdict in ("", glossary.VERDICT_SURE) else verdict,
            check=bool(r.get("번역 확인")), alternatives=[a.strip() for a in alts.split(",") if a.strip()],
            check_reason=reason, chosen=str(r.get("번역") or "")))
    return glossary.export_review(info, terms, info.game_dir / "용어집_검증.xlsx")


def confirm_glossary_rows(project_dir: Path | str, rows: list[dict], *,
                          accept_issues: bool = False) -> GlossaryConfirmResult:
    """화면 표(⑤ 용어집 확정)의 용어를 검사하고, 문제가 없거나 확인하면 게임 용어집으로 확정한다.
    엑셀 가져오기(import_glossary)와 같은 검사·확정. 고친 값은 검증 엑셀에도 남긴다."""
    from app import glossary, workspace

    info = workspace.load_project_info(project_dir)
    try:
        save_glossary_rows(project_dir, rows)
    except OSError:  # 엑셀이 열려 있으면 못 쓴다: 확정에는 필요 없다
        pass
    return _confirm_review(info, glossary.check_rows(info, rows, first_row=1), accept_issues)


def _confirm_review(info, res, accept_issues: bool) -> GlossaryConfirmResult:
    from app import glossary, workspace

    if res.blocking:
        return GlossaryConfirmResult("blocked", blocking=res.blocking, issues=res.issues)
    if not res.terms:
        return GlossaryConfirmResult("blocked", blocking=["확정할 용어가 없습니다 (번역 열이 모두 비어 있음)"])
    if res.issues and not accept_issues:
        return GlossaryConfirmResult("needs_confirmation", terms=len(res.terms), issues=res.issues)
    prev_status = info.status
    c = glossary.confirm(info, res.terms)
    # 번역한 뒤 다시 가져와도 번역이 바뀐 용어가 없으면 번역 상태를 그대로 둔다 (바뀌면 다시 번역해야 함)
    # 용어집은 게임 단위: 같은 게임에서 용어집 검증 중이던 프로젝트도 함께 확정된다
    prevs = {}
    for p_ in workspace.game_projects(info):
        if p_.dir != info.dir and p_.status != "glossary_review":
            continue
        keep = p_.status in ("translating", "translated") and not c["changed"]
        if not keep and p_.status != "glossary_confirmed":
            prevs[str(p_.dir)] = p_.status
        workspace.set_status(p_, p_.status if keep else "glossary_confirmed", glossary_version=c["version"])
    return GlossaryConfirmResult("confirmed", c["version"], c["terms"], c["changed"], issues=res.issues,
                                 changed_detail=c.get("changed_detail", []), prev_status=prev_status,
                                 prev_statuses=prevs)


# ---------------------------------------------------------------- 용어 변경 일괄 반영 (PRD F20)
def _changed_terms(changes: list[dict]) -> list[dict]:
    """번역이 실제로 바뀐 용어만 (옛 번역이 비었거나 같으면 뺀다)."""
    return [c for c in changes or [] if c.get("old") and c.get("new") and c["old"] != c["new"]]


# 옛 번역 바로 뒤에 붙어도 낱말이 끝난 것으로 보는 조사 (긴 것 먼저). 이 밖의 한글이 붙으면 다른 낱말일 수 있다
_TERM_JOSA = ("으로서", "으로써", "에서", "에게", "까지", "부터", "보다", "처럼", "으로", "이다", "이며", "이고", "이나",
              "이라", "을", "를", "은", "는", "이", "가", "과", "와", "로", "에", "의", "도", "만", "나", "라", "며", "고",
              "다")
# 받침에 따라 바뀌는 조사: 조사 -> (받침 있을 때, 받침 없을 때)
_JOSA_PAIRS = {}
for _a, _b in (("을", "를"), ("은", "는"), ("이", "가"), ("과", "와"), ("이나", "나"), ("이라", "라"), ("이며", "며"),
               ("이고", "고"), ("이다", "다")):
    _JOSA_PAIRS[_a] = _JOSA_PAIRS[_b] = (_a, _b)


def _is_hangul(ch: str) -> bool:
    return bool(ch) and "가" <= ch <= "힣"


def _fit_josa(word: str, josa: str) -> str:
    """새 번역의 받침에 맞춘 조사 (금화를 -> 황금을, 마나나 -> 금이나). 받침과 상관없는 조사는 그대로."""
    last = word[-1:]
    if not josa or not _is_hangul(last):
        return josa
    final = (ord(last) - 0xAC00) % 28  # 받침 (0: 없음, 8: ㄹ)
    if josa in _JOSA_PAIRS:
        return _JOSA_PAIRS[josa][0 if final else 1]
    if josa in ("으로", "로"):
        return "으로" if final and final != 8 else "로"
    return josa


def _swap_text(text: str, hits: list[dict], protect: list[str]) -> tuple[str, dict, dict, str]:
    """글 하나에서 옛 번역을 새 번역으로 (긴 낱말 먼저, 한 번에 훑는다).
    protect(새 번역·같은 칸의 다른 용어 번역)에 든 자리는 건드리지 않는다 (장검 안의 검, 가스 트랩도어 안의 트랩).
    옛 번역 앞에 한글이 붙었거나 뒤에 조사가 아닌 한글이 붙은 자리는 다른 낱말일 수 있어 바꾸지 않는다.
    닫는 서식 태그 뒤의 조사도 본다 (<b>마나</b>를 -> <b>체력</b>을).
    돌려줌: (바꾼 글, {용어 원문: 바꾼 수}, {용어 원문: 못 바꾸고 남은 수 (두 글자 이상 옛 번역만)}, 보호한 자리를 지운 글).
    한 글자 옛 번역이 낱말 경계에 없으면 다른 낱말 안의 글자로 보고 세지 않는다 (지금 안의 금)."""
    import re

    words: dict[str, dict | None] = {w: None for w in protect if w}
    for c in hits:
        words.setdefault(c["old"], c)  # 다른 용어 번역과 같은 옛 번역은 보호가 먼저
    if not text or not words:
        return text, {}, {}, text or ""
    rx = re.compile("|".join(re.escape(w) for w in sorted(words, key=len, reverse=True)))
    out, masked, pos = [], [], 0
    swapped: dict[str, int] = {}
    left: dict[str, int] = {}
    for mo in rx.finditer(text):
        if mo.start() < pos:
            continue  # 앞에서 조사까지 바꾼 자리
        c = words[mo.group(0)]
        out.append(text[pos:mo.start()])
        masked.append(text[pos:mo.start()])
        pos = mo.end()
        if c is None:
            out.append(mo.group(0))
            masked.append("\0")
            continue
        key = c.get("source", c["old"])
        masked.append(mo.group(0))
        tags = re.match(r"(?:</[a-z0-9]+>)*", text[mo.end():]).group(0)
        rest = text[mo.end() + len(tags):]
        if not _is_hangul(rest[:1]):
            josa = ""
        else:
            josa = next((j for j in _TERM_JOSA if rest.startswith(j) and not _is_hangul(rest[len(j):len(j) + 1])), None)
        if josa is None or _is_hangul(text[mo.start() - 1] if mo.start() else ""):
            if len(c["old"]) >= 2:
                left[key] = left.get(key, 0) + 1  # 두 글자 이상이면 그 용어를 쓴 것일 수 있다 (직접 고칠 칸)
            out.append(mo.group(0))
            continue
        swapped[key] = swapped.get(key, 0) + 1
        out.append(c["new"] + tags + _fit_josa(c["new"], josa))
        masked.append(tags + josa)
        pos += len(tags) + len(josa)
    out.append(text[pos:])
    masked.append(text[pos:])
    return "".join(out), swapped, left, "".join(masked)


def _term_source_counts(text: str, terms: list[dict]) -> dict[str, int]:
    """원문에 용어마다 나온 횟수 (이형태 포함, translate.terms_in처럼 긴 용어가 차지한 자리는 빼고 센다)."""
    from app.glossary import is_phrase, occurrences

    out, rest = {}, text or ""
    for t in sorted(terms, key=lambda t: -len(t["source"])):
        forms = sorted([t["source"], *t.get("variants", [])], key=len, reverse=True)
        phrase = is_phrase(t["source"], t.get("category", ""))
        tmp, n = (text if phrase else rest), 0
        for f in forms:
            k = occurrences(f, tmp)
            if k:
                n += k
                tmp = tmp.replace(f, "　" * len(f))  # 긴 이형태 안의 짧은 이형태를 두 번 세지 않게
        if n:
            out[t["source"]] = n
            if not phrase:
                rest = tmp
    return out


def _term_plan(value, source: str, changes: list[dict], terms: list[dict]) -> dict:
    """한 번역(value: 글 또는 태그 목록)에 바뀐 용어를 어떻게 반영할지. 바뀜 후보와 캐시 고치기가 함께 쓴다.
    source: 원문 (용어 찾기는 확정 용어집 전체로 해서 긴 용어 안의 짧은 용어는 빼고), terms: 확정 용어집.
    자동으로 바꾸는 것(auto)은 빠짐없이 바꿀 수 있다고 확인될 때만이다: 옛 번역이 남은 용어마다 바꾼 수가 원문에
    나온 수와 같고, 못 바꾸고 남은 옛 번역·활용형이 없고, 한 글자 옛 번역이 아니고, 바뀐 용어끼리 옛·새 번역이
    겹치지 않을 때. 나머지는 직접 고칠 칸이다 (잘못 바꾸는 것보다 사람에게 맡기는 편이 낫다).
    {"hits": 원문에 든 바뀐 용어, "used": 옛 번역이 남은 용어 원문, "swapped": 실제로 바꾼 용어 원문(auto일 때),
     "auto", "proposed": 바꾼 번역(auto일 때만, 아니면 None)}."""
    from app.translate import ko_term_used

    by_src = {c["source"]: c for c in changes}
    counts = _term_source_counts(source, terms)
    hits = [by_src[s] for s in counts if s in by_src]
    none = {"hits": hits, "used": [], "swapped": [], "auto": False, "proposed": None}
    if not hits:
        return none
    items = [v for v in (value if isinstance(value, list) else [value]) if isinstance(v, str)]
    # 바뀐 용어끼리 옛·새 번역이 겹치면(검→방패와 방패→검, 검→장검과 장검→대검) 자동으로 바꾸지 않는다
    chain = any(a is not b and (a["new"] in b["old"] or b["old"] in a["new"]) for a in hits for b in hits)
    if chain:
        used = [c["source"] for c in hits if any(c["old"] in v or ko_term_used(c["old"], v) for v in items)]
        return {**none, "used": used}
    protect = [t["ko"] for t in terms if t["source"] in counts and t["source"] not in by_src and t.get("ko")]
    protect += [c["new"] for c in hits]
    new_items, swapped, left, inflected = [], {}, {}, set()
    for v in (value if isinstance(value, list) else [value]):
        if not isinstance(v, str):
            new_items.append(v)
            continue
        out, sw, lf, masked = _swap_text(v, hits, protect)
        new_items.append(out)
        for k, n in sw.items():
            swapped[k] = swapped.get(k, 0) + n
        for k, n in lf.items():
            left[k] = left.get(k, 0) + n
        for c in hits:  # 옛 번역이 활용된 꼴로만 남음 (버리다 -> 버린다): 직접 고칠 칸
            if c["old"] not in masked and ko_term_used(c["old"], masked):
                inflected.add(c["source"])
    used = [c["source"] for c in hits if c["source"] in set(swapped) | set(left) | inflected]
    auto = bool(used) and not left and not inflected and all(
        len(by_src[s]["old"]) >= 2 and swapped.get(s, 0) == counts[s] for s in used)
    if not auto:
        return {**none, "used": used}
    proposed = new_items if isinstance(value, list) else new_items[0]
    return {"hits": hits, "used": used, "swapped": used, "auto": True, "proposed": proposed}


def _swap_terms(value, changes: list[dict], protect: list[str] = ()):
    """옛 번역을 새 번역으로 바꾼 값 (바꿀 수 없는 자리는 그대로, 검사 없이). 태그 칸(목록)은 하나하나."""
    if isinstance(value, list):
        return [_swap_terms(v, changes, protect) for v in value]
    if not isinstance(value, str) or not changes:
        return value
    return _swap_text(value, changes, list(protect) + [c["new"] for c in changes])[0]


def _term_cells(project_dir: Path | str, changes: list[dict]) -> list[dict]:
    """같은 게임의 모든 프로젝트에서 원문에 바뀐 용어가 든 칸 (바뀜 후보가 아닌 칸도 candidate=False로 함께)."""
    from app import card_edits, glossary, workspace
    from app.excel_io import source_of
    from app.extract import load_extracted

    changes = _changed_terms(changes)
    if not changes:
        return []
    info = workspace.load_project_info(project_dir)
    terms = glossary.load_glossary(info).get("terms", [])
    out = []
    for p_ in workspace.game_projects(info):
        for fd in workspace.file_dirs(p_):
            try:
                proj = load_project(fd)
            except FileNotFoundError:
                continue  # 영역 템플릿이 없는 그룹
            extracted = load_extracted(fd)
            where = "review" if _translate_erase.review_xlsx_for(fd) else "draft"
            base_cards = None
            edits = card_edits.load(fd)
            for pno in workspace.included_pages(fd):
                for reg in proj.template.regions_for(pno):
                    if reg.kind not in ("text", "tag_pills"):
                        continue
                    src, _u = source_of(proj, extracted, pno, reg.name)
                    text = card_edits.norm(src)
                    if not text or not _term_plan("", text, changes, terms)["hits"]:
                        continue
                    if base_cards is None:  # 검수 엑셀은 느리므로 용어가 든 칸이 있을 때만 읽는다
                        base_cards = _review._translation_cards(fd)
                    entry = (base_cards.get(pno) or {}).get(reg.name)
                    base = entry.get("ko") if isinstance(entry, dict) else None
                    e = card_edits.edit_for(edits, pno, reg.name, base)
                    now = e["ko"] if e else base
                    plan = _term_plan(now, text, changes, terms)
                    out.append({"project": p_.name, "project_dir": p_.dir, "file_dir": fd,
                                "file_key": workspace.file_key(fd), "page": pno, "region": reg.name, "kind": reg.kind,
                                "source": text, "sources": list(src) if isinstance(src, list) else [src],
                                "current": card_edits.norm(now),
                                "proposed": card_edits.norm(plan["proposed"]) if plan["auto"] else None,
                                "where": where, "terms": plan["used"], "auto": plan["auto"],
                                "candidate": bool(plan["used"]), "_now": now, "_base": base,
                                "_proposed": plan["proposed"]})
    return out


def term_change_candidates(project_dir: Path | str, changes: list[dict]) -> list[dict]:
    """용어집에서 번역이 바뀐 용어(changes: GlossaryConfirmResult.changed_detail)가 든 칸의 바뀜 후보.
    같은 게임의 모든 프로젝트·파일에서 원문에 그 용어가 있고 지금 쓰는 번역(검수 엑셀 위에 고친 번역, 엑셀이
    없으면 초벌 번역)에 옛 번역이 남은 칸. [{project, file_dir, file_key, page, region, kind, source, current,
    proposed(바꿀 번역, 안전하게 바꿀 수 없으면 None), where(review|draft), terms[원문 용어], auto}].
    옛 번역이 활용된 꼴로만 있거나 다른 낱말에 붙어 있으면 auto=False (직접 고칠 칸)."""
    return [{k: v for k, v in c.items() if not k.startswith("_") and k not in ("candidate", "sources")}
            for c in _term_cells(project_dir, changes) if c["candidate"]]


@dataclass
class TermApplyResult:
    applied: int = 0  # 바꾼 칸
    skipped: int = 0  # 고른 칸 중 바꾸지 못한 칸 (직접 고쳐야 하거나 그 사이 번역이 바뀜)
    status_restored: bool = False  # 이 프로젝트는 바뀐 용어를 모두 반영해 다시 번역할 필요가 없음 (상태를 되돌림)
    restored: list[str] = field(default_factory=list)  # 상태를 되돌린 프로젝트 이름 (같은 게임의 다른 프로젝트 포함)


def apply_term_changes(project_dir: Path | str, picks: list[dict], changes: list[dict],
                       prev_status: str | dict | None = None) -> TermApplyResult:
    """고른 바뀜 후보(picks: term_change_candidates의 칸)에 새 용어 번역을 넣는다.
    검수 엑셀은 건드리지 않고(그림이 빠지므로) 고친 번역(card_edits)으로 둔다. 초벌 번역 캐시도 새 용어로 고쳐
    ⑥ 전체 번역이 그 칸을 다시 번역하지 않게 한다 (고르지 않은 칸은 지금 번역 그대로, 안전하게 바꿀 수 없는
    캐시 항목은 그대로 두어 ⑥이 다시 번역한다).
    prev_status: 가져오기 전 상태 — 글(이 프로젝트) 또는 {프로젝트 폴더: 상태} (GlossaryConfirmResult.prev_statuses).
    바뀐 용어가 든 번역이 모두 새 용어를 쓰게 된 프로젝트는 그 상태(번역 중·번역 완료)로 되돌린다."""
    from app import card_edits, font_plan, glossary, workspace
    from app.translate import _stale, check, load_cache, save_cache, terms_in, unique_sources

    changes = _changed_terms(changes)
    res = TermApplyResult()
    if not changes:
        return res
    info = workspace.load_project_info(project_dir)
    terms = glossary.load_glossary(info).get("terms", [])
    cells = _term_cells(info.dir, changes)
    want = {(str(Path(p["file_dir"]).resolve()), int(p["page"]), p["region"]) for p in picks or []}

    def ident(c):
        return str(Path(c["file_dir"]).resolve()), int(c["page"]), c["region"]
    picked = [c for c in cells if ident(c) in want and c["auto"]]
    res.skipped = len(want) - len(picked)
    picked_ids = {ident(c) for c in picked}
    # 번역 캐시는 프로젝트마다 따로: (프로젝트 폴더, 원문)으로 고른 것·고르지 않은 것을 나눈다
    declined = {(str(c["project_dir"]), s) for c in cells if c["auto"] and ident(c) not in picked_ids
                for s in c["sources"]}
    chosen = {(str(c["project_dir"]), s) for c in picked for s in c["sources"]}

    changed_sources: dict[str, set] = {}  # 프로젝트 폴더 -> 캐시를 고친 원문
    for p_ in workspace.game_projects(info):
        cache = load_cache(p_.dir)
        done = set()
        for s, entry in cache.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("ko"), str):
                continue
            if (str(p_.dir), s) in declined and (str(p_.dir), s) not in chosen:
                continue  # 고르지 않은 칸의 번역은 그대로
            plan = _term_plan(entry["ko"], s, changes, terms)
            if not plan["auto"]:
                continue  # 용어가 없거나 빠짐없이 바꿀 수 없음 (⑥이 다시 번역하게 그대로 둔다)
            ko = plan["proposed"]
            used = dict(entry.get("terms_used") or {})
            for src_ in plan["swapped"]:  # 이 항목에서 실제로 바꾼 용어만 새 번역을 쓴 것으로 적는다
                used[src_] = next(c["new"] for c in changes if c["source"] == src_)
            st, missing = check(s, ko, terms)
            entry.update(ko=ko, terms_used=used, status=st, missing_terms=missing)
            done.add(s)
        if done:
            save_cache(p_.dir, cache)
            changed_sources[str(p_.dir)] = done

    fresh: set[str] = set()
    for c in cells:  # 고친 번역(card_edits): 고른 칸은 바꿀 번역, 초벌 번역이 바뀐 칸은 지금 번역을 지킨다
        is_picked = ident(c) in picked_ids
        moved = bool(changed_sources.get(str(c["project_dir"]), set()) & set(c["sources"]))
        if not is_picked and not (c["where"] == "draft" and moved):
            continue
        if c["where"] == "review":
            base = c["_base"]  # 검수 엑셀 값 (캐시를 고쳐도 그대로라 엑셀을 다시 읽지 않는다)
        else:
            if str(c["file_dir"]) not in fresh:  # 초벌 번역 칸: 고친 캐시로 파일마다 한 번 다시 읽는다
                fresh.add(str(c["file_dir"]))
                _review._TRANSLATION_CARDS.pop(str(c["file_dir"]), None)
            entry = (_review._translation_cards(c["file_dir"]).get(c["page"]) or {}).get(c["region"])
            base = entry.get("ko") if isinstance(entry, dict) else None
        card_edits.set_text(c["file_dir"], c["page"], c["region"], c["_proposed"] if is_picked else c["_now"], base)
        res.applied += int(is_picked)

    prevs = prev_status if isinstance(prev_status, dict) else ({str(info.dir): prev_status} if prev_status else {})
    names = {c["source"] for c in changes}
    for p_ in workspace.game_projects(info):  # 프로젝트마다: 바뀐 용어가 든 캐시 번역이 모두 새 용어면 상태를 되돌린다
        prev = prevs.get(str(p_.dir))
        if prev not in ("translating", "translated") or p_.status != "glossary_confirmed":
            continue
        cache = load_cache(p_.dir)
        stale = False
        for s, *_ in unique_sources(glossary.collect_sources(p_)):
            e = cache.get(s)
            if e and any(t["source"] in names for t in terms_in(s, terms)) and _stale(e, s, terms):
                stale = True
                break
        if stale:
            continue
        workspace.set_status(p_, prev)
        res.restored.append(p_.name)
        res.status_restored = res.status_restored or p_.dir == info.dir
    _review._clear_review_caches()
    font_plan._raw_cache.clear()
    return res
