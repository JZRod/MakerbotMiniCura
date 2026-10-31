"""Minimal network client for MakerBot "Gen 5" printers (Replicator Mini and friends).

Pure standard library: no Cura / Qt imports, so it can be tested on its own.

Protocol summary (reverse engineered by the community, see README for credits):

* Pairing and token exchange happen over plain HTTP on port 80 (``GET /auth``).
  The user has to accept the pairing on the printer itself.
* Printing happens over a TCP JSON-RPC 2.0 connection on port 9999.  Messages are
  bare JSON objects (no length prefix, no delimiter).  The printer may interleave
  unsolicited notification objects (``"method": "state_notification"`` ...) with
  replies, so replies are matched by ``id``.
* A job is sent with ``print`` -> ``process_method(build_plate_cleared)`` ->
  ``put_init`` -> N x (``put_raw`` + raw bytes) -> ``put_term`` (CRC32 of the file).

None of this has been verified against every firmware revision.  Errors are
surfaced with as much context as possible so problems can be diagnosed.
"""

import json
import logging
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Callable, Optional

log = logging.getLogger(__name__)

CLIENT_ID = "MakerWare"
CLIENT_SECRET = "secret"
HTTP_PORT = 80
RPC_PORT = 9999
BLOCK_SIZE = 100000  # bytes per put_raw block

ProgressCallback = Callable[[float, str], None]


class MakerBotError(Exception):
    """Anything that goes wrong talking to the printer."""


class AuthError(MakerBotError):
    """Pairing / authentication problems."""


class RpcError(MakerBotError):
    """The printer answered a JSON-RPC call with an error object."""

    def __init__(self, method: str, error):
        self.method = method
        self.error = error
        message = error.get("message") if isinstance(error, dict) else str(error)
        super().__init__("Printer rejected '%s': %s" % (method, message))


# --------------------------------------------------------------------------- #
# HTTP: pairing and access tokens
# --------------------------------------------------------------------------- #

_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


def validate_host(host: str) -> str:
    """Accept only plain hostnames / IPv4 addresses (no ports, paths or credentials)."""
    host = (host or "").strip()
    if not _HOST_RE.match(host):
        raise MakerBotError("'%s' is not a valid printer IP address or host name" % host)
    return host


def _http_json(host: str, port: int, path: str, params: dict, timeout: float) -> dict:
    host = validate_host(host)
    query = urllib.parse.urlencode(params)
    authority = host if port == 80 else "%s:%d" % (host, port)
    url = "http://%s%s?%s" % (authority, path, query)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as err:
        body = err.read()
        try:
            return json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            raise MakerBotError("Printer answered HTTP %d for %s" % (err.code, path))
    except (urllib.error.URLError, OSError) as err:
        raise MakerBotError("Cannot reach printer at %s: %s" % (host, getattr(err, "reason", err)))
    try:
        parsed = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        raise MakerBotError("Printer sent a non-JSON reply to %s" % path)
    if not isinstance(parsed, dict):
        raise MakerBotError("Unexpected reply from printer for %s: %r" % (path, parsed))
    return parsed


def pair(host: str,
         username: str = "Cura",
         *,
         http_port: int = HTTP_PORT,
         timeout_s: float = 120.0,
         poll_interval: float = 1.0,
         http_timeout: float = 10.0,
         on_waiting: Optional[Callable[[], None]] = None,
         cancel=None) -> str:
    """Ask the printer for an auth code.  The user must accept on the printer.

    ``cancel`` may be a ``threading.Event``.  Returns the long-lived auth code.
    """
    reply = _http_json(host, http_port, "/auth", {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "username": username,
    }, http_timeout)
    answer_code = reply.get("answer_code")
    if not answer_code:
        raise AuthError("Printer did not start pairing (reply: %r)" % (reply,))

    if on_waiting:
        on_waiting()

    deadline = time.monotonic() + timeout_s
    last = {}
    while time.monotonic() < deadline:
        if cancel is not None and cancel.is_set():
            raise AuthError("Pairing cancelled")
        last = _http_json(host, http_port, "/auth", {
            "response_type": "answer",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "answer_code": answer_code,
        }, http_timeout)
        answer = last.get("answer")
        if answer == "accepted":
            code = last.get("code")
            if not code:
                raise AuthError("Printer accepted pairing but sent no code (reply: %r)" % (last,))
            return str(code)
        if answer == "rejected":
            raise AuthError("Pairing was rejected on the printer")
        time.sleep(poll_interval)
    raise AuthError("Timed out waiting for the printer to accept pairing (last reply: %r)" % (last,))


def get_access_token(host: str, auth_code: str, context: str = "jsonrpc",
                     *, http_port: int = HTTP_PORT, http_timeout: float = 10.0) -> str:
    """Exchange the stored auth code for a short-lived access token."""
    reply = _http_json(host, http_port, "/auth", {
        "response_type": "token",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "context": context,
        "auth_code": auth_code,
    }, http_timeout)
    if reply.get("status") == "success" and reply.get("access_token"):
        return str(reply["access_token"])
    raise AuthError("Printer refused the stored credentials - pair the printer again (reply: %r)" % (reply,))


# --------------------------------------------------------------------------- #
# TCP JSON-RPC
# --------------------------------------------------------------------------- #

