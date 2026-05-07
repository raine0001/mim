import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


DEFAULT_BASE_URL = "http://127.0.0.1:18001"
BASE_URL = os.getenv("MIM_TEST_BASE_URL", DEFAULT_BASE_URL)
SHARED_RUNTIME_ROOT = Path(__file__).resolve().parents[2] / "runtime" / "shared"
COLLAB_PROGRESS_FILE = SHARED_RUNTIME_ROOT / "MIM_TOD_COLLAB_PROGRESS.latest.json"
DECISION_PROCESS_FILE = SHARED_RUNTIME_ROOT / "MIM_DECISION_TASK.latest.json"


def _target_selection_message() -> str:
    return (
        "Objective 84 validation must run against a current-source runtime. "
        f"Set MIM_TEST_BASE_URL explicitly or use the default {DEFAULT_BASE_URL}."
    )


def post_json(path: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, json.loads(body) if body else {}


def get_json(path: str, query: dict | None = None) -> tuple[int, dict | list]:
    url = f"{BASE_URL}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, json.loads(body) if body else {}


def get_text(path: str) -> tuple[int, str]:
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, resp.read().decode("utf-8")


def post_multipart(path: str, *, fields: dict[str, str], files: list[dict]) -> tuple[int, dict]:
    boundary = f"----mim-boundary-{uuid4().hex}"
    body = bytearray()
    for key, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")
    for item in files:
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="{item["field"]}"; '
                f'filename="{item["filename"]}"\r\n'
            ).encode("utf-8")
        )
        body.extend(f'Content-Type: {item["content_type"]}\r\n\r\n'.encode("utf-8"))
        body.extend(item["content"])
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=bytes(body),
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8")
        return exc.code, json.loads(body_text) if body_text else {}


