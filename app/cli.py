"""명령줄 진입점. 처리 흐름은 app.service에 있고, 여기서는 인자를 받아 부르고 결과를 출력만 한다.

게임·프로젝트·그룹 (PRD 2장)
    python -m app.cli new-project --name 던전1차 [--game Dungeon]      (게임 목록을 보여 주고 고르게 함)
    python -m app.cli add-files --project projects/던전1차 --files a.pdf b.pdf   (그룹 목록을 보여 주고 고르게 함)
    python -m app.cli add-files --project projects/던전1차 --files s1.jpg s2.jpg --cols 3 --rows 3 --name 시트   (이미지)
    python -m app.cli extract-project --project projects/던전1차 [--group 아이템]
    python -m app.cli finish-extraction --project projects/던전1차        (‘추출 완료’ -> 용어집_검증.xlsx)
    python -m app.cli find-icons --project projects/던전1차               (이름 없는 아이콘 -> 아이콘_검증.xlsx)
    python -m app.cli import-icons --project projects/던전1차 --excel projects/던전1차/아이콘_검증.xlsx
    python -m app.cli import-glossary --project projects/던전1차 --excel projects/던전1차/용어집_검증.xlsx
    python -m app.cli translate --project projects/던전1차                (확정 용어집 기준 초벌 번역)
    python -m app.cli export-excel --project projects/던전1차             (파일마다 번역_검수.xlsx)
    python -m app.cli erase-project --project projects/던전1차            (7단계, 모든 파일)
    python -m app.cli compose-project --project projects/던전1차          (8단계, 검수 엑셀 또는 초벌 번역)
    python -m app.cli set-pages --project projects/던전1차 --file 아이템/Items --exclude 1   (뺄 쪽)

3단계 글자 추출 (파일 하나) -> <파일 폴더>/extract/  (끊겨도 같은 명령으로 이어 감)
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
        if ev.stage in ("erase", "compose", "extract", "recheck", "translate"):
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
    if (Path(args.project) / "project.json").exists():  # 게임 프로젝트: 파일마다 하나씩
        try:
            outs = service.export_project_excel(args.project, force=args.force)
        except FileExistsError as e:
            print(f"[export-excel] 이미 있습니다: {e}\n  덮어쓰려면 --force.", file=sys.stderr)
            return 1
        for o in outs:
            print(f"[export-excel] {o}")
        return 0
    try:
        out = service.export_excel(args.project, args.out, args.pages, force=args.force)
    except FileExistsError as e:
        print(f"[export-excel] 이미 있습니다: {e}\n  검수한 내용을 덮어쓰지 않도록 멈춥니다. 덮어쓰려면 --force.",
              file=sys.stderr)
        return 1
    print(f"[export-excel] {out}")
    return 0


def _ask(prompt: str, choices: list[str]) -> str | None:
    """번호로 고르게 한다 (입력이 없거나 대화형이 아니면 None)."""
    if not sys.stdin.isatty():
        return None
    for i, c in enumerate(choices, start=1):
        print(f"  {i}. {c}")
    try:
        a = input(prompt).strip()
    except EOFError:
        return None
    if a.isdigit() and 1 <= int(a) <= len(choices):
        return choices[int(a) - 1]
    return a or None


def cmd_new_project(args: argparse.Namespace) -> int:
    games = service.list_games()
    game = args.game
    if not game:
        print("[new-project] 게임을 고르세요 (번호), 또는 새 게임 이름을 입력하세요:")
        if not games:
            print("  (아직 게임이 없습니다)")
        game = _ask("게임: ", [g["name"] for g in games])
        if not game:
            names = ", ".join(g["name"] for g in games) or "없음"
            print(f"[new-project] --game 으로 게임 이름을 주세요. 이미 있는 게임: {names}", file=sys.stderr)
            return 1
    try:
        info, new_game = service.new_project(args.name, game, args.lang)
    except FileExistsError as e:
        print(f"[new-project] 같은 이름의 프로젝트가 이미 있습니다: {e}", file=sys.stderr)
        return 1
    print(f"[new-project] {'새 게임' if new_game else '기존 게임'} '{info.game}'에 프로젝트 '{info.name}'을 만듦: {info.dir}")
    return 0


def _print_groups(groups: list[dict]) -> None:
    if not groups:
        print("  (아직 그룹이 없습니다)")
    for g in groups:
        reg = f"영역 {g['regions']}개" if g["regions"] else "영역 미지정"
        print(f"  - {g['name']}: 파일 {len(g['files'])}개 ({', '.join(g['files']) or '-'}), {reg}")


def cmd_add_files(args: argparse.Namespace) -> int:
    groups = service.list_groups(args.project)
    print("[add-files] 이 프로젝트의 그룹:")
    _print_groups(groups)
    group, new = args.group, args.new_group
    if group is None:
        print("넣을 그룹을 고르세요 (번호), 또는 새 그룹 이름을 입력하세요:")
        group = _ask("그룹: ", [g["name"] for g in groups])
        if not group:
            print("[add-files] --group <기존 그룹> 또는 --group <새 이름> --new-group 으로 정하세요.", file=sys.stderr)
            return 1
        new = group not in [g["name"] for g in groups]
    from app.images import is_image

    imgs = [f for f in args.files if is_image(f)]
    pdfs = [f for f in args.files if not is_image(f)]
    try:
        r = {"added": [], "skipped": [], "has_regions": False}
        if pdfs:
            r = service.add_files(args.project, pdfs, group, new_group=new, regions_from=args.regions_from)
        if imgs:  # 이미지는 카드 한 장이 한 쪽인 PDF 하나로 묶어 넣는다 (배열이면 --cols --rows)
            ri = service.add_images(args.project, imgs, group, cols=args.cols, rows=args.rows, name=args.name,
                                    new_group=new and not pdfs, regions_from=None if pdfs else args.regions_from)
            if ri.file_dir:
                r["added"].append(ri.file_dir)
                print(f"[add-files] 이미지 {len(imgs)}장 -> 카드 {ri.pages}장 ({ri.file_dir.name})")
            for w in ri.warnings:
                print(f"  [확인] {w}")
            r["skipped"] += ri.skipped
            r["has_regions"] = ri.has_regions
    except KeyError:
        print(f"[add-files] 그룹 '{group}'이 없습니다. 새 그룹이면 --new-group 을 붙이세요.", file=sys.stderr)
        return 1
    except FileExistsError:
        print(f"[add-files] 그룹 '{group}'이 이미 있습니다. 기존 그룹에 넣으려면 --new-group 을 빼세요.", file=sys.stderr)
        return 1
    print(f"[add-files] '{group}'{' (새 그룹)' if new else ''}에 {len(r['added'])}개 넣음")
    for sk in r["skipped"]:
        print(f"  [건너뜀] {sk}")
    if not r["has_regions"]:
        print("  [확인] 이 그룹은 영역 템플릿이 없습니다. 추출 전에 영역을 지정하세요 (--regions-from regions.json).")
    return 0


def cmd_extract_project(args: argparse.Namespace) -> int:
    if args.limit is not None:
        n = service.set_card_limit(args.project, args.limit)
        print(f"[extract-project] 시험 모드: 파일마다 앞 {n}장만 (번역·검수·지우기·한글 넣기도 그 카드만)" if n
              else "[extract-project] 시험 모드 끔: 모든 카드")
    try:
        r = service.extract_project(args.project, args.group, mode=args.mode, engine=args.engine,
                                    progress=_print_progress("extract"))
    except KeyboardInterrupt:
        print("[extract-project] 멈춤. 다시 실행하면 이어 갑니다.", file=sys.stderr)
        return 130
    for f in r.files:
        print(f"  {f['file']}: {f['state']}" + (f", 읽음 {f['done']}장, 남음 {f['remaining']}장, 실패 {f['failed']}장, "
                                              f"두 번 확인 같음 {f['recheck'][0]}·다름 {f['recheck'][1]}"
                                              if "done" in f else f" ({f.get('reason', '')})"))
    if r.state == "done":
        print("[extract-project] 완료. 모든 그룹이 끝나면 finish-extraction 으로 ‘추출 완료’를 보내세요.")
        return 0
    when = f" {r.retry_after} 이후" if r.retry_after else ""
    print(f"[extract-project] {r.state}: {r.reason}.{when} 다시 실행하면 이어 갑니다.", file=sys.stderr)
    return 3 if r.state == "paused" else 1


def cmd_finish_extraction(args: argparse.Namespace) -> int:
    print("[finish-extraction] 모든 그룹·파일의 원문을 모아 용어집 후보를 만듭니다")
    r = service.finish_extraction(args.project, accept_issues=args.yes, engine=args.engine)
    for i in r.issues:
        print(f"  [확인] {i}")
    if r.state == "needs_confirmation":
        print("[finish-extraction] 위 항목을 확인한 뒤 계속하려면 --yes를 붙여 다시 실행하세요.", file=sys.stderr)
        return 1
    if r.state in ("blocked", "paused"):
        when = f" {r.retry_after} 이후 다시 실행하세요." if r.retry_after else ""
        print(f"[finish-extraction] {r.state}: {r.reason}.{when}", file=sys.stderr)
        return 3 if r.state == "paused" else 1
    if r.state == "icons_review":
        print(f"[finish-extraction] 먼저 아이콘 이름을 정해야 합니다: {r.reason}")
        print(f"[finish-extraction] 아이콘 검증 엑셀: {r.review_xlsx}")
        print("  이름·뜻·한국어 이름을 확인한 뒤 import-icons 로 가져오고 finish-extraction 을 다시 실행하세요.")
        return 2
    print(f"[finish-extraction] 새 용어 후보 {r.new_terms}개, 이미 확정된 용어 {r.confirmed_terms}개")
    print(f"[finish-extraction] 용어집 검증 엑셀: {r.review_xlsx}")
    print("  B열(번역)을 확인·수정한 뒤 import-glossary 로 가져오면 용어집이 확정됩니다.")
    return 0


def cmd_find_icons(args: argparse.Namespace) -> int:
    r = service.find_icons(args.project, engine=args.engine, suggest=not args.no_suggest)
    if r.auto:
        print("[find-icons] 아이콘 사전과 같아 바로 바꿈: " + ", ".join(f"[[{n}]] {c}곳" for n, c in r.auto.items()))
    if r.state == "done":
        print("[find-icons] 이름을 정할 아이콘이 없습니다.")
        return 0
    print(f"[find-icons] 이름을 정할 묶음 {r.groups}개 ({r.occurrences}곳)"
          + (f", 위치 못 찾음 {r.not_found}곳" if r.not_found else ""))
    if r.reason:
        print(f"  [확인] {r.reason}")
    print(f"[find-icons] 아이콘 검증 엑셀: {r.review_xlsx}")
    print("  이름(영문 소문자)·뜻·한국어 이름을 확인한 뒤 import-icons 로 가져오세요.")
    return 2


def cmd_import_icons(args: argparse.Namespace) -> int:
    r = service.import_icons(args.project, args.excel)
    for b in r.blocking:
        print(f"  [멈춤] {b}", file=sys.stderr)
    if r.state == "blocked":
        return 1
    if r.added:
        print(f"[import-icons] 사전에 새로 넣음: {', '.join(r.added)}")
    if r.reused:
        print(f"[import-icons] 사전에 있던 아이콘: {', '.join(r.reused)}")
    print(f"[import-icons] 원문에서 [[?]] {r.replaced}곳을 이름으로 바꾸고 {r.removed}곳을 뺐습니다.")
    print("  이제 finish-extraction 을 다시 실행하세요 (이미 번역했다면 translate 로 바뀐 칸만 다시 번역).")
    return 0


def cmd_import_glossary(args: argparse.Namespace) -> int:
    r = service.import_glossary(args.project, args.excel, accept_issues=args.yes)
    for b in r.blocking:
        print(f"  [멈춤] {b}", file=sys.stderr)
    for i in r.issues:
        print(f"  [확인] {i}")
    if r.state == "blocked":
        return 1
    if r.state == "needs_confirmation":
        print("[import-glossary] 위 항목을 확인한 뒤 확정하려면 --yes를 붙여 다시 실행하세요.", file=sys.stderr)
        return 1
    print(f"[import-glossary] 게임 용어집 확정: 버전 {r.version}, 용어 {r.terms}개")
    if r.changed:
        print(f"  [확인] 번역이 바뀐 확정 용어: {', '.join(r.changed)} (이 용어가 든 번역은 다시 해야 합니다)")
    print("  이제 번역(5단계)을 시작할 수 있습니다.")
    return 0


def cmd_translate(args: argparse.Namespace) -> int:
    try:
        r = service.translate_project(args.project, model=args.model, engine=args.engine,
                                      progress=_print_progress("translate"))
    except KeyboardInterrupt:
        print("[translate] 멈춤. 번역한 칸은 저장되어 있고, 다시 실행하면 이어 갑니다.", file=sys.stderr)
        return 130
    if r.state == "blocked":
        print(f"[translate] {r.reason}", file=sys.stderr)
        return 1
    print(f"[translate] 서로 다른 원문 {r.cells}칸: 이번에 번역 {r.translated}, 용어집으로 바로 {r.from_glossary}, "
          f"이미 번역됨 {r.cached}")
    if r.term_mismatch:
        print(f"  [확인] 용어 불일치 {len(r.term_mismatch)}칸 (엑셀 상태에 표시): " + ", ".join(r.term_mismatch[:5]))
    if r.icon_mismatch:
        print(f"  [확인] 아이콘 불일치 {len(r.icon_mismatch)}칸")
    if r.leftover:
        print(f"  [확인] 일본어가 남은 번역 {len(r.leftover)}칸: " + ", ".join(r.leftover[:5]))
    if r.state == "paused":
        when = f" {r.retry_after} 이후" if r.retry_after else ""
        print(f"[translate] 일시 정지: {r.reason}.{when} 다시 실행하면 이어 갑니다.", file=sys.stderr)
        return 3
    print("[translate] 완료. export-excel --project <프로젝트> 로 번역 검수 엑셀을 만드세요.")
    return 0


def cmd_set_pages(args: argparse.Namespace) -> int:
    from app import workspace

    info = workspace.load_project_info(args.project)
    fds = {workspace.file_key(fd): fd for fd in workspace.file_dirs(info)}
    if args.file not in fds:
        print(f"[set-pages] 파일이 없습니다: {args.file}. 있는 파일: {', '.join(fds) or '없음'}", file=sys.stderr)
        return 1
    fd = fds[args.file]
    from app.project import parse_page_spec

    excl = parse_page_spec(args.exclude, workspace.page_count(fd)) if args.exclude else []
    workspace.set_excluded_pages(fd, excl, args.reason or "")
    inc = workspace.included_pages(fd, limited=False)
    print(f"[set-pages] {args.file}: 뺀 쪽 {workspace.pages_spec(excl) or '없음'}, 처리할 쪽 {len(inc)}장 "
          f"({workspace.pages_spec(inc)})")
    return 0


def _print_step(tag: str, r) -> int:
    for f in r.files:
        extra = ", ".join(f"{k}={v}" for k, v in f.items() if k not in ("file", "state", "dir", "issues", "blocking") and v)
        print(f"  {f['file']}: {f['state']}" + (f" ({extra})" if extra else ""))
        for b in f.get("blocking") or []:
            print(f"    [멈춤] {b}", file=sys.stderr)
        for i in f.get("issues") or []:
            print(f"    [확인] {i}")
    if r.state == "done":
        print(f"[{tag}] 완료")
        return 0
    print(f"[{tag}] {r.state}: {r.reason}", file=sys.stderr)
    return 1


def cmd_erase_project(args: argparse.Namespace) -> int:
    return _print_step("erase-project", service.erase_project(args.project, args.group, redo=args.redo,
                                                               progress=_print_progress("erase")))


def cmd_compose_project(args: argparse.Namespace) -> int:
    r = service.compose_project(args.project, args.group, accept_issues=args.yes, use_draft=args.draft,
                                progress=_print_progress("compose"))
    code = _print_step("compose-project", r)
    if r.state == "needs_confirmation":
        print("  확인한 뒤 계속하려면 --yes를 붙여 다시 실행하세요.", file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("erase", help="영역 글자 지우기 (레드랙션 + 배경 이미지 정리 + 태그 라벨 다시 그리기)")
    e.add_argument("--project", required=True)
    e.add_argument("--pages", required=True, help="예: 1-3 또는 1,5,12-14")
    e.add_argument("--test", action="store_true", help="프로젝트 cleared/ 대신 test_runs/<시각>-erase/ 에 쓴다")
    e.add_argument("--out-root", default="test_runs")
    e.set_defaults(func=cmd_erase)
    t = sub.add_parser("extract", help="3단계(파일 하나): 영역 원문을 Claude로 읽기 (끊기면 다시 실행해 이어 감)")
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
    n = sub.add_parser("new-project", help="0단계: 새 게임 또는 기존 게임으로 프로젝트 만들기")
    n.add_argument("--name", required=True, help="프로젝트 이름")
    n.add_argument("--game", help="게임 이름 (없으면 기존 게임 목록에서 고르게 함; 처음 쓰는 이름이면 새 게임)")
    n.add_argument("--lang", default="ja", choices=["ja", "en"], help="원문 언어")
    n.set_defaults(func=cmd_new_project)
    a = sub.add_parser("add-files", help="1단계: 파일을 그룹에 넣기 (기존 그룹 목록을 보여 줌)")
    a.add_argument("--project", required=True)
    a.add_argument("--files", nargs="+", required=True)
    a.add_argument("--group", help="넣을 그룹 (없으면 목록에서 고르게 함)")
    a.add_argument("--new-group", action="store_true", help="--group 이름으로 새 그룹을 만든다")
    a.add_argument("--regions-from", help="새 그룹의 영역 템플릿으로 복사할 regions.json")
    a.add_argument("--cols", type=int, default=1, help="이미지 한 장에 가로로 놓인 카드 수 (배열 이미지)")
    a.add_argument("--rows", type=int, default=1, help="이미지 한 장에 세로로 놓인 카드 수 (배열 이미지)")
    a.add_argument("--name", help="이미지를 묶은 파일 이름 (기본: 첫 이미지 이름)")
    a.set_defaults(func=cmd_add_files)
    ep = sub.add_parser("extract-project", help="3단계: 그룹(없으면 전체)의 모든 파일에서 텍스트 추출")
    ep.add_argument("--project", required=True)
    ep.add_argument("--group")
    ep.add_argument("--mode", default="double-check", choices=["double-check", "opus"])
    ep.add_argument("--engine", default="claude-code", choices=["claude-code", "api"])
    ep.add_argument("--limit", type=int, help="시험 모드: 파일마다 앞 X장만 추출·번역·검수 (0이면 끄고 전체). 프로젝트에 저장됨")
    ep.set_defaults(func=cmd_extract_project)
    fx = sub.add_parser("finish-extraction", help="‘추출 완료’: 모은 원문으로 용어집 후보 -> 용어집_검증.xlsx")
    fx.add_argument("--project", required=True)
    fx.add_argument("--yes", action="store_true", help="남은 카드·결론 못 낸 칸이 있어도 계속")
    fx.add_argument("--engine", default="claude-code", choices=["claude-code", "api"])
    fx.set_defaults(func=cmd_finish_extraction)
    sp = sub.add_parser("set-pages", help="파일에서 뺄 쪽 지정 (그룹 형식과 다른 쪽, 예: 규칙 카드)")
    sp.add_argument("--project", required=True)
    sp.add_argument("--file", required=True, help="'그룹/파일' (예: 아이템/Items)")
    sp.add_argument("--exclude", default="", help="뺄 쪽 (예: 1 또는 1,30). 비우면 빼지 않음")
    sp.add_argument("--reason", help="뺀 이유 (기록용)")
    sp.set_defaults(func=cmd_set_pages)
    er = sub.add_parser("erase-project", help="7단계: 모든 그룹·파일 글자 지우기 (이미 지운 파일은 건너뜀)")
    er.add_argument("--project", required=True)
    er.add_argument("--group")
    er.add_argument("--redo", action="store_true", help="이미 지운 파일도 다시 지운다")
    er.set_defaults(func=cmd_erase_project)
    cp = sub.add_parser("compose-project", help="8단계: 모든 그룹·파일 한글 넣기 (검수 엑셀이 있으면 그것, 없으면 초벌 번역)")
    cp.add_argument("--project", required=True)
    cp.add_argument("--group")
    cp.add_argument("--yes", action="store_true", help="엑셀 확인 항목이 있어도 계속")
    cp.add_argument("--draft", action="store_true", help="검수 엑셀이 있어도 초벌 번역으로 넣는다")
    cp.set_defaults(func=cmd_compose_project)
    tr = sub.add_parser("translate", help="5단계: 확정 용어집 기준으로 프로젝트 전체 원문 초벌 번역")
    tr.add_argument("--project", required=True)
    tr.add_argument("--model", default="sonnet", choices=["sonnet", "opus"])
    tr.add_argument("--engine", default="claude-code", choices=["claude-code", "api"])
    tr.set_defaults(func=cmd_translate)
    fi = sub.add_parser("find-icons", help="이름 없는 아이콘 [[?]] 찾기 -> 아이콘_검증.xlsx")
    fi.add_argument("--project", required=True)
    fi.add_argument("--engine", default="claude-code", choices=["claude-code", "api"])
    fi.add_argument("--no-suggest", action="store_true", help="모델에게 이름 제안을 받지 않음")
    fi.set_defaults(func=cmd_find_icons)
    ii = sub.add_parser("import-icons", help="아이콘 검증 엑셀을 가져와 게임 아이콘 사전에 넣고 [[?]]를 바꿈")
    ii.add_argument("--project", required=True)
    ii.add_argument("--excel", required=True)
    ii.set_defaults(func=cmd_import_icons)
    ig = sub.add_parser("import-glossary", help="4단계: 검증한 용어집 엑셀을 가져와 게임 용어집 확정")
    ig.add_argument("--project", required=True)
    ig.add_argument("--excel", required=True)
    ig.add_argument("--yes", action="store_true", help="확인 항목이 있어도 확정")
    ig.set_defaults(func=cmd_import_glossary)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
