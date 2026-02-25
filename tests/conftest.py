"""Shared test fixtures for CodeClaw tests."""

from __future__ import annotations

import pytest

from codeclaw import db


@pytest.fixture(autouse=True)
def _fresh_db():
    """Provide a fresh in-memory database for every test."""
    db.init_test_database()
    yield
    if db._db:
        db._db.close()
        db._db = None
