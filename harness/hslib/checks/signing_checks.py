"""release-signing (openspec/changes/release-signing): the signing mode setting, the release app's identity and layout,
and its ad-hoc signature.

signing.mode_setting      (quick) a fixture app shaped like the release app (launcher, one payload executable, one
                          dylib) under env/release/: `release.py sign` with the checked-in ad-hoc setting and an
                          environment without any signing or notary variable succeeds and reads no secret name;
                          developer-id without credentials, an unknown mode, a missing mode file and a
                          --signing-config outside env/release/ and harness/out/ each exit nonzero before signing
                          anything (the fixture is byte-identical, no zip, no sign record), naming what they must; and
                          .github/workflows/release.yml follows the setting (mode from signing.json, sign before the
                          no-Homebrew smoke on the signed app, secrets only in developer-id steps, keychain removed in
                          an always() step, notarize/staple/re-zip before the draft, notes from the mode's file).
                          Counterexamples: developer-id with every credential set but outside CI is refused; with one
                          credential set the message names only the others; mutated workflows (sign after the smoke,
                          a secret in an unconditional step, no always() cleanup, fixed notes) are caught.
signing.bundle_layout     (slow) the packaged app's Info.plist (bundle id, executable, no "redistribut" wording) and
                          every file's place (Mach-O only in Contents/MacOS or the payload's code folders; code
                          locations hold only Mach-O; Contents/MacOS only the launcher). Counterexamples on scratch
                          clones: a planted Mach-O under harness/ and a text file in Contents/MacOS.
signing.adhoc_signature   (slow) the packaged app: the bundle and every Mach-O verify (--strict; bundle --deep); every
                          file we signed is adhoc with the runtime flag and its role's exact entitlements (XML, not
                          bplist00); libraries carry none; Oracle's files keep Oracle's Developer ID authority and the
                          cdhash recorded at packaging; the bundle's designated requirement is the pinned one.
                          Counterexample: a scratch clone with one byte flipped in server/lib/libzstd.1.dylib fails.
signing.entitlements_needed (slow) each key in tools/release/entitlements/*.plist: a clone re-signed without it fails
                          the flow design.md 3 names (the release smoke's server --version steps, "different Team
                          IDs"); the packaged app passes the same smoke.
signing.gatekeeper_adhoc  (slow) spctl on a quarantined clone: rejected, no notarized/Developer ID source (text kept).
                          Counterexample: the same test on an Apple-signed system app fails.
signing.stable_identity   (slow) the packaged app and a clone with a changed launcher byte and VERSION, re-signed by
                          sign.py, both satisfy the first's identifier-only requirement; another identifier doesn't.
signing.seal_survives_use (slow, order 18: after the first-launch fixture's users) the fixture app still verifies and
                          equals packaging's bundle-files.json. Counterexample: a __pycache__ in a clone fails.
signing.open_instructions (quick) setup.md / release notes wording, and the documented xattr one-liner on a
                          quarantined ad-hoc fixture. Counterexamples: notes without "Open Anyway"; xattr without -r.
signing.notarized         (slow, CI-only) harness/hslib/notarycheck.py over the release workflow's records; outside CI
                          only its self-test runs and the check reports not applicable (runner.CI_ONLY_SKIP_MSG).
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .. import paths as P
from ..registry import check, require

CAP = "release-signing"
TOOLS = P.ROOT / "tools" / "release"
RELEASE_PY = TOOLS / "release.py"
SIGNING_JSON = TOOLS / "signing.json"
ENTITLEMENTS = TOOLS / "entitlements"
WORKFLOW = P.ROOT / ".github" / "workflows" / "release.yml"
WORK = P.ENV / "release"
APP = WORK / "payload" / "Moonglade.app"
BUILD = WORK / "build"
PAYLOAD_REL = Path("Contents") / "Resources" / "Moonglade"
# the spec's literal expectations (not sign.py's constants, so a wrong constant there fails)
EXPECT_ID = "io.github.zttimmerman.moonglade"
EXPECT_DR = 'designated => identifier "io.github.zttimmerman.moonglade"'
DEVID_NAMES = ("MOONGLADE_DEVID_IDENTITY", "MOONGLADE_TEAM_ID", "MOONGLADE_NOTARY_KEY_P8_BASE64",
               "MOONGLADE_NOTARY_KEY_ID", "MOONGLADE_NOTARY_ISSUER_ID")
WORKFLOW_SECRETS = ("MOONGLADE_DEVID_P12_BASE64", "MOONGLADE_DEVID_P12_PASSWORD") + DEVID_NAMES
ORACLE_AUTHORITY = "Developer ID Application: Oracle America, Inc. (VB5E2TV963)"
ROLE_OF = {"Contents/MacOS/Moonglade": "launcher", "bin/Moonglade": "launcher", "python/bin/python3.13": "python",
           "server/bin/authserver": "server", "server/bin/worldserver": "server"}
CODE_DIRS = ("bin", "server/bin", "server/lib", "mysql/bin", "mysql/lib", "python/bin", "python/lib")
CODE_LOCATIONS = ("Contents/MacOS", "Contents/Frameworks", "Contents/Helpers", "Contents/PlugIns")
MACHO_MAGIC = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}
SIGN_TIMEOUT = 300


def _scratch(tag: str) -> Path:
    d = WORK / f"check-signing-{tag}-{os.getpid()}"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    return d


def _is_macho(p: Path) -> bool:
    if p.is_symlink() or not p.is_file():
        return False
    with open(p, "rb") as fh:
        return fh.read(4) in MACHO_MAGIC


def _tree_fp(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        r = str(p.relative_to(root))
        if p.is_symlink():
            out[r] = "-> " + os.readlink(p)
        elif p.is_file():
            out[r] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _clean_env(**extra) -> dict:
    """No signing or notary variable at all (and none of the harness's test switches)."""
    keep = {k: v for k, v in os.environ.items()
            if not k.startswith(("MOONGLADE_", "HS_TEST_", "GITHUB_", "RUNNER_", "APPLE_", "NOTARY", "CODESIGN"))}
    keep.update(extra)
    return keep


# ================================================================================================ fixture

def _cc(src: str, out: Path, *args: str) -> None:
    c = out.with_suffix(".c")
    c.write_text(src)
    r = subprocess.run(["/usr/bin/clang", "-O0", *args, str(c), "-o", str(out)], capture_output=True, text=True)
    c.unlink()
    if r.returncode != 0:
        raise RuntimeError(f"compiling the fixture {out.name} failed: {r.stderr[-400:]}")


def fixture_app(work: Path) -> Path:
    """<work>/payload/Moonglade.app shaped like the release app: Contents/MacOS/Moonglade (a tiny launcher stand-in,
    never run), Info.plist, the payload with bin/Moonglade, server/bin/worldserver and server/lib/libfixture.dylib,
    plus VERSION. Linker-signed only, as compilers leave them."""
    app = work / "payload" / "Moonglade.app"
    pl = app / PAYLOAD_REL
    for d in (app / "Contents/MacOS", pl / "bin", pl / "server/bin", pl / "server/lib"):
        d.mkdir(parents=True, exist_ok=True)
    _cc("int main(void){return 0;}\n", app / "Contents/MacOS/Moonglade")
    shutil.copy2(app / "Contents/MacOS/Moonglade", pl / "bin/Moonglade")
    _cc("int main(void){return 0;}\n", pl / "server/bin/worldserver")
    _cc("int fixture(void){return 1;}\n", pl / "server/lib/libfixture.dylib", "-dynamiclib",
        "-install_name", "@rpath/libfixture.dylib")
    with open(app / "Contents/Info.plist", "wb") as f:
        plistlib.dump({"CFBundleExecutable": "Moonglade", "CFBundleIdentifier": EXPECT_ID, "CFBundleName": "Moonglade",
                       "CFBundlePackageType": "APPL", "CFBundleInfoDictionaryVersion": "6.0"}, f)
    (pl / "VERSION").write_text('{"version": "fixture"}\n')
    return app


def run_sign(work: Path, env: dict, config: Path | None = None) -> dict:
    rec = work / "record.json"
    rec.unlink(missing_ok=True)
    args = [sys.executable, "-I", str(RELEASE_PY), "sign", "--work", str(work), "--ignore-power", "--record", str(rec)]
    if config is not None:
        args += ["--signing-config", str(config)]
    r = subprocess.run(args, capture_output=True, text=True, env=env, cwd=P.ROOT, timeout=SIGN_TIMEOUT)
    sj = work / "build" / "sign.json"
    try:
        sign_rec = json.loads(sj.read_text()) if sj.is_file() else None
    except ValueError:
        sign_rec = "unreadable"
    return {"rc": r.returncode, "stderr": r.stderr[-1500:], "stdout": r.stdout[-400:], "sign": sign_rec}


def refused_untouched(work: Path, app: Path, fp0: dict, r: dict) -> list[str]:
    """Problems if a refused sign step changed anything: nonzero exit, fixture identical, no zip, no sign record."""
    probs = []
    if r["rc"] == 0:
        probs.append("exited 0")
    if _tree_fp(app) != fp0:
        probs.append("the fixture app changed")
    zips = [str(p) for p in work.rglob("*.zip")]
    if zips:
        probs.append(f"zips exist: {zips}")
    if r["sign"] is not None:
        probs.append("a sign record was written")
    return probs


# ================================================================================================ workflow parsing

def workflow_steps(text: str) -> list[dict]:
    """The job's steps as {name, if, body} (our own workflow's shape: `      - name:` items under `    steps:`)."""
    lines = text.splitlines()
    try:
        i = next(n for n, l in enumerate(lines) if l.rstrip() == "    steps:")
    except StopIteration:
        return []
    steps, cur = [], None
    for l in lines[i + 1:]:
        if l.startswith("      - "):
            cur = {"lines": [l[8:]]}
            steps.append(cur)
        elif cur is not None and (l.startswith("        ") or not l.strip()):
            cur["lines"].append(l[8:])
        elif l.strip():
            break
    for s in steps:
        body = "\n".join(s["lines"])
        s["body"] = body
        m = re.search(r"^name:\s*(.+)$", body, re.M)
        s["name"] = m.group(1).strip() if m else body.split("\n", 1)[0]
        m = re.search(r"^if:\s*(.+)$", body, re.M)
        s["if"] = m.group(1).strip() if m else ""
        m = re.search(r"^id:\s*(\S+)$", body, re.M)
        s["id"] = m.group(1) if m else None
    return steps


def workflow_problems(text: str) -> tuple[list[str], dict]:
    steps = workflow_steps(text)
    ev = {"steps": [s["name"] for s in steps]}
    probs: list[str] = []

    def idx(pred, what):
        hits = [n for n, s in enumerate(steps) if pred(s)]
        if not hits:
            probs.append(f"no step that {what}")
            return None
        return hits[0]
    devid = re.compile(r"steps\.mode\.outputs\.mode\s*==\s*'developer-id'")
    mode_i = idx(lambda s: "tools/release/signing.json" in s["body"] and s["id"] == "mode",
                 "reads the mode from tools/release/signing.json (id: mode)")
    secret_i = idx(lambda s: devid.search(s["if"]) and all(n in s["body"] for n in WORKFLOW_SECRETS)
                   and "exit 1" in s["body"], "checks every Developer ID secret in developer-id mode")
    pkg_i = idx(lambda s: "package.sh" in s["body"] and " build " in f" {s['body']} ", "builds the app (package.sh build)")
    sign_is = [n for n, s in enumerate(steps) if re.search(r"package\.sh\s+sign\b", s["body"])]
    smoke_i = idx(lambda s: "smoke.py" in s["body"], "runs the no-Homebrew smoke")
    aside_i = idx(lambda s: "mv /opt/homebrew /opt/homebrew.aside" in s["body"], "moves Homebrew aside")
    keychain_i = idx(lambda s: "security import" in s["body"] and "create-keychain" in s["body"],
                     "imports the certificate into a temporary keychain")
    cleanup_i = idx(lambda s: "delete-keychain" in s["body"] and "always()" in s["if"],
                    "deletes the temporary keychain in an always() step")
    notarize_i = idx(lambda s: re.search(r"package\.sh\s+notarize\b", s["body"]),
                     "notarizes, staples and re-zips (package.sh notarize)")
    spctl_i = idx(lambda s: "spctl --assess" in s["body"] and "ditto -x" in s["body"],
                  "assesses a fresh unzip with spctl")
    draft_i = idx(lambda s: "gh release create" in s["body"], "creates the draft release")
    ncheck_i = idx(lambda s: "harness/hslib/notarycheck.py" in s["body"],
                   "runs signing.notarized's judgement (harness/hslib/notarycheck.py)")
    if not sign_is:
        probs.append("no sign step (package.sh sign)")
    if any(re.search(r"package\.sh[^\n]*\ball\b", s["body"]) for s in steps):
        probs.append("a step runs package.sh all (signing must happen between packaging and the smoke)")
    if None not in (mode_i, smoke_i, pkg_i, aside_i) and sign_is:
        if not all(pkg_i < n < smoke_i for n in sign_is):
            probs.append("a sign step isn't between packaging and the smoke")
        if not all(n < aside_i for n in sign_is):
            probs.append("signing happens after Homebrew is moved aside")
    if smoke_i is not None and "env/release/payload/Moonglade.app/Contents/Resources/Moonglade" not in steps[smoke_i]["body"]:
        probs.append("the smoke doesn't run the packaged (signed) app's payload")
    if None not in (mode_i, secret_i, pkg_i) and not (mode_i < secret_i < pkg_i):
        probs.append("the secret check doesn't run after reading the mode and before building")
    if None not in (notarize_i, draft_i, spctl_i) and not (notarize_i < spctl_i < draft_i):
        probs.append("notarize -> spctl -> draft order is wrong")
    if None not in (notarize_i, smoke_i) and not smoke_i < notarize_i:
        probs.append("notarization runs before the smoke on the signed app")
    for n, s in enumerate(steps):
        if "secrets." in s["body"] and not devid.search(s["if"]):
            probs.append(f"step '{s['name']}' references a secret but isn't conditional on developer-id")
    if None not in (spctl_i, ncheck_i, draft_i) and not (spctl_i < ncheck_i < draft_i):
        probs.append("signing.notarized's step doesn't run after the spctl assessment and before the draft")
    for n in (keychain_i, notarize_i, spctl_i, ncheck_i):
        if n is not None and not devid.search(steps[n]["if"]):
            probs.append(f"step '{steps[n]['name']}' isn't conditional on developer-id")
    devid_signs = [n for n in sign_is if devid.search(steps[n]["if"])]
    if keychain_i is not None and devid_signs and not keychain_i < min(devid_signs):
        probs.append("the Developer ID sign step runs before the keychain import")
    if cleanup_i is not None and not devid.search(steps[cleanup_i]["if"]):
        probs.append("the keychain cleanup step isn't limited to developer-id")
    if draft_i is not None:
        body = steps[draft_i]["body"]
        if not re.search(r"--notes-file\s+\"?tools/release/notes/\$\{\w+\}\.md", body) \
                or "steps.mode.outputs.mode" not in body:
            probs.append("the draft's notes don't come from tools/release/notes/<mode>.md")
    return probs, ev


def workflow_mutations(text: str) -> dict[str, str]:
    """Counterexamples: each must make workflow_problems() report something."""
    steps = workflow_steps(text)
    out = {}
    # 1. the ad-hoc sign step moved after the smoke
    sign = next(s for s in steps if re.search(r"package\.sh\s+sign\b", s["body"]) and "ad-hoc" in s["if"])
    smoke = next(s for s in steps if "smoke.py" in s["body"])
    blk = "\n".join("      - " + l if i == 0 else ("        " + l if l else "") for i, l in enumerate(sign["lines"]))
    smk = "\n".join("      - " + l if i == 0 else ("        " + l if l else "") for i, l in enumerate(smoke["lines"]))
    if blk in text and smk in text:
        t = text.replace(blk + "\n", "", 1)
        out["sign_after_smoke"] = t.replace(smk, smk + "\n" + blk, 1)
    # 2. a secret in an unconditional step
    out["secret_unconditional"] = text.replace(
        "      - name: Install build dependencies (runner only)\n",
        "      - name: Install build dependencies (runner only)\n        env:\n"
        "          LEAK: ${{ secrets.MOONGLADE_TEAM_ID }}\n", 1)
    # 3. keychain cleanup not always()
    out["cleanup_not_always"] = text.replace("${{ always() && steps.mode.outputs.mode == 'developer-id' }}",
                                             "${{ steps.mode.outputs.mode == 'developer-id' }}", 1)
    # 5. signing.notarized's step dropped (the draft would be created without the judgement)
    nc = next((s for s in steps if "harness/hslib/notarycheck.py" in s["body"]), None)
    if nc is not None:
        ncb = "\n".join("      - " + l if i == 0 else ("        " + l if l else "") for i, l in enumerate(nc["lines"]))
        if ncb in text:
            out["no_notarized_check"] = text.replace(ncb + "\n", "", 1)
    # 4. fixed release notes
    out["fixed_notes"] = re.sub(r'--notes-file "tools/release/notes/\$\{SIGNING_MODE\}\.md"',
                                '--notes "Prebuilt Moonglade."', text, count=1)
    return out


# ================================================================================================ mode_setting

@check("signing.mode_setting", order=13, capability=CAP, stack="any", tier="quick")
def mode_setting(ctx):
    """The sign step follows tools/release/signing.json: ad-hoc signs a fixture with no secrets; developer-id without
    credentials, unknown/missing modes and a config outside the allowed folders are refused before signing; the
    release workflow follows the setting."""
    ev: dict = {"started": time.time()}
    d = _scratch("mode")
    try:
        # --- checked-in setting (ad-hoc), no signing/notary variables at all
        checked_in = json.loads(SIGNING_JSON.read_text())
        ev["checked_in"] = checked_in
        require(checked_in == {"mode": "ad-hoc"}, "tools/release/signing.json isn't the one-line ad-hoc setting", **ev)
        w = d / "adhoc"
        app = fixture_app(w)
        env = _clean_env()
        ev["env_signing_keys"] = sorted(k for k in env if k.startswith("MOONGLADE_"))
        r = run_sign(w, env)
        s = r["sign"] if isinstance(r["sign"], dict) else {}
        ev["adhoc"] = {"rc": r["rc"], "stderr": r["stderr"][-600:], "mode": s.get("mode"),
                       "env_read": s.get("env_read"), "bundle": s.get("bundle"),
                       "signed": [(x["path"], x["signature"], x["flags"]) for x in s.get("signed", [])]}
        require(r["rc"] == 0 and s.get("mode") == "ad-hoc", "ad-hoc signing of the fixture failed", **ev)
        require(s.get("env_read") == [], "the ad-hoc sign step read environment keys", **ev)
        require(not any(n in json.dumps(s) for n in WORKFLOW_SECRETS), "a credential name appears in the record", **ev)
        v = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], capture_output=True,
                           text=True)
        ev["adhoc"]["verify"] = {"rc": v.returncode, "out": v.stderr.strip()[-300:]}
        require(v.returncode == 0, "the ad-hoc-signed fixture doesn't verify", **ev)
        require(len(s.get("signed", [])) == 4 and all(x["signature"] == "adhoc" and "runtime" in (x["flags"] or "")
                                                      for x in s["signed"]),
                "not every fixture file is ad-hoc signed with the hardened runtime", **ev)

        # --- refusals: each on a fresh fixture, which must stay byte-identical
        def refusal(tag: str, cfg: dict | None, env_extra: dict, cfg_path: Path | None = None,
                    write_cfg: bool = True) -> dict:
            wd = d / tag
            a = fixture_app(wd)
            fp0 = _tree_fp(a)
            cp = cfg_path or (wd / "signing.json")
            if cfg is not None and write_cfg:
                cp.write_text(json.dumps(cfg) + "\n")
            rr = run_sign(wd, _clean_env(**env_extra), cp)
            return {"rc": rr["rc"], "stderr": rr["stderr"][-700:], "problems": refused_untouched(wd, a, fp0, rr),
                    "config": str(cp)}
        ev["devid_no_creds"] = rr = refusal("devid", {"mode": "developer-id"}, {})
        require(not rr["problems"], f"developer-id without credentials: {rr['problems']}", **ev)
        missing = [n for n in DEVID_NAMES if n not in rr["stderr"]]
        require(not missing, f"the refusal doesn't name {missing}", **ev)
        # counterexample: one credential present -> named no more (proves names come from the environment)
        ev["devid_one_cred"] = rr = refusal("devid1", {"mode": "developer-id"},
                                            {"MOONGLADE_TEAM_ID": "TEAMFIXTURE"})
        require(not rr["problems"] and "MOONGLADE_TEAM_ID" not in rr["stderr"].split("missing", 1)[-1]
                and all(n in rr["stderr"] for n in DEVID_NAMES if n != "MOONGLADE_TEAM_ID"),
                "with one credential set the refusal should name only the others", **ev)
        require("TEAMFIXTURE" not in rr["stderr"], "a credential value was printed", **ev)
        # counterexample: every credential set (dummy values), but not in the release workflow -> refused
        dummies = {n: f"fixture-value-{i}" for i, n in enumerate(DEVID_NAMES)}
        ev["devid_outside_ci"] = rr = refusal("devidci", {"mode": "developer-id"}, dummies)
        require(not rr["problems"] and "GITHUB_ACTIONS" in rr["stderr"],
                "developer-id outside the release workflow wasn't refused", **ev)
        require(not any(v in rr["stderr"] for v in dummies.values()), "a credential value was printed", **ev)
        # unknown mode, missing file
        for tag, cfg, write in (("typo", {"mode": "adhoc-typo"}, True), ("missing", None, False)):
            ev[tag] = rr = refusal(tag, cfg, {}, write_cfg=write)
            require(not rr["problems"], f"{tag}: {rr['problems']}", **ev)
            require(rr["config"] in rr["stderr"] and '"ad-hoc"' in rr["stderr"] and '"developer-id"' in rr["stderr"],
                    f"{tag}: the message doesn't name the file and the allowed modes", **ev)
        # a config outside env/release/ and harness/out/ is refused (never read)
        ev["outside"] = rr = refusal("outside", None, {}, cfg_path=P.ROOT / "docs" / "signing.json", write_cfg=False)
        require(not rr["problems"] and "only a file under env/release/ or harness/out/" in rr["stderr"],
                "--signing-config outside the allowed folders wasn't refused", **ev)

        # --- the workflow follows the setting
        text = WORKFLOW.read_text()
        probs, wev = workflow_problems(text)
        ev["workflow"] = {"problems": probs, **wev}
        require(not probs, f"release.yml: {probs}", **ev)
        caught = {}
        for name, mutated in workflow_mutations(text).items():
            caught[name] = workflow_problems(mutated)[0][:3] if mutated != text else ["(mutation didn't apply)"]
        ev["workflow"]["counterexamples"] = caught
        require(len(caught) == 5 and all(v and v != ["(mutation didn't apply)"] for v in caught.values()),
                "a mutated workflow wasn't caught", **ev)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return ev


