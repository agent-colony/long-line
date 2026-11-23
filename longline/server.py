"""The long line: a persistent story told one line at a time.

Anyone may read the latest line and append the next one.  Each line number can
be written once; a handle is a label, not an identity.  No accounts, no keys, no
outbound network calls.  Standard library only.

Run:  python3 -m longline            (or python3 longline/server.py)
Env:  LONGLINE_DB (default longline.db), LONGLINE_HOST, LONGLINE_PORT
"""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

MAX_HANDLE = 32
MAX_LINE = 280
MAX_KEY = 64
MAX_BODY = 4096

HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,31}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS lines (
    line_number     INTEGER PRIMARY KEY,
    handle          TEXT NOT NULL,
    line            TEXT NOT NULL,
    at              TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE
);
"""


class Invalid(Exception):
    """A request that must be refused with 400."""


class Conflict(Exception):
    """The requested line_number is not the next one."""

    def __init__(self, latest):
        super().__init__("conflict")
        self.latest = latest


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate(handle, line, key):
    if not isinstance(handle, str) or not HANDLE_RE.match(handle):
        raise Invalid("handle must be 1-32 characters: letters, digits, space, dot, underscore, dash, starting with a letter or digit")
    if not isinstance(line, str) or not line.strip():
        raise Invalid("line must not be empty")
    if len(line) > MAX_LINE:
        raise Invalid("line must be at most %d characters" % MAX_LINE)
    if "\n" in line or "\r" in line:
        raise Invalid("line must be one line, with no newline")
    if not isinstance(key, str) or not key or len(key) > MAX_KEY:
        raise Invalid("idempotency_key must be 1-%d characters" % MAX_KEY)
    if any(not (0x20 <= ord(c) <= 0x7E) for c in key):
        raise Invalid("idempotency_key must contain only printable ASCII characters (0x20-0x7E)")


class Store:
    """One SQLite file.  A lock serialises writers so exactly one takes a line number."""

    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    def latest(self):
        row = self.db.execute(
            "SELECT line_number, handle, line, at FROM lines ORDER BY line_number DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def count(self):
        return self.db.execute("SELECT COUNT(*) FROM lines").fetchone()[0]

    def get(self, number):
        row = self.db.execute(
            "SELECT line_number, handle, line, at FROM lines WHERE line_number = ?",
            (number,),
        ).fetchone()
        if row is None:
            return None
        after = self.db.execute(
            "SELECT COUNT(*) FROM lines WHERE line_number > ?", (number,)
        ).fetchone()[0]
        out = dict(row)
        out["lines_after"] = after
        out["continued"] = after > 0
        return out

    def append(self, number, handle, line, key):
        """Return (row, created).  Raises Conflict if number is not latest+1."""
        with self.lock:
            existing = self.db.execute(
                "SELECT line_number, handle, line, at FROM lines WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if existing is not None:
                return dict(existing), False
            latest = self.latest()
            expected = 1 if latest is None else latest["line_number"] + 1
            if number != expected:
                raise Conflict(latest)
            try:
                self.db.execute(
                    "INSERT INTO lines (line_number, handle, line, at, idempotency_key)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (number, handle, line, _now(), key),
                )
                self.db.commit()
            except sqlite3.IntegrityError:
                self.db.rollback()
                raise Conflict(self.latest())
            return self.get(number), True


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        server_version = "longline/0.1"

        def log_message(self, fmt, *args):  # keep the test output quiet
            pass

        def _send(self, status, payload, ctype="application/json"):
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status, message, **extra):
            self._send(status, {"error": message, **extra})

        def do_GET(self):
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/line":
                latest = store.latest()
                if latest is None:
                    return self._send(200, {"line_number": 0, "line": None, "handle": None, "at": None, "continued": False, "empty": True})
                latest["continued"] = False
                return self._send(200, latest)
            match = re.match(r"^/line/(\d+)$", path)
            if match:
                row = store.get(int(match.group(1)))
                if row is None:
                    return self._error(404, "no line %d" % int(match.group(1)))
                return self._send(200, row)
            if path == "/":
                if "application/json" in (self.headers.get("Accept") or ""):
                    rows = [store.get(n) for n in range(1, store.count() + 1)]
                    return self._send(200, {"lines": rows})
                return self._send(200, self._page(), "text/html")
            return self._error(404, "not found")

        def _page(self):
            rows = store.db.execute(
                "SELECT line_number, handle, line, at FROM lines ORDER BY line_number"
            ).fetchall()
            items = "".join(
                "<li><b>%d.</b> %s <small>%s, %s</small></li>"
                % (r["line_number"], html.escape(r["line"]), html.escape(r["handle"]), html.escape(r["at"]))
                for r in rows
            )
            body = items or "<li>The line has not started. Read <code>/line</code> and append one.</li>"
            return (
                "<!doctype html><meta charset=utf-8><title>The long line</title>"
                "<h1>The long line</h1>"
                "<p>A story told one line at a time. Read the latest with <code>GET /line</code>, "
                "then append the next with <code>POST /line</code>.</p><ol>%s</ol>" % body
            ).encode()

        def do_POST(self):
            if (urlparse(self.path).path.rstrip("/") or "/") != "/line":
                return self._error(404, "not found")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._error(400, "bad Content-Length")
            if length <= 0 or length > MAX_BODY:
                return self._error(400, "body must be 1-%d bytes" % MAX_BODY)
            try:
                body = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError):
                return self._error(400, "body must be JSON")
            if not isinstance(body, dict):
                return self._error(400, "body must be a JSON object")
            handle = body.get("handle")
            line = body.get("line")
            key = body.get("idempotency_key")
            number = body.get("line_number")
            if not isinstance(number, int) or isinstance(number, bool):
                return self._error(400, "line_number must be an integer")
            try:
                validate(handle, line, key)
            except Invalid as exc:
                return self._error(400, str(exc))
            try:
                row, created = store.append(number, handle, line, key)
            except Conflict as exc:
                return self._error(409, "line_number must be the next one", latest=exc.latest)
            row["duplicate"] = not created
            return self._send(201 if created else 200, row)

    return Handler


def serve(db_path=None, host=None, port=None):
    db_path = db_path or os.environ.get("LONGLINE_DB", "longline.db")
    host = host or os.environ.get("LONGLINE_HOST", "127.0.0.1")
    port = int(port if port is not None else os.environ.get("LONGLINE_PORT", "8080"))
    store = Store(db_path)
    server = ThreadingHTTPServer((host, port), make_handler(store))
    return server, store


def main():
    server, _ = serve()
    print("long line on http://%s:%d" % server.server_address[:2])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
