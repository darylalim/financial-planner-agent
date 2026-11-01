# Plan: <what this delivers, in one line>

Drafted <YYYY-MM-DD>. For work spanning several sessions; a change that fits in one
session is a GitHub issue instead (see `CLAUDE.md` → Conventions → Planning). Copy this
file to `docs/plans/<short-name>.md` and fill it in.

## Start here

Each phase is implemented in its own session, which starts without the planning
context. This block is all that session needs to get going.

**Kickoff message** (paste into each new session):
> Read `CLAUDE.md`, then the "Start here" section of `docs/plans/<short-name>.md`.
> Implement the next phase whose status is not `merged`, and only that phase. Ask before
> any paid run or public change. Update the status table before you finish.

### Status

| Phase | Goal | Depends on | Version | Paid runs (est. model time) | Status |
|---|---|---|---|---|---|
| 1 | | — | | none | not started |

Status values: `not started` / `in progress (<branch>)` / `PR #<n>` / `merged`. Set
`PR #<n>` in the PR's own commits, not with a push to `main`. Issues and PRs share one
number sequence, so the next number is the highest from
`gh api 'repos/{owner}/{repo}/issues?state=all&per_page=1' --jq '.[0].number'` plus one.

### Standing rules

- One phase per session and per PR, branched from `main` after the previous phase
  merges.
- The gate, version bumps and paid runs follow `CLAUDE.md` → Commands. Don't restate
  them here.
- Never edit `.claude/settings.json` or `.claude/hooks/*`; ask the owner.
- Facts below were checked on the draft date. Each phase re-checks, against the installed
  versions, any fact it builds on before relying on it.

### Done when (every phase)

Gate green on 3.11 and 3.14 · the phase's live or manual check done (paid runs with the
owner's go-ahead) · saved conversations still resume, if the phase changes the model,
the tool set, the libraries or the checkpoint format (see `CLAUDE.md` on the checkpoint
database) · docs updated · version bumped as the PR's last commit · status table updated
in the same PR.

## Decisions

Settled before implementation, with the reason for each and what was rejected. A phase
that finds one no longer holds stops and asks the owner rather than adapting on the fly.

## Phase 1: <goal>

**Goal:** one or two sentences.

### Design

What changes and why. Point at code (`module.function`) rather than describing it.

### Steps

1. …

**Check:** what proves the phase works, beyond the gate. Name any paid runs here.

## When the plan is finished

Once the last phase merges, delete this file in its own small PR, after moving anything
still true and useful to README (for users) or `CLAUDE.md` (for invariants and process).
Git history keeps the rest.
