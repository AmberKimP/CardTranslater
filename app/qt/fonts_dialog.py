"""한글 넣기 직전 글꼴 고르기 창.

- 영역마다 글꼴을 고른다. 기본 선택은 원본과 느낌이 가장 비슷한 추천(★).
- 영역을 펼치면 카드(문장)마다 따로 고를 수 있다. 기본은 ‘영역과 같음’.
- 위 미리 보기(목록 위): 왼쪽은 원본 카드의 그 영역, 오른쪽은 지운 카드 위에 고른 글꼴·원본 글자 색으로 쓴 번역 (같은 배율).
- 글자 크기: 영역마다 원본 크기 대비 배율 (한글 넣기에 그대로 쓴다).
- 정렬: 영역마다 영역 설정대로 / 왼쪽 / 가운데 / 오른쪽 (영역 지정의 정렬보다 우선한다).
- 목록: 추천 글꼴 + 이 프로젝트에서 쓴 적이 있는 글꼴 + 이 창에서 추가한 글꼴, 맨 아래 ‘글꼴 추가…’
  (PC에 설치된 글꼴 목록 또는 글꼴 파일).
화면은 app.service만 부른다.
"""

from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QFont, QFontDatabase, QPainter, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QSizePolicy, QSpinBox, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from app import service
from app.qt.dialogs import file_label, fit_to_screen, grey_note, hint_bar, page_title, set_ok_text, show_error

ADD = "__add__"
SAME = ""  # 카드 줄: 영역과 같음
ALIGN_CHOICES = [("", "영역 설정대로"), ("left", "왼쪽"), ("center", "가운데"), ("right", "오른쪽")]
ROLE_KIND = Qt.ItemDataRole.UserRole  # 트리 항목: ("region", plan_idx, region_name) | ("card", plan_idx, name, page)

_families: dict[str, str | None] = {}


def qt_family(file: str) -> str | None:
    """글꼴 파일을 화면 미리 보기용으로 등록하고 Qt 글꼴 이름을 돌려준다."""
    if file not in _families:
        fid = QFontDatabase.addApplicationFont(str(service.font_path(file)))
        fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
        _families[file] = fams[0] if fams else None
    return _families[file]


class AddFontDialog(QDialog):
    """PC에 설치된 글꼴(한글이 있고 PDF에 넣어도 되는 것) 중 하나를 고르거나, 글꼴 파일을 고른다."""

    def __init__(self, fonts: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("글꼴 추가")
        self.resize(560, 560)
        self.chosen: dict | None = None
        self.search = QLineEdit()
        self.search.setPlaceholderText("이름으로 찾기")
        self.search.textChanged.connect(self._filter)
        self.list = QListWidget()
        for f in fonts:
            it = QListWidgetItem(f["label"])
            it.setData(Qt.ItemDataRole.UserRole, f)
            it.setToolTip(f["file"])
            self.list.addItem(it)
        self.list.currentItemChanged.connect(self._preview)
        self.list.itemDoubleClicked.connect(lambda *_a: self.accept())
        self.sample = QLabel("가나다라 한글 글꼴 견본 ABC 123")
        self.sample.setMinimumHeight(60)
        self.sample.setStyleSheet("background: white; border: 1px solid #ccc; padding: 6px")
        note = QLabel(f"PC에 설치된 글꼴 중 한글이 있고 PDF에 넣어도 되는 글꼴 {len(fonts)}개")
        note.setStyleSheet("color: #666")
        note.setWordWrap(True)
        b_file = QPushButton("파일에서 찾기…")
        b_file.clicked.connect(self._from_file)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        set_ok_text(bb, "추가")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(b_file)
        row.addStretch(1)
        row.addWidget(bb)
        lay = QVBoxLayout(self)
        lay.addWidget(self.search)
        lay.addWidget(self.list, 1)
        lay.addWidget(self.sample)
        lay.addWidget(note)
        lay.addLayout(row)

    def _filter(self, text: str):
        t = text.strip().lower()
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setHidden(bool(t) and t not in it.text().lower())

    def _preview(self, it, _prev=None):
        if it is None:
            return
        fam = qt_family(it.data(Qt.ItemDataRole.UserRole)["file"])
        if fam:
            self.sample.setFont(QFont(fam, 18))

    def _from_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "글꼴 파일", "", "글꼴 (*.ttf *.otf *.ttc)")
        if not path:
            return
        info = service.font_file_info(path)
        if not info["ok"]:
            show_error(self, "이 글꼴은 쓸 수 없습니다", info["reason"], "다른 글꼴 파일을 고르세요.")
            return
        self.chosen = info
        super().accept()

    def accept(self):
        it = self.list.currentItem()
        if it is None or it.isHidden():
            return
        self.chosen = it.data(Qt.ItemDataRole.UserRole)
        super().accept()


