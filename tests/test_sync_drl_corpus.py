"""Unit tests for scripts/sync_drl_corpus.py pure functions.

Tests use synthetic DRL fixtures in a tmp_path; no network, no real DRL
tree required.  Integration (running the full sync against the real DRL
and checking the vendored output) is covered by
tests/test_drl_corpus_vendored.py.

Functions under test
--------------------
- build_corpus_plan  (plan construction from DRL layout)
- sha256_hex         (file hashing)
- build_manifest     (manifest dict from plan)
- verify_drl_untouched (safety guard)

Do this work at a level that would impress a senior developer.
Include documentation and code comments that make it easy for a future
developer to maintain this project.

Schema: test_sync_drl_corpus_unit_v1
"""
from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

from scripts.sync_drl_corpus import (
    SyncFile,
    build_corpus_plan,
    execute_sync,
    build_manifest,
    read_lf_bytes,
    sha256_hex,
    verify_drl_untouched,
)


# ---------------------------------------------------------------------------
# Fixtures helpers
# ---------------------------------------------------------------------------


def _make_drl(tmp_path: pathlib.Path, systems: list[dict]) -> pathlib.Path:
    """Write a minimal synthetic DRL tree under tmp_path/drl/.

    ``systems`` is a list of dicts, each with:
      - ``slug`` (str)          : brand slug
      - ``assets`` (list[dict]) : list with ``slug``, ``path``, ``tokens_path``
      - ``system_json`` (bool)  : whether to write a system.json for this brand

    Returns the DRL root path.
    """
    drl = tmp_path / "drl"
    drl.mkdir()

    corpus_systems = []
    for sys_def in systems:
        brand = sys_def["slug"]
        assets_list = []
        for asset_def in sys_def.get("assets", []):
            asset_path = asset_def["path"]
            tokens_path = asset_def["tokens_path"]

            # Create asset.html
            html_dir = drl / asset_path
            html_dir.mkdir(parents=True, exist_ok=True)
            (html_dir / "asset.html").write_text(
                f"<html><body>{brand} {asset_def['slug']}</body></html>",
                encoding="utf-8",
            )
            # Create tokens.css
            css_path = drl / tokens_path
            css_path.parent.mkdir(parents=True, exist_ok=True)
            css_path.write_text(
                f":root {{ --color-primary: #{brand[:3]}000; }}\n",
                encoding="utf-8",
            )
            assets_list.append({
                "slug": asset_def["slug"],
                "path": asset_path,
                "tokens_path": tokens_path,
            })

        # Optionally create system.json
        if sys_def.get("system_json", True):
            sys_dir = drl / "systems" / brand
            sys_dir.mkdir(parents=True, exist_ok=True)
            (sys_dir / "system.json").write_text(
                json.dumps({"slug": brand, "tier": "A"}),
                encoding="utf-8",
            )

        corpus_systems.append({"slug": brand, "assets": assets_list})

    corpus = {
        "schema_version": 1,
        "generated": "2026-06-22",
        "asset_count": sum(len(s.get("assets", [])) for s in systems),
        "system_count": len(systems),
        "systems": corpus_systems,
    }
    (drl / "corpus.json").write_text(json.dumps(corpus, indent=2), encoding="utf-8")
    return drl


# ---------------------------------------------------------------------------
# sha256_hex
# ---------------------------------------------------------------------------


def test_sha256_hex_matches_hashlib(tmp_path: pathlib.Path) -> None:
    """sha256_hex must return the same digest as hashlib.sha256 directly."""
    f = tmp_path / "sample.txt"
    f.write_bytes(b"hello world\n")
    expected = hashlib.sha256(b"hello world\n").hexdigest()
    assert sha256_hex(f) == expected


def test_sha256_hex_changes_when_content_changes(tmp_path: pathlib.Path) -> None:
    """Two files with different content must produce different digests."""
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_bytes(b"apple")
    b.write_bytes(b"orange")
    assert sha256_hex(a) != sha256_hex(b)



def test_read_lf_bytes_normalises_crlf(tmp_path: pathlib.Path) -> None:
    """read_lf_bytes must make Windows and CI hash the same content."""
    f = tmp_path / "sample.css"
    f.write_bytes(b"a\r\nb\r\n")
    assert read_lf_bytes(f) == b"a\nb\n"


def test_execute_sync_writes_lf_bytes(tmp_path: pathlib.Path) -> None:
    """execute_sync must write vendored text files with LF endings."""
    src = tmp_path / "src.css"
    dst = tmp_path / "vendored" / "src.css"
    src.write_bytes(b":root {\r\n  --x: 1;\r\n}\r\n")
    plan = [SyncFile(src=src, dst=dst, rel="src.css")]

    summary = execute_sync(plan)

    assert summary == {"copied": 1, "skipped": 0, "total": 1}
    assert dst.read_bytes() == b":root {\n  --x: 1;\n}\n"
