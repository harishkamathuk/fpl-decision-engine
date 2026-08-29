# Gameweek UAT rehearsal evidence

Use this record for each of the **three independent #90 operator rehearsals**. Every rehearsal
must complete in **under 10 minutes**. No rehearsal may be claimed complete without this record
and its supporting execution-summary output.

Run the rehearsal with frozen evidence and a disposable state root where appropriate. Use the
same control-plane commands described in the [operations quickstart](README.md). Capture the
commit/code revision from the checkout and the release tag when running from a tagged release
checkout; do not invent a separate version identifier.

## Rehearsal record

Copy this section once per rehearsal.

- Rehearsal number: `1` / `2` / `3`
- Date: `YYYY-MM-DD`
- Operator: `<name or identifier>`
- Start time (UTC): `YYYY-MM-DDTHH:MM:SSZ`
- End time (UTC): `YYYY-MM-DDTHH:MM:SSZ`
- Elapsed duration: `<minutes:seconds>` (must be `< 10:00`)
- Code revision / commit SHA: `<sha>`
- Release tag, if applicable: `<tag or not-applicable>`
- Season / Gameweek: `<season> / <gameweek>`
- Run ID: `<run-id>`
- Evidence identity / hash: `<identity and/or SHA-256>`
- Manager-state identity/reference, where applicable: `<reference or not-applicable>`
- Run completed successfully: `YES` / `NO`
- Parity against deterministic/manual baseline, where applicable: `PASS` / `FAIL` / `NOT-APPLICABLE`
- Recovery/resume exercised: `YES` / `NO`
- Operator intervention: `<none or describe>`
- Failure encountered: `<none or describe>`
- Final execution-summary reference/output: `<path, captured output, or artefact reference>`
- Overall rehearsal result: `PASS` / `FAIL`

## Evidence checklist

- [ ] Frozen evidence was identified before the run and was not changed during the rehearsal.
- [ ] Doctor result and any remediation are retained.
- [ ] Run ID and evidence identity/hash match the recorded run.
- [ ] Start/end times and elapsed duration are recorded; elapsed duration is under 10 minutes.
- [ ] Code revision/commit SHA is recorded, with release tag when applicable.
- [ ] Execution summary output is retained.
- [ ] Any recovery/resume command and resulting summary are retained.
- [ ] Failures and operator interventions are recorded, including `none` when absent.

# Acceptance status

#90 requires **three independent rehearsals**, each completing in **under 10 minutes**. The
implementation is not accepted as having met this condition until all three records are complete,
with supporting evidence, and marked `PASS`.
