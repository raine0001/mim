from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.models import Tool

router = APIRouter()


class ToolCreate(BaseModel):
    name: str
    description: str = ""


@router.post("")
async def register_tool(payload: ToolCreate, db: AsyncSession = Depends(get_db)) -> dict:
    tool = Tool(name=payload.name, description=payload.description)
    db.add(tool)
    await db.commit()
    await db.refresh(tool)
    return {"id": tool.id, "name": tool.name, "description": tool.description, "enabled": tool.enabled}


@router.get("")
async def list_tools(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(Tool).order_by(Tool.id.desc()))).scalars().all()
    return [{"id": t.id, "name": t.name, "description": t.description, "enabled": t.enabled} for t in rows]
