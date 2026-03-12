from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.interface_service import (
    append_interface_message,
    get_interface_session,
    list_interface_messages,
    list_interface_sessions,
    submit_interface_approval,
    to_interface_approval_out,
    to_interface_message_out,
    to_interface_session_out,
    upsert_interface_session,
)
from core.journal import write_journal
from core.schemas import (
    InterfaceApprovalRequest,
    InterfaceMessageCreateRequest,
    InterfaceSessionUpsertRequest,
)

router = APIRouter()


@router.post("/interface/sessions/{session_key}")
async def upsert_interface_session_endpoint(
    session_key: str,
    payload: InterfaceSessionUpsertRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await upsert_interface_session(
        session_key=session_key,
        actor=payload.actor,
        source=payload.source,
        channel=payload.channel,
        status=payload.status,
        context_json=payload.context_json,
        metadata_json=payload.metadata_json,
        db=db,
    )
    await write_journal(
        db,
        actor=payload.actor,
        action="interface_session_upserted",
        target_type="workspace_interface_session",
        target_id=row.session_key,
        summary=f"Upserted interface session {row.session_key}",
        metadata_json={
            "channel": row.channel,
            "status": row.status,
            **payload.metadata_json,
        },
    )
    await db.commit()
    return {"session": to_interface_session_out(row)}


@router.get("/interface/sessions")
async def list_interface_sessions_endpoint(
    status: str = Query(default=""),
    channel: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await list_interface_sessions(
        status=status,
        channel=channel,
        limit=limit,
        db=db,
    )
    return {"sessions": [to_interface_session_out(item) for item in rows]}


@router.get("/interface/sessions/{session_key}")
async def get_interface_session_endpoint(
    session_key: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await get_interface_session(session_key=session_key, db=db)
    if not row:
        raise HTTPException(status_code=404, detail="interface_session_not_found")
    return {"session": to_interface_session_out(row)}


@router.post("/interface/sessions/{session_key}/messages")
async def append_interface_message_endpoint(
    session_key: str,
    payload: InterfaceMessageCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        session, message = await append_interface_message(
            session_key=session_key,
            actor=payload.actor,
            source=payload.source,
            direction=payload.direction,
            role=payload.role,
            content=payload.content,
            parsed_intent=payload.parsed_intent,
            confidence=payload.confidence,
            requires_approval=payload.requires_approval,
            metadata_json=payload.metadata_json,
            db=db,
        )
    except ValueError as exc:
        if str(exc) == "session_not_found":
            raise HTTPException(status_code=404, detail="interface_session_not_found")
        raise

    await write_journal(
        db,
        actor=payload.actor,
        action="interface_message_appended",
        target_type="workspace_interface_message",
        target_id=str(message.id),
        summary=f"Appended interface message {message.id} to session {session.session_key}",
        metadata_json={
            "direction": message.direction,
            "role": message.role,
            "requires_approval": bool(message.requires_approval),
            **payload.metadata_json,
        },
    )
    await db.commit()
    return {
        "session": to_interface_session_out(session),
        "message": to_interface_message_out(message),
    }


@router.get("/interface/sessions/{session_key}/messages")
async def list_interface_messages_endpoint(
    session_key: str,
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        session, rows = await list_interface_messages(
            session_key=session_key,
            limit=limit,
            db=db,
        )
    except ValueError as exc:
        if str(exc) == "session_not_found":
            raise HTTPException(status_code=404, detail="interface_session_not_found")
        raise

    return {
        "session": to_interface_session_out(session),
        "messages": [to_interface_message_out(item) for item in rows],
    }


@router.post("/interface/sessions/{session_key}/approvals")
async def submit_interface_approval_endpoint(
    session_key: str,
    payload: InterfaceApprovalRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        session, approval = await submit_interface_approval(
            session_key=session_key,
            actor=payload.actor,
            source=payload.source,
            message_id=payload.message_id,
            decision=payload.decision,
            reason=payload.reason,
            metadata_json=payload.metadata_json,
            db=db,
        )
    except ValueError as exc:
        code = str(exc)
        if code == "session_not_found":
            raise HTTPException(status_code=404, detail="interface_session_not_found")
        if code == "message_not_found":
            raise HTTPException(status_code=404, detail="interface_message_not_found")
        if code == "invalid_decision":
            raise HTTPException(status_code=422, detail="invalid_interface_decision")
        raise

    await write_journal(
        db,
        actor=payload.actor,
        action="interface_approval_submitted",
        target_type="workspace_interface_approval",
        target_id=str(approval.id),
        summary=(
            f"Submitted interface approval {approval.id} "
            f"decision={approval.decision} session={session.session_key}"
        ),
        metadata_json={
            "message_id": approval.message_id,
            "decision": approval.decision,
            **payload.metadata_json,
        },
    )
    await db.commit()
    return {
        "session": to_interface_session_out(session),
        "approval": to_interface_approval_out(approval),
    }
