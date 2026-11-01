"""Custom tools exposed to the planning agent.

The agent also gets filesystem (`ls`, `read_file`, `write_file`, `edit_file`,
`glob`, `grep`), planning (`write_todos`) and delegation (`task`) tools, so
nothing here duplicates those. Only the filesystem and delegation tools arrive
by default; the filesystem set is narrowed and `write_todos` added back in
``agent.build_agent`` -- see the middleware list there for why.

These add the three capabilities the harness has no opinion about:
deterministic finance math, market data, and structured document ingestion.
PDF text is the exception: ``READER_TOOLS`` goes to the document-reader
subagent alone.
"""

from financial_planner.tools.calculators import CALCULATOR_TOOLS
from financial_planner.tools.documents import DOCUMENT_TOOLS, READER_TOOLS
from financial_planner.tools.market import MARKET_TOOLS
from financial_planner.tools.search import SEARCH_TOOLS

# The main agent's tools, which the general-purpose subagent inherits. Not the
# reader's: READER_TOOLS is bound only to the document-reader (see agent.py).
ALL_TOOLS = [*CALCULATOR_TOOLS, *DOCUMENT_TOOLS, *MARKET_TOOLS, *SEARCH_TOOLS]

__all__ = [
    "ALL_TOOLS",
    "CALCULATOR_TOOLS",
    "DOCUMENT_TOOLS",
    "MARKET_TOOLS",
    "READER_TOOLS",
    "SEARCH_TOOLS",
]
