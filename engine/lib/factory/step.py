"""The state machine. `factory step <job>` is the orchestrator's only decision-maker.

Each call reads the job's files, records what the agents decided, moves the job
along if their files justify it, and prints the next action as JSON:

  spawn  agents to start (or to continue, see `continue`), all at once, with the
         exact subagent_type, model and prompt
  run    a factory command to run (the gate, the delivery)
  stop   the job is waiting for a person; tell_user says why and what they can do
  done   the job is finished (delivered, rejected or abandoned)

Every judgment that a rule can make (round limits, routing, verdict parsing,
whether the reviewed commit is still the gated commit) is made here, not by
the orchestrating model.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable, List

from . import brief
from .delivery import method_for
from .gate import unlocked_by_spec
from .jobs import CLOSED, Job, remove_worktree
from .route import route
from .util import FactoryError, first_line, git, header_fields, mtime, section, sha256, write_text

MAX_STARTS = 2  # fresh agents per output file; each one is also reminded once before the job goes to a person
MAX_SPEC_REQUESTS = 2  # times per job the builder may send the spec back to the spec writer
AGENT_TYPES = {
    "intake": "factory:intake",
    "spec-writer": "factory:spec-writer",
    "builder": "factory:builder",
    "reviewer": "factory:reviewer",
    "approver": "factory:approver",
}


def step(job: Job) -> dict:
    handlers = {
        "intake": _intake, "spec": _spec, "build": _build, "gate": _gate, "review": _review,
        "approve": _approve, "delivering": _delivering, "needs-info": _parked, "needs-human": _parked,
    }
    if job.stage in CLOSED:
        return _closed(job)
    return handlers[job.stage](job)


# ---------------------------------------------------------------- helpers

def _action(job: Job, kind: str, summary: str, **extra) -> dict:
    out = {"job": job.id, "project": job.profile.name, "stage": job.stage, "round": job.round,
           "action": kind, "summary": f"{job.id}: {summary}"}
    out.update(extra)
    return out


def _fresh(job: Job, slot: str, path: Path) -> bool:
    """Written after we asked for it. Older files never count."""
    since = ((job.data.get("awaiting") or {}).get("since") or {}).get(slot)
    return since is not None and path.is_file() and mtime(path) >= since


def _set_aside(path: Path) -> None:
    """Move an existing output out of the way so only a newly written file can count."""
    if path.exists():
        path.rename(path.with_name(f"{path.name}.stale-{int(time.time() * 1000)}"))


class Spec:
    """One output file the stage needs, and how to get an agent to write it.

    `continues` names an earlier slot whose agent should carry on with this one
    if the orchestrator still has it (the builder, from round to round).
    """

    def __init__(self, slot: str, role: str, model: str, output: Path, make_brief: Callable[[], Path],
                 lens: str = None, set_aside: bool = True, continues: str = None):
        self.slot, self.role, self.model, self.output = slot, role, model, output
        self.make_brief, self.lens, self.set_aside, self.continues = make_brief, lens, set_aside, continues


def reminder(output: Path) -> str:
    return (f"You finished without writing your output file, so the factory couldn't see your work. Write it now "
            f"with the Write tool, to exactly this path:\n\n{output}\n\n"
            "Use the format your instructions give; a script reads the first line. Don't redo the work you "
            "already did. Then reply with one line.")


def _spawn(job: Job, key: str, specs: List[Spec], summary: str) -> dict:
    """Ask for each spec's output file: start an agent, or remind the one that forgot to write it.

    Per output file: start an agent; if it finishes without writing, remind that
    same agent once; if that fails too, start a fresh one and remind it once; then
    hand the job to a person.
    """
    attempts = job.data.setdefault("attempts", {})
    awaiting = job.data.get("awaiting") or {}
    if awaiting.get("key") != key:
        awaiting = {"key": key, "since": {}, "briefs": {}}
    briefs = awaiting.setdefault("briefs", {})
    entries, given_up = [], []
    for s in specs:
        tries = attempts.get(s.slot) or {}
        if isinstance(tries, int):  # job.json from before reminders existed
            tries = {"starts": tries, "reminders": tries}
        starts, reminders = tries.get("starts", 0), tries.get("reminders", 0)
        if starts and reminders < starts and s.slot in briefs:
            reminders += 1
            entry = _entry(job, s, briefs[s.slot])
            entry["continue"] = {"slot": s.slot, "message": reminder(s.output)}
        elif starts < MAX_STARTS:
            starts += 1
            if s.set_aside:
                _set_aside(s.output)
            briefs[s.slot] = str(s.make_brief())
            awaiting["since"][s.slot] = time.time()
            entry = _entry(job, s, briefs[s.slot])
            if s.continues:
                entry["continue"] = {"slot": s.continues, "message": entry["prompt"]}
        else:
            given_up.append(s)
            continue
        attempts[s.slot] = {"starts": starts, "reminders": reminders}
        entries.append(entry)
    job.data["awaiting"] = awaiting
    if given_up:
        names = ", ".join(s.output.name for s in given_up)
        return _park(job, f"{names}: agents finished {MAX_STARTS * 2} times without writing it, even when reminded",
                     retry=job.stage, keep_awaiting=True)
    job.save()
    return _action(job, "spawn", summary, agents=entries)


def _entry(job: Job, s: Spec, brief_path: str) -> dict:
    label = s.role if not s.lens else f"{s.lens} review"
    return {
        "slot": s.slot,
        "subagent_type": AGENT_TYPES[s.role],
        "model": s.model,
        "description": f"{job.id[:3]} {label}",
        "prompt": f"Brief: {brief_path}",
    }


def _park(job: Job, reason: str, retry: str = None, files=(), keep_awaiting: bool = False) -> dict:
    job.park(reason, retry=retry, files=files, keep_awaiting=keep_awaiting)
    return _parked(job)


def _head(job: Job) -> str:
    return git(job.worktree, "rev-parse", "HEAD") if job.worktree.is_dir() else ""


def _gated_head_or_park(job: Job):
    """None if HEAD is still the commit the gate passed; otherwise park and return the stop action."""
    gated = (job.data.get("gate") or {}).get("commit", "")
    head = _head(job)
    if head and head == gated:
        return None
    return _park(job, f"the branch moved after the gate passed (gated {gated[:12]}, now {head[:12] or 'missing'}); "
                      "what was reviewed is not what would merge", retry="gate")


def _model(job: Job, role: str) -> str:
    return job.profile.config["models"][role]


def _run(job: Job, command: str, summary: str) -> dict:
    seconds = job.profile.config["checks_timeout_s"] + 120
    return _action(job, "run", summary, command=command,
                   timeout_ms=min(seconds * 1000, 600000), background=seconds > 600)


# ---------------------------------------------------------------- stages

def _intake(job: Job) -> dict:
    out = job.file("intake.md")
    slot = f"intake-{job.next_index('reply')}"
    if not _fresh(job, slot, out):
        return _spawn(job, slot, [Spec(slot, "intake", _model(job, "intake"), out, lambda: brief.intake_brief(job))],
                      "intake: deciding whether and how to build it")
    fields = header_fields(out)
    decision = fields.get("DECISION", "").upper()
    risk = fields.get("RISK", "").lower()
    raw_surfaces = fields.get("SURFACES", "none")
    surfaces = [] if raw_surfaces.lower() in ("", "none") else \
        [s.strip().lower() for s in raw_surfaces.split(",") if s.strip()]
    job.data["intake"] = {"decision": decision, "risk": risk, "surfaces": surfaces,
                          "rescoped": fields.get("RESCOPED", "").lower() == "yes"}
    if decision == "ACCEPT":
        try:
            job.data["agents"] = route(job.profile, risk, surfaces)
        except FactoryError as e:
            return _park(job, f"intake.md can't be routed: {e}", retry="intake", files=["intake.md"])
        lenses = job.data["agents"]["lenses"]
        job.set_stage("spec", f"accepted, risk {risk}, reviewers {', '.join(lenses)}")
        return _spec(job)
    if decision == "REJECT":
        remove_worktree(job)
        job.set_stage("rejected", "intake rejected the request")
        return _closed(job)
    if decision == "NEEDS-INFO":
        job.set_stage("needs-info", "intake has questions for the requester")
        return _parked(job)
    return _park(job, f"intake.md doesn't start with a DECISION line (got {first_line(out)!r})",
                 retry="intake", files=["intake.md"])


def _continuing(job: Job, role: str, slot: str):
    """The slot this role's agent last worked in, so the same agent can carry on into `slot`."""
    previous = (job.data.get("last_slot") or {}).get(role)
    job.data.setdefault("last_slot", {})[role] = slot
    return previous if previous != slot else None


