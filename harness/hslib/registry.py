"""Check registry. A check returns an evidence dict (pass) or raises CheckFail (fail).
Any other exception → status 'error'. Order matters: checks run by `order`, then id."""
from dataclasses import dataclass
from typing import Callable


class CheckFail(Exception):
    def __init__(self, msg: str, evidence: dict | None = None):
        super().__init__(msg)
        self.evidence = evidence or {}


class CheckSkip(Exception):
    pass


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


# Tiers in inclusion order: `--tier X` runs every check whose tier is X or earlier in this tuple.
TIERS = ("quick", "full", "slow")


def tiers_for(run_tier: str) -> tuple[str, ...]:
    """Check tiers executed by `hs run --tier run_tier` (quick → quick; full → quick+full; slow → all)."""
    if run_tier not in TIERS:
        raise ValueError(f"unknown tier {run_tier!r}; expected one of {TIERS}")
    return TIERS[: TIERS.index(run_tier) + 1]


REGISTRY: dict[str, Check] = {}


def check(id: str, order: int, capability: str, stack: str = "any", tier: str | None = None,
          profile: str | None = None):
    """Register a check. Every check should pass `tier=` explicitly; omitting it defaults to "quick" but is
    recorded as undeclared, which report.tiers_declared fails. `profile` (config.PROFILES) is the config profile a
    stack="up" check needs; the runner restarts a stack running with another one."""
    def deco(fn):
        if id in REGISTRY:
            raise RuntimeError(f"duplicate check id {id}")
        REGISTRY[id] = Check(id, fn, order, capability, stack, (fn.__doc__ or "").strip(),
                             tier if tier is not None else "quick", tier is not None, profile)
        return fn
    return deco


def require(cond, msg: str, **evidence):
    if not cond:
        raise CheckFail(msg, evidence)


def load_all():
    from . import checks  # noqa: F401  (imports every checks/*.py)
    return REGISTRY
