# Touchline Gameweek operations

This is the concise operating guide for running a Gameweek decision through the enforced
Touchline control plane. It replaces the archived manual runbook, which is retained for
historical context at [`docs/archive/gw1-operational-runbook.md`](../archive/gw1-operational-runbook.md)
and is **superseded**.

The control plane is read-only for decision semantics: it records provenance through the
typed run-record ledger, runs the existing doctor diagnostics, binds immutable evidence,
executes the existing blank-squad baseline, and records submission-safety checkpoints. It
does not change the optimiser, projections, scenario lifecycle, or submission-safety
semantics.

## Quickstart

A normal Gameweek run follows this path:

1. **Check the release checkout.** Authoritative runs use a tagged release checkout.
   `touchline doctor` reports the release tag and working-tree cleanliness before anything
   else; see [`troubleshooting.md`](troubleshooting.md) for the check identifiers.
2. **Acquire and freeze evidence.** Obtain the exact `bootstrap-static.json`,
   `fixtures.json` and projection CSV for the Gameweek. Record each file's SHA-256.
   The evidence manifest is the immutable, content-addressed binding used by the
   orchestrator.
3. **Build the evidence manifest.** The manifest is produced by the Gameweek evidence
   tooling (`build_gameweek_evidence_manifest` / `write_gameweek_evidence_manifest`) and
   persisted under the state root. Keep the manifest path and its SHA-256.
4. **Run the orchestrator.** Invoke `touchline run-gameweek` with the manifest, the exact
   code revision and config fingerprint, and the submission-safety inputs. The doctor,
   evidence binding, baseline, and submission-safety checkpoints run as recorded stages on
   the run record.
5. **Confirm external FPL execution.** The operator performs the FPL action outside
   Touchline, then the same invocation is repeated with `--confirm-operator-execution` (or
   resumed) so the post-submission verification can run.
6. **Inspect the execution summary.** `touchline run-record summary <run-id>` renders the
   derived view; `--json` emits the machine-readable form.
7. **Review and promote.** Once the run is completed and verified, promote it to
   authoritative with `touchline run-record promote --by <operator> --reason <text>`.

If any stage fails or blocks, the run stays provisional and is resumed with
`--resume <run-id>` after the cause is fixed; see [`recovery.md`](recovery.md).

## Where things live

All operational state is Git-ignored beneath the configured state root (default `state`):

| Content | Location under state root |
| --- | --- |
| Run-record ledger | `<state-root>/run-records/` (override with `--state-root`) |
| Gameweek evidence manifests | `<state-root>/gameweek-evidence/season=…/gameweek=…/<digest>/` |
| Decision bundles | `<state-root>/decision-bundles/season=…/gameweek=…/<run-id>/` |
| Submission-safety artefacts | referenced from the run record's artefacts |
| Decision runs (DuckDB) | `<state-root>/fpl.duckdb` |

## Stage model

The orchestrator records these mandatory stages per run:

- `doctor`
- `evidence`
- `baseline`
- `pre-submission-verify`
- `operator-execution-confirmation`
- `post-submission-verify`

A fresh run is `provisional` until every stage has a terminal PASS/WARN result and the run
is closed as completed. FAIL or BLOCKED stages block all downstream stages and keep the run
provisional so it can be resumed.

## Operator-facing command surface

Full reference: [`cli-reference.md`](cli-reference.md). Troubleshooting:
[`troubleshooting.md`](troubleshooting.md). Recovery and resume:
[`recovery.md`](recovery.md). UAT evidence template:
[`uat-rehearsal.md`](uat-rehearsal.md).

## UAT rehearsals

Rehearsals run the same control-plane flow with frozen evidence and a disposable state
root, and must complete in under 10 minutes each. The deterministic parity proof — the
orchestrated baseline producing semantically identical recommendation summaries to the
manual HiGHS baseline path on the same frozen evidence — is enforced by
`tests/infrastructure/test_orchestrated_baseline.py`. Recovery/resume behaviour is covered
deterministically by `tests/application/test_orchestration.py`.

Rehearsals that exercise the full submission-safety stages require a live FPL cookie and
operator participation (external FPL execution confirmation), so they are operator-executed:
the exact commands and expected evidence are the same as a normal run against a disposable
state root. Record each rehearsal using [`uat-rehearsal.md`](uat-rehearsal.md), including the
commit SHA/release tag, timing, identities, parity result, interventions, failures, and final
`touchline run-record summary` output.
