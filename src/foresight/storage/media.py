"""Pluggable attachment storage: local filesystem or S3 compatible."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Protocol

from foresight.config import MediaConfig, S3Config


class MediaStore(Protocol):
    """Where attachment bytes live."""

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        """Store bytes and return a URI that points at them."""
        ...

    async def exists(self, key: str) -> bool: ...

    def open(self, key: str) -> AsyncIterator[bytes]: ...


def media_key(attachment_id: int, filename: str | None, now: datetime) -> str:
    """Build a collision free storage key from the attachment id.

    The name of the file is not part of the key: two attachments may carry
    the same name, and a name may carry a path. Only a short and harmless
    extension is kept, so the key stays inside its directory.
    """
    extension = ""
    if filename and "." in filename:
        suffix = filename.rsplit(".", 1)[-1]
        if suffix.isalnum() and len(suffix) <= 8:
            extension = f".{suffix.lower()}"
    return f"{now:%Y/%m/%d}/{attachment_id}{extension}"


class LocalMediaStore:
    """Stores attachments on the local filesystem."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    def _path(self, key: str) -> Path:
        return self._root / key

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        path = self._path(key)
        # The file may be large, so it is written in a thread: the event
        # loop of the collector must not wait for the disk.
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, data)
        return path.resolve().as_uri()

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._path(key).exists)

    async def open(self, key: str) -> AsyncIterator[bytes]:
        path = self._path(key)
        if await asyncio.to_thread(path.exists):
            yield await asyncio.to_thread(path.read_bytes)


class S3MediaStore:
    """Stores attachments in an S3 compatible bucket.

    AWS S3, MinIO, Cloudflare R2 and Aliyun OSS all speak the S3 protocol,
    so one client serves them all: they differ in the endpoint and in the
    way the bucket is addressed.
    """

    def __init__(self, config: S3Config) -> None:
        self._config = config

    def _full_key(self, key: str) -> str:
        prefix = self._config.prefix.strip("/")
        return f"{prefix}/{key}" if prefix else key

    def _client(self):  # noqa: ANN202
        import aioboto3
        from botocore.config import Config as BotoConfig

        # MinIO, R2 and OSS are reached with the path style: the bucket is
        # a path of the endpoint and not a name of a DNS host.
        addressing = "path" if self._config.use_path_style else "auto"
        return aioboto3.Session().client(
            "s3",
            endpoint_url=self._config.endpoint_url or None,
            region_name=self._config.region,
            aws_access_key_id=self._config.access_key_id or None,
            aws_secret_access_key=self._config.secret_access_key or None,
            config=BotoConfig(s3={"addressing_style": addressing}),
        )

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        full_key = self._full_key(key)
        async with self._client() as client:
            await client.put_object(
                Bucket=self._config.bucket,
                Key=full_key,
                Body=data,
                ContentType=content_type or mimetypes.guess_type(key)[0] or "application/octet-stream",
            )
        return f"s3://{self._config.bucket}/{full_key}"

    async def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        async with self._client() as client:
            try:
                await client.head_object(
                    Bucket=self._config.bucket, Key=self._full_key(key)
                )
            except ClientError:
                return False
        return True

    async def open(self, key: str) -> AsyncIterator[bytes]:
        async with self._client() as client:
            response = await client.get_object(
                Bucket=self._config.bucket, Key=self._full_key(key)
            )
            # The body is read through the stream itself: entering the
            # `async with` of the body hands out the raw response, whose
            # `read` takes no size and would read the whole file at once.
            body = response["Body"]
            async for chunk in body.iter_chunks(65536):
                yield chunk


def build_media_store(config: MediaConfig) -> MediaStore:
    if config.backend == "s3":
        return S3MediaStore(config.s3)
    return LocalMediaStore(config.local_root)


def sha256_hex(data: bytes) -> str:
    """The hex digest of the bytes, as the attachment row carries it."""
    return hashlib.sha256(data).hexdigest()
