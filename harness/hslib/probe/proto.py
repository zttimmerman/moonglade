"""Protocol constants and crypto primitives for the 3.3.5a (build 12340) probe.

Every value here is taken from the pinned AzerothCore source in vendor/azerothcore-wotlk (paths cited
inline), not from memory.
"""
import hashlib
import hmac
import struct

from .. import srp6

BUILD = 12340
VERSION = (3, 3, 5)

# --- authserver commands: src/server/apps/authserver/Server/AuthSession.cpp (enum eAuthCmd)
AUTH_LOGON_CHALLENGE = 0x00
AUTH_LOGON_PROOF = 0x01
REALM_LIST = 0x10

# --- AuthResult: src/server/apps/authserver/Authentication/AuthCodes.h
AUTH_RESULTS = {
    0x00: "WOW_SUCCESS", 0x03: "WOW_FAIL_BANNED", 0x04: "WOW_FAIL_UNKNOWN_ACCOUNT",
    0x05: "WOW_FAIL_INCORRECT_PASSWORD", 0x06: "WOW_FAIL_ALREADY_ONLINE", 0x07: "WOW_FAIL_NO_TIME",
    0x08: "WOW_FAIL_DB_BUSY", 0x09: "WOW_FAIL_VERSION_INVALID", 0x0A: "WOW_FAIL_VERSION_UPDATE",
    0x0B: "WOW_FAIL_INVALID_SERVER", 0x0C: "WOW_FAIL_SUSPENDED", 0x0D: "WOW_FAIL_FAIL_NOACCESS",
    0x10: "WOW_FAIL_LOCKED_ENFORCED", 0x19: "WOW_FAIL_UNLOCKABLE_LOCK", 0xFF: "WOW_FAIL_DISCONNECTED",
}
WOW_SUCCESS = 0x00
WOW_FAIL_UNKNOWN_ACCOUNT = 0x04

# Realm flags: src/server/shared/Realms/Realm.h
REALM_FLAG_SPECIFYBUILD = 0x04

# --- world opcodes: src/server/game/Server/Protocol/Opcodes.h
CMSG_CHAR_CREATE = 0x036
CMSG_CHAR_ENUM = 0x037
SMSG_CHAR_CREATE = 0x03A
SMSG_CHAR_ENUM = 0x03B
CMSG_PLAYER_LOGIN = 0x03D
SMSG_CHARACTER_LOGIN_FAILED = 0x041
CMSG_LOGOUT_REQUEST = 0x04B
SMSG_LOGOUT_RESPONSE = 0x04C
SMSG_LOGOUT_COMPLETE = 0x04D
CMSG_UPDATE_ACCOUNT_DATA = 0x20B
SMSG_UPDATE_ACCOUNT_DATA_COMPLETE = 0x463
CMSG_PING = 0x1DC
SMSG_PONG = 0x1DD
SMSG_AUTH_CHALLENGE = 0x1EC
CMSG_AUTH_SESSION = 0x1ED
SMSG_AUTH_RESPONSE = 0x1EE
SMSG_LOGIN_VERIFY_WORLD = 0x236
# chat, group, movement acks, combat log, quest sharing, keepalive (task m2 3.1; same Opcodes.h)
SMSG_NEW_WORLD = 0x03E
SMSG_GROUP_UNINVITE = 0x077
CMSG_GROUP_DISBAND = 0x07B
SMSG_GROUP_DESTROYED = 0x07C
SMSG_GROUP_LIST = 0x07D
CMSG_MESSAGECHAT = 0x095
SMSG_MESSAGECHAT = 0x096
MSG_MOVE_TELEPORT_ACK = 0x0C7
MSG_MOVE_WORLDPORT_ACK = 0x0DC
CMSG_SET_SELECTION = 0x13D
SMSG_ATTACKSTART = 0x143
SMSG_ATTACKSTOP = 0x144
SMSG_ATTACKERSTATEUPDATE = 0x14A
CMSG_PUSHQUESTTOPARTY = 0x19D
SMSG_NOTIFICATION = 0x1CB
SMSG_SPELLNONMELEEDAMAGELOG = 0x250
MSG_QUEST_PUSH_RESULT = 0x276
SMSG_TIME_SYNC_REQ = 0x390
CMSG_TIME_SYNC_RESP = 0x391
SMSG_GM_MESSAGECHAT = 0x3B3
CMSG_KEEP_ALIVE = 0x407
MSG_MOVE_HEARTBEAT = 0x0EE
# object updates and XP log (m3-vanilla-mode era checks; same Opcodes.h)
SMSG_UPDATE_OBJECT = 0x0A9
SMSG_LOG_XPGAIN = 0x1D0
SMSG_COMPRESSED_UPDATE_OBJECT = 0x1F6   # u32 uncompressed size + zlib stream (WorldSocket.cpp CompressIfNeeded)
# static-data queries and the trainer window (m3-vanilla-mode era.elites / era.mounts; same Opcodes.h)
CMSG_ITEM_QUERY_SINGLE = 0x056
SMSG_ITEM_QUERY_SINGLE_RESPONSE = 0x058
CMSG_CREATURE_QUERY = 0x060
SMSG_CREATURE_QUERY_RESPONSE = 0x061
CMSG_TRAINER_LIST = 0x1B0
SMSG_TRAINER_LIST = 0x1B1

