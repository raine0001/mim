# MIM Object Inquiry Simulation Training

Date: 2026-03-21
Scope: Proactive camera-object inquiry, semantic learning, continuity, and mixed conversation behavior.

## Purpose

This pack extends the existing conversation evaluation work with camera-aware simulations that exercise:

1. Novel object inquiry and staged semantic learning.
2. Owner, home-zone, and secondary semantic enrichment.
3. Familiar-object suppression after learning.
4. Uncertain and missing object continuity prompts.
5. Mixed conversation behavior where normal chat and object inquiry must coexist.

## Artifacts

1. `conversation_scenarios/object_inquiry_proactive_pack.json`
2. `scripts/run_mim_object_inquiry_simulation_sweep.py`
3. `tests/test_object_inquiry_simulation_sweep.py`

Related general-conversation extension:

1. `conversation_scenarios/general_conversation_extended_pack.json`

## Recommended Runs

General conversation extension pack:

```bash
MIM_TEST_BASE_URL=http://127.0.0.1:18001 \
/home/testpilot/mim/.venv/bin/python conversation_eval_runner.py \
  --scenarios conversation_scenarios/general_conversation_extended_pack.json \
  --profiles conversation_profiles.json \
  --turn-delay-ms 0 \
  --output runtime/reports/general_conversation_extended_pack.report.json
```

Camera-aware object inquiry pack:

```bash
/home/testpilot/mim/.venv/bin/python scripts/run_mim_object_inquiry_simulation_sweep.py \
  --base-url http://127.0.0.1:18001 \
  --scenarios conversation_scenarios/object_inquiry_proactive_pack.json \
  --output runtime/reports/object_inquiry_proactive_sweep.json
```

## Coverage Themes

The proactive inquiry pack covers these simulation themes:

1. Proactive object-learning ladder.
2. Inquiry interruption and resumption.
3. Wrong-answer correction loops.
4. Familiar-object suppression.
5. Missing-object continuity.
6. Uncertain-object verification.
7. Social conversation blended with inquiry.
8. User deferral.
9. Low-information replies.
10. Noisy reply extraction.
11. Long-horizon semantic correction.
12. Scene-priority arbitration.
13. Multi-object scene grounding.
14. Preference shaping during inquiry.
15. Mixed technical-thread and camera inquiry behavior.

## Notes

1. The general extension pack uses the existing `conversation_eval_runner.py` and now carries a synthetic `conversation_session_id` per scenario/profile run so multi-turn context is evaluated more realistically.
2. The object inquiry pack uses live camera-event injection and object-library verification to validate durable learning, not just prompt wording.