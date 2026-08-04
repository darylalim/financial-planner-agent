"""Custom tools exposed to the planning agent.

Deep Agents already supplies filesystem (`ls`, `read_file`, `write_file`,
`edit_file`, `glob`, `grep`), planning (`write_todos`) and delegation (`task`)
tools, so nothing here duplicates those. These add the three capabilities the
harness has no opinion about: deterministic finance math, market data, and
structured document ingestion.
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
