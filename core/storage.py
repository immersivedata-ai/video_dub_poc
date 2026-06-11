import os
import io
from pathlib import Path
from typing import Optional
from google.cloud import storage
from google.oauth2 import service_account
from core.config import GCS_BUCKET

_client: Optional[storage.Client] = None
_signer: Optional[service_account.Credentials] = None

SIGNER_KEY = os.getenv("SIGNER_KEY_PATH", "")


def _get_signer():
    global _signer
    if _signer is None and SIGNER_KEY and os.path.exists(SIGNER_KEY):
        _signer = service_account.Credentials.from_service_account_file(SIGNER_KEY)
    return _signer


def _get_client() -> storage.Client:
    global _client
    if _client is None:
        _client = storage.Client()
    return _client


def _get_bucket():
    if not GCS_BUCKET:
        raise RuntimeError("GCS_BUCKET not configured")
    return _get_client().bucket(GCS_BUCKET)


def upload_file(local_path: str, gcs_path: str) -> str:
    """Upload a file to GCS. Returns gsutil URI."""
    blob = _get_bucket().blob(gcs_path)
    blob.upload_from_filename(local_path)
    return f"gs://{GCS_BUCKET}/{gcs_path}"


def upload_bytes(data: bytes, gcs_path: str, content_type: str = "application/octet-stream") -> str:
    """Upload bytes to GCS."""
    blob = _get_bucket().blob(gcs_path)
    blob.upload_from_string(data, content_type=content_type)
    return f"gs://{GCS_BUCKET}/{gcs_path}"


def download_to_path(gcs_path: str, local_path: str) -> str:
    """Download a file from GCS to local path."""
    blob = _get_bucket().blob(gcs_path)
    blob.download_to_filename(local_path)
    return local_path


def signed_url(gcs_path: str, expiration_minutes: int = 60) -> str:
    blob = _get_bucket().blob(gcs_path)
    signer = _get_signer()
    kwargs = {"expiration": expiration_minutes * 60}
    if signer:
        kwargs["credentials"] = signer
    return blob.generate_signed_url(**kwargs)


def upload_signed_url(gcs_path: str, content_type: str = "video/mp4", expiration_minutes: int = 10) -> str:
    blob = _get_bucket().blob(gcs_path)
    kwargs = {"expiration": expiration_minutes * 60, "method": "PUT", "content_type": content_type}
    signer = _get_signer()
    if signer:
        kwargs["credentials"] = signer
    return blob.generate_signed_url(**kwargs)


def exists(gcs_path: str) -> bool:
    return _get_bucket().blob(gcs_path).exists()


def delete(gcs_path: str) -> None:
    _get_bucket().blob(gcs_path).delete()


def is_configured() -> bool:
    return bool(GCS_BUCKET)
