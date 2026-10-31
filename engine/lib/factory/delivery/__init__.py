"""Delivery: what happens to a job once it is approved.

Projects differ here. Some need a pull request, some forbid merging to the
base branch, some let you land changes directly. So the pipeline ends at a
pluggable step: a profile picks a method with `delivery.method` and sets that
method's options next to it, for example

    "delivery": { "method": "handoff" }
    "delivery": { "method": "local-merge", "strategy": "squash" }

To add a method (a GitHub pull request, a script of your own), write a
`Method` subclass in this package and add it to METHODS. base.py has the
contract.
"""

from __future__ import annotations

from typing import List, Tuple

from ..jobs import Job
from ..util import FactoryError, locked, now
from .base import Blocked, Method
from .handoff import Handoff
from .local_merge import LocalMerge

METHODS = {m.name: m for m in (Handoff(), LocalMerge())}
DEFAULT = "handoff"


def method_for(profile) -> Tuple[Method, dict]:
    config = profile.config.get("delivery") or {}
    name = config.get("method", DEFAULT)
    if name not in METHODS:
        raise FactoryError(f"unknown delivery method {name!r} (known: {', '.join(METHODS)})")
    method = METHODS[name]
    return method, method.settings(config)


def validate(profile) -> List[str]:
    config = profile.config.get("delivery")
    if not isinstance(config, dict):
        return ["delivery must be an object like {\"method\": \"handoff\"}"]
    name = config.get("method", DEFAULT)
    if name not in METHODS:
        return [f"delivery.method must be one of {', '.join(METHODS)}"]
    return METHODS[name].validate(config)


def deliver(job: Job) -> str:
    if job.stage != "delivering":
        raise FactoryError(f"{job.id} is in {job.stage}; only an approved job (stage delivering) can be delivered")
    method, opts = method_for(job.profile)
    with locked(job.profile.lock):
        try:
            record = method.deliver(job, opts)
        except Blocked as e:
            job.park(str(e), retry="delivering")
            return f"BLOCKED: {e}"
    record.update(method=method.name, reopenable=method.reopenable, at=now())
    job.data["delivery"] = record
    job.set_stage("delivered", record["summary"])
    return f"DELIVERED {job.id}: {record['summary']}"
