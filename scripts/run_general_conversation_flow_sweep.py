#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib import request


def _ask(base_url: str, text: str, parsed_intent: str) -> dict:
    payload = {
        "text": text,
        "parsed_intent": parsed_intent,
        "confidence": 0.9,
    }
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(
        f"{base_url}/gateway/intake/text",
        data=data,
        headers={"content-type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _classify(answer: str) -> str:
    text = str(answer or "").strip().lower()
    if text.startswith("direct answer:"):
        return "generic_direct_answer"
    if text.startswith("got it:"):
        return "generic_got_it"
    if "tod is your task and execution orchestration partner" in text:
        return "tod_identity_specific"
    if "tod's current active objective" in text:
        return "tod_working_specific"
    if "mim handles interaction and context while tod orchestrates tasks" in text:
        return "tod_relationship_specific"
    if "social-media posting capability" in text:
        return "tod_social_capability_specific"
    if "check tod health now" in text:
        return "tod_health_specific"
    if "tod freshness and alignment" in text:
        return "tod_slow_specific"
    if "summarize current focus" in text or "currently driving together" in text:
        return "work_summary_specific"
    if text == "yes. i am mim.":
        return "mim_identity_specific"
    if "online and operating normally" in text:
        return "mim_health_specific"
    if "live weather data" in text:
        return "weather_specific"
    if "scope the application" in text and "mvp" in text:
        return "app_creation_specific"
    if "summarize web pages" in text and "runtime status" in text:
        return "capabilities_specific"
    return "other_specific"


def _families() -> list[tuple[str, str, list[str]]]:
    return [
        (
            "mim_identity",
            "question",
            ["who are you", "are you mim", "do you know who you are"],
        ),
        (
            "mim_health",
            "question",
            ["are you online", "how are you", "status check"],
        ),
        (
            "weather",
            "question",
            [
                "what's the weather like today",
                "weather update for today",
                "how is the weather right now",
            ],
        ),
        (
            "capabilities",
            "question",
            ["what can you do", "what are your capabilities", "help"],
        ),
        (
            "app_creation",
            "discussion",
            [
                "let's create an application today",
                "can we build an application today",
                "help me create an app",
            ],
        ),
        (
            "tod_identity",
            "question",
            ["who is tod", "what is tod", "tell me about tod"],
        ),
        (
            "tod_health",
            "question",
            ["how is tod doing right now", "tod status", "is tod healthy"],
        ),
        (
            "tod_working",
            "question",
            [
                "what is tod working on right now",
                "what tasks is tod working on right now",
                "do you know what tod is working on right now",
            ],
        ),
        (
            "tod_relationship",
            "question",
            [
                "what is your relationship with tod",
                "how do you and tod work together",
                "how do mim and tod work together",
            ],
        ),
        (
            "tod_social_capability",
            "question",
            [
                "can you have TOD check the current capability of agentMIM app to run social media posts",
                "have tod verify if agentmim can post to social media",
                "ask tod to check social media posting capability for agentmim",
            ],
        ),
        (
            "tod_prioritization",
            "question",
            [
                "what are you and tod working on",
                "what should we prioritize with tod next",
                "upcoming tasks with tod",
            ],
        ),
        (
            "tod_slow",
            "question",
            ["is tod stuck", "is tod lagging behind", "tod seems slow"],
        ),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a large mixed general-conversation flow sweep"
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:18001")
    parser.add_argument("--total", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20260319)
    parser.add_argument("--sleep-ms", type=int, default=2)
    parser.add_argument(
        "--output",
        default="runtime/reports/general_conversation_flow_sweep_20260319.json",
    )
    args = parser.parse_args()

    random.seed(args.seed)
    prefixes = [
        "",
        "please ",
        "quickly ",
        "honestly ",
        "just ",
        "can you tell me ",
        "do you know ",
    ]
    suffixes = ["", " right now", " today", " for me", " please"]
    punct = ["", "?", "??", "..."]

    families = _families()
    counts = Counter()
    by_family = defaultdict(Counter)
    samples_bad: list[dict] = []

    for _ in range(max(1, int(args.total))):
        family_name, parsed_intent, prompts = random.choice(families)
        base = random.choice(prompts)
        q = f"{random.choice(prefixes)}{base}{random.choice(suffixes)}{random.choice(punct)}"
        q = re.sub(r"\s+", " ", q).strip()
        try:
            out = _ask(args.base_url.rstrip("/"), q, parsed_intent)
            ans = str(
                (out.get("resolution", {}) or {}).get("clarification_prompt", "")
            ).strip()
            bucket = _classify(ans)
        except Exception as exc:
            ans = f"ERROR: {exc}"
            bucket = "request_error"

        counts[bucket] += 1
        by_family[family_name][bucket] += 1
        if bucket in {"generic_direct_answer", "generic_got_it", "request_error"}:
            if len(samples_bad) < 40:
                samples_bad.append(
                    {
                        "family": family_name,
                        "question": q,
                        "answer": ans,
                        "bucket": bucket,
                    }
                )
        time.sleep(max(0, args.sleep_ms) / 1000.0)

    total = max(1, int(args.total))
    generic_total = counts.get("generic_direct_answer", 0) + counts.get(
        "generic_got_it", 0
    )
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "total": total,
        "seed": args.seed,
        "counts": dict(counts),
        "generic_total": generic_total,
        "generic_rate": round(generic_total / float(total), 6),
        "by_family": {k: dict(v) for k, v in by_family.items()},
        "samples_bad": samples_bad,
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"REPORT {out_path}")
    print(f"TOTAL {total}")
    for key, value in counts.most_common():
        print(f"{key}: {value}")
    print(f"GENERIC_TOTAL {generic_total}")
    print(f"GENERIC_RATE {report['generic_rate']}")
    print(f"SAMPLE_BAD_COUNT {len(samples_bad)}")

    return 0 if counts.get("request_error", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
