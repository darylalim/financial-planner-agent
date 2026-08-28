#!/bin/sh
# Stop hook: one full verification per turn that touched Python.
#
# Runs the exact three commands a developer runs by hand:
#   ruff format --check .  &&  ruff check .  &&  pytest -q
# 284 tests in ~2.8s, fully offline, no API key. That is unusually cheap and is
# what makes a real gate viable here. It catches whole-repo consequences the
# per-file PostToolUse hook structurally cannot -- above all a renamed symbol
# that leaves both files individually lint-clean while breaking the import.
set -u

cat >/dev/null   # drain the payload; the attempt counter replaced every use of it

cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0
root=$(pwd -P)

# Run only if the PostToolUse hook actually touched a .py this turn.
#
# The obvious trigger -- `git status --porcelain -- '*.py'` -- is wrong in both
# directions. It stays non-empty for the whole life of an uncommitted change, so
# a read-only turn mid-refactor gets blocked by failures Claude did not cause;
# and it goes empty the moment Claude commits, so the one turn where red code
# becomes permanent is the one turn the gate skips.
#
# Known gap, accepted: the marker is set by Edit/Write only, so Python written
# through Bash does not arm the gate. Arming it would mean a hook on every Bash
# call.
gd=$(git rev-parse --absolute-git-dir 2>/dev/null) || exit 0
marker="$gd/claude-py-touched"
attempts="$gd/claude-verify-attempts"
[ -e "$marker" ] || exit 0

R="$root/.venv/bin/ruff"
P="$root/.venv/bin/pytest"
[ -x "$R" ] && [ -x "$P" ] || exit 0   # no venv: skip rather than block the stop

if out=$( { "$R" format --check . && "$R" check . && "$P" -q; } 2>&1 ); then
  rm -f "$marker" "$attempts"
  exit 0
fi

# Bounded retries instead of the `stop_hook_active` bypass. That flag is true
# for the whole continuation, so exiting on it meant the gate blocked, demanded
# a fix, and then rubber-stamped whatever came back without re-running anything.
# Counting attempts verifies every stop and still terminates.
#
# The marker is deliberately NOT consumed on the failing path: leaving it armed
# is what lets the retry re-verify, and it also survives this hook being killed
# at its timeout with no verdict.
n=$(cat "$attempts" 2>/dev/null || echo 0)
case $n in ''|*[!0-9]*) n=0 ;; esac
n=$((n + 1))

if [ "$n" -ge 2 ]; then
  rm -f "$marker" "$attempts"
  printf 'Verification still failing after %d attempts; not blocking again. Unresolved:\n%s\n' \
    "$n" "$(printf '%s\n' "$out" | tail -n 40)" >&2
  exit 0
fi

printf '%d' "$n" > "$attempts"
printf 'Verification failed (ruff format --check . && ruff check . && pytest -q). Fix before finishing:\n%s\n' \
  "$(printf '%s\n' "$out" | tail -n 40)" >&2
exit 2
