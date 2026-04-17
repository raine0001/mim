from __future__ import annotations

from datetime import datetime, timezone


def coerce_utc(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def parse_payload_timestamp(raw: object) -> datetime | None:
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
    return coerce_utc(parsed)


def age_seconds(now: datetime, ts: datetime | None) -> float | None:
    anchor = coerce_utc(ts)
    if anchor is None:
        return None
    return max(0.0, (coerce_utc(now) - anchor).total_seconds())


def _payload_observation_rows(payload: dict) -> list[dict]:
    observations = payload.get("observations", []) if isinstance(payload, dict) else []
    if isinstance(observations, list) and observations:
        return [item for item in observations if isinstance(item, dict)]

    label = str(
        payload.get("object_label", "") if isinstance(payload, dict) else ""
    ).strip()
    if not label:
        return []
    return [
        {
            "object_label": label,
            "zone": str(
                payload.get("zone", "") if isinstance(payload, dict) else ""
            ).strip(),
            "confidence": payload.get("confidence", 0.0)
            if isinstance(payload, dict)
            else 0.0,
            "timestamp": payload.get("timestamp")
            if isinstance(payload, dict)
            else None,
        }
    ]


def collect_fresh_camera_observations(
    camera_rows: list[object],
    *,
    now: datetime,
    stale_seconds: float = 90.0,
) -> list[dict[str, object]]:
    latest_row_seen_at = max(
        [
            coerce_utc(getattr(row, "last_seen_at", None))
            for row in camera_rows
            if coerce_utc(getattr(row, "last_seen_at", None))
        ],
        default=None,
    )
    latest_row = next(
        (
            row
            for row in camera_rows
            if coerce_utc(getattr(row, "last_seen_at", None)) == latest_row_seen_at
        ),
        None,
    )
    preferred_session_id = str(getattr(latest_row, "session_id", "") or "").strip()

    fresh: list[dict[str, object]] = []
    seen: set[tuple[str, str, str, float]] = set()

    for row in camera_rows:
        row_seen_at = coerce_utc(getattr(row, "last_seen_at", None))
        row_session_id = str(getattr(row, "session_id", "") or "").strip()
        if preferred_session_id:
            if row_session_id != preferred_session_id:
                continue
        elif latest_row_seen_at and row_seen_at:
            if (latest_row_seen_at - row_seen_at).total_seconds() > 10.0:
                continue

        payload = (
            row.last_event_payload_json
            if isinstance(getattr(row, "last_event_payload_json", None), dict)
            else {}
        )
        for item in _payload_observation_rows(payload):
            label_raw = str(item.get("object_label", "")).strip()
            if not label_raw:
                continue
            zone = str(item.get("zone", "")).strip()
            try:
                confidence = float(item.get("confidence", 0.0) or 0.0)
            except Exception:
                confidence = 0.0
            timestamp = parse_payload_timestamp(item.get("timestamp"))
            anchor = (
                timestamp
                or parse_payload_timestamp(payload.get("timestamp"))
                or row_seen_at
            )
            age = age_seconds(now, anchor)
            if age is not None and age > max(1.0, float(stale_seconds)):
                continue
            dedupe_key = (
                str(getattr(row, "device_id", "") or "").strip(),
                label_raw.lower(),
                zone.lower(),
                round(confidence, 3),
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            fresh.append(
                {
                    "label_raw": label_raw,
                    "zone": zone,
                    "confidence": confidence,
                    "device_id": str(getattr(row, "device_id", "") or "").strip(),
                    "session_id": row_session_id,
                    "timestamp": anchor,
                    "source_id": int(getattr(row, "id", 0) or 0),
                }
            )

    fresh.sort(
        key=lambda item: (
            -float(item.get("confidence", 0.0) or 0.0),
            -(
                coerce_utc(item.get("timestamp")).timestamp()
                if coerce_utc(item.get("timestamp"))
                else 0.0
            ),
            str(item.get("device_id", "")),
            str(item.get("label_raw", "")).lower(),
        )
    )
    return fresh


def _join_phrases(parts: list[str]) -> str:
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return f"{', '.join(parts[:-1])}, and {parts[-1]}"


def summarize_camera_observations(
    observations: list[dict[str, object]],
    *,
    max_items: int = 3,
) -> dict[str, object]:
    if not observations:
        return {
            "summary": "",
            "primary_label": "",
            "primary_zone": "",
            "primary_confidence": 0.0,
            "source_count": 0,
            "observation_count": 0,
            "labels": [],
        }

    primary = observations[0]
    labels: list[str] = []
    phrases: list[str] = []
    seen_labels: set[str] = set()
    seen_phrase_keys: set[tuple[str, str]] = set()
    for item in observations:
        label_raw = str(item.get("label_raw", "")).strip()
        zone = str(item.get("zone", "")).strip()
        if not label_raw:
            continue
        if label_raw.lower() not in seen_labels:
            seen_labels.add(label_raw.lower())
            labels.append(label_raw)
        phrase_key = (label_raw.lower(), zone.lower())
        if phrase_key in seen_phrase_keys:
            continue
        seen_phrase_keys.add(phrase_key)
        phrases.append(f"{label_raw} in {zone}" if zone else label_raw)
        if len(phrases) >= max(1, int(max_items)):
            break

    source_count = len(
        {
            str(item.get("device_id", "")).strip()
            for item in observations
            if str(item.get("device_id", "")).strip()
        }
    )
    joined = _join_phrases(phrases)
    suffix = (
        f" across {source_count} camera feeds" if source_count > 1 else " on camera"
    )
    return {
        "summary": f"{joined}{suffix}" if joined else "",
        "primary_label": str(primary.get("label_raw", "")).strip(),
        "primary_zone": str(primary.get("zone", "")).strip(),
        "primary_confidence": float(primary.get("confidence", 0.0) or 0.0),
        "source_count": source_count,
        "observation_count": len(observations),
        "labels": labels,
    }
