"""Headless 3.3.5a (build 12340) client: SRP6 logon, realm list, world session, char enum/create, login, logout.

Packet layouts follow the pinned AzerothCore source:
  auth:  src/server/apps/authserver/Server/AuthSession.cpp
  world: src/server/game/Server/WorldSocket.cpp, Protocol/ServerPktHeader.h,
         Handlers/CharacterHandler.cpp, Handlers/MiscHandler.cpp, Entities/Player/Player.cpp (BuildEnumData)
  in-world session (start_pump; task m2 3.1), paths under src/server/game/:
         chat send   Handlers/ChatHandler.cpp WorldSession::HandleMessagechatOpcode
         chat recv   Chat/Chat.cpp ChatHandler::BuildChatPacket; SMSG_NOTIFICATION in ChatHandler::SendNotification
         group       Groups/Group.cpp Group::SendUpdateToPlayer (SMSG_GROUP_LIST), Group::Disband
         teleports   Entities/Player/Player.cpp SendTeleportAckPacket / TeleportTo (SMSG_NEW_WORLD),
                     Handlers/MovementHandler.cpp HandleMoveTeleportAck, HandleMoveWorldportAck, HandleTimeSyncResp
         keepalive   Server/WorldSocket.cpp CMSG_KEEP_ALIVE (resets the 60 s SocketTimeOutTimeActive idle timer)
         combat      Entities/Unit/Unit.cpp SendMeleeAttackStart, SendMeleeAttackStop + Server/Packets/CombatPackets.cpp
                     SAttackStop::Write, SendAttackStateUpdate, SendSpellNonMeleeDamageLog
         quests      Handlers/QuestHandler.cpp HandlePushQuestToParty + Server/Packets/QuestPackets.cpp
                     PushQuestToParty::Read, QuestPushResult::Write
         selection   Handlers/MiscHandler.cpp HandleSetSelectionOpcode (uint64 guid)
"""
import os
import secrets
import socket
import struct
import threading
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
        self._send_lock = threading.Lock()
        self._pump = None              # background reader thread once in the world (start_pump)
        self._pump_stop = threading.Event()
        self._cv = threading.Condition()
        self.events = []               # parsed server events while pumping (dicts with "kind", "t")
        self.ignored = {}              # opcode name -> count of packets the pump didn't parse
        self.pump_error = None
        self.group = None              # last parsed SMSG_GROUP_LIST (dict) or None

    # -- context
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.stop_pump()
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    # -- framing
    def send(self, opcode: int, payload: bytes = b""):
        # ClientPktHeader: uint16 size (big-endian, includes the 4-byte cmd) + uint32 cmd (little-endian)
        with self._send_lock:   # the pump thread sends acks; header encryption order must match send order
            hdr = struct.pack(">H", len(payload) + 4) + struct.pack("<I", opcode)
            if self.crypt:
                hdr = self.crypt.encrypt(hdr)
            self.sock.sendall(hdr + payload)
            self.log.append(("C>S", W.OPCODE_NAMES.get(opcode, hex(opcode))))

    def recv(self, first: bytes | None = None) -> tuple[int, bytes]:
        # ServerPktHeader: size big-endian (2 bytes, or 3 when first byte has 0x80) + uint16 opcode LE
        first = first if first is not None else _recv_exact(self.sock, 1)
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
        if self._pump:
            raise ProbeError("wait_for while the pump thread owns the socket; use wait_event")
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

    # ------------------------------------------------------------------ in-world session (task m2 3.1)
    KEEPALIVE_S = 15.0   # CMSG_KEEP_ALIVE period; the server drops an in-world socket idle for 60 s

    def start_pump(self):
        """Hand the socket to a background reader once in the world. It answers what a real client must answer
        (teleport acks, worldport acks, time sync), sends keepalives, and records chat, group, combat and quest-push
        events in self.events. While it runs, use wait_event/ events_since instead of wait_for."""
        if self._pump:
            return
        self._pump_stop.clear()
        self._pump = threading.Thread(target=self._pump_loop, name=f"probe-pump-{self.username}", daemon=True)
        self._pump.start()

    def stop_pump(self, timeout=5.0):
        t = self._pump
        if not t:
            return
        self._pump_stop.set()
        if t is not threading.current_thread():
            t.join(timeout)
        self._pump = None
        if self.sock:
            self.sock.settimeout(self.timeout)

    def _read_one(self, poll_s: float):
        """One packet, or None if nothing started arriving within poll_s. Only the first header byte is read with
        the short timeout, so a timeout never splits a packet."""
        self.sock.settimeout(poll_s)
        try:
            first = self.sock.recv(1)
        except socket.timeout:
            return None
        if not first:
            raise ConnectionError("connection closed by server")
        self.sock.settimeout(30)
        return self.recv(first)

    def _pump_loop(self):
        last_keepalive = time.time()
        try:
            while not self._pump_stop.is_set():
                if time.time() - last_keepalive >= self.KEEPALIVE_S:
                    self.send(W.CMSG_KEEP_ALIVE)
                    last_keepalive = time.time()
                pkt = self._read_one(0.25)
                if pkt is not None:
                    self._dispatch(*pkt)
        except Exception as e:   # recorded; wait_event raises it to the caller
            self.pump_error = f"{type(e).__name__}: {e}"
            with self._cv:
                self._cv.notify_all()

    def _emit(self, kind: str, **ev):
        with self._cv:
            ev = {"kind": kind, "i": len(self.events), "t": round(time.time(), 3)} | ev
            self.events.append(ev)
            self._cv.notify_all()

    def _dispatch(self, op: int, data: bytes):
        r = W.Reader(data)
        if op in (W.SMSG_MESSAGECHAT, W.SMSG_GM_MESSAGECHAT):
            self._emit("chat", **parse_chat(data, gm=(op == W.SMSG_GM_MESSAGECHAT)))
        elif op == W.SMSG_NOTIFICATION:
            self._emit("notification", text=r.cstr())
        elif op == W.SMSG_GROUP_LIST:
            self.group = parse_group_list(data)
            self._emit("group_list", **self.group)
        elif op == W.SMSG_GROUP_DESTROYED:
            self.group = None
            self._emit("group_destroyed")
        elif op == W.SMSG_GROUP_UNINVITE:
            self._emit("group_uninvite")
        elif op == W.MSG_MOVE_TELEPORT_ACK:
            # server: packed guid, uint32 movement counter, MovementInfo (Unit::BuildMovementPacket: u32 flags,
            # u16 flags2, u32 time, x, y, z, o). Reply (HandleMoveTeleportAck): packed guid, uint32, uint32 time;
            # the server moves the player to the teleport destination only on this ack.
            guid = r.packed_guid()
            counter = r.u32()
            r.u32(), r.u16(), r.u32()
            x, y, z, o = r.f32(), r.f32(), r.f32(), r.f32()
            self.send(W.MSG_MOVE_TELEPORT_ACK, W.pack_guid(guid) + struct.pack("<II", counter, _ms()))
            self._emit("teleport_near", guid=guid, x=round(x, 2), y=round(y, 2), z=round(z, 2))
        elif op == W.SMSG_NEW_WORLD:
            mapid, x, y, z, o = struct.unpack("<Iffff", data[:20])
            self.send(W.MSG_MOVE_WORLDPORT_ACK)    # HandleMoveWorldportAck: empty payload
            self._emit("new_world", map=mapid, x=round(x, 2), y=round(y, 2), z=round(z, 2))
        elif op == W.SMSG_TIME_SYNC_REQ:
            self.send(W.CMSG_TIME_SYNC_RESP, struct.pack("<II", r.u32(), _ms()))   # counter, client ticks
        elif op == W.SMSG_ATTACKSTART:
            self._emit("attack_start", attacker=r.u64(), victim=r.u64())
        elif op == W.SMSG_ATTACKSTOP:
            a, v = r.packed_guid(), r.packed_guid()
            self._emit("attack_stop", attacker=a, victim=v, now_dead=r.u32())
        elif op == W.SMSG_ATTACKERSTATEUPDATE:
            hit = r.u32()
            a, v = r.packed_guid(), r.packed_guid()
            self._emit("melee_damage", attacker=a, victim=v, damage=r.u32(), overkill=r.u32(), hit_info=hit)
        elif op == W.SMSG_SPELLNONMELEEDAMAGELOG:
            v, a = r.packed_guid(), r.packed_guid()
            self._emit("spell_damage", attacker=a, victim=v, spell=r.u32(), damage=r.u32(), overkill=r.u32())
        elif op == W.MSG_QUEST_PUSH_RESULT:
            g, m = r.u64(), r.u8()
            self._emit("quest_push_result", guid=g, msg=m, msg_name=W.QUEST_PARTY_MSG.get(m, "?"))
        elif op == W.SMSG_LOGOUT_RESPONSE:
            result, instant = struct.unpack("<IB", data[:5])
            self._emit("logout_response", result=result, instant=instant)
        elif op == W.SMSG_LOGOUT_COMPLETE:
            self._emit("logout_complete")
        else:
            name = W.OPCODE_NAMES.get(op, hex(op))
            self.ignored[name] = self.ignored.get(name, 0) + 1

    def mark(self) -> int:
        """Index into self.events; pass it to events_since / wait_event to look only at later events."""
        with self._cv:
            return len(self.events)

    def events_since(self, since: int = 0, kind: str | None = None) -> list[dict]:
        with self._cv:
            return [e for e in self.events[since:] if kind is None or e["kind"] == kind]

    def wait_event(self, pred, since: int = 0, timeout: float = 30.0) -> dict | None:
        """First event at index >= since with pred(event) true, waiting up to timeout; None on timeout.
        Raises ProbeError if the pump died (connection lost)."""
        deadline = time.time() + timeout
        with self._cv:
            while True:
                for e in self.events[since:]:
                    if pred(e):
                        return e
                since = len(self.events)
                if self.pump_error:
                    raise ProbeError("probe session lost", pump_error=self.pump_error, trail=self.log[-30:])
                left = deadline - time.time()
                if left <= 0:
                    return None
                self._cv.wait(min(left, 0.5))

    def chat(self, msg: str, chat_type: int = W.CHAT_MSG_SAY, to: str | None = None, lang: int = W.LANG_COMMON):
        """CMSG_MESSAGECHAT (HandleMessagechatOpcode read order): uint32 type, uint32 lang, [whisper: target name
        cstring], message cstring. A message starting with '.' is parsed as a chat command (ChatHandler::ParseCommands)
        under this session's security."""
        if len(msg) > 255:
            raise ProbeError("chat message longer than 255 (server drops it)", msg=msg)
        body = struct.pack("<II", chat_type, lang)
        if chat_type == W.CHAT_MSG_WHISPER:
            body += (to or "").encode() + b"\x00"
        elif chat_type == W.CHAT_MSG_CHANNEL:
            raise ProbeError("channel chat not supported")
        self.send(W.CMSG_MESSAGECHAT, body + msg.encode() + b"\x00")

    def command(self, cmd: str, settle: float = 1.5, timeout: float = 10.0) -> list[str]:
        """Type a chat command (say) and return the system-message / notification lines that arrive until `settle`
        seconds pass without a new one (at most `timeout`)."""
        def is_sys(e):
            return e["kind"] == "notification" or (e["kind"] == "chat" and e["type"] == W.CHAT_MSG_SYSTEM)
        since = self.mark()
        self.chat(cmd)
        out, t0, last = [], time.time(), time.time()
        while time.time() - t0 < timeout:
            if self.pump_error:
                raise ProbeError("probe session lost", pump_error=self.pump_error)
            with self._cv:
                new = [e for e in self.events[since:] if is_sys(e)]
                since = len(self.events)
            if new:
                out += [e.get("text") if e["kind"] == "notification" else e["msg"] for e in new]
                last = time.time()
            elif time.time() - last >= settle:
                break
            time.sleep(0.1)
        return out

    def set_selection(self, guid: int):
        """CMSG_SET_SELECTION: uint64 target guid (what clicking a unit does)."""
        self.send(W.CMSG_SET_SELECTION, struct.pack("<Q", guid))

    def push_quest_to_party(self, quest_id: int):
        """CMSG_PUSHQUESTTOPARTY (PushQuestToParty::Read): uint32 quest id; the 'Share quest' button."""
        self.send(W.CMSG_PUSHQUESTTOPARTY, struct.pack("<I", quest_id))

    def leave_group(self):
        """CMSG_GROUP_DISBAND (no payload): leave the current party."""
        self.send(W.CMSG_GROUP_DISBAND)

    def logout_pumped(self, timeout=45) -> dict:
        """logout() for a pumped session: CMSG_LOGOUT_REQUEST, then SMSG_LOGOUT_RESPONSE / _COMPLETE via events."""
        since = self.mark()
        self.send(W.CMSG_LOGOUT_REQUEST)
        resp = self.wait_event(lambda e: e["kind"] == "logout_response", since, timeout=15)
        if not resp or resp["result"] != 0:
            raise ProbeError("logout refused or unanswered", response=resp)
        t0 = time.time()
        done = self.wait_event(lambda e: e["kind"] == "logout_complete", since, timeout=timeout)
        if not done:
            raise ProbeError("no SMSG_LOGOUT_COMPLETE", response=resp)
        self.stop_pump()
        self.player_guid = None
        return {"logout_result": 0, "instant": bool(resp["instant"]), "complete_after_s": round(time.time() - t0, 1)}


def _ms() -> int:
    return int(time.monotonic() * 1000) & 0xFFFFFFFF


def parse_chat(data: bytes, gm: bool = False) -> dict:
    """SMSG_MESSAGECHAT / SMSG_GM_MESSAGECHAT, mirroring ChatHandler::BuildChatPacket (Chat/Chat.cpp): uint8 type,
    int32 lang, uint64 sender, uint32 flags, type-specific names/receiver, uint32 len, message cstring, uint8 tag."""
    r = W.Reader(data)
    t = r.u8()
    lang = struct.unpack("<i", r.take(4))[0]
    sender = r.u64()
    r.u32()
    ev = {"type": t, "type_name": W.CHAT_NAMES.get(t, hex(t)), "lang": lang, "sender": sender}
    if t in W.CHAT_MONSTER_TYPES:
        r.u32()
        ev["sender_name"] = r.cstr()
        recv = r.u64()
        if recv and W.guid_high(recv) not in (W.HIGHGUID_PLAYER, W.HIGHGUID_PET):
            r.u32()
            ev["receiver_name"] = r.cstr()
    elif t == W.CHAT_MSG_WHISPER_FOREIGN:
        r.u32()
        ev["sender_name"] = r.cstr()
        recv = r.u64()
    elif t in W.CHAT_BG_SYSTEM_TYPES:
        recv = r.u64()
        if recv and W.guid_high(recv) != W.HIGHGUID_PLAYER:
            r.u32()
            ev["receiver_name"] = r.cstr()
    elif t in W.CHAT_ACHIEVEMENT_TYPES:
        recv = r.u64()
    else:
        if gm:
            r.u32()
            ev["sender_name"] = r.cstr()
        if t == W.CHAT_MSG_CHANNEL:
            ev["channel"] = r.cstr()
        recv = r.u64()
    ev["receiver"] = recv
    r.u32()                       # message length + 1
    ev["msg"] = r.cstr()
    return ev


def parse_group_list(data: bytes) -> dict:
    """SMSG_GROUP_LIST, mirroring Group::SendUpdateToPlayer (Groups/Group.cpp): the receiver's own slot first, then
    every OTHER member (name, guid, online state, subgroup, flags, roles), then the leader."""
    r = W.Reader(data)
    g = {"group_type": r.u8(), "subgroup": r.u8(), "flags": r.u8(), "roles": r.u8()}
    if g["group_type"] & W.GROUPTYPE_LFG:
        r.u8(), r.u32()
    g["group_guid"] = r.u64()
    g["counter"] = r.u32()
    members = []
    for _ in range(r.u32()):
        members.append({"name": r.cstr(), "guid": r.u64(), "online": bool(r.u8() & W.MEMBER_STATUS_ONLINE),
                        "subgroup": r.u8(), "flags": r.u8(), "roles": r.u8()})
    g["members"] = members
    g["leader"] = r.u64()
    return g


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
