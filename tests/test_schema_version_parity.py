"""Stage 10 - paired endpoints share `schema_version`.

Closes failure-inventory item #10 from the back-on-track TDD plan
(`projects/OptSus Team/cto-reviews/2026-06-03-resemblio-back-on-track-tdd-plan.md`).

The original bug: `GET /v1/extractions` shipped with `schema_version=1`
while `GET /v1/extractions/{id}` shipped with `schema_version=2`. A
client pinning on the wrapper version could not tell the LIST and DETAIL
halves apart. The list-endpoint hygiene fix landed in extractions; this
test locks the invariant for every documented endpoint pair so the same
drift cannot reappear on any future paired surface.

Pairs covered:
- `extractions`: LIST `GET /v1/extractions` and DETAIL `GET /v1/extractions/{id}`
- `credit`: BALANCE `GET /v1/credit/balance` and LEDGER `GET /v1/credit/ledger`

Each row asserts the wrapper-level `schema_version` matches across both
halves. Item-level `schema_version` on LIST rows is verified in the
existing extractions LIST tests; this file is the pair-level guard.
"""
from __future__ import annotations

from typing import NamedTuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.conftest import auth_headers, seed_user


class EndpointPair(NamedTuple):
    """One LIST/DETAIL or balance/ledger pair under parity assertion.

    Attributes:
        name: Human label for the pair; surfaces in pytest IDs.
        first_path: The path of the first half of the pair (LIST or balance).
        second_path: The path of the second half (DETAIL or ledger). May
            be a format string with `{id}` placeholder; the test driver
            substitutes the resource id created during setup.
        needs_resource_id: Whether `second_path` requires a created
            resource id (DETAIL-style endpoint).
    """

    name: str
    first_path: str
    second_path: str
    needs_resource_id: bool


# Schema version constants are bumped together when an envelope evolves.
# Adding a new public pair? Add a row here and run the suite. The pair
# only ships when both halves agree on the wrapper-level version.
ENDPOINT_PAIRS: tuple[EndpointPair, ...] = (
    EndpointPair(
        name="extractions",
        first_path="/v1/extractions",
        second_path="/v1/extractions/{id}",
        needs_resource_id=True,
    ),
    EndpointPair(
        name="credit",
        first_path="/v1/credit/balance",
        second_path="/v1/credit/ledger",
        needs_resource_id=False,
    ),
)


def _create_extraction(client: TestClient, plaintext: str) -> int:
    """Seed one extraction so the DETAIL half of the extractions pair has a target."""
    response = client.post(
        "/v1/extractions",
        headers=auth_headers(plaintext),
        json={"url": "https://example.com"},
    )
    assert response.status_code == 200, response.text
    return int(response.json()["id"])


@pytest.mark.parametrize("pair", ENDPOINT_PAIRS, ids=[p.name for p in ENDPOINT_PAIRS])
def test_paired_endpoints_share_schema_version(
    client: TestClient,
    session: Session,
    pair: EndpointPair,
) -> None:
    """The wrapper `schema_version` matches across both halves of every pair.

    Asserts the failure mode that bit on 2026-06-02 (extractions LIST/DETAIL
    drift) cannot recur silently on this pair. If either half ships a
    contract-shape bump without the matching bump on the other half, this
    test fails and the PR is blocked.
    """
    _, _, plaintext = seed_user(session)
    headers = auth_headers(plaintext)
    resource_id: int | None = None
    if pair.needs_resource_id:
        resource_id = _create_extraction(client, plaintext)

    first_response = client.get(pair.first_path, headers=headers)
    if pair.needs_resource_id:
        assert resource_id is not None  # for type-checkers; setup guarantees it
        second_path = pair.second_path.format(id=resource_id)
    else:
        second_path = pair.second_path
    second_response = client.get(second_path, headers=headers)

    assert first_response.status_code == 200, first_response.text
    assert second_response.status_code == 200, second_response.text

    first_version = first_response.json().get("schema_version")
    second_version = second_response.json().get("schema_version")

    assert first_version is not None, f"{pair.first_path} missing schema_version"
    assert second_version is not None, f"{second_path} missing schema_version"
    assert first_version == second_version, (
        f"schema_version drift in {pair.name!r} pair: "
        f"{pair.first_path} returned {first_version!r}, "
        f"{second_path} returned {second_version!r}. "
        "Bump both halves together or neither."
    )
