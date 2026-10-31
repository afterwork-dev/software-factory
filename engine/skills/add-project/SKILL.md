---
name: add-project
description: Tailor a new software-factory project profile to its repository (checks.sh, config, project notes) and verify it. Only when the user types /factory:add-project.
argument-hint: "<profile name>"
disable-model-invocation: true
---

# Tailor the profile `$0`

`bin/factory add-project` has already created `projects/$0/` from the template
and filled in the repo path, remote, base branch and port. Your part needs
judgment: read the target repository and make the profile describe it. Your
working directory is the factory folder.

Read `projects/$0/config.json` first. `repo.path` is the target repository,
called `<repo>` below, and `base_branch` is `<base>`.

## Ground rules

- The target repository stays exactly as it is. Read it; don't create, edit or
  commit anything in it. The one exception is AGENTS.md in step 6, and only if
  the user says yes.
- Edit only files in `projects/$0/`. Never touch `engine/` or other profiles.
- Don't run commands that change `<repo>`'s working tree (installs, formatters,
  `--fix`). To try a command, use a throwaway worktree and remove it afterwards:
  `git -C <repo> worktree add --detach projects/$0/worktrees/_try <base>`, then
  `git -C <repo> worktree remove --force projects/$0/worktrees/_try`.

## Steps

1. **Survey the repository.** README, AGENTS.md / CLAUDE.md / CONTRIBUTING.md,
   package manifests (package.json, pyproject.toml, Cargo.toml, go.mod,
   Gemfile, …), Makefile or justfile, CI config (.github/workflows,
   .gitlab-ci.yml, …), test folders, lint / format / typecheck config, dev
   server scripts, design tokens and theme files, and the code that handles
   sign-in, permissions, payments and stored data.

2. **config.json** (`projects/example/config.json` documents every key):
   - `commands.setup`: what a fresh checkout needs before anything runs, such
     as `npm ci` or `uv sync`. Non-interactive. Empty if nothing is needed.
   - `commands.test`, `lint`, `typecheck`, `build`, `dev`: the real commands.
     In `dev`, write `{port}` where the port goes.
   - `protected_paths`: patterns for the existing tests, such as `tests/*` or
     `src/*.test.ts` (`*` also matches `/`; a trailing `/` covers a folder).
     Add snapshot and fixture folders if the tests depend on them.
   - `delivery`: what happens to an approved job. Leave the default,
     `{"method": "handoff"}`: the job ends as an approved branch that the user
     pushes and takes through a pull request. If the project has a PR routine
     worth spelling out, put it in `delivery.next_steps` ({branch}, {base},
     {repo} and {job} are filled in). Only if the user says the project allows
     landing on the base branch without a pull request, use
     `{"method": "local-merge", "strategy": "merge" | "squash" | "rebase"}`,
     matching the history (`git -C <repo> log --oneline --merges -20`).
   - `browser`: the name of a page-viewing CLI such as `agent-browser` if one is
     installed (`command -v agent-browser`) and the project has pages.
     Otherwise leave it empty.
   - Leave `routing`, `surfaces`, `models` and `max_rounds` alone unless the
     user asks.

3. **checks.sh**: replace the template body with the project's real checks,
   in the order lint, typecheck, test, build (whichever exist), matching CI
   where you can. It runs with a job's worktree as its working directory, after
   `commands.setup`. Keep `set -euo pipefail`. It must be non-interactive, use
   no watch modes, rewrite no tracked files (no `--fix`, `--write`, snapshot
   updates), and exit non-zero on any failure.

4. **context/ notes.** Short, factual, with paths you actually found. Don't
   invent. The HTML comments in each file explain what belongs there; agents
   never see the comments.
   - `project.md` (every agent): what the project is, the stack, the main
     folders, conventions the code doesn't make obvious. Under about 40 lines.
   - `intake.md`: what the project is for, and what doesn't belong in it.
   - `security.md`: the security hot spots.
   - `ui.md`: where design tokens, the theme and shared components live.
   - `ux.md`: the kinds of users and how they move through the app.
   - `approver.md`: what a person must always see before it merges.
   - `builder.md`, `code.md`, `spec-writer.md`: only if there is something
     specific to say.

5. **Verify**: run `factory verify --project $0` and fix the profile until it
   passes. If checks.sh fails on the base branch because the project itself is
   broken, stop and tell the user what's broken. Never weaken the checks to get
   a pass.

6. **AGENTS.md (optional)**: if `<repo>` has no AGENTS.md, or it doesn't say
   how to set up, run the dev server and run the checks, offer to write or
   extend one with that contributor information, useful to people as much as
   tools. It must not mention the factory, agents or AI. Write it only if the
   user says yes, and don't commit it: tell them to review and commit it.

7. **Report**: what you set, the result of `factory verify`, and how to run a
   job: `bin/factory run --project $0 "<feature>"`, or add items to
   `projects/$0/backlog.md` and run `bin/factory run --project $0 --next`.
