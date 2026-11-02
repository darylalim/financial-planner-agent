# Plan: make "the model never does arithmetic" enforced, not just prompted

Drafted 2026-11-01. `CLAUDE.md`'s first invariant is enforced only by `SYSTEM_PROMPT`
("# The arithmetic rule"). `scripts/answer_checks.py` measures it on live answers, but
only reports. This plan removes the arithmetic the tools still leave to the model, then
turns the measurement into a check that fails a run.

## Start here

Each phase is implemented in its own session, which starts without the planning
context. This block is all that session needs to get going.

**Kickoff message** (paste into each new session):
> Read `CLAUDE.md`, then the "Start here" section of `docs/plans/no-model-arithmetic.md`.
> Implement the next phase whose status is not `merged`, and only that phase. Ask before
> any paid run or public change. Update the status table before you finish.

### Status

| Phase | Goal | Depends on | Version | Paid runs (est. model time) | Status |
|---|---|---|---|---|---|
| 1 | Tools return the figures the model was computing; classify what remains | — | 0.6.0 | ~20 min: a 3-run baseline, a `fees` shakedown, a 3-run after-fix batch | PR #13 |
| 2 | An unsourced figure fails the live check | 1 | 0.7.0 (decisions 8 and 10 change tools) | ~15 min: a shakedown, one 3-run confirmation batch, one rerun if needed | not started |

Status values: `not started` / `in progress (<branch>)` / `PR #<n>` / `merged`. Set
`PR #<n>` in the PR's own commits, not with a push to `main`. Issues and PRs share one
number sequence, so the next number is the highest from
`gh api 'repos/{owner}/{repo}/issues?state=all&per_page=1' --jq '.[0].number'` plus one.

Estimates come from Phase 1 of the previous plan: one 3-run × 5-scenario batch on Opus 5.5
took 409 s and 1.80M input tokens.

### Standing rules

- One phase per session and per PR, branched from `main` after the previous phase
  merges.
- The gate, version bumps and paid runs follow `CLAUDE.md` → Commands. Don't restate
  them here.
- Never edit `.claude/settings.json` or `.claude/hooks/*`; ask the owner.
- Facts below were checked on 2026-11-01. Each phase re-checks, against the installed
  versions, any fact it builds on before relying on it.
- **Read an unsourced figure before writing a rule for it.** The precedent is
  `19fa7b1`: the debt "violations" were the tool omitting the totals people ask for,
  not the model misbehaving. A source the checker accepts is a hole in the check, so
  every one added needs a recorded example of the figure it legitimately covers.

### Done when (every phase)

Gate green on 3.11 and 3.14 · the phase's paid runs done with the owner's go-ahead ·
saved conversations still resume, since Phase 1 changes the tool set (see `CLAUDE.md` on
the checkpoint database; there were 0 real threads on 2026-11-01, so a throwaway-home
resume is the check unless that has changed) · docs updated · version bumped as the
PR's last commit when the app changes · status table updated in the same PR.

## Where things stand (2026-11-01)

- **The measurement.** `answer_checks.unsourced_figures` counts every `$` and `%` figure
  in an answer that no source matches at the displayed precision. Sources are that
  turn's tool outputs (any agent, collected by `live_check.ToolRecorder`) and **that
  turn's** user message only. `live_check.py` prints the figures and sums them per
  scenario, but never fails on them.
- **Last numbers.** The Opus 5 vs Opus 5.5 comparison (3 runs × 5 scenarios) counted 27
  vs 24 unsourced figures. `19fa7b1` then removed one cause (debt went from 5 to 0), and
  `_reset_home` now gives every run a fresh home, which removed the `/AGENTS.md`
  carry-over the Phase 0 calibration saw. No full batch has been run since both changes,
  so the current count is unknown.
- **Known classes still open** (from the Phase 0 calibration): figures restated from an
  **earlier turn** (multi-turn `retirement`); the **prompt's own assumptions** (7%
  equity, 4% bonds, 2.5% inflation in `SYSTEM_PROMPT`'s defaults); and **tool inputs**,
  values the model chose and passed into a tool (`ToolCall.args` records them; the
  checker ignores them). The last class is the dangerous one: `$3,275,000` was a target
  the model computed and then passed to `test_withdrawal_plan`.