def _spec(job: Job) -> dict:
    """Write the spec, or revise it: after a builder's spec-change request, or a person's rework note."""
    out = job.file("spec.md")
    version = job.data.get("spec_version") or (1 if job.round >= 1 else 0)
    reason = job.data.get("spec_reason") or {}
    slot = f"spec-v{version + 1}"
    if not _fresh(job, slot, out):
        writing = "writing spec.md" if not reason else f"revising spec.md ({reason['kind']})"
        return _spawn(job, slot, [Spec(slot, "spec-writer", _model(job, "spec-writer"), out,
                                       lambda: brief.spec_brief(job), set_aside=not reason,
                                       continues=_continuing(job, "spec-writer", slot))], f"spec: {writing}")
    text = out.read_text()
    if not re.search(r"^##\s+Acceptance criteria", text, re.M | re.I):
        return _park(job, "spec.md has no `## Acceptance criteria` section", retry="spec", files=["spec.md"])
    job.data["spec_version"] = version + 1
    job.data["spec_hash"] = sha256(out)
    job.data["unlocked"] = unlocked_by_spec(job, text)
    job.data["spec_reason"] = None
    if reason.get("kind") == "builder":  # same round: the builder carries on against the revised spec
        job.data["build_reason"] = {"kind": "spec-revised", "round": job.round}
    elif reason.get("kind") == "rework":
        job.data["build_reason"] = {"kind": "rework", "round": job.round - 1, "file": reason["file"]}
    else:
        job.data["round"] = 1
        job.data["build_reason"] = {"kind": "first", "round": 0}
    unlocked = job.data["unlocked"]
    job.set_stage("build", f"spec v{version + 1}" + (f", unlocks {', '.join(unlocked)}" if unlocked else ""))
    return _build(job)


