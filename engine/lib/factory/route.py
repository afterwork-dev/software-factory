"""Risk and surfaces → which reviewers run, and on which models.

Intake rates the job; this table lookup, not an agent, decides what that costs.
The table is `routing` and `surfaces` in engine/defaults.json, overridable per profile.
"""

from __future__ import annotations

from typing import Iterable

from .profile import MODELS, Profile
from .util import FactoryError


def stronger(a: str, b: str) -> str:
    return a if MODELS.index(a) >= MODELS.index(b) else b


def route(profile: Profile, risk: str, surfaces: Iterable[str]) -> dict:
    cfg = profile.config
    entry = cfg["routing"].get(risk)
    if entry is None:
        raise FactoryError(f"risk {risk!r} isn't in routing ({', '.join(cfg['routing'])})")
    lenses = dict(entry.get("lenses", {}))
    for surface in surfaces:
        extra = cfg["surfaces"].get(surface)
        if extra is None:
            raise FactoryError(f"surface {surface!r} isn't defined ({', '.join(cfg['surfaces'])})")
        for lens, model in extra["lenses"].items():
            lenses[lens] = stronger(lenses[lens], model) if lens in lenses else model
    return {"builder": entry["builder"], "lenses": dict(sorted(lenses.items()))}