- **Arithmetic a tool hands to the model:** `get_fund_profile` returns
  `expense_ratio_raw` and a note giving its scale, and its docstring tells the model to
  convert it and "translate it into dollars". A misread scale is a 100× error, and the
  dollar figure has no tool behind it.
- **The expense-ratio scales are fixed per field.** Checked on 2026-11-01 for VTI, VOO,
  SPY, QQQ, ARKK, FXAIX, VFIAX and AGTHX: `netExpenseRatio` is always a percentage (VTI
  `0.03`), `annualReportExpenseRatio` always a fraction (FXAIX `0.00015`), and where a
  fund has both they agree. So normalising is safe. Re-check on the installed yfinance.
- **No live scenario calls `get_fund_profile`.**

## Decisions

1. **`get_fund_profile` returns `expense_ratio_percent`,** computed in the tool from the
   source field's known scale. It keeps `expense_ratio_raw` and `expense_ratio_source`
   for audit, and keeps the factsheet caveat. The docstring stops asking the model to
   convert anything.
2. **Fee cost in dollars comes from a new calculator,** not from two `project_savings`
   calls, because subtracting their results would be model arithmetic. A pure function
   in `finance.py` with a tool in `tools/calculators.py`:
   - inputs: starting balance, monthly contribution, years, gross annual return, and
     two expense ratios;
   - returns: both ending balances, the difference, and total fees paid under each;
   - the fee comes off the return before the geometric monthly conversion
     (`finance.monthly_rate`), and the result says so as an assumption.

   Per `CLAUDE.md`, a new kind of figure needs the arithmetic rule extended: add fee
   comparisons to "# The arithmetic rule", naming the tool.
3. **Legitimate sources the checker will accept,** each added only with a recorded
   example (standing rule above):
   - **earlier turns of the same conversation:** their user messages and tool outputs;
   - **the prompt's stated defaults,** read from where `SYSTEM_PROMPT` defines them
     rather than copied. If they are only prose, Phase 1 lifts them into constants the
     prompt and the checker share, the way `PDF_TOTALS_CAVEATS` is shared.
4. **Tool inputs are not a source.** A figure that appears only as a tool input is
   reported in its own category, `unsourced (tool input)`, and still counts. Accepting
   inputs would launder exactly the `$3,275,000` case. Phase 1 may revise this only with
   run evidence recorded here, and the owner's agreement.
5. **Phase 2 fails a run on any unsourced figure: no threshold.** Its exit criterion is
   one clean 3-run batch over every scenario. If that can't be reached, stop and ask the
   owner rather than adding a tolerance. A threshold would hide exactly the kind of drift
   the check exists to catch.
6. **`streaming.py` is not touched.** Everything the checker needs is already collected
   by the harness's callbacks.
7. **Totals of figures no calculator produces come from a sum tool** (decided
   2026-11-02, after the baseline's `$35,750`: two search-sourced limits the model
   added). The tool takes labelled amounts and returns them with their total, so the
   arithmetic rule can name it. Rejected: a prompt rule alone, which is what this plan
   replaces.