def _build(job: Job) -> dict:
    out = job.file("build.md")
    slot = f"builder-r{job.round}-s{job.data.get('spec_version') or 1}"
    if not _fresh(job, slot, out):
        model = job.data["agents"]["builder"]
        return _spawn(job, slot, [Spec(slot, "builder", model, out, lambda: brief.builder_brief(job),
                                       set_aside=False, continues=_continuing(job, "builder", slot))],
                      f"round {job.round}: building ({model})")
    status = first_line(out)
    if status == "STATUS: READY":
        job.set_stage("gate", f"build round {job.round} ready")
        return _gate(job)
    if status == "STATUS: SPEC-CHANGE":
        asked = job.data.get("spec_requests", 0)
        if asked >= MAX_SPEC_REQUESTS:
            return _park(job, f"the builder asked for a spec change {asked + 1} times; read the "
                              "`## Spec change` section of build.md", retry="build", files=["build.md", "spec.md"])
        job.data["spec_requests"] = asked + 1
        job.data["spec_reason"] = {"kind": "builder", "round": job.round}
        job.set_stage("spec", "the builder asked for a spec change")
        return _spec(job)
    if status == "STATUS: BLOCKED":
        why = section(out.read_text(), "Blocked") or "see build.md"
        return _park(job, f"the builder is blocked: {why}", retry="build", files=["build.md"])
    return _park(job, f"build.md doesn't start with a STATUS line (got {status!r})", retry="build", files=["build.md"])


