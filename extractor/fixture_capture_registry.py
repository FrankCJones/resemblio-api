"""Registry of brands whose button styles are captured from saved HTML fixtures.

Why this module exists
----------------------
Most Resemblio brands can be captured live by navigating to their homepage with
Playwright and calling ``capture_computed_styles(url=...)``. A small subset are
blocked by bot-detection walls that Playwright cannot pass:

- **aeon** (Vercel security-checkpoint): its saved fixture is the 33 KB challenge
  shell itself - no real DOM to select against. aeon lives in
  ``DOCUMENTED_SKIP_BRANDS`` in ``tests/test_button_corpus_coverage.py``.

- **openai** (Cloudflare Turnstile, confirmed 2026-06-06): returns HTTP 403 +
  challenge HTML to every headless request. BUT openai's saved fixture IS real
  419 KB SSR markup from before Turnstile was deployed. ``capture_computed_styles``
  can render it via ``page.set_content`` (zero network) and extract real CTA styles.

This registry is the structured home for brands in that second category:
**live-bot-walled AND real-markup fixture exists**. It is intentionally separate
from ``DOCUMENTED_SKIP_BRANDS`` because the two tiers are mutually exclusive:

| Tier | Real markup available? | Capture path |
|------|------------------------|--------------|
| fixture-capturable | Yes (saved fixture) | ``capture_computed_styles(html=fixture)`` |
| permanent skip | No (fixture IS the wall) | ``DOCUMENTED_SKIP_BRANDS`` |

Adding a brand here means:
1. A real-markup HTML fixture is committed at
   ``tests/fixtures/button_capture/<fixture_filename>``.
2. ``BRAND_SELECTOR_OVERRIDES[slug]["cta"]`` is set and proven correct against
   the fixture (see ``tests/test_button_selector_fixtures.py``).
3. ``scripts/capture_button_snapshot_from_fixture.py`` can generate + commit a
   real seed snapshot.

Revisit trigger: re-capture from the live URL when the brand drops its bot wall
or a real-browser/anti-bot capture pipeline exists. Until then, the committed
fixture snapshot is the source of record.

No new runtime or external dependency is introduced by this module.
"""
from __future__ import annotations

from pathlib import Path
from typing import Final, TypedDict


# ---------------------------------------------------------------------------
# Per-brand fixture-capture spec TypedDict
# ---------------------------------------------------------------------------


class FixtureCaptureSpec(TypedDict):
    """Structured metadata for one fixture-capturable brand.

    Fields:
    - fixture_filename: basename of the HTML fixture file under
      ``tests/fixtures/button_capture/``. Always relative; callers use
      ``fixture_path()`` to resolve to an absolute Path.
    - canonical_url: the brand's live homepage URL. Used as ``captured_url``
      in the snapshot envelope so downstream tooling knows where the markup
      originated, even though we are not hitting that URL at capture time.
    - fixture_captured_at: ISO 8601 date string (YYYY-MM-DD) of when the
      fixture HTML was first saved. Documents the snapshot's age for the
      next developer who needs to evaluate its freshness.
    - capture_reason: human-readable explanation of WHY live capture is
      unavailable. Must name the concrete wall (e.g. "Cloudflare Turnstile
      HTTP 403 to headless Playwright, confirmed 2026-06-06") so the next
      developer can determine whether the condition still applies.
    """

    fixture_filename: str
    canonical_url: str
    fixture_captured_at: str
    capture_reason: str


# ---------------------------------------------------------------------------
# Default fixture directory
# ---------------------------------------------------------------------------

# This file lives at <api_root>/extractor/fixture_capture_registry.py.
# The fixture dir is at <api_root>/tests/fixtures/button_capture/.
_API_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE_DIR: Final[Path] = _API_ROOT / "tests" / "fixtures" / "button_capture"
"""Default directory for fixture HTML files.

Tests may override via the ``fixture_dir`` argument to ``fixture_path()``.
"""

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

FIXTURE_CAPTURE_BRANDS: Final[dict[str, FixtureCaptureSpec]] = {
    "openai": FixtureCaptureSpec(
        fixture_filename="openai_homepage.html",
        canonical_url="https://openai.com",
        fixture_captured_at="2026-06-02",
        capture_reason=(
            "openai.com serves a Cloudflare Turnstile HTTP 403 challenge to "
            "headless Playwright (confirmed 2026-06-06; see STATUS.md entry "
            "'Section 7 openai re-capture ceremony: STOPPED'). The saved "
            "fixture is 419 KB real SSR markup captured before Turnstile was "
            "deployed; it contains the real CTA anchor and yields >= 4 "
            "non-default button fields via set_content. Revisit: re-capture "
            "live when openai drops Turnstile or a real-browser capture "
            "pipeline exists."
        ),
    ),
}
"""Brand slug -> FixtureCaptureSpec for brands that are live-bot-walled but
have a real-markup fixture on disk.

Mutually exclusive with ``DOCUMENTED_SKIP_BRANDS`` in
``tests/test_button_corpus_coverage.py``: a brand is either fixture-capturable
(real markup exists) or permanently skipped (no real markup anywhere). Overlap
is a test failure caught by
``TestFixtureCaptureRegistry.test_no_brand_in_both_registries``.
"""


# ---------------------------------------------------------------------------
# Path resolver
# ---------------------------------------------------------------------------


def fixture_path(slug: str, *, fixture_dir: Path | None = None) -> Path:
    """Return the absolute path to the HTML fixture for ``slug``.

    Args:
        slug: Brand slug present in ``FIXTURE_CAPTURE_BRANDS``. Raises
            ``KeyError`` if the slug is not registered.
        fixture_dir: Directory containing the fixture files. Defaults to
            ``DEFAULT_FIXTURE_DIR``. Pass an explicit path in tests to
            resolve against a custom fixture location.

    Returns:
        Absolute ``Path`` to ``<fixture_dir>/<fixture_filename>``.

    Raises:
        KeyError: when ``slug`` is not in ``FIXTURE_CAPTURE_BRANDS``.
    """
    spec = FIXTURE_CAPTURE_BRANDS[slug]  # raises KeyError for unknown slugs
    effective_dir = fixture_dir if fixture_dir is not None else DEFAULT_FIXTURE_DIR
    return effective_dir / spec["fixture_filename"]
