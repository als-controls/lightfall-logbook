"""Tests for image fragment lifecycle — creation and cascade delete."""
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
async def test_deleting_image_fragment_removes_image_file(client, image_dir: Path):
    # Upload image
    png = _make_minimal_png()
    upload = await client.post(
        "/logbook/images",
        files={"file": ("test.png", png, "image/png")},
        headers=HEADERS,
    )
    image_id = upload.json()["image_id"]
    assert (image_dir / f"{image_id}.png").exists()

    # Create entry
    entry_resp = await client.post("/logbook/entries", json={}, headers=HEADERS)
    entry_id = entry_resp.json()["id"]

    # Create image fragment
    frag_resp = await client.post(
        f"/logbook/entries/{entry_id}/fragments",
        json={
            "kind": "image",
            "content": "A test caption",
            "data": {
                "image_id": image_id,
                "filename": "test.png",
                "mime_type": "image/png",
                "size_bytes": len(png),
            },
        },
        headers=HEADERS,
    )
    assert frag_resp.status_code == 201
    fragment_id = frag_resp.json()["id"]

    # Delete fragment — should also delete the image file
    del_resp = await client.delete(f"/logbook/fragments/{fragment_id}", headers=HEADERS)
    assert del_resp.status_code == 204
    assert not (image_dir / f"{image_id}.png").exists()


@pytest.mark.asyncio
async def test_readonly_fragment_cannot_be_deleted(client):
    """Ensure readonly fragments are still rejected for deletion."""
    entry_resp = await client.post("/logbook/entries", json={}, headers=HEADERS)
    entry_id = entry_resp.json()["id"]

    frag_resp = await client.post(
        f"/logbook/entries/{entry_id}/fragments",
        json={"kind": "readonly", "content": "system event", "subtype": "device_change"},
        headers=HEADERS,
    )
    fragment_id = frag_resp.json()["id"]

    del_resp = await client.delete(f"/logbook/fragments/{fragment_id}", headers=HEADERS)
    assert del_resp.status_code == 400