# ================================================================================================ packaged app

def _need_signed(ctx) -> dict:
    from .release_checks import _need_payload, _packaged
    ev = _need_payload(ctx)
    rec = _packaged(ctx)
    sj = BUILD / "sign.json"
    require(sj.is_file() and sj.stat().st_mtime >= rec["started"], "no sign record from this run's packaging",
            sign_json=str(sj), **ev)
    ev["sign_record"] = str(sj)
    return ev


def layout_problems(app: Path) -> tuple[list[str], dict]:
    probs, info = [], {}
    with open(app / "Contents/Info.plist", "rb") as f:
        pl = plistlib.load(f)
    info["plist"] = {k: pl.get(k) for k in ("CFBundleIdentifier", "CFBundleExecutable", "CFBundleIconFile")}
    if pl.get("CFBundleIdentifier") != EXPECT_ID:
        probs.append(f"CFBundleIdentifier {pl.get('CFBundleIdentifier')}")
    if pl.get("CFBundleExecutable") != "Moonglade" or not _is_macho(app / "Contents/MacOS/Moonglade"):
        probs.append("CFBundleExecutable isn't Moonglade under Contents/MacOS")
    words = [f"{k}={v}" for k, v in pl.items() if re.search(r"(?i)redistribut", f"{k} {v}")]
    if words:
        probs.append(f"Info.plist redistribution wording: {words}")
    machos = []
    for p in sorted(app.rglob("*")):
        rel = str(p.relative_to(app))
        if _is_macho(p):
            machos.append(rel)
            if rel == "Contents/MacOS/Moonglade":
                continue
            try:
                prel = str(p.relative_to(app / PAYLOAD_REL))
            except ValueError:
                prel = None
            if prel is None or not any(prel.startswith(c + "/") for c in CODE_DIRS):
                probs.append(f"Mach-O outside the allowed places: {rel}")
        elif any(rel.startswith(c + "/") for c in CODE_LOCATIONS) and not p.is_dir():
            probs.append(f"non-Mach-O in a code location: {rel}")
    mac = sorted(x.name for x in (app / "Contents/MacOS").iterdir())
    if mac != ["Moonglade"]:
        probs.append(f"Contents/MacOS holds {mac}")
    info["macho_files"] = len(machos)
    return probs, info


