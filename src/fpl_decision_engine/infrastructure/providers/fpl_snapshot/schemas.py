"""Source-shaped validation models confined to the FPL snapshot adapter."""

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class SourceModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class SourceEvent(SourceModel):
    id: int = Field(ge=1, le=38)
    name: str = Field(min_length=1)
    deadline_time: AwareDatetime
    finished: bool = False


class SourceTeam(SourceModel):
    id: int = Field(gt=0)
    name: str = Field(min_length=1)
    short_name: str = Field(min_length=1, max_length=4)


class SourcePlayer(SourceModel):
    id: int = Field(gt=0)
    code: int | None = Field(
        default=None,
        gt=0,
        description="Stable cross-season FPL code, distinct from season-specific element id",
    )
    team: int = Field(gt=0)
    first_name: str = Field(min_length=1)
    second_name: str = Field(min_length=1)
    web_name: str = Field(min_length=1)
    element_type: int = Field(gt=0)
    now_cost: int = Field(ge=0)
    status: str = Field(min_length=1)
    news: str = ""
    chance_of_playing_next_round: int | None = Field(default=None, ge=0, le=100)


class SourceBootstrap(SourceModel):
    events: tuple[SourceEvent, ...]
    teams: tuple[SourceTeam, ...]
    elements: tuple[SourcePlayer, ...]


class SourceFixture(SourceModel):
    id: int = Field(gt=0)
    event: int | None = Field(default=None, ge=1, le=38)
    team_h: int = Field(gt=0)
    team_a: int = Field(gt=0)
    kickoff_time: AwareDatetime | None = None
    finished: bool = False
    team_h_score: int | None = Field(default=None, ge=0)
    team_a_score: int | None = Field(default=None, ge=0)
