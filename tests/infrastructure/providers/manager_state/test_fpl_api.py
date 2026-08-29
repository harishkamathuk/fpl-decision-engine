"""Deterministic fake-HTTP coverage for the official FPL manager-state adapter."""

from __future__ import annotations

from typing import Any

import pytest

from fpl_decision_engine.domain import GameweekNumber
from fpl_decision_engine.infrastructure.providers.manager_state.fpl_api import (
    OfficialFplManagerStateSource,
)
from fpl_decision_engine.ports import (
    ProviderAuthenticationError,
    ProviderDataError,
    ProviderUnavailableError,
)

ENTRY_ID = 42
DEADLINE = "2026-08-29T12:00:00Z"


def picks() -> list[dict[str, Any]]:
    result = [
        {
            "element": i,
            "position": i,
            "is_captain": i == 3,
            "is_vice_captain": i == 4,
        }
        for i in range(1, 16)
    ]
    result[1]["position"] = 11
    result[10]["position"] = 10
    result[7]["position"] = 9
    result[11]["position"] = 12
    result[12]["position"] = 13
    result[13]["position"] = 14
    result[14]["position"] = 15
    return result


def bootstrap() -> dict[str, Any]:
    return {
        "events": [{"id": 1, "deadline_time": DEADLINE}],
        "elements": [
            {"id": 1, "element_type": 1},
            {"id": 2, "element_type": 1},
            {"id": 15, "element_type": 1},
            *[{"id": i, "element_type": 2} for i in range(3, 8)],
            *[{"id": i, "element_type": 3} for i in range(8, 13)],
            *[{"id": i, "element_type": 4} for i in range(13, 15)],
        ],
    }


def team(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {"id": ENTRY_ID, "event": 1, "picks": picks()}
    value.update(changes)
    return value


class Response:
    def __init__(self, payload: object = None, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> object:
        return self.payload


class FakeHttp:
    def __init__(self, responses: dict[str, Response]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> Response:
        del timeout
        endpoint = url.removeprefix("https://fpl.test")
        self.calls.append((endpoint, headers))
        response = self.responses[endpoint]
        if isinstance(response, Exception):
            raise response
        return response


def source(*, http: FakeHttp) -> OfficialFplManagerStateSource:
    return OfficialFplManagerStateSource(
        http,
        base_url="https://fpl.test",
        headers={"Cookie": "session-secret", "Authorization": "Bearer secret"},
    )


def valid_http() -> FakeHttp:
    return FakeHttp(
        {
            "/api/me/": Response({"player_id": ENTRY_ID}),
            f"/api/my-team/{ENTRY_ID}/": Response(team()),
            "/api/bootstrap-static/": Response(bootstrap()),
        }
    )


def test_a1_valid_authenticated_acquisition_normalizes_all_selection_state() -> None:
    http = valid_http()
    result = source(http=http).acquire(entry_id=ENTRY_ID, target_event=GameweekNumber(value=1))
    assert result.manager_entry_id == ENTRY_ID
    assert result.authenticated_entry_id == ENTRY_ID
    assert result.squad_player_ids == tuple(range(1, 16))
    assert result.starting_xi_player_ids == tuple(range(1, 12))
    assert result.captain_player_id == 3
    assert result.vice_captain_player_id == 4
    assert result.reserve_goalkeeper_player_id == 15
    assert result.ordered_outfield_bench_player_ids == (12, 13, 14)
    assert result.target_event_id == GameweekNumber(value=1)
    assert result.source_provider == "official_fpl_api"


def test_a2_authentication_rejection_is_typed_and_sanitized() -> None:
    http = FakeHttp({"/api/me/": Response({}, status_code=401)})
    with pytest.raises(ProviderAuthenticationError) as error:
        source(http=http).acquire(entry_id=ENTRY_ID, target_event=GameweekNumber(value=1))
    assert "secret" not in str(error.value)
    assert "Cookie" not in str(error.value)


def test_a3_source_unavailable_is_typed_without_fallback() -> None:
    http = FakeHttp({"/api/me/": Response({}, status_code=503)})
    with pytest.raises(ProviderUnavailableError):
        source(http=http).acquire(entry_id=ENTRY_ID, target_event=GameweekNumber(value=1))


@pytest.mark.parametrize("payload", [{}, {"player_id": "bad"}])
def test_a4_malformed_me_is_deterministic(payload: dict[str, Any]) -> None:
    http = valid_http()
    http.responses["/api/me/"] = Response(payload)
    with pytest.raises(ProviderDataError, match="malformed"):
        source(http=http).acquire(entry_id=ENTRY_ID, target_event=GameweekNumber(value=1))


def test_a5_manager_identity_mismatch_is_rejected() -> None:
    http = valid_http()
    http.responses["/api/me/"] = Response({"player_id": 99})
    with pytest.raises(ProviderDataError, match="does not match"):
        source(http=http).acquire(entry_id=ENTRY_ID, target_event=GameweekNumber(value=1))


@pytest.mark.parametrize(
    "bad_team",
    [
        {"id": ENTRY_ID, "event": 1},
        {"id": ENTRY_ID, "event": 1, "picks": [{"element": 1}]},
    ],
)
def test_a6_malformed_team_is_rejected(bad_team: dict[str, Any]) -> None:
    http = valid_http()
    http.responses[f"/api/my-team/{ENTRY_ID}/"] = Response(bad_team)
    with pytest.raises(ProviderDataError, match="malformed"):
        source(http=http).acquire(entry_id=ENTRY_ID, target_event=GameweekNumber(value=1))


def test_a7_bootstrap_missing_target_event_or_type_is_rejected() -> None:
    http = valid_http()
    http.responses["/api/bootstrap-static/"] = Response({"events": [], "elements": []})
    with pytest.raises(ProviderDataError, match="target event"):
        source(http=http).acquire(entry_id=ENTRY_ID, target_event=GameweekNumber(value=1))


def test_a8_only_approved_read_endpoints_are_called() -> None:
    http = valid_http()
    source(http=http).acquire(entry_id=ENTRY_ID, target_event=GameweekNumber(value=1))
    assert [endpoint for endpoint, _ in http.calls] == [
        "/api/me/",
        f"/api/my-team/{ENTRY_ID}/",
        "/api/bootstrap-static/",
    ]


def test_a9_secret_headers_do_not_enter_snapshot_or_errors() -> None:
    http = valid_http()
    result = source(http=http).acquire(entry_id=ENTRY_ID, target_event=GameweekNumber(value=1))
    rendered = repr(result)
    assert "session-secret" not in rendered
    assert "Bearer secret" not in rendered
    assert "Authorization" not in rendered


def test_a10_provider_shape_is_not_exposed_as_domain_fields() -> None:
    result = source(http=valid_http()).acquire(
        entry_id=ENTRY_ID,
        target_event=GameweekNumber(value=1),
    )
    assert isinstance(result.target_event_id, GameweekNumber)
    assert isinstance(result, object)
    assert not hasattr(result, "picks")
    assert not hasattr(result, "element_type")


def test_a11_process_local_secret_header_injection_allows_acquisition() -> None:
    http = valid_http()
    result = source(http=http).acquire(
        entry_id=ENTRY_ID,
        target_event=GameweekNumber(value=1),
    )

    assert result.manager_entry_id == ENTRY_ID
    assert all(headers["Cookie"] == "session-secret" for _, headers in http.calls)
    assert all(headers["Authorization"] == "Bearer secret" for _, headers in http.calls)
