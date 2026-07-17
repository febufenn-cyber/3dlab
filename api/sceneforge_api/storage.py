"""Storage adapters: where uploads land and assets are served from.

* S3Storage — any S3-compatible service. Production target is Cloudflare R2
  (zero egress fees; see docs/RUNBOOK.md). Presigned PUT for uploads,
  presigned GET (or a public CDN base) for assets.
* LocalStorage — dev/test: files under a directory, "presigned" URLs are
  plain API-served paths.

Keys layout:  scn_x/video.mp4 · scn_x/assets/scene.ksplat · ...
"""

from __future__ import annotations

import abc
import shutil
from pathlib import Path

from .settings import Settings


class Storage(abc.ABC):
    @abc.abstractmethod
    def presign_upload(self, key: str, content_type: str, expires_s: int) -> str: ...

    @abc.abstractmethod
    def presign_download(self, key: str, expires_s: int) -> str: ...

    @abc.abstractmethod
    def exists(self, key: str) -> bool: ...

    @abc.abstractmethod
    def put_file(self, key: str, local_path: Path) -> None: ...

    @abc.abstractmethod
    def get_file(self, key: str, local_path: Path) -> None: ...

    def public_url(self, key: str, expires_s: int = 3600) -> str:
        return self.presign_download(key, expires_s)


class LocalStorage(Storage):
    """Filesystem-backed dev storage. Upload URLs point at the API's own
    PUT /v1/_local-upload/{key} endpoint (dev only)."""

    def __init__(self, root: Path, api_base: str = ""):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.api_base = api_base.rstrip("/")

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if not p.is_relative_to(self.root.resolve()):
            raise ValueError(f"key escapes storage root: {key}")
        return p

    def presign_upload(self, key: str, content_type: str, expires_s: int) -> str:
        return f"{self.api_base}/v1/_local-upload/{key}"

    def presign_download(self, key: str, expires_s: int) -> str:
        return f"{self.api_base}/v1/_local-download/{key}"

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def put_file(self, key: str, local_path: Path) -> None:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, dest)

    def get_file(self, key: str, local_path: Path) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self._path(key), local_path)

    def put_bytes(self, key: str, data: bytes) -> None:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    def get_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()


class S3Storage(Storage):
    """S3-compatible storage (Cloudflare R2 / Backblaze B2 / MinIO / AWS).

    Credentials come from the standard AWS env vars
    (AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY); endpoint from settings.
    """

    def __init__(self, settings: Settings):
        import boto3  # imported here so tests / local dev don't need it

        if not settings.s3_bucket:
            raise ValueError("SCENEFORGE_S3_BUCKET is required for s3 storage")
        self.bucket = settings.s3_bucket
        self.public_base = settings.public_asset_base.rstrip("/")
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url or None,
            region_name=settings.s3_region or None,
        )

    def presign_upload(self, key: str, content_type: str, expires_s: int) -> str:
        return self.client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=expires_s,
        )

    def presign_download(self, key: str, expires_s: int) -> str:
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires_s
        )

    def public_url(self, key: str, expires_s: int = 3600) -> str:
        if self.public_base:
            return f"{self.public_base}/{key}"
        return self.presign_download(key, expires_s)

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    def put_file(self, key: str, local_path: Path) -> None:
        self.client.upload_file(str(local_path), self.bucket, key)

    def get_file(self, key: str, local_path: Path) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, key, str(local_path))


def make_storage(settings: Settings) -> Storage:
    if settings.storage_backend == "s3":
        return S3Storage(settings)
    return LocalStorage(Path(settings.local_storage_dir))
