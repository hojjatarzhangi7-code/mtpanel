#!/usr/bin/env python3
"""
MTPanel node agent.

A tiny, dependency-free HTTPS relay that sits next to telemt on every node.
The central panel talks to this agent over TLS (self-signed cert, pinned by
SHA-256 fingerprint on the panel side) using a shared token. The agent then
forwards a whitelisted subset of requests to telemt's local control API,
which stays bound to 127.0.0.1 and never faces the internet.
"""
import hmac
import ipaddress
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.0"

LISTEN = os.environ.get("AGENT_LISTEN", "0.0.0.0")
PORT = int(os.environ.get("AGENT_PORT", "7443"))
TOKEN = os.environ.get("AGENT_TOKEN", "")
TELEMT_API = os.environ.get("TELEMT_API", "http://127.0.0.1:9091").rstrip("/")
TELEMT_AUTH = os.environ.get("TELEMT_AUTH", "")
CERT = os.environ.get("AGENT_CERT", "/etc/mtpanel-node/agent.crt")
KEY = os.environ.get("AGENT_KEY", "/etc/mtpanel-node/agent.key")
ALLOW_IPS = [
    ipaddress.ip_network(x.strip(), strict=False)
    for x in os.environ.get("AGENT_ALLOW_IPS", "").split(",")
    if x.strip()
]
MAX_BODY = 1024 * 1024

# Only these telemt API paths can be reached through the agent.
ALLOWED_PREFIXES = (
    "/v1/health",
    "/v1/users",
    "/v1/config",
    "/v1/system/info",
    "/v1/system/reload",
    "/v1/runtime/gates",
    "/v1/stats/summary",
    "/v1/stats/users",
)

_failed = {}  # ip -> (count, first_ts) ; naive brute-force throttling
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # direct to 127.0.0.1, ignore *_proxy env


def _throttled(ip):
    cnt, ts = _failed.get(ip, (0, 0))
    if time.time() - ts > 300:
        return False
    return cnt >= 10


def _note_fail(ip):
    cnt, ts = _failed.get(ip, (0, time.time()))
    if time.time() - ts > 300:
        cnt, ts = 0, time.time()
    _failed[ip] = (cnt + 1, ts)


class Handler(BaseHTTPRequestHandler):
    server_version = "nginx"
    sys_version = ""
    timeout = 30

    def handle(self):
        try:
            self.connection.do_handshake()
        except Exception:
            return  # scanners / non-TLS probes: drop silently
        super().handle()

    def log_message(self, fmt, *args):  # quieter logs
        sys.stderr.write("%s %s\n" % (self.client_address[0], fmt % args))

    def _send(self, code, payload, raw=False):
        body = payload if raw else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        ip = self.client_address[0]
        if ALLOW_IPS:
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                return False
            if not any(addr in n for n in ALLOW_IPS):
                return False
        if _throttled(ip):
            return False
        given = self.headers.get("X-Agent-Token", "")
        if TOKEN and hmac.compare_digest(given.encode(), TOKEN.encode()):
            return True
        _note_fail(ip)
        time.sleep(1)
        return False

    def _handle(self, method):
        if not self._authorized():
            self._send(404, {"ok": False, "error": {"code": "not_found"}})
            return
        path = self.path
        if path == "/agent/ping":
            self._send(200, {"ok": True, "data": {"agent": VERSION}})
            return
        clean = path.split("?", 1)[0]
        if not clean.startswith(ALLOWED_PREFIXES) or ".." in clean or "//" in clean:
            self._send(403, {"ok": False, "error": {"code": "path_not_allowed"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._send(413, {"ok": False, "error": {"code": "payload_too_large"}})
            return
        body = self.rfile.read(length) if length else None
        req = urllib.request.Request(TELEMT_API + path, data=body, method=method)
        if TELEMT_AUTH:
            req.add_header("Authorization", TELEMT_AUTH)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with _opener.open(req, timeout=20) as r:
                self._send(r.status, r.read(), raw=True)
        except urllib.error.HTTPError as e:
            self._send(e.code, e.read(), raw=True)
        except Exception as e:  # telemt down / unreachable
            self._send(502, {"ok": False, "error": {"code": "telemt_unreachable", "message": str(e)}})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_DELETE(self):
        self._handle("DELETE")


def main():
    if not TOKEN or len(TOKEN) < 16:
        sys.exit("AGENT_TOKEN must be set (16+ chars)")
    httpd = ThreadingHTTPServer((LISTEN, PORT), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(CERT, KEY)
    # Handshake lazily in the worker thread so a slow client can't block accept().
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    print(f"mtpanel-node agent {VERSION} listening on {LISTEN}:{PORT} -> {TELEMT_API}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