def _clone(app: Path, d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    dst = d / "Moonglade.app"
    subprocess.run(["/bin/cp", "-c", "-R", str(app), str(dst)], check=True)
    return dst


@check("signing.bundle_layout", order=13, capability=CAP, stack="any", tier="slow")
def bundle_layout(ctx):
    """Bundle id io.github.zttimmerman.moonglade, executable Moonglade, every Mach-O in an allowed place, code
    locations Mach-O only, no redistribution wording; planted counterexamples fail."""
    ev = _need_signed(ctx)
    probs, info = layout_problems(APP)
    ev.update(info)
    ev["problems"] = probs
    require(not probs, f"layout: {probs}", **ev)
    d = _scratch("layout")
    try:
        caught = {}
        c1 = _clone(APP, d / "a")
        shutil.copy2(APP / PAYLOAD_REL / "server/lib/libzstd.1.dylib", c1 / PAYLOAD_REL / "harness/x.dylib")
        caught["macho_in_harness"] = layout_problems(c1)[0]
        c2 = _clone(APP, d / "b")
        (c2 / "Contents/MacOS/notes.txt").write_text("not code\n")
        caught["text_in_macos"] = layout_problems(c2)[0]
        ev["counterexamples"] = caught
        require(any("harness/x.dylib" in x for x in caught["macho_in_harness"])
                and any("notes.txt" in x for x in caught["text_in_macos"]), "a planted counterexample passed", **ev)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return ev


def _cs(args: list[str], p: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["/usr/bin/codesign", *args, str(p)], capture_output=True)


def _display(p: Path) -> dict:
    r = _cs(["-dvvv", "-r-"], p)
    out = (r.stderr + r.stdout).decode(errors="replace")
    info: dict = {"authority": []}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        if k in ("Identifier", "CDHash", "Signature", "TeamIdentifier"):
            info[k] = v
        elif k == "Authority":
            info["authority"].append(v)
        elif k == "CodeDirectory v":
            m = re.search(r"flags=(\S+)", v)
            info["flags"] = m.group(1) if m else None
        elif line.startswith("designated => "):
            info["designated"] = line
    return info


def _role_file(role: str) -> dict:
    f = ENTITLEMENTS / f"{role}.plist"
    return plistlib.loads(f.read_bytes()) if f.is_file() else {}


def signature_problems(app: Path, oracle: dict) -> tuple[list[str], dict]:
    probs: list[str] = []
    files = {}
    v = _cs(["--verify", "--deep", "--strict", "--verbose=2"], app)
    if v.returncode != 0:
        probs.append(f"bundle: --verify --deep --strict rc {v.returncode}: {v.stderr.decode(errors='replace')[-300:]}")
    b = _display(app)
    if b.get("designated") != EXPECT_DR:
        probs.append(f"bundle designated requirement {b.get('designated')!r}")
    if b.get("Identifier") != EXPECT_ID or b.get("Signature") != "adhoc":
        probs.append(f"bundle identifier/signature {b.get('Identifier')} {b.get('Signature')}")
    for p in sorted(app.rglob("*")):
        if not _is_macho(p):
            continue
        rel = str(p.relative_to(app))
        prel = str(p.relative_to(app / PAYLOAD_REL)) if (app / PAYLOAD_REL) in p.parents else None
        vr = _cs(["--verify", "--strict"], p)
        info = _display(p)
        ent = _cs(["-d", "--entitlements", "-", "--xml"], p).stdout.strip()
        ents = plistlib.loads(ent) if ent else {}
        row = {"verify": vr.returncode, "identifier": info.get("Identifier"), "cdhash": info.get("CDHash"),
               "flags": info.get("flags"), "signature": info.get("Signature"), "entitlements": sorted(ents)}
        files[rel] = row
        if vr.returncode != 0:
            probs.append(f"{rel}: --verify --strict rc {vr.returncode}: {vr.stderr.decode(errors='replace')[-200:]}")
        if prel is not None and prel in oracle:
            if ORACLE_AUTHORITY not in info["authority"] or info.get("CDHash") != oracle[prel]["cdhash"]:
                probs.append(f"{rel}: Oracle's signature changed ({info['authority'][:1]}, {info.get('CDHash')} vs "
                             f"{oracle[prel]['cdhash']})")
            continue
        if info.get("Signature") != "adhoc" or "runtime" not in (info.get("flags") or ""):
            probs.append(f"{rel}: not adhoc+runtime ({info.get('Signature')}, {info.get('flags')})")
        if ent.startswith(b"bplist00"):
            probs.append(f"{rel}: entitlements are a binary plist")
        role = ROLE_OF.get(rel) or (ROLE_OF.get(prel) if prel else None)
        want = _role_file(role) if role else {}
        if ents != want:
            probs.append(f"{rel}: entitlements {sorted(ents)} != {role or 'library'} role's {sorted(want)}")
    missing_oracle = [k for k in oracle if f"{PAYLOAD_REL}/{k}" not in files]
    if missing_oracle:
        probs.append(f"recorded Oracle files missing: {missing_oracle}")
    return probs, {"bundle": b, "files": files}


@check("signing.adhoc_signature", order=13, capability=CAP, stack="any", tier="slow")
def adhoc_signature(ctx):
    """Every signature in the packaged ad-hoc app verifies, with the runtime flag and the role's exact entitlements;
    Oracle's files keep Oracle's signature and recorded cdhash; the bundle's requirement is pinned; a flipped byte in
    a scratch clone's libzstd fails."""
    ev = _need_signed(ctx)
    oracle = json.loads((BUILD / "oracle.json").read_text())
    ev["oracle_recorded"] = {k: v["cdhash"] for k, v in oracle.items()}
    require(len(oracle) >= 7, "fewer Oracle files recorded than shipped (4 programs + 3 libraries)", **ev)
    probs, info = signature_problems(APP, oracle)
    ev["bundle"] = info["bundle"]
    ev["files"] = info["files"]
    ev["problems"] = probs
    require(not probs, f"signatures: {probs[:8]}", **ev)
    d = _scratch("flip")
    try:
        c = _clone(APP, d)
        lib = c / PAYLOAD_REL / "server/lib/libzstd.1.dylib"
        data = bytearray(lib.read_bytes())
        data[len(data) // 2] ^= 0x01
        lib.unlink()                      # a clone shares blocks: write a new file, never into the shared one
        lib.write_bytes(bytes(data))
        vr = _cs(["--verify", "--strict"], lib)
        ev["counterexample_flip"] = {"rc": vr.returncode, "out": vr.stderr.decode(errors="replace")[-300:]}
        require(vr.returncode != 0, "a flipped byte in libzstd still verifies", **ev)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return ev


# ================================================================================================ entitlements_needed

# Each entitlement a role file carries, with the flow its design.md 3 "Measured" entry names and how that flow fails
# without it. A key in a role file with no entry here fails the check (it must be measured and recorded first).
ENTITLEMENT_FLOWS = {
    ("server", "com.apple.security.cs.disable-library-validation"): {
        "flow": "release smoke (tools/release/smoke.py --sandbox), its worldserver/authserver --version steps",
        "failing_steps": ("worldserver --version", "authserver --version"),
        "error": "different Team IDs",
    },
}
DESIGN_MD = P.ROOT / "openspec" / "changes" / "release-signing" / "design.md"


def _design_text() -> str:
    """design.md of the change, or the archived copy once the change is archived."""
    if DESIGN_MD.is_file():
        return DESIGN_MD.read_text()
    hits = sorted((P.ROOT / "openspec" / "changes" / "archive").glob("*-release-signing/design.md"))
    return hits[-1].read_text() if hits else ""


def _role_paths(app: Path, role: str) -> list[Path]:
    out = []
    for rel, r in ROLE_OF.items():
        if r != role:
            continue
        p = app / rel if rel.startswith("Contents/") else app / PAYLOAD_REL / rel
        if p.is_file():
            out.append(p)
    return out


def _resign(p: Path, ents: dict, d: Path) -> dict:
    """Re-sign one executable ad hoc with the hardened runtime, its own identifier and exactly `ents`."""
    ident = _display(p).get("Identifier")
    cmd = ["/usr/bin/codesign", "--force", "--sign", "-", "--options", "runtime", "--timestamp=none",
           "--identifier", ident]
    if ents:
        f = d / f"ents-{p.name}.plist"
        f.write_bytes(plistlib.dumps(ents, fmt=plistlib.FMT_XML))
        cmd += ["--entitlements", str(f)]
    r = subprocess.run([*cmd, str(p)], capture_output=True, text=True)
    return {"file": p.name, "identifier": ident, "entitlements": sorted(ents), "rc": r.returncode,
            "err": r.stderr.strip()[-300:]}


def run_smoke(app: Path, d: Path, tag: str) -> dict:
    """tools/release/smoke.py --sandbox on `app`'s payload (the release workflow's smoke, with this app's Python)."""
    js = d / f"smoke-{tag}.json"
    pl = app / PAYLOAD_REL
    r = subprocess.run([str(pl / "python/bin/python3"), "-I", str(TOOLS / "smoke.py"), "--payload", str(pl),
                        "--scratch", str(d / f"smoke-{tag}"), "--sandbox", "--json", str(js)],
                       capture_output=True, text=True, cwd=d, timeout=900, env=_clean_env())
    try:
        rec = json.loads(js.read_text())
    except (OSError, ValueError):
        rec = {}
    steps = [st for loc in rec.get("locations") or [] for st in loc.get("steps", [])]
    return {"rc": r.returncode, "ok": rec.get("ok"), "failed": rec.get("failed"), "out": r.stdout[-300:],
            "steps": [{k: s.get(k) for k in ("step", "rc", "stderr_tail")} for s in steps
                      if "version" in str(s.get("step"))]}


@check("signing.entitlements_needed", order=13, capability=CAP, stack="any", tier="slow")
def entitlements_needed(ctx):
    """For every key in tools/release/entitlements/*.plist, a scratch clone of this run's packaged app has that
    role's executables re-signed without it and the flow its design.md entry names (the release smoke) runs from the
    clone: it fails in the named way, while the same flow passes on the packaged app with the full set. With no keys
    at all, no signed executable may carry any."""
    ev = _need_signed(ctx)
    files = {f.stem: plistlib.loads(f.read_bytes()) for f in sorted(ENTITLEMENTS.glob("*.plist"))}
    ev["entitlement_files"] = {k: sorted(v) for k, v in files.items()}
    keys = [(role, k) for role, ents in files.items() for k in ents]
    if not keys:
        probs, info = signature_problems(APP, json.loads((BUILD / "oracle.json").read_text()))
        carrying = {k: v["entitlements"] for k, v in info["files"].items() if v["entitlements"]}
        ev["carrying"] = carrying
        require(not carrying, "no role has entitlements, yet signed files carry some", **ev)
        return ev
    unknown = [f"{r}:{k}" for r, k in keys if (r, k) not in ENTITLEMENT_FLOWS]
    require(not unknown, f"entitlements with no measured flow (design.md 3 + ENTITLEMENT_FLOWS): {unknown}", **ev)
    design = _design_text()
    undocumented = [f"{r}:{k}" for r, k in keys if not re.search(rf"`{r}` role → `{re.escape(k)}`", design)]
    require(not undocumented, f"entitlements without a design.md 3 'Measured' entry: {undocumented}", **ev)
    d = _scratch("ents")
    try:
        full = run_smoke(APP, d, "full")
        ev["full_set"] = full
        require(full["ok"] is True and full["rc"] == 0, "the release smoke fails with the full entitlement set", **ev)
        ev["without"] = {}
        for role, key in keys:
            spec = ENTITLEMENT_FLOWS[(role, key)]
            c = _clone(APP, d / f"c-{role}")
            ents = {k: v for k, v in files[role].items() if k != key}
            resigned = []
            for p in _role_paths(c, role):
                tmp = p.with_name(p.name + ".new")              # a clone shares blocks: re-sign a fresh copy
                shutil.copy2(p, tmp)
                tmp.replace(p)
                resigned.append(_resign(p, ents, d))
            now = {p.name: sorted(plistlib.loads(_cs(["-d", "--entitlements", "-", "--xml"], p).stdout.strip() or
                                                 plistlib.dumps({}))) for p in _role_paths(c, role)}
            r = run_smoke(c, d, f"no-{role}")
            failed_steps = [s for s in r["steps"] if s["step"] in spec["failing_steps"] and s["rc"] != 0]
            named = [s for s in failed_steps if spec["error"] in (s.get("stderr_tail") or "")]
            ev["without"][f"{role}:{key}"] = {"flow": spec["flow"], "resigned": resigned, "entitlements_now": now,
                                              "smoke": r, "failed_as_named": len(named)}
            require(all(x["rc"] == 0 for x in resigned) and all(key not in v for v in now.values()),
                    f"couldn't re-sign {role} without {key}", **ev)
            require(r["ok"] is False and named and set(spec["failing_steps"]) <= {s["step"] for s in named},
                    f"without {key} the {role} flow didn't fail as named ({spec['error']})", **ev)
            shutil.rmtree(c.parent, ignore_errors=True)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return ev


# ================================================================================================ gatekeeper_adhoc

QUARANTINE = "com.apple.quarantine"


def _quarantine_value() -> str:
    return f"0083;{int(time.time()):08x};Safari;"


def gatekeeper_verdict(out: str, rc: int) -> dict:
    """What an ad-hoc release must get from `spctl --assess --type execute -vv`: rejected, nonzero, and no
    notarization / Developer ID source."""
    accepted_src = re.search(r"source=(Notarized Developer ID|Developer ID|Apple System|Mac App Store)", out)
    return {"rc": rc, "rejected": "rejected" in out, "accepted_source": accepted_src.group(1) if accepted_src else None,
            "ok": rc != 0 and "rejected" in out and not accepted_src}


@check("signing.gatekeeper_adhoc", order=13, capability=CAP, stack="any", tier="slow")
def gatekeeper_adhoc(ctx):
    """A clonefile copy of this run's packaged ad-hoc app with a com.apple.quarantine attribute (never opened, so no
    dialog): `spctl --assess --type execute -vv` exits nonzero with "rejected" and names no notarization or Developer
    ID source; the exact text is recorded. Counterexample: the same verdict test on an Apple-signed system app
    (accepted) fails."""
    ev = _need_signed(ctx)
    d = _scratch("gk")
    try:
        c = _clone(APP, d)
        x = subprocess.run(["/usr/bin/xattr", "-w", QUARANTINE, _quarantine_value(), str(c)], capture_output=True,
                           text=True)
        q = subprocess.run(["/usr/bin/xattr", "-p", QUARANTINE, str(c)], capture_output=True, text=True)
        ev["quarantine"] = {"set_rc": x.returncode, "value": q.stdout.strip()}
        require(x.returncode == 0 and q.returncode == 0, "couldn't set the quarantine attribute on the copy", **ev)
        r = subprocess.run(["/usr/sbin/spctl", "--assess", "--type", "execute", "-vv", str(c)], capture_output=True,
                           text=True, timeout=120)
        text = (r.stdout + r.stderr).replace(str(d), "<scratch>")
        ev["spctl"] = {"text": text.strip(), **gatekeeper_verdict(text, r.returncode)}
        require(ev["spctl"]["ok"], "Gatekeeper didn't reject the ad-hoc app as expected", **ev)
        sysapp = "/System/Applications/Calculator.app"
        r2 = subprocess.run(["/usr/sbin/spctl", "--assess", "--type", "execute", "-vv", sysapp], capture_output=True,
                            text=True, timeout=120)
        ev["counterexample_apple_app"] = {"app": sysapp, "text": (r2.stdout + r2.stderr).strip(),
                                          **gatekeeper_verdict(r2.stdout + r2.stderr, r2.returncode)}
        require(not ev["counterexample_apple_app"]["ok"], "counterexample: an accepted app passed the ad-hoc test", **ev)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return ev


# ================================================================================================ stable_identity

def _designated(app: Path) -> str | None:
    r = _cs(["-d", "-r-"], app)
    lines = [l.strip() for l in (r.stdout + r.stderr).decode(errors="replace").splitlines()
             if l.strip().startswith("designated => ")]
    return lines[0] if lines else None


def _satisfies(app: Path, requirement: str) -> dict:
    r = subprocess.run(["/usr/bin/codesign", "--verify", f"-R={requirement}", str(app)], capture_output=True,
                       text=True)
    return {"rc": r.returncode, "out": r.stderr.strip()[-300:]}


def _cstring_offset(p: Path) -> int:
    """File offset of the launcher's __TEXT,__cstring section (arm64 slice of a thin file)."""
    out = subprocess.run(["/usr/bin/otool", "-l", str(p)], capture_output=True, text=True).stdout
    m = re.search(r"sectname __cstring\n\s+segname __TEXT\n(?:.*\n){3}\s+offset (\d+)", out)
    if not m:
        raise RuntimeError("no __TEXT,__cstring in the launcher")
    return int(m.group(1))


@check("signing.stable_identity", order=13, capability=CAP, stack="any", tier="slow")
def stable_identity(ctx):
    """Two ad-hoc release apps with different code hashes (this run's packaged app, and a clone with a changed
    launcher byte and a different VERSION re-signed by the sign step) both satisfy the first one's designated
    requirement, which names no code hash; an app signed with another identifier doesn't satisfy it."""
    sys.path.insert(0, str(TOOLS))
    import sign as SG                                            # noqa: E402  (tools/release/sign.py)
    ev = _need_signed(ctx)
    dr1 = _designated(APP)
    ev["requirement"] = dr1
    require(dr1 == EXPECT_DR and "cdhash" not in dr1 and "H\"" not in dr1,
            "the packaged app's designated requirement isn't the identifier-only one", **ev)
    req = dr1.split("designated => ", 1)[1]
    d = _scratch("ident")
    try:
        app2 = _clone(APP, d / "second")
        for rel in ("Contents/MacOS/Moonglade",):
            p = app2 / rel
            data = bytearray(p.read_bytes())
            off = _cstring_offset(p)
            data[off] = (data[off] + 1) % 256                    # one byte of the launcher's strings
            p.unlink()
            p.write_bytes(bytes(data))
            p.chmod(0o755)
        ver = app2 / PAYLOAD_REL / "VERSION"
        v = json.loads(ver.read_text())
        v["version"] = f"{v.get('version')}-identity-check"
        ver.unlink()
        ver.write_text(json.dumps(v) + "\n")
        b2 = d / "build2"
        b2.mkdir()
        shutil.copy2(BUILD / "oracle.json", b2 / "oracle.json")    # newer than the changed VERSION
        os.utime(b2 / "oracle.json")
        cfg = d / "signing.json"
        cfg.write_text('{"mode": "ad-hoc"}\n')
        rec2 = SG.sign(app2, b2, cfg, b2 / "oracle.json", env=SG.EnvReader({}))
        c1, c2 = _display(APP).get("CDHash"), _display(app2).get("CDHash")
        ev["apps"] = {"first": {"cdhash": c1, "satisfies": _satisfies(APP, req)},
                      "second": {"cdhash": c2, "satisfies": _satisfies(app2, req), "designated": _designated(app2),
                                 "version": v["version"], "sign_mode": rec2["mode"]}}
        require(c1 and c2 and c1 != c2, "the two apps' code hashes don't differ", **ev)
        require(ev["apps"]["first"]["satisfies"]["rc"] == 0 and ev["apps"]["second"]["satisfies"]["rc"] == 0,
                "an update doesn't satisfy the first app's designated requirement", **ev)
        # counterexample: the second app's bundle re-signed with another identifier
        app3 = _clone(app2, d / "other")
        r = subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", "--options", "runtime", "--timestamp=none",
                            "--identifier", "io.github.zttimmerman.other",
                            '-r=designated => identifier "io.github.zttimmerman.other"', str(app3)],
                           capture_output=True, text=True)
        ev["counterexample_other_id"] = {"sign_rc": r.returncode, "designated": _designated(app3),
                                         "satisfies": _satisfies(app3, req)}
        require(r.returncode == 0 and ev["counterexample_other_id"]["satisfies"]["rc"] != 0,
                "counterexample: an app with another identifier satisfied the requirement", **ev)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return ev


