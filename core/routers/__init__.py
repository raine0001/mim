from fastapi import APIRouter

from core.routers import custody, gateway, health, journal, manifest, memory, objectives, operator, preferences, results, reviews, routing, services, status, tasks, tools, workspace

api_router = APIRouter()
api_router.include_router(health.router, prefix="")
api_router.include_router(status.router, prefix="")
api_router.include_router(manifest.router, prefix="")
api_router.include_router(gateway.router, prefix="/gateway", tags=["gateway"])
api_router.include_router(operator.router, prefix="/operator", tags=["operator"])
api_router.include_router(objectives.router, prefix="/objectives", tags=["objectives"])
api_router.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
api_router.include_router(custody.router, prefix="", tags=["custody"])
api_router.include_router(results.router, prefix="/results", tags=["results"])
api_router.include_router(reviews.router, prefix="/reviews", tags=["reviews"])
api_router.include_router(routing.router, prefix="/routing", tags=["routing"])
api_router.include_router(journal.router, prefix="/journal", tags=["journal"])
api_router.include_router(memory.router, prefix="/memory", tags=["memory"])
api_router.include_router(preferences.router, prefix="", tags=["preferences"])
api_router.include_router(workspace.router, prefix="/workspace", tags=["workspace"])
api_router.include_router(tools.router, prefix="/tools", tags=["tools"])
api_router.include_router(services.router, prefix="/services", tags=["services"])
