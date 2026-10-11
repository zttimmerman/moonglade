"""The fixed journey set (harness-reporting "Fixed journey set"; openspec/changes/test-pyramid design 10, part 2).

Each journey is one check with layer "journey", its spec budget and `covers`; the scenarios it asserts keep their own
ids as children (session.run_children: each its own status and evidence), so every spec `Check:` line still names
them. journey.pause lives in pause_checks.py (part 1); the other five are here.

A journey runs the real client (or the app) end to end, one at a time. Warm journeys (stack="up": the shared stack
stays up, HS_PLAY_KEEP_STACK; play_checks.WARM) run before `hs down`; cold ones (stack="down": journey.first_play's
real cold start, and the app's own start, which refuses the keep-stack switch) run after it (runner.segment).
"""
from __future__ import annotations

import time

from ..registry import check
from ..session import run_children

CAP = "harness-reporting"

# The scenarios of each journey, in the order their sessions run (the first scenario of a shared session runs it).
# first_play: the cold session first (./play starts the stack from nothing, Continue, character select, the world, the
# add-on's report, quit, the stack stopped with its backup); then the other sessions warm on one `hs up`.
FIRST_PLAY_COLD = ("login.loading_overlay", "login.charselect_layout", "login.charselect_wording",
                   "login.overlay_progress", "client.wording_addon")
FIRST_PLAY_WARM = ("login.auto_login", "login.no_glue_errors", "login.fresh_menu_waits", "client.first_start_settings",
                   "client.resolution_default")
FIRST_PLAY = FIRST_PLAY_COLD + FIRST_PLAY_WARM
LOGIN_RECOVERY = ("login.friendly_errors", "login.overlay_progress_reload", "login.backdrop_choice",
                  "login.cancel_shows_list")
SAVES = ("savesctl.live_roundtrip", "savesctl.live_codes", "savesctl.live_down", "savesctl.realm_restored",
         "saves.live_charselect_load", "saves.live_charselect_list", "savesctl.account_name_kept")
STOCK_CLIENT = ("client.stock_mode",)
APP_DESKTOP = ("app.names_running", "app.launch_lifecycle", "app.single_instance", "app.relocatable",
               "app.single_dock_icon", "app.handoff_focus", "app.reopen_focuses_game", "app.reopen_during_loading",
               "app.reopen_unminimizes", "app.shutdown_window", "app.loading_stages", "app.shim_scope",
               "app.backups_window")


def _fns() -> dict:
    """Scenario id -> the function that asserts it (each module's former check function)."""
    from . import (app_checks as A, backup_checks as B, client_play_checks as CP, login_checks as L,
                   polish_checks as PO, resolution_checks as R, saves_gui_checks as SG, savesctl_checks as SC,
                   seamless_checks as SE)
    return {
        "login.auto_login": L.auto_login, "login.no_glue_errors": L.no_glue_errors,
        "login.fresh_menu_waits": L.fresh_menu_waits, "client.first_start_settings": L.first_start_settings,
        "login.cancel_shows_list": L.menu_waits, "login.backdrop_choice": L.backdrop_choice,
        "client.resolution_default": R.resolution_default,
        "login.loading_overlay": SE.loading_overlay, "login.charselect_layout": SE.charselect_layout,
        "login.charselect_wording": SE.charselect_wording, "login.overlay_progress": SE.overlay_progress,
        "client.wording_addon": SE.wording_addon, "login.friendly_errors": SE.friendly_errors,
        "login.overlay_progress_reload": SE.overlay_progress_reload,
        "savesctl.live_roundtrip": SC.live_roundtrip, "savesctl.live_codes": SC.live_codes,
        "savesctl.live_down": SC.live_down, "savesctl.realm_restored": SC.realm_restored,
        "savesctl.account_name_kept": SC.account_name_kept,
        "saves.live_charselect_load": SG.live_charselect_load, "saves.live_charselect_list": SG.live_charselect_list,
        "client.stock_mode": CP.stock_mode,
        "app.names_running": A.names_running, "app.launch_lifecycle": A.launch_lifecycle,
        "app.single_instance": A.single_instance, "app.relocatable": A.relocatable,
        "app.single_dock_icon": PO.single_dock_icon, "app.handoff_focus": PO.handoff_focus,
        "app.reopen_focuses_game": PO.reopen_focuses_game, "app.reopen_during_loading": PO.reopen_during_loading,
        "app.reopen_unminimizes": PO.reopen_unminimizes, "app.shutdown_window": PO.shutdown_window,
        "app.loading_stages": PO.loading_stages, "app.shim_scope": PO.shim_scope,
        "app.backups_window": B.backups_window,
    }


