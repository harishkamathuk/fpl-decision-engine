# GW1 decision runbook

> **SUPERSEDED — archived 2026-08-29.**
>
> This manual 26-stage runbook is retained for historical and forensic context only. The
> enforced Touchline control plane is now the supported operating procedure: see
> [`docs/operations/README.md`](../operations/README.md) for the quickstart and
> [`docs/operations/cli-reference.md`](../operations/cli-reference.md) for the current CLI
> surface. Do not follow this document as the primary operating procedure.

This is the supported offline process for the final blank-squad GW1 decision. Run it
from the tagged release candidate, not from an arbitrary later `develop` checkout.
Official FPL and projection files are supplied manually; this process never retrieves
or submits data over the network.

The authoritative Friday run must use the repository-local, Git-ignored `data/` and `state/`
layout, not `/tmp`. Preserve and back up the raw source directory, `state/fpl.duckdb` and every
referenced content-addressed bundle together. `/tmp` is suitable only for disposable rehearsals.

## Evidence and decision workflow

1. Check out the release tag and verify the code revision and clean/dirty state with
   `git rev-parse HEAD` and `git status --short`.
2. Save the exact manually obtained `bootstrap-static.json` and `fixtures.json` in a
   dated local directory outside Git. Do not edit them after acquisition.
3. Save the exact user-supplied projection CSV beside the snapshot evidence. Preserve
   its provider, upstream run/model version and generation timestamp.
4. Record every source file's SHA-256, byte size and local observation time. Treat
   source publication/generation time separately from local observation time. The immutable raw
   snapshot manifest records `observed_at`; the bundle references that snapshot ID/hash rather than
   duplicating its observation timestamp.
5. Ingest the official files with `uv run fpl sync --source snapshot --input <directory>`.
   Record the immutable snapshot ID, detected season, counts, warnings and raw evidence
   location.
6. Use `prepare_snapshot` and `map_snapshot` on that same local snapshot to build the
   canonical player universe; do not introduce another parser.
7. Load projections through `FplForecastCsvAdapter` or the configured local CSV
   provider. Require exact external identity, and record mapped, unmapped, duplicate and
   currently selectable-without-forecast counts.
8. Extract and assess snapshot availability evidence at the decision cutoff. Compare
   evidence timestamps with projection `generated_at` so evidence already known to the
   forecast is not applied twice.
9. Apply only definitive, newer exclusions automatically. Keep doubtful, stale,
   unknown-time and conflicting evidence visible for review without mutating xPts.
10. Build `SingleGameweekOptimisationRequest` from the exact mapped candidates,
    projections and justified exclusions. Expected points remain unconditional.
11. Run the unchanged `HighsSingleGameweekOptimiser`; validate squad quotas, formation,
    budget, club limit, captain, vice-captain and ordered bench.
12. Allocate the DecisionRun UUID, build the recommendation-only v1 decision bundle,
    and write it beneath ignored `state/decision-bundles/`. Record its exact SHA-256.
13. Persist the blank-squad run with `persist_squad_decision_run`, referencing the
    content-addressed bundle and exact input snapshot references.
14. Review forecast age, identity coverage, selectable players without projections,
    availability conflicts, low expected minutes/probabilities, cold starts and other
    assumptions not represented by the mean-only objective.
15. Run explicit force/exclude scenarios only when point-in-time evidence justifies
    them. Preserve each distinct scenario as a separate DecisionRun/bundle; do not
    overwrite the baseline.
16. Select one final model recommendation and retain its run ID and artifact hash.
17. Submit the squad, XI, captain and vice-captain manually in FPL. There is no
    automated transaction path.
18. Record the actual saved choice and timestamp with
    `DecisionBundleV1.record_actual_choice`. If it differs, supply a narrow human reason.
    Write the returned bundle as a new content-addressed artifact; the recommendation
    artifact and recommendation fields remain unchanged.
19. Re-open FPL and verify the saved XI, captain and vice-captain. Retain the release
    tag, source files/hashes, availability cutoff, DecisionRun and both bundle hashes for
    later Issue #12 evaluation.

## Pre-deadline checklist

- [ ] Tagged release checked out; revision and dirty state recorded.
- [ ] Official snapshot and projection bytes preserved outside Git with SHA-256 values.
- [ ] Authoritative `data/` and `state/` evidence is backed up; no final artifact lives only in `/tmp`.
- [ ] Season/GW, projection generation time and decision cutoff are correct and aware.
- [ ] Identity coverage and selectable players without forecasts reviewed.
- [ ] Availability exclusions are definitive and newer than the forecast; conflicts reviewed.
- [ ] Optimiser result is legal and targeted scenarios have separate evidence.
- [ ] DecisionRun reloads and recommendation bundle hash verifies.
- [ ] Manual submission matches the recorded actual-choice bundle or has a reason.
- [ ] FPL confirms the intended XI, captain and vice-captain before the deadline.
