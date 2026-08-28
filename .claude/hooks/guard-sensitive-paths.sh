#!/bin/sh
# PreToolUse guard for Edit/Write/MultiEdit/NotebookEdit.
#
# Refuses Claude Code writes to the two things this repo cannot recover: live
# API keys (.env) and the household's personal financial data under agent_home/
# plus the SQLite checkpointer. Everything else is untouched -- in particular
# agent_home/skills/**, .env.example and .gitkeep stay editable.
#
# SCOPE, stated honestly: this is a tool-dispatch filter, not a syscall boundary.
# Bash is deliberately unmatched, because the Streamlit app writes these same
# paths from its own process and a Bash matcher would refuse to launch it. So
# `cat > .env`, `cp`, `mv`, `sed -i` and friends are NOT covered. Treat this as
# a guard against accidental clobbering, not as a security boundary. The real
# boundary is FilesystemBackend(virtual_mode=True) in agent.py, which governs a
# different actor: the agent under test, not Claude Code.
set -u

# NotebookEdit names its path `notebook_path`, not `file_path`. Reading only
# file_path makes the guard a silent no-op for every notebook write.
f=$(jq -r '.tool_input.file_path // .tool_input.notebook_path // empty' 2>/dev/null) || exit 0
[ -n "$f" ] || exit 0

# Normalize: absolute, symlink- and ".."-resolved, without requiring the file
# (or even its parent directory) to exist -- agent_home/conversation_history/
# only appears the first time a conversation is summarized.
case "$f" in /*) ;; *) f="$PWD/$f" ;; esac
d=$(dirname "$f")
rest=$(basename "$f")
while [ ! -d "$d" ] && [ "$d" != "/" ] && [ "$d" != "." ]; do
  rest="$(basename "$d")/$rest"
  d=$(dirname "$d")
done
p="$(cd "$d" 2>/dev/null && pwd -P)/$rest"

root=$(cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null && pwd -P) || root=""
rel="$p"
if [ -n "$root" ]; then
  case "$p" in "$root"/*) rel="${p#"$root"/}" ;; esac
fi

deny() {
  printf 'Blocked by .claude/hooks/guard-sensitive-paths.sh: %s\n%s\n' "$rel" "$1" >&2
  exit 2
}

# The walk above can only resolve ".." through directories that already exist.
# A path like agent_home/nope/../workspace/stmt.csv leaves an unresolved ".."
# inside $rest that no pattern below can match -- and the write still lands in
# workspace/, because the tool creates the missing parent first and the kernel
# then resolves ".." normally. Refuse rather than try to normalize further.
case "/$rest/" in
  */../*) deny "Path contains an unresolved '..' component. Pass the fully resolved absolute path instead." ;;
esac

# Tracked and explicitly un-ignored -- must win over the workspace rule below.
case "$rel" in
  agent_home/workspace/.gitkeep) exit 0 ;;
esac

case "$p" in
  */.env|*/.streamlit/secrets.toml)
    deny "This file holds live API keys and is gitignored, so an overwrite is unrecoverable. Ask the user to edit it themselves; you may edit .env.example instead." ;;
  *.sqlite|*.sqlite3|*.db)
    deny "This is the SQLite checkpointer holding real planning conversations. It is binary; a text write corrupts it." ;;
esac

case "$rel" in
  agent_home/AGENTS.md)
    deny "This is the household profile the running app maintains (income, balances, goals). It is gitignored personal data. Edit DEFAULT_PROFILE in src/financial_planner/agent.py to change the template instead." ;;
  agent_home/workspace/*|agent_home/conversation_history/*|agent_home/large_tool_results/*)
    deny "This is personal financial data the app writes at runtime (uploaded statements, generated plans, summarizer offloads). It is gitignored and unrecoverable." ;;
esac

exit 0
