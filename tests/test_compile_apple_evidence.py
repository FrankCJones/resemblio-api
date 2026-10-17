"""Unit tests for clean-clone Apple compiler check semantics."""
from __future__ import annotations

from scripts.compile_apple_evidence import _required_products_match


def test_required_products_accept_absent_private_ledger() -> None:
    """A clean clone passes when public bytes match and no private ledger exists."""
    assert _required_products_match({"public": True, "private": None})


def test_required_products_accept_matching_private_ledger() -> None:
    """A developer checkout passes when both generated products match."""
    assert _required_products_match({"public": True, "private": True})


def test_required_products_reject_public_or_private_drift() -> None:
    """Public drift and present private-ledger drift remain hard failures."""
    assert not _required_products_match({"public": False, "private": None})
    assert not _required_products_match({"public": True, "private": False})
