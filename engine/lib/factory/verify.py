"""`factory verify`: is the engine sound, and is a profile ready to run?

Engine checks: plugin manifest, agent and skill frontmatter (Claude Code
ignores unknown keys silently, so a typo would quietly change behaviour),
lens files. Profile checks: config schema, checks.sh passing on a fresh
checkout of the base branch, the backlog parsing, and the target repo left
untouched by all of it.
"""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import List, Tuple

from . import jobs as jobmod
from .delivery import method_for
from .gate import run_checks, tail
from .profile import Profile, base_branch_exists, normalize_remote, validate
from .util import ENGINE, FactoryError, git, read_json, run

# Keys Claude Code reads from a subagent file. Plugin subagents ignore hooks,
# mcpServers, permissionMode and initialPrompt, so those count as errors here.
AGENT_KEYS = {
    "name", "description", "tools", "disallowedTools", "model", "maxTurns", "autoCompactWindow", "skills",
    "memory", "background", "omitClaudeMd", "effort", "isolation", "color", "experimental",
}
AGENT_KEYS_IGNORED_IN_PLUGINS = {"hooks", "mcpServers", "permissionMode", "initialPrompt"}
SKILL_KEYS = {
    "name", "description", "when_to_use", "argument-hint", "arguments", "disable-model-invocation",
    "user-invocable", "allowed-tools", "disallowed-tools", "model", "effort", "context", "agent",
    "background", "hooks", "paths", "shell", "metadata", "license", "compatibility",
}
EXPECTED_AGENTS = ("intake", "spec-writer", "builder", "reviewer", "approver")


def frontmatter(path: Path) -> Tuple[dict, str]:
    """Top-level `key: value` pairs of a markdown file's YAML frontmatter, and the body."""
    text = path.read_text()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        return {}, text
    keys = {}
    for line in m.group(1).splitlines():
        km = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if km:
            keys[km.group(1)] = km.group(2).strip()
    return keys, m.group(2)


class Report:
    def __init__(self):
        self.errors = 0

    def ok(self, msg: str) -> None:
        print(f"  ok    {msg}")

    def warn(self, msg: str) -> None:
        print(f"  warn  {msg}")

    def fail(self, msg: str) -> None:
        self.errors += 1
        print(f"  FAIL  {msg}")


def check_engine(r: Report) -> None:
    print("engine")
    try:
        manifest = read_json(ENGINE / ".claude-plugin" / "plugin.json")
        if manifest.get("name") != "factory":
            r.fail("plugin.json name must be `factory` (skills and agents are addressed as factory:<name>)")
        else:
            r.ok("plugin.json parses; plugin name `factory`")
    except (FactoryError, FileNotFoundError) as e:
        r.fail(f"plugin.json: {e}")
    try:
        read_json(ENGINE / "defaults.json")
        r.ok("defaults.json parses")
    except FactoryError as e:
        r.fail(str(e))

    for name in EXPECTED_AGENTS:
        path = ENGINE / "agents" / f"{name}.md"
        if not path.is_file():
            r.fail(f"agents/{name}.md is missing")
    for path in sorted((ENGINE / "agents").glob("*.md")):
        check_agent_file(path, r)

    for path in sorted((ENGINE / "skills").glob("*/SKILL.md")):
        keys, _ = frontmatter(path)
        rel = path.relative_to(ENGINE)
        unknown = sorted(set(keys) - SKILL_KEYS)
        if unknown:
            r.fail(f"{rel}: unsupported frontmatter keys {', '.join(unknown)}")
        elif not keys.get("description"):
            r.fail(f"{rel}: needs a description")
        else:
            r.ok(f"{rel}: frontmatter keys {', '.join(sorted(keys))}")

    for lens in ("code", "security", "ux", "ui"):
        if not (ENGINE / "lenses" / f"{lens}.md").is_file():
            r.fail(f"lenses/{lens}.md is missing")
    claude = shutil.which("claude")
    if claude:
        res = run([claude, "plugin", "validate", str(ENGINE)], check=False, timeout=120)
        (r.ok if res.returncode == 0 else r.fail)(f"`claude plugin validate engine` exited {res.returncode}")
    else:
        r.warn("`claude` isn't on PATH, so `claude plugin validate` was skipped")


