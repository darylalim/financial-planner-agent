"""Web search via Tavily, for facts that change faster than model weights.

Tax brackets, contribution limits, and prevailing rates all move annually and
are exactly the kind of detail a model will confidently state from a stale
prior. The tool description below is written to trigger on those cases.

Results are trimmed before returning: Tavily's raw payload carries long page
excerpts that would crowd out the rest of the session for little gain.
"""

from __future__ import annotations

from typing import Any

from langchain.tools import tool

# Imported as a module, not `from ... import TAVILY_API_KEY`: a by-value import
# freezes the key at import time, so a later credential change -- or a test
# monkeypatching it -- would never be seen. config.missing_required_keys()
# reads its module global for the same reason.
from financial_planner import config
from financial_planner.envelope import err, ok

MAX_RESULTS = 5
SNIPPET_CHARS = 700

# Financial-reference domains produce far better grounding than open web search
# for rates, limits and rules. Callers can opt out for general queries.
AUTHORITATIVE_DOMAINS = [
    "irs.gov",
    "ssa.gov",
    "federalreserve.gov",
    "consumerfinance.gov",
    "treasury.gov",
    "investor.gov",
    "bls.gov",
]


@tool
def search_web(query: str, authoritative_only: bool = False) -> str:
    """Search the web for current financial facts, rates, rules and limits.

    Call this rather than answering from memory whenever the answer depends on a
    figure that changes: IRS contribution limits, tax brackets, standard
    deduction, Social Security parameters, current mortgage or savings rates,
    or anything the user flags as this-year. Stating a stale limit as fact is a
    real harm in this domain -- an outdated 401(k) cap can cause an over-
    contribution the user has to unwind.

    Args:
        query: Search query. Include the year when it matters, e.g.
            "2026 401k contribution limit".
        authoritative_only: Restrict results to primary government sources
            (irs.gov, ssa.gov, federalreserve.gov and similar). Set True for
            tax, benefit and contribution-limit lookups; leave False for
            general research and product comparisons.

    Returns:
        JSON with an answer summary and up to 5 results, each with title, url
        and a trimmed content snippet. Always cite the URL when you use a result.
    """
    if not config.TAVILY_API_KEY:
        return err(
            "Web search is unavailable: TAVILY_API_KEY is not set. Tell the user "
            "that time-sensitive figures cannot be verified right now, and do "
            "not state current-year tax or contribution limits from memory."
        )

    try:
        from tavily import TavilyClient

        client = TavilyClient(api_key=config.TAVILY_API_KEY)
        kwargs: dict[str, Any] = {
            "query": query,
            "max_results": MAX_RESULTS,
            "include_answer": True,
            "search_depth": "advanced",
        }
        if authoritative_only:
            kwargs["include_domains"] = AUTHORITATIVE_DOMAINS

        raw = client.search(**kwargs)

        return ok(
            {
                "query": query,
                "answer": raw.get("answer"),
                "results": [
                    {
                        "title": r.get("title"),
                        "url": r.get("url"),
                        "snippet": (r.get("content") or "")[:SNIPPET_CHARS],
                    }
                    for r in (raw.get("results") or [])[:MAX_RESULTS]
                ],
            }
        )
    except Exception as exc:  # noqa: BLE001
        return err(exc)


SEARCH_TOOLS = [search_web]
