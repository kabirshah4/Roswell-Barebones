import os
from pathlib import Path

import pytest

from backend.db import database

# Credentials the app reads from the environment. Since config.py started
# loading .env at import, the offline suite would otherwise see the developer's
# real keys -- and a test asserting "no key means disabled" would pass or fail
# depending on a gitignored file. Live tests opt back in explicitly.
_CREDENTIAL_VARS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "FRED_API_KEY", "DISCORD_WEBHOOK_URL",
)


@pytest.fixture(autouse=True)
def isolated_credentials(request, monkeypatch):
    """Offline tests run as if no credentials are configured."""
    if request.node.get_closest_marker("live"):
        yield
        return
    for name in _CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture(autouse=True)
def configured_screener(monkeypatch):
    """Fetch tests inject their own poster, but still need an endpoint set.

    Roswell ships with SCREENER_SCAN_URL unset, so a fetch short-circuits
    before any injected poster runs. The `.invalid` TLD cannot resolve, so a
    test that escapes its fake poster fails loudly instead of calling a real
    service. Tests asserting the unconfigured behaviour delete it again.
    """
    monkeypatch.setenv("SCREENER_SCAN_URL", "https://screener.invalid/{market}/scan")
    yield


@pytest.fixture
def real_credentials():
    """Opt back in for a test that genuinely needs whatever is configured."""
    return {name: os.environ.get(name) for name in _CREDENTIAL_VARS}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    database.init_db(path)
    return path


@pytest.fixture
def conn(db_path: Path):
    with database.get_conn(db_path) as connection:
        yield connection
