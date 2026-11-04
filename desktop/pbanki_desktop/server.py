"""HTTP routes, Server-Sent Events and static files, on 127.0.0.1 only.

Every route in plan section 11 calls one Service method and sends its dict as JSON. Failures are
{"error": "plain sentence"}. Requests with a foreign Host, or changes with a foreign Origin or a
body that isn't JSON, are refused before anything else runs.
"""

import errno
import html
import json
import logging
import re
import select
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from urllib.parse import parse_qs, unquote

from . import paths
from .service import ServiceError

_log = logging.getLogger(__name__)

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PORT_TRIES = 10
WEB_ROOT = paths.desktop_root() / "web"

MAX_BODY = 1024 * 1024
MAX_DRAIN = 16 * 1024 * 1024
DRAIN_SECONDS = 0.5
IDLE_SECONDS = 30
HEARTBEAT_SECONDS = 15
EVENTS_TICK_SECONDS = 0.5
EVENTS_RETRY_MS = 2000
REQUEST_WAIT_SECONDS = 60

_SOMETHING_WRONG = "Something went wrong. Your work is safe on this computer."
_BAD_REQUEST = "pbAnki couldn't read that request."
_NO_SUCH_ADDRESS = "pbAnki has nothing at that address."
_NOT_ALLOWED = "pbAnki can't do that at this address."
_CLOSING = "pbAnki is closing."

_TOPIC = re.compile(r"(reader|prompts|settings|book:[^,\s]+)\Z")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
}

_TYPE_WORDS = {str: "text", bool: "on or off", int: "a whole number", dict: "a set of options", list: "a list"}


class NoFreePort(Exception):
    """None of the ports tried could be used. Its text is a plain sentence."""


class _ClientGone(Exception):
    """The connection broke while reading or writing."""


class _Refused(ServiceError):
    def __init__(self, status, sentence, allow=None, close=False):
        super().__init__(status, sentence)
        self.allow = allow
        self.close = close


def _checked(body, fields):
    """The body's known fields with the right types. Null means left out. Others are refused."""
    given = {}
    for name, value in body.items():
        if name not in fields:
            raise _Refused(400, f'pbAnki doesn\'t know "{name[:40]}" here.')
        if value is None:
            continue
        kind = fields[name]
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise _Refused(400, f'"{name}" has to be {_TYPE_WORDS[kind]}.')
        given[name] = value
    return given


def _required(body, *names):
    for name in names:
        if name not in body:
            raise _Refused(400, f'"{name}" is missing.')


def _query_bool(query, name):
    value = query.get(name)
    if value is None:
        return None
    if value in ("true", "1"):
        return True
    if value in ("false", "0"):
        return False
    raise _Refused(400, f'"{name}" has to be true or false.')


def _topics(query):
    """The topics asked for in ?topics=a,b, or None for all of them."""
    value = query.get("topics")
    if value is None:
        return None
    topics = [topic.strip() for topic in value.split(",") if topic.strip()]
    if not topics or not all(_TOPIC.match(topic) for topic in topics):
        raise _Refused(400, "pbAnki doesn't know one of those topics.")
    return topics


_RUN_FIELDS = {"action": str, "options": dict}
_RUN_OPTIONS = {
    "count": int, "kind": str, "model": str, "prompt": str, "sees": str, "companion": str, "hint": str,
    "highlights": bool, "connections": bool, "meta_model": str, "ideas": bool,
}
_SUGGEST_FIELDS = {"count": int, "kind": str, "model": str, "prompt": str, "sees": str, "companion": str, "hint": str}
_FIND_FIELDS = {"chapters": list, "scope": str, "count": int, "model": str, "prompt": str, "hint": str}
_EDIT_FIELDS = {"front": str, "back": str}
_SAVE_FIELDS = {"mark_connection_sources": bool}
_PROMPT_FILE_FIELDS = {"name": str, "file": str}
_SETTINGS_FIELDS = {
    "mount": str, "auto_suggest": bool, "assistants": dict, "model": str, "meta_model": str, "companion_send": str,
    "sees": str, "kind": str, "count": int, "last_prompt": str, "suggest_connections": bool, "suggest_ideas": bool,
    "mark_connection_sources": bool,
}


def _call_run(s, r):
    options = r.body.get("options")
    return s.run(*r.args, r.body.get("action"), None if options is None else _checked(options, _RUN_OPTIONS))


def _call_choose_prompt(s, r):
    _required(r.body, "prompt")
    return s.choose_prompt(*r.args, r.body["prompt"])


def _call_prompt_file(method):
    def call(s, r):
        _required(r.body, "name", "file")
        return getattr(s, method)(r.body["name"], r.body["file"])

    return call


