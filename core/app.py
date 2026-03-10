from fastapi import FastAPI

from core.config import settings
from core.logging_journal import configure_logging, journal_event
from core.routers import api_router

configure_logging()

app = FastAPI(title=settings.app_name, version=settings.app_version)
app.include_router(api_router)


@app.get("/")
def root():
    journal_event(actor="system", action="root", result="ok")
    return {"status": "MIM core online"}
