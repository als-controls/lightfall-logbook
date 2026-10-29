"""deploy/remediate_scoped_settings.py against a schema built by the server's models."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
from lightfall_settings import deployment_registry
from sqlalchemy import create_engine

from lightfall_logbook import scoped_models  # noqa: F401  (registers tables)
from lightfall_logbook.models import Base

SCRIPT = Path(__file__).parents[1] / "deploy" / "remediate_scoped_settings.py"
_spec = importlib.util.spec_from_file_location("remediate", SCRIPT)
rem = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rem)

ROWS = [
    {"id": "a", "type": "mock", "name": "mock", "enabled": True, "config": {}},
    {"id": "b", "type": "happi", "name": "happi", "enabled": False,
     "config": {"path": "C:\\Users\\admin\\lightfall\\happi\\bcs_12021_chipps.json"}},
]


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "logbook.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    conn = sqlite3.connect(path)
    with conn:
        for scope, key, value in [
            ("user:ron", "tiled_beamline", "12.0.2.1"),
            ("user:ron", "theme", "dark"),
            ("user:ron", "device_backends", ROWS),
            ("user:ron", "device_backends_exclusive_restore", ["a"]),
            ("user:ron", "notes_dir", "/home/alsuser/notes"),
            ("beamline:7.0.1", "device_backends", ROWS[:1]),
        ]:
            conn.execute("INSERT INTO scoped_settings (scope, key, value, version, updated_by,"
                         " updated_at) VALUES (?, ?, ?, 1, 'x', '2026-10-01 00:00:00')",
                         (scope, key, json.dumps(value)))
        conn.execute("INSERT INTO user_settings (user_id, beamline, key, value, updated_at) "
                     "VALUES ('ron', '', 'ipc_topic_prefix', '\"als.12021\"', "
                     "'2026-10-01 00:00:00')")
    yield path
    conn.close()


def _rows(path):
    conn = sqlite3.connect(path)
    try:
        return {(s, k): json.loads(v) for s, k, v in
                conn.execute("SELECT scope, key, value FROM scoped_settings")}
    finally:
        conn.close()


def test_script_key_lists_match_lightfall_settings():
    reg = deployment_registry()
    keys = reg.keys()
    forbidden = {k for k in keys if reg.get(k).scopes >= {"beamline"}
                 and not reg.get(k).allows("user")}
    assert forbidden == rem.USER_FORBIDDEN
    assert set(rem.UNION_ROW_KEYS) == {k for k in keys if reg.get(k).policy == "union"
                                       and reg.get(k).scopes == {"beamline", "branch", "user"}}


def test_audit_flags_polluted_rows(db):
    conn = sqlite3.connect(db)
    found = {(f["table"], f["scope"], f["key"]): f["reasons"] for f in rem.audit(conn)}
    conn.close()
    assert found[("scoped_settings", "user:ron", "tiled_beamline")] == [
        "not allowed at user scope"]
    assert any("absolute paths" in r for r in found[("scoped_settings", "user:ron",
                                                     "device_backends")])
    assert any("absolute paths" in r for r in found[("scoped_settings", "user:ron",
                                                     "notes_dir")])
    assert found[("scoped_settings", "beamline:7.0.1", "device_backends")] == [
        "list-shaped (needs --convert-device-rows)"]
    assert ("user_settings", "user:ron", "ipc_topic_prefix") in found
    assert ("scoped_settings", "user:ron", "theme") not in found


def test_dry_run_changes_nothing(db):
    before = _rows(db)
    assert rem.main([str(db), "--delete", "user:ron:tiled_beamline",
                     "--convert-device-rows"]) == 0
    assert _rows(db) == before


def test_delete_with_apply_removes_and_audits(db):
    rem.main([str(db), "--delete", "user:ron:tiled_beamline", "--delete", "user:ron:nope",
              "--apply", "--reason", "incident"])
    assert ("user:ron", "tiled_beamline") not in _rows(db)
    conn = sqlite3.connect(db)
    audit = conn.execute("SELECT scope, key, action, old, actor FROM setting_audit").fetchall()
    conn.close()
    assert audit == [("user:ron", "tiled_beamline", "delete", '"12.0.2.1"',
                      "remediation (incident)")]


def test_convert_device_rows_matches_the_union_spec(db):
    rem.main([str(db), "--convert-device-rows", "--apply"])
    rows = _rows(db)
    reg = deployment_registry()
    backends = rows[("user:ron", "device_backends")]
    assert backends == {"a": ROWS[0], "b": ROWS[1]}
    assert reg.get("device_backends").validate(backends) == backends
    restore = rows[("user:ron", "device_backends_exclusive_restore")]
    assert reg.get("device_backends_exclusive_restore").validate(restore) == {"a": True}
    assert rows[("beamline:7.0.1", "device_backends")] == {"a": ROWS[0]}


def test_bad_target_is_rejected(db):
    with pytest.raises(SystemExit):
        rem.main([str(db), "--delete", "tiled_beamline"])
