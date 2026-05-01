"""Profile-pic two-step flow: image upload then settings update.

Verifies the server-side hook deletes the previously-set image's bytes
when profile_image_id is updated."""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest
from litestar.testing import AsyncTestClient

from lucid_logbook.app import create_app


def _make_minimal_png() -> bytes:
    """Return bytes for a 1x1 white PNG."""
    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        c = chunk_type + data
        return (
            struct.pack(">I", len(data))
            + c
            + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        )

    header = b"\x89PNG\r\n\x1a\n"
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    raw = zlib.compress(b"\x00\xff\xff\xff")
    idat = _chunk(b"IDAT", raw)
    iend = _chunk(b"IEND", b"")
    return header + ihdr + idat + iend


@pytest.fixture
async def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/test.db")
    image_dir = tmp_path / "images"
    monkeypatch.setenv("IMAGE_STORAGE_DIR", str(image_dir))
    yield image_dir


@pytest.fixture
async def client(env):
    app = create_app()
    async with AsyncTestClient(app=app) as tc:
        yield tc


HEADERS = {"X-User-Id": "alice"}


@pytest.mark.asyncio
async def test_replacing_profile_image_deletes_old_bytes(client, env: Path):
    # Upload first image
    png = _make_minimal_png()
    up1 = await client.post(
        "/logbook/images",
        files={"file": ("a.png", png, "image/png")},
        headers=HEADERS,
    )
    id1 = up1.json()["image_id"]
    assert (env / f"{id1}.png").exists()

    # Set as profile_image_id
    r = await client.put(
        "/logbook/settings/profile_image_id",
        json={"value": id1},
        headers=HEADERS,
    )
    assert r.status_code == 200

    # Upload a second image
    up2 = await client.post(
        "/logbook/images",
        files={"file": ("b.png", png, "image/png")},
        headers=HEADERS,
    )
    id2 = up2.json()["image_id"]
    assert id2 != id1

    # Update profile_image_id
    r = await client.put(
        "/logbook/settings/profile_image_id",
        json={"value": id2},
        headers=HEADERS,
    )
    assert r.status_code == 200

    # First image bytes are gone, second remain
    assert not (env / f"{id1}.png").exists()
    assert (env / f"{id2}.png").exists()


@pytest.mark.asyncio
async def test_first_set_profile_image_id_no_hook_failure(client, env: Path):
    """Setting profile_image_id when no prior value exists must not error."""
    png = _make_minimal_png()
    up = await client.post(
        "/logbook/images",
        files={"file": ("a.png", png, "image/png")},
        headers=HEADERS,
    )
    image_id = up.json()["image_id"]

    r = await client.put(
        "/logbook/settings/profile_image_id",
        json={"value": image_id},
        headers=HEADERS,
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_old_image_delete_failure_does_not_fail_put(
    client, env: Path, monkeypatch
):
    """If image_store.delete raises, the PUT still succeeds."""
    png = _make_minimal_png()
    up1 = await client.post(
        "/logbook/images",
        files={"file": ("a.png", png, "image/png")},
        headers=HEADERS,
    )
    id1 = up1.json()["image_id"]
    await client.put(
        "/logbook/settings/profile_image_id",
        json={"value": id1},
        headers=HEADERS,
    )

    # Patch the ImageStore.delete bound to the app
    app = client.app
    original_delete = app.state.image_store.delete

    def boom(image_id: str) -> bool:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(app.state.image_store, "delete", boom)

    up2 = await client.post(
        "/logbook/images",
        files={"file": ("b.png", png, "image/png")},
        headers=HEADERS,
    )
    id2 = up2.json()["image_id"]

    r = await client.put(
        "/logbook/settings/profile_image_id",
        json={"value": id2},
        headers=HEADERS,
    )
    assert r.status_code == 200
    # Restore
    monkeypatch.setattr(app.state.image_store, "delete", original_delete)
