# ADR-0002: Provider and adapter boundaries

- **Status:** Accepted
- **Date:** 2026-08-14

## Context

Player, fixture, projection, league and news data may come from multiple sources. Optimisation may likewise be implemented internally or delegated to an external project.

## Decision

The core project owns canonical contracts. External data sources and open-source projects implement those contracts through adapters.

Provider capabilities must be explicit because not every source supplies the same fields. Raw provider schemas must not leak beyond the adapter boundary.

Prefer, in order:

1. normal dependency when an upstream package can be used unchanged;
2. adapter around an external API or command;
3. upstream contribution when a small change is required;
4. fork only when sustained internal changes are unavoidable;
5. reimplementation when only the algorithm is useful or licensing/architecture makes direct reuse unsuitable.

## Consequences

The project can combine and compare projection providers, introduce ensembles, replace solvers and incorporate forks later without changing application-facing contracts.
