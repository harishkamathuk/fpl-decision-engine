# Contributing

## Branch model

- `main` contains only stable, releasable code.
- `develop` is the integration branch for the next release.
- Work is performed on short-lived branches created from `develop` and merged back through pull requests.
- Pull-request branches targeting `develop` **must include the GitHub issue number** using `<type>/<issue-number>-<short-description>`.
- Allowed branch types are `feature`, `fix`, `hotfix`, `chore`, `docs`, `refactor`, `test`, `perf`, `build` and `ci`.
- Examples: `feature/1-domain-model`, `fix/27-selling-price`.
- Every non-release change therefore starts with a GitHub issue; CI enforces this naming rule for pull requests into `develop`.
- Release pull requests promote a coherent, tested baseline from `develop` to `main`; the `develop` branch itself is exempt from the issue-number naming pattern.

## Issue lifecycle

- An implementation issue is considered complete once its implementation has been merged into `develop` and all required CI checks are green.
- Close the issue as **Completed** at that point; promotion to `main` is not required for individual issue completion.
- `main` remains the stable/releasable branch. Changes move from `develop` to `main` only as part of a coherent release promotion rather than after every completed issue.
- GitHub closing keywords may not automatically close issues when the closing commit lands on `develop` rather than the default branch. When that happens, close the completed issue manually after the merge and CI have succeeded.
- Release notes or release pull requests should reference the completed issues included in that release instead of keeping those issues artificially open until promotion to `main`.

## Development

The project uses Python 3.12 and `uv`.

```bash
uv sync --all-groups
uv run ruff check .
uv run pyright
uv run pytest
```

Do not commit credentials, authenticated sessions, personal manager state, raw provider snapshots, DuckDB databases or generated Parquet datasets.
