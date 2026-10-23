"""Tests for image upload/download/delete API endpoints."""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest
from litestar.testing import AsyncTestClient

from lightfall_logbook.app import create_app


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


# -- Client-chosen image_id (offline-first Lightfall uploads) ----------------

CLIENT_ID = "0b6f3c1e-2d4a-4f6b-9c8d-7e5f4a3b2c1d"


async def _upload(client, headers=HEADERS, image_id: str | None = CLIENT_ID):
    params = {"image_id": image_id} if image_id is not None else None
    return await client.post(
        "/logbook/images",
        params=params,
        files={"file": ("test.png", _make_minimal_png(), "image/png")},
        headers=headers,
    )


@pytest.mark.asyncio
async def test_upload_keeps_client_image_id(client, image_dir: Path):
    resp = await _upload(client)

    assert resp.status_code == 201
    assert resp.json()["image_id"] == CLIENT_ID
    assert (image_dir / f"{CLIENT_ID}.png").exists()
    got = await client.get(f"/logbook/images/{CLIENT_ID}", headers=HEADERS)
    assert got.status_code == 200


@pytest.mark.asyncio
async def test_reupload_of_own_image_id_is_idempotent(client, image_dir: Path):
    assert (await _upload(client)).status_code == 201

    again = await _upload(client)

    assert again.status_code == 201
    assert again.json()["image_id"] == CLIENT_ID
    assert sorted(p.name for p in image_dir.iterdir()) == [f"{CLIENT_ID}.png"]


@pytest.mark.asyncio
async def test_upload_rejects_image_id_owned_by_another_user(client):
    assert (await _upload(client, headers={"X-User-Id": "alice"})).status_code == 201

    resp = await _upload(client, headers=HEADERS)

    assert resp.status_code == 409
    # alice still owns it; test-user cannot read it
    assert (await client.get(f"/logbook/images/{CLIENT_ID}", headers=HEADERS)).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["../../etc/passwd", "not-a-uuid", ""])
async def test_upload_rejects_non_uuid_image_id(client, image_dir: Path, bad: str):
    resp = await _upload(client, image_id=bad)

    assert resp.status_code == 400
    assert list(image_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_image_fragment_resolves_after_client_id_upload(client):
    """The fragment's image_id (chosen offline) now names a real, readable image."""
    await _upload(client)
    entry = (await client.post("/logbook/entries", json={}, headers=HEADERS)).json()
    await client.post(
        f"/logbook/entries/{entry['id']}/fragments",
        json={"kind": "image", "content": "", "data": {"image_id": CLIENT_ID}},
        headers=HEADERS,
    )

    entry_resp = await client.get(f"/logbook/entries/{entry['id']}", headers=HEADERS)
    frag = entry_resp.json()["fragments"][0]
    got = await client.get(f"/logbook/images/{frag['data']['image_id']}", headers=HEADERS)
    assert got.status_code == 200
