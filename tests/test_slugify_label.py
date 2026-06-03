"""Unit tests for ``library_indexer.slugify_version_label``.

The seed pipeline writes free-form labels to ``asset_versions.version_label``
(e.g. ``"DRL bootstrap 2026-05-21"``). The library indexer must persist a
URL-safe slug on ``library_pages.version_label`` so the
``/library/<brand>/<version>/...`` route resolves cleanly. These tests pin
that contract.

Run command (from ``code/api/``)::

    .venv/Scripts/python -m pytest tests/test_slugify_label.py -x
"""
from __future__ import annotations

import pytest

from app.library_indexer import slugify_version_label


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("DRL bootstrap 2026-05-21", "drl-bootstrap-2026-05-21"),
        ("2026-06", "2026-06"),
        ("  spaced  out  ", "spaced-out"),
        ("Mixed_CASE/Slashes!Punc", "mixed-case-slashes-punc"),
        ("---leading-trailing---", "leading-trailing"),
        ("already-slug", "already-slug"),
        ("multiple   spaces", "multiple-spaces"),
    ],
)
def test_slugify_version_label_happy_path(raw: str, expected: str) -> None:
    """Free-form labels collapse to lowercase dash-separated slugs."""
    assert slugify_version_label(raw) == expected


def test_slugify_version_label_none_passthrough() -> None:
    """``None`` (i.e. row has no version scope) passes through unchanged."""
    assert slugify_version_label(None) is None


def test_slugify_version_label_empty_yields_none() -> None:
    """An empty / all-punctuation string yields ``None`` rather than ``''``.

    Empty-string version labels are indistinguishable from "no version" at
    the route layer; returning ``None`` keeps the contract honest.
    """
    assert slugify_version_label("") is None
    assert slugify_version_label("---") is None
    assert slugify_version_label("!!! ###") is None


def test_slugify_output_matches_url_shape() -> None:
    """Output must match the slug-shape the web validator accepts.

    Mirrors ``library-data.ts > SLUG_SHAPE_RE``: lowercase alphanumeric
    plus dashes, no leading or trailing dash, 1-128 chars. Asserted here
    so the two contracts stay aligned across the TS/Python boundary.
    """
    import re

    shape = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?$")
    for raw in (
        "DRL bootstrap 2026-05-21",
        "2026-06",
        "Aeon Capital Q3",
        "release_v1.2.3",
    ):
        slug = slugify_version_label(raw)
        assert slug is not None
        assert shape.match(slug), f"slug {slug!r} from {raw!r} fails shape check"
