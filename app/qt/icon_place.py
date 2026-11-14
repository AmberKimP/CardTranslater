"""② 아이콘 정의: 카드 보기·자리 잡기 (아이콘 자리를 그림 위에 보여 주고 다시 잡기)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget
from app import service
from app.qt import worker
from app.qt.dialogs import ask_choice, fit_to_screen, hint_bar, show_error, show_problem


# ================================================================ 아이콘 이름
class IconPlaceView(QWidget):
    """카드 영역 그림 위에 아이콘 자리를 보여 주고, 마우스로 끌어 새 자리를 잡는다.

    초록 = 같은 칸에서 이미 찾은 아이콘, 파랑 = 이 아이콘의 지금 자리, 주황 점선 = 예상 위치(못 찾았을 때),
    빨강 = 사람이 새로 잡은 자리. 좌표는 모두 그림 px."""

    drawn = Signal()

    def __init__(self):
        super().__init__()
        self._pm = QPixmap()
        self.marks: list[tuple[list, str]] = []
        self.box = self.guess = self.new = None
        self._drag = None
        self.setMinimumSize(300, 200)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(False)

    def set_data(self, pm: QPixmap, box, guess, marks: list[tuple[list, str]]):
        self._pm, self.box, self.guess, self.marks, self.new, self._drag = pm, box, guess, marks, None, None
        self.update()

    def _geom(self):
        w, h = self._pm.width(), self._pm.height()
        if not w or not h:
            return 1.0, 0.0, 0.0
        s = min((self.width() - 16) / w, (self.height() - 16) / h, 3.0)
        return s, (self.width() - w * s) / 2, (self.height() - h * s) / 2

    def _to_img(self, p) -> tuple[float, float]:
        s, ox, oy = self._geom()
        return (min(max((p.x() - ox) / s, 0), self._pm.width()), min(max((p.y() - oy) / s, 0), self._pm.height()))

    def _rect(self, b) -> QRectF:
        s, ox, oy = self._geom()
        return QRectF(ox + b[0] * s, oy + b[1] * s, (b[2] - b[0]) * s, (b[3] - b[1]) * s)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#FAFAFA"))
        if self._pm.isNull():
            return
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        s, ox, oy = self._geom()
        p.drawPixmap(QRectF(ox, oy, self._pm.width() * s, self._pm.height() * s), self._pm, QRectF(self._pm.rect()))
        f = p.font()
        f.setBold(True)
        p.setFont(f)

        def frame(b, color, label="", dash=False, width=2):
            r = self._rect(b)
            pen = QPen(QColor(color), width)
            if dash:
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r)
            if label:
                fm = p.fontMetrics()
                tw, th = fm.horizontalAdvance(label) + 8, fm.height() + 2
                ty = r.top() - th if r.top() - th >= 0 else r.bottom()
                p.fillRect(QRectF(r.left(), ty, tw, th), QColor(color))
                p.setPen(QColor("white"))
                p.drawText(QRectF(r.left(), ty, tw, th), Qt.AlignmentFlag.AlignCenter, label)

        for b, label, *rest in self.marks:  # (상자, 이름[, 예상 위치인가])
            if rest and rest[0]:  # 위치를 못 찾은 다른 아이콘: 예상 자리에 점선
                frame(b, "#9A6A00", label, dash=True)
            else:
                frame(b, "#2E8B57", label)
        if self.guess and not self.new:
            frame(self.guess, "#E67E22", "예상 위치", dash=True)
        if self.box:
            frame(self.box, "#1F6FEB", "지금 자리", width=3 if not self.new else 2)
        if self.new:
            frame(self.new, "#D62828", "새 자리", width=3)
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and not self._pm.isNull():
            self._drag = self._to_img(e.position())
            self.new = None
            self.update()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            x, y = self._to_img(e.position())
            x0, y0 = self._drag
            self.new = [min(x0, x), min(y0, y), max(x0, x), max(y0, y)]
            self.update()

    def mouseReleaseEvent(self, e):
        if self._drag is None:
            return
        self._drag = None
        if self.new and (self.new[2] - self.new[0] < 4 or self.new[3] - self.new[1] < 4):
            self.new = None  # 그냥 누른 것
        self.update()
        self.drawn.emit()


class IconPlaceDialog(QDialog):
    """아이콘이 나온 카드 자리를 보며, 못 찾았거나 잘못 잘린 아이콘을 마우스로 감싸 직접 잡는다."""

    def __init__(self, project_dir: Path, g: dict, row_label, on_saved, parent=None):
        super().__init__(parent)
        self.project_dir = Path(project_dir)
        self.row_label = row_label  # 묶음 -> ‘3번’ 같은 화면 이름
        self.on_saved = on_saved
        self.outcome: tuple[str, str | None] = ("", None)  # 닫은 뒤 표가 고를 줄: ("saved"|"not_icon", 묶음)
        self.setWindowTitle("카드 보기 · 아이콘 자리 잡기")
        fit_to_screen(self, 1100, 720, maximize_below=0)
        self.where = [dict(w) for w in g.get("where", [])]
        self.combo = QComboBox()
        for w in self.where:
            self.combo.addItem(self._item_text(w), w["occ"])
        self.view = IconPlaceView()
        self.view.drawn.connect(self._drawn)
        self.crop = QLabel()
        self.crop.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.crop.setMinimumWidth(110)
        self.status = QLabel()
        self.status.setWordWrap(True)
        legend = QLabel('<span style="color:#2E8B57">■</span> 이미 찾은 아이콘 &nbsp; '
                        '<span style="color:#1F6FEB">■</span> 이 아이콘의 지금 자리 &nbsp; '
                        '<span style="color:#E67E22">■</span> 예상 위치(못 찾았을 때, 점선) &nbsp; '
                        '<span style="color:#D62828">■</span> 새로 잡은 자리')
        hint = hint_bar("아이콘을 마우스로 끌어 감싸세요 (테두리까지 조금 넉넉하게). 다시 끌면 새로 잡습니다.\n"
                        "‘이 자리로 저장’을 누르면 그 자리로 그림을 다시 잘라 같은 모양끼리 다시 묶습니다.", "info")
        self.save_btn = QPushButton("이 자리로 저장")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._save)
        self.not_icon_btn = QPushButton("이 자리는 아이콘 아님")
        self.not_icon_btn.setToolTip("이 자리만 ‘아이콘 아님’ 줄로 옮깁니다 (같은 묶음의 다른 자리는 그대로).\n"
                                     "글자를 아이콘으로 잘못 읽은 것이면 그 줄의 ‘뜻’ 칸에 그 글자를 적으세요 (비우면 원문에서 뺌)")
        self.not_icon_btn.clicked.connect(self._not_icon)
        self.absorb_btn = QPushButton("옆 아이콘에 포함됨")  # 못 찾은 자리인데 바로 옆 자리의 상자가 이 자리까지 감쌌을 때
        self.absorb_btn.setToolTip("바로 옆에 붙은 아이콘을 잡은 상자가 이 자리까지 감싸고 있으면 누르세요.\n"
                                   "둘을 하나의 아이콘으로 보고 이 자리는 원문에서 뺍니다 (예: 문양 셋이 붙은 아이콘).")
        self.absorb_btn.clicked.connect(self._absorb)
        self.absorb_btn.hide()
        self._run_host: str | None = None
        close = QPushButton("닫기")
        close.clicked.connect(self.accept)
        side = QVBoxLayout()
        side.addWidget(QLabel("잘라 낸 그림:"))
        side.addWidget(self.crop)
        side.addStretch(1)
        mid = QHBoxLayout()
        mid.addWidget(self.view, 1)
        mid.addLayout(side)
        btns = QHBoxLayout()
        btns.addWidget(self.status, 1)
        btns.addWidget(self.absorb_btn)
        btns.addWidget(self.not_icon_btn)
        btns.addWidget(self.save_btn)
        btns.addWidget(close)
        lay = QVBoxLayout(self)
        lay.addWidget(hint)
        lay.addWidget(self.combo)
        lay.addLayout(mid, 1)
        lay.addWidget(legend)
        lay.addLayout(btns)
        self.combo.currentIndexChanged.connect(lambda _i: self._load())
        # 처음 볼 자리: 못 찾은 것 -> 직접 잡은 것(다시 고치러 왔을 때) -> 첫째
        first = next((i for i, w in enumerate(self.where) if not w.get("png")),
                     next((i for i, w in enumerate(self.where) if w.get("manual")), 0))
        if first:
            self.combo.setCurrentIndex(first)  # 신호가 _load를 부른다
        else:
            self._load()

    @staticmethod
    def _item_text(w: dict) -> str:
        tag = " (직접 잡음)" if w.get("manual") else ("" if w.get("png") else " (위치 못 찾음)")
        return w["label"] + tag

    def _load(self):
        i = self.combo.currentIndex()
        if i < 0:
            return
        w = self.where[i]
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            p = service.icon_place(self.project_dir, w["occ"])
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            show_error(self, "카드를 보여 주지 못했습니다", e)
            return
        QApplication.restoreOverrideCursor()
        pm = QPixmap()
        pm.loadFromData(p["png"], "PNG")
        marks = [(m["box"], (f"[[{m['name']}]]" if m.get("name") else self.row_label(m.get("group", "")))
                  + (" · 위치 못 찾음" if m.get("guess") else ""), bool(m.get("guess")))
                 for m in p["marks"]]
        self.view.set_data(pm, p["box"], p["guess"], marks)
        if w.get("png"):
            self.crop.setPixmap(QPixmap(w["png"]).scaledToHeight(64, Qt.TransformationMode.SmoothTransformation))
        else:
            self.crop.setText("아직 없음")
        host = (p.get("run_host") or [None])[0]
        self._run_host = host
        host_mark = next((m for m in p["marks"] if m.get("occ") == host), None) if host else None
        host_label = (f"[[{host_mark['name']}]]" if host_mark.get("name") else self.row_label(host_mark.get("group", ""))) \
            if host_mark else "옆 아이콘"
        self.absorb_btn.setText(f"{host_label}에 포함됨")
        self.absorb_btn.setVisible(bool(host))
        if p["box"]:
            self.status.setText("자리가 맞지 않으면 아이콘을 끌어 다시 잡으세요.")
        elif host:
            self.status.setText(f"바로 옆 아이콘({host_label})의 상자가 이 자리까지 감싸고 있으면 ‘{host_label}에 포함됨’을 "
                                "누르세요. 따로 있는 아이콘이면 끌어 감싸세요.")
        elif p["guess"]:
            self.status.setText("자동으로 찾지 못했습니다. 주황 점선(예상 위치) 근처의 아이콘을 끌어 감싸세요.")
        else:
            self.status.setText("자동으로 찾지 못했습니다. 아이콘을 끌어 감싸세요.")
        self.save_btn.setEnabled(False)

    def _drawn(self):
        self.save_btn.setEnabled(self.view.new is not None)

    def _save(self):
        box = self.view.new
        if not box:
            return
        i = self.combo.currentIndex()
        w = self.where[i]
        try:
            r = service.set_icon_box(self.project_dir, w["occ"], box)
        except ValueError as e:
            show_problem(self, "자리를 저장하지 못했습니다", str(e), "아이콘을 조금 더 크게 감싸 주세요.")
            return
        except Exception as e:  # noqa: BLE001
            show_error(self, "자리를 저장하지 못했습니다", e)
            return
        if r.get("run_unfound"):  # 바로 옆에 붙은 [[?]]를 못 찾았음: 이 상자가 그것까지 감쌌을 수 있다 (문양 셋이 붙은 아이콘)
            n = len(r["run_unfound"])
            c = ask_choice(self, "붙어 있는 아이콘",
                           f"이 자리 바로 옆에 아이콘([[?]]) {n}개가 붙어 있습니다 (위치를 못 찾았거나 이 상자 안에 있음).\n"
                           f"방금 잡은 상자가 아이콘 {n + 1}개를 한 세트로 감싸고 있나요?",
                           [(f"아이콘 {n + 1}개를 한 세트로", "accept"), ("따로 있는 아이콘임", "reject")], default=0,
                           informative="합치면 붙어 있는 [[?]]가 이 아이콘 하나가 되고, 나머지 자리는 원문에서 뺍니다.\n"
                                       "따로 있는 아이콘이면 그 줄에서 ‘카드 보기 · 자리 잡기’로 하나씩 잡으세요.")
            merged = False
            if c == 0:
                try:
                    service.absorb_icon_run(self.project_dir, w["occ"], r["run_unfound"])
                    merged = True
                except Exception as e:  # noqa: BLE001
                    show_error(self, "아이콘을 합치지 못했습니다", e)
        else:
            merged = False
        self.outcome = ("saved", r["group"])
        self.on_saved(r)  # 이름 정하기 표를 새로 채운다 (줄 번호가 바뀔 수 있음)
        rev = service.icon_review(self.project_dir) or {"groups": []}
        gg = next((x for x in rev["groups"] if x["group"] == r["group"]), {})
        # 여러 아이콘을 한 세트로 합쳤으면 그림이 바뀌었으므로 (예전 한 개짜리 제안은 맞지 않음) 다시 제안 받는다
        if (merged and gg.get("count", 1) <= 1 and not r.get("dict")) or                 (r.get("new_group") and not r.get("dict") and not gg.get("suggest")):
            self._suggest(r["group"])  # 제안을 받은 뒤 닫는다
        else:
            self.accept()  # 저장했으면 닫고 이름 정하기 표로 돌아간다

    def _not_icon(self):
        """지금 고른 자리만 ‘아이콘 아님’ 줄로 옮긴다."""
        i = self.combo.currentIndex()
        if i < 0:
            return
        w = self.where[i]
        try:
            r = service.set_icon_not_icon(self.project_dir, w["occ"])
        except Exception as e:  # noqa: BLE001
            show_error(self, "‘아이콘 아님’으로 정하지 못했습니다", e)
            return
        self.outcome = ("not_icon", r["group"])
        self.on_saved(r)  # 이름 정하기 표를 새로 채운다 (그 줄은 ‘아이콘 아님’이 체크되어 있다)
        self.accept()  # 닫고 이름 정하기 표로 돌아간다 (글자였다면 그 줄의 ‘뜻’ 칸에 글자를 적는다)

    def _absorb(self):
        """지금 자리를 바로 옆 아이콘(상자가 이 자리까지 감쌈)에 포함: 이 자리는 원문에서 뺀다."""
        i = self.combo.currentIndex()
        if i < 0 or not self._run_host:
            return
        try:
            service.absorb_icon_run(self.project_dir, self._run_host, [self.where[i]["occ"]])
        except Exception as e:  # noqa: BLE001
            show_error(self, "아이콘을 합치지 못했습니다", e)
            return
        self.outcome = ("not_icon", None)  # 이 줄은 끝: 다음 줄로
        self.on_saved({})
        # 합친 세트(앞 아이콘)의 이름을 새로 제안 받는다: 혼자 있는 줄이고 사전 아이콘이 아닐 때
        rev = service.icon_review(self.project_dir) or {"groups": []}
        host = next((x for x in rev["groups"] if any(w.get("occ") == self._run_host for w in x["where"])), None)
        if host is not None and host.get("count", 1) <= 1 and not host.get("dict"):
            self._suggest(host["group"])
        else:
            self.accept()

    def _suggest(self, group: str):
        """직접 잡아 새로 생긴 줄의 이름·뜻·한국어 이름을 Claude에게 제안 받는다 (못 받아도 직접 적으면 된다)."""
        eng = service.get_settings().engine
        if not service.engine_status().get(eng):  # 연결할 수 없으면 제안 없이 닫는다 (이름은 표에서 직접 적는다)
            self.accept()
            return

        def done(res):
            self.on_saved({"suggested": group, **(res or {})})  # 표를 다시 채워 제안을 넣는다 (적어 둔 값은 그대로)
            self.accept()

        job = worker.Job(service.suggest_icon_name, self.project_dir, group, engine=eng, with_progress=False)
        worker.run_with_dialog(self, "아이콘 이름 제안 받는 중", job, done, lambda _m: self.accept(),
                               cancellable=False)
