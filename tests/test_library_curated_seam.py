"""Phase 2 - Producer/consumer seam lock for curated metadata (D13).

Tests that the curated-metadata field set is single-sourced and that the
three ends of the seam (producer, reader, panel) cannot silently drift:

  1. ``_extract_curated_metadata`` reader unit tests: well-formed input,
     malformed/non-dict input (must not raise), partial input, empty input.
  2. ``CURATED_METADATA_FIELDS`` constant alignment: the named constant equals
     the exact 6 fields ``build_bundle`` writes and ``BrandMetadataPanel`` consumes.
  3. Endpoint integration: ``GET /v1/library/brands/{slug}`` surfaces curated
     keys when ``asset_versions.dtcg_json`` carries them, and omits them (key
     absent, not None) when it does not.
  4. Producer->reader round-trip: ``build_bundle`` dtcg_json feeds directly into
     ``_extract_curated_metadata`` so a key rename on either side fails a test.
     This is the D13 load-bearing guard - each end was previously pinned only to
     its own string literals; these tests bind the two ends together.
  5. CuratedMetadata TypedDict binding: the TypedDict's annotations must exactly
     match ``CURATED_METADATA_FIELDS`` so adding a field to one without the other
     fails here rather than silently at a mypy boundary no one runs.

Why this matters (D13): a field added to the producer but not the reader, or
added to the reader but not the panel, ships a dead field with no error. The
``CURATED_METADATA_FIELDS`` constant is the single source of truth; this test
enforces that adding a 7th field requires updating all three ends together.

Run: ``pytest tests/test_library_curated_seam.py -v``
"""
from __future__ import annotations

from typing import Any

import pytest

from app.routes.library import (
    CURATED_METADATA_FIELDS,
    CuratedMetadata,
    _extract_curated_metadata,
)
from scripts.seed_from_drl import build_bundle
from transformer import StrippedEntry


# ---------------------------------------------------------------------------
# Named constant alignment (D13) - the load-bearing seam test
# ---------------------------------------------------------------------------

# The 6 curated field names as understood by the producer (build_bundle) and
# the panel (BrandMetadataPanel). The constant under test must match exactly.
# Update this set ONLY when all three ends (producer, reader, panel) are
# updated together.
_EXPECTED_CURATED_FIELDS: frozenset[str] = frozenset({
    "tier",
    "category",
    "design_principles",
    "commercial_signal",
    "mood",
    "applicable_to",
})


class TestCuratedMetadataFieldsConstant:
    """CURATED_METADATA_FIELDS must equal the producer/panel field set exactly."""

    def test_constant_exists_and_is_frozenset(self) -> None:
        """``CURATED_METADATA_FIELDS`` is an importable frozenset from routes.library."""
        assert isinstance(CURATED_METADATA_FIELDS, frozenset), (
            "CURATED_METADATA_FIELDS must be a frozenset (immutable; guards against "
            "accidental mutation in test or application code)"
        )

    def test_constant_equals_expected_field_set(self) -> None:
        """The constant must list exactly the 6 curated fields.

        If this fails, a field was added to or removed from the constant without
        updating the corresponding seam test. Fix: update all three ends
        (build_bundle in seed_from_drl.py, _extract_curated_metadata in
        routes/library.py, BrandMetadataPanel props in BrandMetadataPanel.tsx)
        THEN update ``_EXPECTED_CURATED_FIELDS`` above.
        """
        assert CURATED_METADATA_FIELDS == _EXPECTED_CURATED_FIELDS, (
            f"CURATED_METADATA_FIELDS diverged from the expected set.\n"
            f"  In constant but not expected: {CURATED_METADATA_FIELDS - _EXPECTED_CURATED_FIELDS}\n"
            f"  In expected but not constant: {_EXPECTED_CURATED_FIELDS - CURATED_METADATA_FIELDS}"
        )

    def test_constant_covers_all_build_bundle_keys(self) -> None:
        """Every key build_bundle unconditionally writes must be in the constant.

        ``tier``, ``category``, ``mood``, ``applicable_to`` are always written
        (never conditional); ``design_principles`` and ``commercial_signal`` are
        conditional (only when system.json exists) but are still part of the seam.
        All six must be in the constant.
        """
        unconditional_keys = frozenset({"tier", "category", "mood", "applicable_to"})
        conditional_keys = frozenset({"design_principles", "commercial_signal"})
        all_producer_keys = unconditional_keys | conditional_keys
        assert CURATED_METADATA_FIELDS == all_producer_keys, (
            "CURATED_METADATA_FIELDS must cover all producer keys (both unconditional "
            "and conditional); the distinction is only in whether build_bundle omits "
            "the key when None - the seam contract covers all 6 regardless."
        )


