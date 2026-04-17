#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return int(resp.status), json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return int(exc.code), parsed if isinstance(parsed, dict) else {"data": parsed}


def _get_json(base_url: str, path: str, timeout: int = 20) -> tuple[int, dict]:
    req = urllib.request.Request(f"{base_url}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return int(resp.status), json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return int(exc.code), parsed if isinstance(parsed, dict) else {"data": parsed}


def _evaluate_action_result(
    name: str, status: int, payload: dict, expectations: dict
) -> dict:
    ok_codes = expectations.get("ok_codes", [200])
    expected_details = set(expectations.get("expected_details", []))
    pass_status = status in ok_codes

    detail = str(payload.get("detail", "")).strip()
    if expected_details:
        if status in ok_codes:
            pass
        elif detail in expected_details:
            pass_status = True

    return {
        "name": name,
        "status": status,
        "pass": bool(pass_status),
        "detail": detail,
        "payload_excerpt": json.dumps(payload, ensure_ascii=True)[:320],
    }


def run(base_url: str) -> dict:
    results: list[dict] = []

    # 1) Runtime markers prove we are testing the current deployment.
    st_code, st = _get_json(base_url, "/mim/ui/state")
    results.append(
        _evaluate_action_result(
            "runtime_state_marker",
            st_code,
            st,
            {"ok_codes": [200]},
        )
    )

    # 2) Web summary action path: success (200) or explicit policy guard (403).
    ws_code, ws = _post_json(
        base_url,
        "/gateway/web/summarize",
        {
            "url": "https://example.com",
            "timeout_seconds": 10,
            "max_summary_sentences": 3,
        },
    )
    results.append(
        _evaluate_action_result(
            "web_summary_action",
            ws_code,
            ws,
            {"ok_codes": [200, 403], "expected_details": ["web_access_disabled"]},
        )
    )

    # 3) Capability introspection must always respond.
    mf_code, mf = _get_json(base_url, "/manifest")
    results.append(
        _evaluate_action_result(
            "manifest_capability_introspection",
            mf_code,
            mf,
            {"ok_codes": [200]},
        )
    )

    # 4) Automation monitor: healthy if 200, acceptable guard if automation disabled (503).
    am_code, am = _get_json(base_url, "/automation/status/monitor")
    results.append(
        _evaluate_action_result(
            "automation_status_monitor",
            am_code,
            am,
            {"ok_codes": [200, 503], "expected_details": ["automation_disabled"]},
        )
    )

    total = len(results)
    passed = sum(1 for item in results if item["pass"])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "pass_ratio": round((passed / total) if total else 0.0, 4),
        },
        "results": results,
        "state_runtime_build": str(st.get("runtime_build", "")),
        "state_runtime_features": st.get("runtime_features", []),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run MIM action-execution simulation checks"
    )
    parser.add_argument(
        "--base-url", default=os.getenv("MIM_TEST_BASE_URL", "http://127.0.0.1:18001")
    )
    parser.add_argument(
        "--output", default="runtime/reports/mim_action_simulation_report.json"
    )
    args = parser.parse_args()

    base_url = str(args.base_url).rstrip("/")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    report = run(base_url)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "summary": report["summary"]}, indent=2))

    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
