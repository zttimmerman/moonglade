"""Check registry. A check returns an evidence dict (pass) or raises CheckFail (fail).
Any other exception → status 'error'. Checks run by segment (runner.segment), then `order`, then id.

test-pyramid (harness-reporting "Test layers and run tiers"): every check declares one `layer`:
- unit: no server, no client, no Wine process (the unit tier samples the process table and fails one that starts);
- integration: server probe, SQL, CLI or a stand-in ./play;
- journey: the real client or the app end to end, only the fixed set (report.journeys);
- gate: builds from empty, soaks, packaging, real-stack harness self-tests.
`covers` (harness-reporting "Impact selection is fail-safe") lists what the check owns: repo paths or globs (product
files and the harness modules its file imports directly) and config sections as `<conf path>#<Section prefix>`;
a check that passes none takes its module's `COVERS` (declared per check file). `scope="character"` marks a
shared-server test that touches only its own fixture characters (it may share the 2-worker pool); `budget_s` is a
journey's or session's time budget; `parallel_safe` lets a unit check run in the unit tier's process pool;
`children` are the scenario ids a clustered session records results for (each its own status and evidence).
"""
from dataclasses import dataclass, field
from typing import Callable


class CheckFail(Exception):
    def __init__(self, msg: str, evidence: dict | None = None):
        super().__init__(msg)
        self.evidence = evidence or {}


class CheckSkip(Exception):
    def __init__(self, msg: str = "", evidence: dict | None = None):
        super().__init__(msg)
        self.evidence = evidence or {}


LAYERS = ("unit", "integration", "journey", "gate")
RUN_TIERS = ("unit", "pr", "full")
SCOPES = ("exclusive", "character")


@dataclass
class Check:
    id: str
    fn: Callable
    order: int
    capability: str
    stack: str = "any"   # "up" | "down" | "any": required stack state before running
    doc: str = ""
    layer: str | None = None      # LAYERS; None = undeclared (report.layers_declared fails it)
    profile: str | None = None    # config profile a stack="up" check needs (None = config.DEFAULT_PROFILE)
    gui: bool = False             # needs exclusive screen focus (the GUI preflight; journey.app_desktop)
    ci_only: bool = False         # judges a release-workflow run only (release-signing signing.notarized): outside CI
                                  # it may only skip with runner.CI_ONLY_SKIP_MSG ("not applicable"), never pass
    covers: tuple = ()
    scope: str = "exclusive"
    budget_s: float | None = None
    parallel_safe: bool = False
    children: tuple = field(default_factory=tuple)


REGISTRY: dict[str, Check] = {}
CHILDREN: dict[str, str] = {}     # child scenario id -> the session check that records it


def check(id: str, order: int, capability: str, stack: str = "any", layer: str | None = None,
          profile: str | None = None, gui: bool = False, ci_only: bool = False, covers=None,
          scope: str = "exclusive", budget_s: float | None = None, parallel_safe: bool = False, children=()):
    """Register a check (see the module doc for the fields). `layer` must be one of LAYERS."""
    def deco(fn):
        if id in REGISTRY or id in CHILDREN:
            raise RuntimeError(f"duplicate check id {id}")
        if layer is not None and layer not in LAYERS:
            raise RuntimeError(f"{id}: unknown layer {layer!r}; expected one of {LAYERS}")
        if scope not in SCOPES:
            raise RuntimeError(f"{id}: unknown scope {scope!r}")
        cov = covers if covers is not None else fn.__globals__.get("COVERS", ())
        REGISTRY[id] = Check(id, fn, order, capability, stack, (fn.__doc__ or "").strip(), layer, profile, gui,
                             ci_only, tuple(cov), scope, budget_s, parallel_safe, tuple(children))
        for c in children:
            if c in REGISTRY or c in CHILDREN:
                raise RuntimeError(f"duplicate check id {c} (child of {id})")
            CHILDREN[c] = id
        return fn
    return deco


def require(cond, msg: str, **evidence):
    if not cond:
        raise CheckFail(msg, evidence)


def load_all():
    from . import checks  # noqa: F401  (imports every checks/*.py)
    return REGISTRY


def known_ids() -> set[str]:
    """Every id a scenario may name: registered checks and the children of clustered sessions."""
    load_all()
    return set(REGISTRY) | set(CHILDREN)
