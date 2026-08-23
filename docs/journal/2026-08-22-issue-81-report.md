# Issue #81 — Typed run-record provenance ledger: implementation report

Date: 2026-08-22 · Branch: `feature/81-typed-run-record-ledger` · Issue: #81

## 1. Implementation summary

Added a typed, validated, atomic run-record provenance ledger for the control plane, alongside (not replacing) the existing decision-engine `DecisionRun`/DuckDB provenance. Runs are one schema-validated JSON document each (`state/run-records/<run_id>.json`), written atomically (temp file + fsync + `os.replace`) with optimistic concurrency detection. The `RunRecord` domain model encodes the #80 invariants directly in validation; a `RunRecordService` implements the approved transitions, closure semantics, promotion and previous-run authority; a `touchline run-record` CLI exposes it to operators so supported updates no longer require manual `jq` edits. Legacy/sparse records without `schema_version` are read best-effort — absent fields stay absent, unparseable fields are reported, nothing is fabricated. The existing `DecisionRun`, optimiser, manager-state, snapshot and evidence paths are untouched.

## 2. Files changed

- **`src/fpl_decision_engine/domain/run_record.py`** (new) — `RunRecord` (schema v1), `StageAttempt`, `RunArtefact`, `RecordedDecision`, `AuthorityEvent`, `StageState`, `RunState`, `CloseOutcome`, `LegacyRunRecord`; structural invariants enforced at construction/read (state↔history consistency, attempt numbering, `closed_at` rules, authority-event requirements, SHA-256 format).
- **`src/fpl_decision_engine/ports/run_records.py`** (new) — `RunRecordRepository` protocol + error hierarchy (`RunRecordError`, `RunRecordNotFound`, `InvalidRunRecord`, `InvalidStageTransition`, `InvalidRunStateTransition`, `InvalidPreviousRunReference`, `RunRecordConflict`), subclassing `PersistenceError`.
- **`src/fpl_decision_engine/infrastructure/persistence/run_records.py`** (new) — `RunRecordLedger`: atomic `save` with `expected_raw` optimistic check, strict v1 read, `UnsupportedSchemaVersion` for newer versions, best-effort legacy parse, content-based authoritative-run resolution (never mtime/ordering), list/resolve helpers.
- **`src/fpl_decision_engine/application/run_record_service.py`** (new) — `RunRecordService`: create (previous-run validation/resolution), get/validate/list, start/finish/block/retry stage transitions, artefact + decision recording, close (completed/failed), promote. All validation occurs before any write.
- **`src/fpl_decision_engine/touchline_cli.py`** (new) + **`pyproject.toml`** — `touchline` entry point; `run-record` subcommands `create`, `show`, `list`, `stage`, `artefact`, `decision`, `close`, `promote`, `validate` (`--state-root` configurable, default `state/run-records`).
- **Package exports** — `domain/__init__.py`, `ports/__init__.py`, `infrastructure/persistence/__init__.py`.
- **`tests/domain/test_run_record.py`**, **`tests/application/test_run_record_service.py`**, **`tests/infrastructure/test_run_record_ledger.py`**, **`tests/test_cli_touchline.py`** (new) — 41 new tests.
- **`pyproject.toml` / `uv.lock`** — dev-only `tzdata` added (see §7). **`README.md`**, **`docs/journal/2026-08-22.md`** — docs.

## 3. #81 acceptance criteria

