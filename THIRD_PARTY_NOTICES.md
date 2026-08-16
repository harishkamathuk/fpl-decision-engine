# Third-party notices

This project may integrate with external open-source projects and data providers through adapters.

Before adding a dependency or adapter, record its source, licence, pinned version or commit, purpose, local modifications (if any), and upgrade strategy here or in dedicated dependency documentation.

No third-party source code was incorporated into the repository at initial bootstrap.

## FPL Forecast

- **Source:** [daniel-mehta/fpl-forecast](https://github.com/daniel-mehta/fpl-forecast)
- **Pinned code version:** none; the upstream implementation is not a runtime dependency
- **Software licence:** GNU AGPL-3.0
- **Interface version:** current `phase9_frontend_v1`-shaped `player_gameweek_projections.csv`
  documented during Issue #5 reconnaissance on 15 Aug 2026
- **Purpose:** optional adapter for a local, user-supplied forecast CSV
- **Code incorporated or modified:** none; this repository does not import, vendor or execute the
  upstream implementation
- **Data handling:** no separate explicit licence for published forecast artifacts was identified, so
  artifacts are not redistributed or downloaded automatically
- **Upgrade strategy:** revalidate source fields, identity semantics, licence/artifact terms and
  model lineage before accepting a newer schema; keep upstream-specific fields behind the adapter
