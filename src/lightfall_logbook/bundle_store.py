"""Content-addressed storage for shared-content bundles (zip bytes, named by sha256).

The server never opens a bundle; it only stores and serves the bytes. Identical bundles
are stored once.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class BundleStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, sha256: str) -> Path:
        if not isinstance(sha256, str) or not _SHA256.match(sha256):
            raise ValueError(f"not a sha256 hex digest: {sha256!r}")
        return self._root / sha256[:2] / sha256

    def put(self, data: bytes) -> str:
        """Store ``data``; return its sha256. Storing the same bytes again is a no-op."""
        sha256 = hashlib.sha256(data).hexdigest()
        path = self._path(sha256)
        if path.is_file():
            return sha256
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)  # atomic: readers see the whole file or none
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return sha256

    def get(self, sha256: str) -> bytes:
        return self._path(sha256).read_bytes()

    def exists(self, sha256: str) -> bool:
        return self._path(sha256).is_file()
