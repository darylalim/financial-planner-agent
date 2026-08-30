"""Configuration and filesystem layout.

The agent is given a *dedicated* root directory (``agent_home/``) rather than the
repository root. This is a deliberate security boundary: ``FilesystemBackend``
with ``virtual_mode=True`` confines the agent to its root, so pointing it at the
repo would put ``.env``, source code, and git history inside its reach. A
financial agent that reads user bank statements has no business being one prompt
injection away from the API keys.

Layout::

    agent_home/                 <- FilesystemBackend root (the agent's whole world)
    ├── AGENTS.md               <- always-loaded profile; the agent edits this
    ├── skills/                 <- committed SKILL.md files, loaded on demand
    │   └── <skill-name>/SKILL.md
    └── workspace/              <- gitignored; user documents + generated plans
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --- Real paths on disk -----------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Overridable so the live check can point at a throwaway home. Without this,
# running it against a real installation would overwrite the household's
# AGENTS.md with test data. Must be set before this module is first imported.
AGENT_HOME = Path(os.getenv("FINANCIAL_PLANNER_HOME") or PROJECT_ROOT / "agent_home")
WORKSPACE_DIR = AGENT_HOME / "workspace"
SKILLS_DIR = AGENT_HOME / "skills"
# A *sibling* of AGENT_HOME, not a path under PROJECT_ROOT: pinning it to the
# repo meant FINANCIAL_PLANNER_HOME did not move it, so the live check and the
# test suite -- which exist precisely to run against a throwaway home -- still
# wrote their conversations into the real installation's database. Deriving it
# from AGENT_HOME keeps the default byte-identical (AGENT_HOME defaults to
# PROJECT_ROOT/agent_home, whose parent is PROJECT_ROOT) while making a
# redirected home self-contained.
#
# Deliberately *beside* AGENT_HOME rather than inside it. AGENT_HOME is the
# FilesystemBackend root, so a database holding the full transcript of the
# household's finances would become readable by the agent itself -- the exact
# boundary the module docstring above exists to draw.
CHECKPOINT_DB = AGENT_HOME.parent / "planner_state.sqlite"

# --- Virtual paths, as the agent sees them ----------------------------------
# These are relative to AGENT_HOME because virtual_mode=True re-roots the
# backend. "/workspace/" for the agent is "agent_home/workspace/" on disk.

VIRTUAL_WORKSPACE = "/workspace/"
VIRTUAL_SKILLS = "/skills/"
VIRTUAL_MEMORY = "/AGENTS.md"

# --- Model ------------------------------------------------------------------
# Provider-prefixed string resolved by langchain's init_chat_model, so swapping
# providers is a one-string change. Claude Opus 5 is the default: this agent
# does long-horizon multi-step planning, which is exactly where it earns out.
DEFAULT_MODEL = os.getenv("FINANCIAL_PLANNER_MODEL", "anthropic:claude-opus-5")

# --- Secrets ----------------------------------------------------------------

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")


def ensure_directories() -> None:
    """Create the agent's directory tree if it is missing.

    Safe to call repeatedly; called at agent construction and at app startup.
    """
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)


def missing_required_keys() -> list[str]:
    """Return the names of required env vars that are unset.

    Tavily is deliberately excluded -- web search degrades to unavailable rather
    than blocking startup, so the planner still works offline against local
    documents and market data.
    """
    missing = []
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    return missing
