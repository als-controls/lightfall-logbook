"""PUT /logbook/fragments/{id} for non-text fragments.

Readonly-ness is enforced by the Lightfall UI, not the server: the owner may
update readonly (e.g. bluesky run-card) and image fragments like text ones.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from litestar.testing import AsyncTestClient

from lightfall_logbook.app import create_app
from lightfall_logbook.local_store import LocalStore

HEADERS = {"X-User-Id": "test-user"}
OTHER = {"X-User-Id": "other-user"}

RUN_START = {
    "kind": "readonly",
    "subtype": "bluesky_plan",
    "content": "count [abc123]",
    "data": {"plan_name": "count", "uid": "abc123"},
}


@pytest.fixture
async def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("IMAGE_STORAGE_DIR", str(tmp_path / "images"))
    async with AsyncTestClient(app=create_app()) as tc:
        yield tc


async def _make_fragment(client, payload: dict) -> dict:
    entry = await client.post("/logbook/entries", json={}, headers=HEADERS)
    resp = await client.post(
        f"/logbook/entries/{entry.json()['id']}/fragments", json=payload, headers=HEADERS
    )
    assert resp.status_code == 201
    return resp.json()


async def _get_fragment(client, frag: dict) -> dict:
    entry = await client.get(f"/logbook/entries/{frag['entry_id']}", headers=HEADERS)
    return next(f for f in entry.json()["fragments"] if f["id"] == frag["id"])


@pytest.mark.asyncio
async def test_put_updates_readonly_fragment(client):
    frag = await _make_fragment(client, RUN_START)
    new_data = {**RUN_START["data"], "exit_status": "success", "num_events": 5}

    resp = await client.put(
        f"/logbook/fragments/{frag['id']}",
        json={"content": "count [abc123] — success", "position": 3, "data": new_data},
        headers=HEADERS,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "count [abc123] — success"
    assert body["position"] == 3
    assert body["data"] == new_data
    assert body["kind"] == "readonly"
    assert body["subtype"] == "bluesky_plan"
    assert body["updated_at"] > frag["updated_at"]
    # Persisted, not just echoed
    stored = await _get_fragment(client, frag)
    assert stored["data"] == new_data
    assert stored["content"] == "count [abc123] — success"


@pytest.mark.asyncio
async def test_put_accepts_full_client_row(client):
    """The Lightfall client PUTs its whole local row; extra keys are ignored."""
    frag = await _make_fragment(client, RUN_START)
    row = {
        **frag,
        "kind": "text",  # kind/subtype are immutable; must be ignored
        "subtype": None,
        "content": "count [abc123] — abort",
        "data": {**RUN_START["data"], "exit_status": "abort"},
        "sync_status": "pending",
    }

    resp = await client.put(f"/logbook/fragments/{frag['id']}", json=row, headers=HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "readonly"
    assert body["subtype"] == "bluesky_plan"
    assert body["data"]["exit_status"] == "abort"


@pytest.mark.asyncio
async def test_put_without_data_keeps_existing_data(client):
    frag = await _make_fragment(client, RUN_START)

    resp = await client.put(
        f"/logbook/fragments/{frag['id']}", json={"content": "renamed"}, headers=HEADERS
    )

    assert resp.status_code == 200
    assert resp.json()["data"] == RUN_START["data"]
    assert (await _get_fragment(client, frag))["data"] == RUN_START["data"]


@pytest.mark.asyncio
async def test_put_readonly_fragment_not_owned(client):
    frag = await _make_fragment(client, RUN_START)

    resp = await client.put(
        f"/logbook/fragments/{frag['id']}", json={"content": "hijack"}, headers=OTHER
    )

    assert resp.status_code == 404
    assert (await _get_fragment(client, frag))["content"] == RUN_START["content"]


@pytest.mark.asyncio
async def test_put_updates_image_fragment_caption_and_metadata(client):
    data = {"image_id": "img-1", "filename": "a.png", "mime_type": "image/png"}
    frag = await _make_fragment(client, {"kind": "image", "content": "old", "data": data})

    resp = await client.put(
        f"/logbook/fragments/{frag['id']}",
        json={"content": "new caption", "data": {**data, "filename": "b.png"}},
        headers=HEADERS,
    )

    assert resp.status_code == 200
    assert resp.json()["content"] == "new caption"
    assert resp.json()["data"]["filename"] == "b.png"


@pytest.mark.asyncio
async def test_put_cannot_repoint_image_fragment(client):
    """Changing image_id would let delete remove an unrelated image file."""
    data = {"image_id": "img-1", "filename": "a.png", "mime_type": "image/png"}
    frag = await _make_fragment(client, {"kind": "image", "content": "", "data": data})

    resp = await client.put(
        f"/logbook/fragments/{frag['id']}",
        json={"data": {**data, "image_id": "img-2"}},
        headers=HEADERS,
    )

    assert resp.status_code == 400
    assert (await _get_fragment(client, frag))["data"] == data


@pytest.mark.asyncio
async def test_local_store_updates_and_deletes_readonly(tmp_path: Path):
    store = LocalStore(tmp_path / "local.db")
    await store.init_db()
    logbook = await store.get_or_create_logbook("test-user")
    entry = await store.create_entry(logbook.id)
    frag = await store.add_fragment(
        entry.id, kind="readonly", subtype="bluesky_plan", content="run", data={"a": 1}
    )

    updated = await store.update_fragment(frag.id, content="run — success", data={"a": 1, "b": 2})
    assert updated is not None
    assert updated.content == "run — success"
    assert updated.data == {"a": 1, "b": 2}

    kept = await store.update_fragment(frag.id, position=4)
    assert kept is not None and kept.data == {"a": 1, "b": 2}

    assert await store.delete_fragment(frag.id) is True
    assert await store.update_fragment(uuid.uuid4(), content="x") is None
