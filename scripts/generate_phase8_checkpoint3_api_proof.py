"""Emit deterministic API-owned proof fixtures for Phase 8 checkpoint three.

The generator calls the production Apple export builder for the complete,
classification, and representative record selections required by the phase
plan. It performs no network access and writes canonical JSON to standard
output.

Dependencies: the local ``app.apple_export_product`` module and Python 3.11+.
Run: ``python -m scripts.generate_phase8_checkpoint3_api_proof``
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final, Literal, TypedDict

from app.apple_export_product import AppleSelection, ExportManifest, build_export

PROOF_SCHEMA_VERSION: Final = "resemblio_phase8_checkpoint3_api_proof_v1"

ProofCaseName = Literal[
    "complete",
    "classification-foundation",
    "classification-component",
    "classification-composition",
    "record-component-buttons",
]


class McpArguments(TypedDict, total=False):
    """Normalized arguments accepted by both MCP library export tools."""

    selection: Literal["complete", "classification", "record"]
    classification: Literal["foundation", "component", "composition"]
    record_ids: list[str]


class ApiProofCase(TypedDict):
    """One named selection and its exact API-owned product values."""

    name: ProofCaseName
    api_selection: AppleSelection
    mcp_arguments: McpArguments
    tokens: dict[str, Any]
    manifest: ExportManifest


class ApiProofBundle(TypedDict):
    """Schema-versioned deterministic cross-surface input bundle."""

    schema_version: str
    source: Literal["api-owned-build-export"]
    cases: list[ApiProofCase]


@dataclass(frozen=True)
class ProofCaseSpec:
    """Immutable mapping between one API selection and MCP arguments."""

    name: ProofCaseName
    api_selection: AppleSelection
    mcp_arguments: McpArguments


PROOF_CASES: Final[tuple[ProofCaseSpec, ...]] = (
    ProofCaseSpec(
        name="complete",
        api_selection={"kind": "complete"},
        mcp_arguments={"selection": "complete"},
    ),
    ProofCaseSpec(
        name="classification-foundation",
        api_selection={"kind": "classification", "classification": "foundation"},
        mcp_arguments={"selection": "classification", "classification": "foundation"},
    ),
    ProofCaseSpec(
        name="classification-component",
        api_selection={"kind": "classification", "classification": "component"},
        mcp_arguments={"selection": "classification", "classification": "component"},
    ),
    ProofCaseSpec(
        name="classification-composition",
        api_selection={"kind": "classification", "classification": "composition"},
        mcp_arguments={"selection": "classification", "classification": "composition"},
    ),
    ProofCaseSpec(
        name="record-component-buttons",
        api_selection={"kind": "record", "record_ids": ["component:buttons"]},
        mcp_arguments={"selection": "record", "record_ids": ["component:buttons"]},
    ),
)


def build_api_proof() -> ApiProofBundle:
    """Build all required proof cases from the real API-owned export builder."""
    cases: list[ApiProofCase] = []
    for spec in PROOF_CASES:
        artifact = build_export(spec.api_selection)
        cases.append(
            {
                "name": spec.name,
                "api_selection": spec.api_selection,
                "mcp_arguments": spec.mcp_arguments,
                "tokens": artifact["tokens"],
                "manifest": artifact["manifest"],
            }
        )
    return {
        "schema_version": PROOF_SCHEMA_VERSION,
        "source": "api-owned-build-export",
        "cases": cases,
    }


def canonical_proof_bytes(proof: ApiProofBundle) -> bytes:
    """Serialize proof with sorted keys, compact separators, UTF-8, and one LF."""
    return (
        json.dumps(proof, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def main() -> None:
    """Write one deterministic proof bundle to standard output."""
    import sys

    sys.stdout.buffer.write(canonical_proof_bytes(build_api_proof()))


if __name__ == "__main__":
    main()
