"""명령줄 진입점. 처리 흐름은 app.service에 있고, 여기서는 인자를 받아 부르고 결과를 출력만 한다.

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
        if ev.stage in ("erase", "compose"):
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
