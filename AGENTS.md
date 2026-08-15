# Repository guidance

## Project workflow

- `main` contains stable, releasable code only. `develop` is the integration branch for the next
  release.
- Start non-release work from `develop` on a short-lived, issue-numbered branch following
  `<type>/<issue-number>-<short-description>`.
- Before major implementation, perform focused OSS reconnaissance where relevant. Keep external
  schemas behind adapters rather than adopting them as canonical models.
- Preserve inward-pointing dependencies: `domain` and `ports` must not depend on infrastructure.
- Do not add live external dependencies to CI; use deterministic synthetic fixtures.
- Run Ruff, Pyright and pytest before completing substantive work, and update the daily journal
  after substantive work.
- Implement the issue as scoped. Do not silently broaden its requirements into adjacent features.

An implementation issue is complete when its implementation has been merged into `develop` and all
required CI checks are green. At that point, close the issue manually as **Completed** if GitHub has
not auto-closed it. Do not leave completed issues open merely because closing keywords only
auto-close reliably when changes reach the default branch.

Promotion from `develop` to `main` is a release event, not a prerequisite for completing individual
implementation issues. Release pull requests and release notes should reference the already-completed
issues included in that release.

## Merge strategy

Use different merge strategies for integration history and release history.

### Non-release PRs into `develop`

Feature, fix, docs, refactor, test, chore and similar PRs should use a **merge commit by default**.
This preserves meaningful implementation commits and keeps the PR boundary explicit in `develop`.
Do not squash merely to force a linear integration history.

Before merging:

- required CI must be green and review must be complete;
- commits that represent meaningful implementation steps may remain separate;
- temporary, fixup, typo-only or other low-value commits should be cleaned up when practical before
  merge; and
- do not rewrite published/shared branch history merely to make it look tidier.

The goal is useful engineering history, not maximal commit preservation.

### Release PRs from `develop` to `main`

Use **squash merge** by default. `main` is the stable release history, so a release PR should normally
be represented by one concise release commit while the detailed engineering history remains on
`develop`.

History principle: `develop` preserves useful engineering history; `main` preserves concise release
history.

## Python documentation

Prefer meaningful documentation over mechanically adding docstrings everywhere. Documentation
should capture knowledge that a future developer cannot easily infer from the implementation.

- Public modules should have a concise module-level docstring describing their responsibility.
- Public classes should have docstrings explaining their purpose, important behaviour and relevant
  assumptions.
- Public functions and methods should have docstrings when behaviour, assumptions, side effects,
  exceptions, data semantics or return semantics are not obvious from the name, signature and type
  hints.
- Document complex private functions when their algorithm, reasoning, assumptions or business rules
  would otherwise be difficult to understand.
- Simple private helpers do not need docstrings when good naming and type hints make their purpose
  clear.
- Use type hints consistently. Do not use docstrings merely to repeat information already expressed
  clearly by the signature and types.
- Comments should explain why something is done, important constraints or non-obvious decisions;
  they should not narrate what the code does.

For ingestion, transformation and data-management code, document non-obvious data semantics where
relevant, including:

- source-system behaviour and quirks;
- field meanings that are not self-evident;
- units, conversions and any rounding or precision rules;
- identifiers, keys and their stability or scope;
- null and missing-value handling;
- temporal and date behaviour, including timezones and effective dates;
- transformation assumptions; and
- data-quality expectations and validation rules.

Describe the semantic reason for a transformation, not just its mechanics. For example, when an
upstream API represents money in unusual units, state the source unit, the canonical unit and the
conversion performed rather than merely saying that the code converts the value.
