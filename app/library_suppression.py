"""Single source of truth for brand-slug suppression in the Resemblio library.

Why this module exists
----------------------
The seed pipeline (``scripts/seed_from_drl.py``) and the post-hoc reconciler
(``scripts/suppress_seed_brands.py``) must agree on which brand slugs are
suppressed. Keeping the list in one place prevents the two systems from drifting
apart - if a slug is added here it takes effect in both the seed (at insert time)
and the post-hoc script (for existing rows).

Adding a new suppressed slug
-----------------------------
Add the slug to ``SUPPRESSED_SLUGS`` below. No other files need updating:

1. On the next reseed, ``seed_from_drl.upsert_extraction`` and
   ``seed_from_drl.mine_and_persist_atoms_for_brand`` will write
   ``is_public=False`` for any new rows with that slug.
2. For rows already in the DB with the old value, run the post-hoc script::

       source .env && venv/bin/python scripts/suppress_seed_brands.py

What qualifies a slug for suppression
--------------------------------------
A slug belongs here when it is a DRL utility or shared-components slug that
produces library pages under a non-curated brand identity. The canonical example
is ``"shared"`` (the DRL ``_shared/`` directory), which seeds generic component
patterns that are not associated with any real brand and would appear misleadingly
in the public brand hub.

schema_version: library_suppression_v1
"""
from __future__ import annotations

schema_version = "library_suppression_v1"

#: Slugs that are DRL utility entries, not curated brands.
#: Use ``frozenset`` to prevent accidental in-place mutation.
#: This is the single source of truth for both the seed pipeline and the
#: post-hoc suppress_seed_brands.py reconciler.
SUPPRESSED_SLUGS: frozenset[str] = frozenset({
    "shared",
})


def is_brand_suppressed(slug: str) -> bool:
    """Return True if the given brand slug is in the suppression list.

    Safe to call with any string - falsy slugs (empty string, whitespace) return
    False so the seed pipeline does not suppress a brand due to a missing slug field.

    Args:
        slug: The brand slug to test (e.g. the ``system.get("slug")`` value from
            the DRL corpus, or a ``StrippedEntry.slug``).

    Returns:
        ``True`` when the slug is in ``SUPPRESSED_SLUGS``; ``False`` for any
        falsy input or any slug not explicitly listed.

    Edge cases:
        - Empty string: returns ``False`` (not in the suppression list).
        - Whitespace-only string: returns ``False`` (whitespace is not "shared").
        - A slug that used to be suppressed but was removed: returns ``False``.
    """
    if not slug:
        return False
    return slug in SUPPRESSED_SLUGS
