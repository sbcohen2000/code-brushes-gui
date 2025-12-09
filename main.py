"""Shaper GUI entrypoint."""

from typing import Tuple
from math import floor
import sys
from PySide6.QtGui import (
   QFontDatabase, QResizeEvent,
   QMouseEvent, QTextLayout
)
from PySide6.QtCore import (
    Qt, QRect, QPointF
)
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

        # Set mouse tracking so that we recieve mouse events even when
        # a mouse button is not pressed.
        self.setMouseTracking(True)

        self.viewport().setCursor(Qt.CursorShape.BlankCursor)

        self._cursor_overlay_widget = QWidget(self)
        self._cursor_overlay_widget.setGeometry(QRect(0, 0, 100, 100))
        self._cursor_overlay_widget.setStyleSheet(
            "QWidget { border: 1px solid black; }"
        )
        # Ensure that the cursor overlay widget cannot steal mouse
        # events from the editor.
        self._cursor_overlay_widget.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self.verticalScrollBar().valueChanged.connect(
            lambda: self._update_cursor_overlay_geometry())
        self.horizontalScrollBar().valueChanged.connect(
            lambda: self._update_cursor_overlay_geometry())

    def resizeEvent(self, e: QResizeEvent, /) -> None:
        """Handle resize events.

        Forward the event to the QPlainTextEdit, and also recompute
        the position of the overlay, if necessary.
        """
        super().resizeEvent(e)
        self._update_cursor_overlay_geometry()

    def _find_character_dimensions(self) -> Tuple[float, float]:
        """Find the size of a character in the editor."""
        # We construct a new text layout using the same font as the
        # editor (containing a single character), then measure the
        # line height and width.
        font = self.font()

        layout = QTextLayout("x", font)
        layout.beginLayout()
        line = layout.createLine()
        layout.endLayout()

        line.setPosition(QPointF(0, 0))
        line_height = line.height()
        width = line.naturalTextWidth()

        return width, line_height

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

    def editor_position_under_point(self, point: QPointF) -> Tuple[int, int]:
        """Find the row and column of a point.

        Given a point in the editor's local coordinate space, return a
        tuple of (row, col) describing the location of the character
        under the point.
        """
        vert_scroll_amt = self.verticalScrollBar().value()
        horz_scroll_amt = self.horizontalScrollBar().value()

        char_width, char_height = self._find_character_dimensions()
        return (
            floor(point.y() / char_height) + vert_scroll_amt,
            floor((point.x() + horz_scroll_amt) / char_width)
        )

    def _update_cursor_overlay_geometry(self) -> None:
        if self._cursor_overlay_position is None:
            return

        r = self.rect_for_editor_position(*self._cursor_overlay_position)
        self._cursor_overlay_widget.setGeometry(r)

    def set_overlay_position(self, row: int, col: int) -> None:
        """Set the position of the overlay."""
        self._cursor_overlay_position = (row, col)
        self._update_cursor_overlay_geometry()

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        """Handle mouse move events.

        Forward the event to the QPlainTextEdit, and also update the
        overlay position so that it rests under the mouse cursor.
        """
        super().mouseMoveEvent(e)

        pos = e.position()
        row, col = self.editor_position_under_point(pos)
        self.set_overlay_position(row, col)


class MainWindow(QMainWindow):
    """The main window of the application."""

    _editor: PlainTextEditWithOverlay

    def __init__(self) -> None:
        """Initialize the main window."""
        super().__init__()
        self._configure_editor()
        self._editor.set_overlay_position(1, 10)
        self.setCentralWidget(self._editor)

    def _configure_editor(self) -> None:
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
