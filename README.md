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

Once minted, the secret is sent on subsequent requests as `Authorization: Apikey <secret>`. The middleware accepts either Bearer (Keycloak) or Apikey auth schemes; keys live up to 7 days by default.

## Local Storage

`lightfall_logbook.local_store.LocalStore` provides offline-first SQLite storage with sync-on-reconnect. Used by the Lightfall Qt client.
