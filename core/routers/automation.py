from datetime import datetime, timezone
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.db import get_db
from core.journal import write_journal
from core.models import (
    AutomationAuthChallenge,
    AutomationCarrierRunStatus,
    AutomationExecutionRun,
    AutomationFileArtifact,
    AutomationPlaybook,
    AutomationWebSession,
)
from core.schemas import (
    AutomationAuthChallengeActionRequest,
    AutomationAuthResolveRequest,
    AutomationCalendarGoogleAuthUrlRequest,
    AutomationCalendarGoogleExchangeCodeRequest,
    AutomationCalendarReminderCreateRequest,
    AutomationEmailExtractMfaRequest,
    AutomationEmailPollRequest,
    AutomationFileDetectRequest,
    AutomationFileDownloadRequest,
    AutomationNavigationExecuteRequest,
    AutomationPlaybookRefineRequest,
    AutomationPlaybookUpsertRequest,
    AutomationReconciliationEvaluateRequest,
    AutomationRecoveryEvaluateRequest,
    AutomationRecoveryRetryRequest,
    AutomationRunCarrierStatusUpdateRequest,
    AutomationRunCreateRequest,
    AutomationWebActionRequest,
    AutomationWebNavigateRequest,
    AutomationWebSessionCreateRequest,
)
from core.web_automation_service import web_automation_service

router = APIRouter(tags=["automation"])


def _ensure_enabled() -> None:
    if not settings.automation_enabled:
        raise HTTPException(status_code=503, detail="automation_disabled")


