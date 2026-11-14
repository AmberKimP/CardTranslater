"""2차 화면: 추출 → 원문 확인 → 아이콘 이름 → ‘추출 완료’(용어집 검증) → 용어집 가져오기 → 번역·검수 엑셀.

긴 작업은 worker.Job으로 작업 스레드에서 돌린다. 엔진이 사람에게 물을 일(needs_confirmation 등)은
결과 상태로 돌려주므로, 여기서 확인 창을 띄우고 ‘계속’이면 다시 부른다 (PRD 6장).
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QCheckBox, QFileDialog, QMessageBox
from app import service
from app.qt import worker
from app.qt.dialogs import ask_choice, show_error, show_problem
from app.qt.step_common import _ask_list, _fail_as, state_label
from app.qt.extract_dialog import ExtractDialog
from app.qt.source_review import SourceReviewDialog
from app.qt.icon_review import IconDictionaryDialog, IconReviewDialog

# ================================================================ 흐름 (프로젝트 화면의 버튼)
class StepActions:
    """프로젝트 화면 버튼이 부르는 2단계 흐름. page: ProjectPage (project_dir, ov, refresh)."""

    def __init__(self, page):
        self.page = page
        self.resume_timer: QTimer | None = None
        self.extract_engine: str | None = None  # 마지막 ① 글자 추출에서 고른 로그인 방식

    @property
    def pdir(self) -> Path:
        return self.page.project_dir

    def _engine(self) -> str | None:
        eng = service.get_settings().engine
        if not service.engine_status()[eng]:
            show_error(self.page, "Claude에 연결할 수 없습니다", service.engine_status()["message"],
                       "‘파일 > 로그인 방식’에서 로그인하거나 다른 방식을 고른 뒤 다시 하세요.")
            return None
        return eng

    # ---- 추출
    def ask_scope(self, what: str) -> list | None | bool:
        """전체로 할지 고른 항목만 할지 묻는다. 전체면 그리드에서 ‘전체’를 고른다.
        돌려줌: None(전체) | 파일 폴더 목록(고른 항목) | False(취소)."""
        from app.qt.dialogs import ask_choice

        label, dirs = self.page.scope()
        if dirs is None:
            return None
        r = ask_choice(self.page, what, f"{what}을(를) 어디에 할까요?",
                       [("전체", "accept"), (f"고른 항목만 ({label})", "accept"), ("취소", "reject")], default=0)
        if r == 0:
            self.page.select_all()
            return None
        if r == 1:
            return dirs
        return False

    def extract_summary(self) -> str:
        """읽을 범위를 ‘파일 N개 = 카드 M장’으로 보여 준다. 이미지 한 장을 배열로 자르면 파일 수보다 카드가 많다."""
        fs = self.page.scoped_files()
        cards = sum(f.get("included", 0) for f in fs)
        left = sum(max(0, f.get("included", 0) - f.get("extracted", 0)) for f in fs)
        return (f"읽을 곳: 파일 {len(fs)}개 = 카드 {cards}장 (아직 안 읽은 카드 {left}장)\n"
                "카드는 PDF의 쪽, 또는 배열(가로×세로)로 자른 이미지 한 칸입니다.")

    def extract(self):
        files = self.ask_scope("글자 추출")
        if files is False:
            return
        dlg = ExtractDialog([], self.page, self.page.ov.get("card_limit"), self.extract_summary())
        if not dlg.exec():
            return
        service.set_card_limit(self.pdir, dlg.card_limit())
        _group, mode, engine = dlg.choice()
        self._run_extract(files, mode, engine)

    def _run_extract(self, files, mode, engine):
        self.extract_engine = engine  # 원문 확인의 ‘이 칸 다시 읽기’도 같은 방식으로
        job = worker.Job(service.extract_project, self.pdir, None, mode=mode, engine=engine, files=files)
        worker.run_with_dialog(self.page, "글자 추출", job,
                               lambda r: self._extract_done(r, (files, mode, engine)),
                               _fail_as(self.page, "글자 추출을 끝내지 못했습니다"))

    def _extract_done(self, r, args):
        self.page.refresh()
        lines = [f"{f['file']}: {state_label(f['state'])} — 읽음 {f['done']}장, 남음 {f['remaining']}장, 실패 {f['failed']}장"
                 + (f", 두 번 확인 같음 {f['recheck'][0]}·다름 {f['recheck'][1]}" if f.get("recheck") else "")
                 for f in r.files]
        if r.state == "done":
            n_check = sum(1 for g in self.page.ov["groups"] for f in g["files"]
                          for c in service.extracted_cells(f["dir"]) if SourceReviewDialog.needs_attention(c))
            QMessageBox.information(self.page, "추출 끝", "\n".join(lines)
                                    + (f"\n\n확인할 칸 {n_check}개 — ‘원문 확인’에서 이미지와 비교해 고치세요."
                                       if n_check else "\n\n확인할 칸이 없습니다."))
            return
        if r.state == "paused" and r.retry_after:
            when = _dt.datetime.strptime(r.retry_after, "%Y-%m-%d %H:%M:%S")
            if _ask_list(self.page, "추출 멈춤", f"{r.reason}", lines,
                         f"{r.retry_after} 이후 이어 할 수 있습니다. 앱을 켜 둔 채 그때 자동으로 이어 할까요?"):
                ms = max(0, int((when - _dt.datetime.now()).total_seconds() * 1000)) + 60_000
                self.resume_timer = QTimer(self.page)
                self.resume_timer.setSingleShot(True)
                self.resume_timer.timeout.connect(lambda: self._run_extract(*args))
                self.resume_timer.start(ms)
                self.page.win.statusBar().showMessage(f"추출을 {r.retry_after} 이후 자동으로 이어 합니다")
            return
        if r.state == "cancelled":
            QMessageBox.information(self.page, "글자 추출을 멈췄습니다",
                                    "읽은 카드는 저장했습니다. ‘① 글자 추출’을 다시 누르면 남은 카드부터 이어서 읽습니다.\n\n"
                                    + "\n".join(lines))
            return
        show_problem(self.page, f"글자 추출을 끝내지 못했습니다 ({state_label(r.state)})", r.reason or "",
                     retry_after=r.retry_after, detail="\n".join(lines))

    # ---- 원문 확인
    def review_sources(self, only_check: bool = True, focus_key: str | None = None, focus_label: str | None = None):
        files = self.page.scoped_files()  # 그리드에서 고른 범위
        if focus_key and not any(f["key"] == focus_key for f in files):  # 고칠 파일이 범위 밖이면 그 파일도 넣는다
            allf = [f for g in self.page.ov["groups"] for f in g["files"]]
            files = files + [f for f in allf if f["key"] == focus_key]
        if not files:
            return
        # ② 아이콘 정의에서 ‘나중에 (원문 확인에서)’를 고른 칸: 원문 확인을 열기 전에 먼저 다시 읽는다
        pending = service.icon_reread_pending([f["dir"] for f in files])
        if pending:
            eng = self._engine()
            if eng is not None:
                args = (only_check, focus_key, focus_label)
                job = worker.Job(service.reread_cells, pending, engine=eng)
                worker.run_with_dialog(self.page, f"아이콘이 바뀐 칸 {len(pending)}개 다시 읽는 중", job,
                                       lambda r: self._pending_reread_done(r, files, args),
                                       _fail_as(self.page, "다시 읽지 못했습니다"), cancellable=True,
                                       on_cancelled=lambda: self._pending_reread_cancelled(pending, files, args))
                return
        self._open_source_review(files, only_check, focus_key, focus_label)

    def _pending_reread_done(self, r: dict, files: list[dict], args: tuple):
        if r["state"] != "done" or r["failed"]:
            show_problem(self.page, "아이콘이 바뀐 칸을 다 읽지 못했습니다", r["reason"] or "",
                         f"{r['done']}칸을 다시 읽었습니다. 남은 칸은 원문 확인에서 ‘이 칸 다시 읽기’로 읽을 수 있습니다.",
                         items=[f"{k}: {why}" for k, why in r["failed"]], retry_after=r.get("retry_after"))
        self._open_source_review(files, *args)

    def _pending_reread_cancelled(self, pending: list[dict], files: list[dict], args: tuple):
        # 취소: 남은 칸은 다음에 또 자동으로 읽지 않는다 (‘확인 필요’로 남아 ‘이 칸 다시 읽기’로 읽을 수 있다)
        service.skip_icon_reread(pending)
        self._open_source_review(files, *args)

    def _open_source_review(self, files, only_check, focus_key, focus_label):
        # ‘이 칸 다시 읽기’: 이번에 ① 글자 추출에서 고른 로그인 방식 (아직 안 했으면 설정에 저장된 방식)
        eng = self.extract_engine or service.get_settings().engine
        dlg = SourceReviewDialog(files, self.page, only_check, engine=eng, focus_key=focus_key, focus_label=focus_label)
        dlg.exec()
        if dlg.saved:
            self.page.refresh()

    # ---- 아이콘
    def icons(self):
        """누를 때마다 다시 찾는다 (원문을 고쳤거나 찾기 방식이 나아졌을 수 있으므로 예전 결과를 쓰지 않음)."""
        eng = self._engine()
        if eng is None:
            return
        job = worker.Job(service.find_icons, self.pdir, engine=eng, with_progress=False)
        worker.run_with_dialog(self.page, "아이콘 찾는 중 (이름 제안 포함)", job, self._icons_found,
                               _fail_as(self.page, "아이콘을 찾지 못했습니다"), cancellable=False)

    def _icons_found(self, r):
        msg = ("사전과 같아 바로 바꿈: " + ", ".join(f"[[{n}]] {c}곳" for n, c in r.auto.items())) if r.auto else ""
        if r.state == "done":
            # 새로 정할 아이콘이 없어도 사전을 보여 준다 (이름·크기 고치기, 잘못 묶인 아이콘 다시 정하기)
            rows = service.icon_dictionary(self.pdir)
            if not rows:
                QMessageBox.information(self.page, "아이콘", (msg + "\n" if msg else "") + "원문에 아이콘이 없습니다.")
                return
            dlg = IconDictionaryDialog(rows, (msg + "\n" if msg else "") + "새로 이름을 정할 아이콘은 없습니다. "
                                       "아래는 이 게임의 아이콘 사전입니다.", self.page)
            if dlg.exec():
                res = service.update_icon_dictionary(self.pdir, dlg.rows())
                if res["reset"]:
                    if QMessageBox.question(
                            self.page, "아이콘 다시 정하기",
                            "되돌린 아이콘: " + ", ".join(f"[[{n}]] {c}곳" for n, c in res["reset"].items())
                            + "\n지금 다시 찾아 이름을 정할까요?") == QMessageBox.StandardButton.Yes:
                        self.icons()
                self.page.refresh()
            return
        self._open_icon_review(service.icon_review(self.pdir))

    def _open_icon_review(self, review, from_finish: bool = False):
        """확정하면 결과(IconImportResult), 닫으면 None. from_finish: ④ 용어집 만들기에서 왔음 —
        아이콘 자리가 바뀐 칸이 있으면 용어집으로 이어 가지 않고 ③ 원문 확인으로 안내한다."""
        dlg = IconReviewDialog(self.pdir, review, self.page)
        self._icons_from_finish = from_finish
        if dlg.exec() and dlg.result:
            r = dlg.result
            QMessageBox.information(self.page, "아이콘 확정",
                                    f"새로 넣음: {', '.join(r.added) or '-'}\n사전에 있던 것: {', '.join(r.reused) or '-'}\n"
                                    f"원문에서 바꿈 {r.replaced}곳, 뺌 {r.removed}곳")
            self.page.refresh()
            if r.changed_cells:
                self._reread_icon_cells(r.changed_cells)
            return r
        return None

    def _reread_icon_cells(self, cells: list[dict]):
        """② 아이콘 정의에서 아이콘 자리가 바뀐 칸 (손으로 다시 잡음·세트로 합침·아이콘 아님·위치 못 찾음):
        모델이 그 자리를 잘못 읽었을 수 있다. 지금 다시 읽을지 묻고, ③ 원문 확인으로 이어 준다."""
        n = len(cells)
        c = ask_choice(self.page, "아이콘이 바뀐 칸 다시 읽기",
                       f"아이콘 자리가 바뀐 칸이 {n}개 있습니다 (손으로 다시 잡음·세트로 합침·아이콘 아님·위치 못 찾음).\n"
                       "그 자리를 모델이 잘못 읽었을 수 있습니다. 지금 다시 읽을까요?",
                       [(f"{n}칸 다시 읽기", "accept"), ("나중에 (원문 확인에서)", "reject")], default=0,
                       informative="다시 읽은 결과는 ③ 원문 확인의 ‘읽은 결과’에 들어가고, 원문은 사람이 보고 저장할 때 바뀝니다.\n"
                                   "이 칸들은 ‘확인 필요’로 표시되어 원문 확인의 확인할 칸에 나옵니다.")
        if c != 0:
            if getattr(self, "_icons_from_finish", False):
                QMessageBox.information(self.page, "③ 원문 확인 먼저",
                                        "아이콘 자리가 바뀐 칸이 있어 용어집 만들기를 멈췄습니다.\n"
                                        "‘③ 원문 확인’을 열면 그 칸들을 먼저 다시 읽습니다. 확인한 뒤 ‘④ 용어집 만들기’를 다시 누르세요.")
            return
        eng = self._engine()
        if eng is None:
            return
        job = worker.Job(service.reread_cells, cells, engine=eng)
        worker.run_with_dialog(self.page, f"아이콘이 바뀐 칸 {n}개 다시 읽는 중", job, self._reread_icon_done,
                               _fail_as(self.page, "다시 읽지 못했습니다"), cancellable=True)

    def _reread_icon_done(self, r: dict):
        self.page.refresh()
        if r["state"] != "done":
            show_problem(self.page, f"다시 읽기를 멈췄습니다 ({state_label(r['state'])})", r["reason"] or "",
                         f"{r['done']}칸을 다시 읽었습니다. 남은 칸은 ③ 원문 확인에서 ‘이 칸 다시 읽기’로 읽을 수 있습니다.",
                         retry_after=r.get("retry_after"))
        elif r["failed"]:
            show_problem(self.page, "일부 칸을 다시 읽지 못했습니다", "", "③ 원문 확인에서 그 칸을 다시 읽어 보세요.",
                         items=[f"{k}: {why}" for k, why in r["failed"]])
        if QMessageBox.question(self.page, "원문 확인",
                                f"다시 읽은 {r['done']}칸을 ③ 원문 확인에서 비교해 확인할까요?") \
                == QMessageBox.StandardButton.Yes:
            self.review_sources(only_check=True)

    # ---- 추출 완료 -> 용어집 검증
    def finish(self, accept: bool = False):
        if not accept and self.page.ov.get("status") in ("glossary_confirmed", "translating", "translated"):
            ver = self.page.ov.get("glossary_version")
            r = ask_choice(self.page, "용어집 다시 만들기",
                           f"용어집이 이미 확정되었습니다{f' ({ver}번째 확정본)' if ver else ''}.",
                           [("다시 만들기", "destructive"), ("취소", "reject")], default=1,
                           informative="다시 만들면 상태가 ‘용어집 검증 중’으로 돌아가고, 용어집 엑셀을 다시 가져와야 "
                                       "번역·한글 넣기를 할 수 있습니다.\n확정된 용어는 그대로 남고 새 용어만 후보로 나옵니다.")
            if r != 0:
                return
        eng = self._engine()
        if eng is None:
            return
        job = worker.Job(service.finish_extraction, self.pdir, accept_issues=accept, engine=eng, with_progress=False)
        worker.run_with_dialog(self.page, "용어 후보를 만드는 중 (몇 분 걸릴 수 있음)", job, self._finish_done,
                               _fail_as(self.page, "용어집 엑셀을 만들지 못했습니다"), cancellable=False)

    def _finish_done(self, r):
        self.page.refresh()
        if r.state == "needs_confirmation":
            if _ask_list(self.page, "용어집 만들기 확인", "아래 항목이 남아 있습니다.", r.issues,
                         "그래도 용어집을 만들까요? (‘아니요’를 누르면 그 항목을 고치는 창으로 갑니다)"):
                self.finish(accept=True)
            else:
                self._fix_finish_issues(r.issues)
            return
        if r.state == "icons_review":
            QMessageBox.information(self.page, "아이콘 먼저", f"먼저 ‘② 아이콘 정의’를 해야 합니다: {r.reason}")
            review = service.icon_review(self.pdir)
            r2 = self._open_icon_review(review, from_finish=True) if review else None
            if r2 is not None and not r2.changed_cells:  # 원문이 그대로면 용어집 만들기를 이어 간다
                self.finish(accept=True)
            return
        if r.state in ("blocked", "paused"):
            show_problem(self.page, f"용어집 엑셀을 만들지 못했습니다 ({state_label(r.state)})", r.reason or "",
                         retry_after=r.retry_after)
            return
        self.review_glossary()  # 바로 프로그램 안에서 번역을 확인·확정 (엑셀은 그 창에서 따로 내보내기·가져오기)

    def _fix_finish_issues(self, issues: list[str]):
        """용어집 만들기에서 남은 항목을 고치는 곳으로 간다: ‘결론 못 냄’ → ③ 원문 확인(그 파일의 첫 칸을 골라),
        영역 템플릿 없음 → 영역 지정, 일부만 읽음·실패한 카드 → ① 글자 추출 (이어서 읽기)."""
        # 항목의 파일 이름은 게임 단위 키 ‘프로젝트/그룹/파일’ (같은 게임의 다른 프로젝트 것일 수도 있다).
        # 원문 확인은 이 프로젝트의 ‘그룹/파일’로 찾으므로 이 프로젝트 것만 고른다
        prefix = f"{self.page.ov['name']}/"
        mine = [f["key"] for g in self.page.ov["groups"] for f in g["files"]]
        other = None
        for it in issues:
            m = (re.match(r"‘결론 못 냄’ 칸 \(원문이 틀렸을 수 있음\): (.+) \d+칸$", it)
                 or re.match(r"아이콘이 바뀐 칸 \(③ 원문 확인 필요\): (.+) \d+칸$", it))
            if not m:
                continue
            key = m.group(1)
            key = key[len(prefix):] if key.startswith(prefix) else key
            if key in mine:
                label = "결론 못 냄" if it.startswith("‘결론") else None
                self.review_sources(only_check=True, focus_key=key, focus_label=label)
                return
            other = other or (m.group(1), "‘결론 못 냄’" if it.startswith("‘결론") else "아이콘이 바뀐")
        if other:  # 같은 게임의 다른 프로젝트에 있는 칸: 그 프로젝트를 열어 고쳐야 한다
            QMessageBox.information(self.page, "다른 프로젝트의 칸",
                                    f"{other[1]} 칸이 같은 게임의 다른 프로젝트에 있습니다: {other[0]}\n"
                                    "그 프로젝트를 열어 ‘③ 원문 확인’에서 고치세요.")
            return
        if any(it.startswith("영역 템플릿이 없어") for it in issues):
            g = next((g for g in self.page.ov["groups"] if g["files"] and not g["has_regions"]), None)
            if g is not None:
                self.page._open_editor(g)  # 영역이 없는 그룹의 영역 지정 창
            else:
                self.page.edit_regions()
            return
        if any(it.startswith(("일부만 읽음", "실패한 카드")) for it in issues):
            ans = QMessageBox.question(self.page, "글자 추출 이어 하기",
                                       "아직 읽지 않았거나 읽지 못한 카드가 있습니다. ‘① 글자 추출’로 이어서 읽을까요?")
            if ans == QMessageBox.StandardButton.Yes:
                self.extract()

    # ---- 용어집 확정 (프로그램 안 표; 엑셀은 그 창에서 따로)
    def review_glossary(self):
        from app.qt.glossary_review import GlossaryReviewDialog

        rows = service.glossary_review_rows(self.pdir)
        if rows is None:
            show_problem(self.page, "용어 후보가 없습니다", "아직 용어 후보를 만들지 않았습니다.",
                         "먼저 ‘④ 용어집 만들기’를 누르세요.")
            return
        dlg = GlossaryReviewDialog(self.pdir, rows, self.page)
        dlg.exec()
        self.page.refresh()
        if dlg.result is not None:
            self._glossary_confirmed(dlg.result)

    # ---- 용어집 가져오기 (엑셀)
    def import_glossary(self, path: str | None = None, accept: bool = False):
        if path is None:
            default = str(service.game_dir(self.pdir) / "용어집_검증.xlsx")
            path, _ = QFileDialog.getOpenFileName(self.page, "검증한 용어집 엑셀", default, "Excel (*.xlsx)")
            if not path:
                return
        r = service.import_glossary(self.pdir, path, accept_issues=accept)
        if r.state == "blocked":
            show_problem(self.page, "용어집을 가져오지 못했습니다", "", items=r.blocking + r.issues, todo=
                         "엑셀에서 이 내용을 고치고 저장한 뒤 ‘⑤ 용어집 확정’ 창의 ‘엑셀에서 가져오기’를 다시 누르세요.")
            return
        if r.state == "needs_confirmation":
            if _ask_list(self.page, "용어집 확인", "아래 항목을 확인하세요.", r.issues, "이대로 확정할까요?"):
                self.import_glossary(path, accept=True)
            return
        self.page.refresh()
        self._glossary_confirmed(r)

    def _glossary_confirmed(self, r):
        """확정한 뒤: 번역이 바뀐 용어가 있으면 그 용어가 든 칸을 찾아 바뀜 후보로 (F20), 없으면 알린다."""
        if getattr(r, "changed_detail", None):  # 번역이 바뀐 용어: 그 용어가 든 칸을 찾아 바뀜 후보로 (F20)
            job = worker.Job(service.term_change_candidates, self.pdir, r.changed_detail, with_progress=False)
            worker.run_with_dialog(self.page, "바뀐 용어가 든 칸을 찾는 중", job,
                                   lambda rows: self._term_changes(r, rows),
                                   _fail_as(self.page, "바뀐 용어가 든 칸을 찾지 못했습니다"), cancellable=False)
            return
        QMessageBox.information(self.page, "용어집 확정",
                                f"게임 용어집 버전 {r.version}, 용어 {r.terms}개."
                                + (f"\n번역이 바뀐 용어: {', '.join(r.changed)}\n‘번역’을 다시 하면 이 용어가 든 칸만 다시 번역합니다."
                                   if r.changed else "\n번역을 시작하거나 이어 할 수 있습니다."))

    def _term_changes(self, r, rows: list[dict]):
        """바뀐 용어가 든 칸을 보여 주고, 고른 칸을 한꺼번에 바꾼 뒤 요약을 알린다."""
        from app.qt.term_changes import TermChangesDialog

        head = f"게임 용어집 버전 {r.version}, 용어 {r.terms}개.\n번역이 바뀐 용어: {', '.join(r.changed)}"
        redo = "\n‘번역’을 다시 하면 이 용어가 든 칸만 다시 번역합니다."
        prev = getattr(r, "prev_statuses", None) or r.prev_status  # 상태가 바뀐 프로젝트마다 그 전 상태
        if not rows:  # 옛 번역을 쓰는 칸이 없음: 초벌 번역 캐시만 맞추고 (다 맞으면 상태를 되돌림) 알린다
            def done(res):
                self.page.refresh()
                QMessageBox.information(self.page, "용어집 확정", head + "\n옛 번역을 쓰는 칸이 없습니다."
                                        + ("\n다시 번역할 필요가 없습니다." if res.status_restored else redo))
            job = worker.Job(service.apply_term_changes, self.pdir, [], r.changed_detail, prev_status=prev,
                             with_progress=False)
            worker.run_with_dialog(self.page, "초벌 번역을 새 용어로 맞추는 중", job, done,
                                   _fail_as(self.page, "바뀐 용어를 반영하지 못했습니다"), cancellable=False)
            return
        dlg = TermChangesDialog(self.pdir, r.changed_detail, rows, prev, self.page)
        if not dlg.exec() or dlg.result is None:
            QMessageBox.information(self.page, "용어집 확정", head + redo)
            return
        res = dlg.result
        self.page.refresh()
        QMessageBox.information(
            self.page, "바뀐 용어 반영",
            f"{res.applied}칸을 바꿨습니다. 검수 엑셀은 그대로이고, 바꾼 번역은 한글 넣기·검수 엑셀 다시 만들기에 반영됩니다."
            + ("\n다시 번역할 필요가 없습니다." if res.status_restored else redo))

    # ---- 번역
    def translate(self):
        eng = self._engine()
        if eng is None:
            return
        if self.ask_scope("⑥ 전체 번역") is False:  # 그룹·파일을 고른 채 누르면 전체/고른 항목을 묻는다 (기본 전체)
            return
        label, dirs = self.page.scope()
        where = "모든 파일" if dirs is None else label
        box = QMessageBox(self.page)
        box.setWindowTitle("번역")
        box.setText(f"확정 용어집을 기준으로 {where}의 원문을 번역합니다. 이미 번역한 칸은 다시 하지 않습니다.")
        box.setInformativeText("보통 번역으로 충분합니다. 정밀 번역은 느리고 사용량이 많습니다.")
        sonnet = box.addButton("보통 번역 (추천)", QMessageBox.ButtonRole.AcceptRole)
        opus = box.addButton("정밀 번역", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("취소", QMessageBox.ButtonRole.RejectRole)
        then_erase = self._erase_option()
        if then_erase is not None:
            box.setCheckBox(then_erase)
        box.exec()
        model = {sonnet: "sonnet", opus: "opus"}.get(box.clickedButton())
        if not model:
            return
        self._then_erase = bool(then_erase and then_erase.isChecked())
        job = worker.Job(service.translate_project, self.pdir, model=model, engine=eng, files=self.page.scope()[1])
        worker.run_with_dialog(self.page, "번역", job, self._translate_done, _fail_as(self.page, "번역을 끝내지 못했습니다"))

    def _erase_option(self) -> QCheckBox | None:
        """번역 질문 창에 붙일 ‘끝나면 바로 ⑦ 글자 지우기’ 체크 (지울 수 있는 상태일 때만)."""
        btn = getattr(self.page, "step_buttons", {}).get("erase")
        if btn is None or not btn.isEnabled() or not hasattr(self.page, "out"):
            return None
        files = self.page.scoped_files()
        if not files or all(f.get("erased") for f in files):
            return None
        cb = QCheckBox("끝나면 바로 ‘⑦ 글자 지우기’까지 하기")
        cb.setToolTip("번역이 끝나면 이어서 글자를 지웁니다. 이미 지운 파일은 건너뜁니다.")
        cb.setChecked(True)
        return cb

    def _translate_done(self, r):
        """번역이 끝나면 요약과 ‘번역 검수 열기’를 보인다 (검수 엑셀은 자동으로 만들지 않는다)."""
        self.page.refresh()
        if r.state != "done":
            self._then_erase = False
        if r.state == "cancelled":
            QMessageBox.information(self.page, "번역을 멈췄습니다",
                                    "번역한 칸은 저장했습니다. ‘⑥ 전체 번역’을 다시 누르면 남은 칸부터 이어서 합니다.")
            return
        if r.state != "done":
            show_problem(self.page, f"번역을 끝내지 못했습니다 ({state_label(r.state)})", r.reason or "",
                         retry_after=r.retry_after)
            return
        issues = []
        if r.term_mismatch:
            issues.append(f"용어 불일치 {len(r.term_mismatch)}칸")
        if r.icon_mismatch:
            issues.append(f"아이콘 불일치 {len(r.icon_mismatch)}칸")
        if r.leftover:
            issues.append(f"일본어가 남은 번역 {len(r.leftover)}칸")
        summary = (f"서로 다른 원문 {r.cells}칸: 이번에 번역 {r.translated}, 용어집으로 바로 {r.from_glossary}, "
                   f"이미 번역됨 {r.cached}." + ("\n확인: " + ", ".join(issues) if issues else "")
                   + (f"\n{r.excel_note}" if getattr(r, "excel_note", "") else ""))
        if getattr(self, "_then_erase", False):  # 묻지 않고 바로 ⑦ 글자 지우기 (결과는 지우기 끝에 함께)
            self._then_erase = False
            self.page.out.erase(auto=True, note=summary)
            return
        box = QMessageBox(self.page)
        box.setWindowTitle("⑥ 전체 번역")
        box.setText(summary + "\n\n번역을 마쳤습니다. ‘번역 검수’에서 칸마다 확인하세요. "
                    "엑셀은 다른 사람에게 검수를 맡길 때 씁니다.")
        openb = box.addButton("번역 검수 열기", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("닫기", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is openb:
            self.review_translation()

    # ---- 번역 검수
    def review_translation(self):
        """번역 검수 창. 목록에서 고른 그룹·파일이 있으면 범위를 그것으로 미리 고른다."""
        from app.qt.review import TranslationReviewDialog

        sel = self.page._selected()
        preset = (sel[0], sel[1]["dir"]) if sel and sel[0] in ("group", "file") else None
        dlg = TranslationReviewDialog(self.pdir, self.page, preset)
        dlg.exec()
        msg = dlg.closing_message()
        self.page.refresh()
        if msg and getattr(self.page, "win", None) is not None:
            self.page.win.statusBar().showMessage(msg)
