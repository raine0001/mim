#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import random
import re
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


def _post_json(
    base_url: str, path: str, payload: dict, timeout: int = 20
) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            parsed = json.loads(body) if body else {}
            return int(response.status), parsed if isinstance(parsed, dict) else {
                "data": parsed
            }
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return int(exc.code), parsed if isinstance(parsed, dict) else {"data": parsed}


def _research_prompt_families() -> list[tuple[str, list[str]]]:
    return [
        (
            "personal_care",
            [
                "what's the best brand of toothpaste proven to whiten teeth",
                "compare the best whitening toothpaste for sensitive teeth",
                "what are the top reviewed electric toothbrushes for plaque removal",
            ],
        ),
        (
            "consumer_tech",
            [
                "research the best entry-level mirrorless camera under $1000",
                "compare the best budget noise cancelling headphones",
                "what is the best external webcam for low-light video calls",
            ],
        ),
        (
            "software_tools",
            [
                "compare the top password managers for families",
                "what are the best project management tools for a small software team",
                "review the best note taking apps for technical research",
            ],
        ),
        (
            "home_office",
            [
                "what is the best ergonomic office chair for back support",
                "compare the top standing desks for small apartments",
                "best reviewed desk lamps for reducing eye strain",
            ],
        ),
        (
            "fitness_nutrition",
            [
                "what protein powder has the best evidence for muscle recovery",
                "compare the best running shoes for beginner marathon training",
                "what is the best electrolyte drink for endurance workouts",
            ],
        ),
        (
            "travel_gear",
            [
                "best carry on luggage brand for frequent business travel",
                "compare the top travel backpacks for 3 day trips",
                "what is the best portable charger for long flights",
            ],
        ),
        (
            "developer_hardware",
            [
                "best mechanical keyboard for software developers",
                "compare the top 4k monitors for programming and text clarity",
                "what is the best wireless mouse for long coding sessions",
            ],
        ),
        (
            "developer_software",
            [
                "compare the best api testing tools for backend teams",
                "what are the top reviewed observability platforms for microservices",
                "best infrastructure as code tools for a small devops team",
            ],
        ),
        (
            "finance_tools",
            [
                "compare the best budgeting apps for families",
                "what is the best small business invoicing software",
                "best expense trackers for freelancers according to reviews",
            ],
        ),
        (
            "kitchen_appliances",
            [
                "what is the best air fryer for a small kitchen",
                "compare the top espresso machines under $500",
                "best reviewed blender for smoothies and frozen fruit",
            ],
        ),
        (
            "education",
            [
                "best online learning platform for data science beginners",
                "compare the top apps for learning spanish vocabulary",
                "what is the best tablet for college note taking",
            ],
        ),
        (
            "automotive",
            [
                "best dash cam with parking mode according to reviews",
                "compare the top portable tire inflators for road trips",
                "what is the best jump starter for a midsize car",
            ],
        ),
        (
            "sleep_wellness",
            [
                "best mattress topper for side sleepers with back pain",
                "compare the top sleep trackers for accuracy",
                "what is the best white noise machine for light sleepers",
            ],
        ),
    ]


def _styled_query(base_prompt: str) -> str:
    prefixes = [
        "MIM, ",
        "Hey MIM, ",
        "",
        "Please research ",
        "Can you look up ",
        "I need you to research ",
    ]
    suffixes = ["?", "", " for me", " and keep it practical", " with evidence"]
    prompt = f"{random.choice(prefixes)}{base_prompt}{random.choice(suffixes)}"
    return re.sub(r"\s+", " ", prompt).strip()


def _result_error_key(result: dict) -> str:
    status = int(result.get("status", 0) or 0)
    error = str(result.get("error") or "").strip() or "unknown"
    return f"{status}:{error}"


