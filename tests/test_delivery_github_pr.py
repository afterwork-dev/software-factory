"""Tests for the github-pr delivery method, against a local bare remote and a fake `gh`.

Run with:
    python3 -m unittest discover -s tests -v
"""

import json
import os
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine" / "lib"))

from factory import human  # noqa: E402
from factory.delivery import deliver  # noqa: E402
from factory.gate import gate  # noqa: E402
from factory.step import step  # noqa: E402

from test_factory import DeliveryCases, Sandbox, sh  # noqa: E402

PR_URL = "https://github.com/acme/app/pull/1"

FAKE_GH = """#!{python}
import json, sys
from pathlib import Path

state = Path({state!r})
args = sys.argv[1:]
with open(state / "calls.jsonl", "a") as f:
    f.write(json.dumps(args) + "\\n")
if args[:2] == ["auth", "status"]:
    if (state / "auth-fails").exists():
        sys.stderr.write("You are not logged into any GitHub hosts.\\n")
        sys.exit(1)
    print("Logged in to github.com as acme")
elif args[:2] == ["pr", "view"]:
    if not (state / "pr-created").exists():
        sys.stderr.write("no pull requests found for branch\\n")
        sys.exit(1)
    print(json.dumps({{"url": {url!r}, "state": "OPEN"}}))
elif args[:2] == ["pr", "create"]:
    if (state / "create-fails").exists():
        sys.stderr.write("GraphQL: something went wrong\\n")
        sys.exit(1)
    (state / "pr-created").write_text("")
    print("Creating pull request\\n" + {url!r})
else:
    sys.stderr.write("fake gh: unexpected arguments\\n")
    sys.exit(2)
"""


