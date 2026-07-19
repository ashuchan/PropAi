"""Unit tests for ma_poc.storage.gcs helpers.

No network — uses a MagicMock google.cloud.storage.Client so we exercise
parse_gcs_uri, upload/download, prefix walking, and partial-failure
tolerance in upload_prefix.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ma_poc.storage import gcs


def test_parse_gcs_uri_valid() -> None:
    assert gcs.parse_gcs_uri("gs://my-bucket/path/to/obj") == ("my-bucket", "path/to/obj")
    assert gcs.parse_gcs_uri("gs://my-bucket/") == ("my-bucket", "")
    assert gcs.parse_gcs_uri("gs://my-bucket") == ("my-bucket", "")


def test_parse_gcs_uri_rejects_non_gs() -> None:
    with pytest.raises(ValueError, match="Not a gs"):
        gcs.parse_gcs_uri("s3://bucket/k")
    with pytest.raises(ValueError, match="Not a gs"):
        gcs.parse_gcs_uri("/local/path")


def _mock_blob(content: bytes = b"hello") -> MagicMock:
    blob = MagicMock()

    def _download(filename: str) -> None:
        Path(filename).write_bytes(content)

    blob.download_to_filename.side_effect = _download
    return blob


def test_download_object_writes_destination(tmp_path: Path) -> None:
    client = MagicMock()
    client.bucket.return_value.blob.return_value = _mock_blob(b"payload")

    dest = tmp_path / "sub/dir/out.txt"
    gcs.download_object("gs://b/key/x.txt", dest, client=client)

    assert dest.read_bytes() == b"payload"
    client.bucket.assert_called_once_with("b")
    client.bucket.return_value.blob.assert_called_once_with("key/x.txt")


def test_download_object_rejects_uri_without_object() -> None:
    with pytest.raises(ValueError, match="no object path"):
        gcs.download_object("gs://b/", Path("/tmp/x"), client=MagicMock())


def test_upload_object_requires_src_exists(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        gcs.upload_object(tmp_path / "nope", "gs://b/k", client=MagicMock())


def test_upload_object_calls_upload_from_filename(tmp_path: Path) -> None:
    src = tmp_path / "a.txt"
    src.write_bytes(b"hi")
    client = MagicMock()
    blob = client.bucket.return_value.blob.return_value

    gcs.upload_object(src, "gs://b/dest/a.txt", client=client)

    blob.upload_from_filename.assert_called_once_with(str(src))
    client.bucket.assert_called_once_with("b")
    client.bucket.return_value.blob.assert_called_once_with("dest/a.txt")


def test_download_prefix_walks_blobs_and_strips_prefix(tmp_path: Path) -> None:
    client = MagicMock()

    def make_blob(name: str) -> MagicMock:
        b = _mock_blob(f"body:{name}".encode())
        b.name = name
        return b

    client.list_blobs.return_value = [
        make_blob("runs/2026-04-23/events.jsonl"),
        make_blob("runs/2026-04-23/nested/cost.db"),
        make_blob("runs/2026-04-23/"),  # directory placeholder — skipped
    ]

    n = gcs.download_prefix("gs://b/runs/2026-04-23/", tmp_path, client=client)

    assert n == 2
    assert (tmp_path / "events.jsonl").read_text() == "body:runs/2026-04-23/events.jsonl"
    assert (tmp_path / "nested/cost.db").read_text() == "body:runs/2026-04-23/nested/cost.db"
    client.list_blobs.assert_called_once_with("b", prefix="runs/2026-04-23/")


def test_download_prefix_tolerates_empty(tmp_path: Path) -> None:
    client = MagicMock()
    client.list_blobs.return_value = []
    assert gcs.download_prefix("gs://b/missing/", tmp_path, client=client) == 0


def test_upload_prefix_walks_local_dir(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "nested").mkdir(parents=True)
    (src / "a.txt").write_bytes(b"a")
    (src / "nested" / "b.txt").write_bytes(b"b")

    client = MagicMock()
    bucket = client.bucket.return_value

    n = gcs.upload_prefix(src, "gs://b/runs/", client=client)
    assert n == 2
    # Each blob() call should have been named with the relative path under the prefix.
    names = [call.args[0] for call in bucket.blob.call_args_list]
    assert set(names) == {"runs/a.txt", "runs/nested/b.txt"}


def test_upload_prefix_tolerates_per_file_failure(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "good.txt").write_bytes(b"1")
    (src / "bad.txt").write_bytes(b"2")

    client = MagicMock()
    bucket = client.bucket.return_value

    def fake_blob(name: str) -> MagicMock:
        b = MagicMock()
        if name.endswith("bad.txt"):
            b.upload_from_filename.side_effect = RuntimeError("boom")
        return b

    bucket.blob.side_effect = fake_blob

    # 1 success, 1 failure; function returns 1 and does not raise.
    assert gcs.upload_prefix(src, "gs://b/out/", client=client) == 1


def test_upload_prefix_returns_zero_for_missing_src(tmp_path: Path) -> None:
    assert gcs.upload_prefix(tmp_path / "nope", "gs://b/x/", client=MagicMock()) == 0


# ── 2026-07-19: shard-safe upload — only_modified_after (profile-push race fix) ──


def test_upload_prefix_only_modified_after_skips_unmodified(tmp_path: Path) -> None:
    """The core shard-race fix: with a watermark, only files touched AFTER the
    watermark upload — an untouched (downloaded-then-not-modified) file is
    skipped, so a shard never re-uploads another shard's slice."""
    import os as _os

    src = tmp_path / "src"
    src.mkdir()
    old = src / "unmodified.json"
    new = src / "modified.json"
    old.write_bytes(b"seed")
    new.write_bytes(b"learned")

    watermark = 1_000_000.0
    # old = downloaded before the watermark; new = re-saved by this shard after.
    _os.utime(old, (watermark - 100, watermark - 100))
    _os.utime(new, (watermark + 100, watermark + 100))

    client = MagicMock()
    bucket = client.bucket.return_value

    n = gcs.upload_prefix(src, "gs://b/profiles/", client=client, only_modified_after=watermark)

    assert n == 1
    names = [call.args[0] for call in bucket.blob.call_args_list]
    assert names == ["profiles/modified.json"]  # ONLY the modified file


def test_upload_prefix_none_watermark_uploads_all(tmp_path: Path) -> None:
    """Backward-compat: no watermark → upload everything (single-process/local)."""
    import os as _os

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.json").write_bytes(b"a")
    (src / "b.json").write_bytes(b"b")
    _os.utime(src / "a.json", (1.0, 1.0))  # ancient mtime must NOT be skipped

    client = MagicMock()
    n = gcs.upload_prefix(src, "gs://b/p/", client=client, only_modified_after=None)
    assert n == 2


def test_upload_prefix_watermark_skips_all_when_none_modified(tmp_path: Path) -> None:
    import os as _os

    src = tmp_path / "src"
    src.mkdir()
    for name in ("x.json", "y.json", "z.json"):
        f = src / name
        f.write_bytes(b"seed")
        _os.utime(f, (500.0, 500.0))  # all before the watermark

    client = MagicMock()
    n = gcs.upload_prefix(src, "gs://b/p/", client=client, only_modified_after=1000.0)
    assert n == 0  # nothing modified this shard → nothing uploaded → no clobber
