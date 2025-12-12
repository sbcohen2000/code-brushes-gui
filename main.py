"""Shaper GUI entrypoint."""

from abc import ABCMeta, abstractmethod
from math import floor
from shutil import which
from typing import (
    Tuple, TypeVar, Any, TypedDict,
    get_type_hints, cast
)
from dataclasses import dataclass, asdict
import argparse
import json
import subprocess
import sys
import threading
from PySide6.QtGui import (
   QFontDatabase, QResizeEvent,
   QMouseEvent, QTextLayout, QIcon,
   QTextCursor, QSyntaxHighlighter,
   QTextCharFormat, QTextDocument
)
from PySide6.QtCore import (
    QObject, QCoreApplication,
    Qt, QRect, QPointF, QTimer, QEvent, QSignalBlocker
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QPlainTextEdit, QWidget,
    QStatusBar, QLabel
)


type JSON = dict[str, Any]


A = TypeVar('A')


def validate_typed_dict(value: object, typ: A) -> None:
    """
    Check that the given value confirms to the given type.

    This function is due to https://stackoverflow.com/a/76242123.
    """
    if not isinstance(value, dict):
        raise TypeError(f'Value must be a dict not a {type(value).__name__}')
    d = get_type_hints(typ)
    diff = d.keys() ^ value.keys()
    if diff:
        raise TypeError(f"Invalid dict fields: {' '.join(diff)}")
    for k, v in get_type_hints(typ).items():
        if not isinstance(value[k], v):
            raise TypeError(
                f"Invalid type: '{k}' should be {v.__name__} "
                f"but is {type(value[k]).__name__}"
            )


@dataclass
class Request(metaclass=ABCMeta):
    """An object which can be encoded into a JSON-RPC request."""

    @property
    @abstractmethod
    def method_name(self) -> str:
        """Get the name of the method."""
        raise NotImplementedError()


def encode(req: Request, id: int) -> str:
    """Encode a request as a JSON string."""
    method = req.method_name

    obj: JSON = {
        "jsonrpc": "2.0",
        "method": method,
        "id": str(id)
    }

    params = asdict(req)
    if len(params.keys()) > 0:
        obj["params"] = params

    return json.dumps(obj)


class Response(TypedDict):
    """An object which can be decoded from a JSON-RPC response."""

    pass


@dataclass
class ErrorResponse:
    """An error response."""

    code: int
    message: str
    data: JSON | None


def decode(obj: JSON, typ: type[A]) -> A | ErrorResponse:
    """Decode a message into a JSON-RPC response."""
    if "result" in obj:
        validate_typed_dict(obj["result"], typ)
        return cast(A, obj["result"])
    else:
        assert "error" in obj
        error = obj["error"]
        return ErrorResponse(code=int(error["code"]),
                             message=error["message"],
                             data=error["data"])


class RequestResponseEvent(QEvent):
    """An event representing the server's response to a client request."""

    req: Request
    res: JSON

    def __init__(self, req: Request, res: JSON):
        """Construct a new RequestResponseEvent."""
        super().__init__(QEvent.Type(QEvent.Type.User + 1))
        self.req = req
        self.res = res


class PingRequest(Request):
    """Ping the server to check if it's alive."""

    @property
    def method_name(self) -> str:
        """Return the method name."""
        return "ping"


class PingResponse(Response):
    """See PingRequest."""

    pass


class ShutdownRequest(Request):
    """Request that the server shut down."""

    @property
    def method_name(self) -> str:
        """Return the method name."""
        return "shutdown"


Pos = TypedDict("Pos", {"row": int, "col": int})
SrcRange = TypedDict("SrcRange", {"begin": Pos, "end": Pos})


@dataclass
class FormatRequest(Request):
    """Request that the server format the source code."""

    src: str

    @property
    def method_name(self) -> str:
        """Return the method name."""
        return "format"


class FormatResponse(Response):
    """See FormatRequest."""

    src: str


