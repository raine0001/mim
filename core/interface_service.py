from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import WorkspaceInterfaceApproval, WorkspaceInterfaceMessage, WorkspaceInterfaceSession
from core.state_bus_service import append_state_bus_event

INTERFACE_SOURCE = "objective74"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def upsert_interface_session(
    *,
    session_key: str,
    actor: str,
    source: str,
    channel: str,
    status: str,
    context_json: dict,
    metadata_json: dict,
    db: AsyncSession,
) -> WorkspaceInterfaceSession:
    key = str(session_key or "").strip()
    if not key:
        raise ValueError("session_key_required")

    row = (
        await db.execute(
            select(WorkspaceInterfaceSession)
            .where(WorkspaceInterfaceSession.session_key == key)
            .limit(1)
        )
    ).scalars().first()

    now = _utc_now()

    if row is None:
        row = WorkspaceInterfaceSession(
            session_key=key,
            actor=str(actor or "workspace"),
            source=str(source or INTERFACE_SOURCE),
            channel=str(channel or "text"),
            status=str(status or "active"),
            context_json=context_json if isinstance(context_json, dict) else {},
            metadata_json=metadata_json if isinstance(metadata_json, dict) else {},
            updated_at=now,
        )
        db.add(row)
        await db.flush()
        return row

    row.actor = str(actor or row.actor or "workspace")
    row.source = str(source or row.source or INTERFACE_SOURCE)
    row.channel = str(channel or row.channel or "text")
    row.status = str(status or row.status or "active")
    row.context_json = context_json if isinstance(context_json, dict) else {}
    row.metadata_json = metadata_json if isinstance(metadata_json, dict) else {}
    row.updated_at = now
    await db.flush()
    return row


async def get_interface_session(*, session_key: str, db: AsyncSession) -> WorkspaceInterfaceSession | None:
    return (
        await db.execute(
            select(WorkspaceInterfaceSession)
            .where(WorkspaceInterfaceSession.session_key == str(session_key or "").strip())
            .limit(1)
        )
    ).scalars().first()


async def list_interface_sessions(
    *,
    status: str,
    channel: str,
    limit: int,
    db: AsyncSession,
) -> list[WorkspaceInterfaceSession]:
    stmt = select(WorkspaceInterfaceSession).order_by(WorkspaceInterfaceSession.id.desc())
    if str(status or "").strip():
        stmt = stmt.where(WorkspaceInterfaceSession.status == str(status).strip())
    if str(channel or "").strip():
        stmt = stmt.where(WorkspaceInterfaceSession.channel == str(channel).strip())
    stmt = stmt.limit(max(1, min(int(limit), 500)))
    return list((await db.execute(stmt)).scalars().all())


async def append_interface_message(
    *,
    session_key: str,
    actor: str,
    source: str,
    direction: str,
    role: str,
    content: str,
    parsed_intent: str,
    confidence: float,
    requires_approval: bool,
    metadata_json: dict,
    db: AsyncSession,
) -> tuple[WorkspaceInterfaceSession, WorkspaceInterfaceMessage]:
    session = await get_interface_session(session_key=session_key, db=db)
    if not session:
        raise ValueError("session_not_found")

    now = _utc_now()
    row = WorkspaceInterfaceMessage(
        session_id=int(session.id),
        source=str(source or INTERFACE_SOURCE),
        actor=str(actor or "workspace"),
        direction=str(direction or "inbound"),
        role=str(role or "operator"),
        content=str(content or ""),
        parsed_intent=str(parsed_intent or ""),
        confidence=max(0.0, min(float(confidence), 1.0)),
        requires_approval=bool(requires_approval),
        delivery_status="accepted",
        metadata_json=metadata_json if isinstance(metadata_json, dict) else {},
    )
    db.add(row)
    await db.flush()

    if row.direction == "inbound":
        session.last_input_at = now
    else:
        session.last_output_at = now
    session.updated_at = now
    await db.flush()

    await append_state_bus_event(
        actor=actor,
        source=INTERFACE_SOURCE,
        event_domain="mim.assist",
        event_type="interface.message.received" if row.direction == "inbound" else "interface.message.sent",
        stream_key=f"interface:{session.session_key}",
        payload_json={
            "session_key": session.session_key,
            "message_id": int(row.id),
            "direction": row.direction,
            "role": row.role,
            "parsed_intent": row.parsed_intent,
            "requires_approval": bool(row.requires_approval),
        },
        metadata_json={
            **(metadata_json if isinstance(metadata_json, dict) else {}),
            "objective": "objective74",
        },
        db=db,
    )

    return session, row


