"""Shaper GUI entrypoint."""

from abc import ABCMeta, abstractmethod
from math import floor
from typing import Tuple, TypeVar, Any
from shutil import which
import json
import subprocess
import sys
import threading
from PySide6.QtGui import (
   QFontDatabase, QResizeEvent,
   QMouseEvent, QTextLayout, QPixmap, QIcon
)
from PySide6.QtCore import (
    QObject, QCoreApplication,
    Qt, QRect, QPointF, QTimer, QEvent
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QPlainTextEdit, QWidget,
    QStatusBar, QLabel
)


type JSON = dict[str, Any]


class Request(metaclass=ABCMeta):
    """A JSON-RPC request.

    It is able to be encoded into a JSON blob. This class is meant to
    be subclassed so that `method_name` and `method_params` might be
    implemented.
    """

    def encode(self, id: int) -> str:
        """Encode the request as a JSON string.

        Takes the ID to associate with the request.
        """
        method = self.method_name()
        params = self.method_params()

        obj: JSON = {
            "jsonrpc": "2.0",
            "method": method,
            "id": str(id)
        }

        if params is not None:
            obj["params"] = params

        return json.dumps(obj)

    @abstractmethod
    def method_name(self) -> str:
        """Get the method name of the request."""
        raise NotImplementedError()

    def method_params(self) -> JSON | None:
        """Get the method params as a JSON object.

        By default, it returns None, representing a request which has
        no parameters.
        """
        return None

    T = TypeVar('T', bound='Request')

    @classmethod
    def from_named_tuple(
            cls: type[T],
            methodname: str,
            field_names: list[type[Any]]
    ) -> type[Any]:
        """Construct a new Request from a named tuple."""

        def __init__(self: Any, **kwargs: dict[str, Any]) -> None:
            self.keys = kwargs.keys()
            for key, value in kwargs.items():
                self.key = value

        def method_params(self: Any) -> JSON | None:
            if len(self.keys) == 0:
                return None

            obj: JSON = {}
            for key in self.keys:
                value = self.__dict__[key]
                obj[key] = value

            return obj

        attrs: dict[str, Any] = {
            'method_params': method_params,
            'method_name': lambda self: methodname,
            '__init__': __init__
        }

        ty = type(methodname, (Request,), attrs)
        return ty


class Response():
    """A JSON-RPC response.

    It is able to be decoded into a dictionary.
    """

    _id: int
    _data: JSON

    def __init__(self, data: JSON):
        """Construct a Response object from a decoded JSON dictionary."""
        self._data = data
        self._id = int(self._data["id"])

    T = TypeVar('T', bound='Response')

    @classmethod
    def from_json(cls: type[T], blob: str) -> T:
        """Construct a Response object from a JSON string."""
        data = json.loads(blob)
        return cls(data)

    def is_error(self) -> bool:
        """Check if this response represents an error."""
        return "error" in self._data

    def error(self) -> JSON | None:
        """Get the error component of the response.

        Returns None if the response is not an error.
        """
        return self._data.get("error", None)

    def result(self) -> JSON | None:
        """Get the result component of the response.

        Returns None if the response is an error.
        """
        return self._data.get("result", None)

    def id(self) -> int:
        """Get the id of the response."""
        return self._id


class RequestResponseEvent(QEvent):
    """An event representing the server's response to a client request."""

    req: Request
    res: Response

    def __init__(self, req: Request, res: Response):
        """Construct a new RequestResponseEvent."""
        super().__init__(QEvent.Type(QEvent.Type.User + 1))
        self.req = req
        self.res = res


PingRequest = Request.from_named_tuple("ping", [])
ShutdownRequest = Request.from_named_tuple("shutdown", [])


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

        reqText = req.encode(id)

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

    def pop_request(self, res: Response) -> tuple[QObject, Request] | None:
        """Find the request for a given response.

        Find the request with the same ID as the given response,
        removing it from the pool of pending responses. The function
        returns both the original sender of the request along with the
        request itself. If no pending request with the given ID can
        be found, return None.
        """
        req: tuple[QObject, Request] | None
        with self._lk:
            req = self._pending_requests.pop(res.id(), None)

        return req


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
        if isinstance(e, RequestResponseEvent):
            if isinstance(e.req, PingRequest):
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

        # Forward all other events to our superclass.
        return super().event(e)


class MainWindow(QMainWindow):
    """The main window of the application."""

    _editor: PlainTextEditWithOverlay
    _q: ServerMessageQueue

    def __init__(self, q: ServerMessageQueue) -> None:
        """Initialize the main window."""
        super().__init__()
        self._q = q
        self._configure_editor()
        self._editor.set_overlay_position(1, 10)

        self.setCentralWidget(self._editor)

        status_bar = QStatusBar()
        self.setStatusBar(status_bar)

        heartbeat = HeartbeatWidget(q)
        status_bar.addPermanentWidget(heartbeat)

    def _configure_editor(self) -> None:
        self._editor = PlainTextEditWithOverlay()
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        self._editor.setFont(font)
        self._editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._editor.document().setDocumentMargin(0)


def main() -> None:
    """Start the application."""
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

            res = Response.from_json(line)
            req_info = q.pop_request(res)

            if not req_info:
                print(f"Could not find matching request for response: {res}")
            else:
                # Dispatch the event to the main window.
                sender, req = req_info
                ev = RequestResponseEvent(req, res)
                QCoreApplication.postEvent(sender, ev)

    exe_path = which("code-brushes-server")
    if not exe_path:
        print("Cannot locate code-brushes-server")
        exit(1)

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
