"""Headless 3.3.5a (build 12340) client: SRP6 logon, realm list, world session, char enum/create, login, logout.

Packet layouts follow the pinned AzerothCore source:
  auth:  src/server/apps/authserver/Server/AuthSession.cpp
  world: src/server/game/Server/WorldSocket.cpp, Protocol/ServerPktHeader.h,
         Handlers/CharacterHandler.cpp, Handlers/MiscHandler.cpp, Entities/Player/Player.cpp (BuildEnumData)
"""
import os
import secrets
import socket
import struct
import time
import zlib

from .. import paths as P, srp6
from . import proto as W


class ProbeError(Exception):
    def __init__(self, msg: str, **info):
        super().__init__(msg)
        self.info = info


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(f"connection closed by server after {len(buf)}/{n} bytes")
        buf += chunk
    return buf


# ----------------------------------------------------------------------------- authserver

class LogonResult:
    def __init__(self):
        self.challenge_result = None   # AuthResult byte of the challenge response
        self.proof_result = None       # AuthResult byte of the proof response
        self.server_proof_ok = None    # M2 matched
        self.session_key = None        # 40-byte K (only on success)
        self.account_flags = None
        self.realms = []

    @property
    def ok(self):
        return self.challenge_result == W.WOW_SUCCESS and self.proof_result == W.WOW_SUCCESS and bool(self.server_proof_ok)

    def evidence(self) -> dict:
        n = lambda c: None if c is None else f"0x{c:02X} {W.AUTH_RESULTS.get(c, '?')}"
        return {"challenge_result": n(self.challenge_result), "proof_result": n(self.proof_result),
                "server_proof_M2_ok": self.server_proof_ok, "session_key_obtained": self.session_key is not None,
                "realms": self.realms}


