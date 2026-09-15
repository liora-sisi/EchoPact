"""Loopback-only API over the local recall debug packet."""

from __future__ import annotations

from dataclasses import dataclass
import hmac
from typing import Any, Dict, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from backend.memory.recall_debug import run_recall_debug
from backend.memory.records_v1 import _connect_readonly
from backend.memory.source_provenance import source_channel_for_kind
from backend.memory.speech_query_rescue import SpeechQueryRescuePolicy


READONLY_API_SCHEMA_VERSION = "echo-pact-telegram-readonly-api-v1"


@dataclass(frozen=True)
class ReadonlyApiConfig:
    db_path: str
    agent_id: str
    result_limit: int = 5
    expected_source_channels: tuple[str, ...] = ("chatgpt", "codex_local")
    speech_query_policy: Optional[SpeechQueryRescuePolicy] = None
    local_access_token: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.db_path.strip():
            raise ValueError("db_path is required")
        if not self.agent_id.strip():
            raise ValueError("agent_id is required")
        if not 1 <= self.result_limit <= 10:
            raise ValueError("result_limit must be between 1 and 10")
        if self.local_access_token is not None and len(self.local_access_token) < 24:
            raise ValueError("local_access_token must contain at least 24 characters")


class RecallDebugRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    as_of: Optional[str] = None


def database_source_channels(db_path: str) -> list[str]:
    """Inspect adapter kinds through the same fail-closed read-only connector."""

    conn = _connect_readonly(db_path)
    try:
        kinds = [
            str(row["source_kind"])
            for row in conn.execute(
                "SELECT DISTINCT source_kind FROM records_v1 ORDER BY source_kind"
            ).fetchall()
        ]
    finally:
        conn.close()
    return sorted({source_channel_for_kind(kind) for kind in kinds})


def validate_database_source_scope(config: ReadonlyApiConfig) -> list[str]:
    channels = database_source_channels(config.db_path)
    expected = sorted(set(config.expected_source_channels))
    if not expected:
        raise RuntimeError("at least one required source channel must be configured")
    if channels != expected:
        raise RuntimeError(
            "configured database does not match the required source scope"
        )
    return channels


def create_readonly_app(config: ReadonlyApiConfig) -> FastAPI:
    """Create an API that never initializes, migrates, or writes SQLite."""

    source_channels = validate_database_source_scope(config)
    app = FastAPI(
        title="Echo Pact Telegram read-only recall",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        return {
            "schema_version": READONLY_API_SCHEMA_VERSION,
            "status": "ok",
            "source_channels": source_channels,
            "database_path_exposed": False,
        }

    @app.post("/recall-debug")
    async def recall_debug(
        request: RecallDebugRequest,
        x_echo_local_token: Optional[str] = Header(default=None),
    ) -> Dict[str, Any]:
        expected_token = config.local_access_token
        if expected_token is not None and not (
            x_echo_local_token
            and hmac.compare_digest(x_echo_local_token, expected_token)
        ):
            raise HTTPException(status_code=401, detail="Unauthorized")
        try:
            return run_recall_debug(
                request.query,
                db_path=config.db_path,
                agent_id=config.agent_id,
                limit=config.result_limit,
                as_of=request.as_of,
                include_projection=True,
                speech_query_policy=config.speech_query_policy,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except Exception:
            raise HTTPException(
                status_code=500,
                detail="Echo Pact read-only recall failed",
            ) from None

    return app
