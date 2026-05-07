# Objective 61: Live Perception Adapters

Objective 61 connects live perception sources into MIM's gateway so camera and microphone streams produce policy-governed normalized events with source-aware health visibility.

## Scope Implemented

- Live camera adapter pipeline:
  - `POST /gateway/perception/camera/events`
  - camera observations are normalized into gateway vision events.
  - accepted camera observations are persisted to workspace observation memory.
  - first-version fields supported: object label, confidence, zone, timestamp, source device identity.
- Live microphone adapter pipeline:
  - `POST /gateway/perception/mic/events`
  - mic transcripts are normalized into gateway voice events and flow through existing voice policy reasoning.
  - first-version fields supported: transcript, confidence, timestamp, source device identity.
- Perception throttling and noise handling:
  - minimum interval throttling between accepted events.
  - duplicate event suppression using event fingerprint and duplicate window.
  - observation confidence floor.
  - low-confidence transcript discard with clarification-safe outcome.
- Source identity and shared status:
  - persistent source metadata includes `device_id`, `source_type`, `session_id`, and `is_remote`.
  - source-level health and counters track accepted/dropped/duplicate/low-confidence outcomes.
- Inspectability:
  - `GET /gateway/perception/sources`
  - `GET /gateway/perception/status`
  - exposes active adapters, camera source status, mic source status, last event timestamp, and adapter health counts.

## Why Objective 61 Matters

Objective 61 transitions perception from simulated adapter calls to live source-aware ingestion. It makes the system's seeing/hearing state explicit and debuggable while preserving policy safety and gateway contracts.
