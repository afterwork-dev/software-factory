# Design

How the factory works and why. The source is small: one CLI, five agents,
four lenses, five skills, two delivery methods.

## Principles

1. **Projects stay clean.** The factory never writes agent, factory or AI files
   into a project's source tree, and needs nothing committed there. While a
   job runs, the only traces in a target repo are its branch
   `factory/<job-id>` and git's internal `.git/worktrees/<job-id>` entry. When
   it ends, the worktree entry goes. With the default delivery the approved
   branch stays, because it is what you receive.
2. **Scripts decide; agents judge.** Anything a rule can settle (stage
   changes, round limits, routing, parsing verdicts, running checks, delivery)
   is Python in `engine/lib/factory/`. Agents do what rules can't: decide if a
   request belongs, specify it, build it, review it, approve it.
3. **Explicit context.** Agents load nothing on their own. Each gets one
   brief, written by the engine, holding exactly what its role needs.
4. **Files are the state.** Every stage leaves a file. A crashed session loses
   nothing: `factory step` re-reads the files and picks up where the job was.

## Engine and profiles

`engine/` is a Claude Code plugin named `factory`. `bin/factory` starts
`claude --plugin-dir engine` from this folder, so the skills
(`/factory:run`, …), the agents (`factory:builder`, …) and the plugin's `bin/`
(which puts `factory` on the Bash PATH) exist for that session only. Nothing is
installed, and nothing goes into `~/.claude/`.

A profile, `projects/<name>/`, is everything that differs between projects:
where the repo is, its base branch and delivery method, its real checks, which
test files are protected, its commands and ports, and short notes for each
role. Real profiles are gitignored; `projects/example/` is the template.

**Picking the profile.** `--project <name>` wins. Otherwise the CLI takes the
repo (from `--repo`, or the directory you run it in), normalises its `origin`
remote (`git@host:org/repo.git` and `https://host/org/repo` compare equal), and
matches it against each profile's `repo.remote`, then against `repo.path`. If
there's exactly one real profile, that one. Anything ambiguous is an error
that lists the candidates. The template is never picked.

## A job

`factory run` creates the job before Claude Code starts:

- `projects/<name>/jobs/<NNN-slug>/` with `job.json` and `request.md`;
- branch `factory/<NNN-slug>` from the base branch's current commit;
- a worktree at `projects/<name>/worktrees/<NNN-slug>/` (configurable with
  `worktree_root`), outside the project's source tree;
- `commands.setup` run in the worktree;
- SHA-256 hashes of the profile's `checks.sh` and `config.json`.

Only one job per profile can be active at a time. Parked jobs (waiting for a
person) don't block new ones.

### Stages

```
intake ─ACCEPT→ spec ⇄ build → gate ─PASS→ review ─all PASS→ approve ─APPROVE→ delivering → delivered
  │REJECT→ rejected  ▲ ▲  SPEC-CHANGE │FAIL       │CHANGES        │CHANGES  │ESCALATE
  │NEEDS-INFO→       │ └──────────────┴───────────┴───────────────┘ (next round)
  │  needs-info      └ rework (a person's note)                               ▼
  └────────────────────────────── any failure, or rounds used up → needs-human
```

`factory step <job>` is the state machine. Each call:

1. reads the output file the current stage is waiting for;
2. counts it only if it was written **after** the agent was started (its mtime
   is compared with the spawn time recorded in `job.json`). Before an agent
   starts, an old file in its slot is moved aside, so a verdict planted earlier,
   by the builder for example, can never count;
3. parses the first line (`DECISION:`, `STATUS:`, `VERDICT:`, `APPROVE`), and
   either advances the stage or parks the job with a reason;
4. prints the next action as JSON: `spawn` (agents, models, prompts), `run`
   (a factory command), `stop` (what the person should do), or `done`.

The orchestrating skill (`engine/skills/run/SKILL.md`) is deliberately thin:
it runs `factory step`, does exactly what the JSON says, and repeats. It never
reads agents' files and never edits state.

If an agent finishes without writing its file (it put its review in its reply,
say), the next `step` doesn't start over. It asks the orchestrator to send that
same agent a reminder through SendMessage, naming the exact path, so the work
it already did isn't thrown away. Each `spawn` entry carries a `slot`, and the
orchestrator remembers which agent ID served which slot. If the reminder
doesn't work either, a fresh agent gets a fresh brief and one reminder. After
that the job waits for a person with `retry` available. A retry keeps the
outputs that were already written for the stage, such as the other reviews of
the round, so only the missing one runs again. A file that exists but is
malformed never counts again.

Every agent prompt and every brief ends with the same rule: write the output
file with the Write tool before replying, because the factory never reads
replies.

### Rounds

A round is build → gate → review → approve. A failed gate, any CHANGES review,
or an approver's CHANGES starts the next round with a brief that points at
what failed. When `max_rounds` run out, the job waits for a person, with
`decision.md` saying why. `rework` adds a person's note and a fresh set of
rounds. The note goes to the spec writer first, so `spec.md` always describes
what is being built.