| Criterion | Where satisfied |
| --- | --- |
| Creation of a run | `RunRecordService.create_run` — creates a provisional run with mandatory stages, optional `code_revision`/`config_fingerprint`; `touchline run-record create`. |
| Reading and validation of an existing run record | `RunRecordService.get_run` / `validate_run`; `RunRecordLedger.get` strictly validates v1 documents (`parse_run_record`); `touchline run-record show` / `validate`. |
| Recording stage results | `start_stage` (PENDING/RUNNING → RUNNING), `finish_stage` (RUNNING → PASS/WARN/FAIL), `block_stage` (PENDING → BLOCKED), `retry_stage` (FAIL/BLOCKED → new PENDING attempt); `touchline run-record stage`. |
| Recording artefact references and hashes | `record_artefact` — name, reference, lowercase SHA-256 (validated), kind; identical re-records are no-ops, conflicting re-records are rejected. |
| Recording decisions | `record_decision` — reference, optional hash/attribution/summary; `touchline run-record decision`. |
| Validation of `previous_run_id` | `create_run` rejects explicit ids that do not reference an existing record (`InvalidPreviousRunReference` with actionable diagnostic); omitted ids resolve deterministically (see §6). |
| Closing/completing a run | `close_run` — completed requires every mandatory stage PASS/WARN; failed requires a mandatory FAIL/BLOCKED; sets `closed_at`; only from provisional. |
| Approved run/state transitions from #80 | Domain `model_validator` + service guards: provisional → completed/failed → authoritative; authoritative requires recorded `AuthorityEvent`; write-after-close rejected; retries require operator attribution. |
| Atomic persistence | `RunRecordLedger.save` — validate first, stage temp file in same directory, fsync, `os.replace`; failed writes leave the prior record byte-for-byte intact; `expected_raw` optimistic check rejects concurrent edits. |
| Explicit schema/type validation | Pydantic `RunRecord` (schema v1) with structural invariants; strict parse on read; `UnsupportedSchemaVersion` for newer versions; invalid records cannot be constructed or read back. |

## 4. Tests

New tests (41) cover all 12 required scenarios:

1. valid run creation — `tests/application/test_run_record_service.py`
2. reading a valid current-format run — `tests/infrastructure/test_run_record_ledger.py`
3. backward-compatible reading of a sparse historical record — legacy parse tests, `parse_issues` reporting
4. recording a valid stage transition — start → finish PASS
5. rejection of an invalid transition — finish from non-RUNNING, start from terminal, etc.
6. artefact/hash recording — valid SHA-256 accepted; malformed/uppercase/other-length hashes rejected
7. valid `previous_run_id` — explicit existing id and deterministic resolution
8. invalid/missing `previous_run_id` — nonexistent id rejected with diagnostic; no-authority → created without previous
9. run closure/completion — completed with all mandatory PASS/WARN; failed with a mandatory FAIL/BLOCKED
10. atomic persistence — temp-file + fsync + replace; no partial file after simulated failure
11. validation failure does not corrupt the prior record — rejected transition leaves on-disk bytes unchanged
12. missing historical fields are not fabricated — legacy read keeps fields None/empty and reports issues

Plus domain invariant tests (attempt numbering, state↔history consistency, authority-event rules) and CLI smoke tests (`touchline run-record` create/show/stage/close/promote/validate).

**Commands executed and results** (all green):

```
uv run ruff check .            → no issues
uv run pyright                 → no issues
uv run pytest                  → 233 passed
```

`git diff --check` also passes. An end-to-end CLI demo in a scratch directory verified create → stage → artefact → decision → close → promote, including correct #80-scoped previous-run resolution (a GW2 run correctly resolves no previous run when the only authoritative run is a different Gameweek; cross-GW lineage requires an explicit id).

## 5. Compatibility

- Records **without `schema_version`** are treated as legacy and read best-effort via `LegacyRunRecord`: known fields are parsed only where present and type-compatible; genuinely absent or unparseable fields remain `None`/empty and are listed in `parse_issues`; the raw payload is preserved for operator inspection.
- **Nothing is fabricated** — no default states, timestamps, or hashes are invented for historical records.
- Records claiming a **newer `schema_version`** raise `UnsupportedSchemaVersion` (reuse of the existing port error) rather than being silently downgraded.
- **Legacy records are never silently migrated**: any typed mutation of a legacy record fails with an explicit `InvalidRunRecord` telling the operator to recreate the run through the interface. No broad migration was performed — #81 does not require one.
- Legacy and current-format records coexist in `list`/`show`, each clearly labelled (`format: legacy` / `format: v1`).

## 6. Architectural compliance

The #80 invariants are enforced as follows:

