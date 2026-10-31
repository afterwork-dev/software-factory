"""Project profiles: projects/<name>/ holds one target repo's config, checks and notes.

The engine is generic; everything that differs between projects lives in a
profile. A profile is picked by name, or by matching a repo's git remote
against each profile's `repo.remote`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional

from .util import ENGINE, ROOT, FactoryError, git, projects_dir, read_json, run, strip_comments

MODELS = ("haiku", "sonnet", "opus", "fable")  # what the Agent tool accepts, weakest first
RISKS = ("low", "medium", "high")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
TOP_KEYS = {
    "template", "description", "repo", "base_branch", "delivery", "branch_prefix",
    "max_rounds", "protected_paths", "ports", "commands", "browser", "worktree_root",
    "setup_timeout_s", "checks_timeout_s", "models", "routing", "surfaces", "spec_may_unlock_protected",
}
MODEL_ROLES = ("intake", "spec-writer", "approver")


def defaults() -> dict:
    return read_json(ENGINE / "defaults.json")


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def strip_doc_keys(value):
    """Keys starting with `_` are comments in our JSON files."""
    if isinstance(value, dict):
        return {k: strip_doc_keys(v) for k, v in value.items() if not k.startswith("_")}
    if isinstance(value, list):
        return [strip_doc_keys(v) for v in value]
    return value


def normalize_remote(url: str) -> str:
    """`git@github.com:me/app.git`, `https://github.com/me/app` → `github.com/me/app`."""
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    if u.endswith(".git"):
        u = u[:-4]
    if u.startswith("file://"):
        return "file:" + os.path.realpath(u[7:])
    if u.startswith("/") or u.startswith("."):
        return "file:" + os.path.realpath(u)
    m = re.match(r"^[a-z][a-z0-9+.-]*://(?:[^@/]*@)?([^/:]+)(?::\d+)?/(.*)$", u, re.I)
    if not m:
        m = re.match(r"^(?:[^@/]+@)?([^:/]+):(.*)$", u)
    if not m:
        return u.lower()
    host, path = m.groups()
    return f"{host}/{path.strip('/')}".lower()


class Profile:
    def __init__(self, name: str, path: Path):
        self.name = name
        self.dir = path
        config_path = path / "config.json"
        if not config_path.is_file():
            raise FactoryError(f"profile {name!r} has no config.json ({config_path})")
        self.raw = read_json(config_path)
        if not isinstance(self.raw, dict):
            raise FactoryError(f"{config_path} must hold a JSON object")
        self.config = strip_doc_keys(deep_merge(defaults(), self.raw))

    def __repr__(self):
        return f"Profile({self.name!r})"

    @property
    def template(self) -> bool:
        return bool(self.raw.get("template"))

    @property
    def repo(self) -> dict:
        return self.config.get("repo") or {}

    @property
    def repo_path(self) -> Path:
        return Path(self.repo.get("path", "")).expanduser()

    @property
    def remote(self) -> str:
        return self.repo.get("remote", "")

    @property
    def config_path(self) -> Path:
        return self.dir / "config.json"

    @property
    def checks(self) -> Path:
        return self.dir / "checks.sh"

    @property
    def backlog(self) -> Path:
        return self.dir / "backlog.md"

    @property
    def jobs_dir(self) -> Path:
        return self.dir / "jobs"

    @property
    def logs_dir(self) -> Path:
        return self.dir / "logs"

    @property
    def lock(self) -> Path:
        return self.dir / ".lock"

    @property
    def worktree_root(self) -> Path:
        configured = self.config.get("worktree_root") or ""
        if not configured:
            return self.dir / "worktrees"
        path = Path(configured).expanduser()
        return path if path.is_absolute() else (self.dir / path).resolve()

    def context(self, name: str) -> str:
        """A note from context/<name>.md with its HTML comments removed ('' when absent or still a template)."""
        path = self.dir / "context" / f"{name}.md"
        return strip_comments(path.read_text()) if path.is_file() else ""

    def lens_file(self, lens: str) -> Optional[Path]:
        """A profile lens replaces the engine lens of the same name."""
        for path in (self.dir / "lenses" / f"{lens}.md", ENGINE / "lenses" / f"{lens}.md"):
            if path.is_file():
                return path
        return None


def all_profiles(include_templates: bool = False) -> List[Profile]:
    base = projects_dir()
    if not base.is_dir():
        return []
    out = []
    for path in sorted(base.iterdir()):
        if path.is_dir() and NAME_RE.match(path.name) and (path / "config.json").is_file():
            profile = Profile(path.name, path)
            if include_templates or not profile.template:
                out.append(profile)
    return out


def load(name: str) -> Profile:
    path = projects_dir() / name
    if not NAME_RE.match(name or "") or not (path / "config.json").is_file():
        names = ", ".join(p.name for p in all_profiles()) or "none yet"
        raise FactoryError(f"no profile named {name!r} in {projects_dir()} (profiles: {names})")
    profile = Profile(name, path)
    if profile.template:
        raise FactoryError(
            f"{name!r} is a template. Create a real profile with `bin/factory add-project <name> --repo <path>`."
        )
    return profile


def repo_identity(path: Path):
    """(toplevel, normalized origin remote) for a directory inside a git repo, else (None, '')."""
    res = run(["git", "rev-parse", "--show-toplevel"], cwd=path, check=False)
    if res.returncode != 0:
        return None, ""
    top = Path(res.stdout.strip()).resolve()
    remote = run(["git", "remote", "get-url", "origin"], cwd=top, check=False).stdout.strip()
    return top, normalize_remote(remote)


def resolve(project: Optional[str] = None, repo: Optional[str] = None, cwd: Optional[str] = None) -> Profile:
    """Pick the profile: --project, else --repo's remote, else the cwd's remote, else the only profile."""
    if project:
        return load(project)
    profiles = all_profiles()
    for target, explicit in ((repo, True), (cwd, False)):
        if not target:
            continue
        top, remote = repo_identity(Path(target).expanduser())
        if top is None or top == ROOT.resolve():
            if explicit:
                raise FactoryError(f"{target} is not inside a git repository")
            continue
        matches = [p for p in profiles if remote and normalize_remote(p.remote) == remote]
        if not matches:
            matches = [p for p in profiles if p.repo_path.expanduser().resolve() == top]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            names = ", ".join(p.name for p in matches)
            raise FactoryError(f"several profiles match {top}: {names}. Pass --project.")
        if explicit:
            raise FactoryError(
                f"no profile matches {top} (origin: {remote or 'none'}). "
                f"Add one with `bin/factory add-project <name> --repo {top}`."
            )
    if len(profiles) == 1:
        return profiles[0]
    names = ", ".join(p.name for p in profiles)
    if not profiles:
        raise FactoryError("no project profiles yet. Add one with `bin/factory add-project <name> --repo <path>`.")
    raise FactoryError(f"can't tell which project you mean. Pass --project (profiles: {names}).")


def validate(profile: Profile) -> List[str]:
    """Problems that stop the factory from running this profile. Empty means OK."""
    raw, cfg, errors = profile.raw, profile.config, []

    def err(msg):
        errors.append(msg)

    for key in raw:
        if key == "merge_method":
            err("merge_method moved: use \"delivery\": {\"method\": \"local-merge\", \"strategy\": \"...\"} "
                "to merge locally, or leave delivery at its default, handoff")
        elif key not in TOP_KEYS and not key.startswith("_"):
            err(f"unknown key {key!r} (typo?)")

    repo = cfg.get("repo")
    if not isinstance(repo, dict) or not isinstance(repo.get("path"), str) or not repo.get("path"):
        err("repo.path must be the absolute path of the project's git checkout")
    else:
        for key in repo:
            if key not in ("path", "remote") and not key.startswith("_"):
                err(f"unknown key repo.{key}")
        if not isinstance(repo.get("remote", ""), str):
            err("repo.remote must be a string")
        if not profile.template:
            path = profile.repo_path
            if not path.is_absolute():
                err("repo.path must be absolute")
            elif not path.is_dir():
                err(f"repo.path {path} doesn't exist")
            elif run(["git", "rev-parse", "--git-dir"], cwd=path, check=False).returncode != 0:
                err(f"repo.path {path} is not a git repository")

    base = cfg.get("base_branch")
    if not isinstance(base, str) or not base:
        err("base_branch must be a branch name")
    from . import delivery  # imported here: delivery imports jobs, which imports this module
    for problem in delivery.validate(profile):
        err(problem)
    prefix = cfg.get("branch_prefix")
    if not isinstance(prefix, str) or not prefix or \
            run(["git", "check-ref-format", "--branch", f"{prefix}001-x"], check=False).returncode != 0:
        err("branch_prefix must make valid branch names, like `factory/`")

    if not isinstance(cfg.get("spec_may_unlock_protected"), bool):
        err("spec_may_unlock_protected must be true or false")
    if not isinstance(cfg.get("protected_paths"), list) or \
            not all(isinstance(p, str) and p for p in cfg.get("protected_paths", [])):
        err("protected_paths must be a list of path patterns")

    ports = cfg.get("ports")
    if not isinstance(ports, dict) or not _int_in(ports.get("base"), 1024, 65000) or \
            not _int_in(ports.get("step"), 1, 1000):
        err("ports needs integer `base` (1024-65000) and `step` (1-1000)")

    commands = cfg.get("commands")
    if not isinstance(commands, dict) or not all(isinstance(v, str) for v in commands.values()):
        err("commands must map names to shell command strings")
    if not isinstance(cfg.get("browser"), str):
        err("browser must be a string (a CLI name, or empty)")
    if not isinstance(cfg.get("worktree_root"), str):
        err("worktree_root must be a string (empty means projects/<name>/worktrees)")
    for key in ("setup_timeout_s", "checks_timeout_s"):
        if not _int_in(cfg.get(key), 1, 86400):
            err(f"{key} must be a whole number of seconds")
    if not _int_in(cfg.get("max_rounds"), 1, 10):
        err("max_rounds must be 1-10")

    models = cfg.get("models")
    if not isinstance(models, dict):
        err("models must be an object")
    else:
        for role in MODEL_ROLES:
            if models.get(role) not in MODELS:
                err(f"models.{role} must be one of {', '.join(MODELS)}")
        for role in models:
            if role not in MODEL_ROLES:
                err(f"unknown role models.{role}")

    lenses_used = set()
    routing = cfg.get("routing")
    if not isinstance(routing, dict) or set(routing) != set(RISKS):
        err("routing needs exactly the keys low, medium, high")
    else:
        for risk, entry in routing.items():
            if not isinstance(entry, dict) or entry.get("builder") not in MODELS:
                err(f"routing.{risk}.builder must be one of {', '.join(MODELS)}")
                continue
            lenses_used |= _check_lenses(entry.get("lenses"), f"routing.{risk}.lenses", err)

    surfaces = cfg.get("surfaces")
    if not isinstance(surfaces, dict):
        err("surfaces must be an object")
    else:
        for name, entry in surfaces.items():
            if not NAME_RE.match(name):
                err(f"surface name {name!r} must be lowercase letters, digits and dashes")
            if not isinstance(entry, dict) or not isinstance(entry.get("about"), str):
                err(f"surfaces.{name} needs an `about` sentence for intake")
                continue
            lenses_used |= _check_lenses(entry.get("lenses"), f"surfaces.{name}.lenses", err)

    for lens in sorted(lenses_used):
        if profile.lens_file(lens) is None:
            err(f"lens {lens!r} has no checklist: add projects/{profile.name}/lenses/{lens}.md")
    return errors


def _int_in(value, low, high) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _check_lenses(lenses, where, err) -> set:
    if not isinstance(lenses, dict):
        err(f"{where} must map lens names to models")
        return set()
    for lens, model in lenses.items():
        if not NAME_RE.match(lens):
            err(f"{where}: lens name {lens!r} must be lowercase letters, digits and dashes")
        if model not in MODELS:
            err(f"{where}.{lens} must be one of {', '.join(MODELS)}")
    return set(lenses)


def base_branch_exists(profile: Profile) -> bool:
    ref = f"refs/heads/{profile.config['base_branch']}"
    return run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=profile.repo_path, check=False).returncode == 0


def head_of(profile: Profile) -> str:
    return git(profile.repo_path, "rev-parse", "--verify", f"refs/heads/{profile.config['base_branch']}^{{commit}}")
