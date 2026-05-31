"""
Object storage — thin async wrapper over the S3-compatible bucket (Railway/Tigris).

boto3 is synchronous, so each call runs in a threadpool to avoid blocking the
event loop. The client is built lazily and cached: one signed client per process,
reused by the web app and the worker alike.
"""
from __future__ import annotations

import asyncio
import logging

import boto3

from app.config import settings

logger = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=settings.bucket_endpoint_url,
            region_name=settings.bucket_region,
            aws_access_key_id=settings.bucket_access_key_id,
            aws_secret_access_key=settings.bucket_secret_access_key,
        )
    return _client


async def upload_bytes(key: str, data: bytes, content_type: str | None = None) -> None:
    """Store ``data`` at ``key`` in the bucket."""
    extra = {"ContentType": content_type} if content_type else {}
    await asyncio.to_thread(
        _get_client().put_object,
        Bucket=settings.bucket_name,
        Key=key,
        Body=data,
        **extra,
    )
    logger.info("storage: uploaded %s (%d bytes)", key, len(data))


async def download_bytes(key: str) -> bytes:
    """Fetch the object at ``key`` as bytes (used by the ingest worker)."""
    resp = await asyncio.to_thread(
        _get_client().get_object,
        Bucket=settings.bucket_name,
        Key=key,
    )
    return await asyncio.to_thread(resp["Body"].read)