def logon(username: str, password: str, host=P.HOST, port=P.AUTH_PORT, build=W.BUILD, want_realms=True,
          timeout=15) -> LogonResult:
    """AUTH_LOGON_CHALLENGE → AUTH_LOGON_PROOF (→ REALM_LIST). Never raises on a server-side rejection;
    inspect LogonResult. Raises ConnectionError/ProbeError on transport/protocol failures."""
    res = LogonResult()
    I = username.upper().encode()
    with socket.create_connection((host, port), timeout=timeout) as s:
        # sAuthLogonChallenge_C: size = bytes after the 4-byte initial header = 30 + len(I)
        body = (b"WoW\x00" + bytes(W.VERSION) + struct.pack("<H", build)
                + b"68x\x00" + b"niW\x00" + b"SUne"        # 4CC fields are byte-reversed on the wire ("x86","Win","enUS")
                + struct.pack("<I", 0)                       # timezone_bias
                + socket.inet_aton("127.0.0.1")              # ip
                + bytes([len(I)]) + I)
        s.sendall(struct.pack("<BBH", W.AUTH_LOGON_CHALLENGE, 8, len(body)) + body)

        cmd, _zero, result = _recv_exact(s, 3)
        if cmd != W.AUTH_LOGON_CHALLENGE:
            raise ProbeError(f"unexpected auth cmd 0x{cmd:02X} in challenge response")
        res.challenge_result = result
        if result != W.WOW_SUCCESS:
            return res
        B_b = _recv_exact(s, 32)
        g_len = _recv_exact(s, 1)[0]
        g_b = _recv_exact(s, g_len)
        n_len = _recv_exact(s, 1)[0]
        N_b = _recv_exact(s, n_len)
        salt = _recv_exact(s, 32)
        _version_challenge = _recv_exact(s, 16)
        sec_flags = _recv_exact(s, 1)[0]
        if sec_flags:
            raise ProbeError(f"account requires security flags 0x{sec_flags:02X} (PIN/matrix/TOTP); probe doesn't support them")

        while True:
            a = secrets.randbelow(srp6.le_int(N_b) - 1) + 1
            A_b, M1, K = W.srp_client(username, password, B_b, g_b, N_b, salt, a)
            if srp6.le_int(A_b) % srp6.le_int(N_b):
                break
        # sAuthLogonProof_C: cmd, A[32], M1[20], crc_hash[20], number_of_keys, securityFlags
        s.sendall(bytes([W.AUTH_LOGON_PROOF]) + A_b + M1 + bytes(20) + b"\x00\x00")

        cmd, err = _recv_exact(s, 2)
        if cmd != W.AUTH_LOGON_PROOF:
            raise ProbeError(f"unexpected auth cmd 0x{cmd:02X} in proof response")
        res.proof_result = err
        if err != W.WOW_SUCCESS:
            # failure form: cmd, error, uint16 LoginFlags (VERSION_INVALID sends only cmd+error)
            s.settimeout(1.0)
            try:
                s.recv(2)
            except OSError:
                pass
            return res
        # sAuthLogonProof_S (post-BC): M2[20], AccountFlags u32, SurveyId u32, LoginFlags u16
        rest = _recv_exact(s, 20 + 4 + 4 + 2)
        M2 = rest[:20]
        res.account_flags = struct.unpack("<I", rest[20:24])[0]
        res.server_proof_ok = (M2 == W.session_verifier(A_b, M1, K))
        if not res.server_proof_ok:
            return res
        res.session_key = K

        if want_realms:
            s.sendall(bytes([W.REALM_LIST]) + struct.pack("<I", 0))
            cmd = _recv_exact(s, 1)[0]
            if cmd != W.REALM_LIST:
                raise ProbeError(f"unexpected auth cmd 0x{cmd:02X} in realm list response")
            size = struct.unpack("<H", _recv_exact(s, 2))[0]
            r = W.Reader(_recv_exact(s, size))
            r.u32()
            count = r.u16()
            for _ in range(count):
                realm = {"type": r.u8(), "lock": r.u8(), "flags": r.u8(), "name": r.cstr(), "address": r.cstr(),
                         "population": round(r.f32(), 3), "characters": r.u8(), "timezone": r.u8(), "id": r.u8()}
                if realm["flags"] & W.REALM_FLAG_SPECIFYBUILD:
                    realm["version"] = f"{r.u8()}.{r.u8()}.{r.u8()}"
                    realm["build"] = r.u16()
                res.realms.append(realm)
    return res


# ----------------------------------------------------------------------------- worldserver

class Character(dict):
    pass