- **Authority semantics** — `RunState.AUTHORITATIVE` is the only non-terminal outcome and *requires* a recorded `AuthorityEvent` (attributable `by`, timestamped `approved_at`, `reason`); an authority event on any other state is rejected. Promotion is allowed only from `completed`.
- **Provisional/completed/failed semantics** — `completed` requires every mandatory stage to have an acceptable terminal outcome (PASS/WARN) with no mandatory FAIL/BLOCKED remaining; `failed` requires mandatory execution to have failed (a mandatory FAIL/BLOCKED latest attempt); `provisional` never records `closed_at`. These definitions live in the domain model (`mandatory_stages_acceptable`, `mandatory_failure`) and are re-checked on every close.
- **No invented retry/resume behaviour** — retries append new immutable stage attempts (consecutive numbering enforced); attempts are never rewritten; retry requires operator attribution (`by`).
- **Previous-run authority, not filesystem guesswork** — `previous_run_id` is explicit lineage validated against recorded runs; when omitted, `resolve_authoritative_run` reads only recorded document content (authority-event timestamps within season/Gameweek), never mtime or directory ordering. Ambiguous approvals (equal timestamps) fail explicitly.
- **Evidence identity** — not redefined; artefacts are recorded by name/reference/hash and decisions by reference, matching existing conventions; no new identity semantics.
- **State-root/path invariant** — the ledger is a configurable root (default `state/run-records`); the canonical `DecisionRun`/DuckDB state-root path is untouched.
- **No scenario/manager-state/source semantics; no optimiser/recommendation changes** — out of scope, untouched.
- **Dependency direction** — `domain` and `ports` are pure; infrastructure imports both; no inverted dependencies.

## 7. Remaining concerns

- **`tzdata` dev dependency added** — the pre-existing `tests/infrastructure/test_persistence.py` parquet test failed in this environment because the system has no `zoneinfo` tzdata and the package was not installed. This is an environment fix, not an implementation change; flagged as a supporting change for #81.
- **Legacy records are read-only** — a deliberate, documented constraint (#81 asks for reading older records and avoiding silent migration, not for writing them). A future issue can decide migration policy.
- **No #82/#83/#84 scope** — doctor diagnostics (#82), evidence identity (#83) and the orchestrator (#84) are intentionally not implemented; the ledger's interfaces were designed not to preclude them, but nothing beyond #81 was built.
- **Optimistic concurrency** — the ledger detects concurrent edits via `expected_raw` and fails with `RunRecordConflict`; it does not retry. This is the documented behaviour; operators retry explicitly.
- **Ambiguous authority resolution** fails rather than guesses, which is correct per #80 but is worth noting as an operational edge case (equal approval timestamps across runs).

## 8. Review handoff

Independent review should focus on:

- **Provenance integrity** — stage attempts are append-only and immutable; retries create new attempts with consecutive numbering; artefacts/decisions are recorded with validated SHA-256 and can only be added, never rewritten; provenance is immutable after close.
- **Transition correctness** — every state change goes through one of the approved transitions in `RunRecordService`; the domain model re-validates the *whole* candidate record on every commit, so an invalid transition cannot persist even partially.
- **Atomic-write behaviour** — `save` validates first, then writes via temp file + fsync + `os.replace` in the same directory; a failed write leaves the prior record byte-for-byte intact (tested); stale snapshots raise `RunRecordConflict` instead of silently overwriting.
- **Previous-run authority** — explicit-id existence is enforced; deterministic resolution reads authority-event content only; ambiguous approvals fail explicitly; mtime/directory order is never used.
- **Backward compatibility** — `schema_version`-absent records read best-effort with `parse_issues`; newer versions raise `UnsupportedSchemaVersion`; legacy records are never mutated or migrated silently.
- **Merge-safety** — purely additive (new modules, exports, one script entry, dev-only `tzdata`, docs); no existing behaviour touched; `git diff --check`, Ruff, Pyright, pytest all green (233 passed).

---

*This document records the implementation report only. The code changes for #81 remain in the local working tree on `feature/81-typed-run-record-ledger` and were intentionally not included in this commit/push.*
