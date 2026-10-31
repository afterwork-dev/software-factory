"""`factory dashboard`: a read-only board of every profile's jobs, served on localhost.

It reads job folders on each request and stores nothing. File reads are
limited to files inside a job folder.
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .jobs import jobs_of
from .profile import all_profiles
from .util import ENGINE, first_line, is_relative_to, now

MAX_FILE_BYTES = 1_000_000
HIDDEN = re.compile(r"(^|/)\.|\.stale-\d+$")


def job_files(job_dir: Path):
    return sorted(str(p.relative_to(job_dir)) for p in job_dir.rglob("*")
                  if p.is_file() and not HIDDEN.search(str(p.relative_to(job_dir))))


def rounds_of(job) -> dict:
    out = {}
    lenses = list(((job.data.get("agents") or {}).get("lenses") or {}).keys())
    for n in range(1, job.round + 1):
        rd = job.round_dir(n)
        if not rd.is_dir():
            continue
        gate = first_line(rd / "checks.md").replace("GATE: ", "") or None
        reviews = {}
        for lens in lenses:
            line = first_line(rd / f"review-{lens}.md")
            reviews[lens] = line.replace("VERDICT: ", "") if line.startswith("VERDICT:") else None
        out[str(n)] = {"gate": gate, "reviews": reviews}
    return out


def snapshot() -> dict:
    jobs = []
    profiles = all_profiles()
    for profile in profiles:
        for job in jobs_of(profile):
            data = dict(job.data)
            data.pop("attempts", None)
            data.pop("awaiting", None)
            data["project"] = profile.name
            data["files"] = job_files(job.dir)
            data["rounds"] = rounds_of(job)
            jobs.append(data)
    return {"generated": now(), "projects": [p.name for p in profiles], "jobs": jobs}


def read_job_file(project: str, job_id: str, rel: str):
    for profile in all_profiles():
        if profile.name != project:
            continue
        job_dir = profile.jobs_dir / job_id
        target = (job_dir / rel).resolve()
        if not job_dir.is_dir() or not is_relative_to(target, job_dir) or not target.is_file():
            return None
        data = target.read_bytes()[:MAX_FILE_BYTES]
        return data.decode("utf-8", errors="replace")
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self.send(200, (ENGINE / "dashboard" / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/api/jobs":
            self.send(200, json.dumps(snapshot()).encode(), "application/json")
        elif url.path == "/api/file":
            q = parse_qs(url.query)
            text = read_job_file(q.get("project", [""])[0], q.get("job", [""])[0], q.get("path", [""])[0])
            if text is None:
                self.send(404, b"not found", "text/plain; charset=utf-8")
            else:
                self.send(200, text.encode(), "text/plain; charset=utf-8")
        else:
            self.send(404, b"not found", "text/plain; charset=utf-8")


def serve(host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"factory dashboard on http://{host}:{port}/ (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
