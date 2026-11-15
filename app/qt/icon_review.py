"""② 아이콘 정의 창(이름·뜻·세트 정하기)과 아이콘 사전 창."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QCompleter, QDialog, QDialogButtonBox, QDoubleSpinBox, QHeaderView, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)
from app import service
from app.qt.dialogs import (
    ask_choice, card, fit_to_screen, grey_note, hint_bar, page_title, set_hint, set_ok_text, show_problem, subtitle,
)
from app.qt.icon_place import IconPlaceDialog


# ================================================================ 아이콘 사전 (표)
class IconDictionaryPanel(QWidget):
    """게임 아이콘 사전 표: 한국어 이름·뜻·표시 크기를 고치고, ‘다시 정하기’로 원문에서 [[?]]로 되돌린다.
    아이콘 사전 창(새로 정할 아이콘이 없을 때)과 ② 아이콘 정의 창의 ‘사전 전체’ 탭이 함께 쓴다.
    allow_reset=False: 새 아이콘을 정하는 중에는 ‘다시 정하기’를 막는다 (되돌리면 찾은 결과가 맞지 않게 됨)."""

    COLS = ["그림", "이름", "한국어 이름", "뜻(원문)", "표시 크기", "쓰인 곳", "다시 정하기"]
    UNUSED = "지금 프로젝트에서는 안 쓰임 (다음 프로젝트에서 자동으로 알아봄)"

    def __init__(self, rows: list[dict], intro: str = "", parent=None, allow_reset: bool = True):
        super().__init__(parent)
        self.dirty = False  # 고친 것이 있음 (저장 전)
        # 지금 프로젝트에서 안 쓰인 아이콘은 아래로 (같은 무리 안에서는 받은 순서 그대로). rows()가 이 순서를 따른다
        self._rows = rows = sorted(rows, key=lambda r: not r.get("uses"))
        lab = subtitle(intro)
        lab.setVisible(bool(intro))
        reset_note = grey_note("‘다시 정하기’를 체크하면 그 아이콘을 원문에서 되돌리고, 저장한 뒤 다시 찾아 이름을 정합니다."
                               if allow_reset else
                               "사전은 게임 단위라 고치면 같은 게임의 다른 프로젝트에도 쓰입니다. "
                               "‘다시 정하기’는 새 아이콘 이름을 확정한 뒤 아이콘 사전에서 할 수 있습니다.")
        self.table = QTableWidget(len(rows), len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setStyleSheet("QDoubleSpinBox { margin: 7px 6px; padding-top: 1px; padding-bottom: 1px; }"
                                 " QCheckBox { margin-left: 12px; }")
        self.scales: list[QDoubleSpinBox] = []
        self.resets: list[QCheckBox] = []
        for i, r in enumerate(rows):
            unused = not r.get("uses")
            img = QTableWidgetItem()
            if r.get("image"):
                pm = QPixmap(r["image"])
                if not pm.isNull():
                    pm = pm.scaledToHeight(40, Qt.TransformationMode.SmoothTransformation)
                    img.setData(Qt.ItemDataRole.DecorationRole, self._faded(pm) if unused else pm)
            img.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.table.setItem(i, 0, img)
            name = QTableWidgetItem(r["name"])
            name.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(i, 1, name)
            self.table.setItem(i, 2, QTableWidgetItem(r.get("ko", "")))
            self.table.setItem(i, 3, QTableWidgetItem(r.get("meaning", "")))
            sp = QDoubleSpinBox()
            sp.setRange(0.3, 4.0)  # ② 정의 줄(0.3~)과 같게: 작게 정한 크기가 저장할 때 바뀌지 않게
            sp.setSingleStep(0.1)
            sp.setValue(float(r.get("scale") or 1.1))
            sp.setToolTip("한글 글자 높이 대비 아이콘 높이")
            self.table.setCellWidget(i, 4, sp)
            self.scales.append(sp)
            uses = QTableWidgetItem(f"{r.get('uses', 0)}곳" + (f" · {self.UNUSED}" if unused else ""))
            uses.setFlags(Qt.ItemFlag.ItemIsEnabled)
            if unused:
                uses.setToolTip(self.UNUSED)
            self.table.setItem(i, 5, uses)
            if unused:  # 흐리게 (고치기는 그대로 된다)
                for j in (1, 2, 3, 5):
                    self.table.item(i, j).setForeground(QColor("#999"))
            cb = QCheckBox()
            cb.setToolTip("원문의 이 아이콘을 [[?]]로 되돌리고 사전에서 뺍니다. 다시 찾아 새로 묶고 이름을 정합니다")
            cb.setEnabled(allow_reset)
            cb.toggled.connect(self._touch)
            sp.valueChanged.connect(self._touch)
            self.table.setCellWidget(i, 6, cb)
            self.resets.append(cb)
            self.table.setRowHeight(i, 48)
        for i in range(len(rows)):
            self._paint_ko(i)
        self.dirty = False
        self.table.itemChanged.connect(self._item_changed)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(lab)
        lay.addWidget(reset_note)
        lay.addWidget(self.table, 1)

    def _touch(self, *_a):
        self.dirty = True

    def _item_changed(self, it):
        if it.column() == 2:
            self._paint_ko(it.row())
        self.dirty = True

    @staticmethod
    def _faded(pm: QPixmap) -> QPixmap:
        """안 쓰인 아이콘 그림: 흐리게."""
        out = QPixmap(pm.size())
        out.setDevicePixelRatio(pm.devicePixelRatio())
        out.fill(Qt.GlobalColor.transparent)
        p = QPainter(out)
        p.setOpacity(0.35)
        p.drawPixmap(0, 0, pm)
        p.end()
        return out

    def _paint_ko(self, i: int):
        """한국어 이름이 빈 줄은 노랑 (입력하면 지움)."""
        it = self.table.item(i, 2)
        if it is None:
            return
        self.table.blockSignals(True)
        it.setBackground(QBrush(QColor("#FFE066")) if not it.text().strip() else QBrush())
        self.table.blockSignals(False)

    def rows(self) -> list[dict]:
        return [{"name": r["name"], "ko": self.table.item(i, 2).text().strip(),
                 "meaning": self.table.item(i, 3).text().strip(), "scale": self.scales[i].value(),
                 "reset": self.resets[i].isChecked()} for i, r in enumerate(self._rows)]


class IconDictionaryDialog(QDialog):
    """새로 정할 아이콘이 없을 때 여는 아이콘 사전 창 (표는 IconDictionaryPanel)."""

    COLS = IconDictionaryPanel.COLS
    UNUSED = IconDictionaryPanel.UNUSED

    def __init__(self, rows: list[dict], intro: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("아이콘 사전")
        fit_to_screen(self, 900, 560)
        self.panel = IconDictionaryPanel(rows, intro, self)
        self.table, self.scales, self.resets, self._rows = (self.panel.table, self.panel.scales, self.panel.resets,
                                                            self.panel._rows)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        set_ok_text(bb, "저장")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(8)
        lay.addWidget(page_title("아이콘 사전", 14))
        lay.addWidget(self.panel, 1)
        lay.addWidget(bb)

    def rows(self) -> list[dict]:
        return self.panel.rows()



class IconReviewDialog(QDialog):
    """이번 찾기에서 발견한 모든 아이콘의 이름·뜻·한국어 이름을 정한다 (엑셀 대신). 카드의 아이콘 하나 = 한 줄.

    줄은 카드 순서로 묶여 보이고(같은 카드는 같은 띠 색), 손볼 것이 있는 카드가 맨 위에 온다. 사전과 모양이 같은
    아이콘은 이름이 미리 채워져 있고, 이미 이름이 정해진 아이콘(확정됨)도 보여 이름을 바꾸거나 ‘아이콘 아님’으로 할 수
    있다 (그대로 두면 원문은 바뀌지 않는다). 같은 이름을 적은 줄들은 한 아이콘이다. ‘카드 보기’로 그 카드에서
    아이콘 자리를 다시 잡고, 빠진 아이콘을 넣고, 합치거나 나눈다."""

    COLS = ["그림", "이름", "뜻(원문)", "한국어 이름", "아이콘 아님", "표시 크기", "상태", "카드", "카드 보기", "예문"]
    C_STATE, C_PAGES, C_VIEW, C_EX = 6, 7, 8, 9
    CONFLICT_COLOR = "#F4B183"  # 같은 이름인데 한국어 이름·뜻이 다른 줄

    def __init__(self, project_dir: Path, review: dict, parent=None):
        super().__init__(parent)
        self.project_dir = Path(project_dir)
        self.result = None
        self.setWindowTitle("② 아이콘 정의")
        fit_to_screen(self, 1400, 640, maximize_below=0)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setIconSize(QSize(40, 40))
        self.table.setStyleSheet("QLineEdit, QComboBox, QDoubleSpinBox, QPushButton { margin: 5px 4px; }"
                                 " QLineEdit, QDoubleSpinBox { padding-top: 1px; padding-bottom: 1px; }"
                                 " QCheckBox { margin-left: 12px; }")
        self.table.horizontalHeader().setSectionResizeMode(self.C_EX, QHeaderView.ResizeMode.Stretch)
        self.table.setAutoScroll(False)  # 마우스를 표 아래쪽으로 옮길 때 저절로 내려가지 않게
        self.table.setVerticalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)
        # 기본: 카드 번호로 묶어 정렬 (문제 카드 먼저). 머리글을 누르면 그 열로 정렬 (다시 누르면 반대)
        self._sort: tuple[int, bool] | None = (self.C_PAGES, False)
        self._card_of: dict = {}  # 카드 번호로 정렬했을 때 줄 -> ((파일, 쪽), 문제 카드인가)
        hh = self.table.horizontalHeader()
        hh.setSectionsClickable(True)
        hh.sectionClicked.connect(self._sort_by)
        self.steps_text = hint_bar("", "info")
        self.known_note = grey_note("")
        self.same_shape = QCheckBox("이름·뜻·한국어 이름을 고치면 모양이 같은 줄(같은 값이던 줄)도 함께 고치기")
        self.same_shape.setChecked(True)
        self.same_shape.setToolTip("같은 아이콘이 카드마다 한 줄씩 있습니다. 켜 두면 한 줄을 고칠 때 모양이 같고 값이 같던 "
                                   "다른 줄도 같이 바뀝니다.\n한 줄만 고치려면 끄세요.")
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        set_ok_text(bb, "이름 확정")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        # 탭 둘: 아이콘 (이번 찾기의 모든 아이콘) / 사전 전체 (이미 정한 아이콘의 한국어 이름·뜻·크기 고치기)
        new_tab = QWidget()
        nl = QVBoxLayout(new_tab)
        nl.setContentsMargins(8, 8, 8, 8)
        nl.setSpacing(8)
        nl.addWidget(self.steps_text)
        nl.addWidget(self.known_note)
        nl.addWidget(self.same_shape)
        nl.addWidget(self.table, 1)
        try:
            dict_rows = service.icon_dictionary(self.project_dir)
        except Exception:  # noqa: BLE001 - 사전을 못 읽어도 이름 정하기는 된다
            dict_rows = []
        self._dict_images = {r["name"]: r.get("image") for r in dict_rows}
        self._dict_rows = {r["name"]: r for r in dict_rows}
        self.dict_panel = IconDictionaryPanel(dict_rows, "", None, allow_reset=False)
        self.dict_save = QPushButton("사전 저장")
        self.dict_save.setToolTip("이 탭에서 고친 한국어 이름·뜻·표시 크기를 게임 아이콘 사전에 저장합니다")
        self.dict_save.clicked.connect(self.save_dictionary)
        dict_tab = QWidget()
        dl = QVBoxLayout(dict_tab)
        dl.setContentsMargins(8, 8, 8, 8)
        dl.addWidget(self.dict_panel, 1)
        dl.addWidget(self.dict_save, alignment=Qt.AlignmentFlag.AlignRight)
        self.tabs = QTabWidget()
        self.tabs.addTab(new_tab, f"아이콘 ({len(review['groups'])})")
        self.tabs.addTab(dict_tab, f"사전 전체 ({len(dict_rows)})")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(8)
        lay.addWidget(page_title("② 아이콘 정의", 14))
        lay.addWidget(self.tabs, 1)
        lay.addWidget(bb)
        self._fill(review, {})

    def save_dictionary(self) -> bool:
        """‘사전 전체’ 탭에서 고친 것을 저장한다 (다시 정하기는 이 탭에서 막혀 있다)."""
        try:
            service.update_icon_dictionary(self.project_dir, self.dict_panel.rows())
        except Exception as e:  # noqa: BLE001
            show_problem(self, "사전을 저장하지 못했습니다", str(e), "")
            return False
        self.dict_panel.dirty = False
        try:  # 사전 값을 쓰는 줄(이름이 사전에 있는 줄)에 새 값을 보인다
            self._dict_rows = {r["name"]: r for r in service.icon_dictionary(self.project_dir)}
        except Exception:  # noqa: BLE001
            pass
        for i in range(len(getattr(self, "widgets", []))):
            self._sync_row(i)
        return True

    def _dict_unsaved_ok(self) -> bool:
        """사전 탭에 저장 안 한 고침이 있으면 저장할지 묻는다. 계속해도 되면 True."""
        if not self.dict_panel.dirty:
            return True
        r = ask_choice(self, "아이콘 사전", "‘사전 전체’ 탭에서 고친 것을 저장할까요?",
                       [("저장", "accept"), ("저장 안 함", "destructive"), ("취소", "reject")], default=0)
        if r == 0:
            return self.save_dictionary()
        return r == 1

    def reject(self):
        if not self._dict_unsaved_ok():
            return
        # 카드 보기에서 확정 아이콘을 다시 잡았거나(원문이 [[?]]로 열림) 아이콘을 추가한 줄은 원문이 이미 바뀌었다:
        # 이름 확정 없이 닫으면 그 자리가 [[?]]로 남는다
        groups = (getattr(self, "review", None) or {}).get("groups") or []
        opened = [g for g in groups if g.get("added") or g.get("dict_how") == "prev"]
        if opened:
            r = ask_choice(self, "② 아이콘 정의", f"카드 보기에서 다시 잡거나 추가한 아이콘 {len(opened)}곳이 아직 확정되지 않았습니다.",
                           [("닫기", "destructive"), ("돌아가기", "reject")], default=1,
                           informative="닫으면 그 자리는 원문에 이름 없는 아이콘([[?]])으로 남고, 다음에 ② 아이콘 정의를 "
                                       "열면 다시 보입니다. ‘이름 확정’을 누르면 정한 이름으로 바뀝니다.")
            if r != 0:
                return
        super().reject()

    # ---- 표 채우기 (카드 보기에서 바꾸면 다시 채운다; 적어 둔 값은 줄별로 살린다)
    def _fill(self, review: dict, keep: dict):
        review = {**review, "groups": self._sorted(review["groups"], keep)}
        self.review = review
        groups = review["groups"]
        self.table.clearContents()
        self.table.setRowCount(len(groups))
        self.widgets = []
        known = list(review.get("known") or [])
        completer = QCompleter(known, self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        for i, g in enumerate(groups):
            pic = g.get("image") or (g.get("dict_image") if g.get("named") else None)
            img = QTableWidgetItem("" if pic else "위치 못 찾음")
            if pic:
                img.setIcon(QIcon(QPixmap(pic)))
                if not g.get("image"):
                    img.setToolTip("카드에서 자리를 찾지 못해 사전 그림을 보여 줍니다. ‘카드 보기’에서 자리를 잡을 수 있습니다")
            else:
                img.setToolTip("못 찾은 이유: " + (g.get("reason") or "-") + "\n‘카드 보기’에서 아이콘을 끌어 감싸면 잡을 수 있습니다")
                img.setBackground(QBrush(QColor(self.LOST_COLOR)))
            self.table.setItem(i, 0, img)
            name, meaning, ko = QLineEdit(g["suggest"]), QLineEdit(g["meaning"]), QLineEdit(g["ko"])
            name.setPlaceholderText("영문 소문자·숫자·_")
            name.setCompleter(completer)  # 사전 이름을 고를 수 있게
            not_icon = QCheckBox()
            scale = QDoubleSpinBox()
            scale.setRange(0.3, 4.0)
            scale.setSingleStep(0.05)
            scale.setValue(float(g.get("scale") or 1.1))
            view = QPushButton("카드 보기 · 자리 잡기")  # 찾은 아이콘도 자리를 다시 잡을 수 있다
            view.setToolTip("그 카드를 보고 아이콘 자리를 고칩니다: 다시 잡기·빠진 아이콘 넣기·합치기·나누기·아이콘 아님")
            if not pic:
                view.setStyleSheet("font-weight: bold")
            view.clicked.connect(lambda _c=False, gg=g["group"]: self.show_cards(gg))
            for j, w in ((1, name), (2, meaning), (3, ko), (4, not_icon), (5, scale), (self.C_VIEW, view)):
                self.table.setCellWidget(i, j, w)
            self.table.setItem(i, self.C_STATE, QTableWidgetItem(self._state_text(g)))
            where = f"{g.get('card_label') or g['where'][0]['page']} · {g.get('region', '')} {g.get('line', 0)}줄" \
                if g.get("where") else ""
            pages = QTableWidgetItem(where)
            pages.setToolTip("\n".join(w["label"] for w in g.get("where", [])))
            self.table.setItem(i, self.C_PAGES, pages)
            ex = QTableWidgetItem(g["examples"][0] if g["examples"] else "")
            ex.setToolTip("\n".join(g["examples"]))
            self.table.setItem(i, self.C_EX, ex)
            self.table.setRowHeight(i, 46)
            self.widgets.append((g["group"], name, meaning, ko, not_icon, scale))
        self.table.setVerticalHeaderLabels([str(i + 1) for i in range(len(groups))])
        row_of = {g["group"]: i for i, g in enumerate(groups)}
        for i, g in enumerate(groups):  # 적어 둔 값 되살리기
            _g, name, meaning, ko, not_icon, scale = self.widgets[i]
            k = keep.get(g["group"])
            if k:
                for edit, key in ((name, "name"), (meaning, "meaning"), (ko, "ko")):
                    if str(k.get(key, "")).strip():  # 빈 칸은 새로 받은 제안을 살린다
                        edit.setText(k[key])
                not_icon.setChecked(bool(k.get("not_icon")))
                scale.setValue(float(k.get("scale", scale.value())))
            if g.get("not_icon"):  # 카드 보기에서 ‘이 자리는 아이콘 아님’으로 정한 자리 (적어 둔 값보다 우선)
                not_icon.setChecked(True)
            host = g.get("absorbed_group")
            host_label = (f"{row_of[host] + 1}번" if host in row_of else "") if host else \
                (f"[[{g['absorbed_name']}]]" if g.get("absorbed_name") else "")
            if host_label and g.get("not_icon"):  # 옆 아이콘 상자에 포함된 자리: 원문에서 뺀다
                meaning.clear()
                meaning.setPlaceholderText("")
                cell = self.table.item(i, 0)
                cell.setText(f"{host_label}에 포함")
                cell.setIcon(QIcon())
                cell.setBackground(QBrush(QColor("#E8F0E0")))
                cell.setToolTip(f"{host_label} 아이콘의 그림이 이 자리까지 감싸 하나의 아이콘(세트)으로 합쳤습니다.\n"
                                "확정하면 이 자리의 [[?]]는 원문에서 빠집니다. 따로 있는 아이콘이면 ‘아이콘 아님’을 풀고 자리를 잡으세요.")
            for edit in (name, meaning, ko):
                edit.setProperty("last", edit.text())
            not_icon.toggled.connect(lambda _x, r=i: self._sync_row(r))
            name.textChanged.connect(lambda _t, r=i: self._sync_row(r))
            name.textChanged.connect(lambda _t: self._paint_conflicts())
            for col, edit in ((1, name), (2, meaning), (3, ko)):
                edit.editingFinished.connect(lambda r=i, c=col: self._propagate(r, c))
            ko.textChanged.connect(lambda _t, e=ko: self._paint_ko_edit(e))
            meaning.textChanged.connect(lambda _t: self._paint_conflicts())
            ko.textChanged.connect(lambda _t: self._paint_conflicts())
            self._sync_row(i)
        self._paint_conflicts()
        self.table.resizeColumnToContents(self.C_VIEW)
        for col, w in ((1, 150), (2, 170), (3, 150), (5, 100)):  # 입력 칸 글이 잘리지 않게
            self.table.setColumnWidth(col, w)
        self.table.resizeColumnToContents(self.C_STATE)
        self.table.setColumnWidth(self.C_STATE, min(max(self.table.columnWidth(self.C_STATE), 90), 190))
        self.table.resizeColumnToContents(self.C_PAGES)
        self.table.setColumnWidth(self.C_PAGES, min(max(self.table.columnWidth(self.C_PAGES), 72), 180))
        self._paint_card_bands(groups)
        hh = self.table.horizontalHeader()
        if self._sort:
            hh.setSortIndicatorShown(True)
            hh.setSortIndicator(self._sort[0], Qt.SortOrder.DescendingOrder if self._sort[1]
                                else Qt.SortOrder.AscendingOrder)
        else:
            hh.setSortIndicatorShown(False)
        n_bad_cards = len({c for c, bad in self._card_of.values() if bad})
        n_named = sum(1 for g in groups if g.get("named"))
        n_dict = sum(1 for g in groups if g.get("dict_how") == "same")
        n_lost = sum(1 for g in groups if not g.get("image") and not g.get("named") and not g.get("not_icon"))
        set_hint(self.steps_text,
                 f"찾은 아이콘 {len(groups)}개 (카드의 아이콘 하나 = 한 줄)"
                 + (f" · 확정됨 {n_named}" if n_named else "") + (f" · 사전과 모양 같음 {n_dict}" if n_dict else "")
                 + (f" · 손볼 카드 {n_bad_cards}장은 맨 위(붉은 띠)" if n_bad_cards else "")
                 + (f" · 위치 못 찾음 {n_lost}개" if n_lost else "")
                 + "\n"
                 "1. 그림을 보고 ‘이름’(영문 소문자)과 ‘한국어 이름’을 채우세요. 같은 이름을 적은 줄은 한 아이콘입니다.\n"
                 "2. 사전에 있는 이름을 적으면 사전의 한국어 이름·뜻·크기를 씁니다 (고치려면 ‘사전 전체’ 탭).\n"
                 "3. 아이콘이 아니라 글자면 ‘아이콘 아님’을 체크하고 ‘뜻’ 칸에 그 글자를 적으세요 (비우면 원문에서 뺌).\n"
                 "4. 자리가 틀렸거나 빠진 아이콘이 있으면 ‘카드 보기’에서 고치세요.", "info")
        self.known_note.setText("사전에 이미 있는 이름: " + (", ".join(known) or "(없음)"))
        for w in self.widgets:  # 한국어 이름이 빈 줄은 노랑
            self._paint_ko_edit(w[3])

    @staticmethod
    def _state_text(g: dict) -> str:
        if g.get("added"):
            base = "추가함"
        else:
            base = g.get("how_label") or ("위치 못 찾음" if not g.get("image") else "새 아이콘")
        if g.get("shape_count", 1) > 1:
            base += f" · 같은 모양 {g['shape_count']}줄"
        if g.get("set_size", 1) > 1:
            base += f" · 세트{g['set_size']}"
        return base

    def _capture(self) -> dict:
        """지금 표에 적힌 값 (줄별)."""
        return {g: {"name": name.text(), "meaning": meaning.text(), "ko": ko.text(),
                    "not_icon": not_icon.isChecked(), "scale": scale.value()}
                for g, name, meaning, ko, not_icon, scale in self.widgets}

    def _row_message(self, msg: str) -> str:
        """엔진의 ‘묶음: 문제’를 ‘문제: 3번 줄’로 (묶음 이름 ‘위치 못 찾음 o014’는 표에 보이지 않는다)."""
        for i, w in enumerate(self.widgets):
            if msg.startswith(w[0] + ": "):
                rest = msg[len(w[0]) + 2:].replace("처리 열에 ‘아이콘 아님’", "‘아이콘 아님’ 체크")
                return f"{rest}: {i + 1}번 줄"
        return msg

    def _row_label(self, group: str) -> str:
        """카드 보기에 보일 이름: ‘14번’, 이름을 적어 둔 줄이면 ‘14번 crown’."""
        i = next((i for i, w in enumerate(self.widgets) if w[0] == group), None)
        if i is None:
            return "아이콘"
        _g, name, _m, _k, not_icon, _sc = self.widgets[i]
        nm = name.text().strip()
        return f"{i + 1}번" + (f" {nm}" if nm and not not_icon.isChecked() else "")

    def rename_row(self, group: str, name: str) -> None:
        """카드 보기에서 고친 이름을 그 줄에 적는다."""
        i = next((i for i, w in enumerate(self.widgets) if w[0] == group), None)
        if i is None:
            return
        w = self.widgets[i]
        w[4].setChecked(False)
        w[1].setText(name)
        w[1].setProperty("last", name)

    @staticmethod
    def _pages_text(g: dict) -> str:
        """그 아이콘이 나온 카드 번호 (쪽). 파일이 여럿이면 ‘파일:쪽’."""
        where = g.get("where", [])
        files = {w.get("file_dir") for w in where}
        seen, out = set(), []
        for w in where:
            t = f"{Path(str(w.get('file_dir'))).name[:12]}:{w['page']}" if len(files) > 1 else str(w["page"])
            if t not in seen:
                seen.add(t)
                out.append(t)
        return ", ".join(out)

    # ---- 정렬 (머리글 누르기)
    def _sort_by(self, col: int):
        if col == self.C_VIEW:  # 버튼 열은 정렬하지 않는다
            return
        desc = bool(self._sort and self._sort[0] == col and not self._sort[1])
        self._sort = (col, desc)
        self._fill(self.review, self._capture())

    def _sorted(self, groups: list[dict], keep: dict) -> list[dict]:
        if not self._sort:
            self._card_of = {}
            return list(groups)
        col, desc = self._sort
        if col == self.C_PAGES:
            return self._sorted_by_card(groups, keep, desc)
        self._card_of = {}

        def key(g):
            k = keep.get(g["group"], {})
            txt = {1: k.get("name", g["suggest"]), 2: k.get("meaning", g["meaning"]), 3: k.get("ko", g["ko"])}
            if col == 0:
                return (0 if (g.get("image") or g.get("named")) else 1,)
            if col in txt:
                v = (txt[col] or "").strip().lower()
                return (v == "", v)  # 빈 칸은 뒤로
            if col == 4:
                return (bool(k.get("not_icon", g.get("not_icon"))),)
            if col == 5:
                return (k.get("scale", g.get("scale", 1.1)),)
            if col == self.C_STATE:
                return (self._state_text(g),)
            return ((g["examples"][0] if g["examples"] else ""),)

        out = sorted(groups, key=key, reverse=desc)
        if desc and col in (1, 2, 3):  # 빈 칸은 내림차순에서도 뒤로
            out = [g for g in out if key(g)[0] is not True] + [g for g in out if key(g)[0] is True]
        return out

    @staticmethod
    def _cards(g: dict) -> list[tuple[str, int]]:
        """그 아이콘이 나온 카드 (파일, 쪽) — 파일·쪽 순, 겹치지 않게."""
        return sorted({(str(w.get("file_dir") or ""), int(w["page"])) for w in g.get("where", [])})

    @staticmethod
    def _is_problem(g: dict, k: dict) -> bool:
        """확정 전에 손봐야 하는 줄: 위치 못 찾음·이름 빈 칸·한국어 이름 빈 칸 (‘아이콘 아님’이면 아님).
        이미 이름이 있는 아이콘(확정됨)은 자리를 못 찾아도 문제로 보지 않는다 (원문은 그대로 맞다)."""
        if k.get("not_icon", g.get("not_icon")):
            return False
        named = bool(str(k.get("name", g.get("suggest") or "")).strip())
        ko = str(k.get("ko", g.get("ko") or "")).strip()  # 화면에 적은 한국어 이름
        return not (g.get("image") or g.get("named")) or not named or not ko

    def _sorted_by_card(self, groups: list[dict], keep: dict, desc: bool) -> list[dict]:
        """카드 번호로 묶어 정렬: 손볼 줄이 있는 카드를 맨 위로, 그 안에서는 카드 순서, 한 카드 안에서는 원문 순서.
        내림차순은 카드 순서만 뒤집는다 (문제 카드는 그대로 위). 화면 띠 색을 위해 self._card_of에 줄의 카드를 남긴다."""
        first = {g["group"]: (self._cards(g) or [("", 0)])[0] for g in groups}
        bad = {g["group"]: self._is_problem(g, keep.get(g["group"], {})) for g in groups}
        bad_cards = {first[g] for g, b in bad.items() if b}
        order = sorted(set(first.values()), reverse=desc)
        rank = {c: i for i, c in enumerate(order)}
        pos = {g["group"]: i for i, g in enumerate(groups)}
        out = sorted(groups, key=lambda g: (first[g["group"]] not in bad_cards, rank[first[g["group"]]],
                                            g.get("first", pos[g["group"]])))
        self._card_of = {g["group"]: (first[g["group"]], first[g["group"]] in bad_cards) for g in out}
        return out

    def _paint_card_bands(self, groups: list[dict]) -> None:
        """카드 번호로 정렬했을 때: 같은 카드의 줄은 같은 띠 색 (문제 카드는 붉은 띠), 카드의 첫 줄은 카드 칸을 굵게."""
        if not self._card_of:
            return
        prev, band = None, 0
        for i, g in enumerate(groups):
            card, bad = self._card_of[g["group"]]
            first_row = card != prev
            if first_row:
                band ^= 1
                prev = card
            color = ("#FBE3E3" if band else "#F6D0D0") if bad else ("#EEF3FA" if band else "#FFFFFF")
            it = self.table.item(i, self.C_PAGES)
            f = it.font()
            f.setBold(first_row)
            it.setFont(f)
            if bad:
                it.setToolTip("이 카드에 확정 전에 손봐야 할 아이콘이 있습니다 (위치 못 찾음·이름 빈 칸·한국어 이름 빈 칸)\n"
                              + it.toolTip())
            for col in (self.C_STATE, self.C_PAGES, self.C_EX):
                cell = self.table.item(i, col)
                if cell is not None:
                    cell.setBackground(QBrush(QColor(color)))

    def select_group(self, group: str | None) -> None:
        """그 줄을 골라 보이게 한다 (카드 보기를 닫은 뒤)."""
        i = next((i for i, w in enumerate(self.widgets) if w[0] == group), None) if group else None
        if i is None:
            return
        self.table.setCurrentCell(i, 1)
        self.table.selectRow(i)
        self.table.scrollToItem(self.table.item(i, 0), QTableWidget.ScrollHint.EnsureVisible)
        self.widgets[i][1].setFocus()

    def _after_place(self, r: dict) -> bool:
        """카드 보기에서 바꾼 뒤 표를 다시 채운다. 돌려줌: 그 줄에 이미 이름이 적혀 있었는가 (제안으로 덮지 않음)."""
        keep = self._capture()
        g = r.get("group")
        inherited = bool(g in keep and str(keep[g].get("name", "")).strip())
        if g in keep and r.get("dict") and not inherited:  # 다시 잡은 그림이 사전과 같음: 그 이름을 채운다
            keep[g]["name"] = r["dict"]
        sg = r.get("suggested")
        if sg in keep and r.get("suggest"):  # 카드 보기에서 새로 받은 제안 (세트로 합쳐 그림이 바뀜 등): 예전 값 대신
            keep[sg].update(name="", meaning="", ko="")
        review = service.icon_review(self.project_dir)
        if review and g in keep and not str(keep[g].get("name", "")).strip():
            # 다시 잡은 그림이 다른 줄과 같은 모양이면 그 줄에 적어 둔 이름·뜻·한국어 이름을 이어받는다
            shape = next((x.get("shape") for x in review["groups"] if x["group"] == g), "")
            src = next((keep[x["group"]] for x in review["groups"] if shape and x.get("shape") == shape
                        and x["group"] != g and x["group"] in keep and str(keep[x["group"]].get("name", "")).strip()
                        and not keep[x["group"]].get("not_icon")), None)
            if src is not None:
                keep[g].update(name=src["name"], meaning=src["meaning"], ko=src["ko"])
                inherited = True
        if review:
            self._fill(review, keep)
        return inherited

    @staticmethod
    def _paint_ko_edit(edit: QLineEdit):
        edit.setStyleSheet("" if (edit.text().strip() or not edit.isEnabled()) else "background: #FFE066")

    def _known_entry(self, name: str) -> dict | None:
        """사전에 있는 이름이면 그 항목 (표 줄은 사전 값을 쓴다)."""
        return self._dict_rows.get(name) if name else None

    def _sync_row(self, r: int):
        if r >= len(getattr(self, "widgets", [])):
            return
        _g, name, meaning, ko, not_icon, scale = self.widgets[r]
        groups = getattr(self, "review", {}).get("groups") or []
        g = groups[r] if r < len(groups) else {}
        e = self._known_entry(name.text().strip()) if not not_icon.isChecked() else None
        if e is not None:  # 사전에 있는 이름: 사전 값 그대로 (고치려면 ‘사전 전체’ 탭)
            for edit, key in ((meaning, "meaning"), (ko, "ko")):
                edit.blockSignals(True)
                edit.setText(e.get(key, "") or "")
                edit.blockSignals(False)
            scale.setValue(float(e.get("scale") or 1.1))
            tip = "사전에 있는 이름이라 사전 값을 씁니다. 고치려면 ‘사전 전체’ 탭에서 고치세요"
        else:
            tip = ""
        for w in (ko, scale):
            w.setEnabled(e is None and not not_icon.isChecked())
            w.setToolTip(tip)
        name.setEnabled(not not_icon.isChecked())
        meaning.setEnabled(e is None)
        # 아이콘 아님: ‘뜻’ 칸에 그 자리 글자를 적는다 (적으면 [[?]]를 그 글자로, 비우면 뺀다)
        suggested = g.get("meaning") or ""
        if not_icon.isChecked() and suggested and meaning.text().strip() == suggested.strip():
            # 모델이 제안한 아이콘 뜻은 그 자리 글자가 아니다: 비워 둔다 (그대로 두면 원문에 뜻 문장이 글자로 들어감)
            meaning.setProperty("stashed", meaning.text())
            meaning.setText("")
        elif not not_icon.isChecked() and meaning.property("stashed") and not meaning.text().strip():
            meaning.setText(meaning.property("stashed"))  # 다시 아이콘으로 하면 제안 뜻을 되살린다
            meaning.setProperty("stashed", "")
        if not_icon.isChecked():
            meaning.setPlaceholderText("그 자리 글자 (예: 最). 비우면 원문에서 뺌")
            meaning.setToolTip("아이콘이 아니라 글자를 잘못 읽은 것이면 여기에 그 글자를 적으세요")
        else:
            meaning.setPlaceholderText("")
            meaning.setToolTip(tip)
        st = self.table.item(r, self.C_STATE)
        if st is not None and g:
            text = self._state_text(g)
            if g.get("named"):
                if not_icon.isChecked():
                    text = "확정됨 → 아이콘 아님"
                elif name.text().strip() != g["named"]:
                    text = f"확정됨 [[{g['named']}]] → 이름 바꿈"
            st.setText(text)
        self._paint_lost(r)
        self._paint_ko_edit(ko)

    def _propagate(self, r: int, col: int) -> None:
        """한 줄의 이름·뜻·한국어 이름을 고치면: 모양이 같고 예전 값이 같던 다른 줄도 같이 고친다 (켜 둔 경우)."""
        if r >= len(self.widgets):
            return
        edit = self.widgets[r][col]
        old, new = edit.property("last") or "", edit.text()
        edit.setProperty("last", new)
        if old == new or not self.same_shape.isChecked():
            return
        groups = self.review.get("groups") or []
        shape = groups[r].get("shape") if r < len(groups) else ""
        if not shape:
            return
        for j, w in enumerate(self.widgets):
            if j == r or j >= len(groups) or groups[j].get("shape") != shape or w[4].isChecked():
                continue
            other = w[col]
            if other.isEnabled() and other.text() == old:
                other.setText(new)
                other.setProperty("last", new)

    LOST_COLOR, LOST_DONE_COLOR = "#FFE066", "#E0E0E0"  # 위치 못 찾음: 손볼 것(노랑) / 아이콘 아님으로 정함(회색)

    def _paint_lost(self, r: int) -> None:
        """‘위치 못 찾음’ 칸 색: 아이콘 아님을 체크하면 더 손볼 것이 없으므로 회색 (옆 아이콘에 포함된 칸은 그대로)."""
        cell = self.table.item(r, 0)
        groups = getattr(self, "review", {}).get("groups") or []
        if cell is None or r >= len(groups) or cell.text() != "위치 못 찾음":
            return
        done = self.widgets[r][4].isChecked()
        cell.setBackground(QBrush(QColor(self.LOST_DONE_COLOR if done else self.LOST_COLOR)))
        cell.setForeground(QColor("#777") if done else QColor("black"))

    def _name_conflicts(self) -> dict[str, list[int]]:
        """같은 이름(사전에 없는 새 이름)인데 한국어 이름이나 뜻이 서로 다른 줄 {이름: [줄 번호]}.
        같은 이름은 한 아이콘이므로 확정하면 첫 줄의 값을 쓴다."""
        by: dict[str, list[int]] = {}
        for i, (_g, name, _m, _k, not_icon, _sc) in enumerate(self.widgets):
            nm = name.text().strip()
            if nm and not not_icon.isChecked() and self._known_entry(nm) is None:
                by.setdefault(nm, []).append(i)
        out = {}
        for nm, rows in by.items():
            for col in (2, 3):
                vals = {self.widgets[i][col].text().strip() for i in rows} - {""}
                if len(vals) > 1:
                    out[nm] = rows
                    break
        return out

    def _paint_conflicts(self):
        """같은 이름인데 한국어 이름·뜻이 다른 줄은 그 칸을 주황으로 (확정하면 첫 줄 값이 쓰인다)."""
        if len(getattr(self, "widgets", [])) != self.table.rowCount():
            return
        bad = {i: rows for rows in self._name_conflicts().values() for i in rows}
        for i, (_g, _name, meaning, ko, _ni, _sc) in enumerate(self.widgets):
            rows = bad.get(i)
            for edit in (meaning, ko):
                if rows:
                    others = ", ".join(f"{j + 1}번" for j in rows if j != i)
                    edit.setStyleSheet(f"background: {self.CONFLICT_COLOR}")
                    edit.setToolTip(f"{others}과 이름이 같은데 값이 다릅니다. 같은 이름은 한 아이콘이라 확정하면 첫 줄의 값을 씁니다.\n"
                                    "다른 아이콘이면 이름을 바꾸세요 (예: gold_2).")
                elif edit.styleSheet().startswith(f"background: {self.CONFLICT_COLOR}"):
                    edit.setStyleSheet("")
                    edit.setToolTip("")
            if not rows:
                self._paint_ko_edit(ko)

    def show_cards(self, group: str):
        """그 줄의 카드를 보여 주고, 그 영역의 아이콘을 고치게 한다."""
        g = next((x for x in self.review["groups"] if x["group"] == group), None)
        if g is None:
            return
        order = [w[0] for w in self.widgets]
        nxt = order[order.index(group) + 1] if group in order and order.index(group) + 1 < len(order) else None
        dlg = IconPlaceDialog(self.project_dir, g, self._row_label, self._after_place, self, on_rename=self.rename_row)
        dlg.exec()
        act, where = dlg.outcome
        if act == "not_icon":  # 다음 줄로 (아이콘 아님으로 뺀 줄은 끝났으므로)
            self.select_group(nxt if nxt in [w[0] for w in self.widgets] else where)
        elif act == "saved":  # 저장한 자리의 줄
            self.select_group(where)

    def rows(self) -> list[dict]:
        """각 줄의 최종 이름·값 (확정에 넘길 것). 사전에 있는 이름이면 keep_dict (사전 값을 덮어쓰지 않음)."""
        out = []
        for g, n, m, k, ni, sc in self.widgets:
            name = n.text().strip()
            if not ni.isChecked() and self._known_entry(name) is not None:
                out.append({"group": g, "name": name, "meaning": "", "ko": "", "not_icon": False, "text": "",
                            "scale": None, "keep_dict": True})
                continue
            out.append({"group": g, "name": "" if ni.isChecked() else name,
                        "meaning": "" if ni.isChecked() else m.text().strip(), "ko": k.text().strip(),
                        "not_icon": ni.isChecked(), "text": m.text().strip() if ni.isChecked() else "",
                        "scale": sc.value()})
        return out

    def accept(self):
        if not self._dict_unsaved_ok():  # 사전 탭에서 고친 것 먼저 (확정이 사전을 다시 쓰므로)
            return
        rows = self.rows()
        conflicts = self._name_conflicts()
        if conflicts:
            lines = [f"• {nm}: " + ", ".join(f"{i + 1}번" for i in rws) for nm, rws in conflicts.items()]
            r = ask_choice(self, "같은 이름, 다른 값", "같은 이름인데 한국어 이름이나 뜻이 다른 줄이 있습니다 (주황):\n"
                           + "\n".join(lines),
                           [("첫 줄 값으로 확정", "accept"), ("돌아가서 고치기", "reject")], default=1,
                           informative="같은 이름은 한 아이콘이라 첫 줄의 한국어 이름·뜻을 씁니다. 다른 아이콘이면 "
                                       "이름을 다르게 하세요 (예: gold, gold_2).")
            if r != 0:
                return
        r = service.confirm_icons(self.project_dir, rows)
        if r.state != "confirmed":
            show_problem(self, "아이콘 이름을 확정하지 못했습니다", "", "이름을 고친 뒤 다시 누르세요.",
                         items=[self._row_message(m) for m in r.blocking])
            return
        self.result = r
        super().accept()
