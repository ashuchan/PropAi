"""GCS helpers used by Cloud Run job entrypoints.

Previously the entry scripts shelled out to ``gsutil`` for upload/download.
That works when the container has the gcloud SDK installed, but the prod
image was slimmed to drop gcloud (to shave ~300MB), and subprocess calls
started failing at runtime with ``FileNotFoundError: 'gsutil'``. This
module uses ``google-cloud-storage`` directly so the hot path has no
external binary dependency.

All helpers:
  - Parse ``gs://bucket/path`` URIs into (bucket, prefix).
  - Authenticate via ADC (the job's service account).
  - Are best-effort on ``upload_prefix`` — partial failures are logged;
    entry scripts call them from finally blocks where the runner's exit
    code must not be suppressed.

Not a thin wrapper around gsutil: ``download_prefix`` copies every object
under a prefix verbatim, preserving relative paths; ``upload_prefix``
walks a local directory and writes each file under the URI prefix.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from google.cloud import storage

log = logging.getLogger(__name__)


def parse_gcs_uri(uri: str) -> tuple[str, str]:
    """Split ``gs://bucket/path/to/obj`` into ``("bucket", "path/to/obj")``.

    Raises ``ValueError`` on anything else. Empty object path is allowed
    (e.g. ``gs://bucket`` or ``gs://bucket/``) and yields an empty prefix.
    """
    parsed = urlparse(uri)
    if parsed.scheme != "gs" or not parsed.netloc:
        raise ValueError(f"Not a gs:// URI: {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def download_object(uri: str, dest: Path, *, client: storage.Client | None = None) -> None:
    """Download a single object ``gs://bucket/path`` to ``dest``.

    Creates parent dirs. Raises ``google.api_core.exceptions.NotFound`` if
    the object does not exist.
    """
    bucket_name, blob_name = parse_gcs_uri(uri)
    if not blob_name:
        raise ValueError(f"URI has no object path: {uri!r}")
    client = client or storage.Client()
    blob = client.bucket(bucket_name).blob(blob_name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    blob.download_to_filename(str(dest))
    log.info("downloaded %s → %s (%d bytes)", uri, dest, dest.stat().st_size)


def upload_object(src: Path, uri: str, *, client: storage.Client | None = None) -> None:
    """Upload a local file ``src`` to ``gs://bucket/path``.

    Overwrites existing object. Raises ``FileNotFoundError`` if ``src``
    doesn't exist.
    """
    bucket_name, blob_name = parse_gcs_uri(uri)
    if not blob_name:
        raise ValueError(f"URI has no object path: {uri!r}")
    if not src.exists():
        raise FileNotFoundError(src)
    client = client or storage.Client()
    blob = client.bucket(bucket_name).blob(blob_name)
    blob.upload_from_filename(str(src))
    log.info("uploaded %s → %s", src, uri)


def download_prefix(
    uri_prefix: str,
    dest_dir: Path,
    *,
    client: storage.Client | None = None,
) -> int:
    """Download every object under ``gs://bucket/prefix/`` into ``dest_dir``.

    The ``prefix`` is treated as a directory boundary — object names have
    it stripped before being joined onto ``dest_dir``. Missing prefix is
    tolerated (returns 0); tolerates fresh-day runs where nothing exists.

    Returns the number of objects downloaded.
    """
    bucket_name, prefix = parse_gcs_uri(uri_prefix)
    prefix = prefix.rstrip("/") + "/" if prefix else ""
    client = client or storage.Client()
    dest_dir.mkdir(parents=True, exist_ok=True)

    bucket = client.bucket(bucket_name)
    count = 0
    for blob in client.list_blobs(bucket_name, prefix=prefix):
        # Strip prefix → relative path; skip "directory placeholders" (empty)
        rel = blob.name[len(prefix) :]
        if not rel or rel.endswith("/"):
            continue
        local = dest_dir / rel
        local.parent.mkdir(parents=True, exist_ok=True)
        try:
            blob.download_to_filename(str(local))
        except Exception as exc:
            # ``list_blobs`` returns a generation-pinned Blob. During a
            # parallel canary another shard can replace that profile between
            # listing and download, making the pinned generation return 404
            # even though a newer object exists. Retry exactly once through a
            # fresh, unpinned handle; permanent errors still propagate.
            log.info(
                "download generation changed for gs://%s/%s; retrying latest: %s",
                bucket_name,
                blob.name,
                exc,
            )
            bucket.blob(blob.name).download_to_filename(str(local))
        count += 1
    log.info("downloaded %d objects from %s → %s", count, uri_prefix, dest_dir)
    return count


def upload_prefix(
    src_dir: Path,
    uri_prefix: str,
    *,
    client: storage.Client | None = None,
    only_modified_after: float | None = None,
) -> int:
    """Walk ``src_dir`` and upload each file under ``uri_prefix``.

    Preserves relative paths. Best-effort: per-file errors are logged and
    skipped, so a broken file doesn't mask the runner's actual exit code.
    Returns the number of objects successfully uploaded.

    ``only_modified_after`` (a ``time.time()`` epoch) makes the upload
    SHARD-SAFE: only files whose mtime is strictly greater are uploaded.
    Without it, every parallel Cloud Run shard — which downloads the WHOLE
    shared prefix at startup, mutates only its own ~1/N slice, then re-uploads
    the whole tree — clobbers the other shards' fresh writes with the stale
    copies it downloaded (last-writer-wins). Passing the post-download
    watermark uploads only the profiles THIS shard actually modified, so no
    object is ever written by two shards and no learning is lost. ``None``
    preserves the upload-everything behaviour for single-process / local runs.
    """
    bucket_name, prefix = parse_gcs_uri(uri_prefix)
    prefix = prefix.rstrip("/") + "/" if prefix else ""
    if not src_dir.exists() or not src_dir.is_dir():
        log.warning("upload_prefix: %s missing or not a dir; nothing to do", src_dir)
        return 0
    client = client or storage.Client()
    bucket = client.bucket(bucket_name)

    count = 0
    skipped = 0
    for root, _, files in os.walk(src_dir):
        for fname in files:
            src = Path(root) / fname
            if only_modified_after is not None:
                try:
                    if src.stat().st_mtime <= only_modified_after:
                        skipped += 1
                        continue
                except OSError:
                    pass  # can't stat → fall through and attempt the upload
            rel = src.relative_to(src_dir).as_posix()
            blob = bucket.blob(prefix + rel)
            try:
                blob.upload_from_filename(str(src))
                count += 1
            except Exception as exc:  # noqa: BLE001 — don't suppress runner exit code
                log.warning("upload_prefix: failed %s → %s%s: %s", src, uri_prefix, rel, exc)
    log.info(
        "uploaded %d objects from %s → %s (%d unmodified skipped)",
        count, src_dir, uri_prefix, skipped,
    )
    return count
