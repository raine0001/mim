from fastapi import APIRouter

from core.routers import health, journal, manifest, memory, objectives, results, reviews, services, status, tasks, tools

api_router = APIRouter()
api_router.include_router(health.router, prefix="")
api_router.include_router(status.router, prefix="")
api_router.include_router(manifest.router, prefix="")
api_router.include_router(objectives.router, prefix="/objectives", tags=["objectives"])
api_router.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
api_router.include_router(results.router, prefix="/results", tags=["results"])
api_router.include_router(reviews.router, prefix="/reviews", tags=["reviews"])
api_router.include_router(journal.router, prefix="/journal", tags=["journal"])
api_router.include_router(memory.router, prefix="/memory", tags=["memory"])
api_router.include_router(tools.router, prefix="/tools", tags=["tools"])
api_router.include_router(services.router, prefix="/services", tags=["services"])
