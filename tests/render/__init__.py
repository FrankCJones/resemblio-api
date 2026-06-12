"""Render-fidelity test sub-package.

Houses tests that compare live Resemblio library page renders against
reference captures from real brand sites. Separate from the main API
test tree because these tests require Playwright + the
visual_fidelity_check sub-package and intentionally skip when those are
not available so the rest of the suite stays green.

Schema versions read:
    - reference_capture_manifest_v1 (input)
    - visual_fidelity_tolerance_v1  (input)
    - fidelity_spec_v2              (input, per-(brand, category))

Schema versions written:
    - library_visual_fidelity_gate_report_v1 (JSON + Markdown)
"""