class RpcClient:
    """Tiny JSON-RPC 2.0 client for the printer's TCP service."""

    def __init__(self, host: str, port: int = RPC_PORT, timeout: float = 15.0):
        self.host = validate_host(host)
        self.port = port
        self.timeout = timeout
        self.notifications = []
        self._sock = None
        self._buf = bytearray()
        self._next_id = 0
        self._decoder = json.JSONDecoder()

    # -- connection ---------------------------------------------------------
    def connect(self) -> None:
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as err:
            raise MakerBotError("Cannot open JSON-RPC connection to %s:%d: %s" % (self.host, self.port, err))
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- low level ----------------------------------------------------------
    def _try_extract(self):
        if not self._buf:
            return None
        text = self._buf.decode("utf-8", errors="surrogateescape")
        stripped = text.lstrip()
        if not stripped:
            self._buf.clear()
            return None
        lead = len(text) - len(stripped)
        try:
            obj, end = self._decoder.raw_decode(stripped)
        except json.JSONDecodeError:
            return None  # incomplete; wait for more bytes
        consumed = len(text[:lead + end].encode("utf-8", errors="surrogateescape"))
        del self._buf[:consumed]
        return obj

    def _read_message(self, deadline: float):
        while True:
            obj = self._try_extract()
            if obj is not None:
                return obj
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MakerBotError("Timed out waiting for the printer")
            self._sock.settimeout(remaining)
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:
                raise MakerBotError("Timed out waiting for the printer")
            except OSError as err:
                raise MakerBotError("Connection to printer failed: %s" % err)
            if not chunk:
                raise MakerBotError("Printer closed the connection")
            self._buf.extend(chunk)

    def send_raw(self, data: bytes) -> None:
        try:
            self._sock.settimeout(self.timeout)
            self._sock.sendall(data)
        except OSError as err:
            raise MakerBotError("Failed sending data to printer: %s" % err)

    # -- JSON-RPC -----------------------------------------------------------
    def call(self, method: str, params=None, timeout: Optional[float] = None):
        if self._sock is None:
            raise MakerBotError("Not connected")
        self._next_id += 1
        request_id = self._next_id
        payload = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        log.debug("-> %s", payload[:300])
        self.send_raw(payload.encode("utf-8"))

        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        while True:
            message = self._read_message(deadline)
            if not isinstance(message, dict):
                continue
            if "method" in message and "result" not in message:
                # Unsolicited notification from the printer; keep a few for debugging.
                self.notifications.append(message)
                del self.notifications[:-20]
                continue
            if str(message.get("id")) != str(request_id):
                log.debug("ignoring reply with unexpected id: %r", message)
                continue
            log.debug("<- %s", str(message)[:300])
            if message.get("error"):
                raise RpcError(method, message["error"])
            return message.get("result")


# --------------------------------------------------------------------------- #
# High level: send a .makerbot file and start printing
# --------------------------------------------------------------------------- #

def sanitize_filename(name: str) -> str:
    stem = re.sub(r"\.makerbot$", "", name or "", flags=re.IGNORECASE)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "cura_print"
    return stem[:40] + ".makerbot"


def upload_and_print(host: str,
                     auth_code: str,
                     data: bytes,
                     filename: str,
                     *,
                     http_port: int = HTTP_PORT,
                     rpc_port: int = RPC_PORT,
                     block_size: int = BLOCK_SIZE,
                     progress: Optional[ProgressCallback] = None) -> dict:
    """Transfer ``data`` (a complete .makerbot file) and start the print.

    The caller is responsible for having the user confirm that the build plate is
    clear: this function acknowledges the printer's "build plate clear" prompt.
    """
    def report(fraction: float, text: str) -> None:
        if progress:
            progress(max(0.0, min(1.0, fraction)), text)

    filename = sanitize_filename(filename)
    report(0.0, "Authenticating")
    token = get_access_token(host, auth_code, "jsonrpc", http_port=http_port)

    client = RpcClient(host, rpc_port)
    client.connect()
    try:
        handshake = client.call("handshake", {})
        client.call("authenticate", {"access_token": token})

        report(0.02, "Starting job on printer")
        client.call("print", {"filepath": filename, "transfer_wait": True})
        client.call("process_method", {"method": "build_plate_cleared"})
        client.call("put_init", {
            "block_size": block_size,
            "file_id": "1",
            "file_path": "/current_thing/%s" % filename,
            "length": len(data),
        })

        sent = 0
        total = len(data)
        for offset in range(0, total, block_size):
            block = data[offset:offset + block_size]
            client.call("put_raw", {"file_id": "1", "length": len(block)})
            client.send_raw(block)
            sent += len(block)
            report(0.05 + 0.93 * sent / max(total, 1), "Uploading %d / %d KB" % (sent // 1024, total // 1024))

        client.call("put_term", {
            "crc": zlib.crc32(data) & 0xFFFFFFFF,
            "file_id": "1",
            "length": total,
        }, timeout=60.0)
        report(1.0, "Done")
        return {"handshake": handshake, "filename": filename, "bytes": total}
    finally:
        client.close()


# --------------------------------------------------------------------------- #
# Optional discovery via mDNS (needs the 'zeroconf' package, which Cura ships)
# --------------------------------------------------------------------------- #

def discover(timeout: float = 3.0) -> list:
    """Return [(name, ip)] of printers announcing ``_makerbot-jsonrpc._tcp``.

    Older firmware may not announce itself; in that case enter the IP manually.
    """
    try:
        from zeroconf import ServiceBrowser, Zeroconf  # type: ignore
    except ImportError:
        return []

    found = {}

    class _Listener:
        def add_service(self, zc, type_, name):
            try:
                info = zc.get_service_info(type_, name, 2000)
            except Exception:  # pragma: no cover - network dependent
                return
            if info:
                for address in info.parsed_addresses():
                    found[address] = name.split(".")[0]

        def update_service(self, zc, type_, name):
            self.add_service(zc, type_, name)

        def remove_service(self, zc, type_, name):
            pass

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, "_makerbot-jsonrpc._tcp.local.", _Listener())
        time.sleep(timeout)
    finally:
        zc.close()
    return [(name, ip) for ip, name in found.items()]