class ServerMessageQueue():
    """Stores pending responses from the language server."""

    _lk: threading.Lock
    _next_request_id: int
    _pending_requests: dict[int, tuple[QObject, Request]]
    _proc: subprocess.Popen[str] | None

    def __init__(self) -> None:
        """Create a new message queue using a process handle to the server."""
        self._lk = threading.Lock()
        self._next_request_id = 0
        self._pending_requests = {}
        self._proc = None

    def send(self, sender: QObject, req: Request) -> None:
        """Send a request to the server process.

        We record the sender of the request so that when the response
        is recieved, it can be routed to the sender.
        """
        id: int
        with self._lk:
            id = self._next_request_id
            self._next_request_id += 1

        reqText = encode(req, id)

        with self._lk:
            if self._proc is None or self._proc.stdin is None:
                print("Tried to send message to ServerMessageQueue \
                  without attached process")
                return

            self._pending_requests[id] = (sender, req)
            self._proc.stdin.write(reqText + "\n")
            self._proc.stdin.flush()

    def attach_process(self, proc: subprocess.Popen[str]) -> None:
        """Register a new langauge server process with the queue."""
        with self._lk:
            self._proc = proc

    def pop_request(self, res: JSON) -> tuple[QObject, Request] | None:
        """Find the request for a given response.

        Find the request with the same ID as the given response,
        removing it from the pool of pending responses. The function
        returns both the original sender of the request along with the
        request itself. If no pending request with the given ID can
        be found, return None.
        """
        req: tuple[QObject, Request] | None
        with self._lk:
            req = self._pending_requests.pop(int(res["id"]), None)

        return req


class ErrorHighlighter(QSyntaxHighlighter):
    """A Syntax Highlighter which can highlight source ranges."""

    _rng: SrcRange | None
    _format: QTextCharFormat

    def __init__(self, parent: QTextDocument, format: QTextCharFormat):
        """Initialize the ErrorHighlighter with the desired char format."""
        super().__init__(parent)
        self._rng = None
        self._format = format

    def set_src_range(self, rng: SrcRange) -> None:
        """Set the source range of the highlighted error."""
        self._rng = rng
        self.rehighlight()

    def unset_src_range(self) -> None:
        """Unset the error source range.

        After this method has been called, until set_src_range is
        called, the highlighter will not apply any style to the
        document.
        """
        self._rng = None
        self.rehighlight()

    def highlightBlock(self, _text: str) -> None:
        """Highlight a single block in the document."""
        # If we don't have a source range, there's nothing to
        # highlight.
        if not self._rng:
            return

        block = self.currentBlock()
        row_no = block.blockNumber()
        n_cols = block.length()

        begin_row = self._rng["begin"]["row"]
        begin_col = self._rng["begin"]["col"]
        end_row = self._rng["end"]["row"]
        end_col = self._rng["end"]["col"]

        if row_no == begin_row:
            self.setFormat(begin_col, n_cols - begin_col, self._format)
        elif row_no == end_row:
            self.setFormat(0, end_col + 1, self._format)
        elif begin_row < row_no and row_no < end_row:
            self.setFormat(0, n_cols, self._format)


