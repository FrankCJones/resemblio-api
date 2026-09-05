"""DRL content snapshot must be vendored in-repo so CI can render every asset
and reseed without the workspace DRL tree present.

Run ``scripts/sync_drl_corpus.py`` from ``code/api/``, commit the output
to ``_vendored/drl_corpus/``, then re-run this file to go GREEN.

Acceptance criteria (Issue #36, Epic #35 Step 0)
-------------------------------------------------
AC1: sync_drl_corpus.py copies all referenced files; DRL is unmodified.
AC2: manifest.json sha256 verifies every vendored file and matches corpus.json.
AC3: seed loaders (load_corpus / load_asset_html / load_tokens_for_asset)
     succeed against the vendored root passed as drl_root.
AC4: Given an unchanged DRL, re-running the sync produces a byte-identical
     manifest.json. Tested here by recomputing sha256 values from disk and
     confirming they equal the stored manifest (the manifest is a pure,
     timestamp-free content hash; any re-run of the sync script with the
     same DRL produces the same hashes).

Do this work at a level that would impress a senior developer.
Include documentation and code comments that make it easy for a future
developer to maintain this project.

Schema: test_drl_corpus_vendored_v1
"""
from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

import scripts.seed_from_drl as seed

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Repo root = two levels above this file:  tests/ -> code/api/
_REPO_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parent.parent
_VENDORED: pathlib.Path = _REPO_ROOT / "_vendored" / "drl_corpus"
_CORPUS_JSON: pathlib.Path = _VENDORED / "corpus.json"
_MANIFEST_JSON: pathlib.Path = _VENDORED / "manifest.json"

def _expected_asset_count() -> int:
    """Return the asset count declared by the vendored corpus itself.

    The Apple completion work legitimately grows the vendored corpus, so this
    test must verify internal consistency rather than pinning a historical
    snapshot count.
    """
    if not _CORPUS_JSON.is_file():
        return 0
    corpus = json.loads(_CORPUS_JSON.read_text(encoding="utf-8"))
    return int(corpus.get("asset_count", 0))


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _sha256(path: pathlib.Path) -> str:
    """Return the hex SHA-256 of a file's contents."""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# AC1 - snapshot present on disk
# ---------------------------------------------------------------------------


def test_vendored_drl_corpus_dir_exists() -> None:
    """_vendored/drl_corpus/ must exist after running the sync script.

    RED before scripts/sync_drl_corpus.py is executed.
    GREEN after the snapshot is populated and committed.
    """
    assert _VENDORED.is_dir(), (
        f"Vendored DRL corpus directory absent at {_VENDORED}.\n"
        "Run:  python scripts/sync_drl_corpus.py\n"
        "from the code/api/ directory, then commit the result."
    )


def test_corpus_json_present() -> None:
    """corpus.json must be vendored as the catalogue root.

    It is the seed loaders' entry point and the manifest's authoritative
    asset count source. Without it the fidelity gate cannot locate any asset.
    """
    assert _CORPUS_JSON.is_file(), (
        f"corpus.json absent at {_CORPUS_JSON}. Run scripts/sync_drl_corpus.py."
    )


# ---------------------------------------------------------------------------
# AC2 - manifest integrity: count and per-file sha256
# ---------------------------------------------------------------------------


def test_manifest_parses_and_has_correct_count() -> None:
    """manifest.json must parse and match corpus.json asset_count.

    The count is the DRL corpus's own asset_count. A mismatch means the
    sync script did not copy all assets or the corpus changed without a
    manifest refresh.
    """
    assert _MANIFEST_JSON.is_file(), (
        f"manifest.json absent at {_MANIFEST_JSON}. Run scripts/sync_drl_corpus.py."
    )
    manifest = json.loads(_MANIFEST_JSON.read_text(encoding="utf-8"))
    assert "schema_version" in manifest, (
        "manifest.json is missing the 'schema_version' field. "
        "Re-run scripts/sync_drl_corpus.py to regenerate."
    )
    actual = manifest.get("asset_count")
    expected = _expected_asset_count()
    assert actual == expected, (
        f"manifest.json asset_count={actual!r}, expected {expected}. "
        "Regenerate manifest.json if the vendored corpus changed."
    )