def _gate(job: Job) -> dict:
    result = job.data.get("gate") or {}
    if result.get("round") != job.round or not result.get("verdict"):
        return _run(job, f"factory gate {job.id} --project {job.profile.name}",
                    f"round {job.round}: running the gate (checks.sh)")
    if result.get("tampered"):
        return _park(job, " and ".join(result["tampered"]) + " changed after the job started relying on it "
                     "(the profile's checks.sh or config.json, or the accepted spec.md). If a person made that "
                     f"change on purpose, run `bin/factory trust {job.id}`, then retry.",
                     retry="gate", files=[f"round-{job.round}/checks.md"])
    if result["verdict"] == "PASS":
        lenses = job.data["agents"]["lenses"]
        job.data["reviews"] = {lens: "pending" for lens in lenses}
        job.set_stage("review", f"gate passed round {job.round}")
        return _review(job)
    return _next_round(job, "gate-failed", f"the gate failed round {job.round}")


def _review(job: Job) -> dict:
    lenses = job.data["agents"]["lenses"]
    key = f"review-r{job.round}"
    verdicts, missing = {}, []
    for lens, model in lenses.items():
        path = job.round_dir() / f"review-{lens}.md"
        slot = f"review-{lens}-r{job.round}"
        if not _fresh(job, slot, path):
            missing.append(Spec(slot, "reviewer", model, path, lambda lens=lens: brief.reviewer_brief(job, lens), lens))
            continue
        m = re.match(r"^VERDICT:\s*(PASS|CHANGES)$", first_line(path))
        if not m:
            return _park(job, f"review-{lens}.md doesn't start with `VERDICT: PASS` or `VERDICT: CHANGES`",
                         retry="review", files=[f"round-{job.round}/review-{lens}.md"])
        verdicts[lens] = m.group(1)
    job.data["reviews"] = {lens: verdicts.get(lens, "pending") for lens in lenses}
    if missing:
        names = ", ".join(f"{s.lens} ({s.model})" for s in missing)
        return _spawn(job, key, missing, f"round {job.round}: reviewing in parallel: {names}")
    job.save()
    moved = _gated_head_or_park(job)
    if moved:
        return moved
    changes = [lens for lens, v in verdicts.items() if v == "CHANGES"]
    if not changes:
        job.set_stage("approve", f"round {job.round}: all reviews PASS")
        return _approve(job)
    return _next_round(job, "changes", f"round {job.round}: {', '.join(changes)} asked for changes")


def _next_round(job: Job, kind: str, why: str) -> dict:
    limit = job.data["round_limit"]
    if job.round >= limit and kind == "approver":  # keep the approver's own decision.md: it says what to change
        return _park(job, f"{why}, but that was the last round this job had ({job.round} of {limit}). "
                          "decision.md lists the changes.", files=["decision.md"])
    if job.round >= limit:
        write_text(job.file("decision.md"), (
            "ESCALATE\n\n"
            f"## Why\n{why}, and that was the last round this job had ({job.round} of {limit}).\n\n"
            "## For the human\n"
            f"Read round-{job.round}/ (checks.md and the reviews) and build.md. Then send it back with guidance "
            f"(`bin/factory rework {job.id} \"<note>\"`), accept it as it is and deliver it "
            f"(`bin/factory approve {job.id}`), or drop it (`bin/factory abandon {job.id}`).\n"
        ))
        return _park(job, f"{why}; no rounds left ({job.round} of {limit})",
                     files=["decision.md", f"round-{job.round}/"])
    job.data["round"] = job.round + 1
    job.data["build_reason"] = {"kind": kind, "round": job.round - 1}
    job.set_stage("build", why)
    return _build(job)


