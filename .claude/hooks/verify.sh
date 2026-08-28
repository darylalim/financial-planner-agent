#!/bin/sh
# Stop hook: one full verification per turn that touched Python.
#
# Runs the exact three commands a developer runs by hand, in order:
#   ruff format --check .  &&  ruff check .  &&  pytest -q
# 284 tests in ~2.8s, fully offline, no API key. That is unusually cheap and is
# what makes a real gate viable here. It catches whole-repo consequences the
# per-file PostToolUse hook structurally cannot -- above all a deleted or
# renamed symbol that leaves both files individually lint-clean while breaking
# the import between them.
set -u

payload=$(cat)

cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0
root=$(pwd -P)

# Run only if the PostToolUse hook actually touched a .py this turn, and clear
# the marker so each turn is judged on its own writes.
#
# The obvious alternative -- `git status --porcelain -- '*.py'` -- is wrong in
# both directions: it stays non-empty for the whole life of an uncommitted
# change (so a read-only turn mid-refactor gets blocked by failures Claude did
# not cause), and it goes empty the moment Claude commits (so the one turn where
# red code becomes permanent is the one turn the gate skips).
#
# Known gap, accepted: the marker is set by Edit/Write only, so Python written
# through Bash (a heredoc, `cp`, code generation) does not arm the gate. That is
# the rare path; arming it would mean a hook on every Bash call.
gd=$(git rev-parse --absolute-git-dir 2>/dev/null) || gd=""
[ -n "$gd" ] || exit 0
[ -e "$gd/claude-py-touched" ] || exit 0
rm -f "$gd/claude-py-touched"

# Never re-block a turn that is already continuing because of this hook. This
# sits AFTER the sentinel is consumed on purpose: exiting earlier would leave
# the marker behind, and a later turn that touched no Python at all would then
# pay for the gate it never earned.
case $(printf '%s' "$payload" | jq -r '.stop_hook_active // false' 2>/dev/null) in
  true) exit 0 ;;
esac

R="$root/.venv/bin/ruff"
P="$root/.venv/bin/pytest"
[ -x "$R" ] && [ -x "$P" ] || exit 0   # no venv: skip rather than block the stop

if out=$( { "$R" format --check . && "$R" check . && "$P" -q; } 2>&1 ); then
  exit 0
fi

printf 'Verification failed (ruff format --check . && ruff check . && pytest -q). Fix before finishing:\n%s\n' \
  "$(printf '%s\n' "$out" | tail -n 40)" >&2
exit 2
