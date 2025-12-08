"""Shaper GUI entrypoint."""

from typing import Tuple
import sys
from PySide6.QtGui import QFontDatabase, QFontMetricsF
from PySide6.QtCore import QRect
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QPlainTextEdit, QWidget
)


class PlainTextEditWithOverlay(QPlainTextEdit):
    """A QPlainTextEdit with a cursor overlay.

    This class is the same as a QPlainTextEdit, but with additional
    facilities for placing a cursor overlay above an arbitrary
    character position.
    """

    _editor: QPlainTextEdit
    _cursor_overlay_position: Tuple[int, int] | None = None
    _cursor_overlay_widget: QWidget

    def __init__(self, parent: QWidget | None = None) -> None:
        """Initialize the widget with an optional parent."""
        super().__init__(parent)

        self._cursor_overlay_widget = QWidget(self)
        self._cursor_overlay_widget.setGeometry(QRect(0, 0, 100, 100))
        self._cursor_overlay_widget.setStyleSheet(
            "QWidget { border: 1px solid black; }"
        )

        self.verticalScrollBar().valueChanged.connect(
            lambda: self._update_cursor_overlay_geometry())
        self.horizontalScrollBar().valueChanged.connect(
            lambda: self._update_cursor_overlay_geometry())

    def resizeEvent(self, e, /):
        """Handle resize events.

        Forward the event to the QPlainTextEdit, and also recompute
        the position of the overlay, if necessary.
        """
        super().resizeEvent(e)
        self._update_cursor_overlay_geometry()

    def _find_character_dimensions(self) -> Tuple[float, float]:
        """Find the size of a character in the editor."""
        font = self.font()
        font_metrics = QFontMetricsF(font)
        width = font_metrics.averageCharWidth()
        height = font_metrics.height()
        return width, height

    def rect_for_editor_position(self, row: int, col: int) -> QRect:
        """Find the (hypothetical) position of the character at (row, col)."""
        # The vertical and horizontal scroll amounts are in
        # lines/pixels respectively.
        vert_scroll_amt = self.verticalScrollBar().value()
        horz_scroll_amt = self.horizontalScrollBar().value()
        char_width, char_height = self._find_character_dimensions()

        return QRect(
            round(col * char_width - horz_scroll_amt),
            round(row * char_height - vert_scroll_amt * char_height),
            round(char_width),
            round(char_height)
        )

    def _update_cursor_overlay_geometry(self):
        if self._cursor_overlay_position is None:
            return

        r = self.rect_for_editor_position(*self._cursor_overlay_position)
        self._cursor_overlay_widget.setGeometry(r)

    def set_overlay_position(self, row: int, col: int) -> None:
        """Set the position of the overlay."""
        self._cursor_overlay_position = (row, col)
        self._update_cursor_overlay_geometry()


class MainWindow(QMainWindow):
    """The main window of the application."""

    _editor: PlainTextEditWithOverlay

    def __init__(self):
        """Initialize the main window."""
        super().__init__()
        self._configure_editor()
        self._editor.set_overlay_position(1, 10)
        self.setCentralWidget(self._editor)

    def _configure_editor(self):
        self._editor = PlainTextEditWithOverlay()
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        self._editor.setFont(font)
        self._editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._editor.document().setDocumentMargin(0)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    main = MainWindow()
    main.show()
    app.exec()
