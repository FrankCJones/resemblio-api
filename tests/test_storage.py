"""Tests for R2 storage through moto's S3 mock."""
from __future__ import annotations

from moto import mock_aws

from app.config import Settings
from app.storage import R2Storage


@mock_aws
def test_storage_put_get_and_sign_round_trip() -> None:
    """R2Storage uploads, downloads, and signs against mocked S3."""
    settings = Settings(
        key_pepper="test-pepper-value-with-thirty-two-chars",
        r2_endpoint="https://s3.amazonaws.com",
        r2_access_key="access",
        r2_secret_key="secret",
        r2_bucket="resemblio-extractions",
        r2_region="us-east-1",
    )
    storage = R2Storage(settings)
    key, digest = storage.put_extraction_zip(9, 3, b"zip-bytes")
    assert key == "extractions/3/9.zip"
    assert digest
    assert storage.get_extraction_zip(key) == b"zip-bytes"
    assert key in storage.sign_download_url(key)
