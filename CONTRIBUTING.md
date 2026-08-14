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

## Development

The project uses Python 3.12 and `uv`.

```bash
uv sync --all-groups
uv run ruff check .
uv run pyright
uv run pytest
```

Do not commit credentials, authenticated sessions, personal manager state, raw provider snapshots, DuckDB databases or generated Parquet datasets.