def check_agent_file(path: Path, r: Report) -> None:
    keys, body = frontmatter(path)
    rel = path.relative_to(ENGINE) if path.is_relative_to(ENGINE) else path
    ignored = sorted(set(keys) & AGENT_KEYS_IGNORED_IN_PLUGINS)
    unknown = sorted(set(keys) - AGENT_KEYS - AGENT_KEYS_IGNORED_IN_PLUGINS)
    if ignored:
        r.fail(f"{rel}: {', '.join(ignored)} {'is' if len(ignored) == 1 else 'are'} ignored for plugin agents")
    if unknown:
        r.fail(f"{rel}: unknown frontmatter keys {', '.join(unknown)}")
    if not keys.get("name") or not keys.get("description"):
        r.fail(f"{rel}: needs name and description")
    elif keys["name"] != path.stem:
        r.fail(f"{rel}: name `{keys['name']}` should match the file name `{path.stem}`")
    if not ignored and not unknown and keys.get("name") and keys.get("description"):
        r.ok(f"{rel}: frontmatter keys {', '.join(sorted(keys))}")
    if not body.strip():
        r.fail(f"{rel}: empty prompt")


def check_profile(profile: Profile, r: Report, run_checks_too: bool = True) -> None:
    print(f"profile {profile.name} ({profile.dir})")
    errors = validate(profile)
    for e in errors:
        r.fail(f"config.json: {e}")
    if errors:
        return
    r.ok("config.json is valid")
    method, opts = method_for(profile)
    shown = ", ".join(f"{k}={v!r}" for k, v in opts.items() if v)
    r.ok(f"delivery: {method.name}" + (f" ({shown})" if shown else "") + f": {method.about}")

    repo = profile.repo_path
    remote = run(["git", "remote", "get-url", "origin"], cwd=repo, check=False).stdout.strip()
    if profile.remote and normalize_remote(remote) != normalize_remote(profile.remote):
        r.warn(f"repo.remote is {profile.remote!r} but {repo} has origin {remote or 'unset'}: "
               "resolving this profile by remote won't find it")
    else:
        r.ok(f"repo {repo} (origin {remote or 'none'})")
    base = profile.config["base_branch"]
    if not base_branch_exists(profile):
        r.fail(f"base branch {base!r} doesn't exist in {repo}")
        return
    r.ok(f"base branch {base} exists")

    if not profile.checks.is_file():
        r.fail("checks.sh is missing")
    elif "still the template" in profile.checks.read_text():
        r.fail("checks.sh is still the template")
    for name in ("project", "intake"):
        if not profile.context(name):
            r.warn(f"context/{name}.md is empty or still the template")
    for leftover in (".claude/agents", ".claude/skills/factory", "factory/checks.sh"):
        if (repo / leftover).exists():
            r.warn(f"{repo / leftover} exists in the project; this factory doesn't need it")

    try:
        pending = [i for i in jobmod.parse_backlog(profile) if not i["done"]]
        running = jobmod.active_jobs(profile)
        r.ok(f"`factory queue` works: {len(pending)} backlog item(s) waiting, {len(running)} job(s) running")
    except FactoryError as e:
        r.fail(f"queue: {e}")

    if run_checks_too and profile.checks.is_file():
        verify_checks_on_base(profile, r)


def verify_checks_on_base(profile: Profile, r: Report) -> None:
    repo = profile.repo_path
    before = git(repo, "status", "--porcelain")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    wt = profile.worktree_root / f"_verify-{stamp}"
    log = profile.logs_dir / f"verify-{stamp}.log"
    profile.worktree_root.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "--quiet", "--detach", str(wt), f"refs/heads/{profile.config['base_branch']}")
    try:
        ok, summary = jobmod.run_setup(profile, wt, profile.logs_dir / f"verify-{stamp}-setup.log")
        if not ok:
            r.fail(f"setup on a fresh checkout: {summary} (log: {profile.logs_dir}/verify-{stamp}-setup.log)")
            return
        code, seconds = run_checks(profile, wt, log, None)
        if code == 0:
            r.ok(f"checks.sh passes on a fresh checkout of {profile.config['base_branch']} ({seconds}s)")
        else:
            r.fail(f"checks.sh exits {code} on a fresh checkout of {profile.config['base_branch']}. "
                   f"The factory could never pass a job. Log: {log}\n" + tail(log, 25))
    finally:
        run(["git", "worktree", "remove", "--force", str(wt)], cwd=repo, check=False)
        if wt.exists():
            shutil.rmtree(wt, ignore_errors=True)
        run(["git", "worktree", "prune"], cwd=repo, check=False)
    after = git(repo, "status", "--porcelain")
    (r.ok if before == after else r.fail)("the project's working tree is unchanged by verification")


def verify(profiles: List[Profile], engine: bool = True, run_checks_too: bool = True) -> int:
    r = Report()
    if engine:
        check_engine(r)
    for profile in profiles:
        check_profile(profile, r, run_checks_too)
    print(f"\n{'all good' if r.errors == 0 else f'{r.errors} problem(s)'}")
    return 0 if r.errors == 0 else 1