def _probe_current_source_runtime() -> None:
    issues: list[str] = []

    try:
        mim_status, mim_html = get_text("/mim")
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ConnectionRefusedError):
            reason_text = "connection refused"
        elif isinstance(reason, socket.timeout):
            reason_text = "connection timed out"
        else:
            reason_text = str(reason)
        raise RuntimeError(
            "Objective 84 runtime guard: target runtime is unreachable at "
            f"{BASE_URL} ({reason_text}). {_target_selection_message()}"
        ) from exc

    if mim_status != 200:
        issues.append(f"GET /mim returned {mim_status}")
    elif 'id="systemReasoningPanel"' not in mim_html:
        issues.append("GET /mim is missing systemReasoningPanel")

    try:
        state_status, state_payload = get_json("/mim/ui/state")
    except urllib.error.URLError as exc:
        raise RuntimeError(
            "Objective 84 runtime guard: target runtime failed while loading /mim/ui/state at "
            f"{BASE_URL}. {_target_selection_message()}"
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

    governance_status, governance_payload = post_json(
        "/execution-truth/governance/evaluate",
        {
            "actor": "objective84-runtime-guard",
            "source": "objective84-runtime-guard",
            "managed_scope": "objective84-runtime-guard",
            "lookback_hours": 1,
            "metadata_json": {"guard": True},
        },
    )
    if governance_status == 404:
        issues.append("POST /execution-truth/governance/evaluate returned 404")
    elif governance_status >= 500:
        issues.append(
            "POST /execution-truth/governance/evaluate returned "
            f"{governance_status}: {governance_payload}"
        )

    if issues:
        issue_text = "; ".join(issues)
        raise RuntimeError(
            "Objective 84 runtime guard: stale or wrong runtime detected at "
            f"{BASE_URL}. Expected current-source surfaces on /mim, /mim/ui/state, and "
            f"/execution-truth/governance/evaluate. Issues: {issue_text}. "
            f"{_target_selection_message()}"
        )


class Objective84OperatorVisibleSystemReasoningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _probe_current_source_runtime()

    def _write_collaboration_progress_fixture(self, payload: dict) -> str | None:
        SHARED_RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        prior = COLLAB_PROGRESS_FILE.read_text(encoding="utf-8") if COLLAB_PROGRESS_FILE.exists() else None
        COLLAB_PROGRESS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return prior

    def _restore_collaboration_progress_fixture(self, prior: str | None) -> None:
        if prior is None:
            if COLLAB_PROGRESS_FILE.exists():
                COLLAB_PROGRESS_FILE.unlink()
            return
        COLLAB_PROGRESS_FILE.write_text(prior, encoding="utf-8")

    def _write_decision_process_fixture(self, payload: dict) -> str | None:
        SHARED_RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        prior = DECISION_PROCESS_FILE.read_text(encoding="utf-8") if DECISION_PROCESS_FILE.exists() else None
        DECISION_PROCESS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return prior

    def _restore_decision_process_fixture(self, prior: str | None) -> None:
        if prior is None:
            if DECISION_PROCESS_FILE.exists():
                DECISION_PROCESS_FILE.unlink()
            return
        DECISION_PROCESS_FILE.write_text(prior, encoding="utf-8")

    def _register_workspace_scan(self) -> None:
        status, payload = post_json(
            "/gateway/capabilities",
            {
                "capability_name": "workspace_scan",
                "category": "diagnostic",
                "description": "Scan workspace and return observation set",
                "requires_confirmation": False,
                "enabled": True,
                "safety_policy": {"scope": "non-actuating", "mode": "scan-only"},
            },
        )
        self.assertEqual(status, 200, payload)

    def _create_stale_observation(self, *, zone: str, run_id: str) -> None:
        stale_time = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"objective84 reasoning stale scan {run_id}",
                "parsed_intent": "observe_workspace",
                "confidence": 0.95,
                "metadata_json": {
                    "scan_mode": "full",
                    "scan_area": zone,
                    "confidence_threshold": 0.6,
                    "run_id": run_id,
                },
            },
        )
        self.assertEqual(status, 200, event)
        execution_id = int(event.get("execution", {}).get("execution_id", 0) or 0)
        self.assertGreater(execution_id, 0)

        for state in ["accepted", "running"]:
            status, updated = post_json(
                f"/gateway/capabilities/executions/{execution_id}/feedback",
                {
                    "status": state,
                    "reason": state,
                    "actor": "tod",
                    "feedback_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, updated)

        status, done = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "scan complete",
                "actor": "tod",
                "feedback_json": {
                    "run_id": run_id,
                    "observations": [
                        {
                            "label": f"obj84-stale-{run_id}",
                            "zone": zone,
                            "confidence": 0.91,
                            "observed_at": stale_time,
                        }
                    ],
                },
            },
        )
        self.assertEqual(status, 200, done)

    def _seed_stewardship_prereqs(self, *, scope: str, run_id: str, source: str) -> None:
        self._register_workspace_scan()
        self._create_stale_observation(zone=scope, run_id=run_id)

        status, pref = post_json(
            "/preferences",
            {
                "user_id": "operator",
                "preference_type": "stewardship_priority:default",
                "value": 0.8,
                "confidence": 0.9,
                "source": source,
            },
        )
        self.assertEqual(status, 200, pref)

        status, goals = post_json(
            "/strategy/goals/build",
            {
                "actor": "objective84-test",
                "source": source,
                "lookback_hours": 48,
                "max_items_per_domain": 50,
                "max_goals": 4,
                "min_context_confidence": 0.0,
                "min_domains_required": 1,
                "min_cross_domain_links": 0,
                "generate_horizon_plans": False,
                "generate_improvement_proposals": False,
                "generate_maintenance_cycles": False,
                "metadata_json": {"run_id": run_id, "managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, goals)

    def _run_stewardship_cycle(self, *, scope: str, run_id: str, source: str) -> dict:
        status, payload = post_json(
            "/stewardship/cycle",
            {
                "actor": "objective84-test",
                "source": source,
                "managed_scope": scope,
                "stale_after_seconds": 300,
                "lookback_hours": 168,
                "max_strategies": 5,
                "max_actions": 5,
                "auto_execute": False,
                "force_degraded": True,
                "target_environment_state": {
                    "zone_freshness_seconds": 300,
                    "critical_object_confidence": 0.8,
                    "max_degraded_zones": 0,
                    "max_zone_uncertainty_score": 0.35,
                    "max_system_drift_rate": 0.05,
                    "max_missing_key_objects": 0,
                    "key_objects": [f"objective84-missing-{run_id}"],
                    "intervention_policy": {
                        "max_interventions_per_window": 1,
                        "window_minutes": 180,
                        "scope_cooldown_seconds": 3600,
                        "per_strategy_limit": 1,
                    },
                },
                "metadata_json": {"run_id": run_id, "managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, payload)
        return payload

    def _register_capability(self, *, capability_name: str) -> None:
        status, payload = post_json(
            "/gateway/capabilities",
            {
                "capability_name": capability_name,
                "category": "diagnostic",
                "description": "Objective 84 operator reasoning probe",
                "requires_confirmation": False,
                "enabled": True,
            },
        )
        self.assertEqual(status, 200, payload)

    def _seed_execution_truth(self, *, scope: str, run_id: str, suffix: str) -> None:
        capability_name = f"objective84_truth_probe_{run_id}_{suffix}"
        self._register_capability(capability_name=capability_name)

        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"objective84 governance probe {run_id} {suffix}",
                "parsed_intent": "workspace_check",
                "requested_goal": "collect execution truth for operator reasoning",
                "metadata_json": {
                    "capability": capability_name,
                    "managed_scope": scope,
                    "run_id": run_id,
                },
            },
        )
        self.assertEqual(status, 200, event)
        execution_id = int(event.get("execution", {}).get("execution_id", 0) or 0)
        self.assertGreater(execution_id, 0)

        status, accepted = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "accepted",
                "reason": "accepted",
                "actor": "tod",
                "correlation_json": {"managed_scope": scope, "target_scope": scope},
                "feedback_json": {"managed_scope": scope, "run_id": run_id},
            },
        )
        self.assertEqual(status, 200, accepted)

        status, done = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "execution truth recorded",
                "runtime_outcome": "recovered",
                "actor": "tod",
                "correlation_json": {"managed_scope": scope, "target_scope": scope},
                "feedback_json": {"managed_scope": scope, "run_id": run_id},
                "execution_truth": {
                    "contract": "execution_truth_v1",
                    "execution_id": execution_id,
                    "capability_name": capability_name,
                    "expected_duration_ms": 900,
                    "actual_duration_ms": 1710,
                    "duration_delta_ratio": round((1710 - 900) / 900.0, 6),
                    "retry_count": 2,
                    "fallback_used": True,
                    "runtime_outcome": "recovered",
                    "environment_shift_detected": True,
                    "simulation_match_status": "mismatch",
                    "truth_confidence": 0.95,
                    "published_at": datetime.now(timezone.utc).isoformat(),
                },
            },
        )
        self.assertEqual(status, 200, done)

    def _generate_questions(self, *, run_id: str, source: str) -> dict:
        status, generated = post_json(
            "/inquiry/questions/generate",
            {
                "actor": "objective84-test",
                "source": source,
                "lookback_hours": 24,
                "max_questions": 10,
                "min_soft_friction_count": 3,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, generated)
        return generated

    def test_mim_page_contains_system_reasoning_panel_hooks(self) -> None:
        status, html = get_text("/mim")
        self.assertEqual(status, 200)
        self.assertIn('id="systemReasoningPanel"', html)
        self.assertIn('id="systemReasoningList"', html)
        self.assertIn('id="systemReasoningSummary"', html)
        self.assertIn("function renderSystemReasoningPanel(reasoning = {})", html)
        self.assertIn("Current recommendation", html)
        self.assertIn("Trust signals", html)
        self.assertIn("Lightweight autonomy", html)
        self.assertIn("Human feedback loop", html)
        self.assertIn("Stability guard", html)

    def test_mim_page_contains_chat_first_operator_surface_hooks(self) -> None:
        status, html = get_text("/mim")
        self.assertEqual(status, 200)
        self.assertIn('id="chatLog"', html)
        self.assertIn('id="chatDropzone"', html)
        self.assertIn('id="imageUploadBtn"', html)
        self.assertIn('id="chatMicBtn"', html)
        self.assertIn('id="secondaryTabDiagnostics"', html)
        self.assertIn('id="secondaryTabMedia"', html)
        self.assertIn('id="activeObjectiveText"', html)
        self.assertIn('One persistent primary thread', html)

    def test_state_exposes_primary_chat_thread_payload(self) -> None:
        status, payload = get_json("/mim/ui/state")
        self.assertEqual(status, 200, payload)
        self.assertIsInstance(payload, dict)
        chat_thread = payload.get("chat_thread", {})
        self.assertIsInstance(chat_thread, dict)
        self.assertEqual(str(chat_thread.get("primary_thread", "")).strip(), "primary_operator")
        self.assertIn("messages", chat_thread)
        self.assertIsInstance(chat_thread.get("messages"), list)

    def test_image_upload_appends_into_primary_chat_thread(self) -> None:
        png_bytes = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDAT\x08\x99c``\x00\x00\x00\x04"
            b"\x00\x01\x0b\xe7\x02\x9d\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        prompt = f"what is wrong here? {uuid4().hex[:8]}"
        status, payload = post_multipart(
            "/mim/ui/chat/upload-image",
            fields={"prompt": prompt, "session_key": "primary_operator"},
            files=[
                {
                    "field": "file",
                    "filename": "objective84-upload.png",
                    "content_type": "image/png",
                    "content": png_bytes,
                }
            ],
        )
        self.assertEqual(status, 200, payload)
        self.assertIsInstance(payload, dict)
        attachment = payload.get("attachment", {})
        self.assertIsInstance(attachment, dict)
        self.assertIn("/mim/ui/media/", str(attachment.get("url", "")))

        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)
        chat_thread = state_payload.get("chat_thread", {})
        self.assertIsInstance(chat_thread, dict)
        messages = chat_thread.get("messages", [])
        self.assertIsInstance(messages, list)
        self.assertTrue(
            any(
                isinstance(message, dict)
                and isinstance(message.get("attachment"), dict)
                and str(message.get("attachment", {}).get("url", "")).strip()
                == str(attachment.get("url", "")).strip()
                for message in messages
            ),
            state_payload,
        )

    def test_state_exposes_operator_visible_reasoning_bundle(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective84-reasoning-{run_id}"
        source = "objective84-operator-visible-reasoning"

        self._seed_stewardship_prereqs(scope=scope, run_id=run_id, source=source)
        self._run_stewardship_cycle(scope=scope, run_id=run_id, source=source)
        self._run_stewardship_cycle(scope=scope, run_id=run_id, source=source)

        for suffix in ["a", "b", "c"]:
            self._seed_execution_truth(scope=scope, run_id=run_id, suffix=suffix)

        status, governance_payload = post_json(
            "/execution-truth/governance/evaluate",
            {
                "actor": "objective84-test",
                "source": source,
                "managed_scope": scope,
                "lookback_hours": 168,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, governance_payload)
        governance = governance_payload.get("governance", {}) if isinstance(governance_payload, dict) else {}
        governance_decision = str(governance.get("governance_decision", "")).strip()
        self.assertIn(governance_decision, {"lower_autonomy_boundary", "escalate_to_operator"}, governance)

        status, boundary_payload = post_json(
            "/autonomy/boundaries/recompute",
            {
                "actor": "objective84-test",
                "source": source,
                "scope": scope,
                "lookback_hours": 168,
                "min_samples": 1,
                "apply_recommended_boundaries": False,
                "hard_ceiling_overrides": {
                    "human_safety": True,
                    "legality": True,
                    "system_integrity": True,
                },
                "evidence_inputs_override": {
                    "success_rate": 0.88,
                    "escalation_rate": 0.08,
                    "retry_rate": 0.1,
                    "interruption_rate": 0.05,
                    "memory_delta_rate": 0.72,
                    "sample_count": 18,
                    "override_rate": 0.02,
                    "replan_rate": 0.08,
                    "environment_stability": 0.76,
                    "development_confidence": 0.78,
                    "constraint_reliability": 0.84,
                    "experiment_confidence": 0.74,
                },
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, boundary_payload)

        generated = self._generate_questions(run_id=run_id, source=source)
        questions = generated.get("questions", []) if isinstance(generated, dict) else []
        self.assertTrue(questions, generated)

        status, state = get_json("/mim/ui/state")
        self.assertEqual(status, 200, state)
        self.assertIn("operator_reasoning_summary", state.get("runtime_features", []))
        self.assertIn("system_awareness_visibility", state.get("runtime_features", []))
        self.assertIn("operator_trust_signals", state.get("runtime_features", []))
        self.assertIn("lightweight_autonomy_guidance", state.get("runtime_features", []))
        self.assertIn("human_feedback_loop", state.get("runtime_features", []))
        self.assertIn("system_stability_guard", state.get("runtime_features", []))

        operator_reasoning = state.get("operator_reasoning", {}) if isinstance(state, dict) else {}
        self.assertTrue(str(operator_reasoning.get("summary", "")).strip(), operator_reasoning)
        self.assertTrue(str(operator_reasoning.get("trust_signal_summary", "")).strip(), operator_reasoning)
        runtime_health = operator_reasoning.get("runtime_health", {}) if isinstance(operator_reasoning.get("runtime_health", {}), dict) else {}
        self.assertTrue(str(runtime_health.get("summary", "")).strip(), runtime_health)
        self.assertEqual(
            str((state.get("conversation_context", {}) or {}).get("operator_reasoning_summary", "")).strip(),
            str(operator_reasoning.get("summary", "")).strip(),
            state,
        )
        self.assertEqual(
            str((state.get("conversation_context", {}) or {}).get("trust_signal_summary", "")).strip(),
            str(operator_reasoning.get("trust_signal_summary", "")).strip(),
            state,
        )
        self.assertEqual(
            str((state.get("conversation_context", {}) or {}).get("runtime_health_summary", "")).strip(),
            str(runtime_health.get("summary", "")).strip(),
            state,
        )

        active_goal = operator_reasoning.get("active_goal", {}) if isinstance(operator_reasoning.get("active_goal", {}), dict) else {}
        self.assertTrue(str(active_goal.get("reasoning_summary", "")).strip(), active_goal)

        inquiry = operator_reasoning.get("inquiry", {}) if isinstance(operator_reasoning.get("inquiry", {}), dict) else {}
        self.assertIn(
            str(inquiry.get("trigger_type", "")).strip(),
            {"stewardship_persistent_degradation", "execution_truth_runtime_mismatch"},
            inquiry,
        )
        self.assertTrue(str(inquiry.get("decision_state", "")).strip(), inquiry)

        ui_governance = operator_reasoning.get("governance", {}) if isinstance(operator_reasoning.get("governance", {}), dict) else {}
        self.assertEqual(str(ui_governance.get("managed_scope", "")).strip(), scope, ui_governance)
        self.assertEqual(str(ui_governance.get("governance_decision", "")).strip(), governance_decision, ui_governance)

        autonomy = operator_reasoning.get("autonomy", {}) if isinstance(operator_reasoning.get("autonomy", {}), dict) else {}
        self.assertEqual(str(autonomy.get("scope", "")).strip(), scope, autonomy)
        self.assertEqual(str(autonomy.get("governance_decision", "")).strip(), governance_decision, autonomy)
        self.assertTrue(str(autonomy.get("adaptation_summary", "")).strip(), autonomy)

        recommendation = operator_reasoning.get("current_recommendation", {}) if isinstance(operator_reasoning.get("current_recommendation", {}), dict) else {}
        self.assertTrue(str(recommendation.get("summary", "")).strip(), recommendation)
        self.assertTrue(str(recommendation.get("source", "")).strip(), recommendation)
        self.assertEqual(
            str((state.get("conversation_context", {}) or {}).get("current_recommendation_summary", "")).strip(),
            str(recommendation.get("summary", "")).strip(),
            state,
        )

        trust = operator_reasoning.get("trust_explainability", {}) if isinstance(operator_reasoning.get("trust_explainability", {}), dict) else {}
        self.assertTrue(str(trust.get("what_it_did", "")).strip(), trust)
        self.assertTrue(str(trust.get("what_it_will_do_next", "")).strip(), trust)
        self.assertIn(str(trust.get("confidence_tier", "")).strip(), {"", "low", "guarded", "medium", "moderate", "high"}, trust)

        lightweight_autonomy = operator_reasoning.get("lightweight_autonomy", {}) if isinstance(operator_reasoning.get("lightweight_autonomy", {}), dict) else {}
        self.assertTrue(str(lightweight_autonomy.get("summary", "")).strip(), lightweight_autonomy)
        self.assertEqual(
            str((state.get("conversation_context", {}) or {}).get("lightweight_autonomy_summary", "")).strip(),
            str(lightweight_autonomy.get("summary", "")).strip(),
            state,
        )

        feedback_loop = operator_reasoning.get("feedback_loop", {}) if isinstance(operator_reasoning.get("feedback_loop", {}), dict) else {}
        self.assertTrue(str(feedback_loop.get("summary", "")).strip(), feedback_loop)
        self.assertEqual(
            str((state.get("conversation_context", {}) or {}).get("feedback_loop_summary", "")).strip(),
            str(feedback_loop.get("summary", "")).strip(),
            state,
        )

        stability_guard = operator_reasoning.get("stability_guard", {}) if isinstance(operator_reasoning.get("stability_guard", {}), dict) else {}
        self.assertTrue(str(stability_guard.get("summary", "")).strip(), stability_guard)
        self.assertEqual(
            str((state.get("conversation_context", {}) or {}).get("stability_guard_summary", "")).strip(),
            str(stability_guard.get("summary", "")).strip(),
            state,
        )

        stewardship = operator_reasoning.get("stewardship", {}) if isinstance(operator_reasoning.get("stewardship", {}), dict) else {}
        self.assertEqual(str(stewardship.get("managed_scope", "")).strip(), scope, stewardship)
        self.assertTrue(bool(stewardship.get("persistent_degradation", False)), stewardship)
        self.assertEqual(str(stewardship.get("followup_status", "")).strip(), "generated", stewardship)

    def test_state_keeps_operator_reasoning_scope_coherent_when_newer_rows_exist_for_other_scope(self) -> None:
        run_id = uuid4().hex[:8]
        primary_scope = f"objective84-primary-{run_id}"
        secondary_scope = f"objective84-secondary-{run_id}"
        source = "objective84-operator-visible-reasoning"

        self._seed_stewardship_prereqs(scope=primary_scope, run_id=run_id, source=source)
        self._run_stewardship_cycle(scope=primary_scope, run_id=run_id, source=source)
        self._run_stewardship_cycle(scope=primary_scope, run_id=run_id, source=source)
        for suffix in ["a", "b", "c"]:
            self._seed_execution_truth(scope=primary_scope, run_id=run_id, suffix=f"primary-{suffix}")

        status, primary_governance_payload = post_json(
            "/execution-truth/governance/evaluate",
            {
                "actor": "objective84-test",
                "source": source,
                "managed_scope": primary_scope,
                "lookback_hours": 168,
                "metadata_json": {"run_id": run_id, "scope": primary_scope},
            },
        )
        self.assertEqual(status, 200, primary_governance_payload)
        primary_governance = primary_governance_payload.get("governance", {}) if isinstance(primary_governance_payload, dict) else {}

        status, primary_boundary_payload = post_json(
            "/autonomy/boundaries/recompute",
            {
                "actor": "objective84-test",
                "source": source,
                "scope": primary_scope,
                "lookback_hours": 168,
                "min_samples": 1,
                "apply_recommended_boundaries": False,
                "hard_ceiling_overrides": {
                    "human_safety": True,
                    "legality": True,
                    "system_integrity": True,
                },
                "evidence_inputs_override": {
                    "success_rate": 0.88,
                    "escalation_rate": 0.08,
                    "retry_rate": 0.1,
                    "interruption_rate": 0.05,
                    "memory_delta_rate": 0.72,
                    "sample_count": 18,
                    "override_rate": 0.02,
                    "replan_rate": 0.08,
                    "environment_stability": 0.76,
                    "development_confidence": 0.78,
                    "constraint_reliability": 0.84,
                    "experiment_confidence": 0.74,
                },
                "metadata_json": {"run_id": run_id, "scope": primary_scope},
            },
        )
        self.assertEqual(status, 200, primary_boundary_payload)

        generated = self._generate_questions(run_id=run_id, source=source)
        primary_questions = generated.get("questions", []) if isinstance(generated, dict) else []
        self.assertTrue(primary_questions, generated)

        self._seed_execution_truth(scope=secondary_scope, run_id=run_id, suffix="secondary-a")
        status, secondary_governance_payload = post_json(
            "/execution-truth/governance/evaluate",
            {
                "actor": "objective84-test",
                "source": source,
                "managed_scope": secondary_scope,
                "lookback_hours": 168,
                "metadata_json": {"run_id": run_id, "scope": secondary_scope},
            },
        )
        self.assertEqual(status, 200, secondary_governance_payload)

        status, secondary_boundary_payload = post_json(
            "/autonomy/boundaries/recompute",
            {
                "actor": "objective84-test",
                "source": source,
                "scope": secondary_scope,
                "lookback_hours": 168,
                "min_samples": 1,
                "apply_recommended_boundaries": False,
                "hard_ceiling_overrides": {
                    "human_safety": True,
                    "legality": True,
                    "system_integrity": True,
                },
                "evidence_inputs_override": {
                    "success_rate": 0.98,
                    "escalation_rate": 0.0,
                    "retry_rate": 0.0,
                    "interruption_rate": 0.0,
                    "memory_delta_rate": 0.95,
                    "sample_count": 24,
                    "override_rate": 0.0,
                    "replan_rate": 0.0,
                    "environment_stability": 0.96,
                    "development_confidence": 0.94,
                    "constraint_reliability": 0.98,
                    "experiment_confidence": 0.95,
                },
                "metadata_json": {"run_id": run_id, "scope": secondary_scope},
            },
        )
        self.assertEqual(status, 200, secondary_boundary_payload)

        status, state = get_json("/mim/ui/state")
        self.assertEqual(status, 200, state)

        operator_reasoning = state.get("operator_reasoning", {}) if isinstance(state, dict) else {}
        inquiry = operator_reasoning.get("inquiry", {}) if isinstance(operator_reasoning.get("inquiry", {}), dict) else {}
        ui_governance = operator_reasoning.get("governance", {}) if isinstance(operator_reasoning.get("governance", {}), dict) else {}
        autonomy = operator_reasoning.get("autonomy", {}) if isinstance(operator_reasoning.get("autonomy", {}), dict) else {}
        stewardship = operator_reasoning.get("stewardship", {}) if isinstance(operator_reasoning.get("stewardship", {}), dict) else {}

        inquiry_scope = str(inquiry.get("managed_scope", "")).strip()
        self.assertIn(inquiry_scope, {"", primary_scope}, inquiry)
        self.assertNotEqual(inquiry_scope, secondary_scope, inquiry)
        self.assertEqual(str(ui_governance.get("managed_scope", "")).strip(), primary_scope, ui_governance)
        self.assertEqual(str(autonomy.get("scope", "")).strip(), primary_scope, autonomy)
        self.assertEqual(str(stewardship.get("managed_scope", "")).strip(), primary_scope, stewardship)
        self.assertEqual(
            str(ui_governance.get("governance_decision", "")).strip(),
            str(primary_governance.get("governance_decision", "")).strip(),
            ui_governance,
        )

    def test_state_exposes_tod_collaboration_progress_in_operator_reasoning(self) -> None:
        request_id = f"objective97-ui-request-{uuid4().hex[:8]}"
        task_id = f"objective97-ui-task-{uuid4().hex[:8]}"
        prior = self._write_collaboration_progress_fixture(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "type": "mim_tod_collaboration_progress_v1",
                "execution_id": request_id,
                "id_kind": "bridge_request_id",
                "execution_lane": "tod_bridge_request",
                "task_id": task_id,
                "request_id": request_id,
                "owners": {
                    "mim": "publish_and_decision_owner",
                    "tod": "consume_and_execution_owner",
                },
                "workstreams": [
                    {
                        "id": 1,
                        "name": "consume_mutation_tracking",
                        "mim_status": "auto_watch_captured_consume_mutation",
                        "tod_status": "result_published_for_target_task",
                        "latest_observation": f"consume evidence captured for task={task_id}",
                    },
                    {
                        "id": 3,
                        "name": "tod_recovery_progress",
                        "mim_status": "auto_observing_tod_recovery_signal",
                        "tod_status": "no_heartbeats_recovery_in_progress",
                        "latest_observation": "TOD recovery alert reports issue=publication_surface_divergence",
                    },
                ],
            }
        )
        try:
            status, state = get_json("/mim/ui/state")
            self.assertEqual(status, 200, state)
            self.assertIn("tod_collaboration_progress", state.get("runtime_features", []))

            operator_reasoning = state.get("operator_reasoning", {}) if isinstance(state, dict) else {}
            collaboration = operator_reasoning.get("collaboration_progress", {}) if isinstance(operator_reasoning.get("collaboration_progress", {}), dict) else {}
            self.assertEqual(str(collaboration.get("execution_id", "")).strip(), request_id, collaboration)
            self.assertEqual(str(collaboration.get("id_kind", "")).strip(), "bridge_request_id", collaboration)
            self.assertEqual(str(collaboration.get("task_id", "")).strip(), task_id, collaboration)
            self.assertEqual(str(collaboration.get("request_id", "")).strip(), request_id, collaboration)
            self.assertIn("no heartbeats recovery in progress", str(collaboration.get("summary", "")).strip())
            self.assertIn("bridge request", str(collaboration.get("summary", "")).strip())

            active_workstream = collaboration.get("active_workstream", {}) if isinstance(collaboration.get("active_workstream", {}), dict) else {}
            self.assertEqual(str(active_workstream.get("name", "")).strip(), "tod_recovery_progress", active_workstream)
            self.assertEqual(
                str(active_workstream.get("tod_status", "")).strip(),
                "no_heartbeats_recovery_in_progress",
                active_workstream,
            )

            conversation_context = state.get("conversation_context", {}) if isinstance(state.get("conversation_context", {}), dict) else {}
            self.assertEqual(
                str(conversation_context.get("tod_collaboration_execution_id", "")).strip(),
                request_id,
                conversation_context,
            )
            self.assertEqual(
                str(conversation_context.get("tod_collaboration_id_kind", "")).strip(),
                "bridge_request_id",
                conversation_context,
            )
            self.assertEqual(
                str(conversation_context.get("tod_collaboration_task_id", "")).strip(),
                task_id,
                conversation_context,
            )
            self.assertEqual(
                str(conversation_context.get("tod_collaboration_request_id", "")).strip(),
                request_id,
                conversation_context,
            )
            self.assertEqual(
                str(conversation_context.get("tod_collaboration_summary", "")).strip(),
                str(collaboration.get("summary", "")).strip(),
                conversation_context,
            )
        finally:
            self._restore_collaboration_progress_fixture(prior)

    def test_state_exposes_request_id_only_bridge_collaboration_progress(self) -> None:
        request_id = f"objective97-bridge-only-{uuid4().hex[:8]}"
        prior = self._write_collaboration_progress_fixture(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "type": "mim_tod_collaboration_progress_v1",
                "execution_id": request_id,
                "id_kind": "bridge_request_id",
                "execution_lane": "tod_bridge_request",
                "request_id": request_id,
                "owners": {
                    "mim": "publish_and_decision_owner",
                    "tod": "consume_and_execution_owner",
                },
                "workstreams": [
                    {
                        "id": 1,
                        "name": "consume_mutation_tracking",
                        "mim_status": "auto_watch_waiting_for_consume_mutation",
                        "tod_status": "awaiting_target_task_consume",
                        "latest_observation": f"watching for TOD ACK and RESULT mutation for bridge_request={request_id}",
                    }
                ],
            }
        )
        try:
            status, state = get_json("/mim/ui/state")
            self.assertEqual(status, 200, state)
            self.assertIn("tod_collaboration_progress", state.get("runtime_features", []))

            operator_reasoning = state.get("operator_reasoning", {}) if isinstance(state, dict) else {}
            collaboration = operator_reasoning.get("collaboration_progress", {}) if isinstance(operator_reasoning.get("collaboration_progress", {}), dict) else {}
            self.assertEqual(str(collaboration.get("execution_id", "")).strip(), request_id, collaboration)
            self.assertEqual(str(collaboration.get("id_kind", "")).strip(), "bridge_request_id", collaboration)
            self.assertEqual(str(collaboration.get("execution_lane", "")).strip(), "tod_bridge_request", collaboration)
            self.assertEqual(str(collaboration.get("request_id", "")).strip(), request_id, collaboration)
            self.assertEqual(str(collaboration.get("task_id", "")).strip(), "", collaboration)
            self.assertIn("bridge request", str(collaboration.get("summary", "")).strip())
            self.assertEqual(
                str(collaboration.get("execution_id_label", "")).strip(),
                f"bridge request {request_id}",
                collaboration,
            )

            conversation_context = state.get("conversation_context", {}) if isinstance(state.get("conversation_context", {}), dict) else {}
            self.assertEqual(
                str(conversation_context.get("tod_collaboration_execution_id", "")).strip(),
                request_id,
                conversation_context,
            )
            self.assertEqual(
                str(conversation_context.get("tod_collaboration_id_kind", "")).strip(),
                "bridge_request_id",
                conversation_context,
            )
            self.assertEqual(
                str(conversation_context.get("tod_collaboration_request_id", "")).strip(),
                request_id,
                conversation_context,
            )
            self.assertEqual(
                str(conversation_context.get("tod_collaboration_task_id", "")).strip(),
                "",
                conversation_context,
            )
        finally:
            self._restore_collaboration_progress_fixture(prior)

    def test_state_exposes_tod_decision_process_in_operator_reasoning(self) -> None:
        task_id = f"objective97-decision-task-{uuid4().hex[:8]}"
        prior = self._write_decision_process_fixture(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "state": "watching",
                "decision_process": {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "state": "watching",
                    "questions": {
                        "tod_knows_what_mim_did": {
                            "known": False,
                            "detail": "TOD has not yet acknowledged the latest MIM action",
                            "evidence": ["awaiting_ack"],
                        },
                        "mim_knows_what_tod_did": {
                            "known": True,
                            "detail": "TOD reported active review status",
                            "evidence": ["task_status_review"],
                        },
                        "tod_current_work": {
                            "known": True,
                            "task_id": task_id,
                            "objective_id": "objective-97",
                            "phase": "reviewing_request",
                            "detail": "TOD is reviewing the current request",
                        },
                        "tod_liveness": {
                            "status": "silent",
                            "ask_required": True,
                            "latest_progress_age_seconds": 180,
                            "ping_response_age_seconds": 75,
                            "primary_alert_code": "tod_silent",
                        },
                    },
                    "communication_escalation": {
                        "required": True,
                        "code": "tod_silent",
                        "detail": "TOD has gone silent long enough to require escalation",
                        "console_url": "http://192.168.1.161:8844",
                        "kick_hint": "ask_loudly",
                    },
                    "selected_action": {
                        "code": "ask_loudly",
                        "detail": "Ask TOD loudly for status",
                    },
                },
                "blocking_reason_codes": ["communication_escalation"],
            }
        )
        try:
            status, state = get_json("/mim/ui/state")
            self.assertEqual(status, 200, state)
            self.assertIn("tod_decision_process_visibility", state.get("runtime_features", []))

            operator_reasoning = state.get("operator_reasoning", {}) if isinstance(state, dict) else {}
            decision = operator_reasoning.get("tod_decision_process", {}) if isinstance(operator_reasoning.get("tod_decision_process", {}), dict) else {}
            self.assertEqual(str(decision.get("state", "")).strip(), "watching", decision)
            self.assertFalse(bool((decision.get("tod_knows_what_mim_did", {}) if isinstance(decision.get("tod_knows_what_mim_did", {}), dict) else {}).get("known")), decision)
            self.assertTrue(bool((decision.get("mim_knows_what_tod_did", {}) if isinstance(decision.get("mim_knows_what_tod_did", {}), dict) else {}).get("known")), decision)
            self.assertEqual(
                str((decision.get("tod_current_work", {}) if isinstance(decision.get("tod_current_work", {}), dict) else {}).get("task_id", "")).strip(),
                task_id,
                decision,
            )
            self.assertEqual(
                str((decision.get("tod_liveness", {}) if isinstance(decision.get("tod_liveness", {}), dict) else {}).get("status", "")).strip(),
                "silent",
                decision,
            )
            self.assertTrue(
                bool((decision.get("communication_escalation", {}) if isinstance(decision.get("communication_escalation", {}), dict) else {}).get("required")),
                decision,
            )
            self.assertEqual(
                str((decision.get("selected_action", {}) if isinstance(decision.get("selected_action", {}), dict) else {}).get("code", "")).strip(),
                "ask_loudly",
                decision,
            )
            self.assertIn("TOD does not know what MIM did", str(decision.get("summary", "")).strip())
            self.assertIn("TOD decision", str(operator_reasoning.get("summary", "")).strip())
        finally:
            self._restore_decision_process_fixture(prior)


if __name__ == "__main__":
    unittest.main(verbosity=2)