class FontPlanDialog(QDialog):
    """plans: service.font_plans() 결과. 확인하면 choices()로 파일마다 고른 것을 돌려준다."""

    def __init__(self, plans: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("한글 글꼴 고르기")
        fit_to_screen(self, 1100, 720)
        self.plans = plans
        self.added: list[dict] = []  # 이 창에서 추가한 글꼴 {"file", "label"}
        self.combos: list[tuple[QComboBox, str]] = []  # (콤보, 종류: region|card)
        self._cb_item: dict = {}  # 콤보 -> 트리 줄
        self.sizes: dict[tuple[int, str], float] = {  # (파일 번호, 영역): 글자 크기 배율
            (pi, r["name"]): float(r.get("size_scale") or 1.0) for pi, p in enumerate(plans) for r in p["regions"]}
        self.aligns: dict[tuple[int, str], str] = {  # (파일 번호, 영역): "" (영역 설정대로) | left | center | right
            (pi, r["name"]): r.get("align") or "" for pi, p in enumerate(plans) for r in p["regions"]}

        title = page_title("한글 글꼴 고르기", 14)
        help_ = hint_bar("아래 목록에서 영역(또는 카드·문장)을 고르면 위에 원본과 미리 보기가 나옵니다. 글꼴 칸에서 바꾸세요. "
                         "★는 원본과 가장 비슷한 추천입니다. 영역을 펼치면 카드·문장마다 따로 고를 수 있고, "
                         "목록 맨 아래 ‘글꼴 추가…’로 PC의 다른 글꼴도 쓸 수 있습니다.", "info")
        notes = [(f"{file_label(p['file'])}: " if len(plans) > 1 else "") + p["note"] for p in plans if p.get("note")]
        self.note = QLabel("\n".join(notes))
        self.note.setStyleSheet("color: #9A4B00")
        self.note.setWordWrap(True)
        self.note.setVisible(bool(notes))

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["영역 / 카드 / 문장", "글꼴", "번역"])
        self.tree.setColumnWidth(0, 200)
        self.tree.setColumnWidth(1, 320)
        self.tree.header().setStretchLastSection(True)
        self.tree.setMinimumHeight(140)
        # 줄 안의 글꼴 칸이 줄 높이보다 커서 잘리지 않게 (테마의 넉넉한 칸 여백을 줄 안에서는 줄인다)
        self.tree.setStyleSheet("QComboBox { padding: 1px 26px 1px 6px; } QTreeView::item { padding: 4px 2px; }")
        self.tree.itemExpanded.connect(self._expand)
        self.tree.currentItemChanged.connect(lambda *_a: self._preview())

        box = "background: white; border: 1px solid #E6E9EE; border-radius: 8px; padding: 4px"
        self._orig_pm = QPixmap()
        self._sample_pm = QPixmap()
        self.orig = QLabel()
        self.sample = QLabel()
        for lb in (self.orig, self.sample):
            lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lb.setMinimumSize(200, 100)
            lb.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)  # 그림이 창을 넓히지 않게
            lb.setStyleSheet(box)
        self.sample.setWordWrap(True)
        self.where = QLabel()
        self.where.setStyleSheet("font-weight: bold; color: #0b5cad")
        self.sample_note = QLabel()
        self.sample_note.setStyleSheet("color: #5F6B7A")
        self.sample_note.setTextFormat(Qt.TextFormat.RichText)
        self.sample_note.setWordWrap(True)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        cap_o, cap_s = QLabel("원본"), QLabel("미리 보기 (고른 글꼴 · 원본 글자 색 · 지운 카드 위)")
        for c in (cap_o, cap_s):
            c.setStyleSheet("color: #334E68; font-weight: bold")
        grid.addWidget(cap_o, 0, 0)
        grid.addWidget(cap_s, 0, 1)
        grid.addWidget(self.orig, 1, 0)
        grid.addWidget(self.sample, 1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(1, 1)

        self.size_label = QLabel("글자 크기")
        self.size = QSpinBox()
        self.size.setRange(50, 200)
        self.size.setSingleStep(5)
        self.size.setSuffix(" %")
        self.size.setMinimumWidth(90)
        self.size.setToolTip("원본 글자 크기 대비 배율입니다. 한글 넣기에서 이 크기로 시작해, 칸에 넘치면 조금씩 줄입니다.")
        self.size.valueChanged.connect(self._size_changed)
        b_reset = QPushButton("원본 크기로")
        b_reset.clicked.connect(lambda: self.size.setValue(100))
        size_row = QHBoxLayout()
        size_row.addWidget(self.size_label)
        size_row.addWidget(self.size)
        size_row.addWidget(b_reset)
        size_row.addSpacing(24)
        self.align_label = QLabel("정렬")
        self.align = QComboBox()
        for key, text in ALIGN_CHOICES:
            self.align.addItem(text, key)
        self.align.setToolTip("이 영역의 모든 카드에서 한글을 놓는 방법입니다.\n"
                              "영역 설정대로: 영역 지정에서 정한 정렬 (기본은 원문 줄을 따라감)")
        self.align.currentIndexChanged.connect(self._align_changed)
        size_row.addWidget(self.align_label)
        size_row.addWidget(self.align)
        size_row.addStretch(1)

        # 위: 미리 보기 묶음, 아래: 목록 (사용자 요구: 원본/미리 보기 그림이 목록 위)
        self.preview_box = QWidget()
        pv = QVBoxLayout(self.preview_box)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.addWidget(self.where)
        pv.addLayout(grid, 1)
        pv.addWidget(self.sample_note)
        pv.addLayout(size_row)
        self.split = QSplitter(Qt.Orientation.Vertical)
        self.split.addWidget(self.preview_box)
        self.split.addWidget(self.tree)
        self.split.setChildrenCollapsible(False)
        self.split.setStretchFactor(0, 2)
        self.split.setStretchFactor(1, 3)
        self.split.splitterMoved.connect(lambda *_a: QTimer.singleShot(0, self._fit_previews))

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        set_ok_text(bb, "이대로 한글 넣기")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        self.b_all_rec = QPushButton("모두 추천으로")
        self.b_all_rec.setToolTip("모든 영역을 ★ 추천 글꼴로, 카드·문장은 ‘영역과 같음’으로 되돌립니다")
        self.b_all_rec.clicked.connect(self._all_recommended)
        bottom = QHBoxLayout()
        bottom.addWidget(self.b_all_rec)
        bottom.addWidget(grey_note("누르면 고른 글꼴을 저장하고 바로 한글 넣기를 시작합니다."), 1)
        bottom.addWidget(bb)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(8)
        lay.addWidget(title)
        lay.addWidget(help_)
        lay.addWidget(self.note)
        lay.addWidget(self.split, 1)
        lay.addLayout(bottom)
        self._build()
        self.split.setSizes([300, 400])

    # ---- 목록
    def _options(self, pi: int, name: str, level: str) -> list[tuple[str, str, str]]:
        """콤보 항목 [(보이는 글, 파일, 설명)]. 구분선은 ("", "-", ""). level: region | card | sentence."""
        plan = self.plans[pi]
        reg = next(r for r in plan["regions"] if r["name"] == name)
        out: list[tuple[str, str, str]] = []
        if level == "card":
            out.append(("(영역과 같음)", SAME, "이 영역에서 고른 글꼴을 씁니다"))
        elif level == "sentence":
            out.append(("(카드와 같음)", SAME, "이 카드에서 고른 글꼴을 씁니다"))
        seen = set()
        for i, r in enumerate(reg["recommended"]):
            mark = "★ " if i == 0 else "    "
            by = {"claude": "추천", "shape": "모양 비슷", "default": "기본"}.get(r.get("by", ""), "")
            out.append((f"{mark}{r['label']}", r["file"], f"{by}: {r.get('reason', '')}"))
            seen.add(r["file"])
        picked = self._picked_fonts()  # 다른 영역·카드·문장에서 고른 글꼴도 고를 수 있게
        others = [u for u in plan.get("used", []) + self.added + picked if u["file"] not in seen]
        if reg.get("selected") and reg["selected"] not in seen and all(u["file"] != reg["selected"] for u in others):
            others.insert(0, {"file": reg["selected"], "label": Path(reg["selected"]).stem})
        if others:
            out.append(("", "-", ""))
            for u in others:
                if u["file"] in seen:
                    continue
                seen.add(u["file"])
                src = ("추가한 글꼴" if u in self.added else "이 창에서 고른 글꼴" if u in picked
                       else "이 프로젝트에서 쓴 글꼴")
                out.append((f"    {u['label']}", u["file"], src))
        out.append(("", "-", ""))
        out.append(("글꼴 추가…", ADD, "PC에 설치된 다른 글꼴이나 글꼴 파일을 추가합니다"))
        return out

    def _picked_fonts(self) -> list[dict]:
        """이 창에서 지금 고른 글꼴 (모든 영역·카드·문장, 펼치지 않은 줄은 저장된 선택) [{"file", "label"}]."""
        files: list[str] = []

        def add(f):
            if f and f not in (SAME, ADD) and f not in files:
                files.append(f)

        for plan in self.plans:
            for reg in plan["regions"]:
                add(reg.get("selected"))
                for c in reg["cards"]:
                    add(c.get("selected"))
                    for x in c.get("sentences") or []:
                        add(x.get("selected"))
        for cb, _kind in getattr(self, "combos", []):
            add(cb.currentData())
        return [{"file": f, "label": service.font_label(f)} for f in files]

    def _fill(self, combo: QComboBox, pi: int, name: str, level: str, selected: str | None):
        combo.blockSignals(True)
        combo.clear()
        for text, file, tip in self._options(pi, name, level):
            if file == "-":
                combo.insertSeparator(combo.count())
                continue
            combo.addItem(text, file)
            combo.setItemData(combo.count() - 1, tip, Qt.ItemDataRole.ToolTipRole)
        idx = combo.findData(selected if selected is not None else (SAME if level != "region" else None))
        combo.setCurrentIndex(max(idx, 0))
        combo.setProperty("prev", combo.currentData())
        combo.blockSignals(False)

    def _combo(self, item: QTreeWidgetItem, pi: int, name: str, level: str, selected: str | None) -> QComboBox:
        cb = QComboBox()
        cb.setProperty("meta", (pi, name, level))
        self._fill(cb, pi, name, level, selected)
        cb.currentIndexChanged.connect(lambda _i, c=cb, it=item: self._changed(c, it))
        self.tree.setItemWidget(item, 1, cb)
        self.combos.append((cb, level))
        self._cb_item[cb] = item
        self._mark(cb)
        return cb

    def _mark(self, cb: QComboBox):
        """사람이 추천과 다르게 고른 줄은 0열을 하늘색으로 (카드·문장 줄은 ‘같음’이 아닐 때)."""
        item = self._cb_item.get(cb)
        if item is None:
            return
        pi, name, level = cb.property("meta")
        if level == "region":
            rec = self._reg(pi, name)["recommended"]
            changed = bool(rec) and cb.currentData() not in (rec[0]["file"], None)
        else:
            changed = cb.currentData() not in (SAME, None)
        item.setBackground(0, QBrush(QColor("#CFE2F3")) if changed else QBrush())

    def _reg(self, pi: int, name: str) -> dict:
        return next(r for r in self.plans[pi]["regions"] if r["name"] == name)

    def _build(self):
        many = len(self.plans) > 1
        for pi, plan in enumerate(self.plans):
            parent = self.tree.invisibleRootItem()
            if many:
                parent = QTreeWidgetItem([plan["file"]])
                self.tree.addTopLevelItem(parent)
                parent.setExpanded(True)
            for reg in plan["regions"]:
                first = next((c["text"] for c in reg["cards"] if c["text"]), "")
                label = reg["name"] + ("  (문장마다)" if reg.get("font_mode") == "sentence" else "")
                it = QTreeWidgetItem([label, "", first[:60]])
                it.setData(0, ROLE_KIND, ("region", pi, reg["name"]))
                it.setToolTip(2, first)
                tips = []
                orig = next((r.get("original_style") for r in reg["recommended"] if r.get("original_style")), None)
                if orig:
                    tips.append(f"원문 글씨 (Claude 판단): {orig}")
                if reg.get("font_mode") == "sentence":
                    tips.append("문장마다 글꼴을 고를 수 있는 영역입니다 (카드를 펼치면 문장 목록)")
                if tips:
                    it.setToolTip(0, "\n".join(tips))
                parent.addChild(it)
                if reg["cards"]:
                    it.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
                self._combo(it, pi, reg["name"], "region", reg["selected"])
                picked = any(c["selected"] or any(x["selected"] for x in c.get("sentences") or [])
                             for c in reg["cards"])
                if picked:  # 카드·문장별로 고른 것이 있으면 펼쳐 보인다
                    it.setExpanded(True)
        top = self.tree.topLevelItem(0)
        if top is not None:
            self.tree.setCurrentItem(top.child(0) if many and top.childCount() else top)

    def _expand(self, item: QTreeWidgetItem):
        kind = item.data(0, ROLE_KIND)
        if not kind or item.childCount():
            return
        if kind[0] == "region":
            _k, pi, name = kind
            reg = self._reg(pi, name)
            for c in reg["cards"]:
                ch = QTreeWidgetItem([f"{c['page']}쪽", "", c["text"][:80] or "(번역 없음)"])
                ch.setData(0, ROLE_KIND, ("card", pi, name, c["page"]))
                ch.setToolTip(2, c["text"])
                item.addChild(ch)
                self._combo(ch, pi, name, "card", c["selected"] or SAME)
                if c.get("sentences"):
                    ch.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
                    if any(x["selected"] for x in c["sentences"]):
                        ch.setExpanded(True)
        elif kind[0] == "card":
            _k, pi, name, page = kind
            card = next(c for c in self._reg(pi, name)["cards"] if c["page"] == page)
            for x in card.get("sentences") or []:
                ch = QTreeWidgetItem([f"문장 {x['idx'] + 1}", "", x["text"][:80]])
                ch.setData(0, ROLE_KIND, ("sentence", pi, name, page, x["idx"]))
                ch.setToolTip(2, x["text"])
                item.addChild(ch)
                self._combo(ch, pi, name, "sentence", x["selected"] or SAME)

    # ---- 고르기
    def _changed(self, combo: QComboBox, item: QTreeWidgetItem):
        if combo.currentData() == ADD:
            new = self._add_font()
            if new is None:
                combo.blockSignals(True)
                combo.setCurrentIndex(max(combo.findData(combo.property("prev")), 0))
                combo.blockSignals(False)
                return
            if all(a["file"] != new["file"] for a in self.added):
                self.added.append(new)
            for cb, _kind in self.combos:  # 모든 목록에 추가하고, 이 줄은 새 글꼴로
                pi, name, level = cb.property("meta")
                keep = new["file"] if cb is combo else cb.currentData()
                self._fill(cb, pi, name, level, keep)
        else:  # 고른 글꼴이 다른 줄의 목록에 없으면 넣는다 (그 줄의 선택은 그대로)
            f = combo.currentData()
            if f not in (SAME, ADD, None):
                for cb, _kind in self.combos:
                    if cb is not combo and cb.findData(f) < 0:
                        pi, name, level = cb.property("meta")
                        self._fill(cb, pi, name, level, cb.currentData())
        combo.setProperty("prev", combo.currentData())
        for cb, _kind in self.combos:
            self._mark(cb)
        self.tree.setCurrentItem(item)
        self._preview()

    def _add_font(self) -> dict | None:
        from PySide6.QtGui import QGuiApplication

        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            fonts = service.installed_fonts()  # 처음 한 번은 몇 초 걸린다
        finally:
            QGuiApplication.restoreOverrideCursor()
        dlg = AddFontDialog(fonts, self)
        if not dlg.exec() or not dlg.chosen:
            return None
        try:
            file = service.prepare_font(dlg.chosen["file"])  # 글꼴 모음(.ttc)이면 그 글꼴만 .ttf로 꺼낸다
        except Exception as e:  # noqa: BLE001
            show_error(self, "글꼴을 쓸 수 없습니다", str(e), "다른 글꼴을 고르세요.")
            return None
        return {"file": file, "label": dlg.chosen["label"]}

    def _row_font(self, item: QTreeWidgetItem) -> str | None:
        cb = self.tree.itemWidget(item, 1)
        if cb is None:
            return None
        f = cb.currentData()
        if f == SAME and item.parent() is not None:
            return self._row_font(item.parent())
        return f if f not in (ADD, SAME) else None

    def _where(self, item: QTreeWidgetItem) -> tuple[int, str, int | None] | None:
        """고른 줄의 (파일 번호, 영역, 쪽). 항목 줄이면 번역이 있는 첫 카드."""
        kind = item.data(0, ROLE_KIND)
        if not kind:
            return None
        pi, name = kind[1], kind[2]
        if kind[0] == "region":
            cards = self._reg(pi, name)["cards"]
            c = next((c for c in cards if c["text"]), cards[0] if cards else None)
            return pi, name, (c["page"] if c else None)
        return pi, name, kind[3]

    def _size_changed(self, v: int):
        item = self.tree.currentItem()
        w = self._where(item) if item is not None else None
        if w is None:
            return
        self.sizes[(w[0], w[1])] = v / 100.0
        self._preview()

    def _align_changed(self, _i: int):
        item = self.tree.currentItem()
        w = self._where(item) if item is not None else None
        if w is None:
            return
        self.aligns[(w[0], w[1])] = self.align.currentData() or ""
        self._preview()

    def _preview(self):
        item = self.tree.currentItem()
        if item is None:
            return
        text = item.toolTip(2) or item.text(2) or "가나다라 한글 글꼴 견본"
        f = self._row_font(item)
        fam = qt_family(f) if f else None
        w = self._where(item)
        scale = self.sizes.get((w[0], w[1]), 1.0) if w else 1.0
        self.size.blockSignals(True)
        self.size.setEnabled(w is not None)
        self.size.setValue(round(scale * 100))
        self.size.blockSignals(False)
        self.size_label.setText(f"글자 크기 (‘{w[1]}’ 영역의 모든 카드)" if w else "글자 크기")
        al = self.aligns.get((w[0], w[1]), "") if w else ""
        pills = bool(w) and self._reg(w[0], w[1]).get("kind") == "tag_pills"  # 태그 라벨은 정렬을 쓰지 않는다
        self.align.blockSignals(True)
        self.align.setCurrentIndex(max(self.align.findData(al), 0))
        self.align.setEnabled(w is not None and not pills)
        self.align.blockSignals(False)
        self.where.setText(self._where_text(item, w))
        info = None
        if w and w[2] is not None:
            try:
                info = service.font_preview(self.plans[w[0]]["file_dir"], w[2], w[1], f, scale)
            except Exception:  # noqa: BLE001 - 미리 보기를 못 만들어도 고르기는 된다
                info = None
        if info is not None and al and not pills:
            info = {**info, "align": al}
        if info is None:
            self._orig_pm = QPixmap()
            self._sample_pm = QPixmap()
            self.orig.setPixmap(QPixmap())
            self.orig.setText("(원본 그림 없음)")
            if fam:
                self.sample.setFont(QFont(fam, round(16 * scale)))
            self.sample.setPixmap(QPixmap())
            self.sample.setText(text[:200])
        else:
            op = QPixmap()
            op.loadFromData(info["original_png"])
            self._orig_pm = op
            self._sample_pm = self._draw(info, text, fam)
            self.orig.setText("")
            self.sample.setText("")
            self._fit_previews()
        cb = self.tree.itemWidget(item, 1)
        note = cb.currentText().strip().lstrip("★").strip() if cb else ""
        reason = ""
        if cb is not None and cb.currentIndex() >= 0:
            tip = cb.itemData(cb.currentIndex(), Qt.ItemDataRole.ToolTipRole) or ""
            reason = tip.split(": ", 1)[1] if ": " in tip else ""
        if cb is not None and cb.currentData() == SAME and item.parent() is not None:
            note = "위와 같음: " + (service.font_label(f) if f else "")
            reason = ""
        elif cb is not None:
            note = cb.currentText().strip()
        if reason:
            note = f"추천 이유: {reason} · {note}"
        note = html.escape(note, quote=False)
        if info is not None:  # 색 값(#…)·pt 대신 색 견본과 크기 숫자로
            note += (f'&nbsp;&nbsp;·&nbsp;&nbsp;글자 색 <span style="color:{info["color"]}">■</span> 원본과 같게'
                     f'&nbsp;&nbsp;·&nbsp;&nbsp;시작 글자 크기 {info["size_pt"]:.0f}')
        self.sample_note.setText(note)

    def _where_text(self, item: QTreeWidgetItem, w) -> str:
        """미리 보는 대상: 어느 영역·쪽의 것인지."""
        if w is None:
            return ""
        pi, name, page = w
        kind = item.data(0, ROLE_KIND)
        who = f"{self.plans[pi]['file']} · " if len(self.plans) > 1 else ""
        if page is None:
            return f"{who}{name} 영역 (번역 있는 카드 없음)"
        if kind and kind[0] == "sentence":
            return f"{who}{name} 영역 · {page}쪽 · 문장 {kind[4] + 1}"
        if kind and kind[0] == "card":
            return f"{who}{name} 영역 · {page}쪽 카드"
        return f"{who}{name} 영역 · {page}쪽 카드로 미리 봅니다"

    def _fit_previews(self):
        """보관한 그림을 칸 크기에 맞춰 줄여 넣는다 (원래보다 키우지는 않는다)."""
        try:
            for lb, pm in ((self.orig, self._orig_pm), (self.sample, self._sample_pm)):
                if pm.isNull():
                    continue
                avail = lb.contentsRect().size()
                avail.setWidth(max(avail.width() - 10, 20))
                avail.setHeight(max(avail.height() - 10, 20))
                if pm.width() > avail.width() or pm.height() > avail.height():
                    pm = pm.scaled(avail, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                lb.setPixmap(pm)
        except RuntimeError:  # 창이 닫혀 지워진 뒤에 늦게 불린 경우
            pass

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        QTimer.singleShot(0, self._fit_previews)

    def showEvent(self, ev):
        super().showEvent(ev)
        QTimer.singleShot(0, self._fit_previews)

    def _all_recommended(self):
        """모든 영역 콤보를 ★ 추천으로, 카드·문장 콤보를 ‘같음’으로."""
        for cb, level in self.combos:
            if level == "region":
                pi, name, _lv = cb.property("meta")
                rec = self._reg(pi, name)["recommended"]
                idx = cb.findData(rec[0]["file"]) if rec else -1
                if idx >= 0:
                    self._set_quiet(cb, idx)
            else:
                idx = cb.findData(SAME)
                if idx >= 0:
                    self._set_quiet(cb, idx)
        self._preview()

    def _set_quiet(self, cb: QComboBox, idx: int):
        cb.blockSignals(True)
        cb.setCurrentIndex(idx)
        cb.setProperty("prev", cb.currentData())
        cb.blockSignals(False)
        self._mark(cb)

    @staticmethod
    def _draw(info: dict, text: str, fam: str | None) -> QPixmap:
        """지운 카드 그림 위에 번역을 원본 글자 색·고른 크기로 쓴다 (칸 너비에서 줄바꿈)."""
        pm = QPixmap()
        pm.loadFromData(info["cleared_png"])
        z = info["zoom"]
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        font = QFont(fam) if fam else QFont()
        font.setPixelSize(max(1, round(info["size_pt"] * z)))
        p.setFont(font)
        p.setPen(QColor(info["color"]))
        h = {"center": Qt.AlignmentFlag.AlignHCenter, "right": Qt.AlignmentFlag.AlignRight}.get(
            info["align"], Qt.AlignmentFlag.AlignLeft)
        bx = info.get("text_box_pt") or [0, 0, info["w_pt"], info["h_pt"]]
        r = QRectF(bx[0] * z, bx[1] * z, (bx[2] - bx[0]) * z, (bx[3] - bx[1]) * z)
        p.drawText(r, int(h | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap), text)
        p.end()
        return pm

    # ---- 결과
    def choices(self) -> list[dict]:
        """파일마다 {"file_dir", "regions": {영역: 글꼴}, "cards": {쪽: {영역: 글꼴}}, "sizes": {영역: 크기 배율},
        "sentences": {쪽: {영역: {문장 번호: 글꼴}}}}. 펼치지 않은 줄의 카드·문장별 선택은 예전 것을 그대로 둔다."""
        out = []
        for plan in self.plans:
            cards, sents = {}, {}
            for reg in plan["regions"]:
                for c in reg["cards"]:
                    if c["selected"]:
                        cards.setdefault(c["page"], {})[reg["name"]] = c["selected"]
                    for x in c.get("sentences") or []:
                        if x["selected"]:
                            sents.setdefault(c["page"], {}).setdefault(reg["name"], {})[x["idx"]] = x["selected"]
            out.append({"file_dir": plan["file_dir"], "regions": {}, "cards": cards, "sentences": sents,
                        "sizes": {name: v for (pi, name), v in self.sizes.items() if pi == len(out)},
                        "aligns": {name: v for (pi, name), v in self.aligns.items() if pi == len(out) and v}})
        it = self.tree.invisibleRootItem()
        stack = [it.child(i) for i in range(it.childCount())]
        while stack:
            item = stack.pop()
            stack += [item.child(i) for i in range(item.childCount())]
            kind = item.data(0, ROLE_KIND)
            cb = self.tree.itemWidget(item, 1)
            if not kind or cb is None:
                continue
            f = cb.currentData()
            keep = f not in (SAME, ADD, None)
            if kind[0] == "region":
                out[kind[1]]["regions"][kind[2]] = f
            elif kind[0] == "card":
                _k, pi, name, page = kind
                regs = out[pi]["cards"].setdefault(page, {})
                if keep:
                    regs[name] = f
                else:
                    regs.pop(name, None)
            else:
                _k, pi, name, page, idx = kind
                regs = out[pi]["sentences"].setdefault(page, {}).setdefault(name, {})
                if keep:
                    regs[idx] = f
                else:
                    regs.pop(idx, None)
        for o in out:
            o["cards"] = {pg: r for pg, r in o["cards"].items() if r}
            o["sentences"] = {pg: {n: v for n, v in r.items() if v} for pg, r in o["sentences"].items()}
            o["sentences"] = {pg: r for pg, r in o["sentences"].items() if r}
        return out
