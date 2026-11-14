"""창 번호와 버튼 코드 (기능을 물을 때 창·버튼을 짧게 가리키려고).

- 창 번호: 숫자 1개 + 알파벳 1개. 숫자는 작업 단계(① 글자 추출 = 1 … ⑧ 한글 넣기 = 8, 결과 보기 = 9),
  0은 단계에 속하지 않는 창(홈·프로젝트 화면·설정 등). 창 제목 앞에 ``[2B] 카드 보기``처럼 붙는다.
- 버튼 코드: ``창 번호-순번`` (예: ``2B-03``). 순번은 그 창(또는 홈·프로젝트 화면) 안 버튼의 만든 순서.
- Ctrl+D: 디버깅 모드. 버튼 글자 대신 버튼 코드를 보이고, 버튼을 누르면 코드만 복사하고 동작은 하지 않는다.
  다시 Ctrl+D를 누르면 원래대로.

각 창은 고치지 않는다: 앱 전체 이벤트 필터 하나가 창이 뜰 때 제목을 붙이고 버튼을 바꾼다.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QAbstractButton, QApplication, QMessageBox, QToolTip, QWidget

# 클래스 이름 -> (번호, 이름). 문서 표(DESIGN.md ‘창 번호’)와 같게 유지한다.
WINDOW_CODES: dict[str, tuple[str, str]] = {
    "HomePage": ("0A", "홈"),
    "ProjectPage": ("0B", "프로젝트 화면"),
    "NewProjectDialog": ("0C", "새 프로젝트"),
    "AddFilesDialog": ("0D", "파일 넣기"),
    "CardBoxesDialog": ("0E", "카드 자리 직접 그리기"),
    "RegionEditor": ("0F", "영역 지정"),
    "ExcludePagesDialog": ("0G", "뺄 쪽"),
    "SettingsDialog": ("0H", "설정"),
    "LoginDialog": ("0I", "로그인"),
    "ReleaseNotesDialog": ("0J", "릴리스 노트"),
    "LicenseDialog": ("0K", "라이선스"),
    "QMessageBox": ("0Z", "알림·질문"),
    "ExtractDialog": ("1A", "글자 추출"),
    "IconReviewDialog": ("2A", "아이콘 정의"),
    "IconPlaceDialog": ("2B", "카드 보기 · 아이콘 자리 잡기"),
    "IconDictionaryDialog": ("2C", "아이콘 사전"),
    "SourceReviewDialog": ("3A", "원문 확인"),
    "GlossaryReviewDialog": ("5A", "용어집 확정"),
    "TermChangesDialog": ("5B", "바뀐 용어 반영"),
    "TranslationReviewDialog": ("6A", "번역 검수"),
    "FontPlanDialog": ("8A", "한글 글꼴 고르기"),
    "AddFontDialog": ("8B", "글꼴 추가"),
    "ResultViewer": ("9A", "결과 보기"),
    "GroupPickDialog": ("9B", "그룹 고르기"),
    "TestHistoryDialog": ("9C", "시험 기록"),
    "TestCompareDialog": ("9D", "두 시험 비교"),
}
PAGE_CLASSES = ("HomePage", "ProjectPage")  # 메인 창 안의 화면: 메인 창 제목이 지금 화면의 번호를 따른다
ORIG = "_ui_code_orig_text"
ORIG_MIN = "_ui_code_orig_min"  # 디버깅 모드 전 버튼 최소 크기 (끌 때 되돌린다)


def code_of(w: QWidget | None) -> str | None:
    """위젯 클래스(부모 클래스 포함)의 창 번호."""
    if w is None:
        return None
    for cls in type(w).__mro__:
        if cls.__name__ in WINDOW_CODES:
            return WINDOW_CODES[cls.__name__][0]
    return None


def owner_of(w: QWidget) -> QWidget | None:
    """버튼이 속한 번호 있는 화면: 가장 가까운 번호 있는 조상 (메인 창 안이면 홈·프로젝트 화면)."""
    p = w.parentWidget()
    while p is not None:
        if code_of(p):
            return p
        if p.isWindow():  # 번호 없는 창(입력 상자·진행 상자): 바깥 화면 번호를 쓰지 않는다
            return None
        p = p.parentWidget()
    return None


def button_code(b: QAbstractButton) -> str:
    own = owner_of(b)
    if own is None:
        return "??-00"
    buttons = [x for x in own.findChildren(QAbstractButton) if owner_of(x) is own]
    n = buttons.index(b) + 1 if b in buttons else 0
    return f"{code_of(own)}-{n:02d}"


def titled(code: str, title: str) -> str:
    return title if title.startswith(f"[{code}]") else f"[{code}] {title}"


class UiCodes(QObject):
    """앱 전체 이벤트 필터: 창 제목에 번호, Ctrl+D 디버깅 모드."""

    def __init__(self, app: QApplication):
        super().__init__(app)
        self.debug = False
        self._busy = False
        app.installEventFilter(self)

    # ---- 창 번호
    def _title(self, w: QWidget):
        if self._busy:
            return
        if type(w).__name__ in PAGE_CLASSES or any(c.__name__ in PAGE_CLASSES for c in type(w).__mro__):
            win, code = w.window(), code_of(w)
            if win is w or not w.isVisible():
                return
        elif w.isWindow():
            win, code = w, code_of(w)
            if code is None and type(w).__name__ == "MainWindow":
                page = self._current_page(w)
                code = code_of(page) if page else None
        else:
            return
        if not code:
            return
        t = win.windowTitle()
        if isinstance(win, QMessageBox) and not t:
            t = WINDOW_CODES["QMessageBox"][1]
        base = t.split("] ", 1)[1] if t.startswith("[") and "] " in t[:6] else t
        new = f"[{code}] {base}"
        if new != t:
            self._busy = True
            try:
                win.setWindowTitle(new)
            finally:
                self._busy = False

    @staticmethod
    def _current_page(win: QWidget) -> QWidget | None:
        for cls in PAGE_CLASSES:
            for p in win.findChildren(QWidget):
                if type(p).__name__ == cls and p.isVisible():
                    return p
        return None

    # ---- 디버깅 모드
    def toggle(self):
        self.debug = not self.debug
        for top in QApplication.topLevelWidgets():
            for b in top.findChildren(QAbstractButton):
                self._label(b) if self.debug else self._unlabel(b)
        QToolTip.showText(QCursor.pos(), "디버깅 모드: 버튼을 누르면 코드를 복사합니다 (Ctrl+D로 끄기)"
                          if self.debug else "디버깅 모드 끔")

    def _label(self, b: QAbstractButton):
        if b.property(ORIG) is not None:
            return
        code = button_code(b)
        b.setProperty(ORIG, b.text())
        b.setProperty("_ui_code", code)
        # 글자가 짧은 코드로 바뀌어도 버튼 크기는 그대로 (배치가 바뀌어 버튼을 찾기 어렵지 않게)
        b.setProperty(ORIG_MIN, [b.minimumWidth(), b.minimumHeight()])
        hint = b.sizeHint()
        b.setMinimumSize(max(b.minimumWidth(), b.width() if b.isVisible() else 0, hint.width()),
                         max(b.minimumHeight(), hint.height()))
        b.setText(code)

    def _unlabel(self, b: QAbstractButton):
        orig = b.property(ORIG)
        if orig is None:
            return
        if b.text() == b.property("_ui_code"):  # 그 사이 화면이 글자를 바꿨으면 그것을 둔다
            b.setText(orig)
        old = b.property(ORIG_MIN)
        if old:
            b.setMinimumSize(int(old[0]), int(old[1]))
        b.setProperty(ORIG_MIN, None)
        b.setProperty(ORIG, None)
        b.setProperty("_ui_code", None)

    def relabel(self, b: QAbstractButton):
        """디버깅 중 화면이 버튼 글자를 바꿨으면: 바뀐 글자를 원래 글자로 기억하고 다시 코드를 보인다."""
        if self.debug and b.property(ORIG) is not None and b.text() != b.property("_ui_code"):
            b.setProperty(ORIG, b.text())
            b.setText(b.property("_ui_code"))

    def _copy(self, b: QAbstractButton):
        code = b.property("_ui_code") or button_code(b)
        QGuiApplication.clipboard().setText(code)
        QToolTip.showText(QCursor.pos(), f"복사함: {code}")

    def eventFilter(self, obj, ev):  # noqa: N802 - Qt 이름
        t = ev.type()
        if t == QEvent.Type.KeyPress and ev.key() == Qt.Key.Key_D and ev.modifiers() == Qt.KeyboardModifier.ControlModifier:
            if not ev.isAutoRepeat():
                self.toggle()
            return True
        if self.debug and t == QEvent.Type.Shortcut:
            return True  # 단축키(QShortcut·QAction·Alt+글자)로 동작이 실행되지 않게 (단축키 객체는 위젯이 아니다)
        if not isinstance(obj, QWidget):
            return False
        if t in (QEvent.Type.Show, QEvent.Type.WindowTitleChange):
            if t == QEvent.Type.Show or obj.isVisible():  # 아직 안 뜬 창은 뜰 때 붙인다 (만들며 제목을 읽는 코드에 영향 없게)
                self._title(obj)
            if t == QEvent.Type.Show and self.debug:
                for b in ([obj] if isinstance(obj, QAbstractButton) else []) + obj.findChildren(QAbstractButton):
                    self._label(b)
            return False
        if self.debug and isinstance(obj, QAbstractButton):
            if t in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
                if ev.button() == Qt.MouseButton.LeftButton:
                    self._copy(obj)
                return True
            if t == QEvent.Type.MouseButtonRelease:
                return True
        if self.debug and t in (QEvent.Type.KeyPress, QEvent.Type.ShortcutOverride) and ev.key() in (
                Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            fw = QApplication.focusWidget()
            if isinstance(fw, QAbstractButton) or ev.key() != Qt.Key.Key_Space:
                return True  # 디버깅 중에는 Enter·Space로도 버튼이 눌리지 않게
        return False


def install(app: QApplication) -> UiCodes:
    ui = getattr(app, "_ui_codes", None)
    if ui is None:
        import atexit

        ui = UiCodes(app)
        app._ui_codes = ui

        def _remove():  # 앱이 끝날 때 파이썬 필터를 먼저 뗀다 (끝나는 중에 불리면 Qt가 죽을 수 있다)
            try:
                app.removeEventFilter(ui)
            except RuntimeError:  # 이미 지워진 Qt 객체
                pass

        app.aboutToQuit.connect(_remove)
        atexit.register(_remove)
    return ui
