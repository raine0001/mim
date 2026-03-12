# Objective 27 — Workspace Map and Relational Context

Date: 2026-03-10

## Goal

Add structured spatial context so workspace reasoning uses zone relationships and object-to-object relations, not only isolated object/zone memory.

## Implemented Scope

### Task A — Zone Map Model

Added persistent zone map entities:

- `workspace_zones`
- `workspace_zone_relations`

Seeded default map with zones:

- `front-left`, `front-center`, `front-right`
- `rear-left`, `rear-center`, `rear-right`

Stored relationships:

- `adjacent_to`
- `left_of`
- `right_of`
- `in_front_of`
- `behind`

### Task B — Object-to-Zone Relational Context

Added object relation persistence:

- `workspace_object_relations`

Scan integration updates relational context:

- objects observed in same zone -> `near`
- objects observed in different zones -> `far` / `inconsistent`
- movement updates location history and uncertainty
- missing objects include likely adjacent-zone movement hints

### Task C — Relational Query Surface

Added endpoints:

- `GET /workspace/map`
- `GET /workspace/map/zones`
- `GET /workspace/objects/{object_memory_id}/relations`
- existing `GET /workspace/objects?zone=...` and label filters remain available

### Task D — Spatial Routing Hints

Memory-informed routing now considers:

- zone hazard level (`unsafe_zone`)
- object spatial uncertainty (`object_uncertain_count`)
- recent movement signals (`moved_recent_count`)
- stale/missing object counts
- spatially stable strong identity (`object_recent_strong_count`)

Routing outcomes include:

- avoid unnecessary reconfirmation when identity+spatial context is strong
- require reconfirmation when spatial relations changed unexpectedly
- escalate to confirmation for unsafe zones

### Task E — Movement / Absence Reasoning

Implemented:

- missing from expected zone updates with confidence decay
- likely moved-to-adjacent-zone hints when available
- relation inconsistency tracking through object relation status
