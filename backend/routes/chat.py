"""Chat route.

This is the second and last route permitted to make an outbound call - it talks
to Anthropic, as POST /api/watchlist talks to Yahoo. Every other read route
serves SQLite only, and the agent's own tools are SQLite-only, so a chat turn
costs one provider call and no market-data traffic.
"""

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator

from backend.routes import get_db_path

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=16000)
    history: list[dict] = Field(default_factory=list)

    @field_validator("message")
    @classmethod
    def not_blank(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("message must not be blank")
        return cleaned


def get_agent(request: Request) -> Any:
    return request.app.state.chat_agent


def get_history_limit(request: Request) -> int:
    return request.app.state.cfg.chat_history_turns


@router.post("")
async def chat(
    body: ChatIn,
    db_path: Path = Depends(get_db_path),
    agent: Any = Depends(get_agent),
    history_limit: int = Depends(get_history_limit),
) -> dict:
    # Trim history so a long session cannot grow the prompt without bound. The
    # ceiling is generous: a chat that forgets what you asked two questions ago
    # is worse than a slightly larger prompt, and the tools are SQLite reads.
    return agent.ask(body.message, body.history[-history_limit:], db_path)