# ---------------------------------------------------------------------------
# _extract_curated_metadata - reader unit tests
# ---------------------------------------------------------------------------


class TestExtractCuratedMetadata:
    """Unit tests for the route-layer reader function."""

    def test_returns_all_six_fields_from_well_formed_input(self) -> None:
        """All 6 curated fields parse correctly from a fully-populated dtcg_json."""
        dtcg: dict[str, Any] = {
            "schema_version": 1,
            "tier": "A",
            "category": "saas",
            "design_principles": ["confident", "minimal"],
            "commercial_signal": "product-led-growth",
            "mood": ["technical", "utilitarian"],
            "applicable_to": ["saas-marketing", "dev-tools"],
        }
        result = _extract_curated_metadata(dtcg)
        assert result["tier"] == "A"
        assert result["category"] == "saas"
        assert result["design_principles"] == ["confident", "minimal"]
        assert result["commercial_signal"] == "product-led-growth"
        assert result["mood"] == ["technical", "utilitarian"]
        assert result["applicable_to"] == ["saas-marketing", "dev-tools"]

    def test_result_contains_exactly_the_present_fields(self) -> None:
        """Only keys with usable values appear in the result; absent keys are not added.

        This is the D13 invariant on the reader side: a missing key on the wire
        means 'not available for this brand'; web consumers treat a missing key
        the same as None and degrade gracefully (the panel omits the row).
        """
        dtcg: dict[str, Any] = {
            "tier": "A",
            "mood": ["editorial"],
        }
        result = _extract_curated_metadata(dtcg)
        assert set(result.keys()) == {"tier", "mood"}
        assert "category" not in result
        assert "design_principles" not in result
        assert "commercial_signal" not in result
        assert "applicable_to" not in result

    def test_returns_empty_dict_on_non_dict_input(self) -> None:
        """Non-dict input (None, str, list, int) must return {} without raising.

        A single bad row in the DB must never 500 the brand page. The route calls
        _extract_curated_metadata on every brand page render; raising here would
        take down the page for every visitor.
        """
        for bad_input in (None, "string", [], 42, False):
            result = _extract_curated_metadata(bad_input)
            assert result == {}, (
                f"Non-dict input {bad_input!r} must return {{}}; got {result!r}"
            )

    def test_returns_empty_dict_on_empty_dict_input(self) -> None:
        """An empty dict (pre-Phase-3 organic extraction) yields an empty result."""
        result = _extract_curated_metadata({})
        assert result == {}

    def test_skips_empty_string_tier(self) -> None:
        """An empty or whitespace-only tier is treated as absent.

        The producer (build_bundle) always writes a non-empty tier from corpus.json,
        but an organic extraction or a corrupted row could carry ''. _clean_str
        catches this; the key must not appear in the result.
        """
        result = _extract_curated_metadata({"tier": "   "})
        assert "tier" not in result, "whitespace-only tier must be treated as absent"

    def test_skips_non_string_values_in_list_fields(self) -> None:
        """Non-string list members are dropped; the key is still present with a filtered list.

        _clean_str_list keeps only str members. An all-non-string list becomes [].
        An all-str list passes through unmodified.
        """
        dtcg: dict[str, Any] = {
            "mood": ["confident", 42, None, "minimal"],
        }
        result = _extract_curated_metadata(dtcg)
        assert result["mood"] == ["confident", "minimal"]

    def test_list_field_present_as_empty_list_when_list_is_all_non_strings(self) -> None:
        """A list of all non-string members collapses to [] (key present, value []).

        This is semantically distinct from a missing key ('not available'):
        the key IS present, the values are just not renderable. The panel's
        ``presentList`` guard filters [] -> absent at render time.
        """
        dtcg: dict[str, Any] = {"mood": [42, None, True]}
        result = _extract_curated_metadata(dtcg)
        assert "mood" in result
        assert result["mood"] == []

    def test_non_list_value_for_list_field_is_skipped(self) -> None:
        """A scalar where a list is expected (wrong producer type) is treated as absent."""
        result = _extract_curated_metadata({"design_principles": "not-a-list"})
        assert "design_principles" not in result

    def test_result_keys_are_a_subset_of_curated_fields_constant(self) -> None:
        """No undocumented key may appear in the result.

        This guards against future code accidentally writing an unexpected key
        into the LibraryPageData envelope via the update() call in _page_to_data.
        """
        dtcg: dict[str, Any] = {
            "tier": "A",
            "category": "saas",
            "design_principles": ["confident"],
            "commercial_signal": "plg",
            "mood": ["technical"],
            "applicable_to": ["saas"],
            "undocumented_field": "should be ignored",
        }
        result = _extract_curated_metadata(dtcg)
        unexpected = set(result.keys()) - CURATED_METADATA_FIELDS
        assert not unexpected, (
            f"Undocumented keys in result: {unexpected}. "
            "Only CURATED_METADATA_FIELDS may appear in the curated-metadata output."
        )

    def test_result_keys_match_curated_fields_constant_on_full_input(self) -> None:
        """A fully-populated dtcg_json yields all 6 keys and exactly those 6 keys."""
        dtcg: dict[str, Any] = {
            "tier": "A",
            "category": "saas",
            "design_principles": ["confident"],
            "commercial_signal": "plg",
            "mood": ["technical"],
            "applicable_to": ["saas"],
        }
        result = _extract_curated_metadata(dtcg)
        assert set(result.keys()) == CURATED_METADATA_FIELDS