_EVENTS = "events"

# (method, address with * for an id, fields a body may have, what to call)
_ROUTES = (
    ("GET", "/api/state", {}, lambda s, r: s.state()),
    ("GET", "/api/events", {}, _EVENTS),
    ("GET", "/api/books", {}, lambda s, r: s.books()),
    ("GET", "/api/books/*", {}, lambda s, r: s.book(*r.args)),
    ("POST", "/api/books/*/open", {}, lambda s, r: s.open_book(*r.args)),
    ("POST", "/api/books/*/prompt", {"prompt": str}, _call_choose_prompt),
    ("POST", "/api/books/*/run", _RUN_FIELDS, _call_run),
    ("POST", "/api/books/*/highlights/*/open", {}, lambda s, r: s.open_highlight(*r.args)),
    ("POST", "/api/books/*/highlights/*/skip", {"skipped": bool},
     lambda s, r: s.skip_highlight(*r.args, r.body.get("skipped", True))),
    ("POST", "/api/books/*/highlights/*/cards", {"kind": str}, lambda s, r: s.add_card(*r.args, r.body.get("kind", "qa"))),
    ("POST", "/api/books/*/highlights/*/suggest", _SUGGEST_FIELDS, lambda s, r: s.suggest(*r.args, r.body)),
    ("POST", "/api/books/*/highlights/*/define", {"term": str, "model": str},
     lambda s, r: s.define(*r.args, r.body.get("term"), r.body.get("model"))),
    ("GET", "/api/books/*/highlights/*/passage", {}, lambda s, r: s.passage(*r.args, r.query.get("more", 0))),
    ("PATCH", "/api/cards/*", _EDIT_FIELDS, lambda s, r: s.update_card(*r.args, r.body.get("front"), r.body.get("back"))),
    ("DELETE", "/api/cards/*", {}, lambda s, r: s.remove_card(*r.args)),
    ("POST", "/api/cards/*/restore", {}, lambda s, r: s.restore_card(*r.args)),
    ("POST", "/api/cards/*/rewrite", {"instruction": str, "model": str},
     lambda s, r: s.rewrite_card(*r.args, r.body.get("instruction"), r.body.get("model"))),
    ("POST", "/api/books/*/connections/find", _FIND_FIELDS, lambda s, r: s.find_connections(*r.args, r.body)),
    ("POST", "/api/connections/*/open", {}, lambda s, r: s.open_connection(*r.args)),
    ("PATCH", "/api/connections/*", _EDIT_FIELDS,
     lambda s, r: s.update_connection(*r.args, r.body.get("front"), r.body.get("back"))),
    ("DELETE", "/api/connections/*", {}, lambda s, r: s.remove_connection(*r.args)),
    ("POST", "/api/ideas/*/open", {}, lambda s, r: s.open_idea(*r.args)),
    ("PATCH", "/api/ideas/*", _EDIT_FIELDS, lambda s, r: s.update_idea(*r.args, r.body.get("front"), r.body.get("back"))),
    ("DELETE", "/api/ideas/*", {}, lambda s, r: s.remove_idea(*r.args)),
    ("GET", "/api/books/*/save", {},
     lambda s, r: s.save_preview(*r.args, _query_bool(r.query, "mark_connection_sources"))),
    ("POST", "/api/books/*/save", _SAVE_FIELDS, lambda s, r: s.save_book(*r.args, r.body.get("mark_connection_sources"))),
    ("POST", "/api/books/*/save/retry", {}, lambda s, r: s.save_retry(*r.args)),
    ("POST", "/api/reader/eject", {}, lambda s, r: s.reader_eject()),
    ("POST", "/api/reader/plugin/update", {}, lambda s, r: s.reader_plugin_update()),
    ("POST", "/api/reader/choose-folder", {}, lambda s, r: s.reader_choose_folder()),
    ("PATCH", "/api/settings", _SETTINGS_FIELDS, lambda s, r: s.update_settings(r.body)),
    ("GET", "/api/prompts/*", {}, lambda s, r: s.prompt(*r.args)),
    ("POST", "/api/prompts/open", _PROMPT_FILE_FIELDS, _call_prompt_file("open_prompt")),
    ("POST", "/api/prompts/reveal", _PROMPT_FILE_FIELDS, _call_prompt_file("reveal_prompt")),
    ("POST", "/api/books/*/companion/create", {}, lambda s, r: s.companion_create(*r.args)),
    ("POST", "/api/books/*/companion/copy", {}, lambda s, r: s.companion_copy(*r.args)),
    ("POST", "/api/books/*/companion/keep", {}, lambda s, r: s.companion_keep(*r.args)),
    ("POST", "/api/books/*/companion/forget", {}, lambda s, r: s.companion_forget(*r.args)),
)


