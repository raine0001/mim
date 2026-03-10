from core.config import settings


def integration_toggles() -> dict[str, bool]:
    return {
        "openai": settings.allow_openai,
        "web_access": settings.allow_web_access,
        "local_devices": settings.allow_local_devices,
    }
