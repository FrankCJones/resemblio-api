"""Tests for ``app.brand_names`` canonical-display map (L-7 Phase B fix).

Locked 2026-06-03 per Phase B of the library public-view TDD plan
(``projects/OptSus Team/cto-reviews/2026-06-03-resemblio-library-public-view-tdd-plan.md``).

The Phase A inspection caught "Openai" / "Read Cv" / "Are Na" rendering
on every brand-snapshot, hub card, and related chip for those brands
because ``library_indexer._brand_placeholder`` title-cased the slug
naively. The fix routes every brand-name surface through
``pretty_brand_name``; these tests pin the map's shape so a future
preset edit that drops a brand silently fails CI rather than ships the
wrong capitalisation.
"""
from __future__ import annotations

import pytest

from app.brand_names import (
    BRAND_NAMES_SCHEMA_VERSION,
    CANONICAL_BRAND_NAMES,
    CANONICAL_BRAND_SOURCE_URLS,
    has_canonical_entry,
    pretty_brand_name,
    source_url_for_brand,
)


# Slugs that ship in the v1 DRL seed corpus. Authored from the Phase A
# audit (``hub_1440.png`` enumeration); the test below asserts every one
# resolves to a canonical (non-title-case) entry. Adding a brand to the
# corpus requires adding it here AND to CANONICAL_BRAND_NAMES.
SEED_CORPUS_SLUGS: tuple[str, ...] = (
    "aeon",
    "aesop",
    "airtable",
    "apple",
    "are-na",
    "cloudflare",
    "craig-mod",
    "daring-fireball",
    "figma",
    "framer",
    "frank-chimero",
    "glossier",
    "gwern",
    "loom",
    "maggie-appleton",
    "openai",
    "patagonia",
    "pitch",
    "read-cv",
    "replit",
    "resend",
    "stripe",
    "the-markup",
    "the-pudding",
)

PHASE_J_SOURCE_URL_SLUGS: tuple[str, ...] = (
    "a24",
    "aeon",
    "aesop",
    "airtable",
    "anthropic",
    "apple",
    "are-na",
    "cloudflare",
    "craig-mod",
    "cursor",
    "daring-fireball",
    "figma",
    "framer",
    "frank-chimero",
    "github",
    "glossier",
    "gwern",
    "hugging-face",
    "linear",
    "locomotive",
    "loom",
    "maggie-appleton",
    "mailchimp",
    "notion",
    "olipop",
    "openai",
    "patagonia",
    "pentagram",
    "pitch",
    "quanta",
    "read-cv",
    "replit",
    "resend",
    "robin-sloan",
    "stripe",
    "substack",
    "the-markup",
    "the-pudding",
    "vercel",
    "webflow",
)

# Slugs whose canonical caps differ from naive title-case. Every entry
# is a regression case for L-7; the test asserts the pretty form matches
# the canonical and does NOT match the title-case shape.
CANONICAL_VS_TITLECASE: tuple[tuple[str, str, str], ...] = (
    # (slug, canonical, naive_titlecase_that_must_not_render)
    ("openai", "OpenAI", "Openai"),
    ("read-cv", "Read.cv", "Read Cv"),
    ("are-na", "Are.na", "Are Na"),
    ("openai-com", "OpenAI", "Openai Com"),
    ("github-com", "GitHub", "Github Com"),
    ("ebay-com", "eBay", "Ebay Com"),
)


# ---------------------------------------------------------------------------
# Schema-shape pins
# ---------------------------------------------------------------------------


def test_schema_version_locked() -> None:
    """Schema version is the v1 string downstream consumers key off."""
    assert BRAND_NAMES_SCHEMA_VERSION == "brand_names_v1"


def test_canonical_source_url_map_uses_real_dotted_urls() -> None:
    """Every Phase J production-corpus slug has a real public source URL."""
    from urllib.parse import urlparse

    for slug in PHASE_J_SOURCE_URL_SLUGS:
        url = source_url_for_brand(slug)
        parsed = urlparse(url)
        assert slug in CANONICAL_BRAND_SOURCE_URLS
        assert parsed.scheme in {"http", "https"}
        assert parsed.hostname is not None
        assert "." in parsed.hostname
        assert " " not in url

def test_canonical_map_keys_are_lowercase_dash_collapsed() -> None:
    """Every key matches the slug shape ``derive_brand_slug`` produces.

    Lowercase, digits, dashes; no leading/trailing dash; no spaces.
    Catches a future entry that ships a typo (uppercase, dot, space)
    that would silently miss the lookup.
    """
    import re

    slug_re = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    for key in CANONICAL_BRAND_NAMES:
        assert slug_re.match(key), f"non-slug-shape key: {key!r}"


def test_canonical_values_are_nonempty_strings() -> None:
    """No empty / whitespace-only display name slipped in."""
    for slug, name in CANONICAL_BRAND_NAMES.items():
        assert isinstance(name, str), f"{slug!r}: value is not str"
        assert name.strip(), f"{slug!r}: empty display name"


# ---------------------------------------------------------------------------
# Seed-corpus coverage gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", SEED_CORPUS_SLUGS)
def test_seed_corpus_brand_has_canonical_entry(slug: str) -> None:
    """Every v1 seed-corpus brand has an explicit canonical entry.

    A brand without an entry falls back to title-case at render time,
    which is the L-7 failure shape. This is the gate that prevents the
    failure from re-introducing silently when a new brand lands.
    """
    assert has_canonical_entry(slug), (
        f"seed-corpus brand {slug!r} missing from CANONICAL_BRAND_NAMES; "
        f"add an entry to ``app.brand_names`` in the same PR that adds "
        f"the brand to the seed corpus"
    )


# ---------------------------------------------------------------------------
# Render-shape correctness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("slug", "canonical", "wrong_titlecase"),
    CANONICAL_VS_TITLECASE,
)
def test_pretty_brand_name_emits_canonical_not_titlecase(
    slug: str, canonical: str, wrong_titlecase: str
) -> None:
    """For each known-bad slug, the function emits canonical caps.

    Three-part assert: canonical equals what we expect, naive title-case
    is NOT the output, output is non-empty. The third leg catches a
    future change that returns ``""`` for canonical-mapped slugs.
    """
    rendered = pretty_brand_name(slug)
    assert rendered == canonical, (
        f"slug {slug!r}: got {rendered!r}, want {canonical!r}"
    )
    assert rendered != wrong_titlecase, (
        f"slug {slug!r}: rendered as naive title-case {wrong_titlecase!r}; "
        f"L-7 regression"
    )
    assert rendered.strip(), f"slug {slug!r}: empty output"


def test_pretty_brand_name_unknown_slug_falls_back_to_titlecase() -> None:
    """Unknown slugs fall back to a humanize-then-title-case shape.

    Pins the safe-default contract: a brand that lands in the corpus
    before its canonical entry ships still renders SOMETHING readable
    rather than raising. The fallback is the only path the route can
    take in production for an unmapped slug.
    """
    assert pretty_brand_name("some-new-brand") == "Some New Brand"
    assert pretty_brand_name("acme") == "Acme"


def test_pretty_brand_name_empty_slug_returns_empty() -> None:
    """Empty slug returns empty string; caller chooses what to render.

    Defensive against a malformed metadata row carrying an empty
    brand_slug; the function must not raise.
    """
    assert pretty_brand_name("") == ""


def test_pretty_brand_name_is_idempotent_on_canonical() -> None:
    """Calling twice returns the same string; no hidden mutation."""
    for slug in SEED_CORPUS_SLUGS:
        first = pretty_brand_name(slug)
        second = pretty_brand_name(slug)
        assert first == second


# ---------------------------------------------------------------------------
# Cross-check against indexer placeholder path
# ---------------------------------------------------------------------------


def test_brand_placeholder_uses_canonical_brand_name() -> None:
    """``library_indexer._brand_placeholder`` emits canonical brand caps.

    End-to-end pin: the L-7 regression surface is the placeholder path,
    not the standalone brand_names module. This test asserts the
    integration: the placeholder resolves the brand display name through
    the canonical map (not naive title-case) for every slot that
    interpolates it.
    """
    from app.library_indexer import _brand_placeholder

    # Title slot for the OpenAI brand: previous bug emitted "Openai
    # design snapshot". Post-fix it must emit "OpenAI design snapshot".
    title = _brand_placeholder("title", brand_slug="openai")
    assert "OpenAI" in title, f"title={title!r}; expected canonical OpenAI"
    assert "Openai design snapshot" not in title, (
        f"title={title!r}; naive title-case form re-introduced (L-7 regression)"
    )

    # Wordmark slot: the same canonical name should surface.
    wordmark = _brand_placeholder("wordmark", brand_slug="read-cv")
    assert wordmark == "Read.cv", f"wordmark={wordmark!r}"

    # Are.na canonical caps.
    headline = _brand_placeholder("headline", brand_slug="are-na")
    assert "Are.na" in headline, f"headline={headline!r}"
