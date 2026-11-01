# lightfall-logbook

Backend service for the Lightfall experiment logbook.

## Overview

Litestar REST API providing per-user logbooks with an Entry→Fragment data model:

- **Logbook**: Per-user container (one per user)
- **Entry**: Per-shift note collection with tags
- **Fragment**: Ordered content within an entry — either user-editable text (markdown) or read-only system records (bluesky plans, device changes, claude responses)

## Quick Start

```bash
pip install -e ".[dev]"
uvicorn lightfall_logbook.app:app --reload
```

## API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/logbook` | Get or create current user's logbook |
| POST | `/logbook/entries` | Create new entry |
| GET | `/logbook/entries` | List entries (sort by `created_at` or `updated_at`) |
| GET | `/logbook/entries/{id}` | Get entry with fragments |
| PUT | `/logbook/entries/{id}` | Update entry (title, tags) |
| POST | `/logbook/entries/{id}/fragments` | Add fragment |
| PUT | `/logbook/fragments/{id}` | Update text fragment |
| DELETE | `/logbook/fragments/{id}` | Delete text fragment |
| POST | `/api/v1/auth/apikey` | Mint a user-scoped API key (requires Bearer in prod) |
| DELETE | `/api/v1/auth/apikey` | Revoke a user-scoped API key by `first_eight` |
| GET | `/.well-known/lightfall` | Facility bootstrap for new clients: public facility settings (no auth) |

Once minted, the secret is sent on subsequent requests as `Authorization: Apikey <secret>`. The middleware accepts either Bearer (Keycloak) or Apikey auth schemes; keys live up to 7 days by default.

## Scoped settings

Settings stored per scope (`facility`, `beamline:<bl>`, `branch:<br>`, `user:<sub>`) with
role checks from Keycloak group paths, locks, an audit trail and approval of new keys.
Rules live in [lightfall-settings](https://github.com/als-controls/lightfall-settings).

| Method | Path | Description |
|--------|------|-------------|
| GET | `/logbook/scopes/chain?bind=<scope>` | Scope chain for a binding, caller's roles, entitled scopes |
| GET | `/logbook/scopes/{scope}/settings` | Snapshot of one scope (`ETag` / `If-None-Match`) |
| PUT / DELETE | `/logbook/scopes/{scope}/settings/{key}` | Set (`If-Match: <version>` on update) / reset to inherited |
| PUT / DELETE | `/logbook/scopes/{scope}/locks/{key}` | Lock / unlock (scope admin) |
| GET | `/logbook/scopes/{scope}/audit` | Change history (maintainer+; owner for user scopes) |
| GET | `/logbook/setting-keys?status=` | Key proposals and approvals (maintainer+) |
| POST | `/logbook/setting-keys/{key}/propose` | Propose a new key's spec |
| POST | `/logbook/setting-keys/{key}/approve` / `reject` | Facility admin review |
| DELETE | `/logbook/setting-keys/{key}` | Revoke an approval (facility admin) |

Writes publish `{scope, key, version}` on NATS subject `lightfall.settings.changed`; key reviews
publish `{key, status}` on `lightfall.settings.keys`. Configure the scope tree with
`SCOPES_CONFIG` (see `deploy/scopes.example.toml`). Design:
`lightfall/docs/superpowers/specs/2026-10-15-scoped-settings-design.md`.

Keys whose spec is `public` (lightfall-settings 0.1.3: sign-on, service URLs, proxy domains,
assistant) are served from facility scope at `GET /.well-known/lightfall` without auth, so the
Lightfall setup wizard can configure a fresh install before login. Secret-like keys can never be
public.

## Shared content

Plans, plugins, agents and skills published to a facility, beamline or branch scope, so everyone
who logs in there gets them. Each item `(scope, type, name)` points at its current immutable
revision; a revision is a zip the client made, stored by sha256. The server never opens a bundle:
it checks the bytes' sha256 and compressed size and the uploader's declared size and file count.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/logbook/scopes/{scope}/content` | Current items (`ETag` / `If-None-Match`); any signed-in user |
| GET | `/logbook/scopes/{scope}/content/{type}/{name}/bundle?revision=` | Zip bytes (`X-Content-SHA256`) |
| GET | `/logbook/scopes/{scope}/content/{type}/{name}/history` | Revisions, newest first |
| POST | `/logbook/scopes/{scope}/content/{type}/{name}` | Publish (multipart `manifest` JSON + `bundle`); maintainer+ |
| POST | `.../{type}/{name}/retract` / `restore` (`{"revision": n}`) | Take down / bring back; maintainer+ |

Listing and history entries carry the publisher's declared `file_count` and
`uncompressed_size` (null if not declared); clients enforce them when unpacking.

`type` is `plan`, `plugin`, `agent` or `skill`; user scopes are refused. Limits come from the
optional `[content]` table in `SCOPES_CONFIG` (see `deploy/scopes.example.toml`); admins of the
target scope are exempt from all but `hard_max_bundle_bytes`. Changes publish
`{scope, type, name, revision}` on NATS subject `lightfall.content.changed` and are audited
(`key = content:<type>/<name>`). Nothing is deleted: retract only clears the current pointer.

Bundles live in `CONTENT_STORAGE_DIR` (default `./content_bundles`). **Back it up together with
`logbook.db`**: the database records revisions, the directory holds their bytes. Design:
`lightfall/docs/superpowers/specs/2026-10-30-scoped-content-design.md`.

## Backups

`deploy/backup_logbook.py` (stdlib only) takes an online, integrity-checked snapshot of
`logbook.db` (plus `.env` and `scopes.toml`), then mirrors `CONTENT_STORAGE_DIR`, and keeps the
newest 14 snapshots. The database goes first: bundles are never deleted, so the later mirror
holds every bundle a snapshot refers to. Install the daily timer:

```bash
sudo install -d -o lucid-logbook -g lucid-logbook -m 700 /var/backups/lucid-logbook
sudo cp deploy/lucid-logbook-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now lucid-logbook-backup.timer
sudo systemctl start lucid-logbook-backup.service   # one run now; see journalctl -u
```

Restore: stop `lucid-logbook`, copy a snapshot's `logbook.db` over the live one and
`bundles/` into `CONTENT_STORAGE_DIR`, start it. The snapshots sit on the same disk, so they
guard against a bad deploy or corruption, not disk loss (the VM backup covers that).

## Local Storage

`lightfall_logbook.local_store.LocalStore` provides offline-first SQLite storage with sync-on-reconnect. Used by the Lightfall Qt client.
