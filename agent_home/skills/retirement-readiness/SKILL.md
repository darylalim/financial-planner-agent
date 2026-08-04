---
name: retirement-readiness
description: Run a full retirement readiness assessment - projecting a portfolio to a target retirement age, testing whether the drawdown is sustainable, and quantifying any gap. Use when the user asks whether they can retire, when they can retire, whether they are on track, or how much they need.
---

# Retirement readiness assessment

## Overview

A readiness assessment answers one question — *does the projected balance
support the intended spending?* — and it takes two separate tool calls to
answer, not one. Accumulation and drawdown are different calculations, and
skipping the second is the most common way this analysis goes wrong.

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

- `annual_withdrawal` = retirement spending − Social Security and other income
- Inflate today's-dollar spending to the retirement year first, or use
  `final_balance_real` and today's spending. Never mix a nominal balance with
  today's-dollar spending; that error overstates readiness by decades of
  inflation.
- `years` = 95 − retirement age, so the plan survives a long life

### 4. Quantify any gap

If the drawdown fails, do not stop at "you are short". Call
`required_savings_rate` for the additional monthly contribution needed, then
present the levers in order of impact — typically: retire later, save more,
spend less. Give the number attached to each.

### 5. Report

Lead with the verdict in one sentence. Then:

- Both projections (7% and 5%), nominal and real
- Whether the drawdown survives, and the initial withdrawal rate
- The sequence-of-returns caveat from `test_withdrawal_plan`, in your own words
- Every assumption used

Write the full analysis to `/workspace/retirement-readiness-<YYYY-MM-DD>.md`
and update `/AGENTS.md` with the goal, target date, and current trajectory.

## Do not

- Do not present a single point estimate as the answer. The rate assumption
  dominates the output, so one number implies a precision that does not exist.
- Do not skip step 3. A large balance is not a sustainable plan.
- Do not recommend specific funds, allocations, or account types. Explain the
  tradeoff and point to a fee-only fiduciary for the decision.
