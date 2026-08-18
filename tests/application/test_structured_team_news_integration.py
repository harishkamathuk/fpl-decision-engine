"""Application-level integration tests for StructuredTeamNewsEvidenceProvider."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fpl_decision_engine.application import apply_availability_exclusions, assess_availability
from fpl_decision_engine.domain import (
    AvailabilityDisposition,
    AvailabilityEvidence,
    AvailabilityReason,
    AvailabilityState,
    EvidenceConfidence,
    GameweekNumber,
    Money,
    Player,
    Projection,
)
from fpl_decision_engine.infrastructure.optimisation import (
    SingleGameweekOptimisationRequest,
)
from fpl_decision_engine.infrastructure.providers.team_news.structured import (
    StructuredTeamNewsEvidenceProvider,
)

FIXTURE_PATH = (
    "/home/harish/dev/fpl-decision-engine/tests/fixtures/team_news/structured_evidence_v1.json"
)


def _players_with_team_news_refs():
    p1 = Player(
        id=UUID("11111111-1111-1111-1111-111111111111"),
        team_id=UUID("22222222-2222-2222-2222-222222222222"),
        first_name="Alice",
        last_name="Smith",
        web_name="ASmith",
        position="MID",
        price=Money(tenths_million=100),
        active=True,
        external_refs=(
            {"provider": "team_news_api", "external_id": "10001"},
            {"provider": "fpl", "external_id": "1"},
        ),
    )
    p2 = Player(
        id=UUID("22222222-2222-2222-2222-222222222222"),
        team_id=UUID("33333333-3333-3333-3333-333333333333"),
        first_name="Bob",
        last_name="Jones",
        web_name="BJones",
        position="FWD",
        price=Money(tenths_million=105),
        active=True,
        external_refs=(
            {"provider": "team_news_api", "external_id": "10002"},
            {"provider": "fpl", "external_id": "2"},
        ),
    )
    p3 = Player(
        id=UUID("33333333-3333-3333-3333-333333333333"),
        team_id=UUID("44444444-4444-4444-4444-444444444444"),
        first_name="Charlie",
        last_name="Brown",
        web_name="CBrown",
        position="DEF",
        price=Money(tenths_million=95),
        active=True,
        external_refs=(
            {"provider": "team_news_api", "external_id": "10003"},
            {"provider": "fpl", "external_id": "3"},
        ),
    )
    return [p1, p2, p3]


def _make_provider():
    return StructuredTeamNewsEvidenceProvider(
        FIXTURE_PATH,
        _players_with_team_news_refs(),
        processed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC),  # noqa
    )


def test_newer_definitive_unavailability_player_excluded() -> None:
    """Newer definitive external unavailability -> EXCLUDE; legal recommendation possible."""
    provider = _make_provider()
    _ = provider.evidence().data

    # Create a projection for one of the players (10001 -> Alice Smith)
    player1_id = UUID("11111111-1111-1111-1111-111111111111")
    projection = Projection(
        player_id=player1_id,
        gameweek=GameweekNumber(value=1),
        expected_points=7.5,
        appearance_probability=0.8,
        start_probability=0.5,
        source="forecast",
        model_version="v1",
        generated_at=datetime(2026, 8, 14, 20, 0, 0, tzinfo=UTC),
    )

    item = AvailabilityEvidence(
        evidence_id="tn-available",
        player_id=player1_id,
        state=AvailabilityState.AVAILABLE,
        reason=AvailabilityReason.AVAILABLE,
        confidence=EvidenceConfidence.INDICATIVE,
        source_provider="team_news_api",
        source_snapshot_id="20260814T200000Z_abc123def456",
        source_external_player_id="10001",
        source_text=None,
        reported_chance_percent=None,
        published_at=datetime(2026, 8, 14, 19, 30, 0, tzinfo=UTC),
        observed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC),
        processed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC),
        attributes=(),
    )

    assessments = assess_availability((projection,), (item,))

    assert assessments.assessments[0].disposition is AvailabilityDisposition.NO_ACTION
    assert assessments.assessments[0].applied_evidence_ids == ()

    # Optimiser should not change projection metadata
    request = SingleGameweekOptimisationRequest(
        target_gameweek=GameweekNumber(value=1),
        players=[
            Player(
                id=player1_id,
                team_id=UUID("22222222-2222-2222-2222-222222222222"),
                first_name="Alice",
                last_name="Smith",
                web_name="ASmith",
                position="MID",
                price=Money(tenths_million=100),
                active=True,
                external_refs=(
                    {"provider": "team_news_api", "external_id": "10001"},
                    {"provider": "fpl", "external_id": "1"},
                ),
            )
        ],
        projections=(projection,),
    )
    updated = apply_availability_exclusions(request, assessments)
    # Projection metadata unchanged
    assert updated.projections[0].expected_points == 7.5
    assert updated.projections[0].appearance_probability == 0.8
    assert updated.projections[0].start_probability == 0.5


def test_decision_run_provenance_survives() -> None:
    """DecisionRun integration: external evidence provenance reference survives."""
    provider = _make_provider()
    response = provider.evidence()

    # Check provenance fields
    prov = response.provenance
    assert prov.provider_id == "structured_team_news_evidence"
    assert prov.provider_version == "1"
    assert prov.retrieved_at == datetime(2026, 8, 14, 20, 15, 30, tzinfo=UTC)
    assert prov.snapshot_id == "20260814T200000Z_abc123def456"
    assert prov.source_sha256 is not None
    assert prov.mapping_fingerprint is not None
    assert prov.season is None

    # Check freshness
    assert isinstance(response.freshness, type(_make_provider().evidence().freshness))
    assert response.freshness.as_of == datetime(2026, 8, 14, 20, 15, 30, tzinfo=UTC)  # observed_at

    # Verify the input snapshot reference format
    # "availability:structured_team_news_evidence@1:<source_snapshot_id>:sha256=<source_sha256>:mapping=<mapping_fingerprint>"  # noqa
    ref_str = f"availability:structured_team_news_evidence@1:{prov.snapshot_id}:sha256={prov.source_sha256}:mapping={prov.mapping_fingerprint}"  # noqa
    assert "structured_team_news_evidence" in ref_str
    assert prov.snapshot_id in ref_str
    assert prov.source_sha256 in ref_str
    assert prov.mapping_fingerprint in ref_str


def test_positive_no_changes_to_projection_metadata() -> None:
    player1_id = UUID("11111111-1111-1111-1111-111111111111")
    projection = Projection(
        player_id=player1_id,
        gameweek=GameweekNumber(value=1),
        expected_points=7.5,
        appearance_probability=0.8,
        start_probability=0.5,
        source="forecast",
        model_version="v1",
        generated_at=datetime(2026, 8, 14, 20, 0, 0, tzinfo=UTC),
    )

    item = AvailabilityEvidence(
        evidence_id="tn-available",
        player_id=player1_id,
        state=AvailabilityState.AVAILABLE,
        reason=AvailabilityReason.AVAILABLE,
        confidence=EvidenceConfidence.INDICATIVE,
        source_provider="team_news_api",
        source_snapshot_id="20260814T200000Z_abc123def456",
        source_external_player_id="10001",
        source_text=None,
        reported_chance_percent=None,
        published_at=datetime(2026, 8, 14, 19, 30, 0, tzinfo=UTC),
        observed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC),
        processed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC),
        attributes=(),
    )

    assessments = assess_availability((projection,), (item,))

    assert assessments.assessments[0].disposition is AvailabilityDisposition.NO_ACTION
    assert assessments.assessments[0].applied_evidence_ids == ()

    # Optimiser should not change projection metadata
    request = SingleGameweekOptimisationRequest(
        target_gameweek=GameweekNumber(value=1),
        players=[
            Player(
                id=player1_id,
                team_id=UUID("22222222-2222-2222-2222-222222222222"),
                first_name="Alice",
                last_name="Smith",
                web_name="ASmith",
                position="MID",
                price=Money(tenths_million=100),
                active=True,
                external_refs=(
                    {"provider": "team_news_api", "external_id": "10001"},
                    {"provider": "fpl", "external_id": "1"},
                ),
            )
        ],
        projections=(projection,),
    )
    updated = apply_availability_exclusions(request, assessments)
    # Projection metadata unchanged
    assert updated.projections[0].expected_points == 7.5
    assert updated.projections[0].appearance_probability == 0.8
    assert updated.projections[0].start_probability == 0.5


def test_stale_evidence_retained_no_decision_effect() -> None:
    """Stale evidence -> retained as stale, no exclusion."""
    player1_id = UUID("11111111-1111-1111-1111-111111111111")
    projection = Projection(
        player_id=player1_id,
        gameweek=GameweekNumber(value=1),
        expected_points=7.5,
        appearance_probability=0.8,
        start_probability=0.5,
        source="forecast",
        model_version="v1",
        generated_at=datetime(2026, 8, 14, 20, 0, 0, tzinfo=UTC),
    )

    item = AvailabilityEvidence(
        evidence_id="tn-stale",
        player_id=player1_id,
        state=AvailabilityState.AVAILABLE,
        reason=AvailabilityReason.AVAILABLE,
        confidence=EvidenceConfidence.INDICATIVE,
        source_provider="team_news_api",
        source_snapshot_id="20260814T200000Z_abc123def456",
        source_external_player_id="10001",
        source_text=None,
        reported_chance_percent=None,
        published_at=datetime(
            2026, 8, 14, 20, 30, 0, tzinfo=UTC
        ),  # newer than generated_at but before observed_at
        observed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC),
        processed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC),
        attributes=(),
    )

    assessments = assess_availability((projection,), (item,))

    assert assessments.assessments[0].disposition is AvailabilityDisposition.NO_ACTION
    assert assessments.assessments[0].applied_evidence_ids == ("tn-stale",)


def test_contradictory_latest_evidence_conflict() -> None:
    """Contradictory latest evidence -> CONFLICT, review, no exclusion."""
    player1_id = UUID("11111111-1111-1111-1111-111111111111")
    projection = Projection(
        player_id=player1_id,
        gameweek=GameweekNumber(value=1),
        expected_points=7.5,
        appearance_probability=0.8,
        start_probability=0.5,
        source="forecast",
        model_version="v1",
        generated_at=datetime(2026, 8, 14, 19, 30, 0, tzinfo=UTC),
    )

    item_unav = AvailabilityEvidence(
        evidence_id="tn-conflict-1",
        player_id=player1_id,
        state=AvailabilityState.UNAVAILABLE,
        reason=AvailabilityReason.INJURY,
        confidence=EvidenceConfidence.DEFINITIVE,
        source_provider="team_news_api",
        source_snapshot_id="20260814T200000Z_abc123def456",
        source_external_player_id="10001",
        source_text=None,
        reported_chance_percent=None,
        published_at=datetime(2026, 8, 14, 20, 0, 0, tzinfo=UTC),
        observed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC),
        processed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC),
        attributes=(),
    )

    item_avail = AvailabilityEvidence(
        evidence_id="tn-available-2",
        player_id=player1_id,
        state=AvailabilityState.AVAILABLE,
        reason=AvailabilityReason.AVAILABLE,
        confidence=EvidenceConfidence.INDICATIVE,
        source_provider="team_news_api",
        source_snapshot_id="20260814T200000Z_abc123def456",
        source_external_player_id="10001",
        source_text=None,
        reported_chance_percent=None,
        published_at=datetime(2026, 8, 14, 20, 0, 0, tzinfo=UTC),
        observed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC),
        processed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC),
        attributes=(),
    )

    assessments = assess_availability((projection,), (item_unav, item_avail))

    # Contradictory latest evidence -> CONFLICT, review recommended, no exclusion
    assert assessments.assessments[0].disposition is AvailabilityDisposition.CONFLICT
    assert assessments.assessments[0].applied_evidence_ids == ("tn-available-2", "tn-conflict-1")


def test_availability_assessment_basic() -> None:
    """Basic availability assessment test."""
    from fpl_decision_engine.application import assess_availability

    player1_id = UUID("11111111-1111-1111-1111-111111111111")
    projection = Projection(
        player_id=player1_id,
        gameweek=GameweekNumber(value=1),
        expected_points=7.5,
        appearance_probability=0.8,
        start_probability=0.5,
        source="forecast",
        model_version="v1",
        generated_at=datetime(2026, 8, 14, 20, 0, 0, tzinfo=UTC),
    )

    item = AvailabilityEvidence(
        evidence_id="tn-available",
        player_id=player1_id,
        state=AvailabilityState.AVAILABLE,
        reason=AvailabilityReason.AVAILABLE,
        confidence=EvidenceConfidence.INDICATIVE,
        source_provider="team_news_api",
        source_snapshot_id="20260814T200000Z_abc123def456",
        source_external_player_id="10001",
        source_text=None,
        reported_chance_percent=None,
        published_at=datetime(2026, 8, 14, 19, 30, 0, tzinfo=UTC),
        observed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC),
        processed_at=datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC),
        attributes=(),
    )

    assessments = assess_availability((projection,), (item,))

    assert assessments.assessments[0].disposition is AvailabilityDisposition.NO_ACTION
    assert assessments.assessments[0].applied_evidence_ids == ()
