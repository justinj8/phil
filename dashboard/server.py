#!/usr/bin/env python3
"""Phil dashboard: a read-only window onto the paper-trading run. Stdlib only.

Routes:
  /               the dashboard (static/index.html + app.js + app.css)
  /healthz        liveness for Railway; never needs auth, never touches disk
  /api/state      the run snapshot (data.snapshot), cached for STATE_TTL_S
  /api/mtm        live CLOB marks for open positions, refreshed in the background
  /api/retro      ?name=RETRO-...md  one retrospective, as text
  /api/log        ?name=<session log> the tail of a supervisor session log

Nothing here writes to the run or starts a session: the dashboard cannot place
a bet, edit a strategy file or spend a token. Set DASHBOARD_PASSWORD to put
HTTP basic auth in front of everything except /healthz.

Environment: PORT (default 8080), PHIL_HOME, PHIL_RUNTIME, DASHBOARD_PASSWORD,
MTM_REFRESH_S (default 180; 0 disables live marks).
"""
import base64
import hmac
import json
import os
import pathlib
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import data

STATIC = pathlib.Path(__file__).resolve().parent / "static"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/theme.js": ("theme.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
STATE_TTL_S = 15
MTM_REFRESH_S = int(os.environ.get("MTM_REFRESH_S", "180"))
PASSWORD = os.environ.get("DASHBOARD_PASSWORD") or ""


class Cache:
    """One snapshot shared by every request inside its TTL."""

    def __init__(self):
        self.lock = threading.Lock()
        self.at = 0.0
        self.body = None

    def state(self):
        with self.lock:
            if self.body is None or time.monotonic() - self.at > STATE_TTL_S:
                try:
                    snap = data.finite(data.snapshot())
                except Exception as e:  # noqa: BLE001 - report, keep serving
                    snap = {"fatal": f"{type(e).__name__}: {e}", "generated_utc": data.iso(data.utcnow())}
                self.body = json.dumps(snap).encode()
                self.at = time.monotonic()
            return self.body


CACHE = Cache()
MTM = {"updated_utc": None, "rows": [], "error": None, "enabled": MTM_REFRESH_S > 0}


def mtm_loop():
    """Refresh live marks off the request path: 60 open positions is 60 CLOB calls."""
    while True:
        try:
            result = data.mark_open_positions()
            MTM.update(rows=result.get("rows", []), error=result.get("error"),
                       updated_utc=data.iso(data.utcnow()))
        except Exception as e:  # noqa: BLE001 - marks are advisory
            MTM.update(error=f"{type(e).__name__}: {e}", updated_utc=data.iso(data.utcnow()))
        time.sleep(MTM_REFRESH_S)


class Handler(BaseHTTPRequestHandler):
    server_version = "phil-dashboard"

    def log_message(self, fmt, *args):  # quiet: only errors reach the Railway log
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            sys.stderr.write("dashboard: %s - %s\n" % (self.address_string(), fmt % args))

    def send(self, code, body, ctype="application/json", cache="no-store"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                         "script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def authorized(self):
        if not PASSWORD:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(header[6:]).decode().partition(":")
            except (ValueError, UnicodeDecodeError):
                return False
            return hmac.compare_digest(given.encode(), PASSWORD.encode())
        return False

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        if path == "/healthz":
            return self.send(200, "ok", "text/plain; charset=utf-8")
        if not self.authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="phil", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        query = parse_qs(url.query)
        if path in ASSETS:
            name, ctype = ASSETS[path]
            try:
                return self.send(200, (STATIC / name).read_bytes(), ctype, "no-cache")
            except OSError:
                return self.send(404, "missing asset", "text/plain; charset=utf-8")
        if path == "/api/state":
            return self.send(200, CACHE.state())
        if path == "/api/mtm":
            return self.send(200, json.dumps(data.finite(MTM)))
        if path == "/api/retro":
            text = data.read_retro((query.get("name") or [""])[0])
            if text is None:
                return self.send(404, "no such retro", "text/plain; charset=utf-8")
            return self.send(200, text, "text/plain; charset=utf-8")
        if path == "/api/log":
            text = data.read_log((query.get("name") or [""])[0])
            if text is None:
                return self.send(404, "no such log", "text/plain; charset=utf-8")
            return self.send(200, text, "text/plain; charset=utf-8")
        return self.send(404, "not found", "text/plain; charset=utf-8")


def main():
    port = int(os.environ.get("PORT", "8080"))
    if MTM_REFRESH_S > 0:
        threading.Thread(target=mtm_loop, name="mtm", daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    httpd.daemon_threads = True
    print(f"dashboard: serving {data.HOME} on :{port}"
          f"{' (basic auth on)' if PASSWORD else ''}", file=sys.stderr, flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
