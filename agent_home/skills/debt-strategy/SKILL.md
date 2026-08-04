---
name: debt-strategy
description: Compare debt payoff strategies (avalanche vs snowball), build a payoff timeline, and decide whether to prioritise debt or investing. Use when the user has multiple debts, asks which to pay first, when they will be debt-free, or whether to pay down debt before investing.
---

# Debt payoff strategy

## Overview

Two questions usually arrive together and need separating: *which debt first*
(avalanche vs snowball) and *debt versus investing*. The first has a
mathematical answer; the second depends on the rate spread and on the user's
own preferences. Answer them in that order.

## When to use

The user has more than one debt, asks which to pay first, wants a debt-free
date, or asks whether to pay down debt before investing.

## Instructions

### 1. Inventory every debt

For each: name, current balance, APR, minimum payment. Read `/AGENTS.md` first.

APR must be a decimal — 22% is `0.22`. This is the single most common input
error here and it silently produces a wildly optimistic timeline. If a user
says "22", confirm they mean 22%, not 2200%.

Then establish the total monthly amount available for debt. It must be at least
the sum of minimums; `plan_debt_payoff` errors if it is not, which is the
correct outcome — a plan that cannot cover minimums needs a conversation about
hardship programs or renegotiation, not a projection.

### 2. Run both strategies

Call `plan_debt_payoff` twice, once with `"avalanche"` and once with
`"snowball"`. Compare `months_to_debt_free` and `total_interest_paid`.

Present the real tradeoff without editorialising: avalanche always costs less
interest — that is arithmetic, not opinion — while snowball clears individual
debts sooner, and for many people the visible early wins are what makes the
plan survive contact with a bad month. If the interest difference is small
relative to the balances, say so; a few hundred dollars is a fair price for a
plan someone actually follows.

Give the number, then let them choose.

### 3. Debt versus investing

Compare each debt's APR against a realistic expected return (7% nominal
equities). Use `get_historical_return` if the user wants that grounded.

- **APR clearly above the expected return** (credit cards, most personal loans):
  paying down is a guaranteed return at the APR, and beats an uncertain 7%.
- **APR clearly below** (many mortgages, subsidised student loans): investing
  has the higher expected value, with the caveat that it is expected, not
  guaranteed.
- **APR near the expected return:** genuinely close. Say so rather than
  manufacturing a recommendation.

Two things override the arithmetic, and both should be raised before the
comparison rather than after:

1. **Employer match first.** An unmatched 401(k) contribution is an immediate
   50–100% return; it beats paying down almost any debt.
2. **Emergency fund.** Without a buffer, an unexpected expense goes straight
   back onto the highest-rate card, which unwinds the plan.

### 4. Report

Lead with the debt-free date under the recommended strategy, and the interest
difference between the two. Then the payoff order with each debt's month.

Write the plan to `/workspace/debt-payoff-plan-<YYYY-MM-DD>.md` and record the
strategy chosen and target debt-free date in `/AGENTS.md`.

## Do not

- Do not recommend debt consolidation, balance transfers, refinancing, or
  settlement as a course of action. Explain how the mechanism works if asked,
  note that terms and fees decide whether it helps, and point to a fee-only
  advisor or a nonprofit credit counsellor.
- Do not moralise about how the debt was accumulated. It is not relevant to the
  payoff arithmetic and it is the fastest way to end the conversation.
