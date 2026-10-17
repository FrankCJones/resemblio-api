# Apple compiler inventory

This directory contains the approved Apple classification matrix required by
the deterministic system-manifest compiler. It is vendored so a bare clone of
the API repository can compile and verify the Apple artifacts without access
to the surrounding development workspace.

The source is the Phase 9 approved matrix dated 2026-09-09. Its normalized LF
SHA-256 is `2eab2998867e68d11409b71fc4681fe0dba28ba478da0583913aecd975d5ac28`.
The compiler verifies this lock before using the default matrix. An intentional
matrix change requires an explicit compiler-lock update and regenerated
deterministic artifacts.

This copy is a release input. The upstream Design Reference Library remains
read-only and is not modified by this repository.
