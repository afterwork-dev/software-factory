"""The `factory` command. `bin/factory <command> --help` for details."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import List, Optional

from . import brief as briefs
from . import dashboard, human, profile as profiles, verify as verifier
from .gate import gate, summary_line
from .jobs import ACTIVE, CLOSED, PARKED, Job, active_jobs, create, create_next, find, jobs_of, parse_backlog, split_request
from .delivery import deliver
from .route import route
from .step import step
from .util import ENGINE, ROOT, FactoryError, git, is_relative_to, projects_dir, read_json, run, write_json

PLUGIN_SKILL = "/factory:"


# ---------------------------------------------------------------- launching Claude Code

def claude_bin() -> str:
    found = os.environ.get("FACTORY_CLAUDE_BIN") or shutil.which("claude")
    if not found:
        raise FactoryError("can't find the `claude` CLI. Install Claude Code, or set FACTORY_CLAUDE_BIN to its path.")
    return found


def _rule(tool: str, path) -> str:
    """An absolute-path permission rule (`//` means absolute in Claude Code rules)."""
    return f"{tool}(//{str(path).lstrip('/')})"


def session_settings(mode: str) -> str:
    """engine/settings.json plus deny rules, with absolute paths, for files agents must never write."""
    settings = read_json(ENGINE / "settings.json")
    settings.pop("_readme", None)
    perms = settings.setdefault("permissions", {})
    projects = projects_dir()
    guarded = [ENGINE / "**", ROOT / "bin" / "**"]
    if mode == "run":
        guarded += [projects / "*" / name for name in (
            "config.json", "checks.sh", "backlog.md", "context/**", "lenses/**",
            "jobs/*/job.json", "jobs/*/events.log", "jobs/*/request.md", "jobs/*/reply-*.md",
            "jobs/*/rework-*.md", "jobs/*/briefs/**")]
    # Edit rules cover every file-editing tool (Write included); Claude Code ignores Write(path) rules.
    perms.setdefault("deny", []).extend(_rule("Edit", path) for path in guarded)
    return json.dumps(settings)


def launch(prompt: str, mode: str, extra_dirs: List[Path], claude_args: List[str], print_only: bool) -> int:
    cmd = [claude_bin(), "--plugin-dir", str(ENGINE), "--settings", session_settings(mode)]
    for d in extra_dirs:
        if not is_relative_to(d, ROOT):
            cmd += ["--add-dir", str(d)]
    # `--` ends option parsing. Without it a variadic option such as --add-dir swallows the prompt.
    cmd += claude_args + ["--", prompt]
    if print_only:
        print(shlex.join(cmd))
        return 0
    print(f"starting Claude Code in {ROOT}: {prompt}", flush=True)
    os.chdir(ROOT)
    os.execvp(cmd[0], cmd)
    return 0  # not reached


# ---------------------------------------------------------------- commands

def _profile(args) -> profiles.Profile:
    return profiles.resolve(getattr(args, "project", None), getattr(args, "repo", None), os.getcwd())


def _job(args) -> Job:
    return find(args.job, getattr(args, "project", None))


def cmd_run(args) -> int:
    if args.job and not (args.project or args.repo):
        job = find(args.job)  # a job id is enough; it names its own profile
        profile = job.profile
    else:
        profile = _profile(args)
    if args.job:
        job = find(args.job, profile.name)
    elif args.next:
        job = create_next(profile)
        print(f"{job.id}: created from the backlog")
    elif args.file or args.stdin:
        text = Path(args.file).read_text() if args.file else sys.stdin.read()
        title, body = split_request(text)
        job = create(profile, title, body, source=f"file {args.file}" if args.file else "stdin")
        print(f"{job.id}: created")
    elif args.request:
        job = create(profile, " ".join(args.request), source="command argument")
        print(f"{job.id}: created")
    else:
        running = active_jobs(profile)
        if not running:
            raise FactoryError("nothing to run: give a feature request, --next, --file, or --job <id>")
        job = running[0]
        print(f"{job.id}: resuming ({job.stage})")
    if job.stage not in ACTIVE:
        action = step(job)
        print(action.get("tell_user") or action["summary"])
        return 0
    extra = [projects_dir(), profile.worktree_root]
    return launch(f"{PLUGIN_SKILL}run --project {profile.name} --job {job.id}", "run", extra,
                  args.claude_args, args.print_command)


def cmd_new(args) -> int:
    profile = _profile(args)
    if args.next:
        job = create_next(profile)
    else:
        text = sys.stdin.read() if args.stdin else " ".join(args.request)
        title, body = split_request(text)
        job = create(profile, title, body, source="stdin" if args.stdin else "command argument")
    print(job.id)
    return 0


def cmd_step(args) -> int:
    print(json.dumps(step(_job(args)), indent=2))
    return 0


def cmd_gate(args) -> int:
    job = _job(args)
    result = gate(job, mode="dry" if args.dry_run else "round")
    print(summary_line(job, result))
    if args.dry_run and result["verdict"] != "PASS":
        print(f"log: {job.dir / 'logs' / 'dry-checks.log'}")
    return 0 if result["verdict"] == "PASS" else 1


def cmd_deliver(args) -> int:
    print(deliver(_job(args)))
    return 0


def cmd_reply(args) -> int:
    print(human.reply(_job(args), sys.stdin.read() if args.stdin else " ".join(args.text)))
    return 0


def cmd_rework(args) -> int:
    print(human.rework(_job(args), sys.stdin.read() if args.stdin else " ".join(args.text)))
    return 0


def cmd_retry(args) -> int:
    print(human.retry(_job(args)))
    return 0


def cmd_approve(args) -> int:
    print(human.approve(_job(args), args.yes))
    return 0


def cmd_abandon(args) -> int:
    print(human.abandon(_job(args), args.yes))
    return 0


def cmd_trust(args) -> int:
    print(human.trust(_job(args), args.yes))
    return 0


def cmd_show(args) -> int:
    job = _job(args)
    print(json.dumps(job.data, indent=2))
    return 0


def cmd_brief(args) -> int:
    job = _job(args)
    makers = {
        "intake": lambda: briefs.intake_brief(job),
        "spec-writer": lambda: briefs.spec_brief(job),
        "builder": lambda: briefs.builder_brief(job),
        "approver": lambda: briefs.approver_brief(job),
        "reviewer": lambda: briefs.reviewer_brief(job, args.lens or "code"),
    }
    print(makers[args.role]())
    return 0


def cmd_route(args) -> int:
    profile = _profile(args)
    surfaces = [s for s in (args.surfaces or "").split(",") if s]
    print(json.dumps(route(profile, args.risk, surfaces), indent=2))
    return 0


def cmd_resolve(args) -> int:
    profile = _profile(args)
    print(f"{profile.name}\t{profile.repo_path}\t{profile.remote}")
    return 0


def _verdicts(job: Job) -> str:
    gate_v = (job.data.get("gate") or {}).get("verdict") or "-"
    reviews = " ".join(f"{k}:{v}" for k, v in (job.data.get("reviews") or {}).items())
    return f"gate:{gate_v} {reviews}".strip()


def cmd_status(args) -> int:
    chosen = [profiles.load(args.project)] if args.project else profiles.all_profiles()
    if not chosen:
        print("no profiles yet: bin/factory add-project <name> --repo <path>")
    for profile in chosen:
        print(f"{profile.name}  ({profile.repo_path}, base {profile.config['base_branch']})")
        jobs = jobs_of(profile)
        if not args.all:
            jobs = [j for j in jobs if j.stage not in CLOSED] or jobs[-5:]
        if not jobs:
            print("  no jobs")
        for job in jobs:
            print(f"  {job.id:<32} {job.stage:<12} round {job.round:<2} {_verdicts(job)}")
    return 0


def cmd_queue(args) -> int:
    profile = _profile(args)
    running = active_jobs(profile)
    parked = [j for j in jobs_of(profile) if j.stage in PARKED]
    pending = [i for i in parse_backlog(profile) if not i["done"]]
    print(f"{profile.name}: running {', '.join(f'{j.id} ({j.stage})' for j in running) or 'nothing'}")
    for j in parked:
        print(f"  waiting for you: {j.id} ({j.stage})")
    print(f"  backlog: {len(pending)} unchecked item(s) in {profile.backlog}")
    for item in pending[:10]:
        print(f"    - {item['title']}")
    if running:
        print(f"next: bin/factory run --project {profile.name} --job {running[0].id}")
    elif pending:
        print(f"next: bin/factory run --project {profile.name} --next")
    return 0


def cmd_verify(args) -> int:
    if args.engine_only:
        return verifier.verify([], engine=True)
    chosen = [_profile(args)] if (args.project or args.repo) else profiles.all_profiles()
    return verifier.verify(chosen, engine=True, run_checks_too=not args.no_checks)


def cmd_dashboard(args) -> int:
    dashboard.serve(args.host, args.port)
    return 0


def detect_base_branch(repo: Path) -> str:
    head = run(["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], cwd=repo, check=False).stdout.strip()
    if head:
        return head.rsplit("/", 1)[-1]
    for name in ("main", "master", "trunk", "develop"):
        if run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], cwd=repo, check=False).returncode == 0:
            return name
    return git(repo, "branch", "--show-current") or "main"


def cmd_add_project(args) -> int:
    name = args.name
    if not profiles.NAME_RE.match(name) or name == "example":
        raise FactoryError("a profile name is lowercase letters, digits and dashes, and not `example`")
    target = projects_dir() / name
    template = ROOT / "projects" / "example"
    repo_arg = Path(args.repo).expanduser().resolve()
    top = run(["git", "rev-parse", "--show-toplevel"], cwd=repo_arg, check=False)
    if top.returncode != 0:
        raise FactoryError(f"{repo_arg} is not inside a git repository")
    repo = Path(top.stdout.strip()).resolve()
    if is_relative_to(repo, ROOT):
        raise FactoryError("the target repo can't be inside the factory folder")

    if target.exists():
        print(f"profile {name} already exists at {target}; leaving its files as they are")
    else:
        remote = args.remote if args.remote is not None else \
            run(["git", "remote", "get-url", "origin"], cwd=repo, check=False).stdout.strip()
        taken = [p.config["ports"]["base"] for p in profiles.all_profiles()]
        port = 5300
        while any(abs(port - t) < 20 for t in taken):
            port += 20
        shutil.copytree(template, target, ignore=shutil.ignore_patterns("jobs", "worktrees", "logs", ".lock"))
        config = read_json(target / "config.json")
        config.pop("template", None)
        config["_readme"] = f"Profile for {repo.name}. See projects/example/config.json and README.md for every key."
        config["repo"] = {"path": str(repo), "remote": remote}
        config["base_branch"] = args.base or detect_base_branch(repo)
        config["ports"] = {"base": port}
        write_json(target / "config.json", config)
        (target / "checks.sh").chmod(0o755)
        print(f"created {target}\n  repo   {repo}\n  remote {remote or '(none)'}\n  base   {config['base_branch']}")

    if args.no_session:
        print(f"next: fill in {target}/checks.sh and context/, then `bin/factory verify --project {name}`")
        return 0
    return launch(f"{PLUGIN_SKILL}add-project {name}", "setup", [projects_dir(), repo],
                  args.claude_args, args.print_command)


# ---------------------------------------------------------------- argument parsing

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="factory", description=(
        "A local software factory for Claude Code: intake → spec → build → gate → review → approve, "
        "one job at a time, against a project profile in projects/<name>/."))
    sub = p.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    def add(name, func, help_text, job=False, where=True):
        sp = sub.add_parser(name, help=help_text, description=help_text)
        sp.set_defaults(func=func)
        if where:
            sp.add_argument("--project", "-p", help="profile name (default: resolve from the repo's git remote)")
            sp.add_argument("--repo", help="target repo path, to resolve the profile by its remote")
        if job:
            sp.add_argument("job", help="job id (004-dark-mode) or number (4)")
        return sp

    sp = add("run", cmd_run, "Create or resume a job, then open Claude Code to run it")
    sp.add_argument("request", nargs="*", help="the feature request (quote it)")
    sp.add_argument("--next", action="store_true", help="take the first unchecked backlog item")
    sp.add_argument("--file", help="read the request from a file (first line is the title)")
    sp.add_argument("--stdin", action="store_true", help="read the request from stdin")
    sp.add_argument("--job", help="resume this job")
    sp.add_argument("--print-command", action="store_true", help="print the claude command instead of running it")

    sp = add("add-project", cmd_add_project, "Create a profile for a repo, then tailor it in Claude Code", where=False)
    sp.add_argument("name")
    sp.add_argument("--repo", required=True, help="path to the project's git checkout")
    sp.add_argument("--remote", help="override the remote recorded in the profile")
    sp.add_argument("--base", help="base branch (default: detected)")
    sp.add_argument("--no-session", action="store_true", help="only scaffold; don't open Claude Code")
    sp.add_argument("--print-command", action="store_true")

    sp = add("verify", cmd_verify, "Check the engine and a profile (config, checks.sh on the base branch, queue)")
    sp.add_argument("--no-checks", action="store_true", help="skip running checks.sh")
    sp.add_argument("--engine-only", action="store_true")

    sp = add("status", cmd_status, "List jobs", where=False)
    sp.add_argument("--project", "-p")
    sp.add_argument("--all", action="store_true", help="include finished jobs")

    add("queue", cmd_queue, "Show the running job, jobs waiting for you, and the backlog")

    sp = add("dashboard", cmd_dashboard, "Serve the read-only job board", where=False)
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8737)

    sp = add("new", cmd_new, "Create a job without opening Claude Code; prints its id")
    sp.add_argument("request", nargs="*")
    sp.add_argument("--next", action="store_true")
    sp.add_argument("--stdin", action="store_true")

    add("step", cmd_step, "Advance a job from its files and print the next action as JSON", job=True)
    sp = add("gate", cmd_gate, "Run the gate for a job's current round", job=True)
    sp.add_argument("--dry-run", action="store_true", help="run checks.sh in the worktree, record nothing")
    add("deliver", cmd_deliver, "Deliver an approved job with the profile's delivery method", job=True)

    sp = add("reply", cmd_reply, "Answer intake's questions for a job in needs-info", job=True)
    sp.add_argument("text", nargs="*")
    sp.add_argument("--stdin", action="store_true")
    sp = add("rework", cmd_rework, "Send a waiting or handed-off job back to the builder with a note", job=True)
    sp.add_argument("text", nargs="*")
    sp.add_argument("--stdin", action="store_true")
    add("retry", cmd_retry, "Retry the step that failed for a job in needs-human", job=True)
    for name, func, text in (("approve", cmd_approve, "Accept a job in needs-human and deliver it (person only)"),
                             ("abandon", cmd_abandon, "Drop a job and delete its worktree and branch (person only)"),
                             ("trust", cmd_trust, "Accept changed checks.sh/config.json for a job (person only)")):
        sp = add(name, func, text, job=True)
        sp.add_argument("--yes", "-y", action="store_true", help="skip the confirmation (still needs a terminal)")

    add("show", cmd_show, "Print a job's job.json", job=True)
    sp = add("brief", cmd_brief, "Write a brief for a role (debugging)", job=True)
    sp.add_argument("role", choices=["intake", "spec-writer", "builder", "reviewer", "approver"])
    sp.add_argument("--lens")
    sp = add("route", cmd_route, "Show the reviewers and models for a risk and surfaces")
    sp.add_argument("risk", choices=profiles.RISKS)
    sp.add_argument("--surfaces", help="comma-separated, e.g. ui,security")
    add("resolve", cmd_resolve, "Print which profile the current repo (or --repo/--project) resolves to")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    claude_args: List[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, claude_args = argv[:i], argv[i + 1:]
    args = build_parser().parse_args(argv)
    args.claude_args = claude_args
    try:
        return args.func(args)
    except FactoryError as e:
        print(f"factory: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