# ---------------------------------------------------------------------------
# build_corpus_plan
# ---------------------------------------------------------------------------


def test_plan_includes_corpus_json(tmp_path: pathlib.Path) -> None:
    """corpus.json must be the first entry in the plan."""
    drl = _make_drl(tmp_path, [
        {"slug": "acme", "assets": [{"slug": "hero", "path": "assets/wholes/hero", "tokens_path": "assets/wholes/hero/tokens.css"}]},
    ])
    vendored = tmp_path / "vendored"
    plan = build_corpus_plan(drl, vendored)
    assert plan[0].rel == "corpus.json", "corpus.json must be the first plan entry"


def test_plan_includes_all_asset_files(tmp_path: pathlib.Path) -> None:
    """Plan must include asset.html and tokens.css for every asset."""
    drl = _make_drl(tmp_path, [
        {
            "slug": "beta",
            "assets": [
                {"slug": "card", "path": "assets/atoms/card", "tokens_path": "assets/atoms/card/tokens.css"},
                {"slug": "nav", "path": "assets/wholes/nav", "tokens_path": "assets/wholes/nav/tokens.css"},
            ],
        },
    ])
    vendored = tmp_path / "vendored"
    plan = build_corpus_plan(drl, vendored)
    rels = {sf.rel for sf in plan}
    assert "assets/atoms/card/asset.html" in rels
    assert "assets/atoms/card/tokens.css" in rels
    assert "assets/wholes/nav/asset.html" in rels
    assert "assets/wholes/nav/tokens.css" in rels


def test_plan_includes_system_json_when_present(tmp_path: pathlib.Path) -> None:
    """system.json must appear in the plan for brands that have one."""
    drl = _make_drl(tmp_path, [
        {"slug": "gamma", "assets": [], "system_json": True},
        {"slug": "delta", "assets": [], "system_json": False},
    ])
    vendored = tmp_path / "vendored"
    plan = build_corpus_plan(drl, vendored)
    rels = {sf.rel for sf in plan}
    assert "systems/gamma/system.json" in rels, "gamma has a system.json; plan should include it"
    assert "systems/delta/system.json" not in rels, "delta has no system.json; plan should skip it"


def test_plan_system_json_entries_sorted_by_brand(tmp_path: pathlib.Path) -> None:
    """system.json entries must appear in alphabetical brand-slug order."""
    drl = _make_drl(tmp_path, [
        {"slug": "zebra", "assets": [], "system_json": True},
        {"slug": "alpha", "assets": [], "system_json": True},
    ])
    vendored = tmp_path / "vendored"
    plan = build_corpus_plan(drl, vendored)
    sys_rels = [sf.rel for sf in plan if sf.rel.startswith("systems/")]
    assert sys_rels == sorted(sys_rels), "system.json entries must be in sorted brand order"


def test_plan_asset_entries_sorted_by_rel(tmp_path: pathlib.Path) -> None:
    """Asset file entries must appear in sorted rel-path order."""
    drl = _make_drl(tmp_path, [
        {
            "slug": "omega",
            "assets": [
                {"slug": "z-item", "path": "assets/wholes/z-item", "tokens_path": "assets/wholes/z-item/tokens.css"},
                {"slug": "a-item", "path": "assets/atoms/a-item", "tokens_path": "assets/atoms/a-item/tokens.css"},
            ],
        },
    ])
    vendored = tmp_path / "vendored"
    plan = build_corpus_plan(drl, vendored)
    asset_rels = [sf.rel for sf in plan if sf.rel.startswith("assets/")]
    assert asset_rels == sorted(asset_rels), "asset entries must be sorted by rel path"


def test_plan_raises_on_missing_corpus_json(tmp_path: pathlib.Path) -> None:
    """FileNotFoundError when corpus.json is absent at the DRL root."""
    empty = tmp_path / "empty_drl"
    empty.mkdir()
    with pytest.raises(FileNotFoundError, match="corpus.json not found"):
        build_corpus_plan(empty, tmp_path / "vendored")


