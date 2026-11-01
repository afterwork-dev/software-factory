"""github-pr: push the approved branch and open a GitHub pull request. Opt-in.

For projects where every change goes through a pull request anyway. Only a
job the approver approved is pushed; a job a person accepts after an
escalation (`bin/factory approve`) is handed off like `handoff`, with the
commands to push it and open the pull request.

1. Check that `gh` is installed and signed in, and that `remote` is one of
   the repo's remotes.
2. Push the branch to it. Never rewriting: a push the remote rejects parks
   the job.
3. Reuse the branch's open pull request (after a rework), or open one into
   the base branch, titled with the job title, its body taken from
   decision.md.
4. Remove the worktree and keep the branch, so `rework` can reopen the job.

Every step that fails raises Blocked and leaves the worktree as it is, so
`bin/factory retry` can run the delivery again. Commands get their arguments
as a list, with values in `--flag=value` form, and never wait for input.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil

from ..jobs import Job, remove_worktree
from ..util import run, section
from .base import Blocked, Method, Option, checked_head, reviewer_notes


class GitHubPR(Method):
    name = "github-pr"
    about = ("push the branch and open a GitHub pull request when the approver approves; a job a person accepts "
             "after an escalation is handed off without pushing")
    reopenable = True
    options = {
        "remote": Option("origin", kind=str, about="the git remote to push the branch to"),
        "draft": Option(False, kind=bool, about="open the pull request as a draft"),
    }

    def approve_meaning(self, job: Job, opts: dict) -> str:
        return (f"In this project APPROVE pushes the branch to `{opts['remote']}` and opens a pull request into "
                f"`{job.base_branch}`; the project's own review and branch protection decide the merge. A person "
                "reviews it there, so a change in a sensitive area (sign-in, permissions, payments, personal data, "
                "dependencies, build or deploy configuration, schemas) is not by itself a reason to escalate: "
                "approve it and point the reviewer at it under `## For the reviewer`, together with anything you "
                "couldn't verify. Keep the same bar for quality as if it merged right away: anything an agent can "
                "fix is CHANGES, not a note.")

    def deliver(self, job: Job, opts: dict) -> dict:
        head = checked_head(job)
        repo, base, branch, remote = job.profile.repo_path, job.base_branch, job.branch, opts["remote"]
        record = {"method": self.name, "branch": branch, "commit": head, "base_branch": base, "remote": remote}
        notes = reviewer_notes(job)
        notes_text = f"\n\nThe approver asks whoever reviews it to look at:\n{notes}" if notes else ""

        if job.data.get("approved_by_person"):
            remove_worktree(job, keep_branch=True)
            q = shlex.quote
            create = f"cd {q(str(repo))} && gh pr create --head {q(branch)} --base {q(base)} --fill"
            steps = (f"Push it and open a pull request into {base}:\n"
                     f"  git -C {q(str(repo))} push -u {q(remote)} {q(branch)}\n"
                     f"  {create}{' --draft' if opts['draft'] else ''}")
            record.update(
                pushed=False,
                summary=(f"accepted by a person and handed off as branch {branch} at {head[:12]}. Nothing was "
                         "pushed; github-pr opens pull requests only for jobs the approver approves."),
                next_steps=steps + notes_text,
            )
            return record

        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GH_PROMPT_DISABLED="1")

        def tool(*cmd):
            return run(list(cmd), cwd=repo, check=False, env=env)

        def output(res) -> str:
            return (res.stdout + res.stderr).strip()[-1500:]

        if shutil.which("gh") is None:
            raise Blocked("the GitHub CLI `gh` isn't installed or not on PATH; install it and sign in with "
                          f"`gh auth login`, then run `bin/factory retry {job.id}`")
        res = tool("gh", "auth", "status")
        if res.returncode != 0:
            raise Blocked(f"`gh` isn't signed in; run `gh auth login`, then `bin/factory retry {job.id}`:\n"
                          f"{output(res)}")
        res = tool("git", "remote")
        if res.returncode != 0 or remote not in res.stdout.split():
            raise Blocked(f"there is no remote named {remote!r} in {repo}; fix delivery.remote in the profile, "
                          f"then run `bin/factory retry {job.id}`")
        res = tool("git", "push", "-u", remote, branch)
        if res.returncode != 0:
            raise Blocked(f"the push of {branch} to {remote} was rejected or failed:\n{output(res)}")

        url = ""
        res = tool("gh", "pr", "view", branch, "--json", "url,state")
        if res.returncode == 0:
            try:
                pr = json.loads(res.stdout)
            except ValueError:
                pr = None
            if isinstance(pr, dict) and pr.get("state") == "OPEN" and pr.get("url"):
                url = str(pr["url"])
        reused = bool(url)
        if not reused:
            cmd = ["gh", "pr", "create", f"--head={branch}", f"--base={base}", f"--title={job.title}",
                   f"--body={self.body(job)}"]
            if opts["draft"]:
                cmd.append("--draft")
            res = tool(*cmd)
            lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
            if res.returncode != 0 or not lines or not lines[-1].startswith("http"):
                raise Blocked(f"`gh pr create` failed:\n{output(res)}")
            url = lines[-1]

        remove_worktree(job, keep_branch=True)
        record.update(
            pushed=True,
            pr_url=url,
            summary=(f"approved; pushed {branch} at {head[:12]} to {remote} and "
                     f"{'updated' if reused else 'opened'} pull request {url}"),
            next_steps=f"Review and merge the pull request: {url}" + notes_text,
        )
        return record

    def body(self, job: Job) -> str:
        parts = [f"Opened by software-factory job {job.id} after its gate, reviews and approval passed."]
        decision = job.file("decision.md")
        text = decision.read_text() if decision.is_file() else ""
        for heading in ("Why", "Acceptance criteria", "For the reviewer"):
            content = reviewer_notes(job) if heading == "For the reviewer" else section(text, heading)
            if content:
                parts.append(f"## {heading}\n{content}")
        return "\n\n".join(parts)
