---
name: retirement-readiness
description: Run a full retirement readiness assessment - projecting a portfolio to a target retirement age, testing whether the drawdown is sustainable, simulating how likely it is to last, and quantifying any gap. Use when the user asks whether they can retire, when they can retire, whether they are on track, or how much they need.
---

# Retirement readiness assessment

## Overview

A readiness assessment answers one question — *does the projected balance
support the intended spending?* — and it takes two separate tool calls to
answer, not one. Accumulation and drawdown are different calculations, and
skipping the second is the most common way this analysis goes wrong. A third
call then gives the drawdown a probability, because one smooth path cannot show
what bad years early in retirement do.

## When to use

"Can I retire at 60", "am I on track", "how much do I need", "will my savings
last", or any request to review a retirement plan.

## Instructions

### 1. Gather inputs

Read `/AGENTS.md` first. Only ask for what is genuinely missing:

- Current age and target retirement age
- Current retirement balances, and the as-of date
- Current monthly contribution, including any employer match
- Expected annual spending in retirement, in today's dollars
- Other expected income: Social Security, pension, rental

If retirement spending is unknown, derive it from actual outgoings with
`summarize_spending` rather than applying a replacement-ratio rule of thumb.
A rule of thumb applied to an unknown baseline compounds two guesses.

### 2. Project accumulation

Call `project_savings` with years = target age − current age.

Run it **twice**, at 7% and at 5%. The spread between the two is the honest
answer about uncertainty, and showing it prevents a plan that only survives at
the optimistic rate.

Employer match is part of the monthly contribution — a 50%-of-6% match on a
$100k salary is $250/month and materially changes the result.

### 3. Test the drawdown

Take `final_balance_nominal` from step 2 into `test_withdrawal_plan`:

- `monthly_withdrawal` or `annual_withdrawal` = retirement spending − Social
  Security and other income, in the unit the user gave it. Never convert
  monthly to annual yourself; the tool takes either and returns both
- Inflate today's-dollar spending to the retirement year first, or use
  `final_balance_real` and today's spending. Never mix a nominal balance with
  today's-dollar spending; that error overstates readiness by decades of
  inflation.
- `annual_return` 0.05, the balanced return from your assumptions, on every
  drawdown, the 7% projection's included: 7% is a return for the saving years.
  If the user named their own retirement return, use that. Either way, say
  which return the drawdown used
- `years` = 95 − retirement age, so the plan survives a long life
- `retirement_age`, so the answer comes back as ages. If the money runs out,
  `depleted_by_age` is the age to quote; `yearly_path` carries an `age` on
  every row. Never add years to an age yourself

### 4. Simulate the drawdown

Call `simulate_withdrawal_plan` **once**, not once per projection, with the
portfolio, withdrawal and `years` from the step-3 test on the 5% projection.
Pass `retirement_age` so depletion comes back as ages.

Unless the user named their holdings, use the balanced pair from your
assumptions: `annual_return` 0.05 and `annual_volatility` 0.11 (5% and 11%),
and say so. If they did name a holding, take `annual_volatility` from
`get_historical_return` on it.

The figures to quote, each from the field named:

- `success_probability`, written as a percentage, as "the money lasted to
  <`horizon_end_age`> in <`success_probability`> of simulated markets" --
  never as a chance that the person will be fine. If you give the chance of
  running out, it is `failure_probability`, not 100 minus the success figure
- `worst_10_percent_depleted_by_age`, if it is not null, as the age by which
  the money ran out in the worst tenth of markets
- The `ending_balance_real` median, if you give a typical ending balance

Relay the `caveat` it returns: this is a model, not a forecast, and running out
in a simulated market means spending would have had to be cut, not that
nothing is left.

### 5. Quantify any gap

If the drawdown fails, do not stop at "you are short". Call
`required_savings_rate` with the user's current contribution as
`current_monthly_contribution`: it returns the total needed each month and
`increase_over_current`, the additional amount. Then present the levers in
order of impact — typically: retire later, save more,
spend less. Give the number attached to each.

### 6. Report

Lead with the verdict in one sentence. Then:

- Both projections (7% and 5%), nominal and real
- Whether the drawdown survives, and the initial withdrawal rate
- The probability from step 4, worded as step 4 says, with its volatility
- Every assumption used

Write the full analysis to `/workspace/retirement-readiness-<YYYY-MM-DD>.md`
and update `/AGENTS.md` with the goal, target date, and current trajectory.

## Do not

- Do not present a single point estimate as the answer. The rate assumption
  dominates the output, so one number implies a precision that does not exist.
- Do not skip step 3. A large balance is not a sustainable plan.
- Do not simulate both projections. Two probabilities beside the 7%/5% spread
  bury the verdict; the 5% one is the conservative test.
- Do not recommend specific funds, allocations, or account types. Explain the
  tradeoff and point to a fee-only fiduciary for the decision.
