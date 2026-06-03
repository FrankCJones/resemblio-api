"""Tests for `scripts.capture_all_button_snapshots`.

Covers:

- DRL discovery from a synthetic `_extractions/` tree
- URL resolution from `extraction.json` `inspired_by` provenance
- `--single` filter
- `--limit` cap
- Idempotency (existing snapshot skipped unless `--force`)
- Per-brand failure isolation (one brand returns status=error;
  others still capture)
- Dry-run writes nothing
- The on-disk envelope preserves the ComputedStyleReport shape
  the indexer loader expects (top-level `status`/`signals`).

The real Playwright capture is never invoked: the `capture_fn` runner
slot lets us inject a deterministic fake.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.capture_all_button_snapshots import (
    BrandSpec,
    CaptureArgs,
    aggregate_report,
    build_brand_specs,
    capture_one_brand,
    discover_brand_dirs,
    parse_args,
    resolve_brand_url,
    run,
    select_brands,
)


# --- Synthetic DRL fixture ---------------------------------------------------


_BRANDS: dict[str, str] = {
    "apple": "https://www.apple.com",
    "stripe": "https://stripe.com",
    "the-pudding": "https://pudding.cool",
}


def _make_extraction_json(slug: str, url: str | None) -> dict[str, Any]:
    """Build a minimal extraction.json with inspired_by URL provenance."""
    inspired = [{"site": slug, "url": url}] if url else []
    return {
        "schema_version": 1,
        "system_slug": slug,
        "tokens": {},
        "sections": {
            "alphabet": {"inspired_by": inspired},
            "library": {"inspired_by": inspired},
        },
    }


def _write_drl(root: Path, brands: dict[str, str | None]) -> None:
    """Create a synthetic DRL `_extractions/` tree under `root`."""
    extractions = root / "_extractions"
    extractions.mkdir(parents=True, exist_ok=True)
    for slug, url in brands.items():
        brand_dir = extractions / slug
        brand_dir.mkdir()
        (brand_dir / "extraction.json").write_text(
            json.dumps(_make_extraction_json(slug, url)), encoding="utf-8"
        )
    # Hidden + underscore dirs that must be filtered out.
    (extractions / "_INBOX").mkdir()
    (extractions / ".cache").mkdir()


@pytest.fixture()
def drl_root(tmp_path: Path) -> Path:
    """A fresh synthetic DRL root for each test."""
    root = tmp_path / "drl"
    _write_drl(root, dict(_BRANDS))
    return root


@pytest.fixture()
def out_dir(tmp_path: Path) -> Path:
    """Empty output dir; tests assert what gets written."""
    p = tmp_path / "computed_styles"
    p.mkdir()
    return p


# --- Capture fake -----------------------------------------------------------


def _ok_report(slot_props: dict[str, str] | None = None) -> dict[str, Any]:
    """Return a well-formed ComputedStyleReport with one `cta` slot."""
    props = slot_props or {
        "color": "rgb(255, 255, 255)",
        "background-color": "rgb(0, 102, 204)",
        "border-radius": "980px",
        "padding": "12px 22px",
        "font-size": "17px",
        "font-weight": "400",
    }
    return {
        "status": "ok",
        "signals": [
            {"slot": "cta", "selector": "button, .cta, [role=button]", "properties": props}
        ],
        "error": None,
        "schema_version": 1,
    }


def _make_capture_fn(fail_for: set[str] | None = None) -> Any:
    """Return a deterministic `capture_fn` that succeeds for every URL.

    URLs whose host appears in `fail_for` get an error-status report.
    """
    fail_hosts = fail_for or set()

    def fn(html: str | None, url: str | None, timeout_ms: int) -> dict[str, Any]:
        assert html is None and url is not None
        for host in fail_hosts:
            if host in (url or ""):
                return {
                    "status": "error",
                    "signals": [],
                    "error": "simulated",
                    "schema_version": 1,
                }
        return _ok_report()

    return fn


# --- Discovery + URL resolution ---------------------------------------------


def test_discover_brand_dirs_filters_hidden_and_underscore(drl_root: Path) -> None:
    dirs = discover_brand_dirs(drl_root)
    names = {d.name for d in dirs}
    assert names == set(_BRANDS.keys())


def test_discover_raises_when_extractions_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        discover_brand_dirs(tmp_path / "nope")


def test_resolve_brand_url_reads_inspired_by(drl_root: Path) -> None:
    dirs = discover_brand_dirs(drl_root)
    by_slug = {d.name: d for d in dirs}
    assert resolve_brand_url(by_slug["apple"]) == "https://www.apple.com"
    assert resolve_brand_url(by_slug["stripe"]) == "https://stripe.com"


def test_resolve_brand_url_returns_none_when_no_provenance(tmp_path: Path) -> None:
    root = tmp_path / "drl"
    _write_drl(root, {"empty": None})
    brand_dir = root / "_extractions" / "empty"
    assert resolve_brand_url(brand_dir) is None


def test_resolve_brand_url_returns_none_when_file_missing(tmp_path: Path) -> None:
    brand_dir = tmp_path / "brand"
    brand_dir.mkdir()
    assert resolve_brand_url(brand_dir) is None


def test_resolve_brand_url_returns_none_when_malformed(tmp_path: Path) -> None:
    brand_dir = tmp_path / "brand"
    brand_dir.mkdir()
    (brand_dir / "extraction.json").write_text("not json", encoding="utf-8")
    assert resolve_brand_url(brand_dir) is None


# --- Filtering ---------------------------------------------------------------


def test_select_brands_single(drl_root: Path) -> None:
    specs = build_brand_specs(discover_brand_dirs(drl_root))
    selected = select_brands(specs, single="apple", limit=None)
    assert [s["slug"] for s in selected] == ["apple"]


def test_select_brands_single_unknown_raises(drl_root: Path) -> None:
    specs = build_brand_specs(discover_brand_dirs(drl_root))
    with pytest.raises(ValueError):
        select_brands(specs, single="nope", limit=None)


def test_select_brands_limit(drl_root: Path) -> None:
    specs = build_brand_specs(discover_brand_dirs(drl_root))
    selected = select_brands(specs, single=None, limit=2)
    assert len(selected) == 2


# --- capture_one_brand ------------------------------------------------------


def test_capture_one_brand_writes_envelope(out_dir: Path) -> None:
    spec: BrandSpec = {"slug": "apple", "url": "https://www.apple.com"}
    outcome = capture_one_brand(
        spec, out_dir, force=False, timeout_ms=8000, capture_fn=_make_capture_fn()
    )
    assert outcome["status"] == "captured"
    p = out_dir / "apple.json"
    assert p.exists()
    data = json.loads(p.read_text(encoding="utf-8"))
    # The loader casts the dict directly to ComputedStyleReport: status +
    # signals must live at the TOP level. envelope keys are additive.
    assert data["status"] == "ok"
    assert isinstance(data["signals"], list)
    assert data["signals"][0]["slot"] == "cta"
    assert data["captured_url"] == "https://www.apple.com"
    assert data["envelope_schema_version"] == 1


def test_capture_one_brand_idempotent_without_force(out_dir: Path) -> None:
    spec: BrandSpec = {"slug": "apple", "url": "https://www.apple.com"}
    first = capture_one_brand(spec, out_dir, force=False, timeout_ms=8000, capture_fn=_make_capture_fn())
    assert first["status"] == "captured"
    second = capture_one_brand(spec, out_dir, force=False, timeout_ms=8000, capture_fn=_make_capture_fn())
    assert second["status"] == "skipped"
    assert "exists" in (second["error"] or "")


def test_capture_one_brand_force_overwrites(out_dir: Path) -> None:
    spec: BrandSpec = {"slug": "apple", "url": "https://www.apple.com"}
    capture_one_brand(spec, out_dir, force=False, timeout_ms=8000, capture_fn=_make_capture_fn())
    second = capture_one_brand(spec, out_dir, force=True, timeout_ms=8000, capture_fn=_make_capture_fn())
    assert second["status"] == "captured"


def test_capture_one_brand_skips_when_no_url(out_dir: Path) -> None:
    spec: BrandSpec = {"slug": "ghost", "url": None}
    outcome = capture_one_brand(spec, out_dir, force=False, timeout_ms=8000, capture_fn=_make_capture_fn())
    assert outcome["status"] == "skipped"
    assert "no canonical url" in (outcome["error"] or "")
    assert not (out_dir / "ghost.json").exists()


def test_capture_one_brand_failure_isolated(out_dir: Path) -> None:
    spec: BrandSpec = {"slug": "broken", "url": "https://broken.example"}
    outcome = capture_one_brand(
        spec,
        out_dir,
        force=False,
        timeout_ms=8000,
        capture_fn=_make_capture_fn(fail_for={"broken.example"}),
    )
    assert outcome["status"] == "failed"
    assert "error" in (outcome["error"] or "") or "simulated" in (outcome["error"] or "")
    assert not (out_dir / "broken.json").exists()


def test_capture_one_brand_handles_runner_exception(out_dir: Path) -> None:
    def boom(html, url, ms):  # noqa: ANN001
        raise RuntimeError("kaboom")

    spec: BrandSpec = {"slug": "x", "url": "https://x.example"}
    outcome = capture_one_brand(spec, out_dir, force=False, timeout_ms=8000, capture_fn=boom)
    assert outcome["status"] == "failed"
    assert "RuntimeError" in (outcome["error"] or "")


# --- End-to-end run() -------------------------------------------------------


def _args(drl_root: Path, out_dir: Path, **kw: Any) -> CaptureArgs:
    return CaptureArgs(
        apply=kw.get("apply", False),
        force=kw.get("force", False),
        drl_root=drl_root,
        out_dir=out_dir,
        single=kw.get("single"),
        limit=kw.get("limit"),
        timeout_ms=kw.get("timeout_ms", 8000),
    )


def test_run_dry_run_writes_nothing(drl_root: Path, out_dir: Path) -> None:
    report = run(_args(drl_root, out_dir, apply=False))
    assert report.brands_discovered == 3
    assert report.brands_processed == 3
    assert report.captured == 0
    assert list(out_dir.iterdir()) == []
    statuses = {o["status"] for o in report.outcomes}
    assert "dry-run" in statuses


def test_run_apply_captures_all(drl_root: Path, out_dir: Path) -> None:
    report = run(_args(drl_root, out_dir, apply=True), capture_fn=_make_capture_fn())
    assert report.captured == 3
    assert report.failed == 0
    assert (out_dir / "apple.json").exists()
    assert (out_dir / "stripe.json").exists()


def test_run_apply_isolates_per_brand_failure(drl_root: Path, out_dir: Path) -> None:
    report = run(
        _args(drl_root, out_dir, apply=True),
        capture_fn=_make_capture_fn(fail_for={"stripe.com"}),
    )
    assert report.captured == 2
    assert report.failed == 1
    failed = [o for o in report.outcomes if o["status"] == "failed"]
    assert failed and failed[0]["slug"] == "stripe"


def test_run_apply_single(drl_root: Path, out_dir: Path) -> None:
    report = run(
        _args(drl_root, out_dir, apply=True, single="apple"),
        capture_fn=_make_capture_fn(),
    )
    assert report.brands_processed == 1
    assert report.captured == 1
    assert (out_dir / "apple.json").exists()
    assert not (out_dir / "stripe.json").exists()


def test_run_apply_limit(drl_root: Path, out_dir: Path) -> None:
    report = run(
        _args(drl_root, out_dir, apply=True, limit=2),
        capture_fn=_make_capture_fn(),
    )
    assert report.brands_processed == 2
    assert report.captured == 2


def test_aggregate_counts() -> None:
    outcomes = [
        {"slug": "a", "url": "x", "out_path": "", "status": "captured", "error": None},
        {"slug": "b", "url": "x", "out_path": "", "status": "skipped", "error": None},
        {"slug": "c", "url": "x", "out_path": "", "status": "failed", "error": "x"},
    ]
    report = aggregate_report(Path("/drl"), Path("/out"), 5, outcomes)  # type: ignore[arg-type]
    assert report.captured == 1
    assert report.skipped == 1
    assert report.failed == 1


# --- parse_args --------------------------------------------------------------


def test_parse_args_defaults_to_dry_run() -> None:
    args = parse_args(["--drl-root", "/tmp/drl"])
    assert args.apply is False
    assert args.force is False
    assert args.single is None


def test_parse_args_apply_force() -> None:
    args = parse_args(["--apply", "--force", "--single", "apple", "--drl-root", "/tmp/drl"])
    assert args.apply is True
    assert args.force is True
    assert args.single == "apple"
