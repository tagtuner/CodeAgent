"""Asyncio-compatible context for wiring per-chat workspace into tools."""

from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path

# Set at the start of each Agent.run() to this chat's workspace root .../workspaces/<session_id>
SESSION_WORKSPACE: ContextVar[Path | None] = ContextVar("SESSION_WORKSPACE", default=None)
