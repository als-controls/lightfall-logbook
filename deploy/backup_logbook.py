#!/usr/bin/env python3
"""Back up the logbook: a database snapshot, the content bundles and a few config files.

Layout under ``--dest``::

    db/20261101T031500Z/logbook.db   online SQLite backup, integrity-checked (+ --extra files)
    bundles/ab/ab12…                 mirror of CONTENT_STORAGE_DIR (copied, never pruned)

The database is copied first and the bundles after it: bundles are only ever added, so
the mirror taken afterwards holds every bundle the snapshot refers to. Snapshots beyond
``--keep`` are removed, newest kept. Stdlib only; run it daily (deploy/lucid-logbook-backup.*).

Restore: stop the service, copy a snapshot's logbook.db over /opt/lucid-logbook/logbook.db
and the bundles/ mirror into CONTENT_STORAGE_DIR, start the service.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_STAMP = re.compile(r"^\d{8}T\d{6}Z$")
_PREFIX = re.compile(r"^[0-9a-f]{2}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_PARTIAL = ".partial-"


def backup_db(src: Path, dest: Path) -> None:
    """Copy ``src`` to ``dest`` with SQLite's online backup, then integrity-check the copy."""
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
            result = target.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            target.close()
    finally:
        source.close()
    if result != "ok":
        raise sqlite3.DatabaseError(f"integrity check of the copy failed: {result}")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def mirror_bundles(src: Path, dest: Path) -> int:
    """Copy bundles missing (or damaged) in ``dest``; returns how many were copied.

    Only ``ab/<sha256>`` files are bundles (temp files and anything else are skipped);
    a source file whose content doesn't match its name is reported and not copied.
    Nothing is ever deleted from the mirror.
    """
    if not src.is_dir():
        return 0
    copied = 0
    for prefix in sorted(src.iterdir()):
        if not prefix.is_dir() or not _PREFIX.match(prefix.name):
            continue
        for path in sorted(prefix.iterdir()):
            name = path.name
            if not path.is_file() or not _SHA.match(name) or not name.startswith(prefix.name):
                continue
            target = dest / prefix.name / name
            if target.is_file() and target.stat().st_size == path.stat().st_size \
                    and _sha256(target) == name:
                continue
            if _sha256(path) != name:
                print(f"warning: {path}: sha256 mismatch, not copied", file=sys.stderr)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
            os.close(fd)
            try:
                shutil.copyfile(path, tmp)
                os.replace(tmp, target)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            copied += 1
    return copied


def prune(snapshots: Path, keep: int) -> list[Path]:
    """Remove snapshots beyond the newest ``keep``, and leftovers of interrupted runs."""
    removed = []
    for path in snapshots.iterdir():
        if path.is_dir() and path.name.startswith(_PARTIAL):
            shutil.rmtree(path)
            removed.append(path)
    stamped = sorted(p for p in snapshots.iterdir() if p.is_dir() and _STAMP.match(p.name))
    for path in stamped[:max(len(stamped) - keep, 0)]:
        shutil.rmtree(path)
        removed.append(path)
    return removed


def run(*, db: Path, bundles: Path, dest: Path, keep: int, extras: list[Path],
        now: datetime | None = None) -> Path:
    """One backup: snapshot, then mirror, then prune. Returns the snapshot folder."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")  # noqa: UP017 (3.9)
    snapshots = dest / "db"
    snapshots.mkdir(parents=True, exist_ok=True)
    final = snapshots / stamp
    partial = snapshots / f"{_PARTIAL}{stamp}"
    partial.mkdir(mode=0o700)
    try:
        backup_db(db, partial / db.name)
        for extra in extras:
            if extra.is_file():
                shutil.copy2(extra, partial / extra.name)
        os.chmod(partial, 0o700)
        os.replace(partial, final)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    mirror_bundles(bundles, dest / "bundles")
    prune(snapshots, keep)
    return final


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--bundles", type=Path, required=True, help="CONTENT_STORAGE_DIR")
    p.add_argument("--dest", type=Path, required=True)
    p.add_argument("--keep", type=int, default=14, help="snapshots to keep (default 14)")
    p.add_argument("--extra", type=Path, action="append", default=[],
                   help="also copy this file into each snapshot (repeatable)")
    args = p.parse_args(argv)
    os.umask(0o077)
    snap = run(db=args.db, bundles=args.bundles, dest=args.dest, keep=args.keep,
               extras=args.extra)
    size = (snap / args.db.name).stat().st_size
    print(f"backed up {args.db.name} ({size} bytes) to {snap}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
