# Contributing

## Branch model

- `main` contains only stable, releasable code.
- `develop` is the integration branch for the next release.
- Work is performed on short-lived branches created from `develop` and merged back through pull requests.
- Release pull requests promote a coherent, tested baseline from `develop` to `main`.

## Development

The project uses Python 3.12 and `uv`.

```bash
uv sync --all-groups
uv run ruff check .
uv run pyright
uv run pytest
```

Do not commit credentials, authenticated sessions, personal manager state, raw provider snapshots, DuckDB databases or generated Parquet datasets.