class PlainTextEditWithOverlay(QPlainTextEdit):
    """A QPlainTextEdit with a cursor overlay.

    This class is the same as a QPlainTextEdit, but with additional
    facilities for placing a cursor overlay above an arbitrary
    character position.
    """

    _cursor_overlay_position: Pos | None = None
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

    def rect_for_editor_position(self, pos: Pos) -> QRect:
        """Find the (hypothetical) position of the character at (row, col)."""
        # The vertical and horizontal scroll amounts are in
        # lines/pixels respectively.
        vert_scroll_amt = self.verticalScrollBar().value()
        horz_scroll_amt = self.horizontalScrollBar().value()
        char_width, char_height = self._find_character_dimensions()

        return QRect(
            round(pos["col"] * char_width - horz_scroll_amt),
            round(pos["row"] * char_height - vert_scroll_amt * char_height),
            round(char_width),
            round(char_height)
        )

    def editor_position_under_point(self, point: QPointF) -> Pos:
        """Find the row and column of a point.

        Given a point in the editor's local coordinate space, return a
        tuple of (row, col) describing the location of the character
        under the point.
        """
        vert_scroll_amt = self.verticalScrollBar().value()
        horz_scroll_amt = self.horizontalScrollBar().value()

        char_width, char_height = self._find_character_dimensions()
        return {"row": floor(point.y() / char_height) + vert_scroll_amt,
                "col": floor((point.x() + horz_scroll_amt) / char_width)}

    def cursor_position(self) -> Pos:
        """Find the current position of the cursor."""
        cursor = self.textCursor()
        row = cursor.blockNumber()
        col = cursor.positionInBlock()
        return {"row": row, "col": col}

    def set_cursor_position(self, pos: Pos) -> None:
        """Set the position of the cursor.

        If the position doesn't correspond to a valid document
        location, this method will set the cursor to the closest valid
        position.
        """
        cursor = self.cursor_for_src_position(pos)
        self.setTextCursor(cursor)

    def cursor_for_src_position(self, pos: Pos) -> QTextCursor:
        """Produce a QTextCursor at the given source position.

        If the position doesn't correspond to a valid document
        location, this method will set the cursor to the closest valid
        position.

        When `cursor` is provided, move it instead of creating a new
        cursor.
        """
        # Reset the cursor to the beginning of the document.
        cursor = QTextCursor(self.document().findBlockByNumber(pos["row"]))
        cursor.setPosition(0)

        # Move the cursor down by the number of rows.
        cursor.movePosition(QTextCursor.MoveOperation.Down, n=pos["row"])

        # Find the number of columns on the desired line.
        n_cols = cursor.block().length()
        n_moves = min(pos["col"], n_cols - 1)

        # Move the cursor right by the number of columns
        cursor.movePosition(QTextCursor.MoveOperation.Right, n=n_moves)
        return cursor

    def cursor_for_src_range(self, rng: SrcRange) -> QTextCursor:
        """Produce a QTextCursor which selects the given source range."""
        cursor = self.cursor_for_src_position(rng["begin"])

        if rng["begin"]["row"] == rng["end"]["row"]:
            # If the range has only a single row, we need only move
            # the cursor right by the correct number of columns.

            n_moves = rng["end"]["col"] - rng["begin"]["col"]
            cursor.movePosition(QTextCursor.MoveOperation.Right,
                                QTextCursor.MoveMode.KeepAnchor,
                                n=n_moves)
        else:
            # Otherwise, we need to reset the cursor to the beginning
            # of the line, then move down by the proper number of
            # rows, then move the cursor right by the correct number
            # of columns.

            cursor.movePosition(QTextCursor.MoveOperation.StartOfLine,
                                QTextCursor.MoveMode.KeepAnchor)

            n_down_moves = rng["end"]["row"] - rng["begin"]["row"]
            cursor.movePosition(QTextCursor.MoveOperation.Down,
                                QTextCursor.MoveMode.KeepAnchor,
                                n=n_down_moves)

            n_right_moves = rng["end"]["col"]
            cursor.movePosition(QTextCursor.MoveOperation.Right,
                                QTextCursor.MoveMode.KeepAnchor,
                                n=n_right_moves)

        return cursor

    def _update_cursor_overlay_geometry(self) -> None:
        if self._cursor_overlay_position is None:
            return

        r = self.rect_for_editor_position(self._cursor_overlay_position)
        self._cursor_overlay_widget.setGeometry(r)

    def set_overlay_position(self, pos: Pos) -> None:
        """Set the position of the overlay."""
        self._cursor_overlay_position = pos
        self._update_cursor_overlay_geometry()

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        """Handle mouse move events.

        Forward the event to the QPlainTextEdit, and also update the
        overlay position so that it rests under the mouse cursor.
        """
        super().mouseMoveEvent(e)

        point = e.position()
        pos = self.editor_position_under_point(point)
        self.set_overlay_position(pos)