def _build_table():
    # Segment count to [(segments, method, fields, call)], literal addresses first
    table = {}
    for method, address, fields, call in _ROUTES:
        segments = address.split("/")[1:]
        table.setdefault(len(segments), []).append((segments, method, fields, call))
    for rows in table.values():
        rows.sort(key=lambda row: row[0].count("*"))
    return table


_TABLE = _build_table()


def _find_route(raw_path):
    """{method: (fields, call, decoded ids)} for an address. A literal address beats one with ids."""
    parts = raw_path.split("/")[1:]
    methods = {}
    for segments, method, fields, call in _TABLE.get(len(parts), ()):
        if method in methods or not all(s == "*" and p or s == p for s, p in zip(segments, parts)):
            continue
        try:
            ids = [unquote(p, errors="strict") for s, p in zip(segments, parts) if s == "*"]
        except UnicodeDecodeError:
            continue
        methods[method] = (fields, call, ids)
    return methods


class _Call:
    def __init__(self, args, body, query):
        self.args = args
        self.body = body
        self.query = query


def _plain_page(sentence):
    text = html.escape(sentence, quote=False)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>pbAnki</title></head>'
        f"<body><p>{text}</p></body></html>\n"
    ).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "pbAnki"
    sys_version = ""
    timeout = IDLE_SECONDS

    _unread = 0
    _head_sent = False

    def log_message(self, format, *args):  # noqa: A002
        pass

    # Sending

    def _send(self, status, body, content_type, allow=None):
        if self._unread:
            self._drain()
        if self.server.closing.is_set():
            self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            if allow:
                self.send_header("Allow", allow)
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            self._head_sent = True
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError as e:
            raise _ClientGone from e

    def _send_json(self, status, payload, allow=None):
        body = json.dumps(payload, allow_nan=False).encode("ascii")
        self._send(status, body, "application/json; charset=utf-8", allow)

    def _drain(self):
        """Reads a refused body, so the client can finish sending and read the answer."""
        left, self._unread = min(self._unread, MAX_DRAIN), 0
        self.close_connection = True
        try:
            self.connection.settimeout(DRAIN_SECONDS)
            while left > 0:
                chunk = self.rfile.read1(min(left, 65536))
                if not chunk:
                    return
                left -= len(chunk)
        except OSError:
            pass

    def send_error(self, code, message=None, explain=None):
        """Replaces http.server's HTML error page."""
        self.close_connection = True
        if self._head_sent:
            return
        try:
            self._send_json(code, {"error": _BAD_REQUEST})
        except _ClientGone:
            pass

    # Handling one request

    def _handle(self):
        self._unread = 0
        self._head_sent = False
        try:
            try:
                self._answer()
            except _Refused as e:
                if e.close:
                    self.close_connection = True
                self._send_json(e.status, e.payload(), e.allow)
            except ServiceError as e:
                self._send_json(e.status, e.payload())
            except _ClientGone:
                raise
            except Exception:
                _log.exception("A request failed: %s %s", self.command, self.path)
                if self._head_sent:
                    self.close_connection = True
                else:
                    self._send_json(500, {"error": _SOMETHING_WRONG})
        except _ClientGone:
            self.close_connection = True

    do_GET = do_HEAD = do_POST = do_PATCH = do_DELETE = do_PUT = do_OPTIONS = _handle

    def _answer(self):
        self._check_host()
        length = self._content_length()
        if self.command not in ("GET", "HEAD"):
            self._check_origin()
        if self.server.closing.is_set():
            raise _Refused(503, _CLOSING, close=True)
        raw, _, query = self.path.partition("?")
        if not raw.startswith("/"):
            raise _Refused(400, _BAD_REQUEST, close=True)
        if raw == "/api" or raw.startswith("/api/"):
            self._answer_api(raw, query, length)
        else:
            self._answer_static(raw, length)

    # Checks

    def _check_host(self):
        # A request in the old form without headers has no Host to check
        hosts = [] if self.request_version == "HTTP/0.9" else self.headers.get_all("Host") or []
        if len(hosts) != 1 or hosts[0].strip().lower() not in self.server.hosts:
            raise _Refused(403, "pbAnki only answers at its own address on this computer.", close=True)

    def _check_origin(self):
        origins = self.headers.get_all("Origin") or []
        if any(origin.strip().lower() not in self.server.origins for origin in origins):
            raise _Refused(403, "pbAnki only takes changes from its own page.", close=True)

    def _content_length(self):
        if self.headers.get_all("Transfer-Encoding"):
            self._unread = MAX_DRAIN
            raise _Refused(411, "pbAnki needs to know the size of a request first.", close=True)
        values = self.headers.get_all("Content-Length") or []
        if not values:
            return 0
        text = values[0].strip()
        if len(values) != 1 or not (text.isascii() and text.isdigit()) or len(text) > 18:
            raise _Refused(400, _BAD_REQUEST, close=True)
        self._unread = int(text)
        return self._unread

    def _check_content_type(self, length):
        value = self.headers.get("Content-Type")
        if value is None:
            if length:
                raise _Refused(415, "pbAnki only reads requests sent as JSON.")
            return
        kind, _, parameters = value.partition(";")
        charset = re.search(r"charset\s*=\s*\"?([^\s;\"]+)", parameters, re.I)
        if kind.strip().lower() != "application/json" or (charset and charset.group(1).lower() != "utf-8"):
            raise _Refused(415, "pbAnki only reads requests sent as JSON.")

    def _read_json(self, length):
        self._check_content_type(length)
        if length > MAX_BODY:
            raise _Refused(413, "That request is too large for pbAnki to read.")
        if not length:
            return {}
        try:
            data = self.rfile.read(length)
        except TimeoutError:
            self._unread = 0
            raise _Refused(408, "That request took too long to arrive.", close=True) from None
        except OSError as e:
            raise _ClientGone from e
        self._unread = 0
        if len(data) != length:
            raise _Refused(400, _BAD_REQUEST, close=True)
        try:
            body = json.loads(data.decode("utf-8"), parse_constant=_refuse_constant)
            json.dumps(body, ensure_ascii=False).encode("utf-8")
        except (ValueError, RecursionError):
            raise _Refused(400, "pbAnki couldn't read that as JSON.") from None
        if not isinstance(body, dict):
            raise _Refused(400, "pbAnki expected a JSON object.")
        return body

    # API

    def _answer_api(self, raw, query_text, length):
        methods = _find_route(raw)
        if not methods:
            raise _Refused(404, _NO_SUCH_ADDRESS)
        route = methods.get(self.command)
        if route is None:
            raise _Refused(405, _NOT_ALLOWED, allow=", ".join(sorted(methods)))
        fields, call, ids = route
        if self.command == "GET":
            if length:
                raise _Refused(400, _BAD_REQUEST, close=True)
            body = {}
        else:
            body = _checked(self._read_json(length), fields)
        try:
            pairs = parse_qs(query_text, keep_blank_values=True, max_num_fields=20, errors="strict")
        except ValueError:
            raise _Refused(400, _BAD_REQUEST) from None
        query = {name: values[-1] for name, values in pairs.items()}
        if call is _EVENTS:
            self._answer_events(query)
            return
        self._send_json(200, call(self.server.service, _Call(ids, body, query)))

    # Server-Sent Events

    def _answer_events(self, query):
        topics = _topics(query)
        self.close_connection = True
        with self.server.service.subscribe(topics) as listener:
            try:
                self._stream(listener)
            except OSError:
                pass

    def _stream(self, listener):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self._head_sent = True
        self.wfile.write(f"retry: {EVENTS_RETRY_MS}\n\n".encode("ascii"))
        poller = select.poll()
        poller.register(self.connection, select.POLLIN)
        heartbeat = self.server.heartbeat
        next_beat = time.monotonic() + heartbeat
        while not self.server.closing.is_set():
            wait = min(EVENTS_TICK_SECONDS, max(next_beat - time.monotonic(), 0))
            event = listener.get(timeout=wait)
            if event is not None:
                self._write_event(event)
            if time.monotonic() >= next_beat:
                self.wfile.write(b": heartbeat\n\n")
                next_beat = time.monotonic() + heartbeat
            if poller.poll(0) and self._client_left():
                return

    def _write_event(self, event):
        try:
            text = json.dumps(event, allow_nan=False)
        except (TypeError, ValueError):
            _log.exception("An event could not be sent")
            return
        self.wfile.write(f"data: {text}\n\n".encode("ascii"))

    def _client_left(self):
        # Only called when the socket is readable, so this doesn't wait
        try:
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except TimeoutError:
            return False
        except OSError:
            return True

    # Static files

    def _answer_static(self, raw, length):
        if self.command not in ("GET", "HEAD"):
            raise _Refused(405, _NOT_ALLOWED, allow="GET, HEAD")
        if length:
            raise _Refused(400, _BAD_REQUEST, close=True)
        root = self.server.web_root
        if not root.is_dir():
            self._send_missing_page(f"pbAnki's screens aren't here yet. The folder {root} doesn't exist.")
            return
        if raw == "/" and not (root / "index.html").is_file():
            self._send_missing_page(f"pbAnki's screens aren't here yet. {root / 'index.html'} doesn't exist.")
            return
        file = self._static_file(root, "index.html" if raw == "/" else raw[1:])
        try:
            data = file.read_bytes()
        except OSError:
            raise _Refused(404, _NO_SUCH_ADDRESS) from None
        self._send(200, data, CONTENT_TYPES.get(file.suffix.lower(), "application/octet-stream"))

    def _send_missing_page(self, sentence):
        self._send(503, _plain_page(sentence), "text/html; charset=utf-8")

    @staticmethod
    def _static_file(root, encoded):
        """The file inside `root`. Anything else, however it is spelled, is not found."""
        not_found = _Refused(404, _NO_SUCH_ADDRESS)
        try:
            name = unquote(encoded, errors="strict")
        except UnicodeDecodeError:
            raise not_found from None
        parts = name.split("/")
        if any(not part or part.startswith(".") or "\\" in part or "\0" in part for part in parts):
            raise not_found
        try:
            home = root.resolve(strict=True)
            file = (home / name).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            raise not_found from None
        if not file.is_relative_to(home) or not file.is_file():
            raise not_found
        return file


