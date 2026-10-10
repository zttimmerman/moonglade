"""The launcher's control listener: how the front menu (glue) asks the launcher something (openspec change
saves-channel, channel A1 of docs/research/front-menu-saves-spike.md on spike/front-menu-saves).

While the game runs, `./play` (and the app, which runs ./play) opens a TCP listener on 127.0.0.1 with a random free
port and a per-launch random nonce, and hands both to the glue in the generated HSProfiles.lua (frontmenu.render:
HS_CONTROL; inside the play client only, 0600, removed on exit, never in client/). To send a request the glue points
the client's `realmList` at the listener and "logs in" with a coded account name. The client sends an ordinary
3.3.5a AUTH_LOGON_CHALLENGE (sAuthLogonChallenge_C, vendor/azerothcore-wotlk/src/server/apps/authserver/Server/
AuthSession.cpp; harness/hslib/probe/client.py:logon builds the same packet), and the listener answers with a
failure code. The glue reads that code's text from the stock status dialog.

- Token: the account name `HS<nonce><verb><arg>`. The client upper-cases it, so it's `[A-Z0-9]`; the nonce is
  NONCE_HEX hex digits. Verbs: `S` (status, no argument) and `L<g><nn>` (front-menu-saves: load the save at index
  `nn` of the character screen's list generation `g`, charsaves.py). An action (Listener.actions[verb]) decides the
  reply: it returns (ok, why); ACCEPTED only when ok. A validator (Listener.validators[verb]) refuses first without
  running the action (`L`: a stale generation or an unknown index). `L` runs one at a time: a second `L` while one
  runs is REFUSED.
  Connections are served on their own threads (at most MAX_CONCURRENT), so that refusal is immediate.
- Only the 3.3.5a client (build 12340, version 3.3.5) is served: any other challenge is REFUSED ("wrong build").
- Reply: three bytes `00 00 <code>` (cmd, error, result, as AuthSession::HandleLogonChallenge sends a failure). Never
  a success (0x00), never a B/salt, so the client never computes or sends an SRP proof; the dummy password never
  leaves it. ACCEPTED for a valid request, REFUSED for a wrong nonce, an unknown verb or a malformed challenge; a
  truncated or oversized packet is closed with no reply. The codes come from the live table (CODE_TEXT, pinned by
  savesctl.live_codes).
- One challenge per connection, at most MAX_READ bytes read, and a declared size that must fit a challenge
  (MAX_BODY). After the reply the listener lets the client close first (at most LINGER_S), then closes.
- Log: one line per connection, naming the verb, the outcome (and an action's reason) and the source address; never
  the nonce, the account name, a save id or a path (env/logs/savesctl.log, rewritten at each start, and the events in
  play-last.json).

Test-only knobs ($HS_SAVESCTL_TEST, JSON; ./play refuses it without --allow-test-client-dir and uses it only for
--account test): "codes" = the codes valid requests get, in order (then ACCEPTED again), "delay_s" = wait before each
reply, "stop_after" = close the listener after that many connections (the "listener down" scenario).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import socket
import struct
import threading
import time
from pathlib import Path

from . import paths as P

HOST = "127.0.0.1"
NONCE_HEX = 6
VERBS = {"S": "", "L": "[0-9A-F][0-9]{2}"}   # verb -> its argument's regex ("" = no argument)
BUILD, VERSION = 12340, (3, 3, 5)  # the only client served (WotLK 3.3.5a)
MAX_CONCURRENT = 4                # connections served at once (each on its own thread)
EXCLUSIVE = {"L"}                 # verbs whose action runs one at a time (a second one meanwhile is refused)
TOKEN_RE = re.compile(r"^HS([0-9A-F]{%d})([A-Z])([A-Z0-9]*)$" % NONCE_HEX)

# Wire code -> the client's status text (its GlueStrings.lua global), as measured live (savesctl.live_codes,
# 2026-10-09; design.md "Reply-code table"). ACCEPTED and REFUSED each have a text no other tested code produces.
CODE_TEXT = {0x03: "LOGIN_BANNED", 0x04: "LOGIN_UNKNOWN_ACCOUNT", 0x07: "LOGIN_NOTIME", 0x08: "LOGIN_DBBUSY",
             0x0C: "LOGIN_SUSPENDED", 0x10: "LOGIN_LOCKED_ENFORCED"}
ACCEPTED = 0x08
REFUSED = 0x03
CANDIDATES = (0x03, 0x04, 0x07, 0x08, 0x0C, 0x10)   # the codes savesctl.live_codes measures (spec: at least these)

AUTH_LOGON_CHALLENGE = 0x00
HEADER = 4                        # cmd u8, error u8, size u16 (bytes after the header)
FIXED = 30                        # "WoW\0" ver[3] build u16 platform os locale tz ip I_len (AuthSession.cpp)
MAX_BODY = 1024                   # a challenge is FIXED + a name of at most 255 bytes; anything larger is refused
MAX_READ = 64 * 1024              # bytes read from one connection, at most
READ_TIMEOUT_S = 2.0              # the client sends its challenge in one write
# After the reply the listener waits (reading nothing it acts on) for the client to close, at most LINGER_S, then
# closes. Measured live (2026-10-09): closing right after the 3 bytes made the client report "You have been
# disconnected from the server." instead of the code's own text; the real authserver also leaves the closing to the
# client after a failed challenge (AuthSession.cpp sends the failure and keeps reading).
LINGER_S = 2.0
TEST_ENV = "HS_SAVESCTL_TEST"
LOG = P.LOGS / "savesctl.log"


class ControlError(RuntimeError):
    pass


def new_nonce() -> str:
    return secrets.token_hex(NONCE_HEX // 2).upper()


def parse_challenge(data: bytes) -> dict:
    """A complete challenge packet -> {"build", "version", "game", "name"}; raises ValueError if malformed."""
    if len(data) < HEADER + FIXED:
        raise ValueError("short")
    cmd, _err, size = struct.unpack_from("<BBH", data, 0)
    if cmd != AUTH_LOGON_CHALLENGE:
        raise ValueError(f"cmd 0x{cmd:02X}")
    body = data[HEADER:HEADER + size]
    if len(body) != size or size < FIXED:
        raise ValueError("size")
    game = body[0:4]
    if game != b"WoW\x00":
        raise ValueError("game")
    version = tuple(body[4:7])
    (build,) = struct.unpack_from("<H", body, 7)
    n = body[29]
    if FIXED + n != size:
        raise ValueError("name length")
    name = body[30:30 + n].decode("ascii", errors="replace")
    return {"build": build, "version": version, "game": game, "name": name}


def challenge(name: str, build: int = 12340, version: tuple = (3, 3, 5)) -> bytes:
    """A 3.3.5a logon challenge for `name` (the layout probe.client.logon sends); for the offline checks."""
    I = name.upper().encode()
    body = (b"WoW\x00" + bytes(version) + struct.pack("<H", build) + b"68x\x00" + b"niW\x00" + b"SUne"
            + struct.pack("<I", 0) + socket.inet_aton("127.0.0.1") + bytes([len(I)]) + I)
    return struct.pack("<BBH", AUTH_LOGON_CHALLENGE, 8, len(body)) + body


def test_knobs(raw: str | None) -> dict | None:
    """$HS_SAVESCTL_TEST -> {"codes", "delay_s", "stop_after"} (validated; never a success code)."""
    if not raw:
        return None
    try:
        t = json.loads(raw)
    except ValueError as e:
        raise ControlError(f"${TEST_ENV} is not JSON: {e}") from e
    if not isinstance(t, dict) or set(t) - {"codes", "delay_s", "stop_after"}:
        raise ControlError(f"${TEST_ENV} must be an object with codes / delay_s / stop_after")
    codes = t.get("codes") or []
    if not all(isinstance(c, int) and 0 < c <= 0xFF for c in codes):
        raise ControlError(f"${TEST_ENV} codes must be failure codes 1..255 (never a success)")
    return {"codes": list(codes), "delay_s": float(t.get("delay_s") or 0),
            "stop_after": int(t["stop_after"]) if t.get("stop_after") is not None else None}


class Listener:
    """One per launch. start() binds 127.0.0.1:0 and serves on a daemon thread; stop() closes it."""

    def __init__(self, nonce: str | None = None, test: dict | None = None, log_path: Path | None = LOG,
                 actions: dict | None = None, linger_s: float = LINGER_S):
        self.nonce = (nonce or new_nonce()).upper()
        if not re.fullmatch(r"[0-9A-F]{%d}" % NONCE_HEX, self.nonce):
            raise ControlError("bad nonce")
        self.linger_s = linger_s
        self.test = test or {}
        self.codes = list(self.test.get("codes") or [])
        self.log_path = log_path
        self.actions = actions or {}         # verb -> callable(arg) -> (ok, why) | bool (S needs none)
        self.validators: dict = {}           # verb -> callable(arg) -> why it's refused before the action, or None
        self.events: list[dict] = []
        self.sock: socket.socket | None = None
        self.port: int | None = None
        self.thread: threading.Thread | None = None
        self.connections = 0
        self.closed_at: float | None = None
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT)
        self._busy: set[str] = set()       # exclusive verbs whose action is running
        self._workers: list[threading.Thread] = []

    # -- lifetime
    def start(self) -> "Listener":
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind((HOST, 0))
        s.listen(4)
        self.sock = s
        self.port = s.getsockname()[1]
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log_path.write_text("")
        self._log({"event": "listening", "addr": f"{HOST}:{self.port}"})
        self.thread = threading.Thread(target=self._serve, name="savesctl", daemon=True)
        self.thread.start()
        return self

    def stop(self) -> dict:
        s, self.sock = self.sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass
            self.closed_at = time.time()
            self._log({"event": "closed"})
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        return self.summary()

    def summary(self) -> dict:
        """What play-last.json keeps: never the nonce."""
        return {"addr": f"{HOST}:{self.port}" if self.port else None, "connections": self.connections,
                "events": [dict(e) for e in self.events], "closed": self.sock is None}

    # -- serving
    def _log(self, ev: dict) -> None:
        ev = {"t": round(time.time(), 3), **ev}
        with self._lock:
            if ev.get("event") == "request":
                self.events.append(ev)
            if self.log_path:
                try:
                    with open(self.log_path, "a") as f:
                        f.write(json.dumps(ev) + "\n")
                except OSError:
                    pass

    def _serve(self) -> None:
        s = self.sock
        s.settimeout(0.25)                   # so stop() from another thread is noticed (close doesn't wake accept)
        while self.sock is s:
            try:
                conn, addr = s.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections += 1
            if not self._slots.acquire(blocking=False):           # too many at once: no reply, closed
                self._log({"event": "request", "outcome": "refused", "why": "busy", "src": addr[0], "reply": None})
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            t = threading.Thread(target=self._serve_one, args=(conn, addr), name="savesctl-conn", daemon=True)
            self._workers.append(t)
            t.start()
            stop_after = self.test.get("stop_after")
            if stop_after is not None and self.connections >= stop_after:
                for w in self._workers:
                    w.join(timeout=READ_TIMEOUT_S + LINGER_S + 15)
                self.stop()
                return

    def _serve_one(self, conn: socket.socket, addr) -> None:
        try:
            self._handle(conn, addr)
        except Exception as e:  # noqa: BLE001  (one bad connection never takes the listener down)
            self._log({"event": "request", "outcome": "error", "why": type(e).__name__, "src": addr[0]})
        finally:
            try:
                conn.close()
            except OSError:
                pass
            self._slots.release()

    def _read(self, conn: socket.socket) -> tuple[bytes, str | None]:
        """One challenge: (packet, None), or (partial, why) for truncated / oversized."""
        conn.settimeout(READ_TIMEOUT_S)
        buf = b""
        need = HEADER
        while len(buf) < need:
            try:
                chunk = conn.recv(min(4096, MAX_READ - len(buf)))
            except socket.timeout:
                return buf, "truncated"
            if not chunk:
                return buf, "truncated"
            buf += chunk
            if len(buf) >= HEADER and need == HEADER:
                size = struct.unpack_from("<H", buf, 2)[0]
                if size > MAX_BODY:
                    return buf, "oversized"
                need = HEADER + size
            if len(buf) >= MAX_READ:
                return buf, "oversized"
        return buf[:need], None

    def _reply(self, conn: socket.socket, code: int) -> None:
        if code == 0:
            raise ControlError("never a success")      # unreachable by construction; fail closed
        delay = self.test.get("delay_s") or 0
        if delay:
            time.sleep(delay)
        conn.sendall(bytes((AUTH_LOGON_CHALLENGE, 0, code)))
        self._linger(conn)

    def _linger(self, conn: socket.socket) -> None:
        """Until the client closes (or linger_s, or MAX_READ more bytes): nothing it sends is acted on."""
        end, got = time.time() + self.linger_s, 0
        while got < MAX_READ:
            left = end - time.time()
            if left <= 0:
                return
            conn.settimeout(left)
            try:
                chunk = conn.recv(4096)
            except (socket.timeout, OSError):
                return
            if not chunk:
                return
            got += len(chunk)

    def _handle(self, conn: socket.socket, addr) -> None:
        src = addr[0]
        data, why = self._read(conn)
        if why:
            self._log({"event": "request", "outcome": "refused", "why": why, "src": src, "reply": None})
            return
        try:
            c = parse_challenge(data)
        except ValueError as e:
            self._reply(conn, REFUSED)
            self._log({"event": "request", "outcome": "refused", "why": f"malformed ({e})", "src": src,
                       "reply": REFUSED})
            return
        m = TOKEN_RE.match(c["name"].upper())
        if c["build"] != BUILD or c["version"] != VERSION:
            why = "wrong build"
        elif not m:
            why = "not a control token"
        elif not secrets.compare_digest(m.group(1), self.nonce):
            why = "wrong nonce"
        elif m.group(2) not in VERBS:
            why = "unknown verb"
        elif not re.fullmatch(VERBS[m.group(2)], m.group(3)):
            why = "bad argument"
        else:
            why = None
        if why:
            self._reply(conn, REFUSED)
            self._log({"event": "request", "outcome": "refused", "why": why, "src": src, "build": c["build"],
                       "reply": REFUSED})
            return
        verb = m.group(2)
        act = self.actions.get(verb)
        ok, why = True, None
        check = self.validators.get(verb)
        stale = check(m.group(3)) if check else None
        if stale:                                   # e.g. a stale list generation: refused before the action runs
            ok, why, act = False, stale, None
        if act:
            with self._lock:
                busy = verb in EXCLUSIVE and verb in self._busy
                if not busy and verb in EXCLUSIVE:
                    self._busy.add(verb)
            if busy:
                ok, why = False, "busy"
            else:
                try:
                    res = act(m.group(3))
                    ok, why = (res if isinstance(res, tuple) else (res is not False, None))
                except Exception as e:  # noqa: BLE001  (a failing action is a refusal, never a crash)
                    ok, why = False, f"action failed ({type(e).__name__})"
                finally:
                    with self._lock:
                        self._busy.discard(verb)
        if not ok:
            self._reply(conn, REFUSED)
            self._log({"event": "request", "outcome": "refused", "verb": verb, "why": why, "src": src,
                       "build": c["build"], "reply": REFUSED})
            return
        code = self.codes.pop(0) if self.codes else ACCEPTED
        self._reply(conn, code)
        self._log({"event": "request", "outcome": "accepted", "verb": verb, "src": src, "build": c["build"],
                   "version": list(c["version"]), "reply": code} | ({"why": why} if why else {}))


def from_env(account: str, environ=None) -> Listener:
    """The play loop's listener: test knobs only for the test account (./play refuses $HS_SAVESCTL_TEST without
    --allow-test-client-dir before anything starts)."""
    env = os.environ if environ is None else environ
    test = test_knobs(env.get(TEST_ENV)) if account == "test" else None
    return Listener(test=test)
