"""Tests for the factory's deterministic spine, on throwaway git repos.

Agents are simulated by writing the files they would write. Run with:
    python3 -m unittest discover -s tests -v
"""

import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine" / "lib"))

from factory import cli, human, jobs, profile as profiles, verify  # noqa: E402
from factory.dashboard import read_job_file, snapshot  # noqa: E402
from factory.gate import gate  # noqa: E402
from factory.delivery import deliver  # noqa: E402
from factory.step import step  # noqa: E402
from factory.util import FactoryError  # noqa: E402

CHECKS = """#!/usr/bin/env bash
set -euo pipefail
python3 -m unittest discover -s tests -t . -q
"""


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Sandbox(unittest.TestCase):
    """A target repo, a profiles folder pointing at it, and helpers that play the agents."""

    delivery = {"method": "handoff"}
    max_rounds = 3

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.env_backup = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.env_backup)))
        (self.tmp / "gitconfig").write_text("")
        os.environ.update({
            "FACTORY_PROJECTS_DIR": str(self.tmp / "projects"),
            "GIT_CONFIG_GLOBAL": str(self.tmp / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
        })

        self.repo = self.tmp / "app"
        (self.repo / "tests").mkdir(parents=True)
        sh(self.tmp, "git", "init", "-q", "-b", "main", str(self.repo))
        sh(self.repo, "git", "config", "user.name", "Test")
        sh(self.repo, "git", "config", "user.email", "test@example.com")
        (self.repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (self.repo / "tests" / "__init__.py").write_text("")
        (self.repo / "tests" / "test_calc.py").write_text(
            "import unittest\nfrom calc import add\n\n\nclass AddTest(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
        sh(self.repo, "git", "add", ".")
        sh(self.repo, "git", "commit", "-q", "-m", "Initial commit")
        sh(self.repo, "git", "remote", "add", "origin", "git@github.com:acme/app.git")

        self.pdir = self.tmp / "projects" / "app"
        (self.pdir / "context").mkdir(parents=True)
        (self.pdir / "checks.sh").write_text(CHECKS)
        (self.pdir / "context" / "project.md").write_text("<!-- guidance -->\nA calculator library. PROJECT-NOTE\n")
        (self.pdir / "context" / "security.md").write_text("SECURITY-NOTE: nothing is public.\n")
        (self.pdir / "context" / "ui.md").write_text("UI-NOTE: tokens live in web/theme.css\n")
        (self.pdir / "backlog.md").write_text(
            "# Backlog\n\n- [x] Old item → 000-old\n- [ ] Add subtract\n  Return a - b.\n- [ ] Add multiply\n")
        self.write_config()

    def write_config(self, **overrides):
        config = {
            "repo": {"path": str(self.repo), "remote": "https://github.com/acme/app"},
            "base_branch": "main",
            "delivery": self.delivery,
            "max_rounds": self.max_rounds,
            "protected_paths": ["tests/test_calc.py"],
            "ports": {"base": 5400},
            "commands": {"test": "python3 -m unittest discover -s tests -t ."},
        }
        config.update(overrides)
        (self.pdir / "config.json").write_text(json.dumps(config, indent=2))

    @property
    def profile(self):
        return profiles.load("app")

    def new_job(self, title="Add a subtract function"):
        return jobs.create(self.profile, title, "Return a minus b.")

    def reload(self, job):
        return jobs.load_job(self.profile, job.id)

    # -- playing the agents

    def write(self, path, text):
        time.sleep(0.02)  # strictly after the spawn timestamp
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def intake(self, job, decision="ACCEPT", risk="low", surfaces="none", extra=""):
        self.write(job.file("intake.md"),
                   f"DECISION: {decision}\nRISK: {risk}\nSURFACES: {surfaces}\nRESCOPED: no\n\n## Scope\nSubtract.\n\n"
                   f"## Why\nSmall.\n{extra}")

    def spec(self, job):
        self.write(job.file("spec.md"), "# Subtract\n\n## Requirements\n1. subtract(a, b)\n\n"
                                        "## Acceptance criteria\n1. `subtract(5, 3) == 2`\n")

    def build(self, job, status="READY", touch_protected=False, leave_dirty=False, name="subtract", op="-"):
        wt = job.worktree
        with open(wt / "calc.py", "a") as f:
            f.write(f"\n\ndef {name}(a, b):\n    return a {op} b\n")
        (wt / "tests" / f"test_{name}.py").write_text(
            f"import unittest\nfrom calc import {name}\n\n\nclass T(unittest.TestCase):\n"
            f"    def test_it(self):\n        self.assertEqual({name}(6, 3), 6 {op} 3)\n")
        paths = ["calc.py", f"tests/test_{name}.py"]
        if touch_protected:
            (wt / "tests" / "test_calc.py").write_text("# gutted\n")
            paths.append("tests/test_calc.py")
        sh(wt, "git", "add", *paths)
        sh(wt, "git", "commit", "-q", "-m", f"Add {name}")
        if leave_dirty:
            (wt / "stray.txt").write_text("oops")
        self.write(job.file("build.md"), f"STATUS: {status}\n\n# Build\n\n## Blocked\nNo idea.\n")

    def review(self, job, lens, verdict="PASS"):
        self.write(job.round_dir() / f"review-{lens}.md", f"VERDICT: {verdict}\n\n## Findings\nNone.\n")

    def decide(self, job, decision="APPROVE"):
        self.write(job.file("decision.md"), f"{decision}\n\n## Why\nFine.\n\n## For the human\nCheck pricing.\n")

    def to_review(self, job, risk="low", surfaces="none", **build_kwargs):
        """Drive a fresh job to the review stage; returns the spawn action for the reviewers."""
        step(job)
        self.intake(job, risk=risk, surfaces=surfaces)
        step(job)
        self.spec(job)
        step(job)
        self.build(job, **build_kwargs)
        self.assertEqual(step(job)["action"], "run")
        self.assertEqual(gate(job)["verdict"], "PASS")
        return step(job)

    def brief_text(self, action, i=0):
        return Path(action["agents"][i]["prompt"].split("Brief: ", 1)[1]).read_text()

    def latest_brief(self, job, role):
        return sorted((job.dir / "briefs").glob(f"*-{role}*.md"))[-1].read_text()


class HappyPath(Sandbox):
    def test_request_to_approved_branch(self):
        job = self.new_job()
        self.assertEqual(job.stage, "intake")
        self.assertTrue(jobs.is_relative_to(job.worktree, self.pdir / "worktrees"))
        self.assertEqual(sh(self.repo, "git", "status", "--porcelain"), "")
        request = job.file("request.md").read_text()
        self.assertIn("<<<REQUEST-", request)
        self.assertIn("Title: Add a subtract function", request)

        a = step(job)
        self.assertEqual(a["action"], "spawn")
        self.assertEqual(a["agents"][0]["subagent_type"], "factory:intake")
        self.assertEqual(a["agents"][0]["model"], "opus")
        text = self.brief_text(a)
        self.assertIn("Title: Add a subtract function", text)
        self.assertIn("PROJECT-NOTE", text)
        self.assertNotIn("guidance", text)
        self.assertNotIn("SECURITY-NOTE", text)

        self.intake(job)
        a = step(job)
        self.assertEqual((job.stage, a["agents"][0]["subagent_type"]), ("spec", "factory:spec-writer"))
        self.assertEqual(job.data["agents"], {"builder": "sonnet", "lenses": {"code": "haiku"}})

        self.spec(job)
        a = step(job)
        self.assertEqual((job.stage, job.round, a["agents"][0]["model"]), ("build", 1, "sonnet"))
        self.assertIn("tests/test_calc.py", self.brief_text(a))

        self.build(job)
        a = step(job)
        self.assertEqual((job.stage, a["action"]), ("gate", "run"))
        self.assertIn("factory gate", a["command"])
        self.assertEqual(gate(job)["verdict"], "PASS")
        self.assertTrue((job.round_dir() / "checks.md").read_text().startswith("GATE: PASS"))

        a = step(job)
        self.assertEqual(job.stage, "review")
        self.assertEqual([(x["subagent_type"], x["model"]) for x in a["agents"]], [("factory:reviewer", "haiku")])
        code_brief = self.brief_text(a)
        self.assertIn("Is this a correct, simple change", code_brief)
        self.assertNotIn("SECURITY-NOTE", code_brief)

        self.review(job, "code")
        a = step(job)
        self.assertEqual((job.stage, a["agents"][0]["subagent_type"]), ("approve", "factory:approver"))

        self.assertIn("hands the branch to a person", self.brief_text(a))
        gated = job.data["gate"]["commit"]
        main_before = sh(self.repo, "git", "rev-parse", "main")

        self.decide(job)
        a = step(job)
        self.assertEqual((job.stage, a["action"]), ("delivering", "run"))
        self.assertIn("factory deliver", a["command"])
        out = deliver(job)
        self.assertTrue(out.startswith("DELIVERED"), out)

        job = self.reload(job)
        self.assertEqual((job.stage, job.data["delivery"]["method"]), ("delivered", "handoff"))
        self.assertEqual(sh(self.repo, "git", "rev-parse", "main"), main_before, "handoff never touches main")
        self.assertEqual(sh(self.repo, "git", "rev-parse", job.branch), gated, "the branch is exactly the gated commit")
        self.assertFalse(job.worktree.exists())
        self.assertEqual(sh(self.repo, "git", "status", "--porcelain"), "")
        a = step(job)
        self.assertEqual(a["action"], "done")
        self.assertIn(f"push -u origin {job.branch}", a["tell_user"])
        self.assertIn("bin/factory rework", a["tell_user"])


class Routing(Sandbox):
    def test_surfaces_add_lenses_and_notes_stay_with_their_lens(self):
        job = self.new_job()
        a = self.to_review(job, risk="high", surfaces="ui")
        lenses = {x["description"].split(" ", 1)[1]: x["model"] for x in a["agents"]}
        self.assertEqual(lenses, {"code review": "sonnet", "security review": "opus",
                                  "ui review": "sonnet", "ux review": "sonnet"})
        self.assertEqual(job.data["agents"]["builder"], "opus")
        by_lens = {x["description"].split(" ")[1]: self.brief_text(a, i) for i, x in enumerate(a["agents"])}
        self.assertIn("SECURITY-NOTE", by_lens["security"])
        self.assertNotIn("PROJECT-NOTE", by_lens["security"])
        for lens in ("code", "ui", "ux"):
            self.assertNotIn("SECURITY-NOTE", by_lens[lens])
        self.assertIn("UI-NOTE", by_lens["ui"])
        self.assertNotIn("UI-NOTE", by_lens["ux"])

    def test_unknown_surface_parks_the_job(self):
        job = self.new_job()
        step(job)
        self.intake(job, surfaces="payments")
        a = step(job)
        self.assertEqual((job.stage, a["action"]), ("needs-human", "stop"))
        self.assertIn("payments", a["tell_user"])


class Gate(Sandbox):
    def drive_to_gate(self, **build_kwargs):
        job = self.new_job()
        step(job)
        self.intake(job)
        step(job)
        self.spec(job)
        step(job)
        self.build(job, **build_kwargs)
        step(job)
        return job

    def test_protected_test_file_fails_the_gate(self):
        job = self.drive_to_gate(touch_protected=True)
        result = gate(job)
        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(result["protected"], ["M tests/test_calc.py"])
        a = step(job)
        self.assertEqual((job.stage, job.round), ("build", 2))
        self.assertIn("The gate failed round 1", self.brief_text(a))

    def test_dirty_worktree_fails_the_gate(self):
        job = self.drive_to_gate(leave_dirty=True)
        result = gate(job)
        self.assertEqual(result["verdict"], "FAIL")
        self.assertIn("stray.txt", result["problems"][0])

    def test_changed_checks_need_a_person(self):
        job = self.drive_to_gate()
        (self.pdir / "checks.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
        result = gate(job)
        self.assertEqual((result["verdict"], result["tampered"], result["exit"]), ("FAIL", ["checks.sh"], None))
        a = step(job)
        self.assertEqual(job.stage, "needs-human")
        self.assertIn("factory trust", a["tell_user"])
        with self.assertRaises(FactoryError):
            human.trust(job)

    def test_failing_checks_fail_the_gate(self):
        job = self.drive_to_gate(op="+")  # subtract implemented as addition; its own test still passes...
        (job.worktree / "tests" / "test_extra.py").write_text(
            "import unittest\nfrom calc import subtract\n\n\nclass X(unittest.TestCase):\n"
            "    def test_x(self):\n        self.assertEqual(subtract(5, 3), 2)\n")
        sh(job.worktree, "git", "add", "tests/test_extra.py")
        sh(job.worktree, "git", "commit", "-q", "-m", "Add a test")
        result = gate(job)
        self.assertEqual((result["verdict"], result["exit"]), ("FAIL", 1))
        self.assertIn("FAILED", (job.round_dir() / "checks.md").read_text())

    def test_dry_run_records_nothing(self):
        job = self.drive_to_gate()
        self.assertEqual(gate(job, mode="dry")["verdict"], "PASS")
        self.assertIsNone(self.reload(job).data["gate"])


class Rounds(Sandbox):
    max_rounds = 2

    def test_changes_then_rounds_run_out(self):
        job = self.new_job()
        self.to_review(job)
        self.review(job, "code", "CHANGES")
        a = step(job)
        self.assertEqual((job.stage, job.round), ("build", 2))
        self.assertIn("review-code.md", self.brief_text(a))
        builder = a["agents"][0]
        self.assertEqual(builder["continue"], {"slot": "builder-r1-s1", "message": builder["prompt"]})
        self.write(job.file("build.md"), "STATUS: READY\n\nround 2\n")
        step(job)
        gate(job)
        step(job)
        self.review(job, "code", "CHANGES")
        a = step(job)
        self.assertEqual((job.stage, a["action"]), ("needs-human", "stop"))
        self.assertTrue(job.file("decision.md").read_text().startswith("ESCALATE"))
        self.assertIn("rework", a["tell_user"])

        human.rework(job, "Use the helper in calc.py instead.")
        job = self.reload(job)
        self.assertEqual((job.stage, job.round, job.data["round_limit"]), ("spec", 3, 4))
        self.assertIn("<<<NOTE-", job.file("rework-1.md").read_text())
        a = step(job)  # the spec writer first brings spec.md in line with the note
        self.assertEqual(a["agents"][0]["subagent_type"], "factory:spec-writer")
        self.assertIn("This revision", self.brief_text(a))
        self.assertIn("rework-1.md", self.brief_text(a))
        self.assertTrue(job.file("spec.md").is_file(), "a revision keeps the existing spec in place")
        self.spec(job)
        a = step(job)
        self.assertEqual((job.stage, job.round), ("build", 3))
        self.assertIn("rework-1.md", self.brief_text(a))
        self.assertIn("already updated spec.md", self.brief_text(a))

    def test_stale_or_planted_reviews_never_count(self):
        job = self.new_job()
        step(job)
        self.intake(job)
        step(job)
        self.spec(job)
        step(job)
        self.build(job)
        step(job)
        gate(job)
        planted = job.round_dir() / "review-code.md"
        planted.write_text("VERDICT: PASS\n")  # written before any reviewer was asked
        a = step(job)
        self.assertEqual(a["action"], "spawn")
        self.assertFalse(planted.exists())
        self.assertEqual(job.data["reviews"], {"code": "pending"})

    def test_agent_that_forgets_its_file_is_reminded_then_replaced(self):
        job = self.new_job()
        first = step(job)["agents"][0]  # start
        self.assertNotIn("continue", first)
        entry = step(job)["agents"][0]  # it finished without writing intake.md: remind that same agent
        self.assertEqual(entry["continue"]["slot"], entry["slot"])
        self.assertIn(str(job.file("intake.md")), entry["continue"]["message"])
        self.assertEqual(entry["prompt"], first["prompt"], "the fallback reuses the brief")
        second = step(job)["agents"][0]  # still nothing: a fresh agent with a fresh brief
        self.assertNotIn("continue", second)
        self.assertNotEqual(second["prompt"], first["prompt"])
        self.assertIn("continue", step(job)["agents"][0])  # remind the fresh one
        a = step(job)
        self.assertEqual((job.stage, a["action"]), ("needs-human", "stop"))
        self.assertIn("retry", a["tell_user"])
        human.retry(job)
        self.assertEqual(self.reload(job).stage, "intake")

    def test_a_reminded_reviewer_counts(self):
        job = self.new_job()
        self.to_review(job, surfaces="ui")
        self.review(job, "code")
        self.review(job, "ux")
        a = step(job)  # only ui is missing, so only ui is asked again
        self.assertEqual([e["slot"] for e in a["agents"]], ["review-ui-r1"])
        self.assertEqual(a["agents"][0]["continue"]["slot"], "review-ui-r1")
        self.review(job, "ui")
        step(job)
        self.assertEqual(job.stage, "approve")

    def test_retry_keeps_reviews_that_were_written(self):
        job = self.new_job()
        self.to_review(job, surfaces="ui")
        self.review(job, "code")
        self.review(job, "ux")
        for _ in range(3):  # remind, start again, remind again
            step(job)
        self.assertEqual(step(job)["action"], "stop")
        human.retry(job)
        job = self.reload(job)
        a = step(job)
        self.assertEqual([e["slot"] for e in a["agents"]], ["review-ui-r1"])
        self.assertNotIn("continue", a["agents"][0])
        self.review(job, "ui")
        step(job)
        self.assertEqual(job.stage, "approve")

    def test_a_malformed_file_is_not_kept_for_retry(self):
        job = self.new_job()
        step(job)
        self.write(job.file("intake.md"), "I think we should build it.\n")
        step(job)
        self.assertEqual(job.stage, "needs-human")
        human.retry(job)
        a = step(self.reload(job))
        self.assertEqual(a["action"], "spawn", "the bad intake.md must not count again")

    def test_branch_moving_after_the_gate_is_caught(self):
        job = self.new_job()
        self.to_review(job)
        (job.worktree / "late.py").write_text("x = 1\n")
        sh(job.worktree, "git", "add", "late.py")
        sh(job.worktree, "git", "commit", "-q", "-m", "Sneak in a change")
        self.review(job, "code")
        a = step(job)
        self.assertEqual(job.stage, "needs-human")
        self.assertIn("moved after the gate", a["tell_user"])


class Autonomy(Sandbox):
    """Problems agents can solve themselves don't reach a person."""

    def at_build(self, spec_extra=""):
        job = self.new_job()
        step(job)
        self.intake(job)
        step(job)
        self.write(job.file("spec.md"), "# Subtract\n\n## Acceptance criteria\n1. works\n" + spec_extra)
        step(job)
        self.assertEqual(job.stage, "build")
        return job

    def change_protected_test(self, job):
        with open(job.worktree / "tests" / "test_calc.py", "a") as f:
            f.write("\n    def test_add_negative(self):\n        self.assertEqual(add(-1, -2), -3)\n")
        sh(job.worktree, "git", "commit", "-q", "-am", "Extend the add tests")

    def test_spec_can_unlock_a_protected_test(self):
        job = self.at_build("\n## Protected changes\n- `tests/test_calc.py`: add a negative-number case.\n"
                            "- `tests/other.py`: not protected, ignored.\n")
        self.assertEqual(job.data["unlocked"], ["tests/test_calc.py"])
        self.assertIn("The spec unlocks these", self.latest_brief(job, "builder"))
        self.build(job)
        self.change_protected_test(job)
        step(job)
        result = gate(job)
        self.assertEqual((result["verdict"], result["protected"], result["unlocked"]),
                         ("PASS", [], ["M tests/test_calc.py"]))
        self.assertIn("Changed with the spec's permission", (job.round_dir() / "checks.md").read_text())

    def test_unlisted_protected_files_still_fail(self):
        job = self.at_build("\n## Protected changes\nNone.\n")
        self.build(job)
        self.change_protected_test(job)
        step(job)
        self.assertEqual(gate(job)["protected"], ["M tests/test_calc.py"])

    def test_profile_can_forbid_unlocking(self):
        self.write_config(spec_may_unlock_protected=False)
        job = self.at_build("\n## Protected changes\n- `tests/test_calc.py`: add a case.\n")
        self.assertEqual(job.data["unlocked"], [])
        self.assertIn("never change", self.latest_brief(job, "spec-writer"))

    def test_editing_the_accepted_spec_stops_the_gate(self):
        job = self.at_build()
        self.build(job)
        with open(job.file("spec.md"), "a") as f:
            f.write("\n## Protected changes\n- `tests/test_calc.py`: sneaky.\n")
        step(job)
        self.assertEqual(gate(job)["tampered"], ["spec.md"])
        self.assertIn("accepted spec.md", step(job)["tell_user"])

    def test_builder_asks_for_a_spec_change_and_carries_on(self):
        job = self.at_build()
        first_builder = job.data["last_slot"]["builder"]
        self.write(job.file("build.md"), "STATUS: SPEC-CHANGE\n\n## Spec change\ntests/test_calc.py pins add().\n")
        a = step(job)
        self.assertEqual((job.stage, a["agents"][0]["subagent_type"]), ("spec", "factory:spec-writer"))
        self.assertEqual(a["agents"][0]["continue"]["slot"], "spec-v1", "the same spec writer revises its spec")
        self.assertIn("## Spec change", self.brief_text(a))
        self.write(job.file("spec.md"), "# Subtract\n\n## Acceptance criteria\n1. works\n\n## Protected changes\n"
                                        "- `tests/test_calc.py`: add a case.\n\n## Revisions\n1. Unlocked it.\n")
        a = step(job)
        self.assertEqual((job.stage, job.round, job.data["unlocked"]), ("build", 1, ["tests/test_calc.py"]))
        self.assertEqual(a["agents"][0]["continue"]["slot"], first_builder, "the same builder carries on")
        self.assertIn("continued", self.brief_text(a))

    def test_spec_change_requests_are_limited(self):
        job = self.at_build()
        for version in (2, 3):
            self.write(job.file("build.md"), "STATUS: SPEC-CHANGE\n\n## Spec change\nAgain.\n")
            step(job)
            self.write(job.file("spec.md"), f"# v{version}\n\n## Acceptance criteria\n1. works\n")
            step(job)
        self.write(job.file("build.md"), "STATUS: SPEC-CHANGE\n\n## Spec change\nAgain.\n")
        self.assertEqual(step(job)["action"], "stop")

    def test_approver_changes_send_it_back(self):
        job = self.new_job()
        self.to_review(job)
        self.review(job, "code")
        step(job)
        self.write(job.file("decision.md"), "CHANGES\n\n## Why\nWorkaround.\n\n## Changes\n1. Use v-btn.\n")
        a = step(job)
        self.assertEqual((job.stage, job.round), ("build", 2))
        self.assertIn("The approver sent round 1 back", self.brief_text(a))

    def test_approver_changes_without_rounds_left_keep_its_notes(self):
        self.write_config(max_rounds=1)
        job = self.new_job()
        self.to_review(job)
        self.review(job, "code")
        step(job)
        self.write(job.file("decision.md"), "CHANGES\n\n## Changes\n1. Use v-btn.\n")
        a = step(job)
        self.assertEqual(job.stage, "needs-human")
        self.assertTrue(job.file("decision.md").read_text().startswith("CHANGES"))
        self.assertIn("decision.md lists the changes", a["tell_user"])

    def test_escalations_are_shown_in_full(self):
        job = self.new_job()
        self.to_review(job)
        self.review(job, "code")
        step(job)
        long_question = "Decide on the button. " * 80 + "THE END"
        self.write(job.file("decision.md"), f"ESCALATE\n\n## For the human\n{long_question}\n")
        a = step(job)
        self.assertIn("THE END", a["tell_user"])
        self.assertIn(str(job.file("decision.md")), a["tell_user"])

    def test_approver_brief_matches_the_delivery(self):
        job = self.new_job()
        self.to_review(job)
        self.review(job, "code")
        step(job)
        text = self.latest_brief(job, "approver")
        self.assertIn("is not by itself a reason to escalate", text)


class Intake(Sandbox):
    def test_needs_info_then_reply(self):
        job = self.new_job()
        step(job)
        self.intake(job, decision="NEEDS-INFO", extra="\n## Questions\n1. Integers only?\n")
        a = step(job)
        self.assertEqual((job.stage, a["action"]), ("needs-info", "stop"))
        self.assertIn("Integers only?", a["tell_user"])
        human.reply(job, "Yes, integers.")
        job = self.reload(job)
        self.assertEqual(job.stage, "intake")
        self.assertTrue(job.file("intake-1.md").is_file())
        a = step(job)
        self.assertIn("reply-1.md", self.brief_text(a))

    def test_reject_removes_worktree_and_branch(self):
        job = self.new_job()
        step(job)
        self.intake(job, decision="REJECT")
        a = step(job)
        self.assertEqual((job.stage, a["action"]), ("rejected", "done"))
        self.assertFalse(job.worktree.exists())
        self.assertEqual(sh(self.repo, "git", "branch", "--list", job.branch), "")


class DeliveryCases:
    """Run under every delivery method."""

    def approved(self):
        job = self.new_job()
        self.to_review(job)
        self.review(job, "code")
        step(job)
        self.decide(job)
        step(job)
        self.assertEqual(job.stage, "delivering")
        return job

    def test_commits_after_approval_are_refused(self):
        job = self.approved()
        (job.worktree / "late.py").write_text("x = 1\n")
        sh(job.worktree, "git", "add", "late.py")
        sh(job.worktree, "git", "commit", "-q", "-m", "Unreviewed")
        self.assertIn("no gate passed", deliver(job))
        self.assertEqual(self.reload(job).data["needs_human"]["retry"], "delivering")
        self.assertFalse((self.repo / "late.py").exists())

    def test_delivery_refuses_unapproved_jobs(self):
        job = self.new_job()
        with self.assertRaises(FactoryError):
            deliver(job)


class Handoff(DeliveryCases, Sandbox):
    def test_custom_next_steps(self):
        self.write_config(delivery={"method": "handoff", "next_steps": "gh pr create --head {branch} --base {base}"})
        job = self.approved()
        deliver(job)
        a = step(self.reload(job))
        self.assertIn(f"gh pr create --head {job.branch} --base main", a["tell_user"])

    def test_rework_after_handoff_reopens_the_branch(self):
        job = self.approved()
        deliver(job)
        human.rework(self.reload(job), "The PR review wants a docstring.")
        job = self.reload(job)
        self.assertEqual((job.stage, job.round, job.data["delivery"]), ("spec", 2, None))
        self.assertTrue(job.worktree.is_dir())
        self.assertEqual(sh(job.worktree, "git", "branch", "--show-current"), job.branch)
        self.assertIn("rework-1.md", self.brief_text(step(job)))
        self.spec(job)
        self.assertIn("rework-1.md", self.brief_text(step(job)))
        self.assertEqual(job.stage, "build")

    def test_approver_notes_reach_the_person(self):
        job = self.new_job()
        self.to_review(job)
        self.review(job, "code")
        step(job)
        self.write(job.file("decision.md"), "APPROVE\n\n## Why\nGood.\n\n## For the reviewer\n"
                                            "- Nobody looked at the login page in a browser.\n")
        step(job)
        deliver(job)
        self.assertIn("Nobody looked at the login page", step(self.reload(job))["tell_user"])


class LocalMerge(DeliveryCases, Sandbox):
    delivery = {"method": "local-merge", "strategy": "merge"}

    def test_merge_commit(self):
        job = self.approved()
        self.assertIn("merges the branch into `main` locally", self.latest_brief(job, "approver"))
        self.assertTrue(deliver(job).startswith("DELIVERED"))
        job = self.reload(job)
        self.assertEqual((job.stage, job.data["delivery"]["method"]), ("delivered", "local-merge"))
        parents = sh(self.repo, "git", "rev-list", "--parents", "-n", "1", "main").split()
        self.assertEqual(len(parents), 3, "a merge commit has two parents")
        self.assertIn("def subtract", (self.repo / "calc.py").read_text())
        self.assertEqual(sh(self.repo, "git", "branch", "--list", "factory/*"), "")
        self.assertNotIn("tell_user", step(job))
        with self.assertRaises(FactoryError):
            human.rework(job, "more")

    def test_squash(self):
        self.write_config(delivery={"method": "local-merge", "strategy": "squash"})
        job = self.approved()
        deliver(job)
        parents = sh(self.repo, "git", "rev-list", "--parents", "-n", "1", "main").split()
        self.assertEqual(len(parents), 2)
        self.assertEqual(sh(self.repo, "git", "log", "-1", "--format=%s", "main"), "Add a subtract function")

    def test_rebase_keeps_history_linear(self):
        self.write_config(delivery={"method": "local-merge", "strategy": "rebase"})
        job = self.approved()
        sh(self.repo, "git", "checkout", "-q", "--detach")  # base not checked out: update-ref path
        (self.repo / "README.md").write_text("hello\n")
        sh(self.repo, "git", "add", "README.md")
        sh(self.repo, "git", "commit", "-q", "-m", "Docs")
        sh(self.repo, "git", "branch", "-f", "main", "HEAD")
        deliver(job)
        self.assertEqual(self.reload(job).stage, "delivered")
        merges = sh(self.repo, "git", "rev-list", "--merges", "main")
        self.assertEqual(merges, "")
        self.assertIn("def subtract", sh(self.repo, "git", "show", "main:calc.py"))
        self.assertTrue(job.file("merge-checks.md").read_text().startswith("GATE: PASS"))

    def test_base_moved_is_synced_and_gated_again(self):
        job = self.approved()
        (self.repo / "README.md").write_text("hello\n")
        sh(self.repo, "git", "add", "README.md")
        sh(self.repo, "git", "commit", "-q", "-m", "Docs")
        deliver(job)
        job = self.reload(job)
        self.assertEqual(job.stage, "delivered")
        self.assertTrue(job.file("merge-checks.md").is_file())
        self.assertTrue((self.repo / "README.md").is_file())
        self.assertIn("def subtract", (self.repo / "calc.py").read_text())

    def test_dirty_main_checkout_waits_then_retries(self):
        job = self.approved()
        (self.repo / "calc.py").write_text("# local edit\n")
        result = deliver(job)
        self.assertTrue(result.startswith("BLOCKED"), result)
        job = self.reload(job)
        self.assertEqual((job.stage, job.data["needs_human"]["retry"]), ("needs-human", "delivering"))
        sh(self.repo, "git", "checkout", "--", "calc.py")
        human.retry(job)
        self.assertEqual(self.reload(job).stage, "delivered")

    def test_retry_after_sync_keeps_the_gated_sync_commit(self):
        job = self.approved()
        (self.repo / "README.md").write_text("hello\n")
        sh(self.repo, "git", "add", "README.md")
        sh(self.repo, "git", "commit", "-q", "-m", "Docs")
        (self.repo / "README.md").write_text("local edit\n")  # main checkout busy
        self.assertTrue(deliver(job).startswith("BLOCKED"))
        sh(self.repo, "git", "checkout", "--", "README.md")
        human.retry(self.reload(job))
        self.assertEqual(self.reload(job).stage, "delivered")

    def test_conflict_with_base_waits_for_a_person(self):
        job = self.approved()
        (self.repo / "calc.py").write_text("def add(a, b):\n    return b + a\n\n\ndef subtract(a, b):\n    return 0\n")
        sh(self.repo, "git", "commit", "-q", "-am", "Conflicting change")
        result = deliver(job)
        self.assertIn("conflicts", result)
        self.assertEqual(sh(job.worktree, "git", "status", "--porcelain"), "")



class Queue(Sandbox):
    def test_one_job_at_a_time(self):
        self.new_job()
        with self.assertRaises(FactoryError) as ctx:
            self.new_job("Something else")
        self.assertIn("one job at a time", str(ctx.exception))

    def test_backlog_next_claims_first_unchecked_item(self):
        job = jobs.create_next(self.profile)
        self.assertEqual(job.title, "Add subtract")
        self.assertIn("Return a - b.", job.file("request.md").read_text())
        self.assertIn(f"- [x] Add subtract → {job.id}", (self.pdir / "backlog.md").read_text())
        self.assertIn("- [ ] Add multiply", (self.pdir / "backlog.md").read_text())

    def test_numbering_and_slugs(self):
        job = self.new_job("Fix: the <script> tag & stuff!!")
        self.assertEqual(job.id, "001-fix-the-script-tag")
        self.assertEqual(job.branch, "factory/001-fix-the-script-tag")


class Profiles(Sandbox):
    def test_remote_normalisation(self):
        n = profiles.normalize_remote
        self.assertEqual(n("git@github.com:Acme/App.git"), "github.com/acme/app")
        self.assertEqual(n("https://github.com/acme/app/"), "github.com/acme/app")
        self.assertEqual(n("ssh://git@github.com:22/acme/app.git"), "github.com/acme/app")

    def test_resolve_by_remote_path_and_name(self):
        self.assertEqual(profiles.resolve(repo=str(self.repo)).name, "app")
        self.assertEqual(profiles.resolve(cwd=str(self.repo / "tests")).name, "app")
        self.assertEqual(profiles.resolve(project="app").name, "app")
        self.write_config(repo={"path": str(self.repo), "remote": ""})
        self.assertEqual(profiles.resolve(repo=str(self.repo)).name, "app")  # falls back to the path

    def test_templates_are_never_picked(self):
        (self.tmp / "projects" / "example").mkdir()
        (self.tmp / "projects" / "example" / "config.json").write_text('{"template": true, "repo": {"path": "/x"}}')
        with self.assertRaises(FactoryError):
            profiles.load("example")
        self.assertEqual([p.name for p in profiles.all_profiles()], ["app"])

    def test_validation_catches_mistakes(self):
        self.write_config(merge_method="squash", colour="blue", models={"intake": "gpt"})
        errors = "\n".join(profiles.validate(self.profile))
        self.assertIn("merge_method moved", errors)
        self.assertIn("unknown key 'colour'", errors)
        self.assertIn("models.intake", errors)

    def test_delivery_validation(self):
        for delivery, expected in (({"method": "carrier-pigeon"}, "delivery.method must be one of"),
                                   ({"method": "local-merge", "strategy": "octopus"}, "delivery.strategy must be one of"),
                                   ({"method": "handoff", "strategy": "squash"}, "isn't an option of `handoff`"),
                                   ({"method": "handoff", "next_steps": 3}, "delivery.next_steps must be a str")):
            self.write_config(delivery=delivery)
            self.assertIn(expected, "\n".join(profiles.validate(self.profile)))

    def test_missing_lens_checklist_is_an_error(self):
        self.write_config(surfaces={"perf": {"about": "hot paths", "lenses": {"perf": "sonnet"}}})
        self.assertIn("lens 'perf' has no checklist", "\n".join(profiles.validate(self.profile)))
        (self.pdir / "lenses").mkdir()
        (self.pdir / "lenses" / "perf.md").write_text("Is it fast?\n")
        self.assertEqual(profiles.validate(self.profile), [])

    def test_example_profile_is_a_valid_template(self):
        os.environ["FACTORY_PROJECTS_DIR"] = str(ROOT / "projects")
        example = profiles.Profile("example", ROOT / "projects" / "example")
        self.assertTrue(example.template)
        self.assertEqual(profiles.validate(example), [])


class People(Sandbox):
    def test_person_only_commands_refuse_agents(self):
        job = self.new_job()
        job.data["round"] = 1
        job.park("test")
        for action in (human.approve, human.abandon, human.trust):
            with self.assertRaises(FactoryError) as ctx:
                action(job)
            self.assertIn("person", str(ctx.exception))


class Engine(Sandbox):
    def test_engine_agents_and_skills_use_supported_frontmatter(self):
        r = verify.Report()
        with redirect_stdout(io.StringIO()):
            verify.check_engine(r)
        self.assertEqual(r.errors, 0)

    def test_frontmatter_check_flags_ignored_and_unknown_keys(self):
        bad = self.tmp / "bad.md"
        bad.write_text("---\nname: bad\ndescription: x\nhooks: {}\ncolour: red\n---\nbody\n")
        r = verify.Report()
        with redirect_stdout(io.StringIO()):
            verify.check_agent_file(bad, r)
        self.assertEqual(r.errors, 2)

    def test_verify_profile_runs_checks_on_a_fresh_base_checkout(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = verify.verify([self.profile], engine=False)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("checks.sh passes on a fresh checkout of main", out.getvalue())
        self.assertEqual(sh(self.repo, "git", "worktree", "list").count("\n"), 0)

    def test_run_launches_claude_with_the_plugin_and_guards(self):
        os.environ["FACTORY_CLAUDE_BIN"] = "/usr/bin/true"
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["run", "--project", "app", "--print-command", "Add", "subtract", "--", "--model", "opus"])
        self.assertEqual(code, 0)
        line = out.getvalue().strip().splitlines()[-1]
        self.assertIn(f"--plugin-dir {ROOT / 'engine'}", line)
        self.assertIn("--model opus", line)
        self.assertIn("--add-dir", line)  # the test profiles live outside the factory folder
        self.assert_prompt_survives(line, "/factory:run --project app --job 001-add-subtract")
        settings = json.loads(cli.session_settings("run"))
        self.assertIn(f"Edit(/{self.tmp}/projects/*/checks.sh)", settings["permissions"]["deny"])
        self.assertIn("Bash(factory *)", settings["permissions"]["allow"])

    def assert_prompt_survives(self, command_line, prompt):
        """--add-dir is variadic in Claude Code: anything after it is a directory until `--` ends the options."""
        args = shlex.split(command_line)
        self.assertEqual(args[-2:], ["--", prompt])
        self.assertLess(max(i for i, a in enumerate(args) if a == "--add-dir"), args.index("--"))

    def test_run_job_finds_its_profile_without_project(self):
        os.environ["FACTORY_CLAUDE_BIN"] = "/usr/bin/true"
        other = self.tmp / "projects" / "other"
        other.mkdir()
        (other / "config.json").write_text(json.dumps({"repo": {"path": str(self.repo), "remote": "x"}}))
        job = self.new_job()
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["run", "--job", job.id, "--print-command"])
        self.assertEqual(code, 0, out.getvalue())
        self.assert_prompt_survives(out.getvalue().strip().splitlines()[-1],
                                    f"/factory:run --project app --job {job.id}")

    def test_add_project_scaffolds_and_launches_with_its_prompt(self):
        os.environ["FACTORY_CLAUDE_BIN"] = "/usr/bin/true"
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["add-project", "demo", "--repo", str(self.repo / "tests"), "--print-command"])
        self.assertEqual(code, 0, out.getvalue())
        config = json.loads((self.tmp / "projects" / "demo" / "config.json").read_text())
        self.assertEqual((config["repo"]["path"], config["base_branch"]), (str(self.repo), "main"))
        self.assertNotIn("template", config)
        line = out.getvalue().strip().splitlines()[-1]
        self.assertIn(f"--add-dir {self.repo}", line)
        self.assert_prompt_survives(line, "/factory:add-project demo")

    def test_dashboard_reads_jobs_but_not_outside_them(self):
        job = self.new_job()
        data = snapshot()
        self.assertEqual([j["id"] for j in data["jobs"]], [job.id])
        self.assertIn("request.md", data["jobs"][0]["files"])
        self.assertIsNotNone(read_job_file("app", job.id, "request.md"))
        self.assertIsNone(read_job_file("app", job.id, "../../config.json"))
        self.assertIsNone(read_job_file("app", job.id, "/etc/passwd"))


if __name__ == "__main__":
    unittest.main()