# server text the player can read (seamless-experience client.login_text_clean; same Opcodes.h)
SMSG_CHANNEL_NOTIFY = 0x099
SMSG_CHAT_SERVER_MESSAGE = 0x291
SMSG_AREA_TRIGGER_MESSAGE = 0x2B8
SMSG_RAID_INSTANCE_MESSAGE = 0x2FA
SMSG_DEFENSE_MESSAGE = 0x33A
SMSG_MOTD = 0x33D

OPCODE_NAMES = {v: k for k, v in list(globals().items()) if k.startswith(("CMSG_", "SMSG_")) and isinstance(v, int)}

# --- ChatMsg (src/server/shared/SharedDefines.h enum ChatMsg) and Language (enum Language, same file)
CHAT_MSG_SYSTEM = 0x00
CHAT_MSG_SAY = 0x01
CHAT_MSG_PARTY = 0x02
CHAT_MSG_WHISPER = 0x07
CHAT_MSG_WHISPER_FOREIGN = 0x08
CHAT_MSG_WHISPER_INFORM = 0x09
CHAT_MSG_CHANNEL = 0x11
CHAT_MSG_PARTY_LEADER = 0x33
CHAT_NAMES = {0x00: "system", 0x01: "say", 0x02: "party", 0x07: "whisper", 0x09: "whisper_inform", 0x11: "channel",
              0x33: "party_leader"}
# ChatHandler::BuildChatPacket (src/server/game/Chat/Chat.cpp) writes a sender name for these types
CHAT_MONSTER_TYPES = {0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x29, 0x2A, 0x2F}   # MONSTER_SAY/PARTY/YELL/WHISPER/EMOTE,
                                                                         # RAID_BOSS_EMOTE/WHISPER, BATTLENET
CHAT_BG_SYSTEM_TYPES = {0x24, 0x25, 0x26}
CHAT_ACHIEVEMENT_TYPES = {0x30, 0x31}
LANG_UNIVERSAL = 0     # refused for CMSG_MESSAGECHAT say/party/whisper (ChatHandler.cpp HandleMessagechatOpcode)
LANG_COMMON = 7        # Alliance common tongue (SKILL_LANG_COMMON 98); the harness characters are human

# Group (src/server/game/Groups/Group.h): GroupType, GroupMemberOnlineStatus
GROUPTYPE_LFG = 0x08
MEMBER_STATUS_ONLINE = 0x0001

# QuestShareMessages (src/server/game/Quests/QuestDef.h), sent in MSG_QUEST_PUSH_RESULT
QUEST_PARTY_MSG = {0: "SHARING_QUEST", 1: "CANT_TAKE_QUEST", 2: "ACCEPT_QUEST", 3: "DECLINE_QUEST", 4: "BUSY",
                   5: "LOG_FULL", 6: "HAVE_QUEST", 7: "FINISH_QUEST", 8: "CANT_BE_SHARED_TODAY",
                   9: "SHARING_TIMER_EXPIRED", 10: "NOT_IN_PARTY"}

# ObjectUpdateType: src/server/game/Entities/Object/Updates/UpdateData.h. The probe parses only VALUES and the
# guid-list blocks; a create/movement block (variable-length movement data) ends parsing of that packet.
UPDATETYPE_VALUES = 0
UPDATETYPE_OUT_OF_RANGE_OBJECTS = 4
UPDATETYPE_NEAR_OBJECTS = 5
# Update field indices: src/server/game/Entities/Object/Updates/UpdateFields.h (OBJECT_END = 0x0006,
# UNIT_END = OBJECT_END + 0x008E, UNIT_FIELD_LEVEL = OBJECT_END + 0x0030, PLAYER_XP = UNIT_END + 0x01E6,
# PLAYER_NEXT_LEVEL_XP = UNIT_END + 0x01E7)
OBJECT_END = 0x0006
UNIT_END = OBJECT_END + 0x008E
UNIT_FIELD_LEVEL = OBJECT_END + 0x0030
PLAYER_XP = UNIT_END + 0x01E6
PLAYER_NEXT_LEVEL_XP = UNIT_END + 0x01E7

