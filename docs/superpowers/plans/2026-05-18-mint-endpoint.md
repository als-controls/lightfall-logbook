# lucid-logbook auth-v2 mint endpoint

**Coordination plan reference:** `ncs/ncs/docs/superpowers/plans/2026-05-17-lucid-auth-v2-coordination.md` Step 5.

**Spec reference:** `ncs/ncs/docs/superpowers/specs/2026-05-17-lucid-auth-v2-design.md`.

## Goal

Add Tiled-shape user-scoped API-key minting to lucid-logbook so the LUCID logbook consumer (Step 6) can stop carrying the Keycloak bearer.

## Wire contract

```
POST /api/v1/auth/apikey
  Authorization: Bearer <keycloak_token>
  Content-Type: application/json
  {"expires_in": 604800, "scopes": [], "note": "lucid session 2026-05-18"}

  -> 201 Created
  {
    "secret": "<64 hex chars>",
    "first_eight": "<8 hex chars>",
    "expiration_time": "2026-05-25T17:30:00+00:00",
    "scopes": [],
    "note": "lucid session 2026-05-18"
  }

DELETE /api/v1/auth/apikey?first_eight=<8 hex chars>
  Authorization: Bearer <keycloak_token>

  -> 204 No Content   (revoked own key)
  -> 404 Not Found    (no matching un-revoked key owned by caller)
```

Subsequent requests use `Authorization: Apikey <secret>` against any existing logbook endpoint; the caller's `sub` is recovered from the api_keys table and injected into `request.state.user_id` exactly as the Keycloak middleware does today.

## Design decisions

- **DB-backed, not JWT.** Tiled stores apikeys in a table; we copy that pattern for symmetry and because revocation needs DB state anyway. Storage is internal; the wire protocol is what's spec'd. (Spec calls out either as acceptable.)
- **SHA-256 hash storage.** Secrets are random hex (32 bytes from `secrets.token_hex`); only the hash is persisted. Hash collisions are infeasible at this width.
- **`first_eight`** is the first 8 chars of the cleartext secret (matches the LUCID-side `MintedKey.first_eight` semantics already shipped on `feature/notebook-pipelines-impl`).
- **Single combined auth middleware** replaces the current Keycloak-only middleware. Handles `Apikey ...` and `Bearer ...` schemes; falls through with no header in dev mode (so the existing `X-User-Id` fallback in `_get_user_id` keeps working).
- **No revocation cascade.** A revoked key returns 401 on subsequent requests; we keep the row (revoked=True) for audit, not delete it.
- **Mint requires Bearer.** You cannot mint an apikey using another apikey (avoids infinite chain). Revoke accepts either Bearer or Apikey scheme so existing sessions can clean up.
- **No scopes today.** The logbook doesn't have a scope model; we accept `scopes=[]` and store it. Reserved for future use.

## Files

Create:
- `src/lucid_logbook/apikeys.py` -- ApiKeyRow model, helpers (mint, lookup, revoke), Pydantic schemas, AuthController route handlers
- `tests/test_apikeys.py` -- mint round-trip, key-based auth on existing endpoint, revoke flow, expired key, wrong-owner DELETE

Modify:
- `src/lucid_logbook/models.py` -- export `ApiKeyRow` (or re-export from `apikeys`)
- `src/lucid_logbook/auth.py` -- replace `KeycloakAuthMiddleware` with `CombinedAuthMiddleware` that handles both schemes
- `src/lucid_logbook/app.py` -- register the new middleware and the new controller; the middleware now always registers (the dev-mode fallthrough is internal)
- `README.md` -- add the two new endpoints to the route table; add one sentence on Apikey auth

## Tasks

### Task 1: ApiKeyRow model + helpers

**Files:** `src/lucid_logbook/apikeys.py` (new)

Write the SQLAlchemy model and the three helpers (mint, lookup by hash, revoke by first_eight). No HTTP code yet. Helpers operate on an `AsyncSession`.

`ApiKeyRow` columns:
- `id` (PK, autoinc int)
- `secret_hash` (str, unique, indexed, not null)
- `first_eight` (str, indexed, not null)
- `sub` (str, indexed, not null)
- `expires_at` (datetime, tz-aware, not null)
- `scopes` (JSON, not null, default `[]`)
- `note` (str, not null, default `""`)
- `revoked` (bool, not null, default False)
- `created_at` (datetime, tz-aware, not null, default utcnow)

Helpers:
- `async def mint_key(session, *, sub, expires_in_seconds, scopes, note) -> tuple[str, ApiKeyRow]` returns `(cleartext_secret, row)`.
- `async def lookup_user_by_secret(session, secret) -> str | None` returns `sub` if the secret hash matches a non-revoked, non-expired row; else None.
- `async def revoke_key(session, *, sub, first_eight) -> bool` returns True if revoked, False if not found / not owned.

