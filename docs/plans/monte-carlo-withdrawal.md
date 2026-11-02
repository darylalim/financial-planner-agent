# Plan: give a retirement drawdown a probability of success, not just one path

Drafted 2026-11-02, for issue #10. `test_withdrawal_plan` (`finance.withdrawal_sustainability`)
walks one path at a constant return, so it cannot see sequence-of-returns risk: bad years
early in retirement can exhaust a portfolio the smooth path calls safe. This plan adds a
seeded Monte Carlo tool beside it and teaches `retirement-readiness` to use both.

## Start here

Each phase is implemented in its own session, which starts without the planning
context. This block is all that session needs to get going.

**Kickoff message** (paste into each new session):
> Read `CLAUDE.md`, then the "Start here" section of `docs/plans/monte-carlo-withdrawal.md`.
> Implement the next phase whose status is not `merged`, and only that phase. Ask before
> any paid run or public change. Update the status table before you finish.

### Status

| Phase | Goal | Depends on | Version | Paid runs (est. model time) | Status |
|---|---|---|---|---|---|
| 1 | A seeded Monte Carlo tool, its prompt rule and its tests | — | 0.8.0 (new tool) | ~4 min: one `retirement` shakedown, one 1-run batch over every scenario | not started |
| 2 | `retirement-readiness` reports the probability; the live check expects the tool | 1 | 0.8.1 (skill change) | ~12 min: a shakedown, `retirement --repeat 3`, one 3-run batch over every scenario | not started |

Status values: `not started` / `in progress (<branch>)` / `PR #<n>` / `merged`. Set
`PR #<n>` in the PR's own commits, not with a push to `main`. Issues and PRs share one
number sequence, so the next number is the highest from
`gh api 'repos/{owner}/{repo}/issues?state=all&per_page=1' --jq '.[0].number'` plus one.

Estimates come from the no-model-arithmetic plan: one 3-run × 5-scenario batch on Opus 5.5
took 409 s, about 27 s per scenario-run. `retirement` has two turns and takes longer.

### Standing rules

- One phase per session and per PR, branched from `main` after the previous phase
  merges.
- The gate, version bumps and paid runs follow `CLAUDE.md` → Commands. Don't restate
  them here.
- Never edit `.claude/settings.json` or `.claude/hooks/*`; ask the owner.
- Facts below were checked on 2026-11-02. Each phase re-checks, against the installed
  versions, any fact it builds on before relying on it.
- **Read an unsourced figure before writing a rule for it** (`CLAUDE.md` → The live
  check). If a probability or percentile shows up unsourced, the first suspect is a field
  the tool should have returned.

### Done when (every phase)

Gate green on 3.11 and 3.14 · the phase's paid runs done with the owner's go-ahead ·
saved conversations still resume, since Phase 1 changes the tool set (throwaway-home
resume, per `CLAUDE.md` on the checkpoint database) · docs updated · version bumped as
the PR's last commit · status table updated in the same PR.

## Where things stand (2026-11-02)

- `finance.withdrawal_sustainability` steps **annually**: `balance * (1 + r) - withdrawal`,
  withdrawal grown by inflation, clamped at zero on depletion, whole-dollar path. Its
  caveat and the tool docstring both say a Monte Carlo is needed for a real probability.
- `finance.py` imports only `math` and `dataclasses`. `numpy` arrives only through
  `pandas`/`yfinance`; it is not a direct dependency.
- `get_historical_return` returns `annualized_volatility` over a 1–25 year window, using
  live yfinance data.
- `prompts.PROMPT_DEFAULT_PERCENTS` holds the 7% / 4% / 2.5% / 10% defaults the checker
  accepts. The prompt states no volatility default.
- `answer_checks.EXPECTED_TOOLS["retirement"]` is `{project_savings, test_withdrawal_plan}`.
- The retirement skill tests the drawdown once per projection (step 3), and relays
  the sequence-of-returns caveat "in your own words" (step 5).

## Decisions

1. **A new tool, `simulate_withdrawal_plan`, next to `test_withdrawal_plan`. No flag on
   the old one.** The deterministic path is still the explainable one (a yearly balance a
   person can follow), and keeping it unchanged keeps its tests and the skill's step 3.
   Rejected: a `simulate=True` flag, which gives one tool two payload shapes and a
   conditional docstring the model has to read correctly every time.