# ObjectGuid high parts (src/server/game/Entities/Object/ObjectGuid.h enum HighGuid): GUID = high << 48 | ...
HIGHGUID_PLAYER = 0x0000
HIGHGUID_UNIT = 0xF130
HIGHGUID_PET = 0xF140


def guid_high(guid: int) -> int:
    return (guid >> 48) & 0xFFFF


def guid_entry(guid: int) -> int:
    """Entry of a creature GUID (ObjectGuid(HighGuid, entry, counter): entry in bits 24..47)."""
    return (guid >> 24) & 0xFFFFFF


def pack_guid(guid: int) -> bytes:
    """ByteBuffer::appendPackGUID (src/server/shared/Packets/ByteBuffer.h): mask byte, then the non-zero bytes."""
    mask, out = 0, bytearray()
    for i in range(8):
        b = (guid >> (8 * i)) & 0xFF
        if b:
            mask |= 1 << i
            out.append(b)
    return bytes([mask]) + bytes(out)


# AccountDataType (src/server/game/Server/WorldSession.h); 6 is per-character (PER_CHARACTER_CACHE_MASK 0xEA)
PER_CHARACTER_LAYOUT_CACHE = 6

# --- ResponseCodes: src/server/shared/SharedDefines.h
AUTH_OK = 0x0C
AUTH_FAILED = 0x0D
AUTH_WAIT_QUEUE = 0x1B
CHAR_CREATE_SUCCESS = 0x2F
CHAR_CREATE_NAME_IN_USE = 0x32
RESPONSE_NAMES = {
    0x0C: "AUTH_OK", 0x0D: "AUTH_FAILED", 0x0E: "AUTH_REJECT", 0x0F: "AUTH_BAD_SERVER_PROOF",
    0x10: "AUTH_UNAVAILABLE", 0x11: "AUTH_SYSTEM_ERROR", 0x15: "AUTH_UNKNOWN_ACCOUNT",
    0x16: "AUTH_INCORRECT_PASSWORD", 0x1B: "AUTH_WAIT_QUEUE", 0x1C: "AUTH_BANNED", 0x1D: "AUTH_ALREADY_ONLINE",
    0x27: "REALM_LIST_REALM_NOT_FOUND",
    0x2F: "CHAR_CREATE_SUCCESS", 0x30: "CHAR_CREATE_ERROR", 0x31: "CHAR_CREATE_FAILED",
    0x32: "CHAR_CREATE_NAME_IN_USE", 0x33: "CHAR_CREATE_DISABLED", 0x34: "CHAR_CREATE_PVP_TEAMS_VIOLATION",
    0x35: "CHAR_CREATE_SERVER_LIMIT", 0x36: "CHAR_CREATE_ACCOUNT_LIMIT",
    0x57: "CHAR_NAME_SUCCESS", 0x58: "CHAR_NAME_FAILURE", 0x5C: "CHAR_NAME_INVALID_CHARACTER",
    0x5F: "CHAR_NAME_RESERVED",
}

# --- AuthCrypt seeds: src/common/Cryptography/Authentication/AuthCrypt.cpp
# The server encrypts with HMAC(ServerEncryptionKey, K) → the client decrypts with it.
# The server decrypts with HMAC(ServerDecryptionKey, K) → the client encrypts with it.
SERVER_ENCRYPTION_KEY = bytes([0xCC, 0x98, 0xAE, 0x04, 0xE8, 0x97, 0xEA, 0xCA, 0x12, 0xDD, 0xC0, 0x93, 0x42, 0x91, 0x53, 0x57])
SERVER_DECRYPTION_KEY = bytes([0xC2, 0xB3, 0x72, 0x3C, 0xC6, 0xAE, 0xD9, 0xB5, 0x34, 0x3C, 0x53, 0xEE, 0x2F, 0x43, 0x67, 0xCE])


