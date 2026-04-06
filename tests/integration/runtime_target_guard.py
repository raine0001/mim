"""Shared guard for integration runtime targeting.

Checkpoint 2026-04-04:
- The canonical current-source integration target is http://127.0.0.1:18001.
- Treat DEFAULT_BASE_URL and these fail-fast probes as deployment-topology settings,
    not per-suite tuning knobs.
- Do not change the validation target logic unless the intended current-source
    deployment topology is being changed deliberately.
"""

import json
import os
import socket
import urllib.error
import urllib.request


DEFAULT_BASE_URL = "http://127.0.0.1:18001"


def target_selection_message() -> str:
    return (
        "Integration validation must run against a current-source runtime. "
        f"Set MIM_TEST_BASE_URL explicitly or use the default {DEFAULT_BASE_URL}."
    )


def _get_text(base_url: str, path: str) -> tuple[int, str]:
    req = urllib.request.Request(f"{base_url}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, resp.read().decode("utf-8")


def _get_json(base_url: str, path: str) -> tuple[int, dict | list]:
    req = urllib.request.Request(f"{base_url}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        if not body:
            return exc.code, {}
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"detail": body}


def _post_json(base_url: str, path: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        if not body:
            return exc.code, {}
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"detail": body}


def _format_expected_surfaces(expected_surfaces: list[str]) -> str:
    if len(expected_surfaces) == 1:
        return expected_surfaces[0]
    if len(expected_surfaces) == 2:
        return f"{expected_surfaces[0]} and {expected_surfaces[1]}"
    return ", ".join(expected_surfaces[:-1]) + f", and {expected_surfaces[-1]}"


def _describe_unreachable(reason: object) -> str:
    if isinstance(reason, ConnectionRefusedError):
        return "connection refused"
    if isinstance(reason, socket.timeout):
        return "connection timed out"
    return str(reason)


def probe_current_source_runtime(
    *,
    suite_name: str,
    base_url: str,
    require_mim: bool = False,
    require_ui_state: bool = False,
    require_governance: bool = False,
    require_self_health: bool = False,
    require_safety: bool = False,
    require_execution_truth_projection: bool = False,
    require_governed_inquiry_contract: bool = False,
    require_execution_control_plane: bool = False,
    require_proposal_arbitration_learning: bool = False,
) -> None:
    expected_surfaces: list[str] = []
    issues: list[str] = []

    if require_mim:
        expected_surfaces.append("/mim")
        try:
            mim_status, mim_html = _get_text(base_url, "/mim")
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"{suite_name} runtime guard: target runtime is unreachable at {base_url} "
                f"({_describe_unreachable(exc.reason)}). {target_selection_message()}"
            ) from exc
        if mim_status != 200:
            issues.append(f"GET /mim returned {mim_status}")
        elif 'id="systemReasoningPanel"' not in mim_html:
            issues.append("GET /mim is missing systemReasoningPanel")

    if require_ui_state:
        expected_surfaces.append("/mim/ui/state")
        try:
            state_status, state_payload = _get_json(base_url, "/mim/ui/state")
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"{suite_name} runtime guard: target runtime failed while loading /mim/ui/state at "
                f"{base_url}. {target_selection_message()}"
            ) from exc
        if state_status != 200:
            issues.append(f"GET /mim/ui/state returned {state_status}")
        elif not isinstance(state_payload, dict):
            issues.append("GET /mim/ui/state did not return an object payload")
        else:
            runtime_features = state_payload.get("runtime_features", [])
            operator_reasoning = state_payload.get("operator_reasoning")
            if "operator_reasoning_summary" not in runtime_features:
                issues.append("GET /mim/ui/state is missing runtime_features.operator_reasoning_summary")
            if not isinstance(operator_reasoning, dict):
                issues.append("GET /mim/ui/state is missing operator_reasoning payload")

    if require_governance:
        expected_surfaces.append("/execution-truth/governance/evaluate")
        governance_status, governance_payload = _post_json(
            base_url,
            "/execution-truth/governance/evaluate",
            {
                "actor": "runtime-target-guard",
                "source": "runtime-target-guard",
                "managed_scope": f"{suite_name.lower().replace(' ', '-')}-runtime-guard",
                "lookback_hours": 1,
                "metadata_json": {"guard": True, "suite": suite_name},
            },
        )
        if governance_status == 404:
            issues.append("POST /execution-truth/governance/evaluate returned 404")
        elif governance_status >= 500:
            issues.append(
                "POST /execution-truth/governance/evaluate returned "
                f"{governance_status}: {governance_payload}"
            )

    if require_self_health:
        expected_surfaces.append("/mim/self/health")
        health_status, health_payload = _get_json(base_url, "/mim/self/health")
        if health_status == 404:
            issues.append("GET /mim/self/health returned 404")
        elif health_status >= 500:
            issues.append(f"GET /mim/self/health returned {health_status}: {health_payload}")
        elif not isinstance(health_payload, dict):
            issues.append("GET /mim/self/health did not return an object payload")

        expected_surfaces.append("/mim/self/health/record-metric")
        metric_status, metric_payload = _post_json(
            base_url,
            "/mim/self/health/record-metric",
            {
                "memory_percent": 10.0,
                "api_latency_ms": 50.0,
                "api_error_rate": 0.0,
                "cpu_percent": 10.0,
            },
        )
        if metric_status == 404:
            issues.append("POST /mim/self/health/record-metric returned 404")
        elif metric_status >= 500:
            issues.append(
                f"POST /mim/self/health/record-metric returned {metric_status}: {metric_payload}"
            )

    if require_safety:
        expected_surfaces.append("/mim/safety/assess-action")
        assess_status, assess_payload = _post_json(
            base_url,
            "/mim/safety/assess-action",
            {
                "user_id": "runtime-target-guard",
                "action_type": "execute_capability",
                "description": "apt install dangerous-package",
                "category": "software_installation",
                "command": "apt install dangerous-package",
                "target_path": "/usr/local/bin",
                "parameters": {},
            },
        )
        if assess_status == 404:
            issues.append("POST /mim/safety/assess-action returned 404")
        elif assess_status >= 500:
            issues.append(f"POST /mim/safety/assess-action returned {assess_status}: {assess_payload}")
        elif not isinstance(assess_payload, dict) or not assess_payload.get("action_id"):
            issues.append("POST /mim/safety/assess-action did not return an action_id")
        else:
            expected_surfaces.append("/mim/safety/inquiries")
            inquiry_status, inquiry_payload = _post_json(
                base_url,
                "/mim/safety/inquiries?action_id="
                f"{assess_payload['action_id']}&user_id=runtime-target-guard&action_description=runtime-target-guard",
                {},
            )
            if inquiry_status == 404:
                issues.append("POST /mim/safety/inquiries returned 404")
            elif inquiry_status >= 500:
                issues.append(f"POST /mim/safety/inquiries returned {inquiry_status}: {inquiry_payload}")

    if require_execution_truth_projection:
        expected_surfaces.append("/gateway/capabilities/executions/truth/latest")
        projection_status, projection_payload = _get_json(
            base_url,
            "/gateway/capabilities/executions/truth/latest?limit=1",
        )
        if projection_status == 404:
            issues.append("GET /gateway/capabilities/executions/truth/latest returned 404")
        elif projection_status >= 500:
            issues.append(
                "GET /gateway/capabilities/executions/truth/latest returned "
                f"{projection_status}: {projection_payload}"
            )
        elif not isinstance(projection_payload, dict):
            issues.append(
                "GET /gateway/capabilities/executions/truth/latest did not return an object payload"
            )

    if require_governed_inquiry_contract:
        expected_surfaces.append("/inquiry/questions/generate")
        inquiry_status, inquiry_payload = _post_json(
            base_url,
            "/inquiry/questions/generate",
            {
                "actor": "runtime-target-guard",
                "source": "runtime-target-guard",
                "lookback_hours": 1,
                "max_questions": 1,
                "min_soft_friction_count": 3,
                "metadata_json": {"guard": True, "suite": suite_name},
            },
        )
        if inquiry_status == 404:
            issues.append("POST /inquiry/questions/generate returned 404")
        elif inquiry_status >= 500:
            issues.append(
                "POST /inquiry/questions/generate returned "
                f"{inquiry_status}: {inquiry_payload}"
            )
        elif not isinstance(inquiry_payload, dict):
            issues.append("POST /inquiry/questions/generate did not return an object payload")
        else:
            if "decisions" not in inquiry_payload:
                issues.append("POST /inquiry/questions/generate is missing decisions payload")
            questions = inquiry_payload.get("questions", [])
            if isinstance(questions, list) and questions:
                first_question = questions[0] if isinstance(questions[0], dict) else {}
                if "decision_state" not in first_question:
                    issues.append(
                        "POST /inquiry/questions/generate questions are missing decision_state"
                    )

    if require_execution_control_plane:
        expected_surfaces.append("/execution/intents")
        intents_status, intents_payload = _get_json(
            base_url,
            "/execution/intents?managed_scope=runtime-target-guard",
        )
        if intents_status == 404:
            issues.append("GET /execution/intents returned 404")
        elif intents_status >= 500:
            issues.append(f"GET /execution/intents returned {intents_status}: {intents_payload}")
        elif not isinstance(intents_payload, dict):
            issues.append("GET /execution/intents did not return an object payload")

    if require_proposal_arbitration_learning:
        expected_surfaces.append("/workspace/proposals/arbitration-learning")
        learning_status, learning_payload = _get_json(
            base_url,
            "/workspace/proposals/arbitration-learning?limit=1",
        )
        if learning_status == 404:
            issues.append("GET /workspace/proposals/arbitration-learning returned 404")
        elif learning_status != 200:
            issues.append(
                "GET /workspace/proposals/arbitration-learning returned "
                f"{learning_status}: {learning_payload}"
            )
        elif not isinstance(learning_payload, dict):
            issues.append(
                "GET /workspace/proposals/arbitration-learning did not return an object payload"
            )
        elif "learning" not in learning_payload:
            issues.append("GET /workspace/proposals/arbitration-learning is missing learning payload")

    if issues:
        expected_text = _format_expected_surfaces(expected_surfaces)
        issue_text = "; ".join(issues)
        raise RuntimeError(
            f"{suite_name} runtime guard: stale or wrong runtime detected at {base_url}. "
            f"Expected current-source surfaces on {expected_text}. Issues: {issue_text}. "
            f"{target_selection_message()}"
        )


def runtime_base_url() -> str:
    return os.getenv("MIM_TEST_BASE_URL", DEFAULT_BASE_URL)