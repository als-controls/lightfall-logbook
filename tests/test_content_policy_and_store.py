"""Content policy config ([content] in SCOPES_CONFIG) and the content-addressed bundle store."""
import hashlib

import pytest

from lightfall_logbook.bundle_store import BundleStore
from lightfall_logbook.scoped_config import ContentPolicy, parse_scoped_config


def test_content_policy_defaults_and_toml():
    assert parse_scoped_config("").content == ContentPolicy()
    cfg = parse_scoped_config("[content]\nmax_files = 10\nmax_bundle_bytes = 1024\n")
    assert (cfg.content.max_files, cfg.content.max_bundle_bytes) == (10, 1024)
    assert cfg.content.hard_max_bundle_bytes == ContentPolicy().hard_max_bundle_bytes


@pytest.mark.parametrize("bad", [
    "max_files = 0",
    "max_bundle_bytes = -1",
    "max_bundle_bytes = 600000000\nhard_max_bundle_bytes = 1000",
    'max_files = "x"',
    "max_files = true",
    "unknown_limit = 5",
])
def test_bad_content_policy_is_refused(bad):
    with pytest.raises(ValueError):
        parse_scoped_config(f"[content]\n{bad}\n")


def test_bundle_store_round_trip_dedup_and_atomic(tmp_path):
    store = BundleStore(tmp_path)
    sha = store.put(b"zipbytes")
    assert sha == hashlib.sha256(b"zipbytes").hexdigest()
    assert store.put(b"zipbytes") == sha
    assert store.get(sha) == b"zipbytes"
    assert store.exists(sha)
    assert (tmp_path / sha[:2] / sha).is_file()
    assert not list(tmp_path.rglob("*.tmp"))


def test_bundle_store_missing_is_file_not_found(tmp_path):
    store = BundleStore(tmp_path)
    missing = "0" * 64
    assert not store.exists(missing)
    with pytest.raises(FileNotFoundError):
        store.get(missing)


@pytest.mark.parametrize("bad", ["../etc/passwd", "ABC", "0" * 63, "A" * 64])
def test_bundle_store_rejects_non_hash_names(tmp_path, bad):
    store = BundleStore(tmp_path)
    with pytest.raises(ValueError):
        store.get(bad)
    with pytest.raises(ValueError):
        store.exists(bad)