8. **A recalled figure still fails; fix where it comes from** (decided 2026-11-02, after
   the after-fix batch's `5.75%` sales load and "20–30%" swings). A deterministic check
   cannot tell a recalled figure from a computed one, so a "recalled" class would be an
   exemption list, and decision 5 stands. Each recalled figure gets a source instead:
   - a fund's sales load: `get_fund_profile` returns it if the installed yfinance
     exposes it (check first); if not, the prompt's conditional rule extends to "If you
     state a fund's fee, load or limit that no tool gave you, look it up with
     `search_web`";
   - a range read off a volatility: `get_historical_return` returns it in plain terms,
     or the model gives none.

   Rejected: reporting recalled figures as their own class, which only an LLM judge
   could separate (see Rejected, below).
9. **A dropped connection is its own outcome, never clean** (decided 2026-11-02, after a
   `Connection reset by peer` killed the after-fix batch and lost its results).
   `live_check` catches transport errors per run and retries once on a fresh home. A
   run that still fails is *not run*: it keeps the results already collected, prints
   the summary, and makes the batch exit `2` ("cannot run", the code `main()` already
   uses), not `1` or `0`. This is about infrastructure, so it is not the tolerance
   decision 5 rules out.
10. **Estimates are returned in whole dollars; amounts that must match a statement keep
    cents** (decided 2026-11-02, after `820949.52` was shown as `$820,949` in 3 of 3
    runs). Projection and comparison balances (`project_savings`, `compare_fund_fees`,
    `test_withdrawal_plan`) round to the dollar: cents on a 25-year estimate are false
    precision, and with none there is nothing to truncate. Loan payments, debt interest
    and spending totals keep cents, since `$1,798.65` has to match the user's
    statement. Rejected: accepting truncation in the checker, which would let every
    figure match two values; and a prompt rule alone.

Rejected: an LLM judge for "is this figure derived". It is non-deterministic, costs
money on every run, and the deterministic check has been good enough to find the real
causes so far.

## Phase 1: tools return the figures the model was computing

**Goal:** remove the arithmetic the tools leave to the model, and know what the
remaining unsourced figures are.

### Steps

1. **Baseline.** Ask the owner, then run a 3-run batch of every non-PDF scenario on the
   current code. Record each unsourced figure with its scenario, the answer sentence it
   appears in, and a classification: *model arithmetic*, *earlier turn*, *prompt
   default*, *tool input*, or *other*. Put the table in this plan under Phase 1.
2. **Expense ratio** (decision 1), with offline tests for both fields' scales and a
   missing value.
3. **Fee calculator** (decision 2): the `finance.py` function and its tests, the tool,
   its `TOOL_LABELS` entry in `app.py`, the prompt extension, and the README's tool
   table.
4. **A `fees` scenario** in `live_check.SCENARIOS`, with `EXPECTED_TOOLS` naming
   `get_fund_profile` and the new calculator. For example: "I hold $80,000 in AGTHX and
   add $500 a month. What is its fee costing me over 25 years compared with VTI?" Do one
   cheap shakedown run of it before any batch.
5. **Fix whatever else step 1 classified as model arithmetic** the same way: the tool
   returns the figure. One commit per cause, each naming the figure it removes.
6. **After-fix batch:** 3 runs, every non-PDF scenario plus `fees`. Record the counts
   beside the baseline. The *model arithmetic* class should be empty, or each remaining
   entry explained.

**Check:** the after-fix table, in the PR description and here.

### Baseline (2026-11-01, Opus 5.5, 3 runs, `6b3bf49`)

15 unsourced figures. Each row is one figure, with how many runs showed it.

| Scenario | Figure | Runs | Sentence (abridged) | Class |
|---|---|---|---|---|
| search | $35,750 | 2 of 3 | "catch-up is $11,250 … That brings your total to $35,750" | **model arithmetic** (24,500 + 11,250; the snippets give the parts only) |
| market | 7% | 3 of 3 (4 figures) | "I'll keep using a 7% nominal return … (your current assumption)" | prompt default (equity return) |
| retirement | 2.5% | 3 of 3, both turns (6) | "Assumptions: … 2.5% inflation" | prompt default (inflation; also a tool input, and `project_savings` does not echo it) |
| retirement | $1,900 | 3 of 3, turn 2 (3) | "Assumptions: same as before … a flat $1,900/month" | earlier turn (turn 1's user message) |

Seen in the baseline but not counted:
- **Coincidental match.** market: "never go above 10%", the prompt's cap, was accepted
  because the question says "last 10 years". Any number in the user message sources any
  percentage of that value. Phase 2 should close this when it adds the prompt defaults
  as a source.
- **Outside the checker's scope.** retirement: "a failure about nine years before 95"
  (95 − 86) is model arithmetic, but the checker counts only `$` and `%` figures.
- **Not arithmetic.** debt runs 2 and 3 failed on `tools returned errors: ['edit_file']`
  (the agent's `/AGENTS.md` update). Three `edit_file` calls ran, but only the two
  successes were recorded, so the log does not show the failure's text.

### After-fix batch (2026-11-02, Opus 5.5, 3 runs, `626d742`)

| Scenario | Baseline | After | Change |
|---|---|---|---|
| budget | 0 | 0 | — |
| debt | 0 | 1 | `$630` (see below) |
| market | 4 | 6 | prompt defaults, plus two recalled figures |
| search | 2 | **0** | `add_amounts` ran in 3 of 3 |
| retirement | 9 | 20 | earlier-turn restatements (13 of the 20) |
| fees | — | 3 | new scenario |

30 figures, by class:

| Class | Count | Figures |
|---|---|---|
| **model arithmetic** | 2 | debt `$630` (card's $210 minimum + $420 extra, rolled to the car loan); fees "the other ~$41,000" (balance gap − fee gap). Both fixed after the batch, in `9ff1676` and `b393c7d`: the tools now return the figure. |
| prompt default | 10 | market `7%` (4), retirement `2.5%` (6) |
| earlier turn | 13 | retirement turn 2 restating turn 1's tool outputs: `$1.63M`, `$1.13M`, `$1,625,950`, `$1,126,469`, `4.8%`, `$1,654`, `$1,900`; and `$1,392,560` / `$1,010,100`, turn 1's `yearly_balances` row for year 21 (age 62) |
| tool input | 1 | retirement `$3,500/month`, a contribution the model tried (probable: `live_check` does not print tool arguments) |
| other | 4 | fees `5.75%`, AGTHX's sales charge from the model's own knowledge; market "swings of 20–30%" from an 18% volatility (2); fees `$820,949`, the tool's `820949.52` **truncated** rather than rounded |

The batch ran in two parts. A `Connection reset by peer` during the first debt run
killed the process, and with it every result already collected: `run_scenario` has no
per-run error handling. budget's three runs had finished and are kept.

Three questions this batch raised for Phase 2 (recalled facts, a dropped connection,
truncation) are settled as decisions 8–10. Truncation turned out to be consistent, not
noise: the confirmation runs below truncated `820949.52` to `$820,949` in 3 of 3.

**Confirmation** (2026-11-02, 3 runs each of debt and fees, `dab4655`): debt 0 unsourced
in 3 of 3, stating `$630` and `$970` from `monthly_payment_before_payoff`; fees 1 in each
run, the truncated `$820,949`, with "$41,049 in lost growth" now from the tool. The *model
arithmetic* class is empty.

## Phase 2: an unsourced figure fails the live check

**Goal:** a model or prompt change that breaks the arithmetic rule turns the live check
red.

### Steps

1. **A dropped connection is its own outcome** (decision 9), with an offline test that a
   transport error in one run neither ends the batch nor counts as clean. First, so no
   later batch is lost to one.
2. **Teach `answer_checks.py` the sources in decision 3,** each with offline tests built
   from Phase 1's recorded examples, plus a test that a tool-input-only figure stays
   unsourced (decision 4). Close the coincidental match recorded under the baseline (a
   bare `10` in the question sourcing a `10%`) in the same change.
3. **Give recalled figures a source** (decision 8): check yfinance for a sales-load
   field, then the tool or the prompt rule; and the volatility range.
4. **Whole dollars for estimates** (decision 10), with tests that the debt and loan
   figures keep their cents.
5. **Make an unsourced figure fail the run** in `live_check.run_scenario`, the way a
   missing tool does. Report the tool-input category separately in the summary table.
6. **README → The live check:** say what counts as sourced, that an unsourced figure
   fails, and that a run lost to the network is reported as not run.
7. **Confirmation batch:** 3 runs over every scenario, PDFs included, since they check
   figures too. Clean means done. Anything else follows decision 5.

**Check:** a clean 3-run batch, its table in the PR description.

## When the plan is finished

Once Phase 2 merges, delete this file in its own small PR, after moving anything still
true and useful to README (for users) or `CLAUDE.md` (for invariants and process). Git
history keeps the rest.
