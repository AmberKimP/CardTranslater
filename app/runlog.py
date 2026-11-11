"""⑦ 글자 지우기·⑧ 한글 넣기 결과를 로그로 요약한다 (문제를 받았을 때 고칠 곳과 걸린 시간을 알 수 있게).

쪽마다 걸린 시간은 지우기 메타(page meta의 "timing")·한글 넣기 기록(pages_detail의 "timing")에 남긴다.
여러 프로세스로 나눠 돌려도(app.parallel) 작업 프로세스의 로그는 파일에 남지 않으므로, 주 프로세스가
이 기록을 모아 한꺼번에 남긴다. 화면 코드 없음.
"""

from __future__ import annotations

import logging

from app.applog import TIME_TAG

log = logging.getLogger("cardtranslater.run")
tlog = logging.getLogger("cardtranslater.time")

# 한글 넣기 상태 중 사람이 봐야 하는 것 (‘글자 축소’처럼 흔하고 문제없는 것은 개수만 센다)
COMPOSE_PROBLEMS = ("넘침", "서식 불일치", "아이콘 오류", "아이콘 불일치", "빈 영역", "원문 글자 남음", "글꼴 없음",
                    "줄 자리 대신 영역 전체", "결론 못 냄")
MAX_LINES = 40  # 문제 줄을 이만큼까지만 (로그가 너무 길어지지 않게)


def _slowest(per_page: dict[int, float], k: int = 3) -> str:
    top = sorted(per_page.items(), key=lambda kv: -kv[1])[:k]
    return ", ".join(f"{p}쪽 {s:.1f}초" for p, s in top if s > 0)


def _sum(per_page: dict[int, float]) -> str:
    if not per_page:
        return ""
    vals = list(per_page.values())
    return f"{sum(vals):.1f}초 (평균 {sum(vals) / len(vals):.2f}초, 최대 {max(vals):.2f}초)"


def log_erase(name: str, page_metas: list[dict], wall: float, workers: int) -> None:
    """지우기 한 번: 쪽마다 주의할 점(그림 배경·찾은 글자가 적음·원문에 없어 남긴 조각 …)과 단계별 시간."""
    erase_t, verify_t = {}, {}
    lines = 0
    notes: dict[tuple[str, str], list] = {}  # (영역, 참고) -> [쪽 (방식)]: 같은 참고는 한 줄로 모은다
    for m in page_metas:
        p = m.get("source_page")
        t = m.get("timing") or {}
        erase_t[p], verify_t[p] = float(t.get("erase_s") or 0), float(t.get("verify_s") or 0)
        for w in m.get("warnings") or []:
            if lines < MAX_LINES:
                log.warning("[%s] %s쪽 지우기 경고: %s", name, p, w)
                lines += 1
        for r in m.get("regions") or []:
            tmm = ((r.get("erase") or {}).get("text_mask")) or {}
            bits = list((r.get("erase") or {}).get("flags") or [])
            if tmm.get("few_text"):
                bits.append("글자를 거의 못 찾음 (영역 위치 확인)")
            bits += [n for n in tmm.get("notes") or [] if "지우지 않음" in n or "더 찾아" in n]
            used = tmm.get("used") or (r.get("erase") or {}).get("method") or ""
            for b in bits:
                notes.setdefault((str(r.get("name")), b), []).append(f"{p}" + (f"({used})" if used else ""))
    for (region, note), pages in sorted(notes.items(), key=lambda kv: (kv[0][0], -len(kv[1]))):
        log.info("[%s] 지우기 참고 %s: %s — %d쪽: %s", name, region, note, len(pages), ", ".join(pages[:30]))
    parts = [f"쪽 {len(page_metas)}", f"동시 {workers}" if workers > 1 else "",
             f"지우기 {_sum(erase_t)}" if any(erase_t.values()) else "",
             f"확인·비교 이미지 {_sum(verify_t)}" if any(verify_t.values()) else "",
             f"느린 쪽 {_slowest({p: erase_t[p] + verify_t.get(p, 0) for p in erase_t})}"]
    tlog.info("%s ⑦ 글자 지우기 [%s] %.1f초 | %s", TIME_TAG, name, wall, " | ".join(x for x in parts if x))


def log_compose(name: str, page_recs: list[dict], wall: float, workers: int) -> None:
    """한글 넣기 한 번: 문제 칸(쪽·영역·상태·이유)과 상태별 개수, 단계별 시간."""
    counts: dict[str, int] = {}
    comp_t, rend_t = {}, {}
    lines = 0
    for pr in page_recs:
        p = pr.get("page")
        t = pr.get("timing") or {}
        comp_t[p], rend_t[p] = float(t.get("compose_s") or 0), float(t.get("render_s") or 0)
        for rname, rec in (pr.get("regions") or {}).items():
            st = list(rec.get("status") or [])
            for s in st:
                counts[s] = counts.get(s, 0) + 1
            bad = [s for s in st if s in COMPOSE_PROBLEMS]
            if bad and lines < MAX_LINES:
                why = [str(x) for x in (rec.get("errors") or []) + (rec.get("warnings") or [])][:3]
                if rec.get("overflow_text"):
                    why.append(f"빠진 글자 «{str(rec['overflow_text'])[:40]}»")
                font = str(rec.get("font_file") or "").replace("\\", "/").split("/")[-1]
                log.warning("[%s] %s쪽 %s 한글 넣기: %s%s%s", name, p, rname, ", ".join(bad),
                            f" — {'; '.join(why)}" if why else "", f" (글꼴 {font})" if font else "")
                lines += 1
    if lines >= MAX_LINES:
        log.warning("[%s] 문제 칸이 많아 앞 %d개만 적었습니다 (report.json의 status_summary 참고)", name, MAX_LINES)
    parts = [f"쪽 {len(page_recs)}", f"동시 {workers}" if workers > 1 else "",
             f"한글 넣기 {_sum(comp_t)}" if any(comp_t.values()) else "",
             f"미리보기·비교 이미지 {_sum(rend_t)}" if any(rend_t.values()) else "",
             f"느린 쪽 {_slowest({p: comp_t[p] + rend_t.get(p, 0) for p in comp_t})}",
             ("상태 " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))) if counts else ""]
    tlog.info("%s ⑧ 한글 넣기 [%s] %.1f초 | %s", TIME_TAG, name, wall, " | ".join(x for x in parts if x))
