# MIM Dialog Flow Test Plan

Date: 2026-03-17
Purpose: Verify natural conversation handling without repeated low-value loops.
Scope: MIM voice/mic behavior in normal operator conversation.
Extended training: `docs/mim-general-conversation-simulation-training.md`

## Preconditions

1. Run backend on patch test port:

```bash
/home/testpilot/mim/.venv/bin/python -m uvicorn core.app:app --host 127.0.0.1 --port 18001
```

2. Open MIM UI at `http://127.0.0.1:18001/mim` (browser or Electron shell).
2. Turn `Listening On` and confirm `/mim/ui/health` reports fresh mic activity.
3. Use a quiet room for baseline run, then repeat once with light ambient noise.

## Test Rules

1. Speak each line once, then wait up to 8 seconds.
2. If MIM asks for clarification, repeat the same line once only.
3. Mark a failure if MIM repeats substantially the same clarification more than once within 90 seconds.
4. Mark a failure if MIM gets stuck in a question loop and does not return to new user input.

## Flow A: General Conversation Warm-Up

User line 1: `Hello MIM.`
Expected:

- MIM gives one short greeting.
- MIM does not keep repeating greeting prompts.

User line 2: `How are you doing today?`
Expected:

- MIM gives a short status-style response.
- Response is not replaced by identity-only or low-confidence loop text.

User line 3: `Can we have a normal conversation?`
Expected:

- MIM acknowledges and stays conversational.
- No repeated startup identity prompt unless identity is genuinely unknown and required.

## Flow B: Weather/General Utility

User line 4: `How is the weather?`
Expected:

- MIM either answers with available context or states clearly that live weather data is unavailable.
- MIM asks at most one concise follow-up question.

User line 5: `Give me a simple weather summary.`
Expected:

- MIM responds once.
- No repeating the same question back-to-back.

## Flow C: TOD Status

User line 6: `How is TOD doing?`
Expected:

- MIM provides current TOD state summary if available.
- If unavailable, MIM states what is missing and what it can check next.

User line 7: `Is TOD healthy right now?`
Expected:

- MIM answers directly with health/alignment wording.
- No loop into unrelated curiosity prompts.

## Flow D: Work Focus and Upcoming Tasks

User line 8: `What are you working on right now?`
Expected:

- MIM summarizes active objective/task context in plain language.
- No repeated low-value clarification unless transcript confidence is truly low.

User line 9: `Any upcoming tasks we need to talk about?`
Expected:

- MIM lists upcoming items or says none are pending.
- MIM should not ask the same open question repeatedly.

User line 10: `What should we prioritize next?`
Expected:

- MIM proposes one to three next actions.
- MIM remains concise and non-repetitive.

## Flow E: Low-Value Noise Resilience

User line 11: `uh`
Expected:

- MIM may give one short clarification.
- Additional low-value inputs inside cooldown window should be mostly silent or non-repetitive.

User line 12: `you know`
Expected:

- MIM should not re-enter a repeating clarification loop.
- It should return control quickly to the next meaningful input.

User line 13: `How is TOD doing now?`
Expected:

- MIM resumes normal status conversation immediately.

## Pass/Fail Checklist

Pass when all are true:

1. No repeated clarification speech loop occurs during the full 13-line run.
2. MIM processes at least 10 of 13 lines with relevant, non-looping responses.
3. Low-value lines do not trigger repeated speech within the 90-second cooldown window.
4. TOD/status/task questions return focused answers instead of repeated prompts.

Fail if any are true:

1. Same or near-identical clarification is spoken repeatedly without new user intent.
2. Greeting/identity prompts continue after conversation has already moved on.
3. MIM ignores meaningful lines after low-value noise and remains in a loop.

## Quick Transcript Block (Operator Copy)

```text
Hello MIM.
How are you doing today?
Can we have a normal conversation?
How is the weather?
Give me a simple weather summary.
How is TOD doing?
Is TOD healthy right now?
What are you working on right now?
Any upcoming tasks we need to talk about?
What should we prioritize next?
uh
you know
How is TOD doing now?
```

## Suggested Evidence to Capture

1. `runtime/logs/uvicorn_18001_patchtest.log` tail for the test window.
2. A short screen recording of MIM UI during the 13-line flow.
3. Final `GET /mim/ui/state` and `GET /mim/ui/health` snapshots.
