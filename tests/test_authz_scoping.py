"""Authorization scoping: a user must not be able to read or mutate another
user's entries or fragments by guessing their UUID (IDOR).

Every per-resource endpoint authenticates the caller, but must also verify the
resource belongs to the caller's logbook. Cross-user access returns 404 (we do
not reveal that the resource exists).
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest
from litestar.testing import AsyncTestClient

from lightfall_logbook.app import create_app

ALICE = {"X-User-Id": "alice"}
BOB = {"X-User-Id": "bob"}


def _make_minimal_png() -> bytes:
    """1x1 white PNG."""
    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        c = chunk_type + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    header = b"\x89PNG\r\n\x1a\n"
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    idat = _chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff"))
    iend = _chunk(b"IEND", b"")
    return header + ihdr + idat + iend


@pytest.fixture
async def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("IMAGE_STORAGE_DIR", str(tmp_path / "images"))
    app = create_app()
    async with AsyncTestClient(app=app) as tc:
        yield tc


@pytest.fixture
async def alice_entry(client):
    """Alice owns an entry with one text fragment. Returns (entry_id, fragment_id)."""
    entry = await client.post(
        "/logbook/entries", json={"title": "alice secret"}, headers=ALICE
    )
    entry_id = entry.json()["id"]
    frag = await client.post(
        f"/logbook/entries/{entry_id}/fragments",
        json={"kind": "text", "content": "private"},
        headers=ALICE,
    )
    return entry_id, frag.json()["id"]


@pytest.mark.asyncio
async def test_bob_cannot_get_alice_entry(client, alice_entry):
    entry_id, _ = alice_entry
    resp = await client.get(f"/logbook/entries/{entry_id}", headers=BOB)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_bob_cannot_update_alice_entry(client, alice_entry):
    entry_id, _ = alice_entry
    resp = await client.put(
        f"/logbook/entries/{entry_id}", json={"title": "hacked"}, headers=BOB
    )
    assert resp.status_code == 404
    # Alice's entry is untouched.
    check = await client.get(f"/logbook/entries/{entry_id}", headers=ALICE)
    assert check.json()["title"] == "alice secret"


@pytest.mark.asyncio
async def test_bob_cannot_delete_alice_entry(client, alice_entry):
    entry_id, _ = alice_entry
    resp = await client.delete(f"/logbook/entries/{entry_id}", headers=BOB)
    assert resp.status_code == 404
    # Still there for alice.
    check = await client.get(f"/logbook/entries/{entry_id}", headers=ALICE)
    assert check.status_code == 200


@pytest.mark.asyncio
async def test_bob_cannot_add_fragment_to_alice_entry(client, alice_entry):
    entry_id, _ = alice_entry
    resp = await client.post(
        f"/logbook/entries/{entry_id}/fragments",
        json={"kind": "text", "content": "injected"},
        headers=BOB,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_bob_cannot_update_alice_fragment(client, alice_entry):
    _, fragment_id = alice_entry
    resp = await client.put(
        f"/logbook/fragments/{fragment_id}", json={"content": "tampered"}, headers=BOB
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_bob_cannot_delete_alice_fragment(client, alice_entry):
    entry_id, fragment_id = alice_entry
    resp = await client.delete(f"/logbook/fragments/{fragment_id}", headers=BOB)
    assert resp.status_code == 404
    # Fragment still present for alice.
    entry = await client.get(f"/logbook/entries/{entry_id}", headers=ALICE)
    frag_ids = [f["id"] for f in entry.json()["fragments"]]
    assert fragment_id in frag_ids


@pytest.fixture
async def alice_image(client):
    """Alice uploads an image. Returns its image_id."""
    upload = await client.post(
        "/logbook/images",
        files={"file": ("a.png", _make_minimal_png(), "image/png")},
        headers=ALICE,
    )
    return upload.json()["image_id"]


@pytest.mark.asyncio
async def test_bob_cannot_download_alice_image(client, alice_image):
    resp = await client.get(f"/logbook/images/{alice_image}", headers=BOB)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_bob_cannot_delete_alice_image(client, alice_image, tmp_path):
    resp = await client.delete(f"/logbook/images/{alice_image}", headers=BOB)
    assert resp.status_code == 404
    # Still downloadable by the owner.
    owner_resp = await client.get(f"/logbook/images/{alice_image}", headers=ALICE)
    assert owner_resp.status_code == 200


@pytest.mark.asyncio
async def test_owner_can_download_own_image(client, alice_image):
    resp = await client.get(f"/logbook/images/{alice_image}", headers=ALICE)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_owner_still_has_full_access(client, alice_entry):
    """Regression guard: the ownership check must not lock out the owner."""
    entry_id, fragment_id = alice_entry

    assert (await client.get(f"/logbook/entries/{entry_id}", headers=ALICE)).status_code == 200
    assert (
        await client.put(
            f"/logbook/entries/{entry_id}", json={"title": "edited"}, headers=ALICE
        )
    ).status_code == 200
    assert (
        await client.put(
            f"/logbook/fragments/{fragment_id}", json={"content": "edited"}, headers=ALICE
        )
    ).status_code == 200
    assert (
        await client.post(
            f"/logbook/entries/{entry_id}/fragments",
            json={"kind": "text", "content": "more"},
            headers=ALICE,
        )
    ).status_code == 201
    assert (
        await client.delete(f"/logbook/fragments/{fragment_id}", headers=ALICE)
    ).status_code == 204
    assert (
        await client.delete(f"/logbook/entries/{entry_id}", headers=ALICE)
    ).status_code == 204
