import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="mailhub-mcp-test-"))
os.environ.update(
    PUBLIC_BASE_URL="https://example.test",
    GOOGLE_CLIENT_ID="cid",
    GOOGLE_CLIENT_SECRET="secret",
    ALLOWED_EMAILS="me@gmail.com",
    JWT_SIGNING_KEY="k" * 32,
    FASTMCP_HOME=str(_tmp / "fastmcp"),
    ACCOUNTS_DIR=str(_tmp / "accounts"),
    JOURNAL_PATH=str(_tmp / "c.jsonl"),
)

import pytest


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    monkeypatch.setattr("mailhub_mcp.providers.gmail.api._sleep", lambda s: None)
    monkeypatch.setattr("mailhub_mcp.providers.gmail.api._pace_sleep", lambda s: None)
    monkeypatch.setattr("mailhub_mcp.providers.gmail.api._random", lambda: 0.0)  # no jitter in tests