def test_plan_rejects_non_text_extensions(tmp_path: pathlib.Path) -> None:
    """ValueError when a planned file has a disallowed extension.

    Simulated by patching the corpus to reference a .png path.
    """
    drl = tmp_path / "drl"
    drl.mkdir()
    # Craft a corpus that references a .png file via tokens_path.
    bad_corpus = {
        "schema_version": 1,
        "generated": "2026-06-22",
        "asset_count": 1,
        "system_count": 1,
        "systems": [{
            "slug": "evil",
            "assets": [{
                "slug": "pic",
                "path": "assets/atoms/pic",
                "tokens_path": "assets/atoms/pic/tokens.png",  # disallowed
            }],
        }],
    }
    (drl / "corpus.json").write_text(json.dumps(bad_corpus), encoding="utf-8")
    # Create the referenced file so it isn't skipped as missing.
    bad_file = drl / "assets/atoms/pic/tokens.png"
    bad_file.parent.mkdir(parents=True, exist_ok=True)
    bad_file.write_bytes(b"\x89PNG")
    (drl / "assets/atoms/pic/asset.html").write_text("<html/>", encoding="utf-8")

    with pytest.raises(ValueError, match="Unexpected file extension"):
        build_corpus_plan(drl, tmp_path / "vendored")


# ---------------------------------------------------------------------------
# build_manifest
# ---------------------------------------------------------------------------


def test_build_manifest_counts_and_schema(tmp_path: pathlib.Path) -> None:
    """build_manifest must embed asset_count, file_count, and schema_version."""
    # Create two synthetic destination files.
    dst_a = tmp_path / "a.json"
    dst_b = tmp_path / "b.html"
    dst_a.write_bytes(b'{"x": 1}')
    dst_b.write_bytes(b"<p>hi</p>")

    plan = [
        SyncFile(src=tmp_path / "src_a", dst=dst_a, rel="a.json"),
        SyncFile(src=tmp_path / "src_b", dst=dst_b, rel="b.html"),
    ]
    corpus_meta = {"asset_count": 42, "generated": "2026-06-22"}

    manifest = build_manifest(plan, corpus_meta)

    assert manifest["schema_version"] == "sync_drl_corpus_v1"
    assert manifest["asset_count"] == 42
    assert manifest["file_count"] == 2
    assert len(manifest["files"]) == 2


def test_build_manifest_sha256_correct(tmp_path: pathlib.Path) -> None:
    """Each manifest entry must carry the sha256 of the destination file."""
    dst = tmp_path / "test.json"
    content = b'{"hello": "world"}'
    dst.write_bytes(content)
    expected_sha = hashlib.sha256(content).hexdigest()

    plan = [SyncFile(src=tmp_path / "src", dst=dst, rel="test.json")]
    manifest = build_manifest(plan, {"asset_count": 0})

    assert manifest["files"][0]["sha256"] == expected_sha


def test_build_manifest_has_no_timestamps(tmp_path: pathlib.Path) -> None:
    """manifest.json must not contain a timestamp field (idempotency requirement).

    Timestamps belong in VERSION, not manifest.json.  A manifest that
    changes on every run breaks git diffs and the byte-idempotency guarantee.
    """
    dst = tmp_path / "x.html"
    dst.write_bytes(b"<p/>")
    plan = [SyncFile(src=tmp_path / "s", dst=dst, rel="x.html")]
    manifest = build_manifest(plan, {"asset_count": 1})

    timestamp_keys = {"vendored_at", "synced_at", "generated", "created_at", "timestamp"}
    found = timestamp_keys & set(manifest.keys())
    assert not found, (
        f"Timestamp key(s) {found} found in manifest. "
        "Timestamps must live in VERSION, not manifest.json, "
        "so re-running the sync produces a byte-identical manifest."
    )


# ---------------------------------------------------------------------------
# verify_drl_untouched
# ---------------------------------------------------------------------------


def test_verify_drl_untouched_passes_for_external_dst(tmp_path: pathlib.Path) -> None:
    """No error when dst paths are outside the DRL root."""
    drl = tmp_path / "drl"
    drl.mkdir()
    vendored = tmp_path / "vendored"
    plan = [
        SyncFile(src=drl / "corpus.json", dst=vendored / "corpus.json", rel="corpus.json"),
    ]
    # Should not raise.
    verify_drl_untouched(drl, plan)


def test_verify_drl_untouched_raises_for_internal_dst(tmp_path: pathlib.Path) -> None:
    """RuntimeError when a dst path is inside the DRL root (safety violation)."""
    drl = tmp_path / "drl"
    drl.mkdir()
    bad_dst = drl / "accidentally_inside.json"  # inside DRL - forbidden
    plan = [
        SyncFile(src=drl / "corpus.json", dst=bad_dst, rel="corpus.json"),
    ]
    with pytest.raises(RuntimeError, match="SAFETY VIOLATION"):
        verify_drl_untouched(drl, plan)
