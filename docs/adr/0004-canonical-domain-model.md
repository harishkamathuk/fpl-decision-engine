# ADR 0004: Canonical FPL domain model

- Status: Accepted
- Date: 2026-08-14
- Issue: #1

## Context

The engine will consume data and models from several providers. If provider schemas leak into the core application, replacing a provider later would force broad refactoring and make historical decision runs difficult to reproduce.

The canonical model also needs to reject invalid FPL states at the boundary rather than silently repairing them.

## Decision

The `domain` package owns immutable Pydantic models and value objects. It has no dependency on HTTP clients, persistence libraries, dataframes, optimisation solvers or LLM SDKs.

Stable internal entity identifiers use UUIDs. Provider-specific identifiers are represented separately as `ExternalRef` values.

Money is stored as integer tenths of a million pounds rather than binary floating-point values. Forecast values remain continuous numeric values and may carry explicit uncertainty fields.

The squad snapshot enforces the standard FPL structure of 15 players: 2 goalkeepers, 5 defenders, 5 midfielders and 3 forwards, with no more than three players from one club. These constraints are domain invariants; season-specific or changeable rules should remain configurable unless they are fundamental to the canonical model.

For 2026/27, official Premier League guidance confirms the two-set chip structure and that up to five free transfers can be rolled. `ManagerState` therefore allows 0–5 free transfers and represents chips explicitly by type, half and status. More detailed chip transition rules belong in the later strategy/optimisation layer rather than this foundational model.

All domain timestamps that represent an actual instant must be timezone-aware.

## Consequences

- External providers must map into our model through adapters.
- Invalid domain states fail explicitly with validation errors.
- Persistence and optimisation remain replaceable infrastructure concerns.
- Any future FPL rule change that affects a canonical invariant must be made deliberately and covered by tests.

## References

- Premier League, “FPL basics explained: How to pick a squad”: https://www.premierleague.com/en/news/2174419/fpl-basics-how-to-pick-a-squad
- Premier League, “All you need to know about changes to FPL for 2026/27”: https://www.premierleague.com/en/news/4679873/all-you-need-to-know-about-changes-to-fpl-for-202627
