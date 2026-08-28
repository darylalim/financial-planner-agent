#!/bin/sh
# PostToolUse: autofix and format Python this repo owns, right after Claude edits it.
#
# pyproject pins line-length 100 and lint select E,F,I,UP,B. Without this the
# session drifts from the repo's own standard and it surfaces later as a diff
# full of formatting noise. ~30ms; the edit has already landed, so nothing is
# blocked or slowed.
set -u

[ -n "${CLAUDE_PROJECT_DIR:-}" ] || exit 0
cd "$CLAUDE_PROJECT_DIR" 2>/dev/null || exit 0   # keeps .ruff_cache writable
root=$(pwd -P)

f=$(jq -r '.tool_input.file_path // empty' 2>/dev/null) || exit 0
[ -n "$f" ] || exit 0
case "$f" in /*) ;; *) f="$root/$f" ;; esac

# Scope to Python INSIDE the checkout. Without the prefix test this rewrites any
# .py Claude writes anywhere -- a throwaway /tmp probe script silently loses the
# imports its next revision needs, with no diagnostic. The suffix test matters
# too: uv.lock parses as almost-Python and floods `ruff check` with megabytes of
# invalid-syntax diagnostics.
case "$f" in
  "$root"/*.py) ;;
  *) exit 0 ;;
esac
[ -f "$f" ] || exit 0                            # deleted-file race

R="$root/.venv/bin/ruff"
[ -x "$R" ] || exit 0                            # no venv yet: skip, never fail

# Order is load-bearing: fix first (import sorting can leave orphan blank lines
# that only the formatter cleans), format second, and grade LAST.
#
# Grading before the format is the trap. E501 is enabled but is not linter-
# fixable and usually IS formatter-fixable, so a pre-format check reports "line
# too long" on a file the very next command makes clean -- then hands Claude a
# diagnostic whose line numbers no longer describe the file on disk. An AST scan
# of this repo found 422 constructs across 27 of 28 tracked files that exceed
# 100 cols when emitted unwrapped, so that false alarm would be the common case,
# not the edge case.
"$R" check --fix -q "$f" >/dev/null 2>&1
"$R" format -q "$f" >/dev/null 2>&1

# Arm the Stop gate: this turn touched Python. Commit-proof and stash-proof,
# unlike deriving it from `git status`. Written before the grading check below
# so a file with surviving lint still arms the gate.
gd=$(git rev-parse --absolute-git-dir 2>/dev/null) || gd=""
[ -n "$gd" ] && : > "$gd/claude-py-touched" 2>/dev/null

# PostToolUse fires after the tool ran, so exit 2 cannot block anything -- it is
# purely how a diagnostic reaches Claude. Fires only on lint that survives
# formatting (F821, unfixable B), with line numbers that match the file.
"$R" check -q "$f" >&2 || exit 2
exit 0
