"""Visual harness capture plan builder.

Turns a list of brand slugs into a deterministic, exhaustive capture plan
(one CaptureTarget per brand x surface x viewport). The plan is the
single source of truth for "what must be photographed" - the harness
(capture_harness.py) consumes it, the visual fidelity gate (0.C) is
parameterised by it, and the contact-sheet builder (contact_sheet.py)
uses it to detect missing captures.

Decision reference: D16 (pixel proof is the readiness definition) in
projects/OptSus Team/missions/resemblio-library-public-view-readiness-tdd-plan-v5.md

Schema: capture_plan_v1
"""
from __future__ import annotations

from enum import Enum
from typing import NamedTuple, Sequence


# ---------------------------------------------------------------------------
# Viewport constants (D16: two canonical viewport sizes)
# ---------------------------------------------------------------------------


class Viewport(NamedTuple):
    """A named capture viewport.

    Attributes:
        label:  Short identifier used in filenames and reports ("desktop",
                "mobile"). Must be slug-safe (alphanumeric + hyphens only).
        width:  Viewport width in CSS pixels.
        height: Viewport height in CSS pixels.
    """

    label: str
    width: int
    height: int


#: Desktop viewport - 1440 x 900 per D16 harness spec.
DESKTOP_VIEWPORT = Viewport(label="desktop", width=1440, height=900)

#: Mobile viewport - 390 x 844 (iPhone 14 Pro natural resolution).
MOBILE_VIEWPORT = Viewport(label="mobile", width=390, height=844)

#: Canonical ordered list used by build_capture_plan; order determines
#: the iteration order in the output plan.
ALL_VIEWPORTS: tuple[Viewport, ...] = (DESKTOP_VIEWPORT, MOBILE_VIEWPORT)


# ---------------------------------------------------------------------------
# Surface constants (D16: landing page + specimen/alphabet page)
# ---------------------------------------------------------------------------


class Surface(Enum):
    """A named page surface within the Resemblio library.

    Each enum value carries a url_path_template (formatted with ``slug``)
    and a label used in filenames.
    """

    LANDING = ("landing", "/library/{slug}")
    SPECIMEN = ("specimen", "/library/{slug}/alphabet")

    def __init__(self, label: str, url_path_template: str) -> None:
        self.label = label
        self.url_path_template = url_path_template

    def url_for(self, slug: str, *, base_url: str) -> str:
        """Return the full URL for this surface and brand slug.

        Strips a trailing slash from base_url before joining so the caller
        does not need to normalise the base.
        """
        path = self.url_path_template.format(slug=slug)
        return base_url.rstrip("/") + path


#: Canonical ordered list used by build_capture_plan; order determines
#: the iteration order in the output plan.
ALL_SURFACES: tuple[Surface, ...] = (Surface.LANDING, Surface.SPECIMEN)


# ---------------------------------------------------------------------------
# CaptureTarget
# ---------------------------------------------------------------------------


class CaptureTarget(NamedTuple):
    """One screenshot target: a (brand, surface, viewport) triple.

    Attributes:
        brand_slug:       Slug as returned by the hub API (e.g. "stripe").
        surface:          Which page this target points at (LANDING or SPECIMEN).
        viewport_label:   Human-readable viewport name ("desktop" / "mobile").
        width:            Viewport width in CSS pixels.
        height:           Viewport height in CSS pixels.
        url:              Full URL to capture.
        output_filename:  Deterministic, slug-safe filename (ends in .png).
                          Pattern: ``<brand_slug>_<surface>_<viewport>.png``
                          Example: ``stripe_landing_desktop.png``
    """

    brand_slug: str
    surface: Surface
    viewport_label: str
    width: int
    height: int
    url: str
    output_filename: str


# ---------------------------------------------------------------------------
# Plan builder
# ---------------------------------------------------------------------------


def build_capture_plan(
    brands: Sequence[str],
    *,
    base_url: str,
) -> list[CaptureTarget]:
    """Build a deterministic capture plan for all brands x surfaces x viewports.

    Args:
        brands:   Ordered sequence of brand slugs. An empty sequence returns
                  an empty plan; no exception is raised (valid edge case when
                  the hub returns zero brands).
        base_url: Base URL prepended to every surface path. Trailing slash
                  is stripped before joining so ``https://resemblio.com/``
                  and ``https://resemblio.com`` produce identical URLs.

    Returns:
        A list of CaptureTarget, one per (brand, surface, viewport) triple.
        Order: brands outer, surfaces middle, viewports inner. Deterministic
        given the same inputs.

    Note on slug-safety: brand slugs from the Resemblio hub contain only
    lowercase letters, digits, and hyphens (e.g. "are-na", "read-cv").
    The output_filename inherits that pattern plus underscores used as
    separators; no further sanitisation is applied. If a future slug
    contains characters outside [a-zA-Z0-9_\\-.], the filename may be
    unsafe on some filesystems - callers should validate upstream.
    """
    plan: list[CaptureTarget] = []
    for slug in brands:
        for surface in ALL_SURFACES:
            for viewport in ALL_VIEWPORTS:
                url = surface.url_for(slug, base_url=base_url)
                filename = (
                    f"{slug}_{surface.label}_{viewport.label}.png"
                )
                plan.append(
                    CaptureTarget(
                        brand_slug=slug,
                        surface=surface,
                        viewport_label=viewport.label,
                        width=viewport.width,
                        height=viewport.height,
                        url=url,
                        output_filename=filename,
                    )
                )
    return plan
