from fastapi import APIRouter

from core.config import settings
from core.integrations import integration_toggles

router = APIRouter(tags=["status"])


@router.get("/status")
def status() -> dict:
    return {
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "integrations": integration_toggles(),
    }
