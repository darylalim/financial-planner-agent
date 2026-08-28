#!/bin/sh
# PreToolUse guard for Edit/Write/MultiEdit/NotebookEdit.
#
# Refuses Claude Code writes to the things this repo cannot recover: live API
# keys (.env), the household's personal financial data under the agent's home,
# the SQLite checkpointer, and the hook configuration that enforces all of it.
# Everything else is untouched -- skills/**, .env.example and .gitkeep stay
# editable.
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

# config.py:40 -- CHECKPOINT_DB is a single fixed path, so match it exactly. A
# broad *.sqlite|*.db rule refused test fixtures and any scratch database
# anywhere on the filesystem, with a message describing a file they were not.
[ "$p" = "$root/planner_state.sqlite" ] && \
  deny "This is the SQLite checkpointer holding real planning conversations. It is binary; a text write corrupts it."

case "$p" in
  "$home"/AGENTS.md)
    deny "This is the household profile the running app maintains (income, balances, goals). It is gitignored personal data. Edit DEFAULT_PROFILE in src/financial_planner/agent.py to change the template instead." ;;
  "$home"/workspace/*|"$home"/conversation_history/*|"$home"/large_tool_results/*)
    deny "This is personal financial data the app writes at runtime (uploaded statements, generated plans, summarizer offloads). It is gitignored and unrecoverable." ;;
esac

# The hooks constrain this session, so this session should not be able to edit
# them out from under itself by accident. Deliberate changes still work through
# Bash, which is unmatched by design.
case "$rel" in
  .claude/settings.json|.claude/settings.local.json|.claude/hooks/*)
    deny "This is the hook configuration constraining this session; an edit here can silently disable the write guard, the formatter and the verification gate. Ask the user to make the change." ;;
esac

exit 0