def _is_retryable_result(result: dict) -> bool:
    if bool(result.get("ok")):
        return False

    status = int(result.get("status", 0) or 0)
    error = str(result.get("error") or "").strip().lower()
    retryable_statuses = {0, 408, 429, 500, 502, 503, 504}
    retryable_markers = {
        "timed out",
        "timeout",
        "tempor",
        "temporarily unavailable",
        "connection reset",
        "connection aborted",
        "connection refused",
        "remote end closed",
        "service unavailable",
        "bad gateway",
        "too many requests",
        "web_research_no_results",
    }
    if status in retryable_statuses:
        return True
    return any(marker in error for marker in retryable_markers)


def _conversation_case(base_url: str, query: str, timeout: int) -> dict:
    status, payload = _post_json(
        base_url,
        "/gateway/intake/text",
        {
            "text": query,
            "parsed_intent": "question",
            "confidence": 0.91,
        },
        timeout=timeout,
    )
    resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
    metadata = (
        resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
    )
    research = metadata.get("web_research", {}) if isinstance(metadata, dict) else {}
    answer = str(resolution.get("clarification_prompt", "") or "").strip()
    topic = str(metadata.get("conversation_topic", "") or "").strip().lower()
    source_count = (
        len(research.get("sources", []))
        if isinstance(research.get("sources"), list)
        else 0
    )
    proactive = "next step:" in answer.lower()
    ok = (
        status == 200
        and bool(answer)
        and topic == "web_research"
        and bool(research.get("ok"))
        and source_count >= 1
    )
    return {
        "mode": "conversation",
        "status": status,
        "ok": ok,
        "topic": topic,
        "answer": answer,
        "source_count": source_count,
        "proactive": proactive,
        "error": ""
        if ok
        else str(
            payload.get("detail")
            or research.get("error")
            or "conversation_web_research_failed"
        ),
    }


def _api_case(base_url: str, query: str, timeout: int) -> dict:
    status, payload = _post_json(
        base_url,
        "/gateway/web/research",
        {
            "query": query,
            "max_results": 5,
            "max_sources": 3,
        },
        timeout=timeout,
    )
    answer = str(payload.get("answer", "") or "").strip()
    source_count = (
        len(payload.get("sources", []))
        if isinstance(payload.get("sources"), list)
        else 0
    )
    next_steps = (
        payload.get("next_steps", [])
        if isinstance(payload.get("next_steps"), list)
        else []
    )
    proactive = "next step:" in answer.lower() and bool(next_steps)
    ok = (
        status == 200 and bool(payload.get("ok")) and bool(answer) and source_count >= 1
    )
    return {
        "mode": "api",
        "status": status,
        "ok": ok,
        "topic": "web_research",
        "answer": answer,
        "source_count": source_count,
        "proactive": proactive,
        "next_steps": next_steps,
        "error": ""
        if ok
        else str(
            payload.get("detail") or payload.get("error") or "api_web_research_failed"
        ),
    }


def _run_case(
    base_url: str, family: str, query: str, case_mode: str, timeout: int
) -> tuple[str, dict]:
    try:
        result = (
            _conversation_case(base_url, query, timeout)
            if case_mode == "conversation"
            else _api_case(base_url, query, timeout)
        )
    except Exception as exc:
        result = {
            "mode": case_mode,
            "status": 0,
            "ok": False,
            "topic": "",
            "answer": "",
            "source_count": 0,
            "proactive": False,
            "error": str(exc),
        }
    return family, result


def _execute_cases(
    *,
    base_url: str,
    cases: list[dict],
    timeout: int,
    concurrency: int,
) -> list[dict]:
    if not cases:
        return []

    normalized_base = base_url.rstrip("/")
    max_workers = max(1, int(concurrency))
    if max_workers == 1:
        results = []
        for case in cases:
            family, result = _run_case(
                normalized_base,
                str(case.get("family") or ""),
                str(case.get("query") or ""),
                str(case.get("mode") or "conversation"),
                timeout,
            )
            results.append({**case, "family": family, "result": result})
        return results

    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                _run_case,
                normalized_base,
                str(case.get("family") or ""),
                str(case.get("query") or ""),
                str(case.get("mode") or "conversation"),
                timeout,
            ): case
            for case in cases
        }
        for future in concurrent.futures.as_completed(future_map):
            case = future_map[future]
            family, result = future.result()
            results.append({**case, "family": family, "result": result})
    return results


