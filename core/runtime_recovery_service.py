from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class RuntimeRecoveryService:
    def __init__(
        self,
        state_dir: Path = Path("runtime/shared"),
        *,
        max_events: int = 200,
        default_window_seconds: int = 1800,
    ):
        self.state_dir = state_dir
        self.max_events = max(20, int(max_events or 200))
        self.default_window_seconds = max(60, int(default_window_seconds or 1800))
        self.events_file = self.state_dir / "mim_ui_runtime_recovery_events.jsonl"
        self.summary_file = self.state_dir / "mim_ui_runtime_recovery_summary.latest.json"

    def record_event(
        self,
        *,
        lane: str,
        event_type: str,
        detail: str = "",
        next_retry_at: str | None = None,
        metadata: dict[str, Any] | None = None,
        emitted_at: str | None = None,
    ) -> dict[str, Any]:
        normalized_lane = str(lane or "").strip().lower()
        if normalized_lane not in {"camera", "microphone"}:
            raise ValueError(f"Unsupported runtime recovery lane: {lane}")

        event = {
            "lane": normalized_lane,
            "event_type": str(event_type or "").strip().lower(),
            "detail": str(detail or "").strip(),
            "next_retry_at": str(next_retry_at or "").strip() or None,
            "metadata": metadata if isinstance(metadata, dict) else {},
            "emitted_at": str(emitted_at or "").strip() or _utcnow_iso(),
        }

        events = self._load_events()
        events.append(event)
        events = events[-self.max_events :]
        self._write_events(events)

        summary = self._build_summary(events, window_seconds=self.default_window_seconds)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.summary_file.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        return event

    def get_summary(self, *, window_seconds: int | None = None) -> dict[str, Any]:
        return self._build_summary(
            self._load_events(),
            window_seconds=window_seconds or self.default_window_seconds,
        )

    def _load_events(self) -> list[dict[str, Any]]:
        if not self.events_file.exists():
            return []
        events: list[dict[str, Any]] = []
        try:
            for raw_line in self.events_file.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if isinstance(item, dict):
                    events.append(item)
        except Exception:
            return []
        return events[-self.max_events :]

    def _write_events(self, events: list[dict[str, Any]]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = "\n".join(json.dumps(item) for item in events) + ("\n" if events else "")
        self.events_file.write_text(payload, encoding="utf-8")

    @staticmethod
    def _event_timestamp(item: dict[str, Any], now: datetime) -> datetime:
        return _parse_iso(item.get("emitted_at")) or now

    @staticmethod
    def _metadata(item: dict[str, Any]) -> dict[str, Any]:
        metadata = item.get("metadata")
        return metadata if isinstance(metadata, dict) else {}

    def _latest_metadata_value(
        self,
        lane_events: list[dict[str, Any]],
        key: str,
        *,
        iso_value: bool = False,
    ) -> str | bool | None:
        for item in reversed(lane_events):
            metadata = self._metadata(item)
            value = metadata.get(key)
            if iso_value:
                parsed = _parse_iso(value)
                if parsed is not None:
                    return parsed.isoformat().replace("+00:00", "Z")
                continue
            if isinstance(value, bool):
                return value
            text = str(value or "").strip()
            if text:
                return text
        return None

    def _build_summary(self, events: list[dict[str, Any]], *, window_seconds: int) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        cutoff_ts = now.timestamp() - max(60, int(window_seconds or self.default_window_seconds))
        recent_events = [
            item
            for item in events
            if self._event_timestamp(item, now).timestamp() >= cutoff_ts
        ]
        recent_events = sorted(
            recent_events,
            key=lambda item: self._event_timestamp(item, now).timestamp(),
        )

        lanes: dict[str, dict[str, Any]] = {}
        summary_parts: list[str] = []
        for lane in ("camera", "microphone"):
            lane_events = [item for item in recent_events if str(item.get("lane") or "").strip().lower() == lane]
            stale_count = sum(1 for item in lane_events if item.get("event_type") == "stale_detected")
            attempt_count = sum(1 for item in lane_events if item.get("event_type") == "recovery_attempted")
            success_count = sum(1 for item in lane_events if item.get("event_type") == "recovery_succeeded")
            failure_count = sum(1 for item in lane_events if item.get("event_type") == "recovery_failed")
            attempt_events = [item for item in lane_events if item.get("event_type") == "recovery_attempted"]
            latest_attempt = attempt_events[-1] if attempt_events else {}
            latest_attempt_ts = self._event_timestamp(latest_attempt, now) if latest_attempt else None
            cycle_events = (
                [item for item in lane_events if self._event_timestamp(item, now) >= latest_attempt_ts]
                if latest_attempt_ts is not None
                else lane_events
            )
            healthy_events = [
                item
                for item in lane_events
                if item.get("event_type") in {"recovery_succeeded", "healthy_observed"}
            ]
            last_event = lane_events[-1] if lane_events else {}
            next_retry_at = str(last_event.get("next_retry_at") or "").strip() or None
            next_retry_dt = _parse_iso(next_retry_at)
            cooldown_active = bool(next_retry_dt and next_retry_dt > now)
            cooldown_remaining_seconds = int(max(0.0, (next_retry_dt - now).total_seconds())) if next_retry_dt else 0
            unstable = attempt_count >= 3 or failure_count >= 2 or (stale_count >= 2 and success_count == 0)
            recovering = attempt_count > success_count + failure_count
            last_recovery_attempt_at = str(latest_attempt.get("emitted_at") or "").strip() or None
            first_healthy_at = None
            if healthy_events:
                first_healthy = healthy_events[-1]
                first_healthy_at = (
                    self._latest_metadata_value([first_healthy], "first_healthy_at", iso_value=True)
                    or str(first_healthy.get("emitted_at") or "").strip()
                    or None
                )
            last_healthy_frame_at = self._latest_metadata_value(lane_events, "last_healthy_frame_at", iso_value=True)
            last_frame_seen_at = self._latest_metadata_value(cycle_events, "last_frame_seen_at", iso_value=True)
            watcher_running_value = self._latest_metadata_value(cycle_events, "watcher_running")
            watcher_running = bool(watcher_running_value) if isinstance(watcher_running_value, bool) else None
            retry_reason = self._latest_metadata_value(cycle_events, "retry_reason")
            retry_reason_detail = self._latest_metadata_value(cycle_events, "retry_reason_detail")
            retry_reason_after_cooldown = retry_reason if attempt_count >= 2 else None
            health_report_disagreement = bool(
                self._latest_metadata_value(cycle_events, "health_report_disagreement")
            )
            bounded_retry_evidence = bool(attempt_count >= 1 and len(healthy_events) >= 1 and not unstable)

            if unstable:
                status = "unstable"
            elif recovering or cooldown_active:
                status = "recovering"
            elif failure_count > 0:
                status = "attention"
            elif success_count > 0:
                status = "healthy"
            else:
                status = "idle"

            if lane_events:
                summary = (
                    f"{lane.capitalize()} recovery attempts={attempt_count}, "
                    f"successes={success_count}, failures={failure_count}."
                )
                if cooldown_active:
                    summary = f"{summary} Retry eligible in {cooldown_remaining_seconds}s."
            else:
                summary = f"No recent {lane} recovery activity."

            lanes[lane] = {
                "lane": lane,
                "status": status,
                "summary": summary,
                "recent_event_count": len(lane_events),
                "stale_detected_count": stale_count,
                "recovery_attempt_count": attempt_count,
                "success_count": success_count,
                "failure_count": failure_count,
                "cooldown_active": cooldown_active,
                "cooldown_remaining_seconds": cooldown_remaining_seconds,
                "next_retry_at": next_retry_at,
                "last_recovery_attempt_at": last_recovery_attempt_at,
                "first_healthy_at": first_healthy_at,
                "last_healthy_frame_at": last_healthy_frame_at,
                "last_frame_seen_at": last_frame_seen_at,
                "watcher_running": watcher_running,
                "retry_reason": retry_reason,
                "retry_reason_detail": retry_reason_detail,
                "retry_reason_after_cooldown": retry_reason_after_cooldown,
                "health_report_disagreement": health_report_disagreement,
                "bounded_retry_evidence": bounded_retry_evidence,
                "last_event": last_event,
                "unstable": unstable,
                "recent_events": lane_events[-8:],
            }
            if lane_events:
                summary_parts.append(summary)

        overall_status = "healthy"
        if any(bool(item.get("unstable")) for item in lanes.values()):
            overall_status = "degraded"
        elif any(str(item.get("status") or "") in {"recovering", "attention"} for item in lanes.values()):
            overall_status = "suboptimal"

        return {
            "generated_at": _utcnow_iso(),
            "window_seconds": max(60, int(window_seconds or self.default_window_seconds)),
            "status": overall_status,
            "summary": " ".join(summary_parts).strip() or "No recent runtime recovery activity.",
            "lanes": lanes,
            "recent_events": recent_events[-20:],
        }