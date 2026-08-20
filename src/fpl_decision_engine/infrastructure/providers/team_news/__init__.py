"""Team-news evidence providers and source-specific collectors."""

from .premier_league_injuries import (
    PL_TO_FPL_TEAM_NAME,
    SOURCE_URL,
    InjuryRow,
    ParsedInjuryPage,
    PremierLeagueCapture,
    PremierLeagueInjuriesCollector,
    PremierLeagueInjuriesParseError,
    parse_injury_page,
)
from .structured import StructuredTeamNewsEvidenceProvider

__all__ = [
    "InjuryRow",
    "PL_TO_FPL_TEAM_NAME",
    "ParsedInjuryPage",
    "PremierLeagueCapture",
    "PremierLeagueInjuriesCollector",
    "PremierLeagueInjuriesParseError",
    "SOURCE_URL",
    "StructuredTeamNewsEvidenceProvider",
    "parse_injury_page",
]
