# ADR-0001: Local-first modular architecture

- **Status:** Accepted
- **Date:** 2026-08-14

## Context

The project must support data ingestion, forecasting, optimisation, simulation, rival analysis and agent interfaces without coupling the core decision logic to any one data source, UI, database or LLM.

## Decision

Use a local-first modular monolith with inward-pointing dependencies:

1. `domain` owns canonical business concepts.
2. `ports` defines provider and engine contracts.
3. `application` orchestrates use cases.
4. `infrastructure` implements storage and external integrations.
5. CLI, web and agent interfaces are clients of application use cases.

Forecasting, strategy, optimisation and explanation remain separate concerns. External repositories are consumed behind adapters rather than becoming the project's domain model.

Initial persistence is DuckDB plus Parquet. The project remains container-capable but does not require Docker for development.

## Consequences

- External projection or optimisation projects can be replaced without restructuring the application.
- Local development remains lightweight.
- More adapter code is required at system boundaries, but this cost is intentional.
