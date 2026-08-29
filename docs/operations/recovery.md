# Recovery and resume

A run that fails or blocks stays **provisional** and is recovered by resuming the same run,
never by starting a fresh one that would discard the recorded history.

## When to resume

Resume when the latest attempt of a stage is `FAIL` or `BLOCKED`, or when a downstream stage
is blocked by an upstream failure. The orchestrator appends a new attempt for the failed or
blocked stage through the approved retry transition and continues downstream. Stages that
already have a terminal `PASS`/`WARN` are never re-run.

## How to resume

Re-invoke `touchline run-gameweek` with the **same** invocation identity plus `--resume`:

```bash
uv run touchline run-gameweek \
  --season 2026-27 \
  --gameweek 1 \
  --evidence-manifest <same-immutable-manifest> \
  --code-revision <same-code-revision> \
  --config-fingerprint <same-fingerprint> \
  --state-root state \
  --resume <run-id> \
  --fpl-entry-id <entry-id> \
  --operator <operator> \
  --confirm-operator-execution
```

### Invariant checks on resume

Resume is rejected without mutation when the invocation drifts from the recorded run:

- season, gameweek, code revision or config fingerprint differ from the record;
- the recorded mandatory stages do not match the orchestrator stage set;
- the supplied evidence manifest does not match the immutable recorded binding
  (identity, reference and SHA-256 must all match);
- the run is not provisional (completed/authoritative runs are not resumed);
- the run is a legacy record without a supported schema;
- a stage attempt is `RUNNING` — there is no safe stale-attempt recovery transition, so the
  run must be inspected rather than guessed.

Reusable submission-safety stages are re-validated against the exact recorded DecisionBundle
and safety artefact before being reused; tampered or missing safety artefacts block resume.

## Recovery examples

| Situation | Recovery |
| --- | --- |
| Doctor FAIL | Fix the environment, re-run doctor, then resume. |
| Evidence binding FAIL/BLOCKED | Provide the exact recorded manifest (or fix the manifest bytes), then resume. |
| Baseline FAIL | Fix the frozen evidence inputs, then resume. |
| Pre-submission safety BLOCKED | Resolve the blocking safety condition, then resume. |
| Operator execution confirmation BLOCKED | Perform the external FPL action, then resume with `--confirm-operator-execution`. |
| Post-submission mismatch FAIL | Inspect the actual FPL state, reconcile, then resume. |

## Inspecting state before and after resume

```bash
uv run touchline run-record show <run-id>
uv run touchline run-record summary <run-id>
uv run touchline run-record summary <run-id> --json
```

The summary is a read-only derived view; it never mutates the run or referenced artefacts.
After a successful resume the run closes as completed and can be promoted:

```bash
uv run touchline run-record promote <run-id> --by <operator> --reason <text>
```
