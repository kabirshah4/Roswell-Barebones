"""Settings editable from the UI.

The only setting so far is the Discord webhook, which is a credential: anyone
holding the URL can post to the channel. It is therefore never sent back to the
browser in full — the UI shows a masked form and whether one is configured.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field, field_validator

from backend.db import database
from backend.routes import get_db_path

router = APIRouter(prefix="/api/settings", tags=["settings"])

WEBHOOK_KEY = "discord_webhook_url"
ACCOUNT_KEY = "account_size"
RISK_KEY = "risk_pct"

# Not a credential, unlike the webhook — these come straight back to the
# browser, because the sizing panel is useless without them.
DEFAULT_ACCOUNT = 10_000.0
DEFAULT_RISK_PCT = 1.0

_VALID_PREFIXES = (
    "https://discord.com/api/webhooks/",
    "https://discordapp.com/api/webhooks/",
    "https://ptb.discord.com/api/webhooks/",
    "https://canary.discord.com/api/webhooks/",
)


class AccountIn(BaseModel):
    account_size: float = Field(gt=0, le=1e12)
    # An upper bound with a reason: above ~20% per trade, five losses in a row
    # is most of the account, and the arithmetic stops being risk management.
    risk_pct: float = Field(gt=0, le=20)


class WebhookIn(BaseModel):
    url: str = Field(min_length=1, max_length=500)

    @field_validator("url")
    @classmethod
    def looks_like_a_discord_webhook(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned.startswith(_VALID_PREFIXES):
            # Rejecting early beats silently posting the user's setups to
            # whatever host they pasted by mistake.
            raise ValueError("must be a https://discord.com/api/webhooks/... URL")
        return cleaned


def mask(url: str | None) -> str | None:
    """Show enough to recognise the webhook, never enough to use it."""
    if not url:
        return None
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return f"…/{tail[:4]}{'•' * 8}" if tail else "…"


@router.get("")
async def read_settings(request: Request, db_path: Path = Depends(get_db_path)) -> dict:
    with database.get_conn(db_path) as conn:
        stored = database.get_setting(conn, WEBHOOK_KEY)
    client = getattr(request.app.state, "discord_client", None)
    return {
        "discord_webhook_configured": bool(getattr(client, "enabled", False)),
        "discord_webhook_hint": mask(stored),
        # An environment variable cannot be edited from the browser; say so
        # rather than letting a save appear to do nothing.
        "discord_webhook_source": "database" if stored else (
            "environment" if getattr(client, "enabled", False) else "unset"
        ),
        **_account(db_path),
    }


def _account(db_path: Path) -> dict:
    with database.get_conn(db_path) as conn:
        raw_account = database.get_setting(conn, ACCOUNT_KEY)
        raw_risk = database.get_setting(conn, RISK_KEY)
    try:
        account = float(raw_account) if raw_account else DEFAULT_ACCOUNT
    except ValueError:
        account = DEFAULT_ACCOUNT
    try:
        risk = float(raw_risk) if raw_risk else DEFAULT_RISK_PCT
    except ValueError:
        risk = DEFAULT_RISK_PCT
    return {
        "account_size": account,
        "risk_pct": risk,
        "account_configured": bool(raw_account),
    }


@router.put("/account")
async def set_account(
    body: AccountIn, db_path: Path = Depends(get_db_path)
) -> dict:
    with database.get_conn(db_path) as conn:
        database.set_setting(conn, ACCOUNT_KEY, str(body.account_size))
        database.set_setting(conn, RISK_KEY, str(body.risk_pct))
    return _account(db_path)


@router.put("/discord-webhook")
async def set_webhook(
    body: WebhookIn, request: Request, db_path: Path = Depends(get_db_path)
) -> dict:
    with database.get_conn(db_path) as conn:
        database.set_setting(conn, WEBHOOK_KEY, body.url)
    # Apply immediately: requiring a restart to start receiving alerts is the
    # kind of thing people quietly conclude is broken.
    client = getattr(request.app.state, "discord_client", None)
    if client is not None and hasattr(client, "set_webhook_url"):
        client.set_webhook_url(body.url)
    return {"discord_webhook_configured": True,
            "discord_webhook_hint": mask(body.url),
            "discord_webhook_source": "database"}


@router.delete("/discord-webhook", status_code=status.HTTP_204_NO_CONTENT)
async def clear_webhook(
    request: Request, db_path: Path = Depends(get_db_path)
) -> Response:
    with database.get_conn(db_path) as conn:
        database.delete_setting(conn, WEBHOOK_KEY)
    client = getattr(request.app.state, "discord_client", None)
    if client is not None and hasattr(client, "set_webhook_url"):
        import os

        # Fall back to the environment rather than going dark: an env var was
        # the configured source before the UI existed.
        client.set_webhook_url(os.environ.get("DISCORD_WEBHOOK_URL") or "")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
