# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

A personal financial planning agent built on Deep Agents, with a Streamlit chat UI.
`README.md` carries the full rationale for every design decision below — this file
is the operational summary and the list of things that break silently.

## Commands

```bash
uv sync                        # install
uv run streamlit run app.py    # the app
```

**The verification gate** is these three, in this order — what `.claude/hooks/verify.sh`,
CI and the README all run. `--check` is what makes formatting a verdict rather than an
edit; `ruff format .` applies it instead, and is the only form that is not the gate:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest -q               # offline, no API key, ~30s
```

**Run `pytest` locally with the keys hidden**, the way CI runs it:

```bash
ANTHROPIC_API_KEY= TAVILY_API_KEY= LANGSMITH_TRACING=false uv run pytest -q
```

`config.py` calls `load_dotenv()`, so a plain local run picks up the owner's real `.env`.
A test that builds a real model could then pass locally and fail in CI, or make a billed
call. The `.env` also turns LangSmith tracing on, so every test that compiles a graph
uploads a trace; this once used up the monthly trace quota in the middle of a gate run.
`load_dotenv()` never overrides a variable that is already set, even to an empty string,
which is why this works. The Stop hook runs plain `pytest`, so it does not count as the
gate.

Tests are grouped in classes, so `tests/test_x.py::test_name` with no class selects
nothing and exits **green** with only a warning. The forms that work:

```bash
uv run pytest tests/test_finance.py                          # file
uv run pytest tests/test_finance.py::TestRateConversions      # class
uv run pytest "tests/test_finance.py::TestRateConversions::test_apr_uses_lender_convention"
uv run pytest -k sign                                         # keyword
```

The live check spends real money against the live API and is not in CI. Scenario names
are the keys of `SCENARIOS` in the script:

```bash
uv run python scripts/live_check.py            # all scenarios, ~5 min
uv run python scripts/live_check.py budget     # one
```

CI (`.github/workflows/ci.yml`) runs `uv sync --locked` plus the gate on Python 3.11 —
the `requires-python` floor — and on 3.14, what a local `.venv` is likely to be. A local
3.11 run must use `uv run --isolated --python 3.11`; without `--isolated` it rebuilds
the project `.venv` on 3.11 under the hooks. A push to `main` whose `pyproject.toml`
version has no tag yet publishes a GitHub Release. **When a PR changes what the app does,
bump the version with `uv version <x.y.z>` as the PR's last commit.** A PR that only
touches docs needs no bump. `uv version` updates `uv.lock`'s copy of the version
too. Editing `pyproject.toml` by hand fails CI's `uv sync --locked`, and so does
committing `pyproject.toml` and `uv.lock` separately.

**Live checks and any one-off script that calls the API spend real money: ask the owner
first.** Ask once per piece of work, giving an estimate of model time and the list of
runs. Before a paid batch, do one cheap run to shake out the harness. To compare models,
use `--repeat N --model SPEC`, because one run of a model whose output varies says
little. Upload live fixtures under neutral names: the model reads a filename as evidence.

## Architecture

Each point is an invariant that breaks silently, not a description.

**The model never does arithmetic.** Every figure the user sees comes from a tool. Most
come from `finance.py` (pure math, no LangChain imports) via `tools/calculators.py`;
`get_historical_return` and `summarize_spending` compute their own. The invariant is
about *where* a number is computed, not which module computes it. Ratios count — a
savings rate or category share is returned by `summarize_spending`, which is why those
keys exist. The rule is enforced at runtime by `SYSTEM_PROMPT` in `prompts.py`
("# The arithmetic rule (non-negotiable)"), so **a new tool returning a new kind of
figure needs the prompt extended too** — the tool alone does not enforce it. A skill is
held to the same rule: one that names a figure must name the field that returns it. The
retirement skill promised "the additional monthly contribution" from a tool that
returned only the total, and the model subtracted to fill the gap.

**Estimates are whole dollars; amounts that must match a statement keep cents.**
`finance.py`'s module docstring has the split. Differences are taken from the rounded
figures, so a fee gap still equals its parts. Putting cents back on an estimate brings
back the truncation (`820949.52` shown as `$820,949`) that the live check fails as a
figure no tool produced.

**The live check fails a run on any unsourced figure, with no tolerance.** README → The
live check says what counts as sourced. The prompt's stated defaults are among the
sources, read from `prompts.PROMPT_DEFAULT_PERCENTS`, so change a default there, never in
the prompt's prose. A value the model passed *into* a tool is never a source: a figure it
computed and then handed to a calculator is still its own arithmetic. The check cannot
enforce that when the tool **echoes the input back**: the output then sources it. A
retirement run passed `$6,500 − $2,500` to `simulate_withdrawal_plan` as `$4,000` and
went green. The echoes stay, because they carry the assumptions to state, so when
reading a live run, read the tool *inputs* for arithmetic too. Every source the
checker accepts is a hole in the check. **Read an unsourced
figure before writing a rule for it**: every one found so far was a tool that did not
return a figure people ask for, fixed in the tool. Add a source to
`scripts/answer_checks.py` only with a test built from a recorded example of the figure
it legitimately covers. The check sees only `$` and `%` figures, so an age the model
worked out ("about nine years before 95") passes it.

**Annual→monthly rate conversion is done two ways on purpose.** Investment returns use
the geometric `(1+r)^(1/12)−1`; debt uses `APR/12`, the convention US lenders quote.
Unifying them silently breaks the mortgage payment on the user's own statement.

**Two roots, and the split is a security boundary.** The agent's filesystem root is
`agent_home/`, never the repo root. `FilesystemBackend(virtual_mode=True)` confines it
there, so a prompt injection in an uploaded PDF cannot reach `.env`, the source, or git
history. `tools/documents.py` re-enforces the same boundary independently because it
receives paths straight from model output. The agent sees `/workspace/`, `/skills/`,
`/AGENTS.md`; on disk these live under `agent_home/`.

**`.streamlit/config.toml` is the other half of that boundary, and it is a whole file
that can go missing.** Streamlit's `server.address` defaults to `0.0.0.0`, and
`_get_bind_address()` widens an unset value to the dual-stack `::` — so without that file
`uv run streamlit run app.py` serves the sidebar's statement filenames, the chat box, the
upload path and the API key's spend to every device on the network, unauthenticated. The
agent-side defences above do not reach it: they all assume the attacker arrived inside an
uploaded document. Deleting the file breaks nothing at runtime and prints no warning; a
Network URL simply reappears. `allowedHosts` is pinned beside `address` because an empty
list accepts any `Host` header. README documents the file for the same reason; its
"single-user by design" is the prose, this is the latch. The file now also carries the
light/dark theme, so it gets edited for cosmetic reasons — append below `[server]`, never
reorder it. `tests/test_app.py::TestStreamlitConfig` pins both server settings and, for
the theme, both modes, no web fonts, and WCAG AA contrast computed from the file's colours.

**`CHECKPOINT_DB` has two branches and both are load-bearing.** Default home →
`PROJECT_ROOT/planner_state.sqlite` (the historic name, so an existing install's saved
conversations are not orphaned). Redirected home → `AGENT_HOME.parent /
f"{AGENT_HOME.name}-state.sqlite"` — named *after the home* because the parent alone is
not unique: `/data/homes/alice` and `/data/homes/bob` would otherwise share one thread
store, and either household could resume the other's finances by thread id. Always a
sibling of `AGENT_HOME`, never inside it, so the agent cannot read its own transcript.
`.claude/hooks/guard-sensitive-paths.sh` re-derives *both* branches (`lib.sh` mirrors
only `AGENT_HOME`). `tests/test_agent.py::TestCheckpointDatabaseLocation` pins
`config.py`'s copy and `tests/test_hooks.py::TestARedirectedHome` the guard's.

**`config.py` reads the environment at import time.** `tests/conftest.py` therefore sets
`FINANCIAL_PLANNER_HOME` before the first `financial_planner` import. The throwaway home
must be a *child* of the temp dir, not the temp dir itself — `CHECKPOINT_DB` is a sibling
of the home, so a home with no parent of its own drops the database in the shared system
temp root, outside the `atexit` cleanup. `envelope.py` imports `config` as a module, not
by value, for the same import-time reason.

**The tool set is narrowed, and the narrowing is fragile by design.** `build_agent`
passes `middleware=` to splice into (not replace) the default stack. A custom middleware
whose `.name` matches a default replaces it in place — which is how `FilesystemMiddleware`
drops `delete` and `execute`, and why `TodoListMiddleware` has to add `write_todos` back.
Matching is by class name, so a rename fails open. `test_agent.py` pins both the main
graph's bound set (`TestBoundTools`) **and the `task` subagent's**
(`TestSubagentInheritsTheNarrowedFilesystem`) — the subagent is a second agent over the
same `agent_home`, and checking only the main graph would let it regain `delete` over the
household's statements with every other assertion still passing.

**The result envelope is a contract between two modules.** `envelope.ok/err` serialize
with compact separators and `"error"` first because `streaming._is_error_result` matches
the *serialized* text against `{"error"`. Separators and key order are behaviour, not
formatting. `redact()` runs on the success path too — search snippets relay upstream
text — and finds secrets by name shape on `config`: exactly `_API_KEY`, `_TOKEN`,
`_SECRET`, and only values ≥8 chars. A bare `_KEY` was **deliberately removed** (it names
ordinary constants as readily as credentials, and redacting one would silently shred
payload data), so `ENCRYPTION_KEY` is *not* covered and has to be renamed to be.

**Two populations of tools report failure two different ways.** Ours return the
`{"error": ...}` envelope; the Deep Agents built-ins set `status="error"` on the
`ToolMessage` and never touch it. `_tool_message_failed` checks both. Checking only one
made a failed `write_file` render as a success.

**`streaming.py` is small and trap-dense.** Filter by `isinstance`, not `.type`
(a streamed chunk reports `"AIMessageChunk"`, not `"ai"`). A change of message `id` is
the paragraph boundary between the agent's preamble and its answer — but read that `id`
only off chunks that carry text, since Anthropic sends a content-free chunk bearing the
new id first. Synthetic streams that jump text→text pass while the real provider fails;
the tests now replay the real interleaving. Middleware that repairs history (after a
Stop mid-tool-call, or an invalid tool call) rewrites *all* of it, and both stream modes
relay the rewrite as new; `_history_before_turn` seeds the turn with the thread's ids and
tool calls so the previous answer is not painted into the new one. A synthetic stream
cannot show this — `TestARewrittenHistoryIsNotReplayed` drives a real compiled graph.

**The assistant's transcript entry is reserved before the turn runs, never appended
after.** `app.py` puts a placeholder dict into `st.session_state.messages` ahead of the
first token and then mutates it. Appending at the end loses a race no `except` can win:
Streamlit delivers rerun and stop requests as `ScriptControlException`, which subclasses
`BaseException` precisely so user code cannot catch it, and every `st.*` call in the
streaming loop is a delivery point — Stop, toolbar Rerun, `runOnSave`, a click on any
widget left mounted. The user's message is already in the list, so the loser of that race
is a question with no reply under it, redrawn as an agent that ignored it. That same dict
is also what keeps text already painted when a turn fails part-way, and what carries
`tool_errors` past the post-turn `st.rerun()` that destroys the `st.status` they were
written into. `TestATurnAlwaysLeavesAReply` and `TestToolActivity` pin both.

**Two escaping functions, deliberately distinct — using the wrong one is a bug class.**
`escape_dollars` is for model *prose* headed to `st.markdown`: `$...$` renders as inline
LaTeX and this agent puts two dollar amounts in most sentences. Its scan is line-based,
skips code spans and fenced blocks, and treats an unclosed fence as running to the end of
the text — the normal state of a mid-stream answer. `escape_markdown` is for untrusted
*data* that must not render at all (browser-supplied filenames, raw exception strings)
headed to `st.warning`/`st.error`/`st.caption`; `uploads.py` strips only the directory
component from an upload name, never markdown metacharacters. Regressions in both *are*
testable (`tests/test_rendering.py`, and `test_app.py` asserts escaping at the `st.error`
boundary); what no test could do was **find** the class originally, since unit tests and
`AppTest` both inspect markdown source rather than the render.

**The summarizer is replaced by name, to carry `FINANCE_SUMMARY_PROMPT`, and that
fails open.** It goes in `middleware=` like the filesystem narrowing, so an upstream
rename (or a subclass, whose `.name` differs) silently restores the coding-session
default prompt. `TestSummarizerIsFinanceAware` pins it on both graphs. `build_model`
sends `drop_block` only to `config.THINKING_BINDING_MODELS`, the models that run
the thinking-block history check a summary trips.

**Sign conventions cannot be settled by the numbers.** `summarize_spending` handles
negative-outflow, `positive_outflow` (Amex and several card issuers), and separate
debit/credit columns (`inflow_column=`). The parameter defaults to `"auto"`, not to a
layout: `auto` assumes the ordinary reading and *says* it assumed via
`sign_convention_inferred`, and raises `AmbiguousSignConvention` for the two shapes that
look like a card statement. Under `positive_outflow` the inflow side is card payments,
not earnings, so `savings_rate` is withheld rather than computed.

**PDF text reaches only the `document-reader`, an agent with nothing else to call.**
The main agent can write `/AGENTS.md`, so it never sees a PDF's words: `read_pdf_text`
is in `READER_TOOLS`, not `ALL_TOOLS` (which the writing `general-purpose` subagent
inherits), and `inspect_document` returns a PDF's structure, never its text. The
reader's spec in `build_agent` must name `tools` and replace `FilesystemMiddleware` —
a declarative subagent otherwise inherits every main tool and gets a *fresh* full
filesystem. It is replaced by `_NoFilesystem`, a no-op matched by name, because
deepagents refuses a `FilesystemMiddleware` without `read_file`, and because the library
writes for an agent whatever tools it has: eviction would put an oversized result, raw
PDF text, in `/large_tool_results/` for the main agent to read. **A quarantine has to
cover what the library writes, not just the tools.** Never give the reader
`mode="fork"` (it would inherit the main conversation). Its answer is still
document-derived data. `TestDocumentReaderIsQuarantined` pins its exact set;
`TestTheReaderNeverOffloads` its eviction, by behaviour.

**PDFs are readable, not aggregatable — and the refusal carries the route forward.**
`summarize_spending` rejects PDFs; `inspect_document` inspects them; only the reader's
`read_pdf_text` reads them. The built-ins cannot: `agent.PDF_READ_DENY` denies
`read_file`, `ls`, `glob` and `grep` every PDF (else `read_file` hands over the whole
document, scans included), spelled case-insensitively and for dotfiles because the
matcher is neither. Since listings then hide PDFs, `app.py` names the files on file in
the user's message on a conversation's first turn and on any upload turn
(`TestTheMainAgentCannotReadPdfsDirectly` pins the rule by behaviour). The dead end is
stated repeatedly on purpose, because the generic "unsupported format" message it
replaced made the model retry with different column names. The statements now have
**two audiences**: the five runtime ones (`NotTabular`, `PdfLocked`, both
`inspect_document` aggregation branches, `read_pdf_text`'s note) are composed by
`_pdf_statement` from shared clauses, and the main-facing ones offer
`PDF_DELEGATE_ROUTE`; `PDF_READ_ROUTE` goes only into the reader's system prompt. Both
routes are built from `PDF_TOTALS_CAVEATS`, so the no-arithmetic and untrusted-data
warnings cannot drift apart. Only the routes a given document actually has are offered.
Tests pin that every statement ends in `PDF_EXPORT_ROUTE`, that no main-facing one names
`read_pdf_text`, and that the budget skill's prose has not drifted from the constants.

**The layout adapts only through Streamlit's own reflow, and the server never learns
the window width.** `st.context` has no viewport width, so Python cannot branch on it.
On a narrow window the sidebar starts collapsed, so **nothing the user needs may live
only in the sidebar**. That is why "New conversation" sits in the header row and
`DISCLAIMER` appears in three places from one constant. `layout="centered"` is deliberate:
the app is prose, and line length is what keeps it readable on a wide screen. A markdown
table is the one thing reflow cannot fix. On 1.66.1 a table too wide for the column
makes the whole `stMain` scroll sideways rather than scrolling in place, so the prompt
caps tables at four columns. Tests find widgets by label, not by index.

**Skills encode procedure, not finance knowledge.** Three in `agent_home/skills/`, loaded
on demand. `retirement-readiness` exists mainly because the accumulation-then-drawdown
structure is easy to skip, and skipping it is the common failure.

## Conventions

- **Ruff** config is in `pyproject.toml` (`[tool.ruff]`) and is the source of truth;
  line-length 100 is the one worth knowing while writing. The PostToolUse hook applies it
  automatically after any Edit/Write of a `.py`.
- **Do not restate a fact that already lives somewhere else.** This repo has repeatedly
  been bitten by drift — four copies of the result envelope, two of the PDF refusal, path
  handling duplicated across the hooks. `verify.sh` deliberately omits the test count for
  this reason. Prefer a pointer or a shared constant to a copy.
- **Commit messages** are an imperative subject naming the fix and its reason ("Judge a
  PDF page by page, not by whichever one was sampled"), with a body explaining the defect,
  how it failed, and why the chosen fix is the right shape.
- **`.claude/hooks/` gates local work**: ruff on write, the gate above before a session
  ends, and a PreToolUse guard refusing Edit/Write to `.env`, the checkpoint DB,
  `.claude/settings.json` + `.claude/hooks/*`, and everything under the agent's home except
  `skills/**` and `workspace/.gitkeep`. That last part is an allowlist, like `.gitignore`'s,
  so a runtime folder a library adds later is refused by default; it skips itself if the
  home is the repo root or above, where it would refuse every edit. `.env.example` stays
  editable. `tests/test_hooks.py` pins the guard by running it on payloads.
  Bash is unmatched by design, so this is a tool-dispatch filter, not a boundary; it also
  skips without a `.venv` and arms only when Edit/Write touched a `.py`. Run the gate
  yourself when working outside those conditions.
- **Never commit real household data**: `agent_home/workspace/*` (except the tracked
  `.gitkeep`), `agent_home/AGENTS.md`, `agent_home/conversation_history/`,
  `agent_home/large_tool_results/`. Ignore patterns must be spelled with the full
  `agent_home/` prefix — a pattern containing a slash is anchored to the directory holding
  `.gitignore`, so a bare `workspace/*` silently matches nothing here.
- **Word a prompt rule about *how* to do something conditionally** ("If you do record
  one…", "If you do use a table…"). Worded as an order, Opus 5.5 treats it as an
  instruction to do the thing: an imperative memory rule took saves after reading a
  statement from 1 run in 20 to 10 in 10. Tests pin the conditional wording.
- **Claude never opens the real checkpoint database**, because it holds household data.
  When saved conversations matter (a library, model or tool-set change), the owner counts
  threads read-only. The database is in WAL mode, so `-readonly` fails with error 14 when
  no `-shm` file exists; use `sqlite3 "file:planner_state.sqlite?immutable=1"`, which is
  safe while there is no `-wal` file. Use a throwaway `FINANCIAL_PLANNER_HOME` for
  Claude's own resume tests.
- **Planning.** A fix or feature that fits in one session is a GitHub issue → one branch
  → one PR (`Fixes #N`). Work spanning several sessions gets a plan in `docs/plans/`,
  started from `docs/plans/TEMPLATE.md`, with one phase per PR. Delete the plan in its own
  small PR once the last phase merges, after moving anything still true to README or this
  file. A plan holds goals and decisions and points at code; it does not describe the
  code, because that copy drifts.

## Upgrading dependencies

`uv lock --upgrade` moves every package, and the failures it causes here are silent, so
passing tests are not enough on their own.
- **Read the `deepagents` changelog for the internals this repo relies on:** middleware
  name matching; `FilesystemMiddleware(tools=...)`; general-purpose subagent assembly and
  what it inherits; `create_summarization_middleware` and its `summary_prompt`; harness
  profiles (`TestHarnessProfileAssumptions` assumes none overrides Anthropic tools); the
  default middleware stack and the nodes in its tool loop (`TestRecursionLimitBudget`; a
  new node once ran `recursion_limit` out a quarter sooner); which extensions `read_file`
  opens as multimodal (relevant to `PDF_READ_DENY`); and where eviction and summarization
  write. Then run the live check: two regressions only showed up live.
- **Check which HTTP client `anthropic` raises from.** `live_check.TRANSPORT_ERRORS`
  names `httpx2` because `anthropic` 1.17 makes its calls with it, not `httpx`. If the SDK
  moves, a dropped connection crashes the batch instead of being reported as *not run*,
  and the offline tests cannot tell.
- **Put a `ruff` bump in one mechanical commit with the lock**, formatting applied to the
  whole repo, before hand-editing any `.py`. Otherwise the hook scatters the new
  formatting through unrelated diffs. Don't force a package past another package's cap.
- **Diff the licences** of all installed packages before and after (`importlib.metadata`),
  and stop at anything in the GPL family. `peewee` (pulled in by `yfinance`) ships no
  licence field; its repository says MIT.
- **New runtime folders** under `agent_home/` need no action: both the `.gitignore` and
  the edit guard are allowlists. Still look for one after a live run
  (`git status --short --ignored agent_home/`), because a new folder means the library
  writes something new, and the quarantine has to cover what the library writes.

## Known limits

`test_withdrawal_plan` is a single deterministic path that ignores sequence-of-returns
risk; `simulate_withdrawal_plan` covers it, but with one lognormal asset and fixed
inflation. Its seed is fixed on purpose, so a re-ask gets the same probability.
`get_fund_profile` converts yfinance's expense ratio by the scale of the field it came
from; that each field's scale is fixed was checked on eight funds, not promised by
yfinance. US-centric throughout. Single-user by design:
`FilesystemBackend` is unsafe in a shared server; multi-user needs a per-user-namespaced
`StoreBackend`.
