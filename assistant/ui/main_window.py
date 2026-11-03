"""The floating search window: a search bar, a result list and a one-line status."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QStyle, QStyledItemDelegate, QVBoxLayout,
                               QWidget)

from assistant.core.plugin_api import SearchResult
from assistant.ui import theme

ROLE_RESULT = Qt.ItemDataRole.UserRole


class ResultDelegate(QStyledItemDelegate):
    """Draws a result as a bold title over a muted subtitle."""

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), theme.ROW_HEIGHT)

    def paint(self, painter: QPainter, option, index) -> None:
        result: SearchResult = index.data(ROLE_RESULT)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(theme.SPACING // 2, 2, -theme.SPACING // 2, -2)
        if option.state & QStyle.StateFlag.State_Selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.SELECTION))
            painter.drawRoundedRect(rect, theme.RADIUS_ROW, theme.RADIUS_ROW)

        text_rect = rect.adjusted(theme.PADDING, 6, -theme.PADDING, -6)
        title_font = QFont(theme.FONT_FAMILY, 0)
        title_font.setPointSizeF(theme.FONT_SIZE_TITLE)
        title_font.setBold(True)
        sub_font = QFont(theme.FONT_FAMILY, 0)
        sub_font.setPointSizeF(theme.FONT_SIZE_SUBTITLE)

        title_fm, sub_fm = QFontMetrics(title_font), QFontMetrics(sub_font)
        painter.setFont(title_font)
        painter.setPen(QColor(theme.TEXT))
        painter.drawText(text_rect.left(), text_rect.top() + title_fm.ascent(),
                         title_fm.elidedText(result.title, Qt.TextElideMode.ElideRight,
                                             text_rect.width()))
        if result.subtitle:
            painter.setFont(sub_font)
            painter.setPen(QColor(theme.TEXT_MUTED))
            y = text_rect.top() + title_fm.height() + sub_fm.ascent()
            painter.drawText(text_rect.left(), y,
                             sub_fm.elidedText(result.subtitle, Qt.TextElideMode.ElideRight,
                                               text_rect.width()))
        painter.restore()


class SearchWindow(QWidget):
    query_changed = Signal(str)
    result_activated = Signal(object)   # SearchResult

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(theme.WINDOW_WIDTH)
        self.setStyleSheet(theme.stylesheet())

        self._generation = -1
        self._results: list[SearchResult] = []
        self.max_results = 8
        self._idle_hint = "Type to search"

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        panel = QFrame(objectName="panel")
        outer.addWidget(panel)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(theme.PADDING, theme.PADDING, theme.PADDING, theme.SPACING)
        layout.setSpacing(theme.SPACING)

        self.input = QLineEdit(objectName="searchInput")
        self.input.setPlaceholderText("Search…")
        self.input.setFixedHeight(theme.INPUT_HEIGHT)
        self.input.textChanged.connect(self.query_changed)
        self.input.installEventFilter(self)
        layout.addWidget(self.input)

        self.list = QListWidget(objectName="resultList")
        self.list.setItemDelegate(ResultDelegate(self.list))
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.itemClicked.connect(lambda item: self._activate(item.data(ROLE_RESULT)))
        self.list.hide()
        layout.addWidget(self.list)

        self.status = QLabel(self._idle_hint, objectName="statusLabel")
        layout.addWidget(self.status)

    # ------------------------------------------------------------ show / hide
    def toggle(self) -> None:
        if self.isVisible() and self.isActiveWindow():
            self.hide()
        else:
            self.summon()

    def summon(self) -> None:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        self.adjustSize()
        self.move(area.center().x() - self.width() // 2, area.top() + int(area.height() * 0.2))
        self.show()
        self.raise_()
        self.activateWindow()
        self.input.setFocus()
        self.input.selectAll()

    def event(self, e: QEvent) -> bool:
        if e.type() == QEvent.Type.WindowDeactivate and self.isVisible():
            self.hide()
        return super().event(e)

    # ----------------------------------------------------------------- state
    def set_idle_hint(self, text: str) -> None:
        self._idle_hint = text
        if not self.input.text().strip():
            self.status.setText(text)

    def begin_search(self, generation: int) -> None:
        self._generation = generation
        self._results.clear()
        self._rebuild()
        self.status.setText("Searching…")

    def add_results(self, generation: int, plugin_id: str, results: list) -> None:
        if generation != self._generation:
            return
        self._results.extend(results)
        self._results.sort(key=lambda r: r.score, reverse=True)
        del self._results[self.max_results:]
        self._rebuild()

    def finish_search(self, generation: int) -> None:
        if generation != self._generation:
            return
        self.status.setText(f"{len(self._results)} result(s)" if self._results else "No results")

    def clear_results(self) -> None:
        self._generation = -1
        self._results.clear()
        self._rebuild()
        self.status.setText(self._idle_hint)

    def _rebuild(self) -> None:
        self.list.clear()
        for result in self._results:
            item = QListWidgetItem()
            item.setData(ROLE_RESULT, result)
            self.list.addItem(item)
        if self._results:
            self.list.setCurrentRow(0)
            rows = min(len(self._results), theme.MAX_LIST_ROWS)
            self.list.setFixedHeight(rows * theme.ROW_HEIGHT + 4)
            self.list.show()
        else:
            self.list.hide()
        self.adjustSize()

    # ------------------------------------------------------------------ keys
    def eventFilter(self, obj, e: QEvent) -> bool:  # noqa: N802
        if obj is self.input and e.type() == QEvent.Type.KeyPress:
            key = e.key()
            if key == Qt.Key.Key_Escape:
                self.hide()
                return True
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up) and self._results:
                step = 1 if key == Qt.Key.Key_Down else -1
                row = (self.list.currentRow() + step) % len(self._results)
                self.list.setCurrentRow(row)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                item = self.list.currentItem()
                if item is not None:
                    self._activate(item.data(ROLE_RESULT))
                return True
        return super().eventFilter(obj, e)

    def _activate(self, result: SearchResult) -> None:
        self.result_activated.emit(result)
        if not result.keep_open:
            self.hide()
