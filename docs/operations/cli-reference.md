# Touchline CLI reference

This reference covers the commands used in a normal Gameweek run. Run any command with
`--help` for the current option list; this document describes the supported contract.

## Top-level commands

```text
touchline doctor        Diagnose environment, repository and state-root readiness.
touchline run-gameweek  Execute or explicitly resume doctor → evidence → baseline.
touchline analytics     Generate downstream history/comparison analytical artefacts.
touchline run-record    Typed, atomic run-record provenance ledger.
```

## `touchline doctor`

Deterministic, read-only readiness checks before a run. Prints one line per check with an
explicit `PASS`/`WARN`/`FAIL` and remediation; exit status is `0` only when no check failed.

```bash
uv run touchline doctor --state-root state/run-records
uv run touchline doctor --state-root state/run-records --json
```

The `--json` form emits the same report machine-readably. The doctor never modifies the
repository or operational state. See [`troubleshooting.md`](troubleshooting.md) for the
stable check identifiers and remediation.

## `touchline run-gameweek`

The orchestrator entry point for a fresh run or an explicit resume:

```bash
uv run touchline run-gameweek \
  --season 2026-27 \
  --gameweek 1 \
  --evidence-manifest <path-to-immutable-manifest> \
  --code-revision <exact-commit-or-tag> \
  --config-fingerprint <effective-fingerprint> \
  --state-root state \
  --fpl-entry-id <entry-id> \
  --operator <operator> \
  --confirm-operator-execution
```

Required options:

- `--season`, `--gameweek` — run identity.
- `--evidence-manifest` — persisted immutable Gameweek evidence manifest (see
  [`README.md`](README.md)).
- `--code-revision` — exact code revision used, for reproducible resume.
- `--config-fingerprint` — effective baseline configuration fingerprint.

Submission-safety options:

- `--fpl-entry-id` — authenticated FPL manager entry id (required for submission safety).
- `--operator` — operator identity for external FPL execution confirmation.
- `--confirm-operator-execution` — record that the operator reports the external FPL action
  was attempted/completed.
- `--previous-manager-state <path>` — optional explicit previous verified manager-state
  artefact.
- `--previous-state-acknowledged` — acknowledge an explicit previous/current manager-state
  difference.

Resume and identity:

- `--resume <run-id>` — resume an existing provisional run instead of starting fresh.
- `--run-id <uuid>` — optional explicit ID for a fresh run (cannot be combined with
  `--resume`).

The command requires the `FPL_COOKIE` runtime environment value for submission safety.

## `touchline run-record`

The typed, atomic provenance ledger. The default ledger root is `state/run-records`
(override with `--state-root`). Writes validate before commit and replace the record
atomically; an invalid transition or hash leaves the previous record untouched.

```bash
uv run touchline run-record --state-root <ledger-root> <subcommand> ...
```

| Subcommand | Purpose |
| --- | --- |
| `create` | Create a provisional run with validated lineage (`--season`, `--gameweek`, `--mandatory-stage` repeatable; optional `--run-id`, `--previous-run-id`, `--code-revision`, `--config-fingerprint`). |
| `show` | Read and validate one run record (`run_id`). |
| `summary` | Render a read-only derived execution summary (`run_id`; `--json` for machine output). |
| `list` | List recorded runs, optionally filtered by `--season` / `--gameweek`. |
| `stage` | Record a stage result through an approved transition (`run_id`, `stage`, `--status` of `running`/`pass`/`warn`/`fail`/`blocked`/`pending`; `--by` required for retries, `--note` optional). |
| `artefact` | Record an artefact reference and content hash (`run_id`, `--name`, `--reference`, `--sha256`, optional `--kind`). |
| `decision` | Record a decision reference (`run_id`, `--reference`, optional `--sha256`, `--by`, `--summary`). |
| `close` | Close a provisional run as `completed` or `failed` (`run_id`, `--outcome`, optional `--by`, `--note`). |
| `promote` | Promote a completed run to authoritative with explicit operator approval (`run_id`, `--by`, `--reason`). |
| `validate` | Validate that an existing run record reads back consistently (`run_id`). |

## `touchline analytics`

Generate downstream history/comparison analytical artefacts for a completed run. Read-only
for the run record; previous state is resolved exclusively from `previous_run_id`.

```bash
uv run touchline analytics <run-id> --state-root state
```

## Evidence ingestion

The offline snapshot ingestion entry point remains `fpl sync`:

```bash
uv run fpl sync --source snapshot --input <directory-or-manifest>
```

The input must contain `bootstrap-static.json` and `fixtures.json`. The immutable Gameweek
evidence manifest consumed by `run-gameweek` is produced by the Gameweek evidence tooling
(`build_gameweek_evidence_manifest` / `write_gameweek_evidence_manifest`), which records the
snapshot identity, exact component byte hashes and projection content identity.