async def list_interface_messages(
    *,
    session_key: str,
    limit: int,
    db: AsyncSession,
) -> tuple[WorkspaceInterfaceSession, list[WorkspaceInterfaceMessage]]:
    session = await get_interface_session(session_key=session_key, db=db)
    if not session:
        raise ValueError("session_not_found")

    rows = (
        await db.execute(
            select(WorkspaceInterfaceMessage)
            .where(WorkspaceInterfaceMessage.session_id == int(session.id))
            .order_by(WorkspaceInterfaceMessage.id.desc())
            .limit(max(1, min(int(limit), 500)))
        )
    ).scalars().all()
    return session, list(rows)


async def submit_interface_approval(
    *,
    session_key: str,
    actor: str,
    source: str,
    message_id: int | None,
    decision: str,
    reason: str,
    metadata_json: dict,
    db: AsyncSession,
) -> tuple[WorkspaceInterfaceSession, WorkspaceInterfaceApproval]:
    session = await get_interface_session(session_key=session_key, db=db)
    if not session:
        raise ValueError("session_not_found")

    related_message_id: int | None = None
    if message_id and int(message_id) > 0:
        related = (
            await db.execute(
                select(WorkspaceInterfaceMessage)
                .where(
                    WorkspaceInterfaceMessage.id == int(message_id),
                    WorkspaceInterfaceMessage.session_id == int(session.id),
                )
                .limit(1)
            )
        ).scalars().first()
        if not related:
            raise ValueError("message_not_found")
        related_message_id = int(related.id)

    allowed = {"approved", "rejected", "deferred"}
    normalized_decision = str(decision or "approved").strip().lower()
    if normalized_decision not in allowed:
        raise ValueError("invalid_decision")

    approval = WorkspaceInterfaceApproval(
        session_id=int(session.id),
        message_id=related_message_id,
        source=str(source or INTERFACE_SOURCE),
        actor=str(actor or "operator"),
        decision=normalized_decision,
        reason=str(reason or ""),
        metadata_json=metadata_json if isinstance(metadata_json, dict) else {},
    )
    db.add(approval)
    session.updated_at = _utc_now()
    await db.flush()

    await append_state_bus_event(
        actor=actor,
        source=INTERFACE_SOURCE,
        event_domain="mim.assist",
        event_type=f"interface.approval.{normalized_decision}",
        stream_key=f"interface:{session.session_key}",
        payload_json={
            "session_key": session.session_key,
            "approval_id": int(approval.id),
            "message_id": related_message_id,
            "decision": normalized_decision,
            "reason": str(reason or ""),
        },
        metadata_json={
            **(metadata_json if isinstance(metadata_json, dict) else {}),
            "objective": "objective74",
        },
        db=db,
    )

    return session, approval


def to_interface_session_out(row: WorkspaceInterfaceSession) -> dict:
    updated_at = row.__dict__.get("updated_at")
    return {
        "session_id": int(row.id),
        "source": row.source,
        "actor": row.actor,
        "session_key": row.session_key,
        "channel": row.channel,
        "status": row.status,
        "last_input_at": row.last_input_at,
        "last_output_at": row.last_output_at,
        "context_json": row.context_json if isinstance(row.context_json, dict) else {},
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "updated_at": updated_at if updated_at is not None else row.created_at,
        "created_at": row.created_at,
    }


def to_interface_message_out(row: WorkspaceInterfaceMessage) -> dict:
    return {
        "message_id": int(row.id),
        "session_id": int(row.session_id),
        "source": row.source,
        "actor": row.actor,
        "direction": row.direction,
        "role": row.role,
        "content": row.content,
        "parsed_intent": row.parsed_intent,
        "confidence": float(row.confidence or 0.0),
        "requires_approval": bool(row.requires_approval),
        "delivery_status": row.delivery_status,
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }


def to_interface_approval_out(row: WorkspaceInterfaceApproval) -> dict:
    return {
        "approval_id": int(row.id),
        "session_id": int(row.session_id),
        "message_id": int(row.message_id) if row.message_id is not None else None,
        "source": row.source,
        "actor": row.actor,
        "decision": row.decision,
        "reason": row.reason,
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }
