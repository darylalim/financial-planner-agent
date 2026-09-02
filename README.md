# Financial Planner Agent

A personal financial planning agent built on
[Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview), with a
Streamlit chat UI.

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

Then attach a CSV or XLSX transaction export in the chat box, or try
`examples/sample-transactions.csv` to see it work with no data of your own.
PDFs are accepted too, but for *reading* only — a budget needs columns. See
[PDFs are readable, not aggregatable](#pdfs-are-readable-not-aggregatable).

`.streamlit/config.toml` binds the app to loopback — provided you run that exact
command from the repository root, and pass no `--server.address` of your own. It
is a whole file that can go missing without breaking anything, which is why
[Storage and the security boundary](#storage-and-the-security-boundary) spends a
paragraph on it.

## How it's put together

| Layer | Purpose |
|---|---|
| `src/financial_planner/finance.py` | Pure financial math, no LangChain imports |
| `src/financial_planner/tools/` | Calculators, document ingestion, market data, web search |
| `src/financial_planner/agent.py` | Assembles the deep agent: model, tools, prompt, backend |
| `src/financial_planner/streaming.py` | Translates LangGraph stream events into UI events |
| `src/financial_planner/envelope.py` | The JSON result envelope every tool returns, and its secret redaction |
| `src/financial_planner/rendering.py` | Markdown prep for Streamlit ([Dollar signs are LaTeX](#dollar-signs-are-latex)) |
| `src/financial_planner/uploads.py` | Untrusted upload names → safe, non-colliding workspace paths |
| `src/financial_planner/prompts.py` | The system prompt, which is what enforces the arithmetic rule |
| `src/financial_planner/config.py` | Paths and credentials, read from the environment at import time |
| `agent_home/` | The agent's entire filesystem view ([Storage and the security boundary](#storage-and-the-security-boundary)) |
| `app.py` | Streamlit chat UI |

### The arithmetic rule

**The model never does the math.** Every figure comes from a *tool*, never from
the model's own arithmetic. A language model will produce a *plausible*
compound-interest number that is wrong by tens of thousands of dollars, and the
person reading it has no way to tell.

Most of those figures come out of `finance.py`, which `tools/calculators.py`
wraps. Two tools compute their own and never import it: `get_historical_return`
derives a CAGR and a volatility from a price series, and `summarize_spending`
aggregates in pandas. They are still tools — the invariant is about *where* a
number is computed, not which module computes it.

The system prompt enforces the rule, and every calculation in `finance.py` is
pinned in `tests/test_finance.py`. Some tests fix externally verifiable values,
such as a standard mortgage payment and textbook compound growth. The rest are
invariants (avalanche never costs more than snowball), self-consistency
round-trips, and error paths.

Annual-to-monthly rate conversion is done **two different ways on purpose**, and
that is the subtlety to know before extending it:

- **Investment returns** use the geometric form `(1+r)^(1/12) − 1`, because "7%
  expected return" means 7% effective.
- **Debt** uses `APR/12`, because that is the convention US lenders quote and it
  reproduces the payment on the user's own statement.

Using the geometric form on a $300k/6% mortgage gives ≈$1,770/mo against the
bank's actual $1,798.65.

The rule extends to ratios: a `savings_rate` or a category's
`by_category_share_of_income` is returned by `summarize_spending`, not divided by
the model. The model can only obey that if a tool actually offers the number,
which is why those keys exist.

### Reading the stream

`streaming.py` looks small and has four traps in it. A run found the first three:

- **Filter by `isinstance`, not by `.type`.** LangGraph's `messages` mode emits
  *every* message type, so tool-result JSON arrives on the same stream as the
  answer. But a streamed chunk reports `type == "AIMessageChunk"`, not `"ai"`,
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
streams jumped straight from text to text, a shape the network never sends. The
tests now replay the interleaving a real provider produces.

**Two populations of tools report failure two different ways.** This fourth trap
took a code review rather than a run to surface. Our own tools return the
`{"error": ...}` envelope; the Deep Agents built-ins never touch it and set
`status="error"` on the `ToolMessage` instead. Testing only the envelope reported
a failed `write_file` to the UI as a success, so the app printed nothing. That is
precisely the "agent claimed it saved a file it never wrote" defect
[the live check](#the-live-check) was written to catch. `_tool_message_failed`
checks both signals.

### The result envelope

Every tool returns JSON through `envelope.py`, and returns errors rather than
raising, so a bad argument lets the model read the problem and retry instead of
ending the turn. Two things about it are behaviour, not formatting:

- `streaming._is_error_result` decides whether the UI marks a call failed by
  matching the *serialized* text against `{"error"`. Compact separators and key
  order are load-bearing. It is only half the test — see the fourth trap under
  [Reading the stream](#reading-the-stream) for the tools that never send this
  envelope.
- Everything it returns lands in the model's context and the saved transcript,
  so `redact()` strips configured secrets first. It runs on the *success* path as
  well as the failure one, since search snippets and other relayed upstream text
  ride back through `ok()` too. Upstream exception text is relayed verbatim,
  which is what makes it useful to the model, and HTTP clients routinely quote
  the failing request back. `app.py` redacts the same way when it surfaces an
  agent-level failure, which is where a model-client error would appear.

Secrets are recognised by the shape of their name on `config`: anything ending
`_API_KEY`, `_TOKEN` or `_SECRET`, holding a value of eight characters or more.
A third credential is therefore covered the day it is added rather than the day
someone remembers to extend a list.

A bare `_KEY` was in that tuple and is not any more. It names an ordinary
constant as readily as a credential, and a `PARTITION_KEY = "transaction_date"`
would have had `redact()` shred that word out of every result that mentioned it —
a spending breakdown's category labels, a schema listing, an extracted PDF page —
with no error raised. The visible cost is that an `ENCRYPTION_KEY` is *not*
covered and has to be renamed to be.

The envelope lives in one module because four copies of it had already drifted:
one omitted the exception type, and only one redacted anything.

### Dollar signs are LaTeX

`st.markdown` renders `$...$` as inline math. "You'd have $2.26M at 7% and $1.56M
at 5%" therefore renders the middle as italic gibberish, and this agent puts two
dollar amounts in almost every sentence it writes.

`rendering.escape_dollars` escapes `$` before anything reaches `st.markdown`,
skipping code spans and fenced blocks — markdown ignores backslash escapes inside
those, so escaping there would show a literal `\$`.

The markdown source is correct, so the unit tests passed and the live check
passed; `AppTest` inspects the source rather than the render too. Nothing but
opening the app and reading the screen could have *found* it.

Once found it is ordinary to test, and `tests/test_rendering.py` pins the
escaping directly, including three cases the first version got wrong. A
four-space-indented block and a `~~~` fence were not recognised as code at all,
so their contents were escaped and the reader saw a literal `\$`. And a fence
that is open but not yet closed — the normal state of an answer mid-stream — was
not code either: dollars inside it were escaped on every token, then silently
un-escaped when the closing fence arrived, rewriting the block on screen. The
scan is line-based now, and an open fence runs to the end of the text.

**`rendering.py` exports a second escaper, and using the wrong one is a bug
class.** `escape_dollars` is for model *prose* headed to `st.markdown`.
`escape_markdown` is for untrusted *data* that must not render at all — a
browser-supplied filename, a raw exception string — headed to `st.warning`,
`st.error` or `st.caption`. `uploads.py` strips only the directory component from
an upload name, never markdown metacharacters, so the escaping has to happen at
the render site.

### Storage and the security boundary

The agent's filesystem root is `agent_home/`, **not** the repository root:

```text
agent_home/
├── AGENTS.md        gitignored; always-loaded profile, agent-maintained
├── skills/          committed SKILL.md workflows, loaded on demand
└── workspace/       gitignored; your documents and generated plans
```

Once a conversation runs long enough to summarize, the Deep Agents summarization
middleware adds `conversation_history/` and `large_tool_results/` beside them.
Both hold the same statement data as `workspace/`, and `.gitignore` already
covers them for the same reason.

`FilesystemBackend(virtual_mode=True)` confines the agent to that root, so a
prompt injection hidden in an uploaded PDF cannot reach `.env`, the source, or
git history. The custom document tools enforce the same boundary independently,
since they receive paths straight from model output.

Two kinds of persistence:

- **Files** on real disk, so the profile and generated plans survive restarts.
- **Conversation and todo state** in the checkpoint database, keyed by thread.

`CHECKPOINT_DB` locates that database, and it has two branches. Under the default
home it is `planner_state.sqlite` in the repository root, keeping an existing
install's saved conversations where they already are. Point
`FINANCIAL_PLANNER_HOME` somewhere else — before the first `financial_planner`
import, since `config.py` reads it then — and the database becomes
`<home-name>-state.sqlite` in that home's parent directory. The filename carries
the home's name because the parent alone is not unique: `/data/homes/alice` and
`/data/homes/bob` would otherwise share one thread store, and either household
could resume the other's finances by thread id. Either way the database is a
*sibling* of the home and never inside it, so the agent cannot read its own
transcript.

**`.streamlit/config.toml` is the other half of this boundary, and it is a whole
file that can go missing.** Streamlit leaves `server.address` unset, and falls
back to `0.0.0.0`; its bind helper widens the unset value further still, to the
dual-stack `::`. Without that file, `uv run streamlit run app.py` serves the
sidebar's statement filenames, the chat box, the upload path and the API key's
spend to every device on the network, unauthenticated. None of the defences above
reach it: they all assume the attacker arrived inside an uploaded document, not
over TCP. `allowedHosts` is
pinned beside `address` because an empty list accepts any `Host` header, so a
hostile page that resolves its own name to `127.0.0.1` would reach a
loopback-bound app as same-origin. Deleting the file breaks nothing and prints no
warning; a Network URL simply reappears.

### Skills

Three workflows in `agent_home/skills/`, loaded only when relevant:

- `retirement-readiness` — project, then test the drawdown, then quantify the gap.
- `budget-from-statements` — verify sign conventions, then aggregate.
- `debt-strategy` — avalanche vs snowball, then debt-vs-investing.

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
| `summarize_spending` | Aggregate a CSV/XLSX export by category and month, with savings rate and monthly averages |
| `read_pdf_text` | Extract a page range from a PDF |
| `get_quote` | Current prices for one or more symbols |
| `get_fund_profile` | Expense ratio and category |
| `get_historical_return` | Realized annualized return over the elapsed calendar span, and volatility scaled by the series' own bar rate |
| `search_web` | Current rates, limits and rules (Tavily) |

Two behaviours do not fit in the table. `get_quote` names the symbols that failed
and errors only if none resolve, so one bad ticker does not lose the rest. And
document tools **summarize rather than dump**: a year of transactions is 5,000+
rows, and returning them raw would crowd out the rest of the session.

### Narrowing the built-ins

The agent also gets the Deep Agents built-ins: `write_todos`, `ls`, `read_file`,
`write_file`, `edit_file`, `glob`, `grep`, `task`.

That list is not what `create_deep_agent` binds on its own: it also binds
`delete` and `execute`, and does not bind `write_todos` at all. So `build_agent`
passes a `middleware=` list that narrows the filesystem tools and adds the
planning tool back.

`delete` is withheld because the agent only ever appends to `/workspace/` and
edits `/AGENTS.md`. Handing recursive deletion to something that also ingests
prompt-injectable PDFs buys nothing, and the UI has no approval gate. `execute`
could only return an error anyway, since `FilesystemBackend` is not a sandbox
backend. `tests/test_agent.py` pins the exact bound set, because the middleware
override is matched by class name and would fail open on a rename. It pins the
`task` subagent's set as well: that subagent is a second agent over the same
`agent_home`, so checking only the main graph would let it regain `delete` with
every other assertion still passing.

### PDFs are readable, not aggregatable

The upload box accepts PDFs, and `inspect_document` and `read_pdf_text` both read
them. `summarize_spending` does not: a PDF has no columns, so there is nothing to
group by category or by month.

That gap is not going to be closed by parsing the PDF. Extracting a transaction
table out of a rendered statement produces rows of unknown fidelity, and a budget
built on them would be a number the user cannot check, which is the failure
[the arithmetic rule](#the-arithmetic-rule) exists to prevent. A bank that emits a
PDF statement almost always offers a CSV export beside it, and that export is
authoritative in a way the rendering is not.

So the refusal is the feature, and it carries the route forward. `_load_table`
raises `NotTabular`, with a message naming both options: read the printed totals
with `read_pdf_text`, or ask for a CSV/XLSX export. It is its own exception type
because `envelope.err` serializes the class name and the model picks its recovery
off it. `inspect_document` says the same thing in its *success* payload under an
`aggregation` key, since the skill tells the agent to call it before any other
document tool, which makes it the earliest point the dead end can be seen. The
budget skill states it again, before either tool runs.

Stating one fact at every site the agent can reach it is deliberate. The generic
`unsupported table format '.pdf'` it replaced was accurate and useless: it named
the formats it wanted without saying what to do instead, so the model retried with
different column names, failed identically, and answered without the numbers.

Deliberate repetition is not the same as hand-copying, and the first version of
this was both. The refusal and the `aggregation` note differ only in their
opening sentence; everything after it `_pdf_statement` composes from clauses they
share, and a test pins that every message *ends* in the one clause each case
keeps. Written out by
hand, they had already drifted inside one commit: the refusal forbade totalling
the transaction lines by hand and the `aggregation` note did not, on the path the
agent reaches first. The skill is the one copy no constant can reach, so a test
reads the file and fails when it drifts.

**Not every PDF has both routes, which is why the text is clauses and not one
string.** A scan or a phone photo of a statement extracts to nothing, so telling
the agent to read the totals it prints is the same dead end one step further
along. `read_pdf_text` returned an empty string inside a *success* envelope,
which reads as "these pages are blank" rather than "this cannot be extracted". A
password-protected statement, which is how banks email them, could not be opened
at all and relayed pypdf's `File has not been decrypted` with no route on it. Both
now say what is actually available, and both keep the export route, the one that
survives every case.

The read route carries the arithmetic and untrusted-data caveats *inside* it, so
a site that offers the route cannot drop them: the caveat goes where its route
goes. The untrusted-data half states what provenance alone left out. The totals a
PDF prints are the document's claim, and nothing in it tells a tampered statement
from a genuine one, so they are attributed to the statement rather than reported
as the household's verified numbers.

### Sign conventions

Exports disagree about what a sign means, and reading one wrong inverts the entire
budget. `summarize_spending` handles all three layouts:

| Layout | Issuers | Parameter |
|---|---|---|
| Negative is money out | Most checking exports, Chase cards | Default |
| *Positive* is money out | Amex and several card issuers | `sign_convention="positive_outflow"` |
| Separate debit/credit columns | Capital One, most EU banks | `inflow_column="Credit"` |

**The numbers cannot settle this, and the tool does not pretend otherwise.** Both
single-column layouts produce mixed signs — a checking export is a few large
deposits against many payments, a card export is many charges against a few
payments — so "there is a negative in here" proves nothing. The information lives
in the file's provenance, which the agent can see in the preview rows and the
tool cannot.

So the default `auto` assumes the ordinary reading and *says* it assumed, by
returning `sign_convention_inferred: true` with a note to check it against the
preview. It refuses outright only for the two shapes that look like a card
statement: every value positive, or positives outnumbering negatives 3:1 or more.
Read as signed, those report every charge as income, a 100% savings rate and an
empty spending breakdown. The 3:1 threshold is deliberately loose so a household
with irregular income still passes.

Under `positive_outflow` the inflow side is card *payments*, not earnings, so
`savings_rate` and `by_category_share_of_income` are withheld rather than computed
against them, and an `income_basis` note explains why. A savings rate against your
own credit-card payments is arithmetic on unrelated quantities.

## Development

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest -q              # 387 tests, offline, no API key
```

Those three, in that order, are the gate: what `.github/workflows/ci.yml` runs and
what the local hook runs before a session ends. `--check` is what makes formatting
a verdict rather than an edit — run `uv run ruff format .` to apply it instead.

The suite is offline because the financial math is pure, the streaming translator
is driven by synthetic event streams captured from a live graph, and the Streamlit
app is exercised via `AppTest`.

CI runs the gate plus `uv sync --locked` on every push to `main` and every pull
request. It pins Python 3.11, the floor `requires-python` declares, rather than
the 3.14 a local `.venv` is likely to be, because that floor and a lockfile that
has drifted from `pyproject.toml` are the two claims nothing else tests. The gap
that leaves: **nothing automated runs on 3.14**, and adding it to a
`strategy.matrix` is what would close it. The suite needs no secrets, so the
workflow is safe on pull requests from forks. The live check is not part of it.

The local gate is `.claude/hooks/`, which runs ruff on write and the full gate
before a session ends, plus a guard that refuses edits to `.env`, the checkpoint
database, `agent_home/AGENTS.md`, the three gitignored `agent_home/` directories,
and `.claude/`'s own settings and hooks. `agent_home/skills/**` stays editable,
since the skills are tracked and routinely changed. It catches things earlier but
covers less: it skips when there is no `.venv`, arms only when a `.py` was
written through Edit/Write,
and runs only inside a Claude Code session on one machine. Run the three commands
above yourself otherwise.

### The live check

```bash
uv run python scripts/live_check.py           # every scenario, ~5 min
uv run python scripts/live_check.py budget    # just one
```

Offline tests cannot see the failures that only a real provider produces. Three
got through a green suite: raw tool JSON rendered as the answer, assistant
messages running together, and the agent reporting it had saved a file it never
wrote. All three were defects in the *rendered answer*, the only artefact the user
reads and the one a unit test never looks at.

The check drives each tool family against the live API and inspects the assembled
answer for those three failure shapes. A scenario also fails if a tool returned an
error.

The read-only Deep Agents built-ins are exempt. `read_file` on a path not written
yet, `grep` with no hits and `ls` of a directory about to be created are routine
probes the agent recovers from in its next step, and failing on those buries the
signal under benign noise. They are printed, not counted. Everything else stays
fatal, our own tools included.

It runs against a throwaway home directory, so your own `AGENTS.md`, workspace and
conversation history are untouched. `CHECKPOINT_DB` is derived from the home
rather than pinned to the repository, which is what keeps the transcript out of
your real checkpoint database. It needs `ANTHROPIC_API_KEY`; the search scenario
is skipped without `TAVILY_API_KEY`, and a run left with no scenarios exits
non-zero rather than reporting a clean pass over nothing.

### Releases

A push to `main` carrying a `version` in `pyproject.toml` that has no tag yet
publishes a GitHub Release, tagged `v<version>`, with notes generated from the
commits since the previous one.

The trigger is the *absence of a tag* rather than a diff against the previous
commit. Diffing breaks on squash merges, re-runs and force pushes; the tag is the
durable record of what has already shipped, so asking it makes the job idempotent.
A bump releases once no matter how many times the workflow runs over it, and a run
cancelled by a later push is picked up by the run that replaced it. The rule is
only complete if every shipped version has a tag, which is why `v0.1.0` is tagged
at the commit before the automation landed. Without that baseline the first run
would have published the current `main` as a new 0.1.0 release.

It runs only after the test job passes, and `contents: write` is scoped to the
release job alone. The rest of the workflow, including everything that runs on
untrusted pull requests, holds a read-only token.

## Known limits

- `test_withdrawal_plan` walks a **single deterministic path**, not a Monte Carlo
  simulation. It ignores sequence-of-returns risk, the dominant risk in early
  retirement. Surviving it is necessary, not sufficient.
- `get_fund_profile` reports the provider's raw expense-ratio value, which
  yfinance scales inconsistently. Confirm against the fund factsheet.
- US conventions throughout: APR quoting, IRS limits, Social Security.
- `FilesystemBackend` is single-user by design and unsafe in a shared web server.
  Multi-user deployment needs a `StoreBackend` namespaced per user.

## License

MIT — see [LICENSE](LICENSE).
