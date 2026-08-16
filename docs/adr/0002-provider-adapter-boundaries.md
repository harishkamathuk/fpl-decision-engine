# ADR-0002: Provider and adapter boundaries

- **Status:** Accepted
- **Date:** 2026-08-14

## Context

Player, fixture, projection, manager, league and news data may come from multiple sources.
Optimisation may likewise be implemented internally or delegated to an external project. External
schemas, transport libraries and solver APIs must not become part of the decision engine's core
model.

Providers also differ materially in what they can supply. For example, one projection source may
provide only expected points while another also exposes expected minutes, start probability or a
point distribution.

## Decision

The core project owns canonical contracts in `fpl_decision_engine.ports`. External data sources and
open-source projects implement those contracts through adapters.

The dependency direction is one-way:

```text
external source / OSS project
          |
        adapter
          |
         port
          |
canonical domain / application
```

External models are translated at the adapter boundary. Canonical domain types never adopt an
external provider's schema merely because it is convenient.

### Provider identity and capabilities

Every provider exposes a `ProviderDescriptor` containing a stable provider ID, display name,
version and an explicit set of `ProviderCapability` values.

Consumers must check capabilities rather than infer them from optional fields. Capabilities include
core FPL data, manager/league state, projections, expected minutes, start probability, point
distributions, xG/xA and news evidence.

### Provenance and freshness

Provider calls return a `ProviderResponse[T]` envelope containing:

- the canonical payload;
- `ProviderProvenance`, including provider/version, retrieval timestamp and optional source or
  snapshot identifiers;
- `Freshness`, recording the time represented by the data and an optional staleness threshold.

Timestamps at this boundary must be timezone-aware. Staleness is deterministic and evaluated
against an explicit clock value supplied by the caller.

### Failure semantics

Adapters translate implementation-specific failures into the `ProviderError` hierarchy. The error
code distinguishes unavailable, authentication, rate limiting, invalid data, mapping failures and
unsupported capabilities. Errors explicitly state whether retrying can be sensible.

Adapters must not silently return partial or fabricated data after a provider failure.

### Evidence and optimisation ports

News/evidence is intentionally generic at this stage. Issue #10 will define the canonical evidence
model; the generic port allows that model to be introduced without changing provider orchestration.

The optimisation port is also generic over request and result types. Issue #6 will define the first
concrete optimisation request/result models while preserving a replaceable solver boundary.

### External project policy

Prefer, in order:

1. normal dependency when an upstream package can be used unchanged;
2. adapter around an external API or command;
3. upstream contribution when a small change is required;
4. fork only when sustained internal changes are unavoidable;
5. reimplementation when only the algorithm is useful or licensing/architecture makes direct reuse
   unsuitable.

## Consequences

- Application code can switch providers through configuration without importing provider-specific
  types.
- Optional provider features are negotiated explicitly.
- Every provider response carries enough provenance/freshness metadata for later reproducibility.
- Provider failures can be handled consistently by CLI, scheduled jobs and future agents.
- Projection providers, evidence providers and optimisation engines can be compared or replaced
  without changing the canonical domain model.
- Contract-test helpers provide a reusable baseline for concrete adapters introduced in later issues.
