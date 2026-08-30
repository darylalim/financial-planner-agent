# financial-planner-agent

A personal financial planning agent built on [Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview), with a Streamlit chat UI.

It reads your own bank and brokerage exports, projects savings and retirement
scenarios, compares debt payoff strategies, and remembers your situation between
sessions.

> **Not financial advice.** This is an educational planning tool. It is not a
> licensed financial advisor and does not provide personalized investment, tax,
> or legal advice. For consequential decisions, talk to a fee-only fiduciary.

## Quickstart

```bash
uv sync
cp .env.example .env      # add your ANTHROPIC_API_KEY
uv run streamlit run app.py
```

Then attach a CSV/XLSX/PDF statement in the chat box, or try
`examples/sample-transactions.csv` to see it work with no data of your own.

## How it's put together

| Layer | What it does |
|---|---|
| `src/financial_planner/finance.py` | Pure financial math. No LangChain imports, fully unit-tested. |
| `src/financial_planner/tools/` | Calculators, document ingestion, market data, web search. |
| `src/financial_planner/agent.py` | Assembles the deep agent: model, tools, prompt, backend. |
| `src/financial_planner/streaming.py` | Translates LangGraph stream events into UI events. |
| `src/financial_planner/rendering.py` | Markdown prep for Streamlit (see "Dollar signs are LaTeX"). |
| `src/financial_planner/envelope.py` | The JSON result envelope every tool returns, and its secret redaction. |
| `src/financial_planner/uploads.py` | Untrusted upload names → safe, non-colliding workspace paths. |
| `agent_home/` | The agent's entire filesystem view (see below). |
| `app.py` | Streamlit chat UI. |

### The arithmetic rule

**The model never does the math.** Every figure comes from a *tool*, never from
the model's own arithmetic. A language model will produce a *plausible*
compound-interest number that is wrong by tens of thousands of dollars, and the
person reading it has no way to tell.

Most of those figures come out of `finance.py`, which `tools/calculators.py`
wraps. Two tools compute their own and never import it: `get_historical_return`
derives a CAGR and a volatility from a price series, and `summarize_spending`
aggregates in pandas. They are still tools, which is the part that matters — the
invariant is about *where* a number is computed, not which module computes it.

The system prompt enforces the rule, and `finance.py` carries 41 tests. Some pin
externally verifiable values — a standard mortgage payment, textbook compound
growth — and the rest are invariants (avalanche never costs more than snowball),
self-consistency round-trips, and error paths.

One subtlety worth knowing if you extend it: annual-to-monthly rate conversion
is done **two different ways on purpose**.

- *Investment returns* use the geometric form `(1+r)^(1/12) − 1`, because "7%
  expected return" means 7% effective.
- *Debt* uses `APR/12`, because that is the convention US lenders quote and it
  reproduces the payment on the user's own statement.

Using the geometric form on a $300k/6% mortgage gives ≈$1,770/mo against the
bank's actual $1,798.65.

The rule extends to ratios. A savings rate or a category's share of income is
returned by `summarize_spending`, not divided by the model — a rule the model
can only follow if a tool actually offers the number, which is why those keys
exist.

### Reading the stream

`streaming.py` looks small and has three traps in it, all found by running the
thing rather than by testing it:

- **Filter by `isinstance`, not by `.type`.** LangGraph's `messages` mode emits
  *every* message type, so tool-result JSON arrives on the same stream as the
  answer. But a streamed chunk reports `type == "AIMessageChunk"`, not `"ai"` —
  so the obvious string comparison drops every real token. `AIMessageChunk`
  subclasses `AIMessage`; `ToolMessage` does not.
- **One turn is several messages.** The agent emits a preamble before each tool
  call and then the answer. Concatenating them naively renders
  `"...inspecting the file.Sign convention confirmed..."`. Chunks of one message
  share an `id`, so a change of `id` is the paragraph boundary.
- **Read that `id` only off chunks that carry text.** Anthropic sends a
  content-free chunk bearing the *new* message's id before the first text delta.
  Recording the id from every chunk consumes the boundary before there is
  anything to separate, and the break silently never fires.

The last one passed its unit tests and still failed live, because the synthetic
streams jumped straight from text to text — a shape the network never sends. The
tests now replay the interleaving a real provider produces.

A fourth trap took a code review rather than a run to surface. **Two populations
of tools report failure two different ways.** Our own return the `{"error": ...}`
envelope; the Deep Agents built-ins never touch it and set `status="error"` on
the `ToolMessage` instead. Testing only the envelope reported a failed
`write_file` to the UI as a success, so the app printed nothing — which is
precisely the "agent claimed it saved a file it never wrote" defect the live
check below was written to catch. `_tool_message_failed` checks both signals.

### Dollar signs are LaTeX

`st.markdown` renders `$...$` as inline math. "You'd have $2.26M at 7% and
$1.56M at 5%" therefore renders the middle as italic gibberish, and this agent
puts two dollar amounts in almost every sentence it writes.

`rendering.escape_dollars` escapes `$` before anything reaches `st.markdown`,
skipping code spans and fenced blocks — markdown ignores backslash escapes
inside those, so escaping there would show a literal `\$`.

Nothing but a browser could *find* this. The markdown source is correct, so the
unit tests passed and the live check passed; `AppTest` also inspects the source
rather than the render. It took opening the app and reading the screen.

Once found it is ordinary to test, and `tests/test_rendering.py` pins the
escaping directly — including three cases the first version got wrong. A
four-space-indented block and a `~~~` fence were not recognised as code at all,
so their contents were escaped and the reader saw a literal `\$`. And a fence
that is open but not yet closed — the normal state of an answer mid-stream — was
not code either, so dollars inside a code block were escaped on every token and
silently un-escaped when the closing fence arrived, rewriting the block on
screen. The scan is line-based now, and an open fence runs to the end of the
text.

### Storage and the security boundary

The agent's filesystem root is `agent_home/`, **not** the repository root:

```
agent_home/
├── AGENTS.md        gitignored; always-loaded profile, agent-maintained
├── skills/          committed SKILL.md workflows, loaded on demand
└── workspace/       gitignored; your documents and generated plans
```

Once a conversation runs long enough to summarize, the Deep Agents
summarization middleware adds `conversation_history/` and `large_tool_results/`
beside them. Both hold the same statement data as `workspace/`, and `.gitignore`
already covers them for the same reason.

`FilesystemBackend(virtual_mode=True)` confines the agent to that root, so a
prompt injection hidden in an uploaded PDF cannot reach `.env`, the source, or
git history. The custom document tools enforce the same boundary independently,
since they receive paths straight from model output.

Two kinds of persistence:

- **Files** on real disk — the profile and generated plans survive restarts.
- **Conversation and todo state** in `planner_state.sqlite`, keyed by thread.

### Skills

Three workflows in `agent_home/skills/`, loaded only when relevant:

- `retirement-readiness` — project, then test the drawdown, then quantify the gap
- `budget-from-statements` — verify sign conventions, then aggregate
- `debt-strategy` — avalanche vs snowball, then debt-vs-investing

They encode *procedure*, not finance knowledge the model already has. The
retirement skill exists mainly because the two-step structure (accumulation
*then* drawdown) is easy to skip, and skipping it is the most common way that
analysis goes wrong.

## Tools

| Tool | Purpose |
|---|---|
| `project_savings` | Portfolio projection, nominal and inflation-adjusted |
| `required_savings_rate` | Solve for the monthly contribution to hit a goal |
| `loan_payment` | Mortgage/auto/student payment and lifetime interest |
| `plan_debt_payoff` | Multi-debt simulation, avalanche or snowball |
| `test_withdrawal_plan` | Retirement drawdown sustainability |
| `inspect_document` | Schema and preview of a CSV/XLSX/PDF |
| `summarize_spending` | Aggregate transactions by category and month, with savings rate and monthly averages |
| `read_pdf_text` | Extract a page range from a PDF |
| `get_quote` | Current prices; names the symbols that failed, and errors if none resolve |
| `get_fund_profile` | Expense ratio and category |
| `get_historical_return` | Realized annualized return and volatility, both scaled by the series' own bar rate |
| `search_web` | Current rates, limits and rules (Tavily) |

Plus the Deep Agents built-ins: `write_todos`, `ls`, `read_file`, `write_file`,
`edit_file`, `glob`, `grep`, `task`.

That list is not what `create_deep_agent` binds on its own: it also binds
`delete` and `execute`, and does not bind `write_todos` at all, so `build_agent`
passes a `middleware=` list that narrows the filesystem tools and adds the
planning tool back. `delete` is withheld because the agent only ever appends to
`/workspace/` and edits `/AGENTS.md` — handing recursive deletion to something
that also ingests prompt-injectable PDFs buys nothing, and the UI has no
approval gate. `execute` could only return an error anyway, since
`FilesystemBackend` is not a sandbox backend. `test_agent.py` pins the exact
bound set, because the middleware override is matched by class name and would
fail open on a rename.

Document tools **summarize rather than dump** — a year of transactions is 5,000+
rows, and returning them raw would crowd out the rest of the session.

### The result envelope

Every tool returns JSON through `envelope.py` and returns errors rather than
raising, so a bad argument lets the model read the problem and retry instead of
ending the turn. Two things about it are behaviour, not formatting:

- `streaming._is_error_result` decides whether the UI marks a call failed by
  matching the *serialized* text against `{"error"`. Compact separators and
  key order are load-bearing. It is only half the test — see the fourth trap
  under "Reading the stream" for the tools that never send this envelope.
- Everything it returns lands in the model's context and the saved transcript,
  so `redact()` strips any configured API key first — on the *success* path as
  well as the failure one, since search snippets and other relayed upstream text
  ride back through `ok()` too. Secrets are recognised by the shape of their
  name on `config` (anything ending `_API_KEY`, `_KEY`, `_TOKEN`, `_SECRET`), so
  a third credential is covered the day it is added rather than the day someone
  remembers to extend a list. Upstream exception text is
  relayed verbatim — that is what makes it useful to the model — and HTTP
  clients routinely quote the failing request back. `app.py` redacts the same
  way when it surfaces an agent-level failure, which is where a model-client
  error would appear.

This lives in one module because four copies of it had already drifted: one
omitted the exception type, and only one redacted anything.

### Sign conventions

Exports disagree about what a sign means, and reading one wrong inverts the
entire budget. `summarize_spending` handles all three layouts:

| Layout | Who does it | How to call it |
|---|---|---|
| Negative is money out | Most checking exports, Chase cards | Default |
| *Positive* is money out | Amex and several card issuers | `sign_convention="positive_outflow"` |
| Separate debit/credit columns | Capital One, most EU banks | `inflow_column="Credit"` |

**The numbers cannot settle this, and the tool does not pretend otherwise.**
Both layouts produce mixed signs — a checking export is a few large deposits
against many payments, a card export is many charges against a few payments —
so "there is a negative in here" proves nothing. The information lives in the
file's provenance, which the agent can see in the preview rows and the tool
cannot.

So `auto` assumes the ordinary reading and *says* it assumed:
`sign_convention_inferred: true`, with a note to check it against the preview.
It refuses outright only for the two shapes that look like a card statement —
every value positive, or positives outnumbering negatives 3:1 or more — because
read as signed those report every charge as income, a 100% savings rate and an
empty spending breakdown. The 3:1 threshold is deliberately loose so a household
with irregular income still passes.

One consequence worth knowing: under `positive_outflow` the inflow side is card
*payments*, not earnings, so `savings_rate` and `by_category_share_of_income`
are withheld rather than computed against them, and an `income_basis` note
explains why. A savings rate against your own credit-card payments is arithmetic
on unrelated quantities.

## Development

```bash
uv run pytest          # 339 tests, no API key or network required
uv run ruff check .
uv run ruff format .
```

The test suite runs entirely offline: the financial math is pure, the streaming
translator is driven by synthetic event streams captured from a live graph, and
the Streamlit app is exercised via `AppTest`.

There is no CI. The gate is `.claude/hooks/`, which runs ruff on write and
format-lint-test before a session ends, plus a guard that refuses edits to
`.env`, `agent_home/` and the checkpoint database. That only helps inside a
Claude Code session — run the three commands above yourself otherwise.

### The live check

Offline tests cannot see the failures that only a real provider produces. Three
got through a green suite: raw tool JSON rendered as the answer, assistant
messages running together, and the agent reporting it had saved a file it never
wrote. All three were defects in the *rendered answer* — the only artefact the
user reads, and the one a unit test never looks at.

```bash
uv run python scripts/live_check.py           # every scenario, ~5 min
uv run python scripts/live_check.py budget    # just one
```

It drives each tool family against the live API and inspects the assembled
answer for those three failure shapes, and fails a scenario if any tool returned
an error envelope. It runs against a throwaway home directory, so your own
`AGENTS.md`, workspace and conversation history are untouched — `CHECKPOINT_DB`
is derived from `AGENT_HOME` rather than pinned to the repository, which is what
keeps the transcript out of your real database. Needs `ANTHROPIC_API_KEY`; the
search scenario is skipped without `TAVILY_API_KEY`, and a run left with no
scenarios exits non-zero rather than reporting a clean pass over nothing.

## Known limits

- `test_withdrawal_plan` is a **single deterministic path**, not a Monte Carlo
  simulation. It ignores sequence-of-returns risk, the dominant risk in early
  retirement. Surviving it is necessary, not sufficient.
- `get_fund_profile` reports the provider's raw expense-ratio value, which
  yfinance scales inconsistently. Confirm against the fund factsheet.
- US-centric: APR conventions, IRS limits, Social Security.
- Single-user by design. `FilesystemBackend` is unsafe in a shared web server —
  for multi-user deployment, swap to a `StoreBackend` namespaced per user.