@router.post("/web/sessions")
async def create_web_session(
    payload: AutomationWebSessionCreateRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    _ensure_enabled()
    simulation_mode = (
        settings.automation_default_simulation
        if payload.simulation_mode is None
        else bool(payload.simulation_mode)
    )
    headless = (
        settings.automation_browser_headless
        if payload.headless is None
        else bool(payload.headless)
    )
    try:
        session = await web_automation_service.create_session(
            db,
            carrier_id=payload.carrier_id,
            session_key=payload.session_key,
            simulation_mode=simulation_mode,
            headless=headless,
            start_url=payload.start_url,
            metadata_json=payload.metadata_json,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await write_journal(
        db,
        actor="automation",
        action="web_session_created",
        target_type="automation_web_session",
        target_id=str(session.id),
        summary=f"Created web session {session.session_key}",
        metadata_json={
            "carrier_id": session.carrier_id,
            "simulation_mode": session.simulation_mode,
        },
    )
    await db.commit()
    await db.refresh(session)
    return await web_automation_service.session_state(session)


@router.post("/web/sessions/{session_id}/navigate")
async def navigate_web_session(
    session_id: int,
    payload: AutomationWebNavigateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    _ensure_enabled()
    session = await db.get(AutomationWebSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session_not_found")
    try:
        state = await web_automation_service.navigate(
            db, session, url=payload.url, timeout_seconds=payload.timeout_seconds
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    return {"ok": True, "session_id": session.id, "state": state}


@router.post("/web/sessions/{session_id}/actions")
async def web_session_action(
    session_id: int,
    payload: AutomationWebActionRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    _ensure_enabled()
    session = await db.get(AutomationWebSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session_not_found")
    try:
        result = await web_automation_service.perform_action(
            session,
            action=payload.action,
            selector=payload.selector,
            text=payload.text,
            key=payload.key,
            value=payload.value,
            timeout_seconds=payload.timeout_seconds,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    return {"ok": True, "session_id": session.id, "result": result}


@router.get("/web/sessions/{session_id}/state")
async def web_session_state(
    session_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
    _ensure_enabled()
    session = await db.get(AutomationWebSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session_not_found")
    return await web_automation_service.session_state(session)


@router.delete("/web/sessions/{session_id}")
async def close_web_session(
    session_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
    _ensure_enabled()
    session = await db.get(AutomationWebSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session_not_found")
    await web_automation_service.close_session(db, session)
    await db.commit()
    return {"ok": True, "session_id": session.id, "status": "closed"}


@router.post("/auth/resolve")
async def resolve_auth(
    payload: AutomationAuthResolveRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    _ensure_enabled()
    session = (
        await db.get(AutomationWebSession, payload.session_id)
        if payload.session_id
        else None
    )

    needs_mfa = payload.pause_if_mfa_detected and not payload.mfa_code.strip()
    if needs_mfa:
        challenge = await web_automation_service.create_auth_challenge(
            db,
            session_id=session.id if session else None,
            carrier_id=payload.carrier_id,
            challenge_type="mfa",
            channel="email",
            prompt="MFA required. Provide 6-digit code to continue.",
            metadata_json=payload.metadata_json,
        )
        await db.commit()
        return {
            "ok": True,
            "status": "paused_for_mfa",
            "challenge_key": challenge.challenge_key,
            "challenge_id": challenge.id,
        }

    await db.commit()
    return {
        "ok": True,
        "status": "authenticated",
        "session_id": session.id if session else None,
        "carrier_id": payload.carrier_id,
        "used_mfa_code": bool(payload.mfa_code.strip()),
    }


@router.post("/auth/challenges/{challenge_key}/pause")
async def pause_auth_challenge(
    challenge_key: str,
    payload: AutomationAuthChallengeActionRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    _ensure_enabled()
    row = (
        await db.execute(
            select(AutomationAuthChallenge).where(
                AutomationAuthChallenge.challenge_key == challenge_key
            )
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="challenge_not_found")
    row.status = "paused"
    row.metadata_json = {
        **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
        "paused_by": payload.actor,
        "reason": payload.reason,
        **payload.metadata_json,
    }
    await db.commit()
    return {"ok": True, "challenge_key": row.challenge_key, "status": row.status}


@router.post("/auth/challenges/{challenge_key}/resume")
async def resume_auth_challenge(
    challenge_key: str,
    payload: AutomationAuthChallengeActionRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    _ensure_enabled()
    row = (
        await db.execute(
            select(AutomationAuthChallenge).where(
                AutomationAuthChallenge.challenge_key == challenge_key
            )
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="challenge_not_found")
    if not payload.mfa_code.strip():
        raise HTTPException(status_code=422, detail="mfa_code_required")
    row = await web_automation_service.apply_auth_resolution(
        db,
        challenge=row,
        mfa_code=payload.mfa_code,
        actor=payload.actor,
        reason=payload.reason,
        metadata_json=payload.metadata_json,
    )
    await db.commit()
    return {"ok": True, "challenge_key": row.challenge_key, "status": row.status}


@router.get("/auth/challenges/{challenge_key}")
async def get_auth_challenge(
    challenge_key: str, db: AsyncSession = Depends(get_db)
) -> dict:
    _ensure_enabled()
    row = (
        await db.execute(
            select(AutomationAuthChallenge).where(
                AutomationAuthChallenge.challenge_key == challenge_key
            )
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="challenge_not_found")
    return {
        "challenge_key": row.challenge_key,
        "challenge_id": row.id,
        "status": row.status,
        "challenge_type": row.challenge_type,
        "channel": row.channel,
        "prompt": row.prompt,
        "carrier_id": row.carrier_id,
        "session_id": row.session_id,
        "resolved_at": row.resolved_at,
        "metadata_json": row.metadata_json
        if isinstance(row.metadata_json, dict)
        else {},
    }


@router.post("/navigation/execute")
async def execute_navigation(
    payload: AutomationNavigationExecuteRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    _ensure_enabled()
    session = await db.get(AutomationWebSession, payload.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session_not_found")

    results = []
    failed_step = None
    for idx, step in enumerate(
        payload.steps[payload.start_step_index :], start=payload.start_step_index
    ):
        action = str(step.get("action") or "").strip().lower()
        try:
            result = await web_automation_service.perform_action(
                session,
                action=action,
                selector=str(step.get("selector") or step.get("field") or ""),
                text=str(step.get("text") or step.get("value") or ""),
                key=str(step.get("key") or ""),
                value=str(step.get("value") or ""),
                timeout_seconds=int(
                    step.get("timeout_seconds")
                    or settings.automation_default_timeout_seconds
                ),
            )
            results.append({"step_index": idx, "ok": True, "result": result})
        except Exception as exc:
            failed_step = idx
            results.append({"step_index": idx, "ok": False, "error": str(exc)})
            if payload.stop_on_failure:
                break

    await db.commit()
    return {
        "ok": failed_step is None,
        "session_id": session.id,
        "failed_step_index": failed_step,
        "results": results,
    }


@router.post("/files/detect")
async def detect_file(
    payload: AutomationFileDetectRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    _ensure_enabled()
    artifact = await web_automation_service.detect_file(
        db,
        session_id=payload.session_id,
        run_id=payload.run_id,
        carrier_id=payload.carrier_id,
        selector=payload.selector,
        expected_name_pattern=payload.expected_name_pattern,
        source_url=payload.source_url,
        metadata_json=payload.metadata_json,
    )
    await db.commit()
    return {
        "ok": True,
        "artifact_id": artifact.id,
        "status": artifact.status,
        "file_name": artifact.file_name,
    }


@router.post("/files/download")
async def download_file(
    payload: AutomationFileDownloadRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    _ensure_enabled()
    artifact = (
        await db.get(AutomationFileArtifact, payload.artifact_id)
        if payload.artifact_id
        else None
    )
    stored = await web_automation_service.store_download(
        db,
        artifact=artifact,
        run_id=payload.run_id,
        session_id=payload.session_id,
        carrier_id=payload.carrier_id,
        file_name=payload.file_name,
        source_url=payload.url,
        content_base64=payload.content_base64,
        metadata_json=payload.metadata_json,
    )
    await db.commit()
    return {
        "ok": True,
        "artifact_id": stored.id,
        "status": stored.status,
        "file_path": stored.file_path,
        "size_bytes": stored.size_bytes,
        "file_sha256": stored.file_sha256,
    }


@router.get("/files/{artifact_id}")
async def get_file_artifact(
    artifact_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
    _ensure_enabled()
    artifact = await db.get(AutomationFileArtifact, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="artifact_not_found")
    return {
        "artifact_id": artifact.id,
        "run_id": artifact.run_id,
        "session_id": artifact.session_id,
        "carrier_id": artifact.carrier_id,
        "status": artifact.status,
        "file_name": artifact.file_name,
        "file_path": artifact.file_path,
        "size_bytes": artifact.size_bytes,
        "file_sha256": artifact.file_sha256,
        "source_url": artifact.source_url,
        "metadata_json": artifact.metadata_json
        if isinstance(artifact.metadata_json, dict)
        else {},
    }


@router.get("/playbooks")
async def list_playbooks(db: AsyncSession = Depends(get_db)) -> list[dict]:
    _ensure_enabled()
    rows = (
        (
            await db.execute(
                select(AutomationPlaybook).order_by(AutomationPlaybook.carrier_id.asc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "playbook_id": row.id,
            "carrier_id": row.carrier_id,
            "enabled": row.enabled,
            "version": row.version,
            "login_method": row.login_method,
            "navigation_steps": row.navigation_steps_json,
            "report_location_logic": row.report_location_logic,
            "parsing_rules": row.parsing_rules_json,
            "recovery_rules": row.recovery_rules_json,
            "metadata_json": row.metadata_json,
            "updated_at": row.updated_at,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.post("/playbooks")
async def upsert_playbook(
    payload: AutomationPlaybookUpsertRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    _ensure_enabled()
    row = (
        await db.execute(
            select(AutomationPlaybook).where(
                AutomationPlaybook.carrier_id == payload.carrier_id.strip()
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = AutomationPlaybook(carrier_id=payload.carrier_id.strip())
        db.add(row)
        await db.flush()
    else:
        row.version = int(row.version) + 1

    row.enabled = payload.enabled
    row.login_method = payload.login_method
    row.navigation_steps_json = payload.navigation_steps
    row.report_location_logic = payload.report_location_logic
    row.parsing_rules_json = payload.parsing_rules
    row.recovery_rules_json = payload.recovery_rules
    row.metadata_json = payload.metadata_json

    await db.commit()
    return {
        "ok": True,
        "playbook_id": row.id,
        "carrier_id": row.carrier_id,
        "version": row.version,
    }


@router.put("/playbooks/{carrier_id}")
async def put_playbook(
    carrier_id: str,
    payload: AutomationPlaybookUpsertRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    if carrier_id.strip() != payload.carrier_id.strip():
        raise HTTPException(status_code=422, detail="carrier_id_mismatch")
    return await upsert_playbook(payload, db)


@router.post("/playbooks/{carrier_id}/refine")
async def refine_playbook(
    carrier_id: str,
    payload: AutomationPlaybookRefineRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    _ensure_enabled()
    row = (
        await db.execute(
            select(AutomationPlaybook).where(
                AutomationPlaybook.carrier_id == carrier_id.strip()
            )
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="playbook_not_found")

    patch = payload.patch if isinstance(payload.patch, dict) else {}
    if "navigation_steps" in patch and isinstance(patch["navigation_steps"], list):
        row.navigation_steps_json = patch["navigation_steps"]
    if "report_location_logic" in patch and isinstance(
        patch["report_location_logic"], dict
    ):
        row.report_location_logic = patch["report_location_logic"]
    if "parsing_rules" in patch and isinstance(patch["parsing_rules"], dict):
        row.parsing_rules_json = patch["parsing_rules"]
    if "recovery_rules" in patch and isinstance(patch["recovery_rules"], dict):
        row.recovery_rules_json = patch["recovery_rules"]
    row.version = int(row.version) + 1
    row.metadata_json = {
        **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
        "last_refined_by": payload.actor,
        "refine_reason": payload.reason,
    }

    await db.commit()
    return {"ok": True, "carrier_id": row.carrier_id, "version": row.version}


@router.post("/recovery/evaluate")
async def evaluate_recovery(
    payload: AutomationRecoveryEvaluateRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    _ensure_enabled()
    failure = payload.failure_type.strip().lower()
    retries = int(payload.retries_attempted)

    strategy = "retry"
    escalate = False
    if failure in {"mfa_expired", "auth_failed"}:
        strategy = "pause_for_human"
        escalate = True
    elif failure in {"selector_not_found", "navigation_timeout"} and retries >= 2:
        strategy = "playbook_refresh_then_retry"
    elif retries >= 4:
        strategy = "escalate"
        escalate = True

    return {
        "ok": True,
        "failure_type": failure,
        "retries_attempted": retries,
        "recommended_strategy": strategy,
        "escalate": escalate,
        "retry_allowed": not escalate
        or strategy in {"playbook_refresh_then_retry", "retry"},
    }


@router.post("/recovery/retry")
async def recovery_retry(
    payload: AutomationRecoveryRetryRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    _ensure_enabled()
    rows = (
        (
            await db.execute(
                select(AutomationCarrierRunStatus)
                .join(
                    AutomationExecutionRun,
                    AutomationExecutionRun.id == AutomationCarrierRunStatus.run_id,
                )
                .where(AutomationExecutionRun.id == payload.run_id)
                .where(AutomationCarrierRunStatus.carrier_id == payload.carrier_id)
                .limit(1)
            )
        )
        .scalars()
        .all()
    )

    row = rows[0] if rows else None
    if row:
        row.retries = int(row.retries) + 1
        row.status = "retrying"
        row.last_error = payload.reason
        row.metadata_json = {
            **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
            "retry_strategy": payload.strategy,
            **payload.metadata_json,
        }
        await db.commit()
        return {
            "ok": True,
            "run_id": row.run_id,
            "carrier_id": row.carrier_id,
            "retries": row.retries,
            "status": row.status,
        }

    return {
        "ok": True,
        "run_id": payload.run_id,
        "carrier_id": payload.carrier_id,
        "status": "retry_recorded_without_run_status",
    }


@router.post("/email/poll")
async def email_poll(
    payload: AutomationEmailPollRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    _ensure_enabled()
    messages = await web_automation_service.poll_email(
        db,
        source=payload.source,
        mailbox=payload.mailbox,
        limit=payload.limit,
        subject_contains=payload.subject_contains,
        sender_contains=payload.sender_contains,
        simulation_messages=payload.simulation_messages,
        metadata_json=payload.metadata_json,
    )
    await db.commit()
    return {
        "ok": True,
        "source": payload.source,
        "count": len(messages),
        "message_ids": [item.id for item in messages],
    }


@router.get("/email/messages")
async def list_email_messages(
    limit: int = 50, db: AsyncSession = Depends(get_db)
) -> list[dict]:
    _ensure_enabled()
    safe_limit = max(1, min(500, int(limit)))
    from core.models import AutomationEmailMessage

    rows = (
        (
            await db.execute(
                select(AutomationEmailMessage)
                .order_by(AutomationEmailMessage.id.desc())
                .limit(safe_limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "message_id": row.id,
            "source": row.source,
            "sender": row.sender,
            "recipient": row.recipient,
            "subject": row.subject,
            "received_at": row.received_at,
            "codes": row.extracted_codes_json
            if isinstance(row.extracted_codes_json, list)
            else [],
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.post("/email/extract-mfa")
async def extract_email_mfa(
    payload: AutomationEmailExtractMfaRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    _ensure_enabled()
    result = await web_automation_service.extract_mfa(
        db,
        lookback_minutes=payload.lookback_minutes,
        sender_contains=payload.sender_contains,
        subject_contains=payload.subject_contains,
    )
    await db.commit()

    if result.get("ok") and payload.challenge_key.strip():
        challenge = (
            await db.execute(
                select(AutomationAuthChallenge).where(
                    AutomationAuthChallenge.challenge_key
                    == payload.challenge_key.strip()
                )
            )
        ).scalar_one_or_none()
        if challenge:
            challenge.status = "code_detected"
            challenge.metadata_json = {
                **(
                    challenge.metadata_json
                    if isinstance(challenge.metadata_json, dict)
                    else {}
                ),
                "latest_detected_code": result.get("latest_code", ""),
            }
            await db.commit()

    return result


@router.post("/calendar/google/auth-url")
async def google_calendar_auth_url(
    payload: AutomationCalendarGoogleAuthUrlRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    _ensure_enabled()
    try:
        url = web_automation_service.build_google_calendar_auth_url(
            state=payload.state,
            scopes=payload.scopes,
            prompt=payload.prompt,
            access_type=payload.access_type,
            include_granted_scopes=payload.include_granted_scopes,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await write_journal(
        db,
        actor="automation",
        action="calendar_google_auth_url_generated",
        target_type="automation_calendar",
        target_id="google",
        summary="Generated Google Calendar OAuth authorization URL",
        metadata_json={
            "has_state": bool(payload.state.strip()),
            "scope_count": len(payload.scopes),
            "access_type": payload.access_type,
        },
    )
    await db.commit()
    return {"ok": True, "provider": "google", "auth_url": url}


@router.post("/calendar/google/exchange-code")
async def google_calendar_exchange_code(
    payload: AutomationCalendarGoogleExchangeCodeRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    _ensure_enabled()
    try:
        token_payload = await web_automation_service.exchange_google_calendar_code(
            code=payload.code,
            redirect_uri=payload.redirect_uri,
            code_verifier=payload.code_verifier,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await write_journal(
        db,
        actor="automation",
        action="calendar_google_code_exchanged",
        target_type="automation_calendar",
        target_id="google",
        summary="Exchanged Google Calendar OAuth code for token payload",
        metadata_json={
            "has_access_token": bool(str(token_payload.get("access_token") or "")),
            "has_refresh_token": bool(str(token_payload.get("refresh_token") or "")),
            "scope": str(token_payload.get("scope") or ""),
            "token_type": str(token_payload.get("token_type") or ""),
        },
    )
    await db.commit()
    return {
        "ok": True,
        "provider": "google",
        "token_payload": token_payload,
    }


@router.post("/calendar/reminders")
async def create_calendar_reminder(
    payload: AutomationCalendarReminderCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    _ensure_enabled()
    try:
        result = await web_automation_service.create_calendar_reminder(
            source=payload.source,
            title=payload.title,
            description=payload.description,
            start_at=payload.start_at,
            end_at=payload.end_at,
            timezone_name=payload.timezone or settings.automation_default_timezone,
            calendar_id=payload.calendar_id,
            reminder_minutes=payload.reminder_minutes,
            attendees=payload.attendees,
            access_token=payload.access_token,
            refresh_token=payload.refresh_token,
            metadata_json=payload.metadata_json,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await write_journal(
        db,
        actor="automation",
        action="calendar_reminder_created",
        target_type="automation_calendar",
        target_id=str(result.get("event_id") or "unknown"),
        summary=f"Created {result.get('provider', payload.source)} calendar reminder",
        metadata_json={
            "provider": result.get("provider", payload.source),
            "calendar_id": result.get("calendar_id", payload.calendar_id),
            "title": payload.title,
            "start_at": payload.start_at.isoformat(),
        },
    )
    await db.commit()
    return result


@router.post("/reconciliation/evaluate")
async def evaluate_reconciliation(
    payload: AutomationReconciliationEvaluateRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    _ensure_enabled()
    current = payload.current_totals if isinstance(payload.current_totals, dict) else {}
    previous = (
        payload.previous_totals if isinstance(payload.previous_totals, dict) else {}
    )

    anomalies = []
    for key, value in current.items():
        try:
            cur = float(value)
            prev = float(previous.get(key, 0.0))
        except Exception:
            continue
        if prev == 0.0:
            continue
        pct = abs((cur - prev) / prev) * 100.0
        if pct >= payload.anomaly_threshold_pct:
            anomalies.append(
                {
                    "metric": key,
                    "current": cur,
                    "previous": prev,
                    "delta_pct": round(pct, 2),
                }
            )

    expected = {str(x).strip() for x in payload.expected_carriers if str(x).strip()}
    present = {str(x).strip() for x in payload.present_carriers if str(x).strip()}
    missing = sorted(expected - present)

    return {
        "ok": True,
        "carrier_id": payload.carrier_id,
        "anomalies": anomalies,
        "missing_carriers": missing,
        "anomaly_count": len(anomalies),
        "missing_carrier_count": len(missing),
    }


@router.post("/runs")
async def create_run(
    payload: AutomationRunCreateRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    _ensure_enabled()
    run_key = payload.run_key.strip() or f"run-{uuid.uuid4().hex[:16]}"
    run = AutomationExecutionRun(
        run_key=run_key,
        status="pending",
        triggered_by=payload.triggered_by,
        started_at=datetime.now(timezone.utc),
        summary_json={},
        metadata_json=payload.metadata_json,
    )
    db.add(run)
    await db.flush()

    for carrier in payload.carriers:
        name = str(carrier).strip()
        if not name:
            continue
        db.add(
            AutomationCarrierRunStatus(
                run_id=run.id,
                carrier_id=name,
                status="pending",
                retries=0,
                requires_human_action=False,
                last_error="",
                last_step_index=-1,
                metadata_json={},
            )
        )

    await db.commit()
    return {"ok": True, "run_id": run.id, "run_key": run.run_key, "status": run.status}


@router.post("/runs/{run_id}/carriers")
async def update_run_carrier_status(
    run_id: int,
    payload: AutomationRunCarrierStatusUpdateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    _ensure_enabled()
    run = await db.get(AutomationExecutionRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run_not_found")

    row = (
        await db.execute(
            select(AutomationCarrierRunStatus)
            .where(AutomationCarrierRunStatus.run_id == run_id)
            .where(AutomationCarrierRunStatus.carrier_id == payload.carrier_id.strip())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        row = AutomationCarrierRunStatus(
            run_id=run_id, carrier_id=payload.carrier_id.strip()
        )
        db.add(row)
        await db.flush()

    row.status = payload.status
    row.retries = payload.retries
    row.requires_human_action = payload.requires_human_action
    row.last_error = payload.last_error
    row.last_step_index = payload.last_step_index
    row.metadata_json = payload.metadata_json

    if payload.status in {"success", "failed", "blocked"}:
        siblings = (
            (
                await db.execute(
                    select(AutomationCarrierRunStatus).where(
                        AutomationCarrierRunStatus.run_id == run_id
                    )
                )
            )
            .scalars()
            .all()
        )
        states = {s.status for s in siblings}
        if states and states.issubset({"success"}):
            run.status = "success"
            run.finished_at = datetime.now(timezone.utc)
        elif "failed" in states or "blocked" in states:
            run.status = "degraded"

    await db.commit()
    return {
        "ok": True,
        "run_id": run.id,
        "carrier_id": row.carrier_id,
        "status": row.status,
    }


@router.get("/runs")
async def list_runs(limit: int = 50, db: AsyncSession = Depends(get_db)) -> list[dict]:
    _ensure_enabled()
    safe_limit = max(1, min(500, int(limit)))
    rows = (
        (
            await db.execute(
                select(AutomationExecutionRun)
                .order_by(AutomationExecutionRun.id.desc())
                .limit(safe_limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "run_id": row.id,
            "run_key": row.run_key,
            "status": row.status,
            "triggered_by": row.triggered_by,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.get("/runs/{run_id}")
async def get_run(run_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    _ensure_enabled()
    row = await db.get(AutomationExecutionRun, run_id)
    if not row:
        raise HTTPException(status_code=404, detail="run_not_found")
    return {
        "run_id": row.id,
        "run_key": row.run_key,
        "status": row.status,
        "triggered_by": row.triggered_by,
        "summary_json": row.summary_json if isinstance(row.summary_json, dict) else {},
        "metadata_json": row.metadata_json
        if isinstance(row.metadata_json, dict)
        else {},
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "created_at": row.created_at,
    }


@router.get("/runs/{run_id}/carriers")
async def get_run_carriers(
    run_id: int, db: AsyncSession = Depends(get_db)
) -> list[dict]:
    _ensure_enabled()
    rows = (
        (
            await db.execute(
                select(AutomationCarrierRunStatus)
                .where(AutomationCarrierRunStatus.run_id == run_id)
                .order_by(AutomationCarrierRunStatus.carrier_id.asc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "carrier_status_id": row.id,
            "run_id": row.run_id,
            "carrier_id": row.carrier_id,
            "status": row.status,
            "retries": row.retries,
            "requires_human_action": row.requires_human_action,
            "last_error": row.last_error,
            "last_step_index": row.last_step_index,
            "metadata_json": row.metadata_json
            if isinstance(row.metadata_json, dict)
            else {},
            "updated_at": row.updated_at,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.get("/status/monitor")
async def get_execution_status_monitor(db: AsyncSession = Depends(get_db)) -> dict:
    _ensure_enabled()
    runs = (
        (
            await db.execute(
                select(AutomationExecutionRun)
                .order_by(AutomationExecutionRun.id.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    carrier_rows = (
        (
            await db.execute(
                select(AutomationCarrierRunStatus)
                .order_by(AutomationCarrierRunStatus.updated_at.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )

    counters = {
        "pending": 0,
        "running": 0,
        "success": 0,
        "failed": 0,
        "blocked": 0,
        "degraded": 0,
    }
    for row in carrier_rows:
        key = row.status if row.status in counters else "pending"
        counters[key] += 1

    human_actions = [
        {
            "run_id": row.run_id,
            "carrier_id": row.carrier_id,
            "status": row.status,
            "last_error": row.last_error,
            "retries": row.retries,
        }
        for row in carrier_rows
        if row.requires_human_action
    ]

    return {
        "generated_at": datetime.now(timezone.utc),
        "run_count": len(runs),
        "carrier_status_counts": counters,
        "recent_runs": [
            {
                "run_id": run.id,
                "run_key": run.run_key,
                "status": run.status,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
            }
            for run in runs
        ],
        "required_human_actions": human_actions[:100],
    }
