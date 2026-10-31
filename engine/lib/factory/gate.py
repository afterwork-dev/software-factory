"""The gate: does the job's branch pass the project's own checks?

It runs the profile's checks.sh, which lives outside every worktree, inside the
job's worktree. A branch can't edit the checks it is judged by, and if the
profile's checks.sh or config.json change while a job runs, the gate refuses
to pass until a person re-accepts them with `factory trust`.

It also fails a branch that modifies, renames or deletes an existing file
matching `protected_paths` (adding new files there is fine), a worktree with
uncommitted changes, and a worktree on the wrong branch.

The one way to change a protected file: the spec names it under
`## Protected changes`, because the requested behaviour makes an existing test
wrong. The spec writer decides that before any code exists; the builder can't,
and if spec.md changes after it was accepted the gate refuses to pass.
Profiles can switch this off with `spec_may_unlock_protected: false`.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
import time
from pathlib import Path
from typing import List, Optional, Tuple

from .jobs import Job, env_for, profile_hashes
from .profile import Profile
from .util import FactoryError, git, now, section, sha256, write_text

TAIL_LINES = 60


def matches_protected(path: str, patterns: List[str]) -> bool:
    """fnmatch patterns, where `*` also matches `/`; a trailing `/` means everything under that folder."""
    for pattern in patterns:
        if pattern.endswith("/"):
            if path.startswith(pattern):
                return True
        elif fnmatch.fnmatchcase(path, pattern):
            return True
    return False


def unlocked_by_spec(job: Job, spec_text: str) -> List[str]:
    """Protected paths the spec lists under `## Protected changes` (as `path` in backticks)."""
    if not job.profile.config.get("spec_may_unlock_protected", True):
        return []
    named = re.findall(r"^\s*(?:[-*]|\d+\.)\s*`([^`]+)`", section(spec_text, "Protected changes"), re.M)
    paths = {n.strip()[2:] if n.strip().startswith("./") else n.strip() for n in named}
    return sorted(p for p in paths if matches_protected(p, job.profile.config["protected_paths"]))


def protected_changes(job: Job) -> Tuple[List[str], List[str]]:
    """(blocked, unlocked): existing protected files the branch changed, as `<status> <path>` lines."""
    patterns = job.profile.config["protected_paths"]
    if not patterns:
        return [], []
    unlocked = set(job.data.get("unlocked") or [])
    diff = git(job.worktree, "diff", "--name-status", "-M", f"{job.base_branch}...HEAD")
    blocked, allowed = [], []
    for line in diff.splitlines():
        status, *paths = line.split("\t")
        if status[:1] in ("A", "C"):  # new files, and copies that leave the original alone
            continue
        if paths and matches_protected(paths[0], patterns):
            entry = f"{status[0]} {paths[0]}" + (f" -> {paths[1]}" if len(paths) > 1 else "")
            (allowed if paths[0] in unlocked else blocked).append(entry)
    return blocked, allowed


def untracked(worktree: Path) -> List[str]:
    out = git(worktree, "status", "--porcelain", "--untracked-files=all")
    return [line[3:] for line in out.splitlines() if line.startswith("?? ")]


def worktree_problems(job: Job, ignore_untracked=()) -> List[str]:
    wt = job.worktree
    if not wt.is_dir():
        return [f"the worktree {wt} is missing"]
    problems = []
    branch = git(wt, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != job.branch:
        problems.append(f"the worktree is on `{branch}`, not `{job.branch}`")
    status = git(wt, "status", "--porcelain", "--untracked-files=all")
    dirty = [line for line in status.splitlines()
             if not (line.startswith("?? ") and line[3:] in ignore_untracked)]
    if dirty:
        listing = "\n".join(f"  {line}" for line in dirty[:20])
        more = f"\n  … and {len(dirty) - 20} more" if len(dirty) > 20 else ""
        problems.append(f"the worktree has uncommitted or untracked changes:\n{listing}{more}")
    return problems


def tampered(job: Job) -> List[str]:
    """Files the job relies on that changed since: the profile's checks and config, and the accepted spec."""
    recorded = job.data.get("profile_hash") or {}
    current = profile_hashes(job.profile)
    changed = [name for name, digest in current.items() if recorded.get(name) != digest]
    if job.data.get("spec_hash") and sha256(job.file("spec.md")) != job.data["spec_hash"]:
        changed.append("spec.md")
    return changed


