#!/bin/sh
# PostToolUse: autofix and format Python this repo owns, right after an edit.
#
# pyproject pins line-length 100 and lint select E,F,I,UP,B. Without this the
# session drifts from the repo's own standard and it surfaces later as a diff
# full of formatting noise. ~50ms; the edit has already landed, so nothing is
# blocked or slowed.
set -u
. "$(dirname "$0")/lib.sh"

command -v jq >/dev/null 2>&1 || exit 0   # the guard reports this loudly already

payload=$(cat)
[ -n "${CLAUDE_PROJECT_DIR:-}" ] || exit 0
cd "$CLAUDE_PROJECT_DIR" 2>/dev/null || exit 0   # keeps .ruff_cache writable
root=$(pwd -P)

f=$(hook_file_path "$payload")
[ -n "$f" ] || exit 0
# Normalize the file the same way the root is normalized. Resolving only one
# side made the hook a silent no-op for any checkout reached through a symlink,
# which on macOS includes anything under /tmp or /var.
p=$(hook_abs "$f")
rel=$(hook_rel "$p" "$root")

# Scope to Python inside the checkout. Three separate tests, because the shell's
# `*` crosses `/`: `"$root"/*.py` alone also matches .venv/lib/.../dep.py, and
# ruff will happily rewrite an installed package in place, deleting its imports.
case "$p"   in "$root"/*) ;; *) exit 0 ;; esac
case "$rel" in *.py)      ;; *) exit 0 ;; esac
case "$rel" in .*|*/.*)        exit 0 ;; esac   # .venv, .git, .ruff_cache, ...
[ -f "$p" ] || exit 0                           # deleted-file race

R="$root/.venv/bin/ruff"
[ -x "$R" ] || exit 0                           # no venv yet: skip, never fail

# Order is load-bearing: fix first (import sorting can leave orphan blank lines
# that only the formatter cleans), format second, and grade LAST.
#
# Grading before the format is the trap. E501 is enabled but is not linter-
# fixable and usually IS formatter-fixable, so a pre-format check reports "line
# too long" on a file the very next command makes clean -- then hands Claude a
# diagnostic whose line numbers no longer describe the file on disk. An AST scan
# of this repo found 422 constructs across 27 of 28 tracked files that exceed
# 100 cols when emitted unwrapped, so that false alarm would be the common case.
#
# --force-exclude makes ruff honor the project's own exclude config even for a
# path passed explicitly, which is the authoritative half of the .venv fix.
"$R" check --force-exclude --fix -q "$p" >/dev/null 2>&1
"$R" format --force-exclude -q "$p" >/dev/null 2>&1

# Arm the Stop gate: this turn touched Python. Written before the grading check
# so a file with surviving lint still arms it.
gd=$(git rev-parse --absolute-git-dir 2>/dev/null) || gd=""
[ -n "$gd" ] && : > "$gd/claude-py-touched" 2>/dev/null

# PostToolUse fires after the tool ran, so exit 2 cannot block anything -- it is
# purely how a diagnostic reaches Claude. Fires only on lint that survives
# formatting, with line numbers that match the file.
"$R" check --force-exclude -q "$p" >&2 || exit 2
exit 0
