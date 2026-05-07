# MIM General Conversation Simulation Training

Date: 2026-03-17
Scope: Extensive simulation training for everyday operator conversation quality.
Primary goal: Make MIM robust in natural dialogue without repetitive question loops.

## Training Outcomes

1. MIM handles normal conversation turns naturally.
2. MIM answers TOD/work-status questions directly and consistently.
3. MIM recovers from low-value/noisy inputs without repeated clarifier loops.
4. MIM maintains context across 10 to 20 turn sessions.

## Recommended Training Cadence

Run 3 rounds per day for at least 3 days:

1. Quiet environment baseline.
2. Light background noise (fan/keyboard/room noise).
3. Slightly faster speaking pace with casual phrasing.

For each round, execute all scenario packs below in order.

## Core Evaluation Metrics

Score each turn from 0 to 2:

1. Relevance

- 2: Directly answers user intent.
- 1: Partially relevant.
- 0: Off-topic.

1. Non-Repetition

- 2: No repeated clarifier loop.
- 1: One repeated phrase.
- 0: Multiple repeats or loop.

1. Continuity

- 2: Correctly carries session context.
- 1: Minor context drift.
- 0: Loses context.

1. Recovery

- 2: Recovers quickly from low-value/noisy input.
- 1: Recovery after extra prompt.
- 0: Stays stuck.

Round pass target:

- Average >= 1.6 on each metric.
- No critical loop event (same clarification repeated more than once in 90 seconds).

## Scenario Pack 1: Everyday Openers

Goal: Stable greeting and basic conversation start.

Prompts:

1. `Hello MIM.`
2. `How are you today?`
3. `Can we do a quick check-in?`
4. `What can you help me with right now?`
5. `Let us keep this simple and conversational.`

Expected behavior:

1. One concise greeting/acknowledgment.
2. No repeated startup identity prompt unless required.
3. Transition into helpful next-step suggestions.

## Scenario Pack 2: General Questions

Goal: Handle common non-technical conversation topics.

Prompts:

1. `How is the weather?`
2. `Give me a short weather-style summary for today.`
3. `What should I focus on this morning?`
4. `Can you give me one practical suggestion for now?`
5. `Summarize that in one sentence.`

Expected behavior:

1. If no live weather source, MIM says so clearly and offers alternatives.
2. Responses stay concise, no repeated follow-up loop.
3. MIM adapts response length when asked.

## Scenario Pack 3: TOD Health and Status

Goal: Answer TOD questions directly without drift.

Prompts:

1. `How is TOD doing?`
2. `Is TOD healthy right now?`
3. `Do we have alignment issues between MIM and TOD?`
4. `What is the most recent TOD signal you can use?`
5. `What is the next action to keep TOD stable?`

Expected behavior:

1. Direct status framing (healthy/degraded/unknown) when possible.
2. If unknown, MIM states missing data and next check.
3. No unrelated curiosity prompt spam.

## Scenario Pack 4: Current Work and Upcoming Tasks

Goal: Keep execution context clear and useful.

Prompts:

1. `What are you working on right now?`
2. `Any upcoming tasks we need to discuss?`
3. `What should be prioritized next?`
4. `Give me the top two upcoming items only.`
5. `What could block progress today?`

Expected behavior:

1. Focus on active objective/task context.
2. Provide prioritized short list on request.
3. Avoid repeated generic prompts.

## Scenario Pack 5: Clarification and Low-Value Noise

Goal: Prevent repetition loops on weak transcripts.

Prompts:

1. `uh`
2. `you know`
3. `hmm`
4. `okay... so how is TOD now?`
5. `what are you working on now?`

Expected behavior:

1. At most one concise clarification in cooldown window.
2. Suppress repeated low-value clarifier speech.
3. Resume normal answering immediately when intent becomes clear.

## Scenario Pack 6: Context Continuity (Longer Session)

Goal: Maintain context over 15 turns.

Prompts:

1. `Hello MIM, quick status check.`
2. `How is TOD doing?`
3. `What are you focused on?`
4. `Give me one risk.`
5. `How can we reduce that risk?`
6. `Summarize in one line.`
7. `Now switch to upcoming tasks.`
8. `Which one is highest priority?`
9. `Why that one?`
10. `Any dependencies?`
11. `Okay, what should I do first?`
12. `Repeat that as a checklist.`
13. `Good. Anything else before we proceed?`
14. `Short final recap.`
15. `Thanks.`

Expected behavior:

1. Maintains thread continuity from status to planning.
2. Responds to formatting constraints (one line/checklist/short recap).
3. No loop behavior near end-of-session fatigue.

## Scenario Pack 7: Web Summary Interaction

Goal: Validate website summarization requests and capability discoverability.

Prompts:

1. `MIM, can you browse the web?`
2. `MIM, summarize this website: https://example.com`
3. `Give me the summary in three sentences.`
4. `What key points did you extract from that page?`
5. `What capabilities do you have for external information?`

Expected behavior:

1. If web access is disabled, MIM states that clearly and tells how to enable it.
2. If enabled, MIM returns a concise page summary and key points.
3. MIM does not repeat the same limitation message in a loop.
4. Capability explanation stays concrete (for example manifest endpoint and web summary support).

## Stress Variants (Run After Baseline Pass)

Use these to harden behavior:

1. Faster speech rate.
2. Colloquial phrasing (`hows TOD`, `whats next`, `anything coming up`).
3. Minor transcript ambiguity (`hows tod doin now`, `any upcomming tasks`).
4. Small pauses and restarts in same sentence.
5. URL phrasing variations (`summarize this url`, `what is on this website`).

Pass condition for stress run:

- No critical loop event.
- TOD/work questions still answered directly.

## Failure Tags

Tag each failure with one primary label:

1. `loop_clarifier_repeat`
2. `context_loss`
3. `tod_status_drift`
4. `oververbose_response`
5. `identity_prompt_replay`
6. `low_value_not_recovered`

## Regression Gate

Training cycle is considered improved only if all are true:

1. Baseline round pass target met 2 sessions in a row.
2. Stress round has zero `loop_clarifier_repeat` events.
3. Scenario Pack 3 and 4 average relevance >= 1.8.

## Operational Evidence to Capture

1. `runtime/logs/uvicorn_18001_patchtest.log` for each round.
2. `GET /mim/ui/state` snapshot at start and end.
3. `GET /mim/ui/health` snapshot at start and end.
4. Completed session log using `docs/mim-general-conversation-session-log-template.md`.

## Quick Operator Script (One Full Round)

```text
Hello MIM.
How are you today?
How is the weather?
How is TOD doing?
Is TOD healthy right now?
What are you working on right now?
Any upcoming tasks we need to discuss?
What should be prioritized next?
uh
you know
Okay, so how is TOD now?
What should I do first?
Short final recap.
```
