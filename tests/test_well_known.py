"""GET /.well-known/lightfall: the public facility bootstrap, served without auth."""
import pytest

from lightfall_logbook.scoped_config import parse_scoped_config
from tests.scoped_helpers import CONFIG, MAINT, STAFF, make_client

WELL_KNOWN = "/.well-known/lightfall"
SECRET_SEED = """
[seed."auth.client_secret"]
type = "str"
scopes = ["facility"]
"""


@pytest.fixture
async def env(tmp_path, monkeypatch):
    async with make_client(tmp_path, monkeypatch) as (tc, _):
        yield tc


async def _set(tc, key, value, scope="facility", headers=STAFF):
    r = await tc.put(f"/logbook/scopes/{scope}/settings/{key}", json={"value": value},
                     headers=headers)
    assert r.status_code == 200, r.text


async def test_needs_no_auth_when_the_rest_does(env, monkeypatch):
    monkeypatch.delenv("LOGBOOK_DEV_MODE", raising=False)
    assert (await env.get(WELL_KNOWN)).status_code == 200
    assert (await env.get("/logbook/scopes/chain")).status_code == 401


async def test_fresh_server_returns_schema_and_facility(env):
    assert (await env.get(WELL_KNOWN)).json() == {
        "schema": 1, "facility": {"id": "als", "name": "ALS"},
    }


async def test_returns_only_public_facility_values(env):
    await _set(env, "auth.provider", "keycloak")
    await _set(env, "auth.server_url", "https://kc.example")
    await _set(env, "tiled_url", "https://tiled.example")
    await _set(env, "facility.proxy_domains", ["*.lbl.gov"])
    await _set(env, "agent.backend", "agy")
    await _set(env, "tiled.url", "https://not-public")  # seeded, not public
    await _set(env, "tiled_url", "https://beamline", scope="beamline:7.0.1", headers=MAINT)
    assert (await env.get(WELL_KNOWN)).json() == {
        "schema": 1,
        "facility": {"id": "als", "name": "ALS"},
        "auth": {"provider": "keycloak", "server_url": "https://kc.example"},
        "services": {"tiled_url": "https://tiled.example"},
        "network": {"proxy_domains": ["*.lbl.gov"]},
        "agent": {"backend": "agy"},
    }


async def test_never_includes_secret_like_keys(tmp_path, monkeypatch):
    async with make_client(tmp_path, monkeypatch, config=CONFIG + SECRET_SEED) as (tc, _):
        await _set(tc, "auth.client_secret", "s3cr3t")
        await _set(tc, "auth.realm", "als")
        r = await tc.get(WELL_KNOWN)
    assert "s3cr3t" not in r.text and "client_secret" not in r.text
    assert r.json()["auth"] == {"realm": "als"}


def test_a_secret_seed_cannot_be_marked_public():
    with pytest.raises(ValueError, match="secret"):
        parse_scoped_config(CONFIG + SECRET_SEED + "public = true\n")


def test_facility_id_slugifies_the_label():
    from lightfall_logbook.well_known import facility_id
    assert facility_id("ALS") == "als"
    assert facility_id("Diamond Light Source") == "diamond-light-source"
    assert facility_id("  ") == "facility"


@pytest.mark.parametrize("path", ["/health", WELL_KNOWN])
def test_auth_exclusions_cover_only_the_public_paths(path):
    from litestar.middleware._utils import build_exclude_path_pattern

    from lightfall_logbook.auth import CombinedAuthMiddleware

    pattern = build_exclude_path_pattern(exclude=CombinedAuthMiddleware.exclude)
    assert pattern.findall(path)
    # Litestar uses findall (search semantics): an unanchored pattern would let any
    # path that merely contains it skip authentication.
    for other in (f"/logbook/settings{path}", f"{path}/x", f"/logbook/images{path}"):
        assert not pattern.findall(other), other
