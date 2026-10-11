"""client-connectivity: offline checks of `hs client-config` against a mock client tree."""
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

from .. import paths as P
from ..registry import check, require

STALE_CONFIG = (
    'SET locale "enUS"\r\n'
    'SET realmlist "logon.example.com"\r\n'
    'SET gxResolution "1920x1080"\r\n'
    'SET realmName "Some Old Realm"\r\n'
    'SET accountName "someone"\r\n'
    'SET SoundOutputDevice "Primary Sound Driver"\r\n'
)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _run(client: Path) -> tuple[subprocess.CompletedProcess, dict | None]:
    r = subprocess.run([str(P.HARNESS / "hs"), "client-config", str(client)],
                       capture_output=True, text=True, timeout=60)
    try:
        out = json.loads(r.stdout)
    except ValueError:
        out = None
    return r, out


def _set_lines(text: str) -> dict[str, list[str]]:
    keys: dict[str, list[str]] = {}
    for line in text.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].upper() == "SET":
            keys.setdefault(parts[1].lower(), []).append(line)
    return keys


# Declared ownership (harness-reporting "Impact selection is fail-safe"): what the checks of this file cover.
# First cut generated from the product paths they assert on and the harness modules this file imports directly
# (test-pyramid design 13.1); maintained by hand from here on. Shared infrastructure falls to the smoke set.
COVERS = (
    "patches/client/**",
    "harness/hslib/checks/client_checks.py",
)


@check("client.config_gen", order=200, capability="client-connectivity", stack="any", layer="unit", parallel_safe=True)
def config_gen(ctx):
    """`hs client-config` on a mock 3.3.5a tree writes realmlist/patchlist, keeps other keys, leaves Wow.exe intact."""
    with tempfile.TemporaryDirectory(prefix="hs-mockclient-") as td:
        client = Path(td)
        wow = client / "Wow.exe"
        wow.write_bytes(os.urandom(4096))
        (client / "Data" / "enUS").mkdir(parents=True)
        mpq = client / "Data" / "common.MPQ"
        mpq.write_bytes(os.urandom(1024))
        (client / "WTF").mkdir()
        cfg = client / "WTF" / "Config.wtf"
        cfg.write_bytes(STALE_CONFIG.encode())
        wow_sha, mpq_sha = _sha(wow), _sha(mpq)
        tree_before = sorted(str(p.relative_to(client)) for p in client.rglob("*"))

        r1, out1 = _run(client)
        ev = {"cmd": f"harness/hs client-config {client}", "exit": r1.returncode,
              "stdout": r1.stdout[-3000:], "stderr": r1.stderr[-1000:],
              "wow_exe_sha256_before": wow_sha}
        require(r1.returncode == 0 and out1 is not None, "client-config failed or printed non-JSON", **ev)

        rl = client / "Data" / "enUS" / "realmlist.wtf"
        require(rl.is_file(), "Data/enUS/realmlist.wtf not written", **ev)
        rl_text = rl.read_text()
        ev["realmlist_wtf"] = rl_text
        require(rl_text.strip() == "set realmlist 127.0.0.1", "realmlist.wtf content wrong", **ev)

        cfg_bytes = cfg.read_bytes()
        cfg_text = cfg_bytes.decode("latin-1")
        ev["config_wtf_after"] = cfg_text
        keys = _set_lines(cfg_text)
        require(keys.get("realmlist") == ['SET realmlist "127.0.0.1"'],
                'Config.wtf lacks exactly one SET realmlist "127.0.0.1"', **ev)
        require("logon.example.com" not in cfg_text, "stale realmlist survived", **ev)
        require(keys.get("patchlist") == ['SET patchlist "127.0.0.1"'],
                "Config.wtf patchlist not pointed at localhost", **ev)
        # Every other original line must survive verbatim and in the same order.
        others = [l for l in STALE_CONFIG.splitlines() if "realmlist" not in l.lower()]
        after_others = [l for l in cfg_text.splitlines()
                        if not l.lower().startswith(("set realmlist ", "set patchlist "))]
        ev["other_keys_expected"], ev["other_keys_after"] = others, after_others
        require(after_others == others, "other Config.wtf keys not preserved in order", **ev)
        require(cfg_bytes.count(b"\n") == cfg_bytes.count(b"\r\n"), "CRLF line endings not preserved", **ev)
        require((client / "WTF" / "Config.wtf.hs-bak").read_bytes() == STALE_CONFIG.encode(),
                "Config.wtf backup missing or not the original", **ev)

        wow_after = _sha(wow)
        ev["wow_exe_sha256_after"] = wow_after
        ev["cli_reported_wow_exe"] = out1.get("wow_exe")
        require(wow_after == wow_sha, "Wow.exe hash changed", **ev)
        require(_sha(mpq) == mpq_sha, "MPQ placeholder hash changed", **ev)
        require(out1.get("wow_exe", {}).get("sha256_before") == wow_sha == out1["wow_exe"].get("sha256_after"),
                "CLI-reported Wow.exe hashes don't match the placeholder", **ev)

        # Idempotence: a second run changes nothing (bytes identical, backups untouched).
        snapshot = {str(p.relative_to(client)): p.read_bytes() for p in client.rglob("*") if p.is_file()}
        r2, out2 = _run(client)
        ev["second_run"] = {"exit": r2.returncode, "changed_files": (out2 or {}).get("changed_files"),
                            "stderr": r2.stderr[-500:]}
        require(r2.returncode == 0 and out2 is not None, "second run failed", **ev)
        snapshot2 = {str(p.relative_to(client)): p.read_bytes() for p in client.rglob("*") if p.is_file()}
        require(snapshot2 == snapshot, "second run modified the tree (not idempotent)", **ev)
        require(out2.get("changed_files") == [], "second run reported changes", **ev)
        ev["tree_before"] = tree_before
        ev["tree_after"] = sorted(snapshot2)

    # Refusal: a directory that isn't a client must be rejected without writing anything.
    with tempfile.TemporaryDirectory(prefix="hs-notclient-") as td:
        r3, _ = _run(Path(td))
        written = list(Path(td).rglob("*"))
        ev["non_client_dir"] = {"exit": r3.returncode, "stderr_tail": r3.stderr.strip().splitlines()[-1:],
                                "files_written": [str(p) for p in written]}
        require(r3.returncode != 0 and not written, "non-client dir was not refused cleanly", **ev)
    return ev
