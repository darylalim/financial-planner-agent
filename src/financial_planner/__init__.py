"""A personal financial planning agent built on Deep Agents."""

from importlib.metadata import version

from financial_planner.agent import build_agent, build_checkpointer

# Read from the installed distribution, not written here: `uv version` bumps
# pyproject.toml and uv.lock but never this file, so a literal copy sat at 0.1.1
# through seven releases. The distribution is named for the project, not the import.
__version__ = version("financial-planner-agent")

__all__ = ["build_agent", "build_checkpointer", "__version__"]
