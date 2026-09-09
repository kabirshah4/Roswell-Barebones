"""Shared FastAPI dependencies. Routes read app state through these."""

from pathlib import Path
from typing import Any

from fastapi import Request


def get_db_path(request: Request) -> Path:
    return request.app.state.cfg.db_path


def get_client(request: Request) -> Any:
    return request.app.state.client