def run_checks(profile: Profile, cwd: Path, log_path: Path, job: Optional[Job]) -> Tuple[int, int]:
    """(exit code, seconds). Output goes to log_path."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    with open(log_path, "w") as log:
        try:
            res = subprocess.run(["bash", str(profile.checks)], cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                 timeout=profile.config["checks_timeout_s"], env=env_for(job, profile, cwd))
            code = res.returncode
        except subprocess.TimeoutExpired:
            log.write(f"\n[factory] checks.sh timed out after {profile.config['checks_timeout_s']}s\n")
            code = 124
    return code, int(time.monotonic() - start)


def tail(path: Path, lines: int = TAIL_LINES) -> str:
    text = path.read_text(errors="replace") if path.is_file() else ""
    return "\n".join(text.rstrip().splitlines()[-lines:])


def gate(job: Job, mode: str = "round") -> dict:
    """mode: `round` (record in job.json and round-N/checks.md), `merge` (re-gate before a local merge,
    merge-checks.md), `dry` (record nothing)."""
    if mode == "round" and job.stage != "gate":
        raise FactoryError(f"{job.id} is in {job.stage}, not gate")
    if mode == "merge" and job.stage != "delivering":
        raise FactoryError(f"{job.id} is in {job.stage}, not delivering")
    if mode == "round" and job.round < 1:
        raise FactoryError(f"{job.id} hasn't built anything yet")

    if mode == "round":
        out_dir, name = job.round_dir(), "checks"
    elif mode == "merge":
        out_dir, name = job.dir, "merge-checks"
    else:
        out_dir, name = job.dir / "logs", "dry-checks"
    log_path = out_dir / f"{name}.log"

    leftovers = (job.data.get("gate") or {}).get("leftovers", [])
    problems = [] if mode == "dry" else worktree_problems(job, leftovers)
    changed_profile = tampered(job)
    commit = git(job.worktree, "rev-parse", "HEAD") if job.worktree.is_dir() else ""
    protected, unlocked = protected_changes(job) if commit else ([], [])
    before = set(untracked(job.worktree)) if commit else set()

    if changed_profile:
        code, seconds = None, 0
        write_text(log_path, "checks.sh was not run: the profile changed since the job started.\n")
    elif not commit:
        code, seconds = None, 0
        write_text(log_path, "checks.sh was not run: the worktree is missing.\n")
    else:
        code, seconds = run_checks(job.profile, job.worktree, log_path, job)

    new_leftovers = sorted(set(untracked(job.worktree)) - before) if commit else []
    passed = code == 0 and not problems and not protected and not changed_profile
    verdict = "PASS" if passed else "FAIL"
    result = {
        "verdict": verdict,
        "round": job.round,
        "commit": commit,
        "exit": code,
        "seconds": seconds,
        "problems": problems,
        "protected": protected,
        "unlocked": unlocked,
        "tampered": changed_profile,
        "leftovers": sorted(set(leftovers) | set(new_leftovers)),
        "at": now(),
    }

    report = [f"GATE: {verdict}", f"COMMIT: {commit or 'none'}", f"ROUND: {job.round}", ""]
    if changed_profile:
        report += ["## Profile changed",
                   "These profile files changed since the job started, so the checks can't be trusted: "
                   + ", ".join(changed_profile)
                   + f". If a person made that change, they accept it with `bin/factory trust {job.id}`.", ""]
    report += ["## Worktree", "\n".join(f"- {p}" for p in problems) if problems else "Clean, on the job branch.", ""]
    report += ["## Protected paths",
               "\n".join(f"- {p}" for p in protected) if protected else "No existing protected file was changed "
               "without the spec's permission.", ""]
    if unlocked:
        report += ["## Changed with the spec's permission",
                   "These protected files are listed under `## Protected changes` in spec.md. Code review checks "
                   "the changes match what the spec says:\n" + "\n".join(f"- {p}" for p in unlocked), ""]
    exit_text = "not run" if code is None else f"exit {code} in {seconds}s"
    report += ["## checks.sh", f"{exit_text}. Last {TAIL_LINES} lines of {log_path.name}:", "",
               "```", tail(log_path), "```", ""]
    if new_leftovers:
        report += ["## Note", "checks.sh left untracked files behind (ignored by later gates): "
                   + ", ".join(new_leftovers[:20]), ""]
    write_text(out_dir / f"{name}.md", "\n".join(report))

    if mode == "round":
        job.data["gate"] = result
        job.event(f"gate round {job.round}: {verdict} ({exit_text})")
        job.save()
    elif mode == "merge":
        job.data["merge_gate"] = result
        job.event(f"merge gate: {verdict} ({exit_text})")
        job.save()
    return result


def summary_line(job: Job, result: dict) -> str:
    bits = [f"GATE: {result['verdict']}", f"{job.id} round {result['round']}"]
    bits.append("checks.sh not run" if result["exit"] is None else f"checks.sh exit {result['exit']} in {result['seconds']}s")
    if result["protected"]:
        bits.append("protected files changed: " + ", ".join(result["protected"]))
    if result["tampered"]:
        bits.append("profile changed: " + ", ".join(result["tampered"]))
    if result["problems"]:
        bits.append(result["problems"][0].splitlines()[0])
    return " | ".join(bits)
