"""Trademark-strip transformer: DRL corpus row to Resemblio-shape entry.

One-way pipeline from the Design Reference Library (read-only upstream) to
Resemblio's internal corpus. This module never writes back to the DRL.

The DRL's ``corpus.json`` carries trademark-bearing assets (system slug
``"anthropic"``, ``"a24"``, etc.). Resemblio's public corpus is
brand-faithful but trademark-stripped: wordmarks, logos, and brand-name
attribution move into private seed metadata that is not part of the API
response surface, while the underlying design language (colours, type,
spacing, scale, component patterns) is preserved as the inspired-by
starting point public copy frames as *inspirado, no copiado*. The
public-facing slug is the asset's neutral class+slug combination.

Stripping rules (v1):

1. The brand-bearing system slug is moved from a public identifier to private
   provenance recorded only via ``seed_source`` + ``source_id`` on the
   ``extractions`` row.
2. The asset's ``tldr``, ``patterns``, ``mood``, ``applicable_to``, and
   ``tags`` are preserved (they describe design behaviour, not brand).
3. Any tag that is the brand's literal name (matched case-insensitively
   against ``system_slug`` / ``system_name``) is dropped.
4. ``provenance_score`` is preserved as a quality grade.

This is sufficient for the v1 seed where the deliverable is the token set
plus DTCG JSON; the richer four-file bundle strip (HTML lorem-ipsum, CSS
class renaming) is deferred to v1.1 when the extraction service ships those
artifacts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypedDict

from .schema import SCHEMA_VERSION as STRIPPED_SCHEMA_VERSION

__all__ = [
    "DrlAssetRow",
    "DrlSystemRow",
    "StrippedEntry",
    "brand_strip",
    "STRIPPED_SCHEMA_VERSION",
]


class DrlAssetRow(TypedDict, total=False):
    """One asset row inside a DRL ``corpus.json`` system entry.

    ``total=False`` because authored DRL rows do not all carry every field
    (e.g. ``tldr`` and ``patterns`` are absent on partials). The fields used
    by the brand strip are listed here; unknown extra keys are tolerated and
    dropped silently.
    """

    slug: str
    cls: str  # mapped from JSON ``"class"`` by ``brand_strip``
    kind: str
    path: str
    tokens_path: str
    tldr: str
    patterns: list[str]
    mood: list[str]
    applicable_to: list[str]
    tags: list[str]
    provenance_score: str


class DrlSystemRow(TypedDict, total=False):
    """The parent system metadata for a DRL asset."""

    slug: str
    name: str
    tier: str
    category: str


@dataclass(frozen=True)
class StrippedEntry:
    """Brand-stripped representation suitable for Resemblio's internal corpus.

    The public-facing fields (``slug``, ``cls``, ``kind``, ``tldr``,
    ``patterns``, ``mood``, ``applicable_to``, ``tags``) carry no
    trademark identifier. Provenance lives only in the private
    ``source_id`` field, which the seeder stores on the ``extractions``
    row alongside ``seed_source="drl_v1"``.
    """

    source_id: str
    slug: str
    cls: str
    kind: str
    tldr: str
    patterns: tuple[str, ...]
    mood: tuple[str, ...]
    applicable_to: tuple[str, ...]
    tags: tuple[str, ...]
    provenance_score: str
    tier: str
    category: str
    schema_version: int = field(default=STRIPPED_SCHEMA_VERSION)


def brand_strip(system: DrlSystemRow | dict[str, Any], asset: DrlAssetRow | dict[str, Any]) -> StrippedEntry:
    """Trademark-strip a DRL ``(system, asset)`` pair to a ``StrippedEntry``.

    Function name is preserved for back-compat with existing call sites;
    the operation is the trademark / brand-name strip described in the
    module docstring (design language is preserved; only the
    trademark-bearing identifiers are stripped).

    Drops any tag whose lower-case form matches the system's slug or name.
    Composes ``source_id`` as ``"<system_slug>/<asset_class>/<asset_slug>"``
    so that ``(seed_source, source_id)`` is globally unique per DRL asset.

    Args:
        system: DRL system row from ``corpus.json`` (carries ``slug``,
            ``name``, ``tier``, ``category``).
        asset: DRL asset row from the system's ``assets`` list. The JSON
            field ``"class"`` is a reserved Python keyword and is mapped to
            ``cls`` internally.

    Returns:
        A frozen ``StrippedEntry``. The original DRL dicts are not mutated.
    """
    system_slug = str(system.get("slug", "")).strip()
    system_name = str(system.get("name", "")).strip()
    asset_class = str(asset.get("class") or asset.get("cls") or "").strip()
    asset_slug = str(asset.get("slug", "")).strip()

    if not system_slug or not asset_class or not asset_slug:
        raise ValueError(
            f"DRL row missing required identifiers: system={system_slug!r}, "
            f"class={asset_class!r}, slug={asset_slug!r}"
        )

    brand_tokens = {token for token in (system_slug.lower(), system_name.lower()) if token}
    stripped_tags = tuple(
        tag for tag in asset.get("tags", []) or () if tag.lower() not in brand_tokens
    )

    return StrippedEntry(
        source_id=f"{system_slug}/{asset_class}/{asset_slug}",
        slug=asset_slug,
        cls=asset_class,
        kind=str(asset.get("kind", "")),
        tldr=str(asset.get("tldr", "")),
        patterns=tuple(asset.get("patterns", []) or ()),
        mood=tuple(asset.get("mood", []) or ()),
        applicable_to=tuple(asset.get("applicable_to", []) or ()),
        tags=stripped_tags,
        provenance_score=str(asset.get("provenance_score", "")),
        tier=str(system.get("tier", "")),
        category=str(system.get("category", "")),
    )
