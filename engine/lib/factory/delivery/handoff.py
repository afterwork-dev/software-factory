"""handoff: the job ends as an approved branch, and a person takes it from there.

Nothing is pushed or merged. The worktree is removed and the branch stays in
the project's repo at exactly the reviewed commit, ready to push and take
through the project's own process: a pull request, usually. If that process
asks for changes, `factory rework` reopens the job on the same branch.
"""

from __future__ import annotations

import shlex

from ..jobs import Job, remove_worktree
from ..util import run, section
from .base import Method, Option, checked_head


class Handoff(Method):
    name = "handoff"
    about = "stop at an approved branch; a person pushes it and opens a pull request (or merges it) the project's way"
    reopenable = True
    options = {
        "next_steps": Option("", about="what to tell the person once a branch is handed off, "
                                       "e.g. this project's pull-request routine; {branch}, {base}, "
                                       "{repo} and {job} are filled in"),
    }

    def approve_meaning(self, job: Job, opts: dict) -> str:
        return ("In this project APPROVE hands the branch to a person, who reviews it in a pull request (or the "
                "project's own process) before it merges. So a change in a sensitive area (sign-in, permissions, "
                "payments, personal data, dependencies, build or deploy configuration, schemas) is not by itself a "
                "reason to escalate: approve it and point the reviewer at it under `## For the reviewer`, together "
                "with anything you couldn't verify. Keep the same bar for quality as if it merged right away: "
                "anything an agent can fix is CHANGES, not a note.")

    def deliver(self, job: Job, opts: dict) -> dict:
        head = checked_head(job)
        remove_worktree(job, keep_branch=True)
        repo, base = job.profile.repo_path, job.base_branch
        if opts["next_steps"].strip():
            steps = opts["next_steps"].format(branch=job.branch, base=base, repo=repo, job=job.id)
        elif run(["git", "remote", "get-url", "origin"], cwd=repo, check=False).returncode == 0:
            steps = (f"Push it and open a pull request into {base}:\n"
                     f"  git -C {shlex.quote(str(repo))} push -u origin {job.branch}")
        else:
            steps = f"Merge or push {job.branch} the way this project expects."
        decision = job.file("decision.md")
        notes = section(decision.read_text(), "For the reviewer") if decision.is_file() else ""
        if notes and notes.lower().rstrip(".") not in ("none", "nothing"):
            steps += f"\n\nThe approver asks whoever reviews it to look at:\n{notes}"
        return {
            "method": self.name,
            "branch": job.branch,
            "commit": head,
            "base_branch": base,
            "summary": f"approved and handed off as branch {job.branch} at {head[:12]}. Nothing was pushed or merged.",
            "next_steps": steps,
        }