def _refuse_constant(name):
    raise ValueError(name)


class Server(ThreadingHTTPServer):
    """The HTTP server and the Service behind it, started and stopped together."""

    daemon_threads = True
    request_queue_size = 64

    def __init__(self, service, port=DEFAULT_PORT, web_root=None, heartbeat=HEARTBEAT_SECONDS):
        self.service = service
        self.web_root = Path(web_root) if web_root is not None else WEB_ROOT
        self.heartbeat = heartbeat
        self.closing = threading.Event()
        self._open = {}
        self._open_lock = threading.Lock()
        self._serving = None
        self._stopped = False
        super().__init__((HOST, port), Handler)
        self.port = self.server_address[1]
        self.url = f"http://{HOST}:{self.port}"
        self.hosts = {f"{HOST}:{self.port}", f"localhost:{self.port}"}
        self.origins = {f"http://{host}" for host in self.hosts}

    def server_bind(self):
        # http.server's version looks up this machine's name, which can take seconds
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def start(self):
        """Starts the Service's watcher and answering requests."""
        self.service.start()
        self._serving = threading.Thread(target=self.serve_forever, args=(0.1,), name="pbanki-server", daemon=True)
        self._serving.start()

    def stop(self, wait=REQUEST_WAIT_SECONDS, waiting=None):
        """Ends event streams and idle connections, lets requests under way finish for up to
        `wait` seconds, then stops the runs and the Service. `waiting()` is called if that takes
        more than a second."""
        if self._stopped:
            return
        self._stopped = True
        self.closing.set()
        if self._serving is not None:
            self.shutdown()
            self._serving.join(wait)
        self.server_close()
        self._finish_requests(wait, waiting)
        self.service.stop()

    def open_connections(self):
        with self._open_lock:
            return len(self._open)

    def _finish_requests(self, wait, waiting):
        started = time.monotonic()
        told = False
        while time.monotonic() - started < wait:
            with self._open_lock:
                connections = list(self._open.values())
            if not connections:
                return
            for connection in connections:
                try:
                    connection.shutdown(socket.SHUT_RD)
                except OSError:
                    pass
            if not told and waiting is not None and time.monotonic() - started > 1:
                told = True
                waiting()
            time.sleep(0.02)
        _log.warning("A request was still running when pbAnki closed")

    def process_request_thread(self, request, client_address):
        thread = threading.current_thread()
        with self._open_lock:
            self._open[thread] = request
        try:
            super().process_request_thread(request, client_address)
        finally:
            try:
                self.service.release_thread()
            finally:
                with self._open_lock:
                    self._open.pop(thread, None)

    def handle_error(self, request, client_address):
        kind = sys.exc_info()[0]
        if kind is None or not issubclass(kind, (ConnectionError, TimeoutError)):
            _log.exception("A connection failed")


def bind(service, port=DEFAULT_PORT, tries=PORT_TRIES, **kwargs):
    """A Server on `port`, or the next free one of `tries` ports. Port 0 means any free port."""
    last = port if port == 0 else min(port + tries - 1, 65535)
    for candidate in range(port, last + 1):
        try:
            return Server(service, candidate, **kwargs)
        except OSError as e:
            if e.errno != errno.EADDRINUSE:
                raise
    raise NoFreePort(f"Ports {port} to {last} are all taken. Pick another with --port.")
