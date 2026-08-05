"""Custom tools exposed to the planning agent.

The agent also gets filesystem (`ls`, `read_file`, `write_file`, `edit_file`,
`glob`, `grep`), planning (`write_todos`) and delegation (`task`) tools, so
nothing here duplicates those. Only the filesystem and delegation tools arrive
by default; the filesystem set is narrowed and `write_todos` added back in
``agent.build_agent`` -- see the middleware list there for why.

These add the three capabilities the harness has no opinion about:
deterministic finance math, market data, and structured document ingestion.
"""

from financial_planner.tools.calculators import CALCULATOR_TOOLS
from financial_planner.tools.documents import DOCUMENT_TOOLS
from financial_planner.tools.market import MARKET_TOOLS
from financial_planner.tools.search import SEARCH_TOOLS

ALL_TOOLS = [*CALCULATOR_TOOLS, *DOCUMENT_TOOLS, *MARKET_TOOLS, *SEARCH_TOOLS]

__all__ = [
    "ALL_TOOLS",
    "CALCULATOR_TOOLS",
    "DOCUMENT_TOOLS",
    "MARKET_TOOLS",
    "SEARCH_TOOLS",
]
