"""Structural corpus must be vendored in-repo so the gate's structural tier
runs on a standalone CI checkout, not only where the workspace
``_verification/`` tree is present.

Handoff: _HANDOFF_2026-06-13_library-v5-phase8-vendor-fidelity-corpus.md
         Phase 8.1 (RED) -> Phase 8.2 (GREEN after vendoring).

v5 Definition of Done: "Visual fidelity gate standing, tracked, green against
real references, running on CI." A gate that always self-skips on CI is not
"running on CI". This test asserts the text corpus exists in-repo so the
structural tier can run without the workspace _verification/ tree.

Schema: corpus_is_vendored_check_v1
"""
from __future__ import annotations

import pathlib

import pytest

# In-repo corpus root: sibling to this file under tests/render/.
_IN_REPO_CORPUS = pathlib.Path(__file__).resolve().parent / "reference_corpus"
_SPECS_DIR = _IN_REPO_CORPUS / "reference_captures" / "specs"


def test_structural_corpus_present_in_repo() -> None:
    """The structural fidelity corpus must be vendored into this repo.

    Asserts the in-repo corpus directory exists and contains the two linear
    specs that the ``test_linear_font_spec_*`` tests rely on, plus
    ``tolerance_config.yml``. Only checks the in-repo path; the workspace
    ``_verification/`` tree is irrelevant here (that is the whole point).

    RED before Phase 8.2 (nothing vendored). GREEN after vendoring.
    """
    assert _IN_REPO_CORPUS.is_dir(), (
        f"In-repo corpus dir absent at {_IN_REPO_CORPUS}. "
        "Run scripts/sync_fidelity_corpus.py from the workspace to vendor the "
        "text corpus into the repo (Phase 8.2). No PNGs are vendored - only "
        "JSON specs, tolerance_config.yml, fidelity_targets.yml, and manifest.json."
    )
    assert (_IN_REPO_CORPUS / "tolerance_config.yml").is_file(), (
        f"tolerance_config.yml not vendored at {_IN_REPO_CORPUS / 'tolerance_config.yml'}"
    )
    assert _SPECS_DIR.is_dir(), (
        f"specs/ subdir absent at {_SPECS_DIR}"
    )
    assert (_SPECS_DIR / "linear_alphabet.json").is_file(), (
        "linear_alphabet.json not vendored - the test_linear_font_spec_* tests "
        "will skip on CI instead of running."
    )
    assert (_SPECS_DIR / "linear_about-team.json").is_file(), (
        "linear_about-team.json not vendored - test_linear_font_spec_* coverage "
        "incomplete on CI."
    )


def test_no_png_in_corpus() -> None:
    """No brand-site PNG may be committed to the in-repo corpus.

    Public-repo trademark defense: the reference PNGs are full-page screenshots
    of real brand homepages (apple.com, stripe.com, vercel.com, ...). Committing
    them to a public repo contradicts Resemblio's inspirado-no-copiado posture.
    SSIM is informational-only (D-5.1 locked Phase 5); the structural gate does
    not need the PNGs to render its verdict.

    This guard runs on every checkout, including CI, so it cannot be silenced by
    the workspace-absent self-skip pattern.
    """
    if not _IN_REPO_CORPUS.exists():
        return  # Nothing vendored yet; nothing to check.
    pngs = list(_IN_REPO_CORPUS.rglob("*.png"))
    assert not pngs, (
        f"PNG(s) found in the in-repo corpus: {pngs}. "
        "Brand-site screenshots must NOT be committed to this public repo. "
        "Remove the PNG(s) and update .gitignore or the sync helper."
    )
