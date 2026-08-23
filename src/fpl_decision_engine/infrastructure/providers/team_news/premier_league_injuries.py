"""Source-specific collector for the Premier League latest-injuries page.

The collector intentionally owns only this page's server-rendered HTML shape. It freezes
response bytes before parsing, maps exact player identities through a contemporaneous FPL
bootstrap, and emits the existing structured team-news evidence artefact used by the #57
provider. It does not alter canonical projections or availability semantics.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from fpl_decision_engine.domain import Player
from fpl_decision_engine.infrastructure.providers.fpl_snapshot.schemas import (
    SourceBootstrap,
    SourcePlayer,
)
from fpl_decision_engine.ports import (
    ProviderDataError,
    ProviderMappingError,
    ProviderUnavailableError,
)

SOURCE_URL = "https://www.premierleague.com/en/latest-player-injuries"
SOURCE_PROVIDER = "fpl_code"
PARSER_VERSION = "1"
CAPTURE_SCHEMA_VERSION = 1

# These are reviewed source-to-FPL labels, not fuzzy aliases. The source heading must
# match one of these keys exactly after only Unicode/whitespace/case normalisation.
PL_TO_FPL_TEAM_NAME: Mapping[str, str] = {
    "Arsenal": "Arsenal",
    "Aston Villa": "Aston Villa",
    "Bournemouth": "Bournemouth",
    "Brentford": "Brentford",
    "Brighton": "Brighton",
    "Burnley": "Burnley",
    "Chelsea": "Chelsea",
    "Crystal Palace": "Crystal Palace",
    "Everton": "Everton",
    "Fulham": "Fulham",
    "Leeds United": "Leeds",
    "Liverpool": "Liverpool",
    "Manchester City": "Man City",
    "Manchester United": "Man Utd",
    "Newcastle United": "Newcastle",
    "Nottingham Forest": "Nott\'m Forest",
    "Sunderland": "Sunderland",
    "Tottenham Hotspur": "Spurs",
    "West Ham United": "West Ham",
    "Wolverhampton Wanderers": "Wolves",
}

_MONTHS = {
    month.casefold(): index
    for index, month in enumerate(
        (
            ("January", "Jan"),
            ("February", "Feb"),
            ("March", "Mar"),
            ("April", "Apr"),
            ("May", "May"),
            ("June", "Jun"),
            ("July", "Jul"),
            ("August", "Aug"),
            ("September", "Sep"),
            ("October", "Oct"),
            ("November", "Nov"),
            ("December", "Dec"),
        ),
        start=1,
    )
    for month in month
}
_LAST_UPDATED_TIME_FIRST_RE = re.compile(
    r"Last\s+updated\s*:?\s*"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s+(?P<zone>BST|GMT)\s*,\s*"
    r"(?P<day>\d{1,2})\s+(?P<month>[A-Za-z]+)\s+(?P<year>\d{4})\s*\.?",
    re.IGNORECASE,
)
_LAST_UPDATED_DATE_FIRST_RE = re.compile(
    r"Last\s+updated\s*:?\s*"
    r"(?P<day>\d{1,2})\s+(?P<month>[A-Za-z]+)\s+(?P<year>\d{4})"
    r"(?:\s+(?:at\s+)?(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<zone>BST|GMT)?)?\s*\.?",
    re.IGNORECASE,
)


class PremierLeagueInjuriesParseError(ValueError):
    """Raised when the observed page no longer has the supported source shape."""


class _PremierLeagueMappingFailure(ProviderMappingError):
    """Mapping error carrying row-level diagnostics for the persisted evaluation."""

    def __init__(
        self,
        message: str,
        *,
        rows: Sequence[dict[str, object]],
        exact_matches: int,
        override_matches: int,
        unmapped_rows: int,
        ambiguous_rows: int,
    ) -> None:
        super().__init__(message, provider_id=SOURCE_URL)
        self.rows = tuple(rows)
        self.exact_matches = exact_matches
        self.override_matches = override_matches
        self.unmapped_rows = unmapped_rows
        self.ambiguous_rows = ambiguous_rows


@dataclass(frozen=True, slots=True)
class InjuryRow:
    """One non-placeholder row from a club injury table."""

    club: str
    player: str
    injury: str
    latest: str


@dataclass(frozen=True, slots=True)
class ParsedInjuryPage:
    """Strictly parsed fields from the supported server-rendered page."""

    rows: tuple[InjuryRow, ...]
    source_rows_seen: int
    page_last_updated_text: str | None
    page_last_updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class PremierLeagueCapture:
    """Paths and metadata for one immutable collection attempt."""

    capture_id: str
    path: Path
    manifest: Mapping[str, object]
    evaluation: Mapping[str, object]
    structured_evidence_path: Path | None


def _normalise_display_text(value: str) -> str:
    return " ".join(value.split())


def _normalise_identity(value: str) -> str:
    return _normalise_display_text(unicodedata.normalize("NFKC", value)).casefold()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def _capture_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("captured_at must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _parse_last_updated(text: str) -> tuple[str | None, datetime | None]:
    match = _LAST_UPDATED_TIME_FIRST_RE.search(text) or _LAST_UPDATED_DATE_FIRST_RE.search(text)
    if match is None:
        return ("Last updated", None) if "last updated" in text.casefold() else (None, None)

    displayed = _normalise_display_text(match.group(0))
    month = _MONTHS.get(match.group("month").casefold())
    hour = match.group("hour")
    minute = match.group("minute")
    if month is None or hour is None or minute is None:
        return displayed, None

    zone_name = (match.group("zone") or "").upper()
    if zone_name == "GMT":
        tz = UTC
    elif zone_name == "BST":
        tz = timezone(timedelta(hours=1), name="BST")
    else:
        tz = ZoneInfo("Europe/London")
    try:
        return (
            displayed,
            datetime(
                int(match.group("year")),
                month,
                int(match.group("day")),
                int(hour),
                int(minute),
                tzinfo=tz,
            ),
        )
    except ValueError:
        return displayed, None


class _InjuryPageHTMLParser(HTMLParser):
    """Parse only club-heading/table blocks from the selected page."""

    _CELL_TAGS = {"th", "td"}
    _HEADING_TAGS = {"h2", "h3"}
    _IGNORED_TAGS = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._heading_depth = 0
        self._heading_parts: list[str] = []
        self._current_heading: str | None = None
        self._table_depth = 0
        self._table_rows: list[tuple[str, ...]] | None = None
        self._row_parts: list[str] | None = None
        self._cell_parts: list[str] | None = None
        self._rows: list[InjuryRow] = []
        self._source_rows_seen = 0
        self._saw_table = False
        self._visible_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.casefold()
        if tag in self._IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag in self._HEADING_TAGS and not self._table_depth:
            self._heading_depth = 1
            self._heading_parts = []
        elif self._heading_depth and tag in self._HEADING_TAGS:
            self._heading_depth += 1
        if tag == "table":
            if self._table_depth:
                raise PremierLeagueInjuriesParseError("nested injury tables are unsupported")
            self._table_depth = 1
            self._table_rows = []
            self._saw_table = True
        elif self._table_depth and tag == "tr":
            if self._row_parts is not None:
                raise PremierLeagueInjuriesParseError("nested table rows are unsupported")
            self._row_parts = []
        elif self._row_parts is not None and tag in self._CELL_TAGS:
            if self._cell_parts is not None:
                raise PremierLeagueInjuriesParseError("nested table cells are unsupported")
            self._cell_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in self._IGNORED_TAGS and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if self._row_parts is not None and tag in self._CELL_TAGS:
            if self._cell_parts is None:
                raise PremierLeagueInjuriesParseError("table cell closed without opening")
            self._row_parts.append(_normalise_display_text(" ".join(self._cell_parts)))
            self._cell_parts = None
        elif tag == "tr" and self._row_parts is not None:
            assert self._table_rows is not None
            cells = tuple(self._row_parts)
            self._table_rows.append(cells)
            self._row_parts = None
        elif tag == "table":
            if self._table_rows is None or self._table_depth != 1:
                raise PremierLeagueInjuriesParseError("invalid injury table state")
            self._finish_table(self._table_rows)
            self._table_rows = None
            self._table_depth = 0
        elif tag in self._HEADING_TAGS and self._heading_depth:
            self._heading_depth -= 1
            if self._heading_depth == 0:
                heading = _normalise_display_text(" ".join(self._heading_parts))
                if heading:
                    self._current_heading = heading
                self._heading_parts = []

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if data.strip():
            self._visible_parts.append(data)
        if self._heading_depth:
            self._heading_parts.append(data)
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    def close(self) -> None:
        super().close()
        if self._table_depth or self._row_parts is not None or self._cell_parts is not None:
            raise PremierLeagueInjuriesParseError("unterminated injury table")

    def _finish_table(self, rows: list[tuple[str, ...]]) -> None:
        if not rows or rows[0] != ("Player", "Injury", "Latest"):
            raise PremierLeagueInjuriesParseError(
                "expected an injury table with Player, Injury and Latest headers"
            )
        if self._current_heading is None:
            raise PremierLeagueInjuriesParseError("injury table has no club heading")
        for row in rows[1:]:
            self._source_rows_seen += 1
            if len(row) != 3:
                raise PremierLeagueInjuriesParseError("injury row must contain three cells")
            player, injury, latest = row
            if not player and not injury and not latest:
                raise PremierLeagueInjuriesParseError("empty injury row is unsupported")
            if player == injury == latest == "-":
                continue
            if player == "-" or not player:
                raise PremierLeagueInjuriesParseError("injury row has no player identity")
            self._rows.append(
                InjuryRow(
                    club=self._current_heading,
                    player=player,
                    injury=injury,
                    latest=latest,
                )
            )

    def result(self) -> ParsedInjuryPage:
        if not self._saw_table:
            raise PremierLeagueInjuriesParseError("no injury table found")
        visible_text = _normalise_display_text(" ".join(self._visible_parts))
        page_last_updated_text, page_last_updated_at = _parse_last_updated(visible_text)
        return ParsedInjuryPage(
            rows=tuple(self._rows),
            source_rows_seen=self._source_rows_seen,
            page_last_updated_text=page_last_updated_text,
            page_last_updated_at=page_last_updated_at,
        )


def parse_injury_page(raw_bytes: bytes) -> ParsedInjuryPage:
    """Parse the observed server-rendered injury page without network access."""

    try:
        html = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PremierLeagueInjuriesParseError("source response is not UTF-8 HTML") from exc
    parser = _InjuryPageHTMLParser()
    try:
        parser.feed(html)
        parser.close()
        return parser.result()
    except PremierLeagueInjuriesParseError:
        raise
    except Exception as exc:
        raise PremierLeagueInjuriesParseError("source HTML cannot be parsed") from exc


@dataclass(frozen=True, slots=True)
class _MappedRow:
    row: InjuryRow
    source_player: SourcePlayer
    fpl_code: str
    mapping_method: str


class PremierLeagueInjuriesCollector:
    """Collect one Premier League injury page into immutable #57 artefacts.

    ``bootstrap_path`` is mandatory for successful evidence emission because every
    record must carry the stable FPL ``code``. ``canonical_players`` is optional for
    collection, but when supplied it is used to validate the emitted artefact through
    the real #57 provider before the result is returned.
    """

    def __init__(
        self,
        output_root: Path,
        *,
        bootstrap_path: Path | None,
        canonical_players: Sequence[Player] = (),
        player_overrides: Mapping[tuple[str, str], str] | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.bootstrap_path = Path(bootstrap_path) if bootstrap_path is not None else None
        self.canonical_players = tuple(canonical_players)
        self.player_overrides = dict(player_overrides or {})

    def collect(self) -> PremierLeagueCapture:
        """Perform one ordinary GET and persist the response before parsing."""

        captured_at = datetime.now(UTC)
        request = Request(SOURCE_URL, method="GET", headers={"User-Agent": "fpl-decision-engine"})
        try:
            with urlopen(request, timeout=30) as response:
                raw_bytes = response.read()
                status = response.status
                content_type = response.headers.get("Content-Type")
                resolved_url = response.geturl()
        except HTTPError as exc:
            raw_bytes = exc.read()
            if raw_bytes:
                return self.collect_response(
                    raw_bytes,
                    captured_at=captured_at,
                    http_status=exc.code,
                    content_type=exc.headers.get("Content-Type"),
                    resolved_url=str(exc.url),
                )
            self._write_failed_attempt(captured_at, None, None, None, "HTTP retrieval failed")
            raise ProviderUnavailableError(
                f"Premier League source returned HTTP {exc.code}",
                provider_id=SOURCE_URL,
            ) from exc
        except (OSError, URLError) as exc:
            self._write_failed_attempt(captured_at, None, None, None, str(exc))
            raise ProviderUnavailableError(
                f"cannot retrieve Premier League injury page: {exc}",
                provider_id=SOURCE_URL,
            ) from exc
        return self.collect_response(
            raw_bytes,
            captured_at=captured_at,
            http_status=status,
            content_type=content_type,
            resolved_url=resolved_url,
        )

    def collect_response(
        self,
        raw_bytes: bytes,
        *,
        captured_at: datetime,
        http_status: int = 200,
        content_type: str | None = "text/html;charset=utf-8",
        resolved_url: str = SOURCE_URL,
    ) -> PremierLeagueCapture:
        """Process frozen response bytes; this is the deterministic test seam."""

        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            raise ValueError("captured_at must be timezone-aware")
        source_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        capture_id = f"{_capture_timestamp(captured_at)}_{source_sha256[:12]}"
        capture_path = self.output_root / "premier-league-injuries" / capture_id
        if capture_path.exists():
            raise ProviderDataError(
                f"refusing to overwrite existing capture: {capture_path}",
                provider_id=SOURCE_URL,
            )
        capture_path.mkdir(parents=True, exist_ok=False)
        (capture_path / "raw-response.bin").write_bytes(raw_bytes)

        page_last_updated_text, page_last_updated_at = self._best_effort_last_updated(raw_bytes)
        manifest = self._manifest(
            capture_id=capture_id,
            captured_at=captured_at,
            http_status=http_status,
            content_type=content_type,
            resolved_url=resolved_url,
            source_sha256=source_sha256,
            byte_count=len(raw_bytes),
            page_last_updated_text=page_last_updated_text,
            page_last_updated_at=page_last_updated_at,
        )
        self._write_json(capture_path / "capture-manifest.json", manifest)

        base_evaluation = self._evaluation(
            collection_success=False,
            http_status=http_status,
            parse_success=False,
            source_rows_seen=0,
            evidence_records_emitted=0,
            exact_matches=0,
            override_matches=0,
            unmapped_rows=0,
            ambiguous_rows=0,
            page_last_updated_at=page_last_updated_at,
            capture_time=captured_at,
            rows=(),
        )
        if http_status != 200:
            self._write_json(capture_path / "evaluation.json", base_evaluation)
            raise ProviderDataError(
                f"Premier League source returned HTTP {http_status}", provider_id=SOURCE_URL
            )
        if content_type is None or not content_type.casefold().startswith("text/html"):
            self._write_json(capture_path / "evaluation.json", base_evaluation)
            raise ProviderDataError(
                f"unexpected Premier League content type: {content_type!r}",
                provider_id=SOURCE_URL,
            )

        try:
            parsed = parse_injury_page(raw_bytes)
        except PremierLeagueInjuriesParseError as exc:
            self._write_json(capture_path / "evaluation.json", base_evaluation)
            raise ProviderDataError(str(exc), provider_id=SOURCE_URL) from exc

        try:
            mapped, comparison_rows, counts = self._map_rows(parsed.rows)
        except ProviderMappingError as exc:
            mapping_failure = (
                exc if isinstance(exc, _PremierLeagueMappingFailure) else None
            )
            failed_evaluation = self._evaluation(
                collection_success=False,
                http_status=http_status,
                parse_success=True,
                source_rows_seen=parsed.source_rows_seen,
                evidence_records_emitted=0,
                exact_matches=mapping_failure.exact_matches if mapping_failure else 0,
                override_matches=mapping_failure.override_matches if mapping_failure else 0,
                unmapped_rows=mapping_failure.unmapped_rows if mapping_failure else 0,
                ambiguous_rows=mapping_failure.ambiguous_rows if mapping_failure else 0,
                page_last_updated_at=parsed.page_last_updated_at,
                capture_time=captured_at,
                rows=mapping_failure.rows if mapping_failure else (),
            )
            self._write_json(capture_path / "evaluation.json", failed_evaluation)
            raise exc

        evidence = self._build_artifact(
            capture_id=capture_id,
            captured_at=captured_at,
            parsed=parsed,
            mapped=mapped,
        )
        structured_path = capture_path / "structured-evidence.json"
        candidate_path = capture_path / ".structured-evidence.json.tmp"
        try:
            self._write_json(candidate_path, evidence)
            if self.canonical_players:
                self._validate_with_issue_57(
                    candidate_path,
                    processed_at=captured_at + timedelta(microseconds=1),
                )
            os.replace(candidate_path, structured_path)
        except Exception as exc:
            candidate_path.unlink(missing_ok=True)
            failure_evaluation = self._evaluation(
                collection_success=False,
                http_status=http_status,
                parse_success=True,
                source_rows_seen=parsed.source_rows_seen,
                evidence_records_emitted=0,
                exact_matches=counts[0],
                override_matches=counts[1],
                unmapped_rows=0,
                ambiguous_rows=0,
                page_last_updated_at=parsed.page_last_updated_at,
                capture_time=captured_at,
                rows=comparison_rows,
            )
            failure_evaluation["error"] = str(exc)
            self._write_json(capture_path / "evaluation.json", failure_evaluation)
            raise

        evaluation = self._evaluation(
            collection_success=True,
            http_status=http_status,
            parse_success=True,
            source_rows_seen=parsed.source_rows_seen,
            evidence_records_emitted=len(mapped),
            exact_matches=counts[0],
            override_matches=counts[1],
            unmapped_rows=0,
            ambiguous_rows=0,
            page_last_updated_at=parsed.page_last_updated_at,
            capture_time=captured_at,
            rows=comparison_rows,
        )
        self._write_json(capture_path / "evaluation.json", evaluation)
        return PremierLeagueCapture(
            capture_id=capture_id,
            path=capture_path,
            manifest=manifest,
            evaluation=evaluation,
            structured_evidence_path=structured_path,
        )

    def _best_effort_last_updated(self, raw_bytes: bytes) -> tuple[str | None, datetime | None]:
        try:
            visible = _normalise_display_text(raw_bytes.decode("utf-8"))
        except UnicodeDecodeError:
            return None, None
        return _parse_last_updated(visible)

    def _manifest(
        self,
        *,
        capture_id: str,
        captured_at: datetime,
        http_status: int | None,
        content_type: str | None,
        resolved_url: str | None,
        source_sha256: str | None,
        byte_count: int | None,
        page_last_updated_text: str | None,
        page_last_updated_at: datetime | None,
    ) -> dict[str, object]:
        return {
            "schema_version": CAPTURE_SCHEMA_VERSION,
            "logical_source_page": SOURCE_URL,
            "retrieval_url": SOURCE_URL,
            "resolved_url": resolved_url,
            "captured_at": _iso(captured_at),
            "http_status": http_status,
            "content_type": content_type,
            "byte_count": byte_count,
            "sha256": source_sha256,
            "page_last_updated_text": page_last_updated_text,
            "page_last_updated_at": _iso(page_last_updated_at),
            "parser_version": PARSER_VERSION,
            "capture_id": capture_id,
        }

    def _map_rows(
        self, rows: Sequence[InjuryRow]
    ) -> tuple[tuple[_MappedRow, ...], tuple[dict[str, object], ...], tuple[int, int]]:
        bootstrap = self._load_bootstrap()
        teams_by_name = {
            _normalise_identity(team.name): team.id for team in bootstrap.teams
        }
        players_by_team: dict[int, list[SourcePlayer]] = {}
        for source_player in bootstrap.elements:
            players_by_team.setdefault(source_player.team, []).append(source_player)

        mapped: list[_MappedRow] = []
        comparison: list[dict[str, object]] = []
        exact_matches = 0
        override_matches = 0
        failures: list[str] = []
        unmapped_rows = 0
        ambiguous_rows = 0
        seen_evidence_keys: set[str] = set()

        for row in rows:
            fpl_team_name = PL_TO_FPL_TEAM_NAME.get(row.club)
            if fpl_team_name is None:
                fpl_team_name = next(
                    (
                        target
                        for source, target in PL_TO_FPL_TEAM_NAME.items()
                        if _normalise_identity(source) == _normalise_identity(row.club)
                    ),
                    None,
                )
            team_id = teams_by_name.get(_normalise_identity(fpl_team_name or ""))
            candidates = players_by_team.get(team_id, []) if team_id is not None else []
            exact = [
                player
                for player in candidates
                if _normalise_identity(f"{player.first_name} {player.second_name}")
                == _normalise_identity(row.player)
            ]
            override_code = self.player_overrides.get((row.club, row.player))
            source_player: SourcePlayer | None = None
            method = "exact"
            if override_code is not None:
                source_player = self._validated_override(
                    row.club, row.player, override_code, bootstrap
                )
                method = "override"
                override_matches += 1
            elif fpl_team_name is None or team_id is None or not exact:
                unmapped_rows += 1
                status = "unmapped"
                if len(exact) > 1:
                    ambiguous_rows += 1
                    status = "ambiguous"
                comparison.append(self._comparison_row(row, status, None, None))
                failures.append(
                    f"{row.club}/{row.player}: {status} under exact team-scoped mapping"
                )
                continue
            elif len(exact) > 1:
                ambiguous_rows += 1
                comparison.append(self._comparison_row(row, "ambiguous", None, None))
                failures.append(f"{row.club}/{row.player}: ambiguous exact player match")
                continue
            else:
                source_player = exact[0]
                exact_matches += 1

            assert source_player is not None
            if source_player.code is None:
                unmapped_rows += 1
                comparison.append(
                    self._comparison_row(row, "missing_fpl_code", source_player, None)
                )
                failures.append(f"{row.club}/{row.player}: mapped player has no stable FPL code")
                continue
            code_matches = [
                item for item in bootstrap.elements if item.code == source_player.code
            ]
            if len(code_matches) != 1:
                unmapped_rows += 1
                comparison.append(
                    self._comparison_row(row, "non_unique_fpl_code", source_player, None)
                )
                failures.append(
                    f"{row.club}/{row.player}: stable FPL code {source_player.code} is not unique"
                )
                continue
            key = f"{row.club}|{row.player}|{row.injury}|{row.latest}"
            if key in seen_evidence_keys:
                failures.append(f"{row.club}/{row.player}: duplicate source injury row")
                continue
            seen_evidence_keys.add(key)
            code = str(source_player.code)
            mapped_row = _MappedRow(
                row=row,
                source_player=source_player,
                fpl_code=code,
                mapping_method=method,
            )
            mapped.append(mapped_row)
            comparison.append(self._comparison_row(row, method, source_player, code))

        if failures:
            raise _PremierLeagueMappingFailure(
                "Premier League injury identity mapping failed: " + "; ".join(failures),
                rows=comparison,
                exact_matches=exact_matches,
                override_matches=override_matches,
                unmapped_rows=unmapped_rows,
                ambiguous_rows=ambiguous_rows,
            )
        return tuple(mapped), tuple(comparison), (exact_matches, override_matches)

    def _load_bootstrap(self) -> SourceBootstrap:
        if self.bootstrap_path is None:
            raise ProviderMappingError(
                "bootstrap-static.json is required for exact fpl_code mapping",
                provider_id=SOURCE_URL,
            )
        try:
            value = json.loads(self.bootstrap_path.read_bytes())
            return SourceBootstrap.model_validate(value)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ProviderMappingError(
                f"cannot load contemporaneous bootstrap-static.json: {exc}",
                provider_id=SOURCE_URL,
            ) from exc

    def _validated_override(
        self,
        club: str,
        player: str,
        code: str,
        bootstrap: SourceBootstrap,
    ) -> SourcePlayer:
        fpl_team_name = PL_TO_FPL_TEAM_NAME.get(club)
        team_id = next(
            (
                team.id
                for team in bootstrap.teams
                if _normalise_identity(team.name)
                == _normalise_identity(fpl_team_name or "")
            ),
            None,
        )
        matches = [
            item
            for item in bootstrap.elements
            if item.code is not None and str(item.code) == code
        ]
        if len(matches) != 1 or team_id is None or matches[0].team != team_id:
            raise ProviderMappingError(
                f"invalid reviewed override for {club}/{player}: code {code}",
                provider_id=SOURCE_URL,
            )
        return matches[0]

    @staticmethod
    def _comparison_row(
        row: InjuryRow,
        mapping_status: str,
        source_player: SourcePlayer | None,
        fpl_code: str | None,
    ) -> dict[str, object]:
        has_fpl_mapping = fpl_code is not None
        already_flagged = bool(
            source_player and (source_player.status != "a" or source_player.news)
        )
        return {
            "club": row.club,
            "player": row.player,
            "injury": row.injury,
            "latest": row.latest,
            "mapping_status": mapping_status,
            "fpl_code": fpl_code,
            "fpl_status": source_player.status if source_player else None,
            "fpl_news": source_player.news if source_player else None,
            "classification": (
                "PL injury row + no FPL mapping"
                if not has_fpl_mapping
                else "PL injury row + FPL already flagged"
                if already_flagged
                else "PL injury row + FPL still status=a"
            ),
        }

    def _build_artifact(
        self,
        *,
        capture_id: str,
        captured_at: datetime,
        parsed: ParsedInjuryPage,
        mapped: Sequence[_MappedRow],
    ) -> dict[str, object]:
        records: list[dict[str, object]] = []
        for item in mapped:
            row = item.row
            evidence_key = "|".join(
                (
                    SOURCE_URL,
                    row.club,
                    row.player,
                    row.injury,
                    row.latest,
                    _iso(parsed.page_last_updated_at) or "",
                )
            )
            evidence_id = f"pl-injury-{hashlib.sha256(evidence_key.encode()).hexdigest()[:24]}"
            attrs: dict[str, str] = {
                "club": row.club,
                "injury": row.injury,
                "latest": row.latest,
            }
            if parsed.page_last_updated_text is not None:
                attrs["page_last_updated_text"] = parsed.page_last_updated_text
            explicit_injury = row.injury not in {"", "-"}
            records.append(
                {
                    "evidence_id": evidence_id,
                    "source_external_player_id": item.fpl_code,
                    "source_reference": SOURCE_URL,
                    "state": "doubtful" if explicit_injury else "unknown",
                    "reason": "injury",
                    "confidence": "indicative" if explicit_injury else "ambiguous",
                    "published_at": _iso(parsed.page_last_updated_at),
                    "source_text": " | ".join((row.player, row.injury, row.latest)),
                    "attributes": attrs,
                }
            )
        return {
            "schema_version": 1,
            "source_provider": SOURCE_PROVIDER,
            "source_snapshot_id": capture_id,
            "observed_at": _iso(captured_at),
            "evidence": records,
        }

    def _validate_with_issue_57(
        self, structured_path: Path, *, processed_at: datetime
    ) -> None:
        from fpl_decision_engine.infrastructure.providers.team_news.structured import (
            StructuredTeamNewsEvidenceProvider,
        )

        try:
            StructuredTeamNewsEvidenceProvider(
                structured_path,
                self.canonical_players,
                processed_at=processed_at,
            ).evidence()
        except Exception as exc:
            raise ProviderDataError(
                f"emitted artefact failed StructuredTeamNewsEvidenceProvider validation: {exc}",
                provider_id=SOURCE_URL,
            ) from exc

    @staticmethod
    def _evaluation(
        *,
        collection_success: bool,
        http_status: int | None,
        parse_success: bool,
        source_rows_seen: int,
        evidence_records_emitted: int,
        exact_matches: int,
        override_matches: int,
        unmapped_rows: int,
        ambiguous_rows: int,
        page_last_updated_at: datetime | None,
        capture_time: datetime,
        rows: Sequence[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "collection_success": collection_success,
            "http_status": http_status,
            "parse_success": parse_success,
            "source_rows_seen": source_rows_seen,
            "evidence_records_emitted": evidence_records_emitted,
            "exact_matches": exact_matches,
            "override_matches": override_matches,
            "unmapped_rows": unmapped_rows,
            "ambiguous_rows": ambiguous_rows,
            "page_last_updated_at": _iso(page_last_updated_at),
            "capture_time": _iso(capture_time),
            "parser_version": PARSER_VERSION,
            "rows": list(rows),
        }

    @staticmethod
    def _write_json(path: Path, value: Mapping[str, object]) -> None:
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _write_failed_attempt(
        self,
        captured_at: datetime,
        http_status: int | None,
        content_type: str | None,
        resolved_url: str | None,
        error: str,
    ) -> None:
        del content_type, resolved_url
        timestamp = _capture_timestamp(captured_at)
        path = self.output_root / "premier-league-injuries" / f"{timestamp}_no-response"
        if path.exists():
            return
        path.mkdir(parents=True, exist_ok=False)
        self._write_json(
            path / "evaluation.json",
            {
                "collection_success": False,
                "http_status": http_status,
                "parse_success": False,
                "source_rows_seen": 0,
                "evidence_records_emitted": 0,
                "exact_matches": 0,
                "override_matches": 0,
                "unmapped_rows": 0,
                "ambiguous_rows": 0,
                "page_last_updated_at": None,
                "capture_time": _iso(captured_at),
                "parser_version": PARSER_VERSION,
                "error": error,
            },
        )
