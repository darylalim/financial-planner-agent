#!/bin/sh
# PreToolUse guard for Edit/Write/MultiEdit/NotebookEdit.
#
# Refuses Claude Code writes to the things this repo cannot recover: live API
# keys (.env), the household's personal financial data under the agent's home,
# the SQLite checkpointer, and the hook configuration that enforces all of it.
# Under the agent's home it is an allowlist, as .gitignore is: only skills/**
# and workspace/.gitkeep are editable, so a runtime folder a library adds later
# is refused by default rather than missed. Elsewhere only the named files are
# refused; .env.example stays editable.
#
# SCOPE, stated plainly: this is a tool-dispatch filter, not a syscall boundary.
# Bash is deliberately unmatched, because the Streamlit app writes these same
# paths from its own process and a Bash matcher would refuse to launch it. So
# `cat > .env`, `cp`, `mv` and `sed -i` are NOT covered. Treat this as a guard
# against accidental clobbering. The real boundary is
# FilesystemBackend(virtual_mode=True) in agent.py, which governs a different
# actor: the agent under test, not Claude Code.
set -u
. "$(dirname "$0")/lib.sh"

# Fail CLOSED. Without jq this script cannot read which file is being written,
# and a guard that cannot tell silently permits everything.
hook_require_jq guard-sensitive-paths.sh || exit 2

payload=$(cat)
f=$(hook_file_path "$payload")
[ -n "$f" ] || exit 0

cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || true
root=$(pwd -P)
p=$(hook_abs "$f")
rel=$(hook_rel "$p" "$root")
home=$(hook_agent_home "$root")

deny() {
  printf 'Blocked by .claude/hooks/guard-sensitive-paths.sh: %s\n%s\n' "$rel" "$1" >&2
  exit 2
}

# Tracked and explicitly un-ignored -- must win over the workspace rule below.
[ "$p" = "$home/workspace/.gitkeep" ] && exit 0

case "$p" in
  */.env|*/.streamlit/secrets.toml)
    deny "This file holds live API keys and is gitignored, so an overwrite is unrecoverable. Ask the user to edit it themselves; you may edit .env.example instead." ;;
esac

# CHECKPOINT_DB, mirrored from config.py: the historic name beside the default
# home, and named after the home once it is redirected. Both branches, because
# guarding only the repo-root default protects a path holding no database for
# anyone who has moved their home -- while the real one, holding every balance
# and account detail the household ever typed, goes unguarded.
#
# Still an exact match rather than *.sqlite|*.db: the broad rule refused test
# fixtures and any scratch database anywhere on the filesystem, with a message
# describing a file they were not.
if [ "$home" = "$(hook_abs "$root/agent_home")" ]; then
  db="$root/planner_state.sqlite"
else
  db="$(dirname "$home")/$(basename "$home")-state.sqlite"
fi
[ "$p" = "$db" ] && \
  deny "This is the SQLite checkpointer holding real planning conversations. It is binary; a text write corrupts it."

# An allowlist, not a list of known runtime folders. The list missed a folder
# each time a library added one (conversation_history/, large_tool_results/,
# then blobs/ for offloaded binary content), and .gitignore had already moved to
# an allowlist for the same reason. Only the tracked skills stay editable;
# workspace/.gitkeep was let through above.
#
# Skipped when the home is the project root or one of its ancestors: the
# pattern below would then refuse every edit in the repository. That layout
# already breaks the agent's filesystem boundary, so it is not one to support.
case "$root/" in
  "$home"/*) home_contains_root=1 ;;
  *)         home_contains_root=0 ;;
esac

case "$p" in
  "$home"/AGENTS.md)
    deny "This is the household profile the running app maintains (income, balances, goals). It is gitignored personal data. Edit DEFAULT_PROFILE in src/financial_planner/agent.py to change the template instead." ;;
  "$home"/skills/*)
    ;;
  "$home"/*)
    [ "$home_contains_root" = 1 ] || \
      deny "This is personal financial data the app writes at runtime under its home (uploaded statements, generated plans, summarizer and eviction offloads, and any folder a library adds later). It is gitignored and unrecoverable. Only skills/** is editable here." ;;
esac

# The hooks constrain this session, so this session should not be able to edit
# them out from under itself by accident. Deliberate changes still work through
# Bash, which is unmatched by design.
case "$rel" in
  .claude/settings.json|.claude/settings.local.json|.claude/hooks/*)
    deny "This is the hook configuration constraining this session; an edit here can silently disable the write guard, the formatter and the verification gate. Ask the user to make the change." ;;
esac

exit 0
