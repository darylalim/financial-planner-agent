---
name: budget-from-statements
description: Turn raw bank or credit-card exports in /workspace/ into a categorized budget with monthly averages and a savings-rate figure. Use when the user uploads transaction files, asks where their money goes, wants a budget built, or asks how much they can afford to save.
---

# Building a budget from statement exports

## Overview

Bank exports are inconsistent between institutions — sign conventions flip,
column names vary, and a credit-card export usually shows charges as positive
while a checking export shows them as negative. Getting the sign convention
wrong inverts the entire budget, so this skill front-loads verification.

## When to use

The user has uploaded transaction files, or asks where their money goes, what
they can afford to save, or wants a budget.

## Instructions

### 1. Inventory

`ls /workspace/` to see what is there. If nothing, ask the user to upload a CSV
or XLSX export from their bank.

### 2. Inspect before aggregating

Run `inspect_document` on every file. You need the real column names — guessing
`"Amount"` when the file says `"Transaction Amount"` wastes a call and produces
a confusing error.

**Read the sign convention off the preview rows before aggregating.** Find a row
you can identify as spending — a grocery store, a utility — and check how it is
encoded. There are three layouts:

- Spending negative, income positive → the default. Nothing to pass.
- Spending positive, payments negative → pass
  `sign_convention="positive_outflow"`. Common on card exports.
- Separate debit and credit columns → pass the debit column as `amount_column`
  and the credit column as `inflow_column`.

`summarize_spending` refuses to guess when every amount is positive, so an
unidentified layout comes back as an error rather than a reversed budget. If you
hit that error, go back to the preview rows and pick the right option above.

### 3. Aggregate

Call `summarize_spending` with `amount_column`, plus `category_column` and
`date_column` whenever those exist, and whichever sign option step 2 identified.
The monthly series is what makes the average trustworthy — a single month is not
a budget.

Check the `sign_convention` field in the response against what you saw in the
preview. It reports what was actually applied, so it is the cheapest way to
catch a reversed budget before you write one.

For several accounts, summarize each separately, then combine. Watch for
double counting: a credit-card payment appears as an outflow in checking *and*
the underlying purchases appear in the card export. Count the purchases, not
the payment.

### 4. Build the picture

- Monthly income — the `average_monthly_inflow` field
- Monthly spending by category — `by_category_monthly_average`, largest first
- Savings rate — the `savings_rate` field
- Fixed versus discretionary, when categories permit

Rows with a blank category arrive under `Uncategorized`. If that bucket is large
enough to change the picture, say so — it is unclassified spending, not an
absence of it.

`summarize_spending` returns every one of those figures already computed. Report
what it returns; do not divide its totals yourself. Check `months_covered` and
`period` before calling an average typical, and mention `undated_transactions`
if it appears — those rows are in the totals but not in the averages.

Flag anything that materially affects planning: a category consuming an unusual
share, subscriptions in aggregate, an interest charge implying a revolving
balance.

### 5. Report

Lead with the three numbers that matter: monthly income, monthly spending,
savings rate. Then the category breakdown, largest first. State how many months
of data the average covers — a one-month average is a snapshot, not a trend.

Write the breakdown to `/workspace/budget-<YYYY-MM>.md` and record monthly
income, spending, and savings rate in `/AGENTS.md`.

## Do not

- Do not report a total without having verified the sign convention.
- Do not label discretionary spending as a problem unprompted. Present the
  numbers; the user decides what is worth what.
- Do not copy transaction rows into your reply. Aggregate — individual
  transactions are private detail the user already has.