class GitHubPR(DeliveryCases, Sandbox):
    delivery = {"method": "github-pr"}

    def setUp(self):
        super().setUp()
        self.origin = self.tmp / "origin.git"
        sh(self.tmp, "git", "clone", "-q", "--bare", str(self.repo), str(self.origin))
        sh(self.repo, "git", "remote", "set-url", "origin", str(self.origin))
        sh(self.repo, "git", "fetch", "-q", "origin")
        self.gh_state = self.tmp / "gh"
        bindir = self.tmp / "bin"
        self.gh_state.mkdir()
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text(FAKE_GH.format(python=sys.executable, state=str(self.gh_state), url=PR_URL))
        gh.chmod(0o755)
        os.environ["PATH"] = f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"

    def gh_calls(self):
        log = self.gh_state / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.is_file() else []

    def pr_creates(self):
        return [c for c in self.gh_calls() if c[:2] == ["pr", "create"]]

    def remote_ref(self, branch):
        res = sh(self.origin, "git", "for-each-ref", "--format=%(objectname)", f"refs/heads/{branch}")
        return res or None

    def approved_with_notes(self):
        job = self.new_job()
        self.to_review(job)
        self.review(job, "code")
        step(job)
        self.write(job.file("decision.md"), "APPROVE\n\n## Why\nSubtract is small and tested.\n\n"
                                            "## Acceptance criteria\n1. [x] subtract works\n\n"
                                            "## For the reviewer\n- Nobody checked negative numbers.\n\n"
                                            "## For the human\nNothing.\n")
        step(job)
        self.assertEqual(job.stage, "delivering")
        return job

    def assert_parked(self, job, result):
        self.assertTrue(result.startswith("BLOCKED:"), result)
        job = self.reload(job)
        self.assertEqual(job.stage, "needs-human")
        self.assertEqual(job.data["needs_human"]["retry"], "delivering")
        self.assertTrue(job.worktree.is_dir())
        return job

    def test_approver_approval_pushes_and_opens_a_pull_request(self):
        job = self.approved_with_notes()
        head = sh(job.worktree, "git", "rev-parse", "HEAD")
        self.assertTrue(deliver(job).startswith("DELIVERED"))
        self.assertEqual(self.remote_ref(job.branch), head)
        creates = self.pr_creates()
        self.assertEqual(len(creates), 1)
        args = creates[0]
        self.assertIn(f"--head={job.branch}", args)
        self.assertIn("--base=main", args)
        self.assertIn(f"--title={job.title}", args)
        self.assertNotIn("--draft", args)
        body = next(a for a in args if a.startswith("--body="))
        self.assertIn(f"software-factory job {job.id}", body)
        self.assertIn("## Why\nSubtract is small and tested.", body)
        self.assertIn("## For the reviewer\n- Nobody checked negative numbers.", body)
        self.assertNotIn("For the human", body)
        job = self.reload(job)
        record = job.data["delivery"]
        self.assertEqual((record["method"], record["pushed"], record["pr_url"]), ("github-pr", True, PR_URL))
        self.assertEqual((record["commit"], record["remote"]), (head, "origin"))
        self.assertEqual(job.stage, "delivered")
        self.assertFalse(job.worktree.exists())
        self.assertEqual(sh(self.repo, "git", "rev-parse", job.branch), head)
        tell = step(job)["tell_user"]
        self.assertIn(PR_URL, tell)
        self.assertIn("Nobody checked negative numbers", tell)

    def test_draft_option(self):
        self.write_config(delivery={"method": "github-pr", "draft": True})
        job = self.approved()
        deliver(job)
        self.assertIn("--draft", self.pr_creates()[0])

    def test_person_accepted_job_is_handed_off_without_pushing(self):
        job = self.approved()
        job.data["approved_by_person"] = True
        job.save()
        self.assertTrue(deliver(job).startswith("DELIVERED"))
        self.assertIsNone(self.remote_ref(job.branch))
        self.assertEqual(self.gh_calls(), [])
        job = self.reload(job)
        record = job.data["delivery"]
        self.assertFalse(record["pushed"])
        self.assertIn(f"push -u origin {job.branch}", record["next_steps"])
        self.assertIn(f"gh pr create --head {job.branch} --base main", record["next_steps"])
        self.assertFalse(job.worktree.exists())
        self.assertTrue(sh(self.repo, "git", "branch", "--list", job.branch))

    def test_rework_pushes_again_and_reuses_the_pull_request(self):
        job = self.approved()
        deliver(job)
        human.rework(self.reload(job), "The PR review wants multiply too.")
        job = self.reload(job)
        self.assertEqual(job.stage, "spec")
        step(job)
        self.spec(job)
        step(job)
        self.build(job, name="multiply", op="*")
        self.assertEqual(step(job)["action"], "run")
        self.assertEqual(gate(job)["verdict"], "PASS")
        step(job)
        self.review(job, "code")
        step(job)
        self.decide(job)
        step(job)
        self.assertEqual(job.stage, "delivering")
        head = sh(job.worktree, "git", "rev-parse", "HEAD")
        self.assertTrue(deliver(job).startswith("DELIVERED"))
        self.assertEqual(self.remote_ref(job.branch), head)
        self.assertEqual(len(self.pr_creates()), 1)
        record = self.reload(job).data["delivery"]
        self.assertEqual(record["pr_url"], PR_URL)
        self.assertIn("updated pull request", record["summary"])

    def test_gh_not_on_path_parks_the_job(self):
        job = self.approved()
        only_git = self.tmp / "only-git"
        only_git.mkdir()
        (only_git / "git").symlink_to(shutil.which("git"))
        os.environ["PATH"] = str(only_git)
        result = deliver(job)
        self.assertIn("isn't installed or not on PATH", result)
        self.assert_parked(job, result)
        self.assertEqual(self.gh_calls(), [])

    def test_gh_not_signed_in_parks_the_job(self):
        job = self.approved()
        (self.gh_state / "auth-fails").write_text("")
        result = deliver(job)
        self.assertIn("isn't signed in", result)
        self.assertIn("not logged into", result)
        self.assert_parked(job, result)
        self.assertIsNone(self.remote_ref(job.branch))

    def test_unknown_remote_parks_the_job(self):
        self.write_config(delivery={"method": "github-pr", "remote": "--upload-pack=touch pwned"})
        job = self.approved()
        result = deliver(job)
        self.assertIn("no remote named", result)
        self.assert_parked(job, result)
        self.assertIsNone(self.remote_ref(job.branch))
        self.assertFalse((self.repo / "pwned").exists())

    def test_rejected_push_parks_the_job(self):
        job = self.approved()
        other = sh(self.origin, "git", "-c", "user.name=Other", "-c", "user.email=other@example.com",
                   "commit-tree", "main^{tree}", "-p", "main", "-m", "someone else's commit")
        sh(self.origin, "git", "update-ref", f"refs/heads/{job.branch}", other)
        result = deliver(job)
        self.assertIn(f"the push of {job.branch} to origin was rejected or failed", result)
        self.assert_parked(job, result)
        self.assertEqual(self.remote_ref(job.branch), other)
        self.assertEqual(self.pr_creates(), [])

    def test_failed_pr_create_parks_the_job_then_retry_delivers(self):
        job = self.approved()
        (self.gh_state / "create-fails").write_text("")
        result = deliver(job)
        self.assertIn("`gh pr create` failed", result)
        self.assertIn("something went wrong", result)
        job = self.assert_parked(job, result)
        pushed = self.remote_ref(job.branch)
        self.assertIsNotNone(pushed)
        (self.gh_state / "create-fails").unlink()
        self.assertTrue(human.retry(job).startswith("DELIVERED"))
        job = self.reload(job)
        self.assertEqual((job.stage, job.data["delivery"]["pr_url"]), ("delivered", PR_URL))
        self.assertEqual(self.remote_ref(job.branch), pushed)

    def test_approver_brief_says_a_pull_request_is_opened(self):
        job = self.approved()
        text = self.latest_brief(job, "approver")
        self.assertIn("opens a pull request", text)
        self.assertIn("is not by itself a reason to escalate", text)


if __name__ == "__main__":
    unittest.main()