# ================================================================================================ seal_survives_use

def seal_problems(bundle: Path, recorded: dict) -> tuple[list[str], dict]:
    sys.path.insert(0, str(TOOLS))
    import sign as SG                                            # noqa: E402
    probs = []
    v = _cs(["--verify", "--deep", "--strict", "--verbose=2"], bundle)
    if v.returncode != 0:
        probs.append(f"bundle: --verify --deep --strict rc {v.returncode}: "
                     f"{v.stderr.decode(errors='replace').strip()[-300:]}")
    bad = []
    n = 0
    for p in sorted((bundle / PAYLOAD_REL).rglob("*")):
        if _is_macho(p):
            n += 1
            if _cs(["--verify", "--strict"], p).returncode != 0:
                bad.append(str(p.relative_to(bundle)))
    if bad:
        probs.append(f"payload Mach-O files that don't verify: {bad[:5]}")
    now = SG.bundle_files(bundle)
    added = sorted(set(now) - set(recorded))
    removed = sorted(set(recorded) - set(now))
    changed = sorted(k for k in set(now) & set(recorded) if now[k] != recorded[k])
    if added or removed or changed:
        probs.append(f"bundle files differ from packaging: added {added[:5]}, removed {removed[:5]}, "
                     f"changed {changed[:5]}")
    return probs, {"payload_machos_verified": n, "files": len(now), "added": added[:10], "removed": removed[:10],
                   "changed": changed[:10], "verify": v.stderr.decode(errors="replace").strip()[-300:]}


