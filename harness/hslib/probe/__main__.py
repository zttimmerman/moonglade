"""CLI: python3 -m hslib.probe [options]   (also: harness/hs probe [options])

Logs on to authserver, prints the realm list, opens a world session, lists characters and,
with --login NAME, enters the world (prints SMSG_LOGIN_VERIFY_WORLD) and logs out again."""
import argparse
import json
import sys

from .. import paths as P
from . import client as C


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hs probe", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", default=P.TEST_USER)
    ap.add_argument("--password", default=P.TEST_PASS)
    ap.add_argument("--logon-only", action="store_true", help="stop after logon proof + realm list")
    ap.add_argument("--create", metavar="NAME", help="create a character (human warrior) if it doesn't exist")
    ap.add_argument("--login", metavar="NAME", help="enter the world with this character, then log out")
    ap.add_argument("--no-logout", action="store_true", help="with --login: drop the socket instead of logging out")
    a = ap.parse_args(argv)

    out = {"user": a.user.upper()}
    try:
        lr = C.logon(a.user, a.password)
        out["logon"] = lr.evidence()
        if not lr.ok or a.logon_only:
            print(json.dumps(out, indent=2))
            return 0 if lr.ok else 1
        realm = C.localhost_realm(lr.realms)
        if not realm:
            raise C.ProbeError("localhost realm not in realm list")
        with C.WorldSession(a.user, lr.session_key, realm) as ws:
            out["world"] = ws.connect()
            chars = ws.char_enum()
            if a.create and not any(c["name"].lower() == a.create.lower() for c in chars):
                code = ws.char_create(a.create, race=1, cls=1)
                out["char_create"] = f"0x{code:02X} {C.W.RESPONSE_NAMES.get(code, '?')}"
                chars = ws.char_enum()
            out["characters"] = chars
            if a.login:
                c = next((c for c in chars if c["name"].lower() == a.login.lower()), None)
                if not c:
                    raise C.ProbeError(f"no character named {a.login}")
                out["login_verify_world"] = ws.player_login(c["guid"])
                if not a.no_logout:
                    out["logout"] = ws.logout()
    except (C.ProbeError, OSError) as e:
        out["error"] = f"{type(e).__name__}: {e}"
        out |= getattr(e, "info", {})
        print(json.dumps(out, indent=2, default=str))
        return 1
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
