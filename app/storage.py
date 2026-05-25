"""Cloudflare R2 storage adapter for extraction ZIP bundles."""
from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from typing import TypeVar

import boto3
from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError

from app.config import Settings, get_settings
from app.constants import DOWNLOAD_URL_TTL_SECONDS

T = TypeVar("T")
R2_BACKOFF_SECONDS = (0.25, 0.75, 1.5)


class R2Storage:
    """Small S3-compatible wrapper for the `resemblio-extractions` bucket."""

    def __init__(self, settings: Settings) -> None:
        """Create a boto3 client from settings without making network calls."""
        if not settings.r2_endpoint or not settings.r2_access_key or not settings.r2_secret_key:
            raise RuntimeError("Cloudflare R2 credentials are required")
        self.bucket = settings.r2_bucket
        self.client: BaseClient = boto3.client(
            "s3",
            endpoint_url=settings.r2_endpoint,
            aws_access_key_id=settings.r2_access_key,
            aws_secret_access_key=settings.r2_secret_key,
            region_name=settings.r2_region,
        )

    def ensure_bucket(self) -> None:
        """Create the bucket if it does not already exist."""
        try:
            self._with_retries(lambda: self.client.head_bucket(Bucket=self.bucket))
        except ClientError as exc:
            status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
            if status not in {403, 404}:
                raise
            self._with_retries(lambda: self.client.create_bucket(Bucket=self.bucket))

    def put_extraction_zip(self, extraction_id: int, user_id: int, zip_bytes: bytes) -> tuple[str, str]:
        """Upload a ZIP bundle and return its object key plus SHA-256 hex."""
        self.ensure_bucket()
        object_key = f"extractions/{user_id}/{extraction_id}.zip"
        sha256_hex = hashlib.sha256(zip_bytes).hexdigest()
        self._with_retries(
            lambda: self.client.put_object(
                Bucket=self.bucket,
                Key=object_key,
                Body=zip_bytes,
                ContentType="application/zip",
            )
        )
        return object_key, sha256_hex

    def get_extraction_zip(self, object_key: str) -> bytes:
        """Download a ZIP bundle by object key."""
        response = self._with_retries(lambda: self.client.get_object(Bucket=self.bucket, Key=object_key))
        return response["Body"].read()

    def sign_download_url(self, object_key: str, expires_in: int = DOWNLOAD_URL_TTL_SECONDS) -> str:
        """Return a short-lived presigned URL for a ZIP bundle."""
        return str(
            self._with_retries(
                lambda: self.client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self.bucket, "Key": object_key},
                    ExpiresIn=expires_in,
                )
            )
        )

    def _with_retries(self, call: Callable[[], T]) -> T:
        """Run one boto call with short exponential backoff on transient errors."""
        last_error: Exception | None = None
        for index, delay in enumerate(R2_BACKOFF_SECONDS):
            try:
                return call()
            except (BotoCoreError, ClientError) as exc:
                last_error = exc
                if index == len(R2_BACKOFF_SECONDS) - 1:
                    break
                time.sleep(delay)
        assert last_error is not None
        raise last_error


def get_storage() -> R2Storage:
    """FastAPI dependency returning an R2 storage adapter."""
    return R2Storage(get_settings())