@check("signing.seal_survives_use", order=18, capability=CAP, stack="down", tier="slow")
def seal_survives_use(ctx):
    """The run's shared first-launch fixture app, after first launch, the stand-in play loop and a backup ran from it
    (run here if the run's earlier checks didn't): the bundle verifies (--deep --strict), every payload Mach-O verifies
    on its own, and the bundle's file list and hashes equal those recorded when this run's packaging signed it.
    Counterexample: a clone with a __pycache__ written into the payload fails both."""
    from .firstlaunch_checks import app_play, fixture, play_ok
    from .release_runtime_checks import heal_scratch, sandbox_profile
    ev = _need_signed(ctx)
    ev["healed"] = heal_scratch()
    fx = fixture(ctx)
    ev["fixture"] = fx
    d, home, bundle = Path(fx["d"]), Path(fx["home"]), Path(fx["bundle"])
    bk = home / "backups"
    backups = sorted(x.name for x in bk.iterdir() if not x.name.startswith(".")) if bk.is_dir() else []
    if not backups:                                   # GIVEN: the play loop (which backs up on exit) ran from it
        prof = d / "release.sb"
        prof.write_text(sandbox_profile(d))
        run = app_play(bundle, home, d, "seal", sandbox=prof)
        pp = play_ok(run)
        ev["play_run_here"] = {"problems": pp, "backup": (run.get("play_last") or {}).get("backup")}
        require(not pp, f"GIVEN: the play loop from the fixture app didn't reach ready and stop cleanly: {pp}", **ev)
        backups = sorted(x.name for x in bk.iterdir() if not x.name.startswith(".")) if bk.is_dir() else []
    ev["home_backups"] = backups
    require(backups and (home / "home.json").is_file(), "GIVEN: no first launch + play + backup in the fixture HOME",
            **ev)
    recorded = json.loads((BUILD / "bundle-files.json").read_text())
    probs, info = seal_problems(bundle, recorded)
    ev["seal"] = info
    ev["problems"] = probs
    require(not probs, f"the signature didn't survive use: {probs}", **ev)
    s = _scratch("seal")
    try:
        c = _clone(bundle, s)
        pc = c / PAYLOAD_REL / "harness" / "hslib" / "__pycache__"
        pc.mkdir(parents=True, exist_ok=True)
        (pc / "paths.cpython-313.pyc").write_bytes(b"\x00counterexample")
        cprobs, _ = seal_problems(c, recorded)
        ev["counterexample_pycache"] = cprobs
        require(any("--deep --strict" in x for x in cprobs) and any("added" in x and "__pycache__" in x for x in cprobs),
                "counterexample: a __pycache__ inside the bundle wasn't caught", **ev)
    finally:
        shutil.rmtree(s, ignore_errors=True)
    return ev


