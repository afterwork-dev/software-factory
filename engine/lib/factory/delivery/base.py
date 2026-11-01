"""The contract every delivery method follows.

A method gets an approved job (stage `delivering`) and does one thing with it:
hand the branch over, merge it, open a pull request, and so on. It must only
deliver what was checked: the commit the gate passed (or a person approved),
or a commit it gates again itself after changing the branch. It returns a
record for job.json, or raises Blocked to hand the job to a person, who can
`retry` once the obstacle is gone.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .. import gate as gates
from ..jobs import Job
from ..util import git, section


class Blocked(Exception):
    """Delivery can't go ahead right now; the job waits for a person."""


class Option:
    def __init__(self, default, choices: Optional[Tuple] = None, kind=str, about: str = ""):
        self.default, self.choices, self.kind, self.about = default, choices, kind, about


class Method:
    name = ""
    about = ""          # one line, shown by `factory verify` and in the docs
    reopenable = False  # True when the branch survives delivery, so `rework` can reopen the job
    options: Dict[str, Option] = {}

    def settings(self, config: dict) -> dict:
        """This method's options: defaults, overridden by the profile's `delivery` object."""
        return {k: config.get(k, opt.default) for k, opt in self.options.items()}

    def validate(self, config: dict) -> List[str]:
        errors = []
        for key, value in config.items():
            if key == "method" or key.startswith("_"):
                continue
            opt = self.options.get(key)
            if opt is None:
                known = ", ".join(self.options) or "none"
                errors.append(f"delivery.{key} isn't an option of `{self.name}` (options: {known})")
            elif not isinstance(value, opt.kind) or isinstance(value, bool) != (opt.kind is bool):
                errors.append(f"delivery.{key} must be a {opt.kind.__name__}")
            elif opt.choices and value not in opt.choices:
                errors.append(f"delivery.{key} must be one of {', '.join(opt.choices)}")
        return errors

    def approve_meaning(self, job: Job, opts: dict) -> str:
        """What APPROVE leads to in this project, for the approver's brief."""
        raise NotImplementedError

    def deliver(self, job: Job, opts: dict) -> dict:
        """Deliver the job. Return a record with at least `summary`; raise Blocked to park it."""
        raise NotImplementedError


def passed_commits(job: Job) -> set:
    return {g["commit"] for g in (job.data.get("gate") or {}, job.data.get("merge_gate") or {})
            if g.get("verdict") == "PASS" and g.get("commit")}


def checked_head(job: Job) -> str:
    """HEAD of a clean worktree that a gate passed, or that a person approved as it is."""
    leftovers = (job.data.get("gate") or {}).get("leftovers", [])
    problems = gates.worktree_problems(job, leftovers)
    if problems:
        raise Blocked("the job's worktree isn't clean: " + "; ".join(problems))
    head = git(job.worktree, "rev-parse", "HEAD")
    if head not in passed_commits(job) and not job.data.get("approved_by_person"):
        raise Blocked("the branch has commits that no gate passed and no reviewer saw; "
                      f"rework it, or accept it as it is with `bin/factory approve {job.id}`")
    return head


def reviewer_notes(job: Job) -> str:
    """The approver's `## For the reviewer` notes from decision.md, or "" when there are none."""
    decision = job.file("decision.md")
    notes = section(decision.read_text(), "For the reviewer") if decision.is_file() else ""
    return "" if notes.lower().rstrip(".") in ("none", "nothing") else notes
