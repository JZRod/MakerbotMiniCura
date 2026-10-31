"""A fake MakerBot printer for testing: HTTP /auth on one port, JSON-RPC TCP on another.

It mimics the protocol as understood from community documentation, including the
awkward bits: unsolicited notifications interleaved with replies, replies that
arrive split across TCP segments, and raw (non-JSON) bytes following put_raw.
"""

import json
import socketserver
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

AUTH_CODE = "authcode-123"
ACCESS_TOKEN = "token-456"


class _HttpHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        printer = self.server.printer
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        reply = {"status": "error"}
        if url.path == "/auth" and q.get("client_id") == "MakerWare" and q.get("client_secret") == "secret":
            kind = q.get("response_type")
            if kind == "code":
                printer.pairing_requested = q.get("username")
                reply = {"status": "success", "answer_code": "ans-1"}
            elif kind == "answer" and q.get("answer_code") == "ans-1":
                printer.answer_polls += 1
                if printer.answer_polls < printer.polls_before_accept:
                    reply = {"status": "success", "answer": "pending"}
                elif printer.reject:
                    reply = {"status": "success", "answer": "rejected"}
                else:
                    reply = {"status": "success", "answer": "accepted", "code": AUTH_CODE}
            elif kind == "token" and q.get("auth_code") == AUTH_CODE and q.get("context") == "jsonrpc":
                reply = {"status": "success", "access_token": ACCESS_TOKEN}
        body = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _RpcHandler(socketserver.BaseRequestHandler):
    def _send(self, obj, split=False):
        raw = json.dumps(obj).encode()
        if split and len(raw) > 6:
            self.request.sendall(raw[:6])
            time.sleep(0.01)
            self.request.sendall(raw[6:])
        else:
            self.request.sendall(raw)

    def _read_exactly(self, n):
        chunks = []
        while n:
            chunk = self.request.recv(min(n, 65536))
            if not chunk:
                raise ConnectionError("client went away")
            chunks.append(chunk)
            n -= len(chunk)
        return b"".join(chunks)

    def handle(self):
        printer = self.server.printer
        decoder = json.JSONDecoder()
        buf = b""
        authed = False
        pending_raw = 0
        while True:
            if pending_raw:
                try:
                    already = min(len(buf), pending_raw)
                    printer.received.extend(buf[:already])
                    buf = buf[already:]
                    missing = pending_raw - already
                    if missing:
                        printer.received.extend(self._read_exactly(missing))
                    pending_raw = 0
                except ConnectionError:
                    return
                continue
            try:
                text = buf.decode()
                obj, end = decoder.raw_decode(text.lstrip()) if text.strip() else (None, 0)
            except (ValueError, UnicodeDecodeError):
                obj = None
            if obj is None:
                chunk = self.request.recv(65536)
                if not chunk:
                    return
                buf += chunk
                continue
            lead = len(text) - len(text.lstrip())
            buf = buf[len(text[:lead + end].encode()):]

            method, params, rid = obj["method"], obj.get("params") or {}, obj["id"]
            printer.calls.append((method, params))
            # unsolicited notification before every reply
            self._send({"jsonrpc": "2.0", "method": "state_notification", "params": {"info": {"state": "idle"}}})

            if method == "handshake":
                self._send({"jsonrpc": "2.0", "id": rid, "result": {"machine_name": "Mock Mini"}}, split=True)
            elif method == "authenticate":
                authed = params.get("access_token") == ACCESS_TOKEN
                if authed:
                    self._send({"jsonrpc": "2.0", "id": rid, "result": "ok"})
                else:
                    self._send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "bad token"}})
            elif not authed:
                self._send({"jsonrpc": "2.0", "id": str(rid), "error": {"code": -1, "message": "not authenticated"}})
            elif method == "put_raw":
                self._send({"jsonrpc": "2.0", "id": rid, "result": None})
                pending_raw = params["length"]
            elif method == "put_term":
                ok = (params["crc"] == (zlib.crc32(bytes(printer.received)) & 0xFFFFFFFF)
                      and params["length"] == len(printer.received))
                printer.crc_ok = ok
                if ok:
                    self._send({"jsonrpc": "2.0", "id": rid, "result": None})
                else:
                    self._send({"jsonrpc": "2.0", "id": rid, "error": {"code": -2, "message": "crc mismatch"}})
            elif method == "print" and printer.busy:
                self._send({"jsonrpc": "2.0", "id": rid, "error": {"code": -3, "message": "printer is busy"}})
            else:
                self._send({"jsonrpc": "2.0", "id": rid, "result": None})


class _ThreadedTcp(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class MockPrinter:
    def __init__(self, polls_before_accept=2, reject=False, busy=False):
        self.polls_before_accept = polls_before_accept
        self.reject = reject
        self.busy = busy
        self.answer_polls = 0
        self.pairing_requested = None
        self.calls = []
        self.received = bytearray()
        self.crc_ok = None
        self.http = HTTPServer(("127.0.0.1", 0), _HttpHandler)
        self.http.printer = self
        self.rpc = _ThreadedTcp(("127.0.0.1", 0), _RpcHandler)
        self.rpc.printer = self
        self.http_port = self.http.server_address[1]
        self.rpc_port = self.rpc.server_address[1]

    def __enter__(self):
        for server in (self.http, self.rpc):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        for server in (self.http, self.rpc):
            server.shutdown()
            server.server_close()