2. **Parametric lognormal returns centred on the user's assumption, not a historical
   bootstrap.** Each year's return is drawn so the median growth factor equals
   `1 + annual_return`, with `annual_volatility` turned into log space in `finance.py`.
   That way the median path is close to `test_withdrawal_plan`'s path, and the
   assumption the user can push back on is still one rate they can see. Rejected:
   bootstrapping from `get_historical_return`'s data. The tool's window is at most 25
   years, which is too few independent annual draws. It would also put the network
   inside a calculator, and it would quietly swap the user's assumption for one fund's
   history.
3. **Inflation stays a fixed rate, as in the deterministic tool.** Rejected: random
   inflation. It adds a second distribution the user never sees, so it can't be pushed
   back on.
4. **The simulation is a pure function in `finance.py`, using the standard library's
   `random.Random(seed)`.** That keeps the module free of dependencies. 10,000 paths ×
   100 years is about 1M draws, well under a second. Rejected: `numpy`, which would
   become a direct dependency of the one module that has none.
5. **The tool always uses a fixed seed.** `finance` takes `seed` as a parameter, and the
   tool passes a module constant. The same inputs then give the same answer in every
   turn, every rerun and every test. A probability that moves from 81% to 79% when the
   question is asked again reads as the agent changing its mind. Rejected: a random
   seed at runtime.
6. **10,000 paths; probabilities rounded to two decimals.** At p = 0.8 the standard error
   is 0.4 points, so a whole percent is honest and a decimal place would be false
   precision. This follows the "estimates are whole dollars" rule (`CLAUDE.md`).
7. **The tool returns every figure an answer needs, so the model computes none.** This
   matters most where the checker can't see a figure:
   - `success_probability` **and** `failure_probability`, both as decimals (the checker
     already treats a decimal as its percentage). This way "a 19% chance of running out"
     is never `1 − 0.81`.
   - Ending balances at the 10th, 50th and 90th percentiles, nominal **and** real. The
     tool does the real conversion, since a balance in today's dollars is the one people
     can judge.
   - Depletion timing: the year by which the worst 10% (and 25%) of paths ran out, or
     null if fewer than that share failed. If an optional `retirement_age` is given, the
     same figures are also returned as ages. The checker sees only `$` and `%`, so an age
     the model works out itself (year + age) would pass unchecked. Returning it is the
     only protection.
   - `paths`, `seed`, `annual_volatility` and the other inputs, echoed back as
     assumptions to state.
   - First-year withdrawal annual and monthly, and the initial withdrawal rate, as
     `test_withdrawal_plan` returns them, so either tool can be quoted alone.
8. **Volatility gets a stated default, held in `prompts.py` constants, and added to
   `PROMPT_DEFAULT_PERCENTS`.** Recommended: 15% for diversified equity and 5% for bonds.
   **The owner confirms the numbers before Phase 1 starts.** If the user names a
   holding, `annual_volatility` comes from `get_historical_return` on that holding. A
   default changes only in `prompts.py`, never in the prompt's prose (`CLAUDE.md`).
9. **One return and one volatility per call. Blending an allocation is out of scope.**
   A 60/40 portfolio's volatility isn't a weighted average, and making the model blend
   it would be model arithmetic. If live runs show the model blending, that becomes its
   own issue (a tool that takes an allocation and a stated correlation). It is not
   added to this plan mid-flight.
10. **The arithmetic rule gets a probability paragraph, worded conditionally**
    (`CLAUDE.md` → Conventions): if you give a chance of success or of running out, it
    is a probability `simulate_withdrawal_plan` returned, quoted as is. Never turn it
    into odds, a complement or "about 1 in 5" yourself.
11. **The skill runs the simulation once, with the same inputs as its step-3 drawdown
    test on the conservative (5%) projection.** It explains the result as "the money
    lasted to 95 in 81% of simulated markets", never as "an 81% chance you'll be fine".
    It says that running out in a simulation means spending would have to be cut, not
    that the person ends up with nothing. And it calls the result a model, not a
    forecast. Rejected: simulating both projections, which doubles the figures in an
    answer that already carries a 7%/5% spread.

Rejected for the whole plan: replacing `test_withdrawal_plan`. A probability alone hides
*when* the money runs out on the expected path, and that path is what the user can check
against their own intuition.

