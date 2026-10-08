"""harness-control: GM + SQL checks (fixtures are in fixture_checks.py)."""
from .. import db, paths as P, soap
from ..registry import check, require


@check("gm.server_info", order=60, capability="harness-control", stack="up", tier="quick")
def server_info(ctx):
    """`hs gm "server info"` exits 0 with revision + player count."""
    import subprocess
    r = subprocess.run([str(P.HARNESS / "hs"), "gm", "server info"], capture_output=True, text=True, timeout=60)
    ev = {"cmd": 'harness/hs gm "server info"', "exit": r.returncode, "output": r.stdout[-1500:]}
    require(r.returncode == 0, "gm command failed", **ev)
    require("AzerothCore rev." in r.stdout, "no core revision in output", **ev)
    require("players online" in r.stdout.lower() or "connected players" in r.stdout.lower(), "no player count in output", **ev)
    return ev


@check("gm.invalid", order=61, capability="harness-control", stack="up", tier="quick")
def invalid(ctx):
    """Unknown GM command exits nonzero and surfaces the server's error text."""
    import subprocess
    r = subprocess.run([str(P.HARNESS / "hs"), "gm", "definitelynotacommand"], capture_output=True, text=True, timeout=60)
    ev = {"exit": r.returncode, "output": r.stdout[-800:], "stderr": r.stderr[-400:]}
    require(r.returncode == 1, "expected exit 1 for unknown command", **ev)
    require("does not exist" in r.stdout, "server error text (LANG_CMD_INVALID) not surfaced", **ev)
    return ev


@check("sql.world_populated", order=70, capability="harness-control", stack="up", tier="quick")
def world_populated(ctx):
    """creature_template has > 1000 rows."""
    ev = db.assert_scalar("SELECT COUNT(*) FROM creature_template", ">", 1000, "world")
    require(ev["ok"], "world DB not populated", **ev)
    return ev


@check("sql.assert_selftest", order=71, capability="harness-control", stack="up", tier="quick")
def assert_selftest(ctx):
    """A deliberately false assertion reports FAIL with expected and actual."""
    ev = db.assert_scalar("SELECT COUNT(*) FROM creature_template", "<", 0, "world")
    require(ev["ok"] is False, "false assertion reported ok", **ev)
    require(ev["actual"] not in (None, "") and ev["expected"] == 0, "missing expected/actual in evidence", **ev)
    return {"selftest_assertion": ev, "behaviour": "false assertion correctly reported as failing"}


@check("client.realmlist", order=80, capability="client-connectivity", stack="up", tier="quick")
def realmlist(ctx):
    """Exactly one realm at 127.0.0.1:8085, gamebuild 12340."""
    rows = db.query("SELECT id, name, address, port, gamebuild FROM realmlist", "auth")
    ev = {"rows": rows}
    require(len(rows) == 1, "expected exactly one realm", **ev)
    _, _, addr, port, build = rows[0]
    require(addr == P.HOST and port == str(P.WORLD_PORT) and build == "12340",
            f"realm not {P.HOST}:{P.WORLD_PORT}/12340", **ev)
    return ev