def run_cold_then_warm(ctx: dict, cold: tuple, warm: tuple) -> dict:
    """The cold scenarios (stack down before and after), then `hs up`, the warm ones, `hs down`: nothing of this repo
    left running at the end (play_checks.remaining)."""
    from . import play_checks as pc
    from .. import stack
    from ..registry import require
    fns = _fns()
    out = run_children(ctx, [(i, fns[i]) for i in cold])
    t0 = time.time()
    up = stack.up()
    out["stack_up"] = {"s": round(time.time() - t0, 1), "ok": all(stack.stack_processes().values())}
    try:
        w = run_journey(ctx, warm, warm=True)
        out["children"] |= w["children"]
        out["warm"] = w.get("warm")
    except Exception as e:  # noqa: BLE001  (a warm-stack failure: its children are in the evidence)
        ev = getattr(e, "evidence", None) or {}
        out["children"] |= ev.get("children") or {}
        out["warm"] = ev.get("warm") or {"status": "error", "message": f"{type(e).__name__}: {e}"}
    finally:
        t1 = time.time()
        out["stack_down"] = {"result": stack.down(), "s": round(time.time() - t1, 1)}
    out["remaining_after"] = pc.remaining()
    out["up_result"] = up
    require(pc.is_clear(out["remaining_after"]), "something from this repo is still running after the journey", **out)
    require((out.get("warm") or {}).get("status") == "pass", f"warm part: {(out.get('warm') or {}).get('message')}", **out)
    return out


def run_journey(ctx: dict, ids: tuple, warm: bool = False) -> dict:
    """The journey's scenarios in order. `warm`: the shared stack must be up; its sessions run ./play with the
    harness's keep-stack switch (play_checks.WARM), and after the last one the same stack must still be running (a
    deliberate restart of a component is recorded by play_checks.warm_refresh)."""
    fns = _fns()
    steps = [(i, fns[i]) for i in ids]
    if not warm:
        return run_children(ctx, steps)
    from . import play_checks as pc
    from .. import stack
    from ..registry import require
    up = stack.stack_processes()
    require(all(up.values()), "GIVEN not met: a warm journey needs the shared stack up", stack=up)
    pc.WARM = {"pids": pc.stack_pids()}
    before = dict(pc.WARM["pids"])

    def stack_kept(_ctx):
        now = pc.stack_pids()
        e = {"stack_at_start": before, "stack_at_end": now, "kept_pids": pc.WARM["pids"],
             "refreshed": pc.WARM.get("refreshed", [])}
        require(all(now.values()) and now == pc.WARM["pids"], "the shared stack didn't stay up with the same pids", **e)
        return e
    try:
        out = run_children(ctx, steps + [("stack_kept", stack_kept)])
    finally:
        pc.WARM = None
    out["warm"] = out["children"].pop("stack_kept")
    if out["warm"]["status"] != "pass":
        from ..registry import CheckFail
        raise CheckFail(f"warm journey: {out['warm'].get('message')}", out)
    return out


JOURNEY_COMMON = ("harness/hslib/checks/journey_checks.py", "harness/hslib/session.py")

FIRST_PLAY_COVERS = JOURNEY_COMMON + (
    "play", "client-mods/**", "patches/client/**",
    "harness/hslib/checks/login_checks.py", "harness/hslib/checks/seamless_checks.py",
    "harness/hslib/checks/resolution_checks.py", "harness/hslib/resolution.py", "harness/hslib/clientcfg.py",
    "harness/hslib/frontmenu.py", "harness/hslib/stages.py")