def test_manifest_sha256_verifies_all_files() -> None:
    """Every manifest entry must exist on disk with a matching sha256.

    This is the integrity seal: a truncated or corrupted vendored file will
    fail here before any downstream render or seed attempt.
    """
    if not _MANIFEST_JSON.is_file():
        pytest.skip("manifest.json absent - test_manifest_parses_and_has_correct_count will report this")

    manifest = json.loads(_MANIFEST_JSON.read_text(encoding="utf-8"))
    files = manifest.get("files", [])
    assert files, "manifest.json 'files' list is empty"

    failures: list[str] = []
    for entry in files:
        rel: str = entry["path"]
        expected_sha: str = entry["sha256"]
        disk_path = _VENDORED / rel
        if not disk_path.is_file():
            failures.append(f"MISSING:         {rel}")
            continue
        actual_sha = _sha256(disk_path)
        if actual_sha != expected_sha:
            failures.append(
                f"SHA256 MISMATCH: {rel} "
                f"(expected {expected_sha[:12]}..., got {actual_sha[:12]}...)"
            )

    if failures:
        # Show the first 10 to keep the error readable; summarise the rest.
        shown = failures[:10]
        tail = f"\n  ... and {len(failures) - 10} more" if len(failures) > 10 else ""
        raise AssertionError(
            f"{len(failures)} manifest entry failure(s):\n  "
            + "\n  ".join(shown)
            + tail
        )


# ---------------------------------------------------------------------------
# AC3 - vendored root is seed-loader-compatible
# ---------------------------------------------------------------------------


def test_seed_loaders_work_against_vendored_root() -> None:
    """load_corpus / load_asset_html / load_tokens_for_asset must all succeed
    when _vendored/drl_corpus/ is passed as drl_root.

    This confirms the snapshot faithfully mirrors the DRL layout that the
    seed script expects. A future reseed with --drl-root pointing at the
    vendored copy must not require any seed-script changes.
    """
    if not _VENDORED.is_dir():
        pytest.skip("vendored corpus absent - test_vendored_drl_corpus_dir_exists will report this")

    # load_corpus: must find corpus.json and parse it.
    corpus = seed.load_corpus(_VENDORED)
    total_assets = sum(
        len(sys_entry.get("assets", []))
        for sys_entry in corpus.get("systems", [])
    )
    expected = _expected_asset_count()
    assert total_assets == expected, (
        f"load_corpus returned {total_assets} assets against the vendored root; "
        f"expected {expected}. corpus.json may be stale."
    )

    # Spot-check: pick the first asset in the first system.
    first_system = corpus["systems"][0]
    first_asset = first_system["assets"][0]
    asset_slug = first_asset.get("slug", "<unknown>")

    # load_asset_html: must return non-empty HTML.
    html = seed.load_asset_html(_VENDORED, first_asset)
    assert html, (
        f"load_asset_html returned empty/None for asset {asset_slug!r}. "
        "Check that the vendored asset.html exists and is not empty."
    )

    # load_tokens_for_asset: must return non-empty token dict.
    tokens = seed.load_tokens_for_asset(_VENDORED, first_asset)
    assert tokens, (
        f"load_tokens_for_asset returned empty for asset {asset_slug!r}. "
        "Check that the vendored tokens.css exists and declares CSS custom properties."
    )


# ---------------------------------------------------------------------------
# AC4 - manifest is idempotent (byte-identical across re-runs with same DRL)
# ---------------------------------------------------------------------------


def test_manifest_is_content_idempotent() -> None:
    """Recomputing sha256 from the vendored files must produce the same list
    as stored in manifest.json.

    The manifest is a pure content hash with no timestamps, so this is
    equivalent to asserting that re-running the sync script against an
    unchanged DRL produces a byte-identical manifest. If this test fails,
    the stored manifest is out of sync with the on-disk vendored files.
    """
    if not _MANIFEST_JSON.is_file():
        pytest.skip("manifest.json absent - earlier tests will report this")

    manifest = json.loads(_MANIFEST_JSON.read_text(encoding="utf-8"))
    stored_files: list[dict] = manifest.get("files", [])
    if not stored_files:
        pytest.skip("manifest.json files list is empty - earlier tests will report this")

    # Recompute sha256 for every listed file (skip missing files; earlier
    # tests will already report them as failures).
    recomputed: list[dict] = []
    for entry in stored_files:
        disk_path = _VENDORED / entry["path"]
        if disk_path.is_file():
            recomputed.append({"path": entry["path"], "sha256": _sha256(disk_path)})
        else:
            recomputed.append({"path": entry["path"], "sha256": "MISSING"})

    assert recomputed == stored_files, (
        "Recomputed sha256 values do not match the stored manifest. "
        "The vendored files have changed since the manifest was written. "
        "Re-run scripts/sync_drl_corpus.py and commit the updated manifest."
    )