class HeartbeatWidget(QLabel):
    """A widget which shows the connection status to the server.

    The connection status is determined by sending a ping request
    every few seconds. If the ping takes too long to respond, the
    heartbeat widget shows an icon indicating that the server may be
    stuck.
    """

    _q: ServerMessageQueue

    # How many periods have we gone without a pong?
    _missed_beats: int
    _got_last_beat: bool

    _icon_normal: QIcon
    _icon_beating: QIcon
    _icon_disabled: QIcon

    def __init__(
            self, q: ServerMessageQueue,
            parent: QWidget | None = None
    ) -> None:
        """Initialize the HeartbeatWidget."""
        super().__init__(parent)

        self._q = q

        self._missed_beats = 0
        self._got_last_beat = True

        # Load icons
        self._icon_normal = QIcon()
        self._icon_beating = QIcon()
        self._icon_disabled = QIcon()

        self._icon_normal.addFile("./icons/heart_normal.png")
        self._icon_beating.addFile("./icons/heart_beating.png")
        self._icon_disabled.addFile("./icons/heart_disabled.png")

        self.setPixmap(self._icon_normal.pixmap(16, 16))

        # Setup timer
        heartbeat_timer = QTimer(self, interval=5000)
        heartbeat_timer.timeout.connect(self._on_heartbeat)
        heartbeat_timer.start()

    def _on_heartbeat(self) -> None:
        if not self._got_last_beat:
            self._missed_beats += 1
            self.setPixmap(self._icon_disabled.pixmap(16, 16))

        self._q.send(self, PingRequest())
        self._got_last_beat = False

    def event(self, e: QEvent) -> bool:
        """Handle custom events."""
        if isinstance(e, RequestResponseEvent) \
           and isinstance(e.req, PingRequest):
            # We got a pong response. Set the icon to beating and
            # reset `self._missed_beats`.
            self._missed_beats = 0
            self._got_last_beat = True

            self.setPixmap(self._icon_beating.pixmap(16, 16))

            # Switch the icon back to normal after a short
            # duration.
            anim_timer = QTimer(self, interval=200, singleShot=True)
            anim_timer.timeout.connect(
                lambda: self.setPixmap(self._icon_normal.pixmap(16, 16))
            )
            anim_timer.start()
            return True

        # Forward all other events to our superclass.
        return super().event(e)


