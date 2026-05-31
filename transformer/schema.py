"""Schema version stamp for transformer outputs.

Bump when the ``StrippedEntry`` field set changes in a way that downstream
consumers (the seeder, the seeded ``extractions`` row's ``tokens_json``
payload) need to detect.
"""
from __future__ import annotations

SCHEMA_VERSION = 1
