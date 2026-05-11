"""Tests for image upload/download/delete API endpoints."""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest
from litestar.testing import AsyncTestClient

from lucid_logbook.app import create_app


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
def image_dir(tmp_path: Path) -> Path:
    d = tmp_path / "images"
    d.mkdir()
    return d


@pytest.fixture
async def client(tmp_path: Path, image_dir: Path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("IMAGE_STORAGE_DIR", str(image_dir))
    app = create_app()
    async with AsyncTestClient(app=app) as tc:
        yield tc


HEADERS = {"X-User-Id": "test-user"}


@pytest.mark.asyncio
async def test_upload_image(client, image_dir: Path):
    png = _make_minimal_png()
    resp = await client.post(
        "/logbook/images",
        files={"file": ("test.png", png, "image/png")},
        headers=HEADERS,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "image_id" in body
    assert body["mime_type"] == "image/png"
    assert (image_dir / f"{body['image_id']}.png").exists()


@pytest.mark.asyncio
async def test_download_image(client, image_dir: Path):
    png = _make_minimal_png()
    upload = await client.post(
        "/logbook/images",
        files={"file": ("test.png", png, "image/png")},
        headers=HEADERS,
    )
    image_id = upload.json()["image_id"]

    resp = await client.get(f"/logbook/images/{image_id}", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == png


@pytest.mark.asyncio
async def test_download_missing_image(client):
    resp = await client.get("/logbook/images/nonexistent", headers=HEADERS)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_image(client, image_dir: Path):
    png = _make_minimal_png()
    upload = await client.post(
        "/logbook/images",
        files={"file": ("test.png", png, "image/png")},
        headers=HEADERS,
    )
    image_id = upload.json()["image_id"]

    resp = await client.delete(f"/logbook/images/{image_id}", headers=HEADERS)
    assert resp.status_code == 204
    assert not (image_dir / f"{image_id}.png").exists()


@pytest.mark.asyncio
async def test_upload_rejects_unsupported_type(client):
    resp = await client.post(
        "/logbook/images",
        files={"file": ("test.bmp", b"fake-bmp-data-padding!!", "image/bmp")},
        headers=HEADERS,
    )
    assert resp.status_code == 400
