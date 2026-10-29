#!/usr/bin/env python3
"""Audit and repair scoped-settings rows polluted by user-scope writes (2026-10 incident).

Standalone (stdlib only) so it runs against the live SQLite DB with whatever version of
the server is installed. Dry run unless ``--apply``; stop the service and back up the DB
before applying.

    python3 remediate_scoped_settings.py logbook.db                     # audit report
    python3 remediate_scoped_settings.py logbook.db --delete user:<sub>:tiled_beamline \\
        --delete user:<sub>:ipc_topic_prefix --apply --reason "7.0.1.1 incident"
    python3 remediate_scoped_settings.py logbook.db --convert-device-rows --apply

Every change writes a ``setting_audit`` row (actor ``remediation``). ``--convert-device-rows``
rewrites list-shaped ``device_backends`` as ``{row id: row}``, the union shape
lightfall-settings 0.1.4 requires; run it only together with a client that reads that shape.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import UTC, datetime
from typing import Any

# Keys a beamline console's identity or hardware depends on: lightfall's
# scopes/keys.py _BEAMLINE_CAPABLE plus lightfall-settings DEPLOYMENT_SPECS.
BEAMLINE_CAPABLE = frozenset({
    "default_data_dir", "panel_layout", "plot_defaults", "acquisition_defaults",
    "device_favorites", "tiled_beamline", "current_beamline", "ipc_topic_prefix",
    "device_backend", "device_mock_enabled", "device_bcs_enabled", "device_bcs_host",
    "device_bcs_port", "device_bcs_beamline", "device_bcs_timeout_ms",
    "device_happi_enabled", "device_happi_beamline", "device_happi_instantiate",
    "device_backends", "device_backends_exclusive_restore",
})
# Not allowed at user scope at all under lightfall-settings 0.1.4.
USER_FORBIDDEN = frozenset({"tiled_beamline", "current_beamline", "ipc_topic_prefix"})
UNION_ROW_KEYS = ("device_backends", "device_backends_exclusive_restore")

_ABS_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/(?:home|opt|usr|Users|mnt|data|srv|tmp|var)/)")
ACTOR = "remediation"


def _load(raw: Any) -> Any:
    return json.loads(raw) if isinstance(raw, (str, bytes)) else raw


def absolute_paths(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if _ABS_PATH.match(value) else []
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        return [p for item in value for p in absolute_paths(item)]
    return []


def audit(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Findings: one dict per suspicious row, ``reasons`` explaining why."""
    found = []
    for scope, key, raw in conn.execute(
            "SELECT scope, key, value FROM scoped_settings ORDER BY scope, key"):
        value = _load(raw)
        reasons = []
        if scope.startswith("user:"):
            if key in USER_FORBIDDEN:
                reasons.append("not allowed at user scope")
            elif key in BEAMLINE_CAPABLE:
                reasons.append("beamline-capable key at user scope")
            paths = absolute_paths(value)
            if paths:
                reasons.append("absolute paths: " + ", ".join(paths))
        if key in UNION_ROW_KEYS and isinstance(value, list):
            reasons.append("list-shaped (needs --convert-device-rows)")
        if reasons:
            found.append({"table": "scoped_settings", "scope": scope, "key": key,
                          "reasons": reasons})
    # Legacy rows the server copies into user scope at startup when the key is absent.
    for user_id, key in conn.execute(
            "SELECT user_id, key FROM user_settings WHERE beamline = '' ORDER BY user_id, key"):
        if key in BEAMLINE_CAPABLE:
            found.append({"table": "user_settings", "scope": f"user:{user_id}", "key": key,
                          "reasons": ["legacy row; re-copied at startup if the scoped row "
                                      "is deleted (unless the server spec forbids it)"]})
    return found


def _now() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat(" ")


def _audit_row(conn, scope: str, key: str, action: str, old: Any, new: Any,
               reason: str) -> None:
    conn.execute(
        "INSERT INTO setting_audit (scope, key, action, old, new, actor, agent, at) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
        (scope, key, action, json.dumps(old), json.dumps(new),
         f"{ACTOR} ({reason})" if reason else ACTOR, _now()),
    )


def delete(conn: sqlite3.Connection, scope: str, key: str, reason: str = "") -> bool:
    row = conn.execute("SELECT value FROM scoped_settings WHERE scope = ? AND key = ?",
                       (scope, key)).fetchone()
    if row is None:
        return False
    conn.execute("DELETE FROM scoped_settings WHERE scope = ? AND key = ?", (scope, key))
    _audit_row(conn, scope, key, "delete", _load(row[0]), None, reason)
    return True


def to_union(key: str, value: list[Any]) -> dict[str, Any]:
    if key == "device_backends":
        out = {}
        for item in value:
            if isinstance(item, dict) and item.get("id"):
                out[str(item["id"])] = item
        return out
    return {str(rid): True for rid in value}


def convert_device_rows(conn: sqlite3.Connection, reason: str = "") -> list[tuple[str, str]]:
    done = []
    marks = ", ".join("?" for _ in UNION_ROW_KEYS)
    for scope, key, raw, version in conn.execute(
            f"SELECT scope, key, value, version FROM scoped_settings WHERE key IN ({marks})",
            UNION_ROW_KEYS).fetchall():
        old = _load(raw)
        if not isinstance(old, list):
            continue
        new = to_union(key, old)
        conn.execute(
            "UPDATE scoped_settings SET value = ?, version = ?, updated_by = ?, updated_at = ? "
            "WHERE scope = ? AND key = ?",
            (json.dumps(new), version + 1, ACTOR, _now(), scope, key))
        _audit_row(conn, scope, key, "set", old, new, reason)
        done.append((scope, key))
    return done


def _split_target(text: str) -> tuple[str, str]:
    kind, _, rest = text.partition(":")
    name, sep, key = rest.partition(":")
    if kind not in ("facility", "beamline", "branch", "user") or not sep:
        raise argparse.ArgumentTypeError(f"expected <kind>:<name>:<key>, got {text!r}")
    return f"{kind}:{name}", key


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("db", help="path to logbook.db")
    ap.add_argument("--delete", action="append", default=[], type=_split_target,
                    metavar="KIND:NAME:KEY", help="delete one scoped value (repeatable)")
    ap.add_argument("--convert-device-rows", action="store_true",
                    help="rewrite list-shaped device rows in the union shape")
    ap.add_argument("--reason", default="", help="recorded in the audit actor")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args(argv)

    uri = f"file:{args.db}" + ("" if args.apply else "?mode=ro")
    conn = sqlite3.connect(uri, uri=True)
    try:
        print("== audit")
        for f in audit(conn):
            print(f"{f['table']:15} {f['scope']} {f['key']}: {'; '.join(f['reasons'])}")
        if not (args.delete or args.convert_device_rows):
            return 0
        if not args.apply:
            print("== dry run; would", *(f"delete {s} {k}" for s, k in args.delete),
                  "and convert device rows" if args.convert_device_rows else "", sep="\n  ")
            return 0
        with conn:  # one transaction
            for scope, key in args.delete:
                print(("deleted" if delete(conn, scope, key, args.reason) else "absent "),
                      scope, key)
            if args.convert_device_rows:
                for scope, key in convert_device_rows(conn, args.reason):
                    print("converted", scope, key)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