LOGIN_RECOVERY_COVERS = JOURNEY_COMMON + (
    "client-mods/glue/HSSeamless.lua", "client-mods/glue/HSFrontMenu.*",
    "harness/hslib/checks/login_checks.py", "harness/hslib/checks/seamless_checks.py", "harness/hslib/frontmenu.py")
SAVES_COVERS = JOURNEY_COMMON + (
    "client-mods/glue/HSCharSaves.lua", "client-mods/glue/HSFrontMenu.*",
    "client-mods/addons/Moonglade/MoongladeSaves.lua", "modules/mod-moonglade/src/MoongladeSaves.*",
    "modules/mod-moonglade/src/logic/Saves*",
    "harness/hslib/checks/saves_gui_checks.py", "harness/hslib/checks/savesctl_checks.py", "harness/hslib/savesctl.py",
    "harness/hslib/charsaves.py")
STOCK_CLIENT_COVERS = JOURNEY_COMMON + ("patches/client/**", "harness/hslib/checks/client_play_checks.py")
APP_DESKTOP_COVERS = JOURNEY_COMMON + (
    "harness/app/*", "branding/*", "harness/hslib/app.py", "harness/hslib/dockshim.py", "harness/hslib/guienv.py",
    "harness/hslib/backups.py", "harness/hslib/checks/app_checks.py", "harness/hslib/checks/polish_checks.py",
    "harness/hslib/checks/backup_checks.py")


@check("journey.first_play", order=460, capability=CAP, stack="down", layer="journey", budget_s=150,
       covers=FIRST_PLAY_COVERS, children=FIRST_PLAY)
def first_play(ctx):
    """The cold first play: a fresh scratch play client from the real ./play (test account), the menu waiting for
    Continue, one Continue, character select and its wording/layout, the world and the add-on's report, the written
    first-start settings and resolution, and nothing left running (the scenarios in FIRST_PLAY, each its own record)."""
    return run_cold_then_warm(ctx, FIRST_PLAY_COLD, FIRST_PLAY_WARM)


@check("journey.stock_client", order=461, capability=CAP, stack="up", layer="journey", budget_s=60,
       covers=STOCK_CLIENT_COVERS, children=STOCK_CLIENT)
def stock_client(ctx):
    """The stock client mode end to end (the scenario client.stock_mode)."""
    return run_journey(ctx, STOCK_CLIENT, warm=True)


@check("journey.login_recovery", order=462, capability=CAP, stack="up", layer="journey", budget_s=120,
       covers=LOGIN_RECOVERY_COVERS, children=LOGIN_RECOVERY)
def login_recovery(ctx):
    """The login screen recovering: the friendly error with the login server down, Try again, the overlay's progress
    after a reload, the backdrop choice kept across a restart and the menu waiting without a login (the scenarios in
    LOGIN_RECOVERY)."""
    return run_journey(ctx, LOGIN_RECOVERY, warm=True)


@check("journey.saves", order=463, capability=CAP, stack="up", layer="journey", budget_s=150,
       covers=SAVES_COVERS, children=SAVES)
def saves(ctx):
    """Saves from the real client: the control channel's round trip, codes and listener-down, the realm and account
    name restored, and a load from the character screen's Saves list (the scenarios in SAVES)."""
    return run_journey(ctx, SAVES, warm=True)


@check("journey.app_desktop", order=464, capability=CAP, stack="down", layer="journey", budget_s=240, gui=True,
       covers=APP_DESKTOP_COVERS, children=APP_DESKTOP)
def app_desktop(ctx):
    """The Mac app on the desktop with the stand-in client: names while running, the launch lifecycle, one instance,
    a relocated copy, one Dock icon, focus hand-off and reopen, the shutdown window, loading stages, the shim's scope
    and the Backups window's restore (the scenarios in APP_DESKTOP)."""
    return run_journey(ctx, APP_DESKTOP)