Tests in this task verify those helpers directly (no HTTP).

### Task 2: CombinedAuthMiddleware

**Files:** `src/lucid_logbook/auth.py` (modify)

Replace `KeycloakAuthMiddleware` with `CombinedAuthMiddleware`. Constructor takes the SQLAlchemy `async_sessionmaker`. ASGI behavior:

```
if scope.type != "http": pass through
if path is in self.exclude: pass through
read Authorization header
if it starts with "Apikey ":
    look up; set state.user_id, state.auth_mode="apikey"; 401 on miss
elif it starts with "Bearer ":
    if keycloak not configured: 401
    decode/validate; set state.user_id, state.user_claims, state.auth_mode="bearer"; 401 on bad token
elif header is empty:
    if keycloak configured: 401 (prod requires auth)
    else: pass through (dev mode; _get_user_id falls back to X-User-Id)
else:
    401 Unsupported scheme
```

`exclude` keeps `/health`. Keep `decode_token` / `_get_jwks_client` / `keycloak_auth_enabled` as-is for the Bearer path.

### Task 3: Mint + Revoke endpoints

**Files:** `src/lucid_logbook/apikeys.py` (extend with `AuthController`)

Litestar `Controller` at path `/api/v1/auth`. Endpoints:

```
POST /apikey  -> calls mint_key(); rejects with 403 if state.auth_mode != "bearer"
DELETE /apikey  -> calls revoke_key(); accepts either auth mode
```

Request/response Pydantic models:
- `ApiKeyMintRequest(expires_in: int = 604800, scopes: list[str] = [], note: str = "")`
- `ApiKeyMintResponse(secret: str, first_eight: str, expiration_time: str, scopes: list[str], note: str)`

`expires_in` must be in `(0, 7 * 86400 + 60)` (clamp at one week; the +60 lets the LUCID client request "604800" without floor-rounding rejection). Reject out-of-range with 422.

Wire `AuthController` into `app.py`'s `route_handlers`.

### Task 4: End-to-end tests

**Files:** `tests/test_apikeys.py` (new)

Use the same pattern as `tests/test_settings_api.py` (`AsyncTestClient`, monkeypatched DATABASE_URL).

Cover:
- Mint round-trip via `X-User-Id` header (dev mode), confirm response shape (secret length 64, first_eight matches first 8 chars, expiration_time is ISO, etc.)
- Mint then immediately use the returned secret on `GET /logbook` via `Authorization: Apikey <secret>` -- expect same logbook as the minting user
- Mint then revoke (DELETE with first_eight); subsequent `Apikey` request returns 401
- DELETE by another user fails with 404 (cross-user revoke blocked)
- Expired key (mint with `expires_in=1`, sleep 2s OR monkeypatch `datetime.utcnow`) returns 401 on use
- Invalid `Apikey` secret returns 401
- Bearer-only restriction on mint: in dev mode the test simulates absence of Bearer by sending only `X-User-Id` and confirms either success (dev fallback) or 403 (if we tighten that path)

Decision in task 3 above: in dev mode, mint succeeds via `X-User-Id` fallback (no Bearer requirement). The "mint requires Bearer" rule applies only when Keycloak is enabled. Test in dev mode passes either way; document this explicitly.

### Task 5: README update

Add two rows to the route table in `README.md`:

```
| POST   | /api/v1/auth/apikey  | Mint a user-scoped API key (requires Bearer in prod) |
| DELETE | /api/v1/auth/apikey  | Revoke a user-scoped API key by first_eight          |
```

One paragraph after the table on Apikey auth: "Once minted, the secret is sent on subsequent requests as `Authorization: Apikey <secret>`. The middleware accepts either Bearer (Keycloak) or Apikey auth schemes; keys live up to 7 days by default."

## Test plan

After implementation:

```
.venv/Scripts/python -m pytest tests/ -v
```

Expect: all existing tests still pass, new test file fully green.

## Deployment notes

- The new `api_keys` table is created by `Base.metadata.create_all` on app startup (no Alembic migrations in this repo today; SQLite + create_all is the pattern). Production sqlite db at `bcglucidlogbook.dhcp.lbl.gov:/opt/lucid-logbook/logbook.db` will gain the table automatically on next startup.
- No new env vars; `KEYCLOAK_URL` / `KEYCLOAK_REALM` are still the bearer-auth toggle.

## Out of scope

- Scopes enforcement on logbook routes (no scope model today; reserved).
- Rate limiting on the mint endpoint (out of scope for v1; Litestar middleware can add it later).
- Per-key audit log surfacing (rows have `revoked` and `created_at`; UI later).
