"""Tests for the vendored DRL version marker."""
from __future__ import annotations

import re

from app import extractor_bridge

SOURCE_LINE = re.compile(
    r"^SOURCE: projects/Design Reference Library/_scripts/ @ "
    r"(?:[0-9a-f]{7,40}|no-git-snapshot \d{4}-\d{2}-\d{2})$"
)
VENDORED_AT_LINE = re.compile(r"^VENDORED_AT: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def test_vendored_version_file_has_source_and_timestamp() -> None:
    """VERSION should pin the source snapshot and vendoring time."""
    version_path = extractor_bridge.API_ROOT / "_vendored" / "drl" / "VERSION"
    lines = version_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert SOURCE_LINE.match(lines[0])
    assert VENDORED_AT_LINE.match(lines[1])
