# Software Factory for Claude Code

A local, self-contained software factory. You give it a feature request; agents
decide whether it's worth building, write a spec, build it on its own branch,
test it with your project's own checks, review it through the lenses that
matter for that change, and hand you an approved branch, or a precise
question when it needs your decision. What happens to an approved branch is up
to each project: by default the factory stops there and you push it and open a
pull request.

```
request (argument or backlog.md)
  → intake ─ REJECT / NEEDS-INFO (waits for your answer)
  → route (script: reviewers + models from risk and surfaces)
  → spec-writer → builder → gate (script: your checks.sh) ─FAIL→ builder
  → reviewers (in parallel, wait for all) ─CHANGES→ builder → gate …
  → approver ─ APPROVE  → deliver (per project; default: hand you the approved branch)
             └ ESCALATE → waits for you
```

One job at a time, one Claude Code session per job. It never pushes, and it
needs no GitHub issues or pull requests to run.

**The factory lives here, not in your projects.** Your project repos get no
`.claude/`, no agent files, no factory folder, nothing to commit. Everything,
the engine and your per-project profiles, lives in this folder, and nothing is
installed into `~/.claude/`.

## Requirements

- Claude Code with the `claude` CLI on your PATH (or set `FACTORY_CLAUDE_BIN`)
- git 2.38 or later, and Python 3.9 or later (standard library only)
- Optional: a page-viewing CLI such as `agent-browser`, for UI and UX reviews

## Layout

```
bin/factory                 the entry point
engine/                     generic, shared by every project; a Claude Code plugin
  .claude-plugin/plugin.json  plugin "factory": /factory:run, factory:builder, …
  skills/                     run (the orchestrator), add-project, rework, reply, status
  agents/                     intake, spec-writer, builder, reviewer, approver
  lenses/                     review checklists: code, security, ux, ui
  bin/factory                 the CLI (on the Bash PATH inside factory sessions)
  lib/factory/                profiles, jobs, step (state machine), gate, route, brief, verify, dashboard
  lib/factory/delivery/       what happens after APPROVE: handoff (default), local-merge (opt-in)
  defaults.json               routing table, models, rounds, timeouts
  settings.json               permission rules for factory sessions
  dashboard/index.html        the job board
projects/
  example/                  the committed template profile
  <name>/                   your profiles: gitignored, one per target repo
    config.json             repo path and remote, base branch, delivery, ports, protected paths, commands
    checks.sh               the repo's real lint/typecheck/test/build: what the gate runs
    backlog.md              queue for `--next`
    context/*.md            notes, each given only to the agent that needs it
    lenses/*.md             optional extra or replacement review checklists
    jobs/ worktrees/ logs/  runtime state (ignored)
tests/                      unit tests for the scripts
```

`DESIGN.md` explains how the pieces fit and why.

## Add a project

```bash
bin/factory add-project myapp --repo ~/code/myapp
```

This copies `projects/example/` to `projects/myapp/`, fills in the repo path,
`origin` remote, base branch and a free port range, then opens Claude Code with
`/factory:add-project myapp`. That session:

1. reads the repo (README, manifests, CI config, tests, lint and typecheck
   config, design tokens, auth and data-access code) without changing it;
2. fills `checks.sh` with the project's real checks, and `config.json` with its
   commands and protected test paths;
3. writes the short `context/` notes: what the project is, what belongs in it,
   security hot spots, where the design tokens live, the kinds of users, and
   what must always reach a person;
4. runs `bin/factory verify --project myapp` until it passes;
5. offers, only offers, to write or extend the repo's `AGENTS.md` with plain
   contributor info (setup, dev server, test commands). It's useful to people
   too, and the factory works without it.

Prefer to do it by hand? `--no-session` only scaffolds; edit the files, then
run `bin/factory verify --project myapp`.