def _compile_report(
    *,
    base_url: str,
    mode: str,
    seed: int,
    total: int,
    sample_limit: int,
    attempted_requests: int,
    retry_passes: int,
    retryable_failures: int,
    recovered_on_retry: int,
    exhausted_retries: int,
    retry_backoff_seconds: float,
    retry_concurrency_scale: float,
    base_concurrency: int,
    pass_summaries: list[dict],
    finalized_results: list[tuple[str, str, dict, int]],
    pending_case_count: int,
    incomplete: bool,
    failure_message: str = "",
) -> dict:
    counts = Counter()
    status_counts = Counter()
    error_counts = Counter()
    by_family = defaultdict(Counter)
    by_family_mode = defaultdict(Counter)
    samples: list[dict] = []
    total_source_count = 0
    proactive_count = 0
    max_source_count = 0
    min_source_count: int | None = None

    effective_total = max(1, int(total))
    for family, query, result, attempt in finalized_results:
        bucket = "ok" if result.get("ok") else "failed"
        counts[bucket] += 1
        counts[f"mode_{result.get('mode', 'unknown')}"] += 1
        counts[f"attempt_{attempt}"] += 1
        status_counts[str(result.get("status", 0))] += 1
        if result.get("topic") == "web_research":
            counts["topic_web_research"] += 1
        if result.get("proactive"):
            proactive_count += 1
            counts["proactive_present"] += 1
        source_count = int(result.get("source_count", 0) or 0)
        total_source_count += source_count
        max_source_count = max(max_source_count, source_count)
        min_source_count = (
            source_count
            if min_source_count is None
            else min(min_source_count, source_count)
        )
        by_family[family][bucket] += 1
        by_family_mode[family][str(result.get("mode", "unknown"))] += 1

        if not result.get("ok"):
            error_counts[_result_error_key(result)] += 1

        if len(samples) < max(1, int(sample_limit)) and (
            not result.get("ok") or not result.get("proactive")
        ):
            samples.append(
                {
                    "family": family,
                    "query": query,
                    "mode": result.get("mode"),
                    "attempt": attempt,
                    "status": result.get("status"),
                    "topic": result.get("topic"),
                    "source_count": result.get("source_count"),
                    "proactive": result.get("proactive"),
                    "error": result.get("error"),
                    "answer": str(result.get("answer", ""))[:500],
                }
            )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "mode": mode,
        "seed": seed,
        "total": effective_total,
        "summary": {
            "ok": counts.get("ok", 0),
            "failed": counts.get("failed", 0),
            "ok_ratio": round(counts.get("ok", 0) / float(effective_total), 6),
            "avg_source_count": round(total_source_count / float(effective_total), 4),
            "min_source_count": int(min_source_count or 0),
            "max_source_count": int(max_source_count),
            "proactive_coverage": round(proactive_count / float(effective_total), 6),
            "web_research_topic_ratio": round(
                counts.get("topic_web_research", 0) / float(effective_total), 6
            ),
            "attempted_requests": attempted_requests,
            "retry_passes": retry_passes,
            "retryable_failures": retryable_failures,
            "recovered_on_retry": recovered_on_retry,
            "exhausted_retries": exhausted_retries,
            "completed_cases": len(finalized_results),
            "pending_cases": pending_case_count,
            "incomplete": incomplete,
        },
        "counts": dict(counts),
        "status_counts": dict(status_counts),
        "error_counts": dict(error_counts),
        "by_family": {key: dict(value) for key, value in by_family.items()},
        "by_family_mode": {key: dict(value) for key, value in by_family_mode.items()},
        "retry_policy": {
            "retry_passes": retry_passes,
            "retry_backoff_seconds": retry_backoff_seconds,
            "retry_concurrency_scale": retry_concurrency_scale,
            "base_concurrency": base_concurrency,
        },
        "pass_summaries": pass_summaries,
        "samples": samples,
        "failure_message": failure_message,
    }


