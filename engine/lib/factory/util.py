"""Small helpers shared by every module. Standard library only, Python 3.9+."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

ENGINE = Path(__file__).resolve().parents[2]
ROOT = ENGINE.parent


class FactoryError(Exception):
    """A refusal or failure the user should read. The CLI prints it and exits 1."""


def projects_dir() -> Path:
    """Where profiles live. FACTORY_PROJECTS_DIR moves them (tests, private profile repos)."""
    env = os.environ.get("FACTORY_PROJECTS_DIR")
    return Path(env).expanduser().resolve() if env else ROOT / "projects"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(cmd, cwd=None, check=True, timeout=None, env=None, input=None) -> subprocess.CompletedProcess:
    res = subprocess.run(
        [str(c) for c in cmd], cwd=cwd, capture_output=True, text=True,
        timeout=timeout, env=env, input=input,
    )
    if check and res.returncode != 0:
        detail = (res.stderr or res.stdout).strip()
        raise FactoryError(f"`{' '.join(str(c) for c in cmd)}` failed: {detail}")
    return res


def git(cwd, *args, check=True) -> str:
    """stdout of a git command with only the trailing newline removed (porcelain output keeps its leading spaces)."""
    return run(["git", *args], cwd=cwd, check=check).stdout.rstrip("\n")


def read_json(path: Path):
    try:
        return json.loads(Path(path).read_text())
    except json.JSONDecodeError as e:
        raise FactoryError(f"{path} is not valid JSON: {e}")


def write_json(path: Path, data) -> None:
    write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _default_mode() -> int:
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def write_text(path: Path, text: str) -> None:
    """Atomic write: readers (the dashboard) never see a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else _default_mode()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


@contextmanager
def locked(path: Path) -> Iterator[None]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def sha256(path: Path) -> Optional[str]:
    path = Path(path)
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def first_line(path: Path) -> str:
    path = Path(path)
    if not path.is_file():
        return ""
    with open(path, errors="replace") as f:
        return f.readline().strip()


def header_fields(path: Path) -> dict:
    """`KEY: value` lines at the top of a file, up to the first blank line or heading."""
    fields = {}
    for line in Path(path).read_text(errors="replace").splitlines():
        if not line.strip() or line.startswith("#"):
            break
        m = re.match(r"^([A-Z][A-Z-]*):\s*(.*)$", line.strip())
        if m:
            fields[m.group(1)] = m.group(2).strip()
    return fields


def section(text: str, heading: str) -> str:
    """Body of `## <heading>` up to the next `## ` heading."""
    m = re.search(rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S | re.I)
    return m.group(1).strip() if m else ""


def strip_comments(text: str) -> str:
    """Drop HTML comments, which template files use for guidance that agents shouldn't see."""
    return re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()


def slugify(title: str, max_words: int = 4, max_len: int = 40) -> str:
    words = re.findall(r"[a-z0-9]+", title.lower())
    slug = "-".join(words[:max_words])[:max_len].strip("-")
    return slug or "job"


def mtime(path: Path) -> float:
    try:
        return Path(path).stat().st_mtime
    except FileNotFoundError:
        return 0.0


def require_person(action: str, yes: bool = False) -> None:
    """Refuse unless a person at a terminal runs this and confirms.

    Agents run commands with a piped stdin, so they can never pass this check.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise FactoryError(
            f"`{action}` must be run by a person in a terminal, not by an agent or a script."
        )
    if yes:
        return
    answer = input(f"{action}? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        raise FactoryError("cancelled")


def is_relative_to(path: Path, other: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(other).resolve())
        return True
    except ValueError:
        return False
