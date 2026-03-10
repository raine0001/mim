from fastapi import FastAPI
from sqlalchemy import text

from core import models  # noqa: F401
from core.config import settings
from core.db import Base, engine
from core.logging_journal import configure_logging, journal_event
from core.routers import api_router

configure_logging()

app = FastAPI(title=settings.app_name, version=settings.app_version)
app.include_router(api_router)


@app.on_event("startup")
async def ensure_schema() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE IF EXISTS routing_execution_metrics ADD COLUMN IF NOT EXISTS policy_version VARCHAR(80) DEFAULT 'routing-policy-v1'"))
        await conn.execute(text("ALTER TABLE IF EXISTS routing_execution_metrics ADD COLUMN IF NOT EXISTS engine_version VARCHAR(120) DEFAULT 'unknown'"))
        await conn.execute(text("ALTER TABLE IF EXISTS actions ADD COLUMN IF NOT EXISTS expected_state_delta JSON DEFAULT '{}'::json"))
        await conn.execute(text("ALTER TABLE IF EXISTS actions ADD COLUMN IF NOT EXISTS validation_method VARCHAR(120) DEFAULT 'hint'"))
        await conn.execute(text("ALTER TABLE IF EXISTS actions ADD COLUMN IF NOT EXISTS sequence_index INTEGER DEFAULT 1"))
        await conn.execute(text("ALTER TABLE IF EXISTS actions ADD COLUMN IF NOT EXISTS depends_on_action_id INTEGER"))
        await conn.execute(text("ALTER TABLE IF EXISTS actions ADD COLUMN IF NOT EXISTS parent_action_id INTEGER"))
        await conn.execute(text("ALTER TABLE IF EXISTS actions ADD COLUMN IF NOT EXISTS retry_of_action_id INTEGER"))
        await conn.execute(text("ALTER TABLE IF EXISTS actions ADD COLUMN IF NOT EXISTS retry_count INTEGER DEFAULT 0"))
        await conn.execute(text("ALTER TABLE IF EXISTS actions ADD COLUMN IF NOT EXISTS replaced_action_id INTEGER"))
        await conn.execute(text("ALTER TABLE IF EXISTS actions ADD COLUMN IF NOT EXISTS replacement_action_id INTEGER"))
        await conn.execute(text("ALTER TABLE IF EXISTS actions ADD COLUMN IF NOT EXISTS recovery_classification VARCHAR(40) DEFAULT ''"))
        await conn.execute(text("ALTER TABLE IF EXISTS actions ADD COLUMN IF NOT EXISTS chain_event VARCHAR(40) DEFAULT ''"))


@app.get("/")
def root():
    journal_event(actor="system", action="root", result="ok")
    return {"status": "MIM core online"}