def _approve(job: Job) -> dict:
    out = job.file("decision.md")
    slot = f"approver-r{job.round}"
    if not _fresh(job, slot, out):
        return _spawn(job, slot, [Spec(slot, "approver", _model(job, "approver"), out,
                                       lambda: brief.approver_brief(job))], "approve: final judgment")
    line = first_line(out).upper()
    if line == "APPROVE":
        moved = _gated_head_or_park(job)
        if moved:
            return moved
        job.set_stage("delivering", "approver: APPROVE")
        return _delivering(job)
    if line == "CHANGES":
        return _next_round(job, "approver", f"round {job.round}: the approver asked for changes")
    if line == "ESCALATE":
        text = out.read_text()
        why = section(text, "For the human") or section(text, "Why") or "see decision.md"
        return _park(job, f"the approver escalated: {why}", files=["decision.md"])
    return _park(job, f"decision.md doesn't start with APPROVE, CHANGES or ESCALATE (got {line!r})",
                 retry="approve", files=["decision.md"])


def _delivering(job: Job) -> dict:
    method, _ = method_for(job.profile)
    return _run(job, f"factory deliver {job.id} --project {job.profile.name}",
                f"approved; delivering with `{method.name}` ({method.about})")


def _parked(job: Job) -> dict:
    jid = job.id
    if job.stage == "needs-info":
        questions = section(job.file("intake.md").read_text(), "Questions") if job.file("intake.md").is_file() else ""
        tell = (f"{jid} needs answers before it can be specified:\n\n{questions or '(see intake.md)'}\n\n"
                f"Answer with `bin/factory reply {jid} \"<answers>\"`, then continue with "
                f"`bin/factory run --job {jid}`.")
        return _action(job, "stop", "waiting for answers from the requester", tell_user=tell)
    info = job.data.get("needs_human") or {}
    options = []
    if info.get("retry"):
        options.append(f"`bin/factory retry {jid}` to retry the {info['retry']} step")
    if job.file("spec.md").is_file():
        options.append(f"`bin/factory rework {jid} \"<what to change>\"` to send it back (the spec is updated "
                       "to match your note, then the builder takes it)")
    if job.round >= 1:
        method, _ = method_for(job.profile)
        options.append(f"`bin/factory approve {jid}` to accept it as it is and deliver it (`{method.name}`)")
    options.append(f"`bin/factory abandon {jid}` to drop it and delete its branch")
    files = "\n".join(f"  {job.dir / f}" for f in info.get("files") or [])
    tell = (f"{jid} needs you: {info.get('reason', 'see events.log')}\n\n"
            f"Job folder: {job.dir}\nWorktree: {job.worktree}\n" + (f"Read:\n{files}\n" if files else "")
            + "\nOptions (in a terminal, from the factory folder):\n" + "\n".join(f"- {o}" for o in options))
    return _action(job, "stop", f"needs a human: {info.get('reason', '')[:160]}", tell_user=tell)


def _closed(job: Job) -> dict:
    if job.stage == "delivered":
        record = job.data.get("delivery") or {}
        tell = record.get("next_steps", "")
        if record.get("reopenable"):
            tell += (("\n\n" if tell else "") + "If a later review asks for changes, send them back through the "
                     f"factory with `bin/factory rework {job.id} \"<what to change>\"`.")
        return _action(job, "done", record.get("summary", "delivered"), **({"tell_user": tell} if tell else {}))
    if job.stage == "rejected":
        why = section(job.file("intake.md").read_text(), "Why") if job.file("intake.md").is_file() else ""
        summary = "rejected at intake" + (f": {why[:300]}" if why else "")
    else:
        summary = job.stage
    return _action(job, "done", summary)
