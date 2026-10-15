"""Test-wide defaults."""
import pytest


@pytest.fixture(autouse=True)
def _dev_mode(monkeypatch):
    """Most tests use dev-mode identity headers; opt in explicitly (Task 9)."""
    monkeypatch.setenv("LOGBOOK_DEV_MODE", "1")
