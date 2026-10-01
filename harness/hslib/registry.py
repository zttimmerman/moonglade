"""Check registry. A check returns an evidence dict (pass) or raises CheckFail (fail).
Any other exception → status 'error'. Order matters: checks run by `order`, then id."""
from dataclasses import dataclass, field
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


REGISTRY: dict[str, Check] = {}


def check(id: str, order: int, capability: str, stack: str = "any"):
    def deco(fn):
        if id in REGISTRY:
            raise RuntimeError(f"duplicate check id {id}")
        REGISTRY[id] = Check(id, fn, order, capability, stack, (fn.__doc__ or "").strip())
        return fn
    return deco


def require(cond, msg: str, **evidence):
    if not cond:
        raise CheckFail(msg, evidence)


def load_all():
    from . import checks  # noqa: F401  (imports every checks/*.py)
    return REGISTRY