# ================================================================================================ open_instructions

NOTES = TOOLS / "notes"
SETUP_MD = P.ROOT / "docs" / "setup.md"
ONE_LINER = "xattr -dr com.apple.quarantine /Applications/Moonglade.app"


def _opening_section(text: str) -> str:
    m = re.search(r"^(#+) Opening it the first time\s*$(.*?)(?=^#{1,3} |\Z)", text, re.M | re.S)
    return m.group(2) if m else ""


def instruction_problems(adhoc: str, devid: str, setup: str) -> list[str]:
    probs = []
    for name, t in (("notes/ad-hoc.md", adhoc), ("docs/setup.md 'Opening it the first time'", setup)):
        for want in ("Privacy & Security", "Open Anyway", "each new version", ONE_LINER):
            if want not in t:
                probs.append(f"{name} doesn't say {want!r}")
    if "Open Anyway" in devid:
        probs.append("notes/developer-id.md mentions Open Anyway")
    if "downloaded from the internet" not in devid:
        probs.append("notes/developer-id.md doesn't describe the normal 'downloaded from the internet' question")
    for name, t in (("notes/ad-hoc.md", adhoc), ("notes/developer-id.md", devid), ("docs/setup.md", setup)):
        if not re.search(r"(?i)no game files are included", t):
            probs.append(f"{name} doesn't say no game files are included")
    return probs