`verify` checks the engine (plugin manifest; agent and skill frontmatter
against the keys Claude Code supports; lenses), validates `config.json`, runs
`checks.sh` on a fresh temporary worktree of the base branch (if it fails
there, no job could ever pass), checks the backlog and queue, and confirms your
repo's working tree is untouched.

## Run a job

From anywhere inside the project, the profile is found by the repo's git remote:

```bash
cd ~/code/myapp
~/software-factory/bin/factory run "Show the app version in the footer"
```

Or from the factory folder:

```bash
bin/factory run --project myapp "Show the app version in the footer"
bin/factory run --project myapp --next            # first unchecked item in backlog.md
bin/factory run --project myapp --file request.md # first line is the title
bin/factory run --project myapp                   # resume the job in progress
bin/factory run --project myapp -- --permission-mode acceptEdits   # args after -- go to claude
```

`run` creates the job first. It fetches the base branch from `origin` and
fast-forwards it when it is strictly behind; if it is ahead, diverged, offline,
has no `origin`, or a dirty checkout blocks the fast-forward, the job starts
from the local branch and the reason goes to the job's `events.log`. Then it
creates the job folder, branch `factory/<job-id>`, worktree,
and `commands.setup`. Then it starts Claude Code in this folder with the engine
loaded as a plugin, running `/factory:run --project myapp --job <id>`. That
session orchestrates the job and stops when it is delivered or needs you.

Inside any session started with the plugin (`claude --plugin-dir engine` from
this folder), the skills are `/factory:run`, `/factory:rework`,
`/factory:reply` and `/factory:status`.

The builder runs real commands (installs, tests, dev servers), so expect
permission prompts in the default mode. Pass a mode after `--` if you prefer.
Whatever the mode, the session's deny rules stop agents from editing the engine,
any profile's config, checks or notes, and any `job.json`.

## What the factory settles on its own

It is built to run without you, so agents settle what an engineer on the
project would settle:

- **Technical choices** are made by the spec writer and the builder, not
  passed to you.
- **A protected test that pins the old behaviour** of something the request
  changes is named in the spec under `## Protected changes`. The gate then lets
  the builder update exactly that file, and code review checks the change.
  Protected files the spec doesn't name still fail the gate.
- **A spec that stands in the builder's way** goes back to the spec writer
  (`STATUS: SPEC-CHANGE`), and the same builder carries on against the revised
  spec. Workarounds are ruled out in the builder's instructions, the code lens
  and the approver's checklist.
- **The approver sends fixable problems back** (`CHANGES`) for another round.
  It escalates only what no agent should decide: business choices with no
  sensible default, contradictory requirements, missing credentials or access,
  and what your profile's `context/approver.md` says to always escalate.
- **With handoff, a person reviews the PR anyway.** So changes in sensitive
  areas (sign-in, permissions, dependencies, …) and checks the factory couldn't
  run are passed on as notes for that reviewer, not as stops. With local-merge,
  nobody looks afterwards, so the approver escalates sensitive areas.

## When a job needs you

The session ends with what happened and your options. All of these work from a
terminal in the factory folder:

| Command | When |
|---|---|
| `bin/factory reply <job> "answers"` | intake asked questions (`needs-info`) |
| `bin/factory rework <job> "note"` | send it back with guidance: the spec writer updates `spec.md` to match your note, then the builder takes it, with a fresh set of rounds |
| `bin/factory retry <job>` | a step failed for a mechanical reason (agent didn't write its file, setup failed, delivery was blocked) |
| `bin/factory approve <job>` | accept an escalated job as it is and deliver it |
| `bin/factory abandon <job>` | drop it; deletes its worktree and branch |
| `bin/factory trust <job>` | you changed the profile's `checks.sh` or `config.json` mid-job on purpose |

After `reply`, `rework` or `retry`, continue with `bin/factory run --job <job>`.
`approve`, `abandon` and `trust` ask for confirmation and refuse to run without
a terminal, so no agent can run them. Nothing is ever pushed.

## When a job is delivered

What happens after APPROVE is the profile's `delivery` setting, because
projects differ: some require a pull request and protect the base branch, some
allow landing changes directly.

- **`handoff`** (default): the job ends as an approved branch in your repo,
  exactly the commit that passed the gate and the reviews. The worktree is
  removed, nothing is pushed or merged, and the session tells you what to do
  next, for example:

  ```
  git -C ~/code/myapp push -u origin factory/004-version-footer
  ```

  then open the pull request your project's way. Set `delivery.next_steps` to
  spell out your routine instead (`{branch}`, `{base}`, `{repo}` and `{job}` are
  filled in), such as `gh pr create --head {branch} --base {base} --fill`. If
  the PR review asks for changes, `bin/factory rework <job> "<what to change>"`
  checks the branch out again and sends it back through the spec, build, gate,
  review and approval. Push again afterwards. Anything the approver wants the
  PR reviewer to look at closely is printed with the next steps.
- **`local-merge`** (opt-in, for projects that allow it): merges into the base
  branch locally with no person looking. It first brings the branch up to date
  with the base branch and gates it again if that changed anything. Choose a
  strategy:

  ```json
  "delivery": { "method": "local-merge", "strategy": "squash" }
  ```

  `merge` makes a merge commit, `squash` one commit per job, `rebase` a linear
  history. If your main checkout has uncommitted changes, the job waits and
  `bin/factory retry <job>` finishes it.

More methods (a GitHub pull request opened by the factory, a script of your
own) fit the same slot; `DESIGN.md` explains how to add one.

Other commands: `bin/factory status`, `bin/factory queue --project myapp`,
`bin/factory step <job>` (what the orchestrator would do next),
`bin/factory --help`.

## Dashboard

```bash
bin/factory dashboard        # http://127.0.0.1:8737
```

A read-only board of every profile's jobs: jobs waiting for you at the top,
stages as columns, gate and review chips per round. Click a card to read every
file the job wrote, including the exact brief each agent got.

## Tune a profile

- **Routing**: `routing` (risk → builder model and review lenses) and
  `surfaces` (surface → extra lenses) in `engine/defaults.json`. Override any
  part in a profile's `config.json`; `null` removes a key.
- **Models** for intake, spec writer and approver: `models` (default `opus`).
- **Base branch sync**: `pull_base` (default `true`) fast-forwards `base_branch`
  from `origin` before each job. Set it to `false` to turn that off.
- **Rounds**: `max_rounds` (default 3) build → gate → review cycles per job.
- **Protected files**: `spec_may_unlock_protected` (default `true`) lets the
  spec unlock the protected tests a change must update. Set it to `false` to
  keep them untouchable; such jobs then come to you.
- **Extra lenses**: add `projects/<name>/lenses/<lens>.md` and route it through a
  surface. See `projects/example/lenses/README.md`.
- **Notes**: `context/<role>.md` reaches only that role (`project.md` reaches
  everyone except the security reviewer). HTML comments are stripped first.

## Keeping your profiles in git

`projects/*` is ignored here so the engine can be shared without your paths,
remotes and notes. To version a profile, make its folder its own repo. It ships
a `.gitignore` that keeps job state, worktrees and logs out:

```bash
cd projects/myapp && git init && git add -A && git commit -m "myapp profile"
git remote add origin git@github.com:you/factory-profile-myapp.git   # private
```

Or keep all your profiles in one private repo somewhere else and point the
factory at it:

```bash
export FACTORY_PROJECTS_DIR=~/code/factory-profiles
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

They build throwaway repos, play the agents by writing their files, and cover
the full path to an approved branch, routing and context isolation, the gate
(protected paths, dirty worktrees, failing checks, changed profiles), rounds
running out, planted or stale verdicts, intake questions and rejections, both
delivery methods (handoff with rework after a PR review; local merge with all
three strategies, a moved or dirty base branch and conflicts), unreviewed
commits after approval, updating the base branch from origin, and person-only commands.

## License

No license file is included yet. Add the one you choose.
