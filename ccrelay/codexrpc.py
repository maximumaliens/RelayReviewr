"""Minimal client for the Codex app-server daemon control socket (JSON-RPC over WebSocket over a Unix socket).

Each call opens a short-lived connection: initialize, one request, close. The relay never
stays subscribed to a thread, so menus (elicitations) are always routed to the Codex window.
"""
import base64
import json
import os
import pathlib
import socket
import struct
import subprocess
import time


class CodexError(Exception):
    pass


def socket_path(cfg):
    return pathlib.Path(cfg["codex_home"]) / "app-server-control" / "app-server-control.sock"


def codex_env(cfg):
    env = dict(os.environ)
    env["CODEX_HOME"] = cfg["codex_home"]
    return env


class _Conn:
    def __init__(self, path, timeout):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.settimeout(timeout)
        self.s.connect(str(path))
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((
            "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            chunk = self.s.recv(4096)
            if not chunk:
                raise CodexError("daemon closed the connection during handshake")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise CodexError(f"unexpected handshake: {head[:80]!r}")
        self.next_id = 1

    def _send(self, obj):
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            hdr = bytes([0x81, 0x80 | n])
        elif n < 65536:
            hdr = bytes([0x81, 0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", n)
        self.s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _exact(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise CodexError("daemon closed the connection")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _frame(self):
        b1, b2 = self._exact(2)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._exact(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._exact(8))[0]
        if b2 & 0x80:
            self._exact(4)
        return b1 & 0x0F, self._exact(n)

    def request(self, method, params):
        rid = self.next_id
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            op, payload = self._frame()
            if op == 8:
                raise CodexError("daemon closed the connection")
            if op != 1:
                continue
            msg = json.loads(payload)
            if msg.get("id") == rid and "method" not in msg:
                if "error" in msg:
                    raise CodexError(f"{method}: {msg['error'].get('message', msg['error'])}")
                return msg.get("result")

    def notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self):
        try:
            mask = os.urandom(4)
            self.s.sendall(bytes([0x88, 0x82]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(b"\x03\xe8")))
        except OSError:
            pass
        try:
            self.s.close()
        except OSError:
            pass


def call(cfg, method, params, timeout=30):
    path = socket_path(cfg)
    if not path.exists():
        raise CodexError(f"Codex daemon socket not found at {path}")
    conn = _Conn(path, timeout)
    try:
        conn.request("initialize", {"clientInfo": {"name": "cc-codex-relay", "version": "1"},
                                    "capabilities": {"experimentalApi": True}})
        conn.notify("initialized")
        return conn.request(method, params)
    finally:
        conn.close()


def daemon_alive(cfg):
    try:
        call(cfg, "thread/loaded/list", {}, timeout=5)
        return True
    except (OSError, CodexError):
        return False


def ensure_daemon(cfg):
    if daemon_alive(cfg):
        return
    # The control socket lives under /tmp behind a link in CODEX_HOME. After a reboot (WSL clears
    # /tmp) or a new sandbox, the link points nowhere and the daemon refuses to start ("File exists").
    link = socket_path(cfg)
    if link.is_symlink() and not link.exists():
        link.unlink()
    r = subprocess.run([cfg["codex_bin"], "app-server", "daemon", "start"], env=codex_env(cfg),
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    for _ in range(30):
        if daemon_alive(cfg):
            return
        time.sleep(1)
    raise CodexError(f"could not start the Codex daemon: {(r.stderr or r.stdout).strip()[:300]}")


def loaded_threads(cfg):
    return set(call(cfg, "thread/loaded/list", {}).get("data", []))


def approval_param(cfg):
    policy = cfg["approval_policy"]
    return policy if isinstance(policy, str) else {"granular": policy}


def start_thread(cfg, cwd, instructions, name):
    result = call(cfg, "thread/start", {
        "cwd": cwd,
        "sandbox": "read-only",
        "approvalPolicy": approval_param(cfg),
        "developerInstructions": instructions,
    }, timeout=60)
    thread_id = result["thread"]["id"]
    call(cfg, "thread/name/set", {"threadId": thread_id, "name": name})
    return thread_id


def ensure_loaded(cfg, thread_id):
    if thread_id not in loaded_threads(cfg):
        call(cfg, "thread/resume", {"threadId": thread_id, "excludeTurns": True}, timeout=60)
    if thread_id not in loaded_threads(cfg):
        raise CodexError(f"thread {thread_id} is not loaded in the Codex daemon")


def queue_message(cfg, thread_id, text, client_id):
    result = call(cfg, "thread/queue/add", {
        "threadId": thread_id,
        "clientUserMessageId": client_id,
        "input": [{"type": "text", "text": text}],
    })
    # Codex replies {"queuedSubmission": {"id": ...}} (ThreadQueueAddResponse); the id is what thread/queue/list lists
    item = (result or {}).get("queuedSubmission")
    return item.get("id") if isinstance(item, dict) else None


def queued_ids(cfg, thread_id):
    result = call(cfg, "thread/queue/list", {"threadId": thread_id}) or {}
    return {item.get("id") for item in result.get("data", [])}


def thread_status(cfg, thread_id):
    result = call(cfg, "thread/read", {"threadId": thread_id, "includeTurns": False}) or {}
    status = (result.get("thread") or {}).get("status")
    return status.get("type") if isinstance(status, dict) else status
