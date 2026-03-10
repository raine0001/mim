from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.models import Service

router = APIRouter()


class ServiceRegister(BaseModel):
    name: str
    status: str = "active"
    dependency_map: dict = Field(default_factory=dict)


@router.post("")
async def register_service(payload: ServiceRegister, db: AsyncSession = Depends(get_db)) -> dict:
    service = Service(
        name=payload.name,
        status=payload.status,
        dependency_map=payload.dependency_map,
        heartbeat_at=datetime.now(timezone.utc),
    )
    db.add(service)
    await db.commit()
    await db.refresh(service)
    return {"id": service.id, "name": service.name, "status": service.status}


@router.post("/{service_id}/heartbeat")
async def heartbeat(service_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    service = await db.get(Service, service_id)
    if not service:
        return {"updated": False, "reason": "service not found"}

    service.heartbeat_at = datetime.now(timezone.utc)
    await db.commit()
    return {"updated": True, "service_id": service_id, "heartbeat_at": service.heartbeat_at}


@router.get("")
async def list_services(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(Service).order_by(Service.id.desc()))).scalars().all()
    return [
        {
            "id": s.id,
            "name": s.name,
            "status": s.status,
            "heartbeat_at": s.heartbeat_at,
            "dependency_map": s.dependency_map,
        }
        for s in rows
    ]
