#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request


def _post_json(
    base_url: str, path: str, payload: dict, timeout: int = 20
) -> tuple[int, dict]:
    req = request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            parsed = json.loads(body) if body else {}
            return int(response.status), parsed if isinstance(parsed, dict) else {
                "data": parsed
            }
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return int(exc.code), parsed if isinstance(parsed, dict) else {"data": parsed}


def _followup_scenarios() -> list[dict[str, object]]:
    return [
        {
            "name": "collatz_deeper_round",
            "opening": "I want to build an application that solves the Collatz Conjecture",
            "turns": [
                "go deeper",
                "why that",
                "research step 2",
                "short final recap",
            ],
            "required_markers": ["step", "next step", "stop when"],
        },
        {
            "name": "distributed_system_design",
            "opening": "Help me research how to build a fault tolerant event driven orchestration service",
            "turns": [
                "go deeper",
                "any dependencies?",
                "continue with step 3",
            ],
            "required_markers": ["step", "next step"],
        },
        {
            "name": "ml_inference_pipeline",
            "opening": "Research how to build a practical machine learning inference pipeline with bounded risk",
            "turns": [
                "research that step",
                "go deeper",
                "repeat that as a checklist",
            ],
            "required_markers": ["step"],
        },
    ]


def _resolution_prompt(payload: dict) -> str:
    resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
    return str(resolution.get("clarification_prompt", "") or "").strip()


def _metadata(payload: dict) -> dict:
    resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
    metadata = (
        resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
    )
    return metadata if isinstance(metadata, dict) else {}


def _run_scenario(
    base_url: str, scenario: dict[str, object], timeout: int
) -> dict[str, object]:
    session_id = f"tech-followup-{uuid.uuid4()}"
    opening = str(scenario.get("opening") or "").strip()
    turns = [
        str(item).strip() for item in scenario.get("turns", []) if str(item).strip()
    ]
    required_markers = [
        str(item).strip().lower()
        for item in scenario.get("required_markers", [])
        if str(item).strip()
    ]
    transcript: list[dict[str, object]] = []

    def ask(text: str) -> tuple[int, dict]:
        return _post_json(
            base_url,
            "/gateway/intake/text",
            {
                "text": text,
                "parsed_intent": "question",
                "confidence": 0.92,
                "metadata_json": {"conversation_session_id": session_id},
            },
            timeout=timeout,
        )

    status, payload = ask(opening)
    first_answer = _resolution_prompt(payload)
    first_metadata = _metadata(payload)
    transcript.append(
        {
            "turn": opening,
            "status": status,
            "answer": first_answer,
            "topic": str(first_metadata.get("conversation_topic", "") or "").strip(),
        }
    )

    ok = status == 200 and bool(first_answer)
    marker_hits = 0
    for turn in turns:
        status, payload = ask(turn)
        answer = _resolution_prompt(payload)
        metadata = _metadata(payload)
        lowered = answer.lower()
        marker_hits += sum(1 for marker in required_markers if marker in lowered)
        transcript.append(
            {
                "turn": turn,
                "status": status,
                "answer": answer,
                "topic": str(metadata.get("conversation_topic", "") or "").strip(),
                "selected_step_index": (
                    metadata.get("web_research", {})
                    if isinstance(metadata.get("web_research", {}), dict)
                    else {}
                ).get("selected_step_index"),
            }
        )
        ok = ok and status == 200 and bool(answer)

    return {
        "name": str(scenario.get("name") or "scenario").strip(),
        "session_id": session_id,
        "ok": ok,
        "required_markers": required_markers,
        "marker_hits": marker_hits,
        "turns": transcript,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a targeted multi-turn technical follow-up sweep against /gateway/intake/text"
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:18001")
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument(
        "--output",
        default="runtime/reports/mim_technical_followup_sweep.json",
    )
    args = parser.parse_args()

    base_url = str(args.base_url).rstrip("/")
    scenarios = _followup_scenarios()
    results = [
        _run_scenario(base_url, scenario, int(args.timeout)) for scenario in scenarios
    ]
    ok_count = sum(1 for item in results if bool(item.get("ok")))
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "scenario_count": len(results),
        "ok_count": ok_count,
        "ok_ratio": round(ok_count / float(max(1, len(results))), 6),
        "results": results,
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output),
                "summary": {"ok_count": ok_count, "scenario_count": len(results)},
            },
            indent=2,
        )
    )
    return 0 if ok_count == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
