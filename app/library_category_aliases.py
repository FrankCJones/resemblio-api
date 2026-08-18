"""Shared category-slug aliases for DRL corpus classes and public routes.

The DRL corpus sometimes names asset classes as plural collection names
(``alphabets``, ``heroes``), while the public library route vocabulary is
driven by the DRL template names used by the indexer (``alphabet``, ``hero``).
This module is the single place that reconciles those two vocabularies.
"""
from __future__ import annotations

DRL_COMPONENT_MARKER = 'data-rs-source="drl-component"'
"""Article marker emitted only for rendered HTML sourced from a real DRL asset."""

DRL_TO_PUBLIC_CATEGORY_SLUG: dict[str, str] = {
    "alphabets": "alphabet",
    "article-layouts": "article-layout",
    "cta-blocks": "cta-block",
    "feature-grids": "feature-grid",
    "footers": "footer",
    "forms": "form-fields",
    "heroes": "hero",
    "libraries": "library",
    "news-lists": "news-list",
    "pricing-tables": "pricing-table",
}
"""Known corpus-class to public-route aliases.

Only aliases whose target is an existing public template class belong here.
Classes with no template, such as token-scale categories, intentionally stay
unmapped until a renderer exists for them.
"""


def canonical_public_category_slug(category_slug: str | None) -> str | None:
    """Return the public template slug for a DRL category/class slug.

    Args:
        category_slug: Raw DRL class or public route slug.

    Returns:
        The public route/template slug when an alias is known, otherwise the
        original value. ``None`` is preserved for legacy callers.
    """
    if category_slug is None:
        return None
    return DRL_TO_PUBLIC_CATEGORY_SLUG.get(category_slug, category_slug)


def category_lookup_slugs(category_slug: str) -> tuple[str, ...]:
    """Return ordered category slugs to try for a public category request.

    The requested slug is always first so canonical public routes preserve
    their current behavior. A known DRL corpus alias is second, letting the API
    serve old oracle-style requests such as ``alphabets`` from the canonical
    ``alphabet`` row without changing sitemap canonicalization.
    """
    canonical = canonical_public_category_slug(category_slug)
    if canonical is None or canonical == category_slug:
        return (category_slug,)
    return (category_slug, canonical)
