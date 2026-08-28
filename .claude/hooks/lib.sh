# Shared helpers for the hooks in this directory. Sourced, never executed.
#
# This file exists because the payload parsing and path normalization were
# duplicated across two scripts and had already drifted apart: one resolved
# symlinks on the file's directory and the other did not, and one read
# NotebookEdit's `notebook_path` while the other did not. Both divergences were
# live bugs, and both were found by review rather than by use.

# Every hook here parses its stdin payload with jq, which ships with neither
# macOS nor `uv sync`. Without this check the scripts fail open in silence --
# the guard exits 0 and the write it exists to refuse goes through.
hook_require_jq() {
  command -v jq >/dev/null 2>&1 && return 0
  printf '%s: jq is not installed, so this hook cannot read its input.\n' "$1" >&2
  printf 'Install it (brew install jq), or remove the hooks from .claude/settings.json.\n' >&2
  return 1
}

# The path the tool is acting on, or empty. NotebookEdit calls it
# `notebook_path`; every other write tool calls it `file_path`.
hook_file_path() {
  printf '%s' "$1" | jq -r '.tool_input.file_path // .tool_input.notebook_path // empty' 2>/dev/null
}

# Absolute, symlink-resolved, "."/".."-collapsed -- without requiring the path
# to exist, since agent_home/conversation_history/ only appears the first time
# a conversation is summarized.
#
# Symlinks are resolved on the deepest existing ancestor via `pwd -P`. The
# remainder cannot contain one, because a symlink pointing at a directory would
# have satisfied the -d test and stopped the walk -- so collapsing ".."
# textually across the join is sound. That collapse is what separates
# `newdir/../foo.py`, which is harmless, from a genuine guard bypass, without
# having to refuse both.
hook_abs() {
  _p=$1
  case "$_p" in /*) ;; *) _p="$PWD/$_p" ;; esac
  _d=$(dirname "$_p")
  _rest=$(basename "$_p")
  while [ ! -d "$_d" ] && [ "$_d" != "/" ] && [ "$_d" != "." ]; do
    _rest="$(basename "$_d")/$_rest"
    _d=$(dirname "$_d")
  done
  _d=$(cd "$_d" 2>/dev/null && pwd -P) || _d=/

  _out=""
  _oldifs=$IFS
  IFS=/
  for _c in $_d/$_rest; do
    case "$_c" in
      ''|.) ;;
      ..)   _out=${_out%/*} ;;
      *)    _out="$_out/$_c" ;;
    esac
  done
  IFS=$_oldifs
  printf '%s' "${_out:-/}"
}

# The project-relative form of an absolute path, or the path unchanged when it
# lies outside the project.
hook_rel() {
  case "$1" in
    "$2"/*) printf '%s' "${1#"$2"/}" ;;
    *)      printf '%s' "$1" ;;
  esac
}

# The agent's home directory, mirroring config.py:35. AGENT_HOME is overridable
# and config.py calls load_dotenv(), so the override can arrive from .env as
# well as the environment -- real environment wins, which is python-dotenv's
# default. A guard that hardcodes agent_home/ protects an empty directory for
# anyone who has moved it, which is the documented reason the override exists.
hook_agent_home() {
  _root=$1
  _home=${FINANCIAL_PLANNER_HOME:-}
  if [ -z "$_home" ] && [ -r "$_root/.env" ]; then
    _home=$(sed -n 's/^[[:space:]]*FINANCIAL_PLANNER_HOME[[:space:]]*=[[:space:]]*//p' "$_root/.env" \
            | tail -1 | sed "s/^[\"']//; s/[\"']\$//")
  fi
  [ -n "$_home" ] || _home="$_root/agent_home"
  hook_abs "$_home"
}