# ---------------------------------------------------------------------------
# Endpoint integration (GET /v1/library/brands/{slug})
# ---------------------------------------------------------------------------


class TestLibraryBrandPageCuratedFields:
    """Endpoint-level integration: curated fields surface/absent correctly."""

    def test_brand_page_surfaces_curated_fields_when_dtcg_json_carries_them(
        self,
        client: Any,  # pytest fixture injected by conftest.py
        seed_library_page: Any,  # pytest fixture injected by conftest.py
    ) -> None:
        """When asset_versions.dtcg_json carries the 6 curated fields, the page
        endpoint surfaces them in the response body.

        This is the producer->route->endpoint path:
          dtcg_json carries tier/category/design_principles/commercial_signal/mood/applicable_to
          -> _extract_curated_metadata reads them
          -> _page_to_data merges them into LibraryPageData
          -> the endpoint serialises them into the JSON response
        """
        brand_slug, _page = seed_library_page(
            brand_slug="seam-brand",
            dtcg_json={
                "schema_version": 1,
                "tier": "A",
                "category": "saas",
                "design_principles": ["confident", "minimal"],
                "commercial_signal": "product-led-growth",
                "mood": ["technical"],
                "applicable_to": ["dev-tools"],
            },
        )
        resp = client.get(f"/v1/library/brands/{brand_slug}")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["tier"] == "A"
        assert data["category"] == "saas"
        assert data["design_principles"] == ["confident", "minimal"]
        assert data["commercial_signal"] == "product-led-growth"
        assert data["mood"] == ["technical"]
        assert data["applicable_to"] == ["dev-tools"]

    def test_brand_page_omits_curated_fields_when_dtcg_json_is_pre_phase3(
        self,
        client: Any,
        seed_library_page: Any,
    ) -> None:
        """When asset_versions.dtcg_json lacks the curated fields (pre-Phase-3 row),
        the page endpoint omits those keys entirely (key absent, not None/null).

        Web consumers check 'key in data' (not 'data.key !== null') to determine
        whether to show the panel. A null value would behave differently from an
        absent key in the TypeScript optional-field contract.
        """
        brand_slug, _page = seed_library_page(
            brand_slug="pre-phase3-brand",
            dtcg_json={
                "schema_version": 1,
                "slug": "pre-phase3-brand",
                "tokens": {},
            },
        )
        resp = client.get(f"/v1/library/brands/{brand_slug}")
        assert resp.status_code == 200
        data = resp.json()["data"]
        for field in CURATED_METADATA_FIELDS:
            assert field not in data, (
                f"Curated field '{field}' must be absent (not None) when dtcg_json "
                f"lacks it; web consumers use 'key in data' to degrade gracefully."
            )


# ---------------------------------------------------------------------------
# Phase A - Producer<->reader round-trip seam guard (D13; prove-it-bites)
# ---------------------------------------------------------------------------

# Synthetic StrippedEntry shared across both round-trip tests.
_STRIPPED_A = StrippedEntry(
    source_id="test-system/alphabets/test-brand",
    slug="test-brand",
    cls="alphabets",
    kind="alphabet",
    tldr="Test brand for seam guard.",
    patterns=("serif-display-sans-body",),
    mood=("editorial", "warm"),
    applicable_to=("editorial-publication",),
    tags=("alphabets", "warm"),
    provenance_score="A",
    tier="A",
    category="editorial-publication",
)