### Revising the spec

The spec is the contract, so nobody but the spec writer changes it. When the
builder finds the spec in its way (it needs a protected file the spec doesn't
unlock, it contradicts the code, a criterion can't be met as written), it
writes `STATUS: SPEC-CHANGE` with its case. The job goes back to `spec`. The
same spec writer, when the orchestrator still has it, decides, rewrites
`spec.md` with a `## Revisions` entry, and the same builder carries on in the
same round. Each acceptance records the spec's hash, and the gate refuses a
branch whose `spec.md` changed afterwards, so the builder can't grant itself
permissions. The builder may ask twice per job; a third request goes to a
person.

### Who decides what

The factory is meant to run unattended. Prompts give agents the authority an
engineer on the project would have and reserve people for what agents can't
decide:

- intake asks the requester only when a sensible guess would likely build the
  wrong thing;
- the spec writer decides open questions, records them as assumptions, and
  marks **(product)** only business decisions with no sensible default;
- the builder never works around a constraint; it asks for a spec change;
- the approver answers APPROVE, CHANGES (fixable by an agent, so another
  round) or ESCALATE (prices, legal text, data access, contradictory
  requirements, missing credentials, the profile's `context/approver.md`).
  What counts as sensitive depends on delivery: under handoff a person reviews
  the PR, so sensitive areas and unverifiable checks become `## For the
  reviewer` notes that the handoff prints. Under local-merge they are escalated.

## Routing

Intake writes `RISK` (low, medium, high) and `SURFACES` (from the profile's
`surfaces`, by default `ui` and `security`). `route.py` looks them up:

| | builder | reviewers |
|---|---|---|
| low | sonnet | code (haiku) |
| medium | sonnet | code (sonnet) |
| high | opus | code (sonnet), security (opus) |
| + `ui` | | ux (sonnet), ui (sonnet) |
| + `security` | | security (opus) |

When two entries name the same lens, the stronger model wins. Models are
passed per spawn with the Agent tool's `model` parameter, so one agent file
serves every tier. Intake, spec writer and approver use `models` (opus by
default). All of it is data in `engine/defaults.json`, overridable per profile.

## Context passing

There is no project `.claude/` to load from, and agents run with
`omitClaudeMd: true` so no personal or project CLAUDE.md leaks in. Instead,
`brief.py` writes `jobs/<id>/briefs/NN-<role>[-lens]-rN.md` for every spawn,
and the agent's whole prompt is `Brief: <path>`. The agent file (the system
prompt) says how to work; the brief says on what.

| Role | Gets from the profile |
|---|---|
| intake | request, replies, `intake.md` (project purpose), `project.md`, surface descriptions |
| spec-writer | request, intake's scope, `project.md`, commands, protected paths, `spec-writer.md` |
| builder | spec and intake paths, this round's reason, `project.md`, commands, protected paths, port, `builder.md` |
| reviewer (lens) | the lens checklist, `<lens>.md` notes, port; `project.md` and commands except for security |
| approver | request, every job file, `project.md`, commands, `approver.md` (extra escalations) |

So the security reviewer sees the security hot spots and nobody else does, the
UI reviewer sees where the design tokens live, and so on. If the repo has an
`AGENTS.md`, `CLAUDE.md` or `CONTRIBUTING.md`, briefs point to it explicitly.

Briefs stay in the job folder, so you can always read exactly what an agent
was told.

### Requests are data

A request, title included, is written to `request.md` by a script, inside
markers with a random tag (`<<<REQUEST-1a2b3c4d … REQUEST-1a2b3c4d>>>`) that
the text can't contain, so it can't fake its own end. Every brief that carries
it says what it is: a description of a wanted change from a person, with no
authority over the agent's instructions, and anything else in it is to be
ignored and reported. Replies and rework notes are quoted the same way. Job ids
and branch names are reduced to `[a-z0-9-]`. With `bin/factory run "…"` the
job exists before Claude Code starts, so the orchestrating session only ever
sees the job id.

## The gate

`factory gate <job>` runs between build and review, and again before a local
merge when the base branch has moved:

- runs the **profile's** `checks.sh` with the worktree as working directory.
  The script is outside every worktree, so no branch can change the checks it
  is judged by;
- fails if the profile's `checks.sh` or `config.json` hash differs from the one
  recorded at job creation (a person accepts a deliberate change with
  `factory trust`). In that case it doesn't run the checks at all;
- fails if the branch modifies, renames or deletes an existing file matching
  `protected_paths` (fnmatch, `*` matches `/`; new files are fine). This blocks
  the commonest way to game tests: editing them. The exception is a file the
  accepted spec lists under `## Protected changes` because the requested
  behaviour makes it wrong. The spec writer decides that before any code
  exists, code review checks the change matches, and a profile can switch it
  off with `spec_may_unlock_protected: false`;
- fails if the job's accepted `spec.md` changed since it was accepted;
- fails if the worktree has uncommitted or untracked changes, or is on the
  wrong branch;
- records the commit it judged. Review, approval and delivery all refuse if
  `HEAD` has moved since, so what was reviewed is exactly what is delivered.

Untracked files that `checks.sh` itself leaves behind (build output a project
forgot to ignore) are noted once and ignored by later gates.

The builder can run the same checks with `factory gate --dry-run`, which
records nothing. Plugin agents can't carry hooks, so this is a nudge; the gate
is what decides.

On top of the gate, factory sessions get deny rules (`Edit(//abs/path)`, which
cover every file-editing tool) for the engine, every profile's config, checks,
notes and backlog, and the job files that scripts own. The hash check is the
backstop for anything that edits through the shell.

## Delivery

The last step differs between projects: one requires a pull request and
protects its base branch, another lets you land changes directly. So the
pipeline ends at a pluggable step. After APPROVE, `step` moves the job to
`delivering` and asks the orchestrator to run `factory deliver <job>`, which
calls the method the profile names in `delivery.method`. The method's options
sit next to it:

```json
"delivery": { "method": "handoff", "next_steps": "gh pr create --head {branch} --base {base} --fill" }
"delivery": { "method": "local-merge", "strategy": "squash" }
```

Every method follows one contract (`engine/lib/factory/delivery/base.py`):

- it delivers only what was checked: the commit the gate passed, a commit it
  gates again itself after changing the branch, or what a person explicitly
  approved with `factory approve`. `checked_head()` enforces this, so commits
  made after approval are refused;
- it returns a record (method, commit, summary, optional `next_steps` for the
  person). The engine stores it in `job.json` and moves the job to `delivered`;
- when something outside the factory stands in the way (a busy checkout, a
  conflict), it raises `Blocked`. The job waits as `needs-human`, and
  `factory retry` runs the delivery again;
- it declares its options, which `factory verify` validates, and says what
  APPROVE means in this project. That sentence goes into the approver's brief,
  so the approver knows whether a person will see the branch next or nobody
  will.

### handoff (default)

The job ends as an approved branch at exactly the reviewed commit. The worktree
is removed; nothing is pushed or merged. The session prints what to do next:
`delivery.next_steps` if the profile sets it, otherwise a `git push` line for
the `origin` remote. The method is *reopenable*: when a pull-request review
asks for changes, `factory rework` checks the branch out again (running
`commands.setup`) and sends it through spec, build, gate, review and approval,
which hands it off again. The approver's `## For the reviewer` notes are
printed with the next steps.

### local-merge (opt-in)

For projects that allow landing on the base branch without a pull request:

1. bring the job branch up to date with the base branch inside its worktree
   (merge, or rebase for `strategy: rebase`); stop on conflicts;
2. gate again if that changed the branch;
3. build the final commit from exactly that tree: `git commit-tree` with two
   parents (`merge`), with one parent (`squash`), or the rebased tip (`rebase`);
4. move the base branch: if it's checked out (your main checkout, usually), a
   `merge --ff-only` there, which needs no uncommitted changes to tracked
   files; otherwise an atomic `update-ref` that fails if the branch moved;
5. remove the worktree and the job branch.

Nothing is pushed. If your main checkout is busy, the job waits with `retry`;
it is already approved and gated.

### Adding a method

Write a `Method` subclass in `engine/lib/factory/delivery/`, add it to
`METHODS` in `__init__.py`, and add tests that play it through `deliver()`. A
GitHub pull-request method would look like this:

- options such as `remote` (default `origin`), `draft` (bool), `reviewers`, and
  `auto_merge` with a merge `strategy` for repos that allow GitHub auto-merge;
- `deliver()`: call `checked_head()`, push the branch with `git push`, open
  the PR with `gh pr create` using the spec's title and a body built from
  `decision.md`, and return the PR URL as `next_steps`. Set `reopenable = True`
  so review feedback can go through `rework` and a later push updates the PR;
- `approve_meaning()`: "APPROVE opens a pull request; the project's own review
  and branch protection decide the merge".

A method that pushes or merges must only ever be switched on by a profile,
never by default, and must keep the `checked_head()` rule.

## People and agents

`approve`, `abandon` and `trust` need a person: they refuse unless stdin and
stdout are a terminal, and they ask for confirmation. Agents run commands with
piped stdin, so an agent, or a request trying to steer one, can't deliver an
escalated job, delete work, or re-bless changed checks. `reply`, `rework` and
`retry` only feed the job back into the pipeline, where the gate, reviewers and
approver still decide, so the orchestrator may run them.

## What the factory does not do

- Run jobs in parallel: one job per session, one active job per profile.
- Read GitHub issues, push, open pull requests, or deploy. Pushing and opening
  pull requests are what a future delivery method would add, per profile.
- Track cost. Claude Code's transcripts hold the numbers if you want them.
- Override whole agent prompts per profile. Profiles add notes and lenses. The
  output contracts (`VERDICT:`, `STATUS:`, …) stay the engine's, because
  `step` depends on them.
