"""Tests for Phase I Library token export normalization."""
from __future__ import annotations

import json

from app.library_token_exports import build_library_token_payload


def _dump_tokens(payload: dict[str, object]) -> str:
    """Return token JSON for simple substring assertions."""
    return json.dumps(payload["tokens"], sort_keys=True).lower()


def test_flat_drl_tokens_normalize_to_w3c_dtcg() -> None:
    """Flat DRL seed tokens become grouped DTCG leaves."""
    payload = build_library_token_payload(
        {
            "tokens": {
                "bg": "#ffffff",
                "color-a24-black": "#050505",
                "font_body": "Inter, sans-serif",
                "space_1": "4px",
                "radius_sm": "6px",
                "shadow_sm": "0 1px 2px rgb(0 0 0 / 0.1)",
            }
        },
        brand_slug="a24",
        source_url="https://a24films.com",
    )

    assert payload is not None
    assert payload["schema_version"] == "library_token_payload_v1"
    assert payload["token_schema"] == "w3c-dtcg"
    assert payload["tokens"]["color"]["bg"]["$value"] == "#ffffff"
    assert payload["tokens"]["color"]["black"]["$value"] == "#050505"
    assert payload["tokens"]["fontFamily"]["body"]["$value"] == "Inter, sans-serif"
    assert payload["tokens"]["dimension"]["space-1"]["$type"] == "dimension"
    assert payload["token_count"] == 6
    assert "a24" not in _dump_tokens(payload)


def test_nested_dtcg_payload_is_scrubbed_and_preserved() -> None:
    """Already grouped DTCG payloads keep safe leaves and drop unsafe ones."""
    payload = build_library_token_payload(
        {
            "color": {
                "primary": {"$value": "#ff3366", "$type": "color"},
                "resemblio-seed": {"$value": "#000000", "$type": "color"},
            },
            "fontFamily": {
                "display": {"$value": "Open Sans, sans-serif", "$type": "fontFamily"},
                "brand": {"$value": "Stripe Sans", "$type": "fontFamily"},
            },
            "schema_version": 1,
        },
        brand_slug="stripe-com",
        source_url="https://stripe.com",
    )

    assert payload is not None
    assert payload["tokens"]["color"]["primary"]["$value"] == "#ff3366"
    assert "resemblio" not in _dump_tokens(payload)
    assert "stripe" not in _dump_tokens(payload)
    assert "brand" not in payload["tokens"].get("fontFamily", {})


def test_empty_or_malformed_payload_fails_closed() -> None:
    """Malformed or empty input returns None instead of an empty export."""
    assert build_library_token_payload(None, brand_slug="a24", source_url="https://a24films.com") is None
    assert build_library_token_payload({}, brand_slug="a24", source_url="https://a24films.com") is None
    assert build_library_token_payload({"tokens": {"copy": "A24 presents"}}, brand_slug="a24", source_url="https://a24films.com") is None


def test_source_attribution_stays_outside_token_names() -> None:
    """Factual attribution is present in metadata, not token identifiers."""
    payload = build_library_token_payload(
        {"tokens": {"a24-accent": "#101010"}},
        brand_slug="a24",
        source_url="https://a24films.com",
    )

    assert payload is not None
    assert payload["source_attribution"] == {
        "source_url": "https://a24films.com",
        "inspired_by": "a24films.com",
    }
    assert "a24" not in _dump_tokens(payload)
