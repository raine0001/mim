from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import SpeechOutputAction, WorkspacePerceptionSource

CAMERA_STALE_SECONDS = 30.0
MICROPHONE_STALE_SECONDS = 30.0
SPEECH_ACTIVE_SECONDS = 8.0
IDLE_RESET_SECONDS = 300.0
MIM_UI_CAMERA_DEVICE_ID = "mim-ui-camera"
MIM_UI_CAMERA_SESSION_ID = "mim-ui-session"


def _age_seconds(now: datetime, ts: datetime | None) -> float | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (now - ts.astimezone(timezone.utc)).total_seconds())


def _isoformat_or_none(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _compact_sentence(raw: str, *, max_len: int = 180) -> str:
    text = " ".join(str(raw or "").split())
    if len(text) <= max_len:
        return text
    return f"{text[: max_len - 3].rstrip()}..."


def _select_preferred_perception_row(
    *,
    rows: list[WorkspacePerceptionSource],
    preferred_device_id: str = "",
    preferred_session_id: str = "",
) -> WorkspacePerceptionSource | None:
    if not rows:
        return None

    normalized_device_id = str(preferred_device_id or "").strip()
    if normalized_device_id:
        for row in rows:
            if str(row.device_id or "").strip() == normalized_device_id:
                return row

    normalized_session_id = str(preferred_session_id or "").strip()
    if normalized_session_id:
        for row in rows:
            if str(row.session_id or "").strip() == normalized_session_id:
                return row

    return rows[0]


def assess_perception_lane(
    *,
    lane: str,
    row: WorkspacePerceptionSource | None,
    now: datetime,
    stale_seconds: float,
    idle_reset_seconds: float = IDLE_RESET_SECONDS,
) -> dict[str, Any]:
    age_seconds = _age_seconds(now, row.last_seen_at if row else None)
    source_status = str(row.status or "").strip() if row else ""
    source_health = str(row.health_status or "").strip() if row else ""
    metadata = row.metadata_json if row and isinstance(row.metadata_json, dict) else {}
    last_adapter_status = str(metadata.get("last_adapter_status") or "").strip()
    device_id = str(row.device_id or "").strip() if row else ""

    if row is None or age_seconds is None:
        return {
            "ok": True,
            "status": "idle",
            "summary": f"{lane.capitalize()} lane is waiting for its first live signal.",
            "diagnostic_code": "",
            "age_seconds": age_seconds,
            "stale_threshold_seconds": stale_seconds,
            "idle_reset_seconds": idle_reset_seconds,
            "source_health": source_health,
            "source_status": source_status,
            "last_adapter_status": last_adapter_status,
            "device_id": device_id,
        }

    if source_status and source_status != "active":
        return {
            "ok": True,
            "status": "idle",
            "summary": f"{lane.capitalize()} lane is not marked active.",
            "diagnostic_code": "",
            "age_seconds": age_seconds,
            "stale_threshold_seconds": stale_seconds,
            "idle_reset_seconds": idle_reset_seconds,
            "source_health": source_health,
            "source_status": source_status,
            "last_adapter_status": last_adapter_status,
            "device_id": device_id,
        }

    if age_seconds <= stale_seconds:
        return {
            "ok": True,
            "status": "healthy",
            "summary": f"{lane.capitalize()} lane is fresh.",
            "diagnostic_code": "",
            "age_seconds": age_seconds,
            "stale_threshold_seconds": stale_seconds,
            "idle_reset_seconds": idle_reset_seconds,
            "source_health": source_health,
            "source_status": source_status,
            "last_adapter_status": last_adapter_status,
            "device_id": device_id,
        }

    if age_seconds > idle_reset_seconds:
        return {
            "ok": True,
            "status": "idle",
            "summary": f"{lane.capitalize()} lane is idle with no recent live signal.",
            "diagnostic_code": "",
            "age_seconds": age_seconds,
            "stale_threshold_seconds": stale_seconds,
            "idle_reset_seconds": idle_reset_seconds,
            "source_health": source_health,
            "source_status": source_status,
            "last_adapter_status": last_adapter_status,
            "device_id": device_id,
        }

    detail = f"No {lane} event for {int(round(age_seconds))} seconds"
    if last_adapter_status:
        detail = f"{detail}; last adapter status was {last_adapter_status.replace('_', ' ')}"
    return {
        "ok": False,
        "status": "stale",
        "summary": f"{lane.capitalize()} lane is stale.",
        "diagnostic_code": f"{lane}_signal_stale",
        "age_seconds": age_seconds,
        "stale_threshold_seconds": stale_seconds,
        "idle_reset_seconds": idle_reset_seconds,
        "source_health": source_health,
        "source_status": source_status,
        "last_adapter_status": last_adapter_status,
        "device_id": device_id,
        "detail": detail,
    }


def assess_speech_lane(
    *,
    row: SpeechOutputAction | None,
    now: datetime,
    active_seconds: float = SPEECH_ACTIVE_SECONDS,
) -> dict[str, Any]:
    age_seconds = _age_seconds(now, row.created_at if row else None)
    delivery_status = str(row.delivery_status or "").strip() if row else ""

    if row is None or age_seconds is None:
        return {
            "ok": True,
            "status": "idle",
            "summary": "Speech output lane is idle.",
            "diagnostic_code": "",
            "age_seconds": age_seconds,
            "active_threshold_seconds": active_seconds,
            "delivery_status": delivery_status,
        }

    if delivery_status == "blocked":
        return {
            "ok": True,
            "status": "blocked",
            "summary": "Speech output was blocked by policy, not stuck.",
            "diagnostic_code": "",
            "age_seconds": age_seconds,
            "active_threshold_seconds": active_seconds,
            "delivery_status": delivery_status,
        }

    if delivery_status == "suppressed":
        return {
            "ok": True,
            "status": "suppressed",
            "summary": "Speech output was intentionally suppressed.",
            "diagnostic_code": "",
            "age_seconds": age_seconds,
            "active_threshold_seconds": active_seconds,
            "delivery_status": delivery_status,
        }

    if age_seconds <= active_seconds:
        return {
            "ok": True,
            "status": "healthy",
            "summary": "Speech output lane is active.",
            "diagnostic_code": "",
            "age_seconds": age_seconds,
            "active_threshold_seconds": active_seconds,
            "delivery_status": delivery_status,
        }

    return {
        "ok": True,
        "status": "idle",
        "summary": "Speech output lane is idle between utterances.",
        "diagnostic_code": "",
        "age_seconds": age_seconds,
        "active_threshold_seconds": active_seconds,
        "delivery_status": delivery_status,
    }


def build_mim_ui_health_snapshot_from_rows(
    *,
    now: datetime,
    speech_row: SpeechOutputAction | None,
    camera_row: WorkspacePerceptionSource | None,
    mic_row: WorkspacePerceptionSource | None,
    db_ok: bool = True,
) -> dict[str, Any]:
    camera = assess_perception_lane(
        lane="camera",
        row=camera_row,
        now=now,
        stale_seconds=CAMERA_STALE_SECONDS,
    )
    microphone = assess_perception_lane(
        lane="microphone",
        row=mic_row,
        now=now,
        stale_seconds=MICROPHONE_STALE_SECONDS,
    )
    speech = assess_speech_lane(
        row=speech_row,
        now=now,
        active_seconds=SPEECH_ACTIVE_SECONDS,
    )

    diagnostics = []
    for lane_name, lane_state in (
        ("camera", camera),
        ("microphone", microphone),
    ):
        diagnostic_code = str(lane_state.get("diagnostic_code") or "").strip()
        if diagnostic_code:
            diagnostics.append(
                {
                    "code": diagnostic_code,
                    "severity": "medium",
                    "lane": lane_name,
                    "summary": str(lane_state.get("summary") or "").strip(),
                    "detail": str(lane_state.get("detail") or lane_state.get("summary") or "").strip(),
                }
            )

    overall_ok = bool(db_ok and camera.get("ok") and microphone.get("ok") and speech.get("ok"))
    overall_status = "healthy" if overall_ok else "degraded"

    return {
        "generated_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": overall_status,
        "ok": overall_ok,
        "summary": summarize_runtime_health(
            {
                "status": overall_status,
                "diagnostics": diagnostics,
                "checks": {
                    "camera": camera,
                    "microphone": microphone,
                    "speech_output": speech,
                },
            }
        ),
        "diagnostics": diagnostics,
        "checks": {
            "backend": {"ok": True, "status": "healthy"},
            "database": {
                "ok": db_ok,
                "status": "healthy" if db_ok else "error",
            },
            "camera": camera,
            "microphone": microphone,
            "speech_output": speech,
        },
        "latest": {
            "camera": {
                "source_id": int(camera_row.id) if camera_row else None,
                "device_id": str(camera_row.device_id or "") if camera_row else "",
                "last_seen_at": _isoformat_or_none(camera_row.last_seen_at) if camera_row else None,
            },
            "microphone": {
                "source_id": int(mic_row.id) if mic_row else None,
                "device_id": str(mic_row.device_id or "") if mic_row else "",
                "last_seen_at": _isoformat_or_none(mic_row.last_seen_at) if mic_row else None,
            },
            "speech_output": {
                "action_id": int(speech_row.id) if speech_row else None,
                "created_at": _isoformat_or_none(speech_row.created_at) if speech_row else None,
            },
        },
    }


async def build_mim_ui_health_snapshot(
    *,
    db: AsyncSession,
    now: datetime | None = None,
) -> dict[str, Any]:
    reference_time = now or datetime.now(timezone.utc)
    db_ok = True

    try:
        speech_row = (
            (
                await db.execute(
                    select(SpeechOutputAction)
                    .order_by(SpeechOutputAction.id.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        camera_rows = (
            (
                await db.execute(
                    select(WorkspacePerceptionSource)
                    .where(WorkspacePerceptionSource.source_type == "camera")
                    .order_by(
                        WorkspacePerceptionSource.last_seen_at.desc().nullslast(),
                        WorkspacePerceptionSource.id.desc(),
                    )
                    .limit(12)
                )
            )
            .scalars()
            .all()
        )
        camera_row = _select_preferred_perception_row(
            rows=camera_rows,
            preferred_device_id=MIM_UI_CAMERA_DEVICE_ID,
            preferred_session_id=MIM_UI_CAMERA_SESSION_ID,
        )
        mic_row = (
            (
                await db.execute(
                    select(WorkspacePerceptionSource)
                    .where(WorkspacePerceptionSource.source_type == "microphone")
                    .order_by(
                        WorkspacePerceptionSource.last_seen_at.desc().nullslast(),
                        WorkspacePerceptionSource.id.desc(),
                    )
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
    except Exception:
        db_ok = False
        speech_row = None
        camera_row = None
        mic_row = None

    return build_mim_ui_health_snapshot_from_rows(
        now=reference_time,
        speech_row=speech_row,
        camera_row=camera_row,
        mic_row=mic_row,
        db_ok=db_ok,
    )


def summarize_runtime_health(snapshot: dict[str, Any]) -> str:
    diagnostics = snapshot.get("diagnostics", []) if isinstance(snapshot, dict) else []
    if isinstance(diagnostics, list) and diagnostics:
        parts = []
        for item in diagnostics[:3]:
            if not isinstance(item, dict):
                continue
            lane = str(item.get("lane") or "runtime").strip().replace("_", " ")
            detail = str(item.get("detail") or item.get("summary") or "").strip()
            if lane and detail:
                parts.append(f"{lane.capitalize()}: {detail}")
        if parts:
            return _compact_sentence(". ".join(parts), max_len=220)

    checks = snapshot.get("checks", {}) if isinstance(snapshot, dict) else {}
    if not isinstance(checks, dict):
        return ""
    idle_parts = []
    for lane in ("camera", "microphone"):
        lane_state = checks.get(lane, {}) if isinstance(checks.get(lane, {}), dict) else {}
        if str(lane_state.get("status") or "").strip() == "idle":
            idle_parts.append(f"{lane.replace('_', ' ')} idle")
    if idle_parts:
        return _compact_sentence(
            "Runtime health is stable; " + ", ".join(idle_parts) + ".",
            max_len=220,
        )
    return "Runtime health is stable."


def merge_health_status(base_status: str, ui_status: str) -> str:
    order = {
        "healthy": 0,
        "suboptimal": 1,
        "degraded": 2,
        "critical": 3,
    }
    reverse = {value: key for key, value in order.items()}
    base_rank = order.get(str(base_status or "healthy"), 0)
    ui_rank = order.get(str(ui_status or "healthy"), 0)
    return reverse[max(base_rank, ui_rank)]