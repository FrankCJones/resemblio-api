"""Canonical brand-name display mapping for library pages.

Locked 2026-06-03 per Phase B of the library public-view TDD plan
(``projects/OptSus Team/cto-reviews/2026-06-03-resemblio-library-public-view-tdd-plan.md``)
to close BLOCKER L-7 surfaced in Phase A inspection: brand slugs were
title-cased at render time, producing "Openai" / "Read Cv" / "Are Na"
where the brand's canonical typography is "OpenAI" / "Read.cv" / "Are.na".

Two contracts live here:

1. ``CANONICAL_BRAND_NAMES`` - the slug -> canonical display map. Authored
   from the 24-brand DRL seed corpus inspected during Phase A. Adding a new
   brand to the corpus REQUIRES a new entry here when the canonical caps
   differ from naive title-case; otherwise the page renders the brand's
   own name wrong on day one, which is the failure shape this module exists
   to prevent.

2. ``pretty_brand_name(slug)`` - the single function every render path
   calls. It looks up the slug in the canonical map, and on a miss falls
   back to a naive humanize-then-title-case. The fallback is the safe
   default for unknown brands; the assertion is that every brand in the
   seed corpus has a canonical entry so the fallback never fires in
   production for known brands.

Schema-version tag: ``brand_names_v1``. Bump if the dict shape changes
(e.g. adds locale variants); the value strings are free-form display copy
and do not need a version bump on edit.

Run command (from ``code/api/``)::

    pytest tests/test_brand_names.py -q
"""
from __future__ import annotations


BRAND_NAMES_SCHEMA_VERSION = "brand_names_v1"
"""Schema-version tag stamped onto any downstream snapshot of this map.

Downstream consumers (route handlers, indexer presets, OG-image renderers)
key off the version string for shape detection; value edits do not bump it.
"""


CANONICAL_BRAND_NAMES: dict[str, str] = {
    # 24-brand DRL seed corpus, Phase A audit 2026-06-03.
    "aeon": "Aeon",
    "aesop": "Aesop",
    "airtable": "Airtable",
    "apple": "Apple",
    "are-na": "Are.na",
    "cloudflare": "Cloudflare",
    "craig-mod": "Craig Mod",
    "daring-fireball": "Daring Fireball",
    "figma": "Figma",
    "framer": "Framer",
    "frank-chimero": "Frank Chimero",
    "glossier": "Glossier",
    "gwern": "Gwern",
    "loom": "Loom",
    "maggie-appleton": "Maggie Appleton",
    "openai": "OpenAI",
    "patagonia": "Patagonia",
    "pitch": "Pitch",
    "read-cv": "Read.cv",
    "replit": "Replit",
    "resend": "Resend",
    "stripe": "Stripe",
    "the-markup": "The Markup",
    "the-pudding": "The Pudding",
    # Common organic-row slug shapes (domain-stripped). Kept here so that
    # an organic row indexed before its brand lands in the curated corpus
    # still paints canonical caps. ``derive_brand_slug`` collapses domains
    # with subdomain segments (e.g. ``shop.example.com`` -> ``shop-example-com``)
    # so the keys mirror that shape.
    "openai-com": "OpenAI",
    "stripe-com": "Stripe",
    "github-com": "GitHub",
    "ebay-com": "eBay",
    "wework-com": "WeWork",
    "youtube-com": "YouTube",
    "linkedin-com": "LinkedIn",
}
"""Slug to canonical display-name map.

Keys are the lowercase, dash-collapsed brand slugs produced by
``library_indexer.derive_brand_slug``. Values are the brand's canonical
typographic spelling for the user-facing page. Order is alphabetical for
review-time legibility; ordering is not load-bearing.

When adding a new brand to the corpus: the entry MUST land here in the
same PR. The unit-test ``test_seed_corpus_brands_have_canonical_entry``
fails for any seed slug missing from this dict, so the omission cannot
ship silently.
"""


def pretty_brand_name(brand_slug: str) -> str:
    """Return the brand's canonical display name for a library page.

    Lookup order:

    1. Direct hit on ``CANONICAL_BRAND_NAMES``. The common path; covers
       every seed-corpus brand and every domain-collapsed organic-row
       slug we have seen in production.
    2. Fallback: humanize-then-title-case (dash-to-space + ``.title()``).
       Identical to the pre-fix behaviour at ``library_indexer.
       _brand_placeholder`` line 607; preserved as the safe default so
       this function is always pure-data and never raises on an unknown
       slug.

    Defensive on inputs:

    - Empty string returns empty string (caller decides what to render).
    - None is coerced to empty string before the title-case fallback so
      a NULL slug from a malformed metadata row does not raise.

    Args:
        brand_slug: Lowercase, dash-collapsed brand slug. Shape produced
            by ``app.library_indexer.derive_brand_slug``. May arrive as
            an empty string from a malformed row; never raises.

    Returns:
        The canonical display-name string. Pure-data; no I/O.

    Examples:
        >>> pretty_brand_name("openai")
        'OpenAI'
        >>> pretty_brand_name("are-na")
        'Are.na'
        >>> pretty_brand_name("some-new-brand")
        'Some New Brand'
        >>> pretty_brand_name("")
        ''
    """
    if not brand_slug:
        return ""
    canonical = CANONICAL_BRAND_NAMES.get(brand_slug)
    if canonical is not None:
        return canonical
    return brand_slug.replace("-", " ").title()


def has_canonical_entry(brand_slug: str) -> bool:
    """Return True when ``brand_slug`` has an explicit canonical entry.

    Used by the seed-corpus assertion test to enumerate gaps without
    relying on string comparison against the fallback's output.
    """
    return brand_slug in CANONICAL_BRAND_NAMES
