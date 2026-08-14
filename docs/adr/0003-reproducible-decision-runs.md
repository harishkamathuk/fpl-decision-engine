# ADR-0003: Reproducible decision runs

- **Status:** Accepted
- **Date:** 2026-08-14

## Context

A recommendation is only meaningful if it can later be reconstructed from the information available when it was made. FPL data, projections, news and manager state change continuously.

## Decision

Every decision run will eventually record at least:

- run identifier and timestamp;
- gameweek and planning horizon;
- source-data snapshot identifiers;
- manager-state snapshot;
- projection provider and model version;
- optimiser and configuration;
- code commit;
- random seed for stochastic processes;
- generated recommendation and alternatives.

Raw inputs are immutable. Curated and derived datasets retain provenance back to their source snapshots.

## Consequences

Historical backtesting can avoid look-ahead bias, model changes can be compared fairly, and recommendations can be audited rather than reconstructed from memory.
