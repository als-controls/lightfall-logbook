# lucid-logbook

Backend service for the LUCID experiment logbook.

## Overview

Litestar REST API providing per-user logbooks with an Entry→Fragment data model:

- **Logbook**: Per-user container (one per user)
- **Entry**: Per-shift note collection with tags
- **Fragment**: Ordered content within an entry — either user-editable text (markdown) or read-only system records (bluesky plans, device changes, claude responses)

## Quick Start

```bash
pip install -e ".[dev]"
uvicorn lucid_logbook.app:app --reload
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

## Local Storage

`lucid_logbook.local_store.LocalStore` provides offline-first SQLite storage with sync-on-reconnect. Used by the LUCID Qt client.
