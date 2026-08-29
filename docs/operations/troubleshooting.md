# Troubleshooting with `touchline doctor`

The doctor is the first diagnostic step for any failed or blocked run. It is read-only and
deterministic: every check prints an explicit `PASS`/`WARN`/`FAIL` with remediation, and the
exit status is `0` only when no check failed.

```bash
uv run touchline doctor --state-root state/run-records
```

## Stable check identifiers

The checks run in a fixed order with stable identifiers (also available via `--json`):

| Identifier | Meaning |
| --- | --- |
| `tools.git` | `git` is installed and on PATH. |
| `git.repository` | The working directory is inside a git repository. |
| `git.commit_sha` | A commit can be resolved at HEAD. |
| `git.release_tag` | Whether HEAD is exactly at a release tag. |
| `git.worktree_clean` | Working tree cleanliness; a dirty **immutable release worktree** is a FAIL. |
| `paths.state_root_resolution` | The configured state root resolves to a canonical path. |
| `paths.state_root_exists` | The state root exists (missing root is a WARN; writers create it). |
| `paths.state_root_placement` | State root is not inside an immutable release worktree. |
| `dirs.required` | Required directories (`data/`, `configs/`) exist (missing is a WARN). |
| `tools.uv` | `uv` is installed and on PATH. |
| `python.version` | Python satisfies `requires-python >= 3.12`. |
| `config.default_file` | The default config file, when present, parses as valid YAML. |

## Reading the result

- **PASS** — the check is satisfied.
- **WARN** — not a blocker, but review the message and remediation before an authoritative
  run (for example, a missing-but-auto-created state root, or a dirty development checkout).
- **FAIL** — the run cannot safely proceed; fix the listed remediation and re-run doctor.
- Exit code `0` means no check failed; any FAIL yields a non-zero exit.

## Common failures and fixes

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `FAIL git.repository` | Not running inside the Touchline checkout. | Run from the directory containing `.git`. |
| `FAIL paths.state_root_placement` | State root is inside an immutable release worktree (detached HEAD at a release tag). | Point `--state-root` outside the release worktree, e.g. a sibling directory. |
| `FAIL git.worktree_clean` | Immutable release worktree has uncommitted changes. | Commit, stash or revert; keep release worktrees clean. |
| `WARN git.worktree_clean` | Ordinary development checkout is dirty. | Commit or stash before an authoritative run. |
| `FAIL tools.git` / `FAIL tools.uv` | Required tool missing. | Install the tool and ensure it is on PATH. |
| `FAIL python.version` | Python older than 3.12. | Use Python 3.12+ as declared in `pyproject.toml`. |
| `FAIL config.default_file` | Default config file is invalid YAML. | Restore a valid file or fix the reported YAML error. |
| `FAIL paths.state_root_resolution` | State root path cannot be resolved (e.g. symlink loop, permission). | Check the path spelling and parent-directory permissions. |

## When a stage fails during a run

If `touchline run-gameweek` reports a FAIL or BLOCKED stage:

1. Inspect the run with the execution summary:
   ```bash
   uv run touchline run-record summary <run-id>
   uv run touchline run-record summary <run-id> --json
   ```
2. Inspect the raw record and any recorded artefacts:
   ```bash
   uv run touchline run-record show <run-id>
   uv run touchline run-record validate <run-id>
   ```
3. Fix the underlying cause (doctor FAIL, evidence binding, baseline input, submission
   safety).
4. Resume the same run with `--resume`; see [`recovery.md`](recovery.md).

Do not manually edit run-record JSON: the ledger is the typed, atomic authority and manual
edits bypass its validation.
