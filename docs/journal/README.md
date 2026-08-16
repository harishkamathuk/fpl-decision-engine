# Project Journal

This directory is the project's lightweight engineering journal: a chronological record of the context that is easy to lose between sessions.

It is intentionally **less formal than an ADR, issue or RAID log**. The aim is to preserve useful memory: what changed, why a choice felt right at the time, what we were worried about, ideas worth revisiting, and where we expected to go next.

## What belongs here

Useful journal material includes:

- key decisions and the reasoning behind them;
- meaningful implementation progress;
- discoveries that changed the plan;
- risks, uncertainties and unresolved questions;
- approaches considered and deliberately deferred or rejected;
- useful technical or product musings that may matter later;
- links to relevant issues and pull requests;
- the intended next step at the end of a working session.

The journal should **not** duplicate issue acceptance criteria, ADRs, commit logs or detailed implementation documentation.

## Relationship to other project records

- **GitHub Issues** track work to be done and definitions of done.
- **ADRs** record durable architectural decisions that the codebase should follow.
- **Pull requests and commits** record exactly what changed in the repository.
- **The journal** records the surrounding context: why, what we learned, what remains uncertain, and what we were thinking at the time.

If the journal conflicts with a later ADR or implemented code, the ADR/code wins. The journal is historical context, not normative specification.

## Convention

Use one Markdown file per substantive working day:

```text
docs/journal/
  README.md
  2026-08-14.md
  2026-08-15.md
  ...
```

Entries do not need a rigid template. A useful default is:

```markdown
# YYYY-MM-DD

## Context

## What changed

## Decisions

## Risks / open questions

## Musings

## Next
```

Only use headings that help that day's notes.

### Writing rules

1. **Keep it candid and concise.** These are engineering field notes, not polished reports.
2. **Capture reasoning, not just outcomes.** "Chose X because..." is more useful than "Implemented X".
3. **Preserve uncertainty.** Record unresolved questions instead of retrospectively making decisions look inevitable.
4. **Prefer links over duplication.** Reference `#issue` / `PR #number` where the detail already exists elsewhere.
5. **Do not rewrite history for neatness.** Later factual corrections are fine, but preserve the original context and note material changes in a later entry.
6. **No secrets or private operational data.** Never record credentials, cookies, personal manager data, private league data or unredistributable source payloads.
7. **Journal substantive sessions.** Before finishing a meaningful work session, add enough notes that someone returning weeks later can recover the thread without rereading every issue and PR.

Coding agents working on the repository should follow the same convention. A journal update may travel in the same issue PR as the work it describes; there is no need to create a separate process or PR solely to satisfy the journal.

### Standalone journal entries

A journal-only change does not require a GitHub issue. Create a short-lived branch from `develop`
named `journal/<YYYY-MM-DD>-<short-description>`. Eligibility depends on both that name and the
complete pull-request diff: every changed path must remain under `docs/journal/`. The pull request
still targets `develop`, runs normal CI and uses the repository's normal merge-commit policy.

This exception exists for recording context, reflection and observations without process overhead.
If the work grows beyond `docs/journal/`, move it to an issue-numbered branch. Code, architecture,
dependency, workflow and policy changes do not qualify. Keep journal notes that accompany substantive
work on the relevant issue branch, and create a separate issue for actionable work discovered while
journalling.

## Why this exists

Technical repositories usually preserve *what* changed very well and *why* surprisingly badly. This journal is a low-cost memory layer intended to reduce rediscovery, repeated debates and accidental reversals of decisions whose rationale has otherwise disappeared.
