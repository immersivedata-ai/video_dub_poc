import os
import io
import json
from pathlib import Path
from typing import Optional
from google.cloud import storage
from google.oauth2 import service_account
from core.config import GCS_BUCKET
from datetime import timedelta

_client: Optional[storage.Client] = None
_signer: Optional[service_account.Credentials] = None


def _get_signer():
    global _signer
    if _signer is None:
        key_json = os.getenv("SIGNER_KEY", "")
        if key_json:
            _signer = service_account.Credentials.from_service_account_info(
                json.loads(key_json),
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
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
    kwargs = {"expiration": timedelta(minutes=expiration_minutes), "version": "v4"}
    signer = _get_signer()
    if signer:
        kwargs["credentials"] = signer
    return blob.generate_signed_url(**kwargs)


def upload_signed_url(gcs_path: str, content_type: str = "video/mp4", expiration_minutes: int = 10) -> str:
    blob = _get_bucket().blob(gcs_path)
    kwargs = {
        "expiration": timedelta(minutes=expiration_minutes),
        "method": "PUT",
        "content_type": content_type,
        "version": "v4"
    }
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