class MainWindow(QMainWindow):
    """The main window of the application."""

    _editor: PlainTextEditWithOverlay
    _highlighter: ErrorHighlighter
    _q: ServerMessageQueue

    # A timer which, when it runs out, indicates that the user has not
    # made a change to the document in a while.
    _change_timer: QTimer

    def __init__(self, q: ServerMessageQueue) -> None:
        """Initialize the main window."""
        super().__init__()
        self._q = q
        self._configure_editor()

        self.setCentralWidget(self._editor)

        status_bar = QStatusBar()
        self.setStatusBar(status_bar)

        heartbeat = HeartbeatWidget(q)
        status_bar.addPermanentWidget(heartbeat)

        # Configure the change timer.
        self._change_timer = QTimer(self, interval=1000, singleShot=True)
        self._change_timer.timeout.connect(self._request_formatting_actions)

    def _reset_change_timer(self) -> None:
        self._change_timer.start()

    def _configure_editor(self) -> None:
        self._editor = PlainTextEditWithOverlay()
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPointSize(16)
        self._editor.setFont(font)
        self._editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._editor.document().setDocumentMargin(0)

        # Setup highlighter
        error_format = QTextCharFormat()
        error_format.setUnderlineStyle(
            QTextCharFormat.UnderlineStyle.WaveUnderline
        )
        error_format.setUnderlineColor("red")
        self._highlighter = ErrorHighlighter(
            self._editor.document(),
            error_format
        )

        # Reset the change timer when the document content changes
        self._editor.document().contentsChanged \
            .connect(self._reset_change_timer)

        # Reset the change timer when the cursor position changes
        self._editor.cursorPositionChanged \
            .connect(self._reset_change_timer)

    def _get_editor_contents(self) -> str:
        """Get the current contents of the editor as a string."""
        return self._editor.document().toPlainText()

    def _request_formatting_actions(self) -> None:
        """Request that the document be re-formatted."""
        src = self._get_editor_contents()
        self._q.send(self, FormatRequest(src=src))

    def _handle_format_response(
            self,
            result: FormatResponse | ErrorResponse
    ) -> bool:
        if isinstance(result, ErrorResponse):
            if result.code == -1:
                # Then, we have a parse error, and can be sure that
                # the error data is a source range.
                rng = cast(SrcRange, result.data)

                # Set error highlights
                self._highlighter.set_src_range(rng)

            return True

        # Remove error highlights
        self._highlighter.unset_src_range()

        new_src = result["src"]

        # Don't trigger contentsChanged or cursorPositionChanged
        # events when we apply the formatting. (This will cause an
        # infinite loop).
        with QSignalBlocker(self._editor.document()), \
             QSignalBlocker(self._editor):
            pos = self._editor.cursor_position()
            self._editor.setPlainText(new_src)
            self._editor.set_cursor_position(pos)

        return True

    def event(self, e: QEvent) -> bool:
        """Handle custom events."""
        if isinstance(e, RequestResponseEvent) \
           and isinstance(e.req, FormatRequest):
            result = decode(e.res, FormatResponse)
            return self._handle_format_response(result)

        # Forward all other events to our superclass.
        return super().event(e)


def main() -> None:
    """Start the application."""
    # Handle command-line arguments

    parser = argparse.ArgumentParser(
        prog='Code brushes GUI',
        description='Edit programs using the Code Brushes Language Server')

    parser.add_argument('--server-bin',
                        help="Specify an alternate language server program.",
                        metavar="PATH")
    args = parser.parse_args()

    # Setup the message queue.

    q = ServerMessageQueue()

    # Setup QTApp, providing the main window with the server message
    # queue so that it can make requests.

    app = QApplication(sys.argv)
    main = MainWindow(q)
    main.resize(720, 480)
    main.show()

    # Setup Langauge Server

    def reader_thread_main(proc: subprocess.Popen[str]) -> None:
        """Read responses from the language server and enqueue them."""
        while True:
            if not proc.stdout:
                print("Lost connection to server. Stopping.")
                break

            line = proc.stdout.readline()

            # This case only occurs when the server has shut down
            # (i.e. the above readline unblocks due to reaching the
            # end of file).
            if not line:
                break

            res = json.loads(line)
            req_info = q.pop_request(res)

            if not req_info:
                print(f"Could not find matching request for response: {res}")
            else:
                # Dispatch the event to the main window.
                sender, req = req_info
                ev = RequestResponseEvent(req, res)
                QCoreApplication.postEvent(sender, ev)

    # Startup the langauge server by opening a subprocess. If the user
    # provided an alternate language server binary, use
    # that. Otherwise, look for a program called `code-brushes-server`
    # on the path.
    exe_path: str
    if args.server_bin is None:
        mexe_path = which("code-brushes-server")
        if mexe_path is None:
            print("Cannot locate code-brushes-server")
            exit(1)
        exe_path = mexe_path
    else:
        exe_path = args.server_bin

    proc = subprocess.Popen(
        [exe_path],
        text=True,
        stdout=subprocess.PIPE,
        stdin=subprocess.PIPE,
        stderr=sys.stdout
    )
    t = threading.Thread(
        target=reader_thread_main,
        args=(proc,)
    )
    t.start()

    q.attach_process(proc)

    app.exec()
    # Signal that we want to stop the reader thread.
    q.send(app, ShutdownRequest())
    t.join()


if __name__ == "__main__":
    main()
