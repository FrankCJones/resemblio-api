"""Voice-rule test for the vendored transformer module's docs.

Phase 0 carry-forward (2026-06-04 Inspirado correction plan), YELLOW
item 4: the transformer module's docstring and README must no longer
read as "produces brand-stripped versions" -- the corrected framing
locked 2026-06-04 is trademark-stripped, brand-faithful output
(inspirado, no copiado). The function name ``brand_strip`` is preserved
for back-compat with call sites, but the prose around it must reflect
the new framing so future readers do not re-anchor on "brand-stripped"
as the product mental model.

Two surfaces are pinned:

1. ``transformer/__init__.py`` -- the module docstring (what
   ``help(transformer)`` and IDE tooltips show).
2. ``transformer/README.md`` -- repo browsing surface.

Run command (from ``code/api/``):

    pytest tests/test_transformer_docs_framing.py
"""
from __future__ import annotations

import re
from pathlib import Path

# Repo paths (relative to this test file).
HERE = Path(__file__).resolve().parent
API_ROOT = HERE.parent
TRANSFORMER_DIR = API_ROOT / "transformer"

# Banned framings, kept in lock-step with the web app's library-copy.ts.
# Note we do NOT ban the bare phrase "brand-strip" because the function
# name and its in-code references are intentionally preserved; we ban
# the prose framings ("brand-stripped", "stripped of brand", "brand
# removed") that drive the public mental model.
BANNED_FRAMINGS = (
    re.compile(r"brand[-\s]stripped", re.IGNORECASE),
    re.compile(r"stripped of brand", re.IGNORECASE),
    re.compile(r"brand removed", re.IGNORECASE),
)

# Prose must positively assert the new framing.
REFRAME_ANCHOR = re.compile(
    r"inspirado|brand[-\s]faithful|trademark[-\s]stripped", re.IGNORECASE
)


def _read(rel: str) -> str:
    return (TRANSFORMER_DIR / rel).read_text(encoding="utf-8")


def _module_docstring() -> str:
    """Extract the top-of-module docstring from ``__init__.py``.

    Pulled by static parse rather than ``import transformer`` so the
    test can run in environments where the module's runtime dependencies
    are not installed.
    """
    source = _read("__init__.py")
    match = re.match(r'^\s*"""(.*?)"""', source, re.DOTALL)
    assert match is not None, "transformer/__init__.py must start with a docstring"
    return match.group(1)


def test_init_docstring_has_no_banned_framing() -> None:
    """Module-level docstring is the top-of-tooltip mental-model anchor."""
    docstring = _module_docstring()
    for banned in BANNED_FRAMINGS:
        assert not banned.search(docstring), (
            f"transformer/__init__.py docstring must not match {banned.pattern!r}; "
            f"got docstring: {docstring!r}"
        )


def test_init_docstring_reflects_inspirado_reframe() -> None:
    """Positive assertion: the new framing must be present in the docstring."""
    docstring = _module_docstring()
    assert REFRAME_ANCHOR.search(docstring), (
        f"transformer docstring should carry the Inspirado reframe; "
        f"got docstring: {docstring!r}"
    )


def test_readme_has_no_banned_framing() -> None:
    """Transformer README is the repo-browsing surface for the module."""
    readme = _read("README.md")
    for banned in BANNED_FRAMINGS:
        assert not banned.search(readme), (
            f"transformer/README.md must not match {banned.pattern!r}"
        )


def test_readme_reflects_inspirado_reframe() -> None:
    """Positive assertion: the README must include the new framing."""
    readme = _read("README.md")
    assert REFRAME_ANCHOR.search(readme), (
        "transformer/README.md should carry the Inspirado reframe"
    )
