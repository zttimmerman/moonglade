"""The fixed journey set (harness-reporting "Fixed journey set"; openspec/changes/test-pyramid design 10, part 2).

Each journey is one check with layer "journey", its spec budget and `covers`; the scenarios it asserts keep their own
ids as children (session.run_children: each its own status and evidence), so every spec `Check:` line still names
them. journey.pause lives in pause_checks.py (part 1); the other five are here.

A journey runs the real client (or the app) end to end, one at a time. Warm journeys (the shared stack stays up,
HS_PLAY_KEEP_STACK) run before `hs down`; cold ones (stack="down": the app's own start, which refuses the keep-stack
switch, and the sessions not yet warm) run after it, with journey.first_play (runner.segment).
"""
from __future__ import annotations

from ..registry import check
from ..session import run_children

CAP = "harness-reporting"

# The scenarios of each journey, in the order their sessions run (the first scenario of a shared session runs it).
FIRST_PLAY = ("login.auto_login", "login.no_glue_errors", "login.fresh_menu_waits", "client.first_start_settings",
              "client.resolution_default", "login.loading_overlay", "login.charselect_layout",
              "login.charselect_wording", "login.overlay_progress", "client.wording_addon")
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


def run_journey(ctx: dict, ids: tuple) -> dict:
    fns = _fns()
    return run_children(ctx, [(i, fns[i]) for i in ids])


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
    return run_journey(ctx, FIRST_PLAY)


@check("journey.stock_client", order=461, capability=CAP, stack="down", layer="journey", budget_s=60,
       covers=STOCK_CLIENT_COVERS, children=STOCK_CLIENT)
def stock_client(ctx):
    """The stock client mode end to end (the scenario client.stock_mode)."""
    return run_journey(ctx, STOCK_CLIENT)


@check("journey.login_recovery", order=462, capability=CAP, stack="down", layer="journey", budget_s=120,
       covers=LOGIN_RECOVERY_COVERS, children=LOGIN_RECOVERY)
def login_recovery(ctx):
    """The login screen recovering: the friendly error with the login server down, Try again, the overlay's progress
    after a reload, the backdrop choice kept across a restart and the menu waiting without a login (the scenarios in
    LOGIN_RECOVERY)."""
    return run_journey(ctx, LOGIN_RECOVERY)


@check("journey.saves", order=463, capability=CAP, stack="down", layer="journey", budget_s=150,
       covers=SAVES_COVERS, children=SAVES)
def saves(ctx):
    """Saves from the real client: the control channel's round trip, codes and listener-down, the realm and account
    name restored, and a load from the character screen's Saves list (the scenarios in SAVES)."""
    return run_journey(ctx, SAVES)


@check("journey.app_desktop", order=464, capability=CAP, stack="down", layer="journey", budget_s=240, gui=True,
       covers=APP_DESKTOP_COVERS, children=APP_DESKTOP)
def app_desktop(ctx):
    """The Mac app on the desktop with the stand-in client: names while running, the launch lifecycle, one instance,
    a relocated copy, one Dock icon, focus hand-off and reopen, the shutdown window, loading stages, the shim's scope
    and the Backups window's restore (the scenarios in APP_DESKTOP)."""
    return run_journey(ctx, APP_DESKTOP)