class ARC4:
    def __init__(self, key: bytes):
        S = list(range(256))
        j = 0
        for i in range(256):
            j = (j + S[i] + key[i % len(key)]) & 0xFF
            S[i], S[j] = S[j], S[i]
        self.S, self.i, self.j = S, 0, 0

    def process(self, data: bytes) -> bytes:
        S, i, j = self.S, self.i, self.j
        out = bytearray(len(data))
        for n, b in enumerate(data):
            i = (i + 1) & 0xFF
            j = (j + S[i]) & 0xFF
            S[i], S[j] = S[j], S[i]
            out[n] = b ^ S[(S[i] + S[j]) & 0xFF]
        self.i, self.j = i, j
        return bytes(out)


class HeaderCrypt:
    """Client side of AuthCrypt: ARC4-drop1024 keyed by HMAC-SHA1(seed, K)."""

    def __init__(self, session_key: bytes):
        self.enc = ARC4(hmac.new(SERVER_DECRYPTION_KEY, session_key, hashlib.sha1).digest())
        self.dec = ARC4(hmac.new(SERVER_ENCRYPTION_KEY, session_key, hashlib.sha1).digest())
        self.enc.process(bytes(1024))
        self.dec.process(bytes(1024))

    def encrypt(self, b: bytes) -> bytes:
        return self.enc.process(b)

    def decrypt(self, b: bytes) -> bytes:
        return self.dec.process(b)


# --- SRP6 client side, mirroring src/common/Cryptography/Authentication/SRP6.cpp (server side).
# All big numbers are little-endian byte arrays (BigNumber default), 32 bytes.

def sha1_interleave(S: bytes) -> bytes:
    """SRP6::SHA1Interleave: 40-byte session key K from the 32-byte S."""
    buf0, buf1 = S[0::2], S[1::2]
    p = 0
    while p < len(S) and not S[p]:
        p += 1
    if p & 1:
        p += 1
    p //= 2
    h0, h1 = srp6.sha1(buf0[p:]), srp6.sha1(buf1[p:])
    return bytes(b for pair in zip(h0, h1) for b in pair)


def srp_client(username: str, password: str, B_b: bytes, g_b: bytes, N_b: bytes, salt: bytes, a: int):
    """Return (A_bytes, M1, K) for the logon proof. Inputs are the wire byte arrays from the challenge."""
    N, g = srp6.le_int(N_b), srp6.le_int(g_b)
    B = srp6.le_int(B_b)
    I = username.upper()
    A = pow(g, a, N)
    A_b = srp6.int_le(A, 32)
    u = srp6.le_int(srp6.sha1(A_b, B_b))
    x = srp6.le_int(srp6.sha1(salt, srp6.sha1(f"{I}:{password.upper()}".encode())))
    k = 3  # SRP6::_B: B = g^b + 3v
    S = pow((B - k * pow(g, x, N)) % N, a + u * x, N)
    K = sha1_interleave(srp6.int_le(S, 32))
    ng = bytes(x ^ y for x, y in zip(srp6.sha1(N_b), srp6.sha1(g_b)))
    M1 = srp6.sha1(ng, srp6.sha1(I.encode()), salt, A_b, B_b, K)
    return A_b, M1, K


def session_verifier(A_b: bytes, M1: bytes, K: bytes) -> bytes:
    """SRP6::GetSessionVerifier: M2 = H(A, M1, K)."""
    return srp6.sha1(A_b, M1, K)


class Reader:
    def __init__(self, data: bytes):
        self.d, self.p = data, 0

    def take(self, n: int) -> bytes:
        if self.p + n > len(self.d):
            raise ValueError(f"short packet: need {n} at {self.p}, have {len(self.d)}")
        b = self.d[self.p:self.p + n]
        self.p += n
        return b

    def u8(self):
        return self.take(1)[0]

    def u16(self):
        return struct.unpack("<H", self.take(2))[0]

    def u32(self):
        return struct.unpack("<I", self.take(4))[0]

    def u64(self):
        return struct.unpack("<Q", self.take(8))[0]

    def f32(self):
        return struct.unpack("<f", self.take(4))[0]

    def packed_guid(self) -> int:
        """ByteBuffer::readPackGUID: mask byte, one byte per set bit (little-endian positions)."""
        mask = self.u8()
        g = 0
        for i in range(8):
            if mask & (1 << i):
                g |= self.u8() << (8 * i)
        return g

    def cstr(self) -> str:
        end = self.d.index(b"\x00", self.p)
        s = self.d[self.p:end].decode(errors="replace")
        self.p = end + 1
        return s

    def remaining(self) -> int:
        return len(self.d) - self.p
