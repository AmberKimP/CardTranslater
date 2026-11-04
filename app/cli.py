"""명령줄 진입점. 처리 흐름은 app.service에 있고, 여기서는 인자를 받아 부르고 결과를 출력만 한다.

3단계 글자 추출 (Claude Opus 비전) -> projects/<p>/extract/  (끊겨도 같은 명령으로 이어 감)
    python -m app.cli extract --project projects/sample [--mode double-check|opus] [--engine claude-code|api]
    python -m app.cli extract --project projects/sample --test   (시험 폴더 test_runs/extract-sample-double-check)
    python -m app.cli extract-eval --project projects/sample [--from test_runs/extract-sample-double-check]
7단계 글자 지우기 -> projects/<p>/cleared/  (한 번만)
    python -m app.cli erase --project projects/sample --pages 1-30
번역 검수 엑셀 만들기 -> projects/<p>/번역_검수.xlsx
    python -m app.cli export-excel --project projects/sample
8단계 한글 넣기 -> projects/<p>/translated/  (지우기를 다시 하지 않음. 엑셀을 고칠 때마다 반복)
    python -m app.cli compose --project projects/sample --excel projects/sample/번역_검수.xlsx

--test를 붙이면 프로젝트 폴더 대신 test_runs/<YYYYMMDD-HHMMSS>-erase|compose/ 에 쓴다.
원본 PDF는 읽기만 한다. Ctrl+C로 멈추면 예전 결과 폴더는 그대로 남는다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app import service
from app.service import ProgressEvent


def _print_progress(prefix: str):
    def cb(ev: ProgressEvent) -> None:
        if ev.stage in ("erase", "compose", "extract", "recheck"):
            print(f"[{prefix}] {ev.done + 1}/{ev.total} {ev.message}", flush=True)
    return cb


def cmd_erase(args: argparse.Namespace) -> int:
    try:
        r = service.erase(args.project, args.pages, test=args.test, out_root=args.out_root,
                          progress=_print_progress("erase"))
    except KeyboardInterrupt:
        print("[erase] 멈춤. 예전 결과는 그대로입니다.", file=sys.stderr)
        return 130
    if not r.ok:
        print(f"[erase] 경고: {r.error}! 결과는 {r.output_dir}에 남겨 둠", file=sys.stderr)
        return 2
    print(f"[erase] 결과: {r.output_dir}")
    for pno, ws in r.warnings.items():
        for w in ws:
            print(f"  p{pno}: {w}")
    return 0


def cmd_compose(args: argparse.Namespace) -> int:
    try:
        r = service.compose(args.project, excel=args.excel, cleared=args.cleared, pages=args.pages,
                            accept_issues=args.yes, test=args.test, out_root=args.out_root,
                            progress=_print_progress("compose"))
    except KeyboardInterrupt:
        print("[compose] 멈춤. 예전 결과는 그대로입니다.", file=sys.stderr)
        return 130
    if args.excel:
        print(f"[compose] 엑셀 {args.excel}: {r.rows}행")
    for b in r.blocking:
        print(f"  [멈춤] {b}", file=sys.stderr)
    for i in r.issues:
        print(f"  [확인] {i}")
    if r.state == "blocked":
        return 1
    if r.state == "needs_confirmation":
        print("[compose] 위 항목을 확인한 뒤 계속하려면 --yes를 붙여 다시 실행하세요.", file=sys.stderr)
        return 1
    for status, keys in r.status_summary.items():
        print(f"  {status}: {len(keys)}  {', '.join(keys)}")
    if r.state == "failed":
        print(f"[compose] 경고: {r.error}! 결과는 {r.output_dir}에 남겨 둠", file=sys.stderr)
        return 2
    print(f"[compose] 결과: {r.output_dir}")
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    try:
        r = service.extract(args.project, args.pages, mode=args.mode, engine=args.engine, batch=args.batch,
                            budget_usd=args.budget, retry_failed=args.retry_failed, test=args.test,
                            out_root=args.out_root, progress=_print_progress("extract"))
    except KeyboardInterrupt:
        print("[extract] 멈춤. 읽은 카드는 저장되어 있고, 다시 실행하면 이어 갑니다.", file=sys.stderr)
        return 130
    if r.model:
        print(f"[extract] 엔진 {r.engine}, 모드 {r.mode} (1차 {r.model}), 결과 폴더 {r.output_dir}")
    cost = (f"Claude Code 요금제 사용 (API 정가 환산 약 ${r.equiv_usd:.3f}, 따로 청구 안 됨)"
            if r.engine == "claude-code" else f"쓴 비용 약 ${r.spent_usd:.3f}")
    print(f"[extract] 읽음 {len(r.done_pages)}장, 남음 {len(r.remaining_pages)}장, 실패 {len(r.failed)}장, {cost}")
    if r.recheck_cells:
        print(f"[extract] 두 번 확인: {r.recheck_cells}칸을 Opus로 다시 읽음 -> 같음 {r.recheck_agreed}칸(확인 필요 해제), "
              f"다름 {r.recheck_disagreed}칸(확인 필요 유지)")
    for pno, why in sorted(r.failed.items()):
        print(f"  [실패] p{pno}: {why}  (--retry-failed로 다시)")
    if r.state == "blocked":
        hint = ("Claude Code에 로그인되어 있는지 확인하세요 (`claude` 실행 후 /login)." if r.engine == "claude-code"
                else ".env에 ANTHROPIC_API_KEY=... 를 넣거나 `ant auth login` 후 다시 실행하세요.")
        print(f"[extract] {r.reason}\n  {hint}", file=sys.stderr)
        return 1
    if r.state == "paused":
        when = f" {r.retry_after} 이후" if r.retry_after else ""
        print(f"[extract] 일시 정지: {r.reason}.{when} 같은 명령을 다시 실행하면 이어 갑니다.", file=sys.stderr)
        return 3
    if r.state == "waiting_batch":
        print(f"[extract] {r.reason}")
        return 0
    print("[extract] 완료")
    return 0


def cmd_extract_eval(args: argparse.Namespace) -> int:
    ev = service.evaluate_extract(args.project, args.from_dir)
    if not ev["cells"]:
        print("[extract-eval] 비교할 칸이 없습니다 (추출 결과와 samples.json 원문이 둘 다 있는 칸)")
        return 1
    print(f"[extract-eval] {ev['cells']}칸: 완전 일치 {ev['exact_rate']:.1%}, 글자 단위 평균 {ev['char_acc_mean']:.1%}, "
          f"틀린 칸 {ev['wrong']}개 중 '확인 필요' 표시 {ev['wrong_flagged_uncertain']}개")
    for r in ev["rows"]:
        if not r["exact"]:
            mark = "확인필요" if r["uncertain"] else "        "
            print(f"  {mark} {r['key']}: {r['char_acc']:.0%}\n      읽음  {r['got']!r}\n      정답  {r['truth']!r}")
    return 0


def cmd_learn_tag_colors(args: argparse.Namespace) -> int:
    vocab, warns = service.learn_tag_colors(args.project, save=args.save)
    for name, items in vocab.items():
        print(f"[learn-tag-colors] {name}: " + ", ".join(f"{v['text']}={v['color']}({v['samples']})" for v in items))
    for w in warns:
        print(f"  [확인] {w}")
    if not vocab:
        print("[learn-tag-colors] 배울 카드가 없습니다 (태그 글자를 아는 카드가 필요)", file=sys.stderr)
        return 1
    print("[learn-tag-colors] regions.json에 저장함" if args.save else "[learn-tag-colors] 저장하려면 --save")
    return 0


def cmd_export_excel(args: argparse.Namespace) -> int:
    try:
        out = service.export_excel(args.project, args.out, args.pages, force=args.force)
    except FileExistsError as e:
        print(f"[export-excel] 이미 있습니다: {e}\n  검수한 내용을 덮어쓰지 않도록 멈춥니다. 덮어쓰려면 --force.",
              file=sys.stderr)
        return 1
    print(f"[export-excel] {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("erase", help="영역 글자 지우기 (레드랙션 + 배경 이미지 정리 + 태그 라벨 다시 그리기)")
    e.add_argument("--project", required=True)
    e.add_argument("--pages", required=True, help="예: 1-3 또는 1,5,12-14")
    e.add_argument("--test", action="store_true", help="프로젝트 cleared/ 대신 test_runs/<시각>-erase/ 에 쓴다")
    e.add_argument("--out-root", default="test_runs")
    e.set_defaults(func=cmd_erase)
    t = sub.add_parser("extract", help="3단계: 영역 원문을 Claude Opus로 읽기 (끊기면 다시 실행해 이어 감)")
    t.add_argument("--project", required=True)
    t.add_argument("--pages", help="기본: 전체 쪽")
    t.add_argument("--engine", default="claude-code", choices=["claude-code", "api"],
                   help="claude-code(기본: 로그인된 Claude Code 요금제, 키 불필요) | "
                        "api(API 키 또는 ant auth login, 사용료 별도)")
    t.add_argument("--batch", action="store_true",
                   help="[api 엔진] 일괄 처리(50%% 할인, 결과는 보통 1시간 안). 다시 실행해 결과를 받음")
    t.add_argument("--budget", type=float, help="[api 엔진] 이번 프로젝트 추출에 쓸 최대 비용(달러). 닿으면 일시 정지")
    t.add_argument("--retry-failed", action="store_true", help="실패로 표시된 카드도 다시 읽는다")
    t.add_argument("--mode", default="double-check", choices=["double-check", "opus"],
                   help="double-check(기본: Sonnet으로 읽고 '확인 필요' 칸만 Opus로 다시 읽어 비교) | "
                        "opus(Opus만, 가장 정확하지만 요금제 한도를 빨리 씀)")
    t.add_argument("--test", action="store_true",
                   help="프로젝트 extract/ 대신 test_runs/extract-<프로젝트>-<모드>/ 에 쓴다 (비교용, 이어 하기 가능)")
    t.add_argument("--out-root", default="test_runs")
    t.set_defaults(func=cmd_extract)
    v = sub.add_parser("extract-eval", help="추출 결과를 samples.json(손으로 옮긴 정답)과 비교")
    v.add_argument("--project", required=True)
    v.add_argument("--from", dest="from_dir", help="비교할 추출 폴더 (기본: 프로젝트 extract/)")
    v.set_defaults(func=cmd_extract_eval)
    g = sub.add_parser("learn-tag-colors", help="태그 '라벨 색 -> 글자' 사전 배우기 (글자를 아는 카드에서)")
    g.add_argument("--project", required=True)
    g.add_argument("--save", action="store_true", help="regions.json의 태그 영역에 저장")
    g.set_defaults(func=cmd_learn_tag_colors)
    x = sub.add_parser("export-excel", help="번역 검수 엑셀(번역_검수.xlsx) 만들기")
    x.add_argument("--project", required=True)
    x.add_argument("--pages", help="기본: 전체 쪽")
    x.add_argument("--out", help="기본: 프로젝트/번역_검수.xlsx")
    x.add_argument("--force", action="store_true", help="이미 있는 엑셀을 덮어쓴다")
    x.set_defaults(func=cmd_export_excel)
    c = sub.add_parser("compose", help="8단계: 지운 카드(cleared/) 위에 한글 넣기 (지우기는 다시 하지 않음)")
    c.add_argument("--project", required=True)
    c.add_argument("--excel", help="검수한 번역 엑셀. 없으면 samples.json의 한글 초안")
    c.add_argument("--cleared", "--erase-run", dest="cleared",
                   help="지운 결과 폴더. 기본: 프로젝트/cleared/ (테스트 실행 폴더도 가능)")
    c.add_argument("--pages", help="기본: 지운 결과에 있는 쪽 전부")
    c.add_argument("--yes", action="store_true", help="엑셀 확인 항목이 있어도 계속한다")
    c.add_argument("--test", action="store_true", help="프로젝트 translated/ 대신 test_runs/<시각>-compose/ 에 쓴다")
    c.add_argument("--out-root", default="test_runs")
    c.set_defaults(func=cmd_compose)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
