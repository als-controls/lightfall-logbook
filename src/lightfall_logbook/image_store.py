"""Server-side image file storage."""
from __future__ import annotations

import uuid
from pathlib import Path

ALLOWED_MIME_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
}

MAX_IMAGE_SIZE = 20 * 1024 * 1024  # 20 MB


class ImageStoreError(Exception):
    pass


class ImageStore:
    """Stores and retrieves image files on disk."""

    def __init__(self, storage_dir: Path) -> None:
        self._dir = storage_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def save(self, data: bytes, mime_type: str, image_id: str | None = None) -> str:
        """Save image bytes, return image_id.

        ``image_id`` lets an offline-first client keep the id it already put in
        its fragments; it must be a UUID (it becomes the file name). Omitted,
        a new one is generated.
        """
        ext = ALLOWED_MIME_TYPES.get(mime_type)
        if ext is None:
            raise ImageStoreError(
                f"Unsupported mime type: {mime_type}. "
                f"Allowed: {', '.join(ALLOWED_MIME_TYPES)}"
            )
        if len(data) > MAX_IMAGE_SIZE:
            raise ImageStoreError(
                f"Image too large: {len(data)} bytes (max {MAX_IMAGE_SIZE})"
            )
        if len(data) < 8:
            raise ImageStoreError("Image data too small to be valid")

        if image_id is None:
            image_id = str(uuid.uuid4())
        else:
            try:
                image_id = str(uuid.UUID(image_id))
            except ValueError:
                raise ImageStoreError(f"image_id must be a UUID: {image_id!r}") from None
        path = self._dir / f"{image_id}{ext}"
        path.write_bytes(data)
        return image_id

    def load(self, image_id: str) -> tuple[bytes, str]:
        """Load image bytes and mime type by image_id."""
        for mime_type, ext in ALLOWED_MIME_TYPES.items():
            path = self._dir / f"{image_id}{ext}"
            if path.exists():
                return path.read_bytes(), mime_type
        raise ImageStoreError(f"Image not found: {image_id}")

    def delete(self, image_id: str) -> bool:
        """Delete image file. Returns True if deleted, False if not found."""
        for ext in ALLOWED_MIME_TYPES.values():
            path = self._dir / f"{image_id}{ext}"
            if path.exists():
                path.unlink()
                return True
        return False

    def exists(self, image_id: str) -> bool:
        """Check if an image exists."""
        return any(
            (self._dir / f"{image_id}{ext}").exists()
            for ext in ALLOWED_MIME_TYPES.values()
        )
