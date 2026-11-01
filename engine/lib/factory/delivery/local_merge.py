"""local-merge: merge the approved branch into the base branch, locally. Opt-in.

Only for projects that allow landing on the base branch without a pull request.
Nothing is pushed.

1. Bring the job branch up to date with the base branch inside the job's
   worktree (merge, or rebase for strategy "rebase").
2. Gate again if that changed the branch, because the base branch may have
   moved since the branch was tested.
3. Build the final commit from the gated tree: a merge commit, a squash
   commit, or the rebased tip.
4. Move the base branch to it. If the base branch is checked out (your main
   checkout, usually), fast-forward it there; that needs no uncommitted
   changes to tracked files. Otherwise update the ref atomically.
5. Remove the worktree and the job branch.
"""

from __future__ import annotations

from .. import gate as gates
from ..jobs import Job, remove_worktree
from ..util import checkout_of, git, is_ancestor, run
from .base import Blocked, Method, Option, checked_head, passed_commits


class LocalMerge(Method):
    name = "local-merge"
    about = "merge into the base branch locally with no person looking (only where that is allowed); never pushes"
    reopenable = False
    options = {
        "strategy": Option("merge", choices=("merge", "squash", "rebase"),
                           about="merge commit, one squashed commit, or a rebased linear history"),
    }

    def approve_meaning(self, job: Job, opts: dict) -> str:
        return (f"In this project APPROVE merges the branch into `{job.base_branch}` locally, and no person reads "
                "it first. So besides your usual reasons, ESCALATE any change in a sensitive area: sign-in, "
                "permissions, payments, personal data, dependencies, build or deploy configuration, schemas or "
                "stored data, messages to real people. Approve only what you would merge yourself.")

    def deliver(self, job: Job, opts: dict) -> dict:
        strategy = opts["strategy"]
        repo, wt, base = job.profile.repo_path, job.worktree, job.base_branch
        base_ref = f"refs/heads/{base}"
        checked_head(job)
        passed = passed_commits(job)
        base_tip = git(repo, "rev-parse", "--verify", f"{base_ref}^{{commit}}")

        # 1. sync with the base branch
        if not is_ancestor(wt, base_tip, "HEAD"):
            if strategy == "rebase":
                cmd, abort = ["git", "rebase", base_tip], ["git", "rebase", "--abort"]
            else:
                cmd = ["git", "merge", "--no-ff", "--no-edit", "-m", f"Merge {base} into {job.branch}", base_tip]
                abort = ["git", "merge", "--abort"]
            res = run(cmd, cwd=wt, check=False)
            if res.returncode != 0:
                run(abort, cwd=wt, check=False)
                raise Blocked(f"bringing the branch up to date with {base} conflicts:\n"
                              f"{(res.stdout + res.stderr).strip()[-1500:]}")
            job.event(f"synced with {base} at {base_tip[:12]}")
        head = git(wt, "rev-parse", "HEAD")

        # 2. gate again unless this exact commit already passed with the current profile
        if head not in passed or gates.tampered(job):
            if gates.gate(job, mode="merge")["verdict"] != "PASS":
                raise Blocked(f"the gate failed after syncing with {base}; see merge-checks.md")

        # 3. the final commit, from exactly the gated tree
        tree = git(wt, "rev-parse", "HEAD^{tree}")
        if strategy == "merge":
            final = git(repo, "commit-tree", tree, "-p", base_tip, "-p", head,
                        "-m", f"Merge {job.branch}: {job.title}")
        elif strategy == "squash":
            final = git(repo, "commit-tree", tree, "-p", base_tip,
                        "-m", f"{job.title}\n\nSquashed from {job.branch} (factory job {job.id}).")
        else:
            final = head

        # 4. move the base branch
        checkout = checkout_of(repo, base)
        if checkout is not None:
            if git(checkout, "status", "--porcelain", "--untracked-files=no"):
                raise Blocked(f"{base} is checked out at {checkout} with uncommitted changes. Commit or stash "
                              f"them, then run `bin/factory retry {job.id}`. The job is approved and gated.")
            res = run(["git", "merge", "--ff-only", "--quiet", final], cwd=checkout, check=False)
            if res.returncode != 0:
                raise Blocked(f"fast-forwarding {base} at {checkout} failed: "
                              f"{(res.stderr or res.stdout).strip()[-800:]}")
        else:
            res = run(["git", "update-ref", "-m", f"factory: merge {job.id}", base_ref, final, base_tip],
                      cwd=repo, check=False)
            if res.returncode != 0:
                raise Blocked(f"{base} moved while merging; run `bin/factory retry {job.id}` to try again")

        # 5. clean up
        remove_worktree(job)
        return {
            "method": self.name,
            "strategy": strategy,
            "commit": final,
            "base_branch": base,
            "base_before": base_tip,
            "summary": f"merged into {base} as {final[:12]} ({strategy}). Nothing was pushed.",
        }