def _write_report(output_path: Path, report: dict) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a bulk MIM web-research sweep")
    parser.add_argument("--base-url", default="http://127.0.0.1:18001")
    parser.add_argument("--total", type=int, default=500)
    parser.add_argument("--seed", type=int, default=20260320)
    parser.add_argument(
        "--mode", choices=["conversation", "api", "mixed"], default="mixed"
    )
    parser.add_argument(
        "--output", default="runtime/reports/mim_web_research_sweep.json"
    )
    parser.add_argument("--sample-limit", type=int, default=60)
    parser.add_argument("--strict-ok-ratio", type=float, default=0.9)
    parser.add_argument("--strict-proactive-coverage", type=float, default=0.9)
    parser.add_argument("--request-timeout", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument(
        "--retry-passes",
        type=int,
        default=2,
        help="Number of deferred retry passes for retryable failures",
    )
    parser.add_argument(
        "--retry-backoff-seconds",
        type=float,
        default=2.0,
        help="Base backoff before each deferred retry pass",
    )
    parser.add_argument(
        "--retry-concurrency-scale",
        type=float,
        default=0.5,
        help="Multiplier applied to concurrency on retry passes",
    )
    args = parser.parse_args()

    random.seed(args.seed)
    families = _research_prompt_families()
    counts = Counter()
    status_counts = Counter()
    error_counts = Counter()
    by_family = defaultdict(Counter)
    by_family_mode = defaultdict(Counter)
    samples: list[dict] = []
    total_source_count = 0
    proactive_count = 0
    max_source_count = 0
    min_source_count: int | None = None
    recovered_on_retry = 0
    exhausted_retries = 0
    attempted_requests = 0
    retryable_failures = 0
    retry_passes = max(0, int(args.retry_passes))
    retry_backoff_seconds = max(0.0, float(args.retry_backoff_seconds))
    retry_concurrency_scale = max(0.1, float(args.retry_concurrency_scale))
    pass_summaries: list[dict] = []
    output = Path(args.output)
    interrupted = False
    failure_message = ""

    cases: list[dict] = []
    for _ in range(max(1, int(args.total))):
        family, prompts = random.choice(families)
        query = _styled_query(random.choice(prompts))
        case_mode = args.mode
        if case_mode == "mixed":
            case_mode = "conversation" if random.random() < 0.7 else "api"

        cases.append(
            {
                "family": family,
                "query": query,
                "mode": case_mode,
                "attempt": 0,
                "first_attempt": 0,
            }
        )

    request_timeout = max(3, int(args.request_timeout))
    base_concurrency = max(1, int(args.concurrency))
    finalized_results: list[tuple[str, str, dict, int]] = []
    pending_cases = cases

    try:
        for pass_index in range(retry_passes + 1):
            if not pending_cases:
                break

            if pass_index == 0:
                pass_concurrency = base_concurrency
                sleep_before_pass = 0.0
            else:
                scaled = int(
                    round(base_concurrency * (retry_concurrency_scale**pass_index))
                )
                pass_concurrency = max(1, scaled)
                sleep_before_pass = retry_backoff_seconds * (2 ** (pass_index - 1))
                if sleep_before_pass > 0:
                    time.sleep(sleep_before_pass)

            for case in pending_cases:
                case["attempt"] = pass_index + 1
                if int(case.get("first_attempt", 0) or 0) <= 0:
                    case["first_attempt"] = pass_index + 1

            executed = _execute_cases(
                base_url=args.base_url,
                cases=pending_cases,
                timeout=request_timeout,
                concurrency=pass_concurrency,
            )
            attempted_requests += len(executed)

            next_pending: list[dict] = []
            pass_ok = 0
            pass_failed = 0
            pass_retry_scheduled = 0
            for case in executed:
                family = str(case.get("family") or "")
                query = str(case.get("query") or "")
                attempt = int(case.get("attempt", pass_index + 1) or pass_index + 1)
                result = (
                    case.get("result", {})
                    if isinstance(case.get("result"), dict)
                    else {}
                )
                retryable = _is_retryable_result(result)

                if bool(result.get("ok")):
                    pass_ok += 1
                    if attempt > 1:
                        recovered_on_retry += 1
                    finalized_results.append((family, query, result, attempt))
                    continue

                pass_failed += 1
                if retryable:
                    retryable_failures += 1
                if retryable and pass_index < retry_passes:
                    pass_retry_scheduled += 1
                    next_pending.append(
                        {
                            "family": family,
                            "query": query,
                            "mode": str(
                                case.get("mode") or result.get("mode") or "conversation"
                            ),
                            "attempt": attempt,
                            "first_attempt": int(
                                case.get("first_attempt", attempt) or attempt
                            ),
                        }
                    )
                else:
                    if retryable and pass_index >= retry_passes:
                        exhausted_retries += 1
                    finalized_results.append((family, query, result, attempt))

            pass_summaries.append(
                {
                    "pass_index": pass_index,
                    "input_cases": len(pending_cases),
                    "ok": pass_ok,
                    "failed": pass_failed,
                    "retry_scheduled": pass_retry_scheduled,
                    "concurrency": pass_concurrency,
                    "sleep_before_pass_seconds": round(sleep_before_pass, 3),
                }
            )
            pending_cases = next_pending
            _write_report(
                output,
                _compile_report(
                    base_url=args.base_url,
                    mode=args.mode,
                    seed=args.seed,
                    total=int(args.total),
                    sample_limit=int(args.sample_limit),
                    attempted_requests=attempted_requests,
                    retry_passes=retry_passes,
                    retryable_failures=retryable_failures,
                    recovered_on_retry=recovered_on_retry,
                    exhausted_retries=exhausted_retries,
                    retry_backoff_seconds=retry_backoff_seconds,
                    retry_concurrency_scale=retry_concurrency_scale,
                    base_concurrency=base_concurrency,
                    pass_summaries=pass_summaries,
                    finalized_results=finalized_results,
                    pending_case_count=len(pending_cases),
                    incomplete=bool(pending_cases),
                ),
            )
    except KeyboardInterrupt:
        interrupted = True
        failure_message = "keyboard_interrupt"
    except Exception as exc:
        interrupted = True
        failure_message = f"{type(exc).__name__}:{exc}"

    report = _compile_report(
        base_url=args.base_url,
        mode=args.mode,
        seed=args.seed,
        total=int(args.total),
        sample_limit=int(args.sample_limit),
        attempted_requests=attempted_requests,
        retry_passes=retry_passes,
        retryable_failures=retryable_failures,
        recovered_on_retry=recovered_on_retry,
        exhausted_retries=exhausted_retries,
        retry_backoff_seconds=retry_backoff_seconds,
        retry_concurrency_scale=retry_concurrency_scale,
        base_concurrency=base_concurrency,
        pass_summaries=pass_summaries,
        finalized_results=finalized_results,
        pending_case_count=len(pending_cases),
        incomplete=bool(pending_cases) or interrupted,
        failure_message=failure_message,
    )
    _write_report(output, report)

    print(json.dumps({"output": str(output), "summary": report["summary"]}, indent=2))

    if interrupted:
        return 130 if failure_message == "keyboard_interrupt" else 1

    if report["summary"]["failed"] > 0 and report["summary"]["ok_ratio"] < float(
        args.strict_ok_ratio
    ):
        return 1
    if report["summary"]["proactive_coverage"] < float(args.strict_proactive_coverage):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