class WorldSession:
    """One world connection. Use as a context manager; close() always closes the socket."""

    def __init__(self, username: str, session_key: bytes, realm: dict, timeout=20):
        self.username = username.upper()
        self.K = session_key
        self.realm = realm
        host, _, port = realm["address"].rpartition(":")
        self.addr = (host, int(port))
        self.timeout = timeout
        self.sock = None
        self.crypt = None
        self.log = []          # (direction, opcode name) trail for evidence
        self.auth_response = None
        self.player_guid = None

    # -- context
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    # -- framing
    def send(self, opcode: int, payload: bytes = b""):
        # ClientPktHeader: uint16 size (big-endian, includes the 4-byte cmd) + uint32 cmd (little-endian)
        hdr = struct.pack(">H", len(payload) + 4) + struct.pack("<I", opcode)
        if self.crypt:
            hdr = self.crypt.encrypt(hdr)
        self.sock.sendall(hdr + payload)
        self.log.append(("C>S", W.OPCODE_NAMES.get(opcode, hex(opcode))))

    def recv(self) -> tuple[int, bytes]:
        # ServerPktHeader: size big-endian (2 bytes, or 3 when first byte has 0x80) + uint16 opcode LE
        first = _recv_exact(self.sock, 1)
        if self.crypt:
            first = self.crypt.decrypt(first)
        large = first[0] & 0x80
        rest = _recv_exact(self.sock, 4 if large else 3)
        if self.crypt:
            rest = self.crypt.decrypt(rest)
        if large:
            size = ((first[0] & 0x7F) << 16) | (rest[0] << 8) | rest[1]
            opcode = struct.unpack("<H", rest[2:4])[0]
        else:
            size = (first[0] << 8) | rest[0]
            opcode = struct.unpack("<H", rest[1:3])[0]
        payload = _recv_exact(self.sock, size - 2)
        self.log.append(("S>C", W.OPCODE_NAMES.get(opcode, hex(opcode))))
        return opcode, payload

    def wait_for(self, *opcodes: int, timeout: float | None = None) -> tuple[int, bytes]:
        deadline = time.time() + (timeout or self.timeout)
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise ProbeError(f"timeout waiting for {[W.OPCODE_NAMES.get(o, hex(o)) for o in opcodes]}",
                                 trail=self.log[-30:])
            self.sock.settimeout(left)
            try:
                op, data = self.recv()
            except socket.timeout:
                continue
            if op in opcodes:
                return op, data

    # -- session
    def connect(self) -> dict:
        """SMSG_AUTH_CHALLENGE → CMSG_AUTH_SESSION → SMSG_AUTH_RESPONSE. Returns evidence; raises on failure."""
        self.sock = socket.create_connection(self.addr, timeout=self.timeout)
        op, data = self.wait_for(W.SMSG_AUTH_CHALLENGE)
        r = W.Reader(data)
        r.u32()                    # uint32(1)
        server_seed = r.take(4)    # _authSeed
        local_challenge = os.urandom(4)
        digest = srp6.sha1(self.username.encode(), bytes(4), local_challenge, server_seed, self.K)
        # WorldSocket::HandleAuthSession read order
        payload = (struct.pack("<II", W.BUILD, 0) + self.username.encode() + b"\x00"
                   + struct.pack("<I", 0) + local_challenge
                   + struct.pack("<III", 0, 0, int(self.realm["id"]))
                   + struct.pack("<Q", 0) + digest
                   + struct.pack("<I", 0))                         # addon info: uncompressed size 0 (ReadAddonsInfo skips)
        self.send(W.CMSG_AUTH_SESSION, payload)
        # the server initialises AuthCrypt with K right after reading CMSG_AUTH_SESSION
        self.crypt = W.HeaderCrypt(self.K)
        op, data = self.wait_for(W.SMSG_AUTH_RESPONSE)
        code = data[0]
        if code == W.AUTH_WAIT_QUEUE:
            op, data = self.wait_for(W.SMSG_AUTH_RESPONSE, timeout=120)
            code = data[0]
        self.auth_response = code
        ev = {"world_addr": f"{self.addr[0]}:{self.addr[1]}", "auth_response": f"0x{code:02X} {W.RESPONSE_NAMES.get(code, '?')}"}
        if code != W.AUTH_OK:
            raise ProbeError("world auth failed", **ev)
        return ev

    def char_enum(self) -> list[Character]:
        self.send(W.CMSG_CHAR_ENUM)
        _, data = self.wait_for(W.SMSG_CHAR_ENUM)
        r = W.Reader(data)
        chars = []
        for _ in range(r.u8()):
            c = Character(guid=r.u64(), name=r.cstr(), race=r.u8(), cls=r.u8(), gender=r.u8())
            r.take(5)  # skin, face, hairStyle, hairColor, facialStyle
            c |= {"level": r.u8(), "zone": r.u32(), "map": r.u32(),
                  "x": round(r.f32(), 3), "y": round(r.f32(), 3), "z": round(r.f32(), 3),
                  "guild": r.u32(), "char_flags": r.u32(), "customize_flags": r.u32(), "first_login": r.u8()}
            r.take(12)       # pet display, level, family
            r.take(23 * 9)   # INVENTORY_SLOT_BAG_END x (displayId u32, invType u8, enchant aura u32)
            chars.append(c)
        return chars

    def char_create(self, name: str, race: int, cls: int, gender=0, skin=0, face=0, hair_style=0, hair_color=0,
                    facial_hair=0, outfit=0) -> int:
        """CMSG_CHAR_CREATE (HandleCharCreateOpcode read order). Returns the SMSG_CHAR_CREATE ResponseCode."""
        self.send(W.CMSG_CHAR_CREATE, name.encode() + b"\x00"
                  + bytes([race, cls, gender, skin, face, hair_style, hair_color, facial_hair, outfit]))
        _, data = self.wait_for(W.SMSG_CHAR_CREATE, timeout=30)
        return data[0]

    def player_login(self, guid: int) -> dict:
        """CMSG_PLAYER_LOGIN → SMSG_LOGIN_VERIFY_WORLD (map u32, x, y, z, o floats)."""
        self.send(W.CMSG_PLAYER_LOGIN, struct.pack("<Q", guid))
        op, data = self.wait_for(W.SMSG_LOGIN_VERIFY_WORLD, W.SMSG_CHARACTER_LOGIN_FAILED, timeout=60)
        if op == W.SMSG_CHARACTER_LOGIN_FAILED:
            raise ProbeError("character login failed", reason=data[0] if data else None)
        r = W.Reader(data)
        self.player_guid = guid
        return {"map": r.u32(), "x": round(r.f32(), 3), "y": round(r.f32(), 3), "z": round(r.f32(), 3),
                "o": round(r.f32(), 3)}

    def update_account_data(self, data_type: int, timestamp: int, text: str) -> None:
        """CMSG_UPDATE_ACCOUNT_DATA (HandleUpdateAccountData: u32 type, u32 time, u32 decompressed size, zlib data
        = the text as a C string) → SMSG_UPDATE_ACCOUNT_DATA_COMPLETE. The server stores it with an async
        REPLACE INTO character_account_data (per-character types)."""
        raw = text.encode() + b"\x00"
        self.send(W.CMSG_UPDATE_ACCOUNT_DATA, struct.pack("<III", data_type, timestamp, len(raw)) + zlib.compress(raw))
        self.wait_for(W.SMSG_UPDATE_ACCOUNT_DATA_COMPLETE)

    def logout(self, timeout=45) -> dict:
        """CMSG_LOGOUT_REQUEST → SMSG_LOGOUT_RESPONSE (u32 result, u8 instant) → SMSG_LOGOUT_COMPLETE.
        A non-instant logout completes after the server's 20 s logout timer (WorldSession::ShouldLogOut)."""
        self.send(W.CMSG_LOGOUT_REQUEST)
        _, data = self.wait_for(W.SMSG_LOGOUT_RESPONSE)
        result, instant = struct.unpack("<IB", data[:5])
        if result != 0:
            raise ProbeError("logout refused", result=result)
        t0 = time.time()
        self.wait_for(W.SMSG_LOGOUT_COMPLETE, timeout=timeout)
        self.player_guid = None
        return {"logout_result": result, "instant": bool(instant), "complete_after_s": round(time.time() - t0, 1)}


def localhost_realm(realms: list[dict]) -> dict | None:
    for r in realms:
        if r["address"] == f"{P.HOST}:{P.WORLD_PORT}":
            return r
    return None


def open_world(username: str, password: str) -> tuple[LogonResult, WorldSession, dict]:
    """Logon + realm list + connected WorldSession (at character select). Raises ProbeError on any failure."""
    lr = logon(username, password)
    if not lr.ok:
        raise ProbeError("logon failed", **lr.evidence())
    realm = localhost_realm(lr.realms)
    if not realm:
        raise ProbeError("localhost realm not in realm list", **lr.evidence())
    ws = WorldSession(username, lr.session_key, realm)
    try:
        ev = ws.connect()
    except Exception:
        ws.close()
        raise
    return lr, ws, ev