## Phase 1: a seeded Monte Carlo tool, its prompt rule and its tests

**Goal:** `simulate_withdrawal_plan` exists, is bound on the main graph and the `task`
subagent, returns the fields in decision 7, and the prompt covers probabilities.

### Design

- `finance.simulate_withdrawal` sits beside `finance.withdrawal_sustainability`, with the
  same validation (`_check_rate`, `_check_non_negative`, `_MAX_YEARS`) and the same
  exactly-one-of annual/monthly withdrawal rule. Factor out what the two functions share
  instead of copying it. The yearly step must be the same operation as the
  deterministic one.
- The tool goes in `tools/calculators.py` and `CALCULATOR_TOOLS` (so `ALL_TOOLS`), never
  `READER_TOOLS`. The docstring names the fields, says the seed is fixed, and points at
  `test_withdrawal_plan` for the yearly path.
- `test_withdrawal_plan`'s caveat and docstring name the new tool instead of "a Monte
  Carlo simulation is required".
- Volatility default constants are in `prompts.py`; `PROMPT_DEFAULT_PERCENTS` and the
  "# Assumptions" section are built from them. The probability paragraph (decision 10)
  goes into "# The arithmetic rule".

### Steps

1. Ask the owner to confirm the decision 8 defaults.
2. Write the `finance` function test-first. The tests that make it trustworthy:
   - **`annual_volatility=0` reproduces `withdrawal_sustainability`.** The same
     depletion year and ending balance, with probability 1 or 0. This pins the yearly
     step.
   - The same seed gives an identical payload. A different seed moves the probability
     by no more than Monte Carlo error.
   - The median path's compounded return is within tolerance of `annual_return`.
   - Moving the return sequence earlier lowers success. A test with a fixed sequence
     shows sequence risk exists.
   - Rounding follows decisions 6 and 7: whole dollars, two-decimal probabilities, null
     depletion percentiles when too few paths fail.
3. Add the tool and update `TestBoundTools` and
   `TestSubagentInheritsTheNarrowedFilesystem` for the new name.
4. Add the prompt constants and the paragraph. Add a test that pins the paragraph's
   conditional wording, as the existing prompt rules do.
5. Check `TestRecursionLimitBudget` still holds now that `retirement` has one more call
   available.
6. Update README → Known limits: the deterministic tool is still a single path, and the
   new tool's model assumptions (lognormal, fixed inflation, one asset) are its limits.
   Update the CLAUDE.md Known limits line to match.

**Check:** one `retirement` shakedown run, then one 1-run batch over every scenario (~4
min). The skill doesn't call the tool yet, so the batch is for regressions from the
prompt change. A call to the new tool here is a bonus, not a requirement. A throwaway-home
resume of a thread saved before the change.

## Phase 2: the skill reports the probability; the live check expects the tool

**Goal:** a retirement answer gives a probability of success from the tool, explained to a
non-expert, and the live check fails a run that skips it.

### Design

- `retirement-readiness`: a new step between "Test the drawdown" and "Quantify any gap"
  that names `simulate_withdrawal_plan` and the fields it quotes (`CLAUDE.md`: a skill that
  names a figure must name the field). Step 5 replaces "the sequence-of-returns caveat …
  in your own words" with the probability, using decision 11's wording rules.
- `answer_checks.EXPECTED_TOOLS["retirement"]` adds `simulate_withdrawal_plan`.

### Steps

1. Edit the skill. Keep the existing step numbering readable; the skill is procedure,
   not finance knowledge.
2. Update `EXPECTED_TOOLS` and any test that pins the retirement set.
3. Shakedown: one `retirement` run. Read every unsourced figure before changing
   anything (standing rule).
4. `retirement --repeat 3`, then one 3-run batch over every scenario.

**Check:** all three `retirement` runs call the tool and are clean, and the full batch
is clean. If a probability appears unsourced or reworded as odds, look at the tool's
fields first, then the prompt rule. Do not add a checker source for it.

## When the plan is finished

Once the last phase merges, delete this file in its own small PR, after moving anything
still true and useful to README (for users) or `CLAUDE.md` (for invariants and process).
Git history keeps the rest. Close #10 with that PR, and leave #11 open.
