from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core import models  # noqa: F401
from core.config import settings
from core.db import Base, engine
from core.logging_journal import configure_logging, journal_event
from core.routers import api_router


configure_logging()


OPTIONAL_ALTER_TABLE_STATEMENTS = (
    "ALTER TABLE IF EXISTS capability_executions ADD COLUMN IF NOT EXISTS resolution_id INTEGER",
    "ALTER TABLE IF EXISTS capability_executions ADD COLUMN IF NOT EXISTS goal_id INTEGER",
    "ALTER TABLE IF EXISTS capability_executions ADD COLUMN IF NOT EXISTS arguments_json JSONB DEFAULT '{}'::jsonb",
    "ALTER TABLE IF EXISTS capability_executions ADD COLUMN IF NOT EXISTS safety_mode VARCHAR(40) DEFAULT 'standard'",
    "ALTER TABLE IF EXISTS capability_executions ADD COLUMN IF NOT EXISTS requested_executor VARCHAR(120) DEFAULT 'tod'",
    "ALTER TABLE IF EXISTS capability_executions ADD COLUMN IF NOT EXISTS dispatch_decision VARCHAR(40) DEFAULT 'requires_confirmation'",
    "ALTER TABLE IF EXISTS capability_executions ADD COLUMN IF NOT EXISTS trace_id VARCHAR(120) DEFAULT ''",
    "ALTER TABLE IF EXISTS capability_executions ADD COLUMN IF NOT EXISTS managed_scope VARCHAR(120) DEFAULT 'global'",
    "ALTER TABLE IF EXISTS capability_executions ADD COLUMN IF NOT EXISTS status VARCHAR(40) DEFAULT 'pending'",
    "ALTER TABLE IF EXISTS capability_executions ADD COLUMN IF NOT EXISTS reason TEXT DEFAULT ''",
    "ALTER TABLE IF EXISTS capability_executions ADD COLUMN IF NOT EXISTS feedback_json JSONB DEFAULT '{}'::jsonb",
    "ALTER TABLE IF EXISTS capability_executions ADD COLUMN IF NOT EXISTS execution_truth_json JSONB DEFAULT '{}'::jsonb",
)


def _optional_schema_deadlock(exc: Exception) -> bool:
    text_value = str(exc or "").lower()
    return "deadlock detected" in text_value or "lock timeout" in text_value


def _schema_connectivity_unavailable(exc: Exception) -> bool:
    text_value = str(exc or "").lower()
    return isinstance(exc, (ConnectionRefusedError, TimeoutError, OSError)) or any(
        phrase in text_value
        for phrase in (
            "refused the network connection",
            "connection refused",
            "failed to connect",
            "could not connect",
            "asyncpg",
            "targetserverattributenotmatched",
        )
    )


async def _execute_optional_schema_ddl(conn, statement: str) -> None:
    try:
        async with conn.begin_nested():
            await conn.execute(text(statement))
    except DBAPIError as exc:
        if not _optional_schema_deadlock(exc):
            raise
        journal_event(
            actor="system",
            action="startup_optional_schema_ddl_deferred",
            result="degraded",
            metadata={
                "statement": statement,
                "error": str(exc),
            },
        )


async def ensure_schema() -> None:
    async with engine.begin() as conn:
        try:
            await conn.execute(text("SELECT pg_advisory_xact_lock(814523791337610201)"))
        except Exception:
            pass
        await conn.run_sync(Base.metadata.create_all)
        for statement in OPTIONAL_ALTER_TABLE_STATEMENTS:
            await _execute_optional_schema_ddl(conn, statement)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        await ensure_schema()
    except Exception as exc:
        if not _schema_connectivity_unavailable(exc):
            raise
        journal_event(
            actor="system",
            action="startup_schema_unavailable",
            result="degraded",
            metadata={
                "error": str(exc),
            },
        )
    yield
    await engine.dispose()


app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)
app.include_router(api_router)


@app.middleware("http")
async def add_no_store_headers(request: Request, call_next) -> Response:
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response
