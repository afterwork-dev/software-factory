#!/usr/bin/env bash
# The project's checks. The factory's gate runs this script with a job's
# worktree as the working directory (after commands.setup has run there), and
# exit 0 means the branch is fit for review.
#
# It lives in the profile, outside every worktree, so a job branch can never
# change the checks it is judged by. Keep it:
#   - non-interactive: no prompts, no watch modes
#   - read-only for tracked files: no --fix, --write or snapshot updates
#   - complete: lint, typecheck, test and build, whichever the project has
#
# The gate sets FACTORY_PROJECT, FACTORY_JOB, FACTORY_WORKTREE and
# FACTORY_BASE_BRANCH. FACTORY_JOB is empty when `factory verify` runs this
# on a fresh checkout of the base branch.
#
# Examples:
#   npm run lint
#   npm run typecheck
#   npm test -- --run
#   npm run build
#
#   uv run ruff check .
#   uv run pytest -q
set -euo pipefail

echo "checks.sh is still the template. Add this project's checks." >&2
exit 1
