"""Tests for image file storage."""
from __future__ import annotations

import struct
import zlib

import pytest
from pathlib import Path

from lightfall_logbook.image_store import ImageStore, ImageStoreError


def _make_minimal_png() -> bytes:
    """1x1 white PNG."""
    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        c = chunk_type + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    header = b"\x89PNG\r\n\x1a\n"
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    raw = zlib.compress(b"\x00\xff\xff\xff")
    idat = _chunk(b"IDAT", raw)
    iend = _chunk(b"IEND", b"")
    return header + ihdr + idat + iend


@pytest.fixture
def store(tmp_path: Path) -> ImageStore:
    return ImageStore(storage_dir=tmp_path)


def test_save_image_creates_file(store: ImageStore, tmp_path: Path):
    png_bytes = _make_minimal_png()
    image_id = store.save(png_bytes, "image/png")

    saved = tmp_path / f"{image_id}.png"
    assert saved.exists()
    assert saved.read_bytes() == png_bytes


def test_save_jpeg(store: ImageStore, tmp_path: Path):
    jpeg_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 20 + b"\xff\xd9"
    image_id = store.save(jpeg_bytes, "image/jpeg")
    assert (tmp_path / f"{image_id}.jpg").exists()


def test_save_rejects_unsupported_mime(store: ImageStore):
    with pytest.raises(ImageStoreError, match="Unsupported mime type"):
        store.save(b"data" * 10, "image/tiff")


def test_save_rejects_oversized(store: ImageStore):
    big = b"\x00" * (20 * 1024 * 1024 + 1)
    with pytest.raises(ImageStoreError, match="too large"):
        store.save(big, "image/png")


def test_save_rejects_too_small(store: ImageStore):
    with pytest.raises(ImageStoreError, match="too small"):
        store.save(b"\x00" * 4, "image/png")


def test_load_returns_bytes_and_mime(store: ImageStore):
    png = _make_minimal_png()
    image_id = store.save(png, "image/png")
    data, mime = store.load(image_id)
    assert data == png
    assert mime == "image/png"


def test_load_missing_raises(store: ImageStore):
    with pytest.raises(ImageStoreError, match="not found"):
        store.load("nonexistent-id")


def test_delete_removes_file(store: ImageStore, tmp_path: Path):
    png = _make_minimal_png()
    image_id = store.save(png, "image/png")
    assert store.delete(image_id) is True
    assert not (tmp_path / f"{image_id}.png").exists()


def test_delete_missing_returns_false(store: ImageStore):
    assert store.delete("nonexistent-id") is False


def test_exists(store: ImageStore):
    png = _make_minimal_png()
    image_id = store.save(png, "image/png")
    assert store.exists(image_id) is True
    assert store.exists("nonexistent") is False
