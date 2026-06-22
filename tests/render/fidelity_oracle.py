"""DRL fidelity oracle stub (RED phase - NotImplementedError on all functions).

Replace with the full implementation in the GREEN commit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple


FIDELITY_PROPERTIES: Tuple[str, ...] = ()
SCHEMA_VERSION = "fidelity_oracle_v1"
BASELINE_SCHEMA_VERSION = "fidelity_baseline_map_v1"


@dataclass(frozen=True)
class StyleDiff:
    state: str
    property: str
    reference: str
    candidate: str


@dataclass
class FidelityVerdict:
    verdict: str
    diffs: List[StyleDiff] = field(default_factory=list)
    tier: str = "none"
    ssim: Optional[float] = None
    schema_version: str = SCHEMA_VERSION


@dataclass
class BaselineEntry:
    brand: str
    asset_class: str
    asset_slug: str
    verdict: str
    diffs: List[StyleDiff] = field(default_factory=list)
    tier: str = "none"
    ssim: Optional[float] = None


@dataclass
class BaselineMap:
    asset_count: int
    pass_count: int
    fail_count: int
    missing_count: int
    entries: List[BaselineEntry]
    generated_at: str
    schema_version: str = BASELINE_SCHEMA_VERSION


@dataclass(frozen=True)
class CorpusAsset:
    import pathlib as _pathlib
    brand: str
    asset_class: str
    asset_slug: str
    html_path: Any  # pathlib.Path


def compare_computed_styles(
    reference: Dict[str, Dict[str, str]],
    candidate: Dict[str, Dict[str, str]],
    *,
    properties: Sequence[str] = FIDELITY_PROPERTIES,
) -> FidelityVerdict:
    raise NotImplementedError


def build_baseline_map(
    verdicts: Sequence[Tuple[str, str, str, FidelityVerdict]],
) -> BaselineMap:
    raise NotImplementedError


def iter_corpus_assets(corpus_root: Any) -> Iterator[CorpusAsset]:
    raise NotImplementedError


def is_candidate_wrapped(rendered_html: str) -> bool:
    raise NotImplementedError


def extract_component_from_candidate(rendered_html: str) -> Optional[str]:
    raise NotImplementedError


def capture_reference_styles(asset_html_path: Any) -> Optional[Dict[str, Dict[str, str]]]:
    raise NotImplementedError


def capture_candidate_styles(rendered_html: str) -> Optional[Dict[str, Dict[str, str]]]:
    raise NotImplementedError
