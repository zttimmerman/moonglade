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
    for n in (keychain_i, notarize_i, spctl_i):
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
        require(len(caught) == 4 and all(v and v != ["(mutation didn't apply)"] for v in caught.values()),
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