def quarantined_files(app: Path) -> list[str]:
    out = []
    for p in [app, *sorted(app.rglob("*"))]:
        r = subprocess.run(["/usr/bin/xattr", "-s", "-p", QUARANTINE, str(p)], capture_output=True)
        if r.returncode == 0:
            out.append(str(p.relative_to(app.parent)))
    return out


def run_one_liner(line: str, app: Path) -> dict:
    import shlex
    argv = shlex.split(line)
    argv[-1] = str(app)                                   # the documented path, substituted
    r = subprocess.run(["/usr/bin/" + argv[0], *argv[1:]] if "/" not in argv[0] else argv, capture_output=True,
                       text=True)
    return {"argv": [*argv[:-1], "<scratch app>"], "rc": r.returncode, "err": r.stderr.strip()[-200:]}


@check("signing.open_instructions", order=13, capability=CAP, stack="any", tier="quick")
def open_instructions(ctx):
    """docs/setup.md ("Opening it the first time") and tools/release/notes/{ad-hoc,developer-id}.md: the ad-hoc text
    names Privacy & Security, "Open Anyway", "each new version" and the exact one-liner; the Developer ID text doesn't
    mention Open Anyway; all say no game files are included; the workflow's notes come from the active mode's file;
    the documented one-liner (path substituted), run on a quarantined, ad-hoc-signed fixture app, leaves no quarantine
    attribute and a signature that still verifies. Counterexamples: notes without "Open Anyway", and the one-liner
    without -r (an inner file keeps its attribute)."""
    ev: dict = {}
    adhoc, devid, setup_all = ((NOTES / "ad-hoc.md").read_text(), (NOTES / "developer-id.md").read_text(),
                               SETUP_MD.read_text())
    setup = _opening_section(setup_all)
    ev["setup_section_chars"] = len(setup)
    probs = instruction_problems(adhoc, devid, setup)
    wprobs, _ = workflow_problems(WORKFLOW.read_text())
    probs += [p for p in wprobs if "notes" in p]
    ev["problems"] = probs
    require(setup and not probs, f"instructions: {probs or 'no Opening it the first time section in docs/setup.md'}",
            **ev)
    ev["counterexample_no_open_anyway"] = instruction_problems(adhoc.replace("Open Anyway", "Open"), devid, setup)
    require(ev["counterexample_no_open_anyway"], "counterexample: notes without Open Anyway passed", **ev)
    d = _scratch("open")
    try:
        results = {}
        for tag, line in (("documented", ONE_LINER), ("without_r", ONE_LINER.replace("-dr", "-d"))):
            w = d / tag
            app = fixture_app(w)
            r = run_sign(w, _clean_env())
            require(r["rc"] == 0, "signing the fixture app failed", sign=r, **ev)
            for p in [app, *app.rglob("*")]:
                if not p.is_symlink():
                    subprocess.run(["/usr/bin/xattr", "-w", QUARANTINE, _quarantine_value(), str(p)], check=True)
            before = len(quarantined_files(app))
            run = run_one_liner(line, app)
            v = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], capture_output=True,
                               text=True)
            results[tag] = {"quarantined_before": before, "run": run, "quarantined_after": quarantined_files(app),
                            "verify_rc": v.returncode, "verify_err": v.stderr.strip()[-200:]}
        ev["one_liner"] = results
        doc = results["documented"]
        require(doc["quarantined_before"] > 1 and doc["run"]["rc"] == 0 and not doc["quarantined_after"]
                and doc["verify_rc"] == 0, "the documented one-liner didn't clear the quarantine with the seal intact",
                **ev)
        require(results["without_r"]["quarantined_after"], "counterexample: a non-recursive xattr left no flag", **ev)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return ev