class TestBuildBundleRoundTrip:
    """Producer->reader round-trip seam guard (D13).

    These tests bind ``build_bundle`` (the producer in seed_from_drl.py) to
    ``_extract_curated_metadata`` (the reader in routes/library.py) so that
    renaming a key in either location fails here rather than silently shipping
    a dead field. Each test feeds ``bundle.dtcg_json`` directly into the reader
    and asserts the expected keys come back - no network, no DB, no DRL disk
    access required.

    Prove-it-bites (D13, 2026-06-10): the two tests below were confirmed RED
    when ``"tier"`` was temporarily renamed to ``"tier_grade"`` in
    ``build_bundle``'s ``dtcg_json`` dict (the reader saw no ``"tier"`` key and
    the assertion ``result["tier"] == "A"`` / ``assert "tier" in result.keys()``
    raised KeyError / AssertionError). The rename was then reverted; the tests
    are GREEN against the current production code.
    """

    def test_build_bundle_output_round_trips_through_reader(self) -> None:
        """build_bundle dtcg_json round-trips through _extract_curated_metadata for all 6 fields.

        Passes BOTH conditional fields (design_principles + commercial_signal) to
        ``build_bundle`` and asserts the reader surfaces all 6 curated keys with the
        producer's values. If a key is renamed in ``build_bundle`` but not in
        ``_extract_curated_metadata`` (or vice versa), one of the assertions below
        will fail, surfacing the seam break before it reaches prod.
        """
        tokens: dict[str, str] = {"ds-bg": "#0A0908", "ds-text": "#F5F1EA"}
        bundle = build_bundle(
            _STRIPPED_A,
            tokens,
            design_principles=["clarity", "restraint"],
            commercial_signal="subscription-media",
        )
        result = _extract_curated_metadata(bundle.dtcg_json)

        assert result["tier"] == "A"
        assert result["category"] == "editorial-publication"
        assert result["mood"] == ["editorial", "warm"]
        assert result["applicable_to"] == ["editorial-publication"]
        assert result["design_principles"] == ["clarity", "restraint"]
        assert result["commercial_signal"] == "subscription-media"
        assert set(result.keys()) == CURATED_METADATA_FIELDS

    def test_build_bundle_omitted_conditionals_round_trip(self) -> None:
        """build_bundle with design_principles=None, commercial_signal=None yields exactly 4 keys.

        Proves the "omit, do not null" contract end to end: when the optional
        fields are absent from the producer call (system.json not found for the
        brand), the reader must return exactly the 4 unconditional keys and must
        NOT include design_principles or commercial_signal in the result.
        """
        tokens: dict[str, str] = {}
        bundle = build_bundle(
            _STRIPPED_A,
            tokens,
            design_principles=None,
            commercial_signal=None,
        )
        result = _extract_curated_metadata(bundle.dtcg_json)

        assert set(result.keys()) == {"tier", "category", "mood", "applicable_to"}
        assert "design_principles" not in result
        assert "commercial_signal" not in result
        assert result["tier"] == "A"
        assert result["category"] == "editorial-publication"
        assert result["mood"] == ["editorial", "warm"]
        assert result["applicable_to"] == ["editorial-publication"]


# ---------------------------------------------------------------------------
# Phase B - CuratedMetadata TypedDict binding (prove-it-bites)
# ---------------------------------------------------------------------------


class TestCuratedMetadataTypeDict:
    """CuratedMetadata TypedDict must declare exactly the constant's fields.

    Binds the type-hint surface to CURATED_METADATA_FIELDS so a field added to
    one but not the other fails here, not silently at a mypy boundary no one
    runs.

    Prove-it-bites (D13, 2026-06-10): the test below was confirmed RED when the
    ``mood`` field was temporarily deleted from ``CuratedMetadata``'s annotations
    (the set assertion raised AssertionError with ``mood`` appearing in
    CURATED_METADATA_FIELDS but not in the TypedDict). The deletion was reverted;
    the test is GREEN against the current production code.
    """

    def test_curated_metadata_typeddict_fields_match_constant(self) -> None:
        """CuratedMetadata must declare exactly the constant's fields.

        Binds the type-hint surface to CURATED_METADATA_FIELDS so a field added to
        one but not the other fails here, not silently at a mypy boundary no one
        runs.
        """
        assert set(CuratedMetadata.__annotations__.keys()) == CURATED_METADATA_FIELDS
