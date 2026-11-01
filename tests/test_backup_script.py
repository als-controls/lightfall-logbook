"""deploy/backup_logbook.py: database snapshots, the bundle mirror and retention."""
import hashlib
import importlib.util
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "deploy" / "backup_logbook.py"
_spec = importlib.util.spec_from_file_location("backup_logbook", SCRIPT)
bk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bk)

T0 = datetime(2026, 11, 1, 3, 15, tzinfo=UTC)


def _db(path: Path, rows: int = 3) -> Path:
    con = sqlite3.connect(path)
    con.execute("create table t (id integer primary key, v text)")
    con.executemany("insert into t (v) values (?)", [(f"r{i}",) for i in range(rows)])
    con.commit()
    con.close()
    return path


def _bundle(root: Path, data: bytes) -> str:
    sha = hashlib.sha256(data).hexdigest()
    (root / sha[:2]).mkdir(parents=True, exist_ok=True)
    (root / sha[:2] / sha).write_bytes(data)
    return sha


def _count(db: Path) -> int:
    con = sqlite3.connect(db)
    try:
        return con.execute("select count(*) from t").fetchone()[0]
    finally:
        con.close()


def test_backup_db_copies_a_consistent_database(tmp_path):
    src = _db(tmp_path / "logbook.db", rows=5)
    held = sqlite3.connect(src)  # the server keeps connections open: online backup
    held.execute("insert into t (v) values ('uncommitted')")
    bk.backup_db(src, tmp_path / "copy.db")
    held.rollback()
    held.close()
    assert _count(tmp_path / "copy.db") == 5


def test_mirror_copies_new_bundles_only_and_never_deletes(tmp_path):
    src, dest = tmp_path / "bundles", tmp_path / "mirror"
    a = _bundle(src, b"a" * 10)
    assert bk.mirror_bundles(src, dest) == 1
    b = _bundle(src, b"b" * 10)
    (src / a[:2] / "junk.tmp").write_bytes(b"half-written")  # BundleStore temp file
    (src / "README").write_bytes(b"not a bundle")
    assert bk.mirror_bundles(src, dest) == 1
    assert (dest / b[:2] / b).read_bytes() == b"b" * 10
    assert not (dest / a[:2] / "junk.tmp").exists() and not (dest / "README").exists()
    (src / a[:2] / a).unlink()
    assert bk.mirror_bundles(src, dest) == 0
    assert (dest / a[:2] / a).exists()


def test_mirror_replaces_a_damaged_copy(tmp_path):
    src, dest = tmp_path / "bundles", tmp_path / "mirror"
    a = _bundle(src, b"good bytes")
    bk.mirror_bundles(src, dest)
    (dest / a[:2] / a).write_bytes(b"bad")
    assert bk.mirror_bundles(src, dest) == 1
    assert (dest / a[:2] / a).read_bytes() == b"good bytes"


def test_mirror_skips_a_source_file_whose_hash_is_wrong(tmp_path, capsys):
    src, dest = tmp_path / "bundles", tmp_path / "mirror"
    sha = hashlib.sha256(b"x").hexdigest()
    (src / sha[:2]).mkdir(parents=True)
    (src / sha[:2] / sha).write_bytes(b"corrupted on disk")
    assert bk.mirror_bundles(src, dest) == 0
    assert not (dest / sha[:2] / sha).exists()
    assert "sha256 mismatch" in capsys.readouterr().err


def test_run_writes_a_snapshot_with_extras_then_mirrors(tmp_path):
    db = _db(tmp_path / "logbook.db")
    bundles = tmp_path / "bundles"
    sha = _bundle(bundles, b"bundle")
    env = tmp_path / ".env"
    env.write_text("KEYCLOAK_URL=x\n")
    dest = tmp_path / "backups"
    snap = bk.run(db=db, bundles=bundles, dest=dest, keep=14, extras=[env], now=T0)
    assert snap == dest / "db" / "20261101T031500Z"
    assert _count(snap / "logbook.db") == 3 and (snap / ".env").read_text() == "KEYCLOAK_URL=x\n"
    assert (dest / "bundles" / sha[:2] / sha).exists()
    if os.name != "nt":
        assert (snap.stat().st_mode & 0o777) == 0o700


def test_run_without_a_bundle_dir_still_backs_up_the_db(tmp_path):
    db = _db(tmp_path / "logbook.db")
    snap = bk.run(db=db, bundles=tmp_path / "missing", dest=tmp_path / "b", keep=3,
                  extras=[tmp_path / "not-there.toml"], now=T0)
    assert (snap / "logbook.db").exists() and not (snap / "not-there.toml").exists()


def test_retention_keeps_the_newest_and_clears_partials(tmp_path):
    db = _db(tmp_path / "logbook.db")
    dest = tmp_path / "b"
    for day in range(5):
        bk.run(db=db, bundles=tmp_path / "none", dest=dest, keep=3, extras=[],
               now=T0 + timedelta(days=day))
    (dest / "db" / ".partial-20261101T000000Z").mkdir()  # a run killed half-way
    (dest / "db" / "notes").mkdir()  # not ours: left alone
    bk.run(db=db, bundles=tmp_path / "none", dest=dest, keep=3, extras=[],
           now=T0 + timedelta(days=5))
    names = sorted(p.name for p in (dest / "db").iterdir())
    assert names == ["20261104T031500Z", "20261105T031500Z", "20261106T031500Z", "notes"]


def test_a_corrupt_database_fails_and_leaves_no_snapshot(tmp_path):
    bad = tmp_path / "logbook.db"
    bad.write_bytes(b"this is not sqlite" * 100)
    with pytest.raises(sqlite3.DatabaseError):
        bk.run(db=bad, bundles=tmp_path / "none", dest=tmp_path / "b", keep=3, extras=[],
               now=T0)
    assert [p.name for p in (tmp_path / "b" / "db").iterdir()] == []


def test_main_parses_arguments(tmp_path, capsys):
    db = _db(tmp_path / "logbook.db")
    assert bk.main(["--db", str(db), "--bundles", str(tmp_path / "x"),
                    "--dest", str(tmp_path / "b"), "--keep", "2"]) == 0
    assert "logbook.db" in capsys.readouterr().out