# ================================================================================================ notarized (CI-only)

@check("signing.notarized", order=13, capability=CAP, stack="any", tier="slow", ci_only=True)
def notarized(ctx):
    """CI-only (developer-id mode, the release workflow on the runner; also run there directly as
    `harness/hslib/notarycheck.py`): every signed Mach-O has the team's Developer ID authority, a secure timestamp and
    the runtime flag; the notary log says Accepted with no issues; stapler validate passed; spctl on the unzipped final
    asset says accepted, source=Notarized Developer ID; the no-Homebrew smoke passed on the signed app; SHA256SUMS
    lists the stapled zip. Outside CI it never passes: it runs the judgement's self-test (a synthetic good record set
    passes, the same with an issue in the notary log fails) and reports not applicable."""
    from .. import notarycheck as N
    from ..registry import CheckSkip
    from ..runner import CI_ONLY_SKIP_MSG
    if os.environ.get("GITHUB_ACTIONS") == "true":
        probs, ev = N.problems(BUILD, WORK / "out", WORK / "smoke.json")
        require(not probs, f"not a clean notarized release: {probs}", **ev)
        return ev
    d = _scratch("notary")
    try:
        st = N.self_test(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    require(st["ok"], "the notarization judgement's self-test failed (it can't tell a bad record set)", self_test=st)
    raise CheckSkip(CI_ONLY_SKIP_MSG, {"self_test": st, "mode": json.loads(SIGNING_JSON.read_text()).get("mode")})
