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

**The verification gate** is these three, in this order — what `.claude/hooks/verify.sh`
and CI both run. `--check` is what makes formatting a verdict rather than an edit; the
README's snippet lists the same tools in a different order and formats in place, and is
deliberately *not* the gate:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest -q               # offline, no API key, ~30s
```

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
the `requires-python` floor, not the 3.14 a local `.venv` is likely to be. **Nothing
automated runs on 3.14.** A push to `main` whose `pyproject.toml` version has no tag yet
publishes a GitHub Release.

## Architecture

Each point is an invariant that breaks silently, not a description.

**The model never does arithmetic.** Every figure the user sees comes from a tool. Most
come from `finance.py` (pure math, no LangChain imports) via `tools/calculators.py`;
`get_historical_return` and `summarize_spending` compute their own. The invariant is
about *where* a number is computed, not which module computes it. Ratios count — a
savings rate or category share is returned by `summarize_spending`, which is why those
keys exist. The rule is enforced at runtime by `SYSTEM_PROMPT` in `prompts.py`
("# The arithmetic rule (non-negotiable)"), so **a new tool returning a new kind of
figure needs the prompt extended too** — the tool alone does not enforce it.

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
uploaded document. Deleting the file breaks nothing and prints no warning; a Network URL
simply reappears. `allowedHosts` is pinned beside `address` because an empty list accepts
any `Host` header. The README's "single-user by design" is the prose, this is the latch.

**`CHECKPOINT_DB` has two branches and both are load-bearing.** Default home →
`PROJECT_ROOT/planner_state.sqlite` (the historic name, so an existing install's saved
conversations are not orphaned). Redirected home → `AGENT_HOME.parent /
f"{AGENT_HOME.name}-state.sqlite"` — named *after the home* because the parent alone is
not unique: `/data/homes/alice` and `/data/homes/bob` would otherwise share one thread
store, and either household could resume the other's finances by thread id. Always a
sibling of `AGENT_HOME`, never inside it, so the agent cannot read its own transcript.
`.claude/hooks/guard-sensitive-paths.sh` re-derives *both* branches (`lib.sh` mirrors
only `AGENT_HOME`), and `tests/test_agent.py::TestCheckpointDatabaseLocation` pins them.

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
the tests now replay the real interleaving.

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

**Sign conventions cannot be settled by the numbers.** `summarize_spending` handles
negative-outflow, `positive_outflow` (Amex and several card issuers), and separate
debit/credit columns (`inflow_column=`). The parameter defaults to `"auto"`, not to a
layout: `auto` assumes the ordinary reading and *says* it assumed via
`sign_convention_inferred`, and raises `AmbiguousSignConvention` for the two shapes that
look like a card statement. Under `positive_outflow` the inflow side is card payments,
not earnings, so `savings_rate` is withheld rather than computed.

**PDFs are readable, not aggregatable — and the refusal carries the route forward.**
`summarize_spending` rejects PDFs; `inspect_document` and `read_pdf_text` accept them.
The dead end is stated repeatedly on purpose, because the generic "unsupported format"
message it replaced made the model retry with different column names. `_pdf_statement`
composes **all five** runtime statements from shared clauses — `NotTabular`, `PdfLocked`,
both `inspect_document` aggregation branches, and `read_pdf_text`'s note — so a clause
change reaches every site; only the routes a given document actually has are offered.
Tests pin that every statement ends in `PDF_EXPORT_ROUTE`, and that the budget skill's
prose has not drifted from the constants.

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
  ends, and a PreToolUse guard refusing Edit/Write to `.env`, `agent_home/AGENTS.md`,
  `agent_home/{workspace,conversation_history,large_tool_results}/*`, the checkpoint DB,
  and `.claude/settings.json` + `.claude/hooks/*`. `agent_home/skills/**`, `.env.example`
  and `workspace/.gitkeep` stay editable — the skills are tracked and routinely edited.
  Bash is unmatched by design, so this is a tool-dispatch filter, not a boundary; it also
  skips without a `.venv` and arms only when Edit/Write touched a `.py`. Run the gate
  yourself when working outside those conditions.
- **Never commit real household data**: `agent_home/workspace/*` (except the tracked
  `.gitkeep`), `agent_home/AGENTS.md`, `agent_home/conversation_history/`,
  `agent_home/large_tool_results/`. Ignore patterns must be spelled with the full
  `agent_home/` prefix — a pattern containing a slash is anchored to the directory holding
  `.gitignore`, so a bare `workspace/*` silently matches nothing here.

## Known limits

`test_withdrawal_plan` is a single deterministic path, not Monte Carlo — it ignores
sequence-of-returns risk. `get_fund_profile` relays yfinance's inconsistently scaled
expense ratio. US-centric throughout. Single-user by design: `FilesystemBackend` is
unsafe in a shared server; multi-user needs a per-user-namespaced `StoreBackend`.
