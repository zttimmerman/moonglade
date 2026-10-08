"""The Mac's GUI state: screen lock, display sleep, battery (B32, easy-setup).

Used by the GUI checks' GIVEN (polish_checks.display_state) and by `hs run`'s start-up preflight: an unfiltered
full/slow/gui run refuses to start, within seconds, when a GUI precondition already fails (2026-10-07: a 43-minute
full run failed at its end because the screen was locked, which was knowable at second 0)."""
from __future__ import annotations

import re
import subprocess

MIN_BATTERY_PCT = 35          # CLAUDE session rule: below 35 % on battery, heavy work pauses
# report.gui_preflight's test switches (test-loop-speedups): a stubbed probe (JSON), a stubbed GUI rule
# (changed | unchanged | unknown) and "decide, print, exit"; the CLI refuses them outside a harness run
PREFLIGHT_TEST_ENV = ("HS_TEST_GUI_PROBE", "HS_TEST_GUI_RULE", "HS_TEST_PREFLIGHT_ONLY")


def screen_locked() -> bool:
    out = subprocess.run(["/usr/sbin/ioreg", "-n", "Root", "-d1", "-a"], capture_output=True, text=True).stdout
    m = re.search(r"<key>CGSSessionScreenIsLocked</key>\s*<(true|false)/>", out)
    c = re.search(r"<key>IOConsoleLocked</key>\s*<(true|false)/>", out)
    return bool((m and m.group(1) == "true") or (c and c.group(1) == "true"))


def display_asleep() -> bool | None:
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e",
                        'ObjC.import("CoreGraphics"); $.CGDisplayIsAsleep($.CGMainDisplayID())'],
                       capture_output=True, text=True, timeout=20)
    v = r.stdout.strip()
    return None if v not in ("0", "1", "true", "false") else v in ("1", "true")


def battery() -> dict:
    """{"on_battery": bool|None, "percent": int|None} from `pmset -g batt`."""
    out = subprocess.run(["/usr/bin/pmset", "-g", "batt"], capture_output=True, text=True, timeout=20).stdout
    src = re.search(r"Now drawing from '([^']+)'", out)
    pct = re.search(r"(\d+)%", out)
    return {"on_battery": (src.group(1) == "Battery Power") if src else None,
            "percent": int(pct.group(1)) if pct else None}


def probe() -> dict:
    return {"locked": screen_locked(), "display_asleep": display_asleep(), "battery": battery()}


def preflight_problem(state: dict, gui_due: bool = True) -> str | None:
    """One line saying why the run can't start now, or None. A locked screen or a sleeping display only counts when
    GUI checks are due (`gui_due`); low battery on battery power always counts. Unknown values don't refuse."""
    if gui_due and state.get("locked"):
        return "the screen is locked"
    if gui_due and state.get("display_asleep"):
        return "the display is asleep"
    b = state.get("battery") or {}
    if b.get("on_battery") and b.get("percent") is not None and b["percent"] < MIN_BATTERY_PCT:
        return f"on battery at {b['percent']} % (below {MIN_BATTERY_PCT} %)"
    return None
