"""Check registry. A check returns an evidence dict (pass) or raises CheckFail (fail).
Any other exception → status 'error'. Order matters: checks run by `order`, then id."""
from dataclasses import dataclass
from typing import Callable


class CheckFail(Exception):
    def __init__(self, msg: str, evidence: dict | None = None):
        super().__init__(msg)
        self.evidence = evidence or {}


class CheckSkip(Exception):
    def __init__(self, msg: str = "", evidence: dict | None = None):
        super().__init__(msg)
        self.evidence = evidence or {}


@dataclass
class Check:
    id: str
    fn: Callable
    order: int
    capability: str
    stack: str = "any"   # "up" | "down" | "any": required stack state before running
    doc: str = ""
    tier: str = "quick"  # "quick" | "full" | "slow" (TIERS); see harness-reporting "Test tiers"
    tier_declared: bool = False   # False = the check relied on the default; report.tiers_declared fails it
    profile: str | None = None    # config profile a stack="up" check needs (None = config.DEFAULT_PROFILE)
    gui: bool = False             # needs exclusive screen focus (harness-reporting "GUI tier"); see runner.gui_tier_skips
    backups: bool = False         # save backups end to end (harness-reporting "Backups tier"); see runner.backups_tier_skips
    ci_only: bool = False         # judges a release-workflow run only (release-signing signing.notarized): outside CI
                                  # it may only skip with runner.CI_ONLY_SKIP_MSG ("not applicable"), never pass


# Tiers in inclusion order: `--tier X` runs every check whose tier is X or earlier in this tuple.
TIERS = ("quick", "full", "slow")
# `--tier gui` (harness-reporting "GUI tier") is a run tier, not a check tier: it runs exactly the checks marked gui
# (whatever their check tier), and is never a full run.
# `--tier backups` (harness-reporting "Backups tier") likewise runs exactly the checks marked backups.
RUN_TIERS = TIERS + ("gui", "backups")


def tiers_for(run_tier: str) -> tuple[str, ...]:
    """Check tiers executed by `hs run --tier run_tier` (quick → quick; full → quick+full; slow → all;
    gui → all tiers, narrowed to gui-marked checks by runner.select)."""
    if run_tier in ("gui", "backups"):
        return TIERS
    if run_tier not in TIERS:
        raise ValueError(f"unknown tier {run_tier!r}; expected one of {RUN_TIERS}")
    return TIERS[: TIERS.index(run_tier) + 1]


REGISTRY: dict[str, Check] = {}


def check(id: str, order: int, capability: str, stack: str = "any", tier: str | None = None,
          profile: str | None = None, gui: bool = False, backups: bool = False, ci_only: bool = False):
    """Register a check. Every check should pass `tier=` explicitly; omitting it defaults to "quick" but is
    recorded as undeclared, which report.tiers_declared fails. `profile` (config.PROFILES) is the config profile a
    stack="up" check needs; the runner restarts a stack running with another one. `gui=True` marks a check that needs
    exclusive screen focus: full runs skip it by design unless a GUI path changed (runner.gui_tier_skips).
    `backups=True` marks a save-backups end-to-end check: full runs skip it by design unless a backup path changed
    (runner.backups_tier_skips); slow runs and `--tier backups` always run it."""
    def deco(fn):
        if id in REGISTRY:
            raise RuntimeError(f"duplicate check id {id}")
        REGISTRY[id] = Check(id, fn, order, capability, stack, (fn.__doc__ or "").strip(),
                             tier if tier is not None else "quick", tier is not None, profile, gui, backups, ci_only)
        return fn
    return deco


def require(cond, msg: str, **evidence):
    if not cond:
        raise CheckFail(msg, evidence)


def load_all():
    from . import checks  # noqa: F401  (imports every checks/*.py)
    return REGISTRY
