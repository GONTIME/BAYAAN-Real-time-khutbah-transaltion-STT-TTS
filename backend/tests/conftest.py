"""Shared test fixtures. Uses a temp SQLite DB - no external services needed."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bayaan_server.api.app import create_app
from bayaan_server.config import DatabaseSettings, Settings

# Admin credentials for test client fixtures (ADR-0004)
# admin:admin base64 encoded
DEFAULT_AUTH_HEADERS = {"Authorization": "Basic YWRtaW46YWRtaW4="}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database=DatabaseSettings(
            url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", echo_sql=False
        ),
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    with TestClient(create_app(settings), headers=DEFAULT_AUTH_HEADERS) as test_client:
        yield test_client


def make_client(settings: Settings, headers: dict[str, str] | None = None) -> TestClient:
    """Factory for tests that need custom configuration."""
    req_headers = DEFAULT_AUTH_HEADERS if headers is None else headers
    return TestClient(create_app(settings), headers=req_headers)