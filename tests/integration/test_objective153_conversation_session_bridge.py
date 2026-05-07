import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path


from tests.integration.runtime_target_guard import DEFAULT_BASE_URL


BASE_URL = os.getenv("MIM_TEST_BASE_URL", DEFAULT_BASE_URL)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def post_json(path: str, payload: dict, *, base_url: str = BASE_URL) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed if isinstance(parsed, dict) else {"data": parsed}


def get_json(path: str, query: dict | None = None, *, base_url: str = BASE_URL) -> tuple[int, dict | list]:
    url = f"{base_url}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed if isinstance(parsed, dict) else {"data": parsed}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _wait_for_http(base_url: str, *, timeout_seconds: float = 30.0) -> Exception | None:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            status, _ = get_json("/openapi.json", base_url=base_url)
            if status == 200:
                return None
        except Exception as exc:  # pragma: no cover - transient startup race
            last_error = exc
        time.sleep(0.25)
    return last_error


class Objective153ConversationSessionBridgeTest(unittest.TestCase):
    def _post_turn(self, session_id: str, text: str, *, base_url: str = BASE_URL) -> tuple[int, dict]:
        return post_json(
            "/gateway/intake/text",
            {
                "text": text,
                "parsed_intent": "discussion",
                "confidence": 0.94,
                "metadata_json": {
                    "conversation_session_id": session_id,
                    "user_id": "operator",
                },
            },
            base_url=base_url,
        )

    @contextmanager
    def _patched_self_evolution_runtime(
        self,
        *,
        snapshot_status: str,
        snapshot_summary: str,
        metadata_flag: str,
        decision_summary: str = "",
        decision_type: str = "",
        context_overrides: dict[str, object] | None = None,
    ):
        with tempfile.TemporaryDirectory(prefix="objective153-status-only-") as tmp_dir:
            tmp_path = Path(tmp_dir)
            module_path = tmp_path / "objective153_status_only_runtime_app.py"
            stdout_path = tmp_path / "uvicorn.stdout.log"
            stderr_path = tmp_path / "uvicorn.stderr.log"
            module_source = textwrap.dedent(
                """
                from core.routers import gateway


                async def _patched_build_self_evolution_briefing(*, actor, source, refresh, lookback_hours, min_occurrence_count, auto_experiment_limit, limit, db):
                    return {{
                        "briefing": {{
                            "snapshot": {{
                                "status": {snapshot_status},
                                "summary": {snapshot_summary},
                            }},
                            "decision": {{
                                "summary": {decision_summary},
                                "decision_type": {decision_type},
                            }},
                            "target": {{
                                "target_kind": "",
                                "target_id": None,
                                "proposal": None,
                                "recommendation": None,
                                "backlog_item": None,
                            }},
                            "metadata_json": {{
                                "objective166_self_evolution_briefing": True,
                                {metadata_flag}: True,
                            }},
                        }}
                    }}


                gateway.build_self_evolution_briefing = _patched_build_self_evolution_briefing

                _original_build_return_briefing_context = gateway._build_return_briefing_context


                async def _patched_build_return_briefing_context(db):
                    context = await _original_build_return_briefing_context(db)
                    context.update({context_overrides})
                    return context


                gateway._build_return_briefing_context = _patched_build_return_briefing_context

                from core.app import app

                app.router.on_startup.clear()
                """
            ).format(
                snapshot_status=repr(snapshot_status),
                snapshot_summary=repr(snapshot_summary),
                metadata_flag=repr(metadata_flag),
                decision_summary=repr(decision_summary),
                decision_type=repr(decision_type),
                context_overrides=repr(context_overrides or {}),
            )
            module_path.write_text(
                module_source.strip() + "\n",
                encoding="utf-8",
            )

            port = _free_port()
            env = os.environ.copy()
            env["PYTHONPATH"] = os.pathsep.join(
                [tmp_dir, str(PROJECT_ROOT), env.get("PYTHONPATH", "")]
            ).rstrip(os.pathsep)
            with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
                "w", encoding="utf-8"
            ) as stderr_handle:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "objective153_status_only_runtime_app:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--log-level",
                        "warning",
                    ],
                    cwd=PROJECT_ROOT,
                    env=env,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    text=True,
                )
                base_url = f"http://127.0.0.1:{port}"
                try:
                    startup_error = _wait_for_http(base_url)
                    if startup_error is not None:
                        stdout_handle.flush()
                        stderr_handle.flush()
                        stdout_text = stdout_path.read_text(encoding="utf-8") if stdout_path.exists() else ""
                        stderr_text = stderr_path.read_text(encoding="utf-8") if stderr_path.exists() else ""
                        raise AssertionError(
                            f"temporary integration runtime did not become ready at {base_url}: {startup_error}\n"
                            f"stdout:\n{stdout_text}\n"
                            f"stderr:\n{stderr_text}"
                        )
                    yield base_url
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)

    @contextmanager
    def _status_only_self_evolution_runtime(self):
        with self._patched_self_evolution_runtime(
            snapshot_status="quiet",
            snapshot_summary="",
            metadata_flag="objective153_status_only_runtime",
        ) as base_url:
            yield base_url

    @contextmanager
    def _unavailable_self_evolution_runtime(self):
        with self._patched_self_evolution_runtime(
            snapshot_status="",
            snapshot_summary="",
            metadata_flag="objective153_unavailable_runtime",
        ) as base_url:
            yield base_url

    @contextmanager
    def _missing_goal_partial_catchup_runtime(self):
        with self._patched_self_evolution_runtime(
            snapshot_status="active",
            snapshot_summary="Self-evolution is active with ranked backlog pressure.",
            metadata_flag="objective153_missing_goal_runtime",
            decision_summary="review the top-ranked improvement recommendation before continuing the loop",
            decision_type="approve_ranked_recommendation",
            context_overrides={
                "goal_description": "",
                "goal_status": "",
                "goal_id": 0,
                "goal_truth_status": "missing",
                "goal_age_hours": 0.0,
                "latest_goal_description": "",
                "latest_goal_status": "",
                "alignment_status": "partial",
            },
        ) as base_url:
            yield base_url

    @contextmanager
    def _stale_goal_runtime(self):
        with self._patched_self_evolution_runtime(
            snapshot_status="quiet",
            snapshot_summary="Self-evolution is quiet; no active ranked backlog pressure is present and the current loop is holding at proposals=0, recommendations=0.",
            metadata_flag="objective153_stale_goal_runtime",
            decision_summary="refresh the self-evolution snapshot to look for new governed improvement pressure",
            decision_type="refresh_self_evolution_state",
            context_overrides={
                "goal_description": "open the dashboard",
                "goal_status": "new",
                "goal_id": 1,
                "goal_truth_status": "stale",
                "goal_age_hours": 36.5,
                "latest_goal_description": "open the dashboard",
                "latest_goal_status": "new",
                "alignment_status": "stale",
            },
        ) as base_url:
            yield base_url

    @contextmanager
    def _conflicting_continuity_runtime(self):
        with self._patched_self_evolution_runtime(
            snapshot_status="operator_review_required",
            snapshot_summary="Self-evolution is active with 1 backlog item awaiting operator review; open proposals=1, open recommendations=1, top priority type=routine_zone_pattern.",
            metadata_flag="objective153_conflicting_runtime",
            decision_summary="review recommendation 12 for the top-ranked improvement item before continuing the loop",
            decision_type="approve_ranked_recommendation",
            context_overrides={
                "goal_description": "",
                "goal_status": "",
                "goal_id": 0,
                "goal_truth_status": "missing",
                "goal_age_hours": 0.0,
                "latest_goal_description": "finish overnight sync",
                "latest_goal_status": "completed",
                "alignment_status": "conflicting",
            },
        ) as base_url:
            yield base_url

    def _create_strategy(self, *, run_id: str, zone_suffix: str) -> int:
        status, payload = post_json(
            "/planning/strategies/generate",
            {
                "actor": "objective153-test",
                "source": "objective153-return-briefing",
                "observed_conditions": [
                    {
                        "condition_type": "routine_zone_pattern",
                        "target_scope": f"front-left-obj153-{run_id}-{zone_suffix}",
                        "severity": 0.84,
                        "occurrence_count": 2,
                        "metadata_json": {"run_id": run_id},
                    }
                ],
                "min_severity": 0.2,
                "max_strategies": 3,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, payload)
        strategies = payload.get("strategies", []) if isinstance(payload.get("strategies", []), list) else []
        self.assertGreaterEqual(len(strategies), 1, payload)
        strategy_id = int((strategies[0] or {}).get("strategy_id", 0))
        self.assertGreater(strategy_id, 0)
        return strategy_id

    def _deactivate_strategy(self, *, strategy_id: int, run_id: str) -> None:
        status, payload = post_json(
            f"/planning/strategies/{strategy_id}/deactivate",
            {
                "actor": "objective153-test",
                "reason": "objective153 return briefing synthetic stall pattern",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, payload)

    def test_gateway_text_turns_are_persisted_into_interface_session(self) -> None:
        session_id = f"objective153-{uuid.uuid4()}"
        encoded_session = urllib.parse.quote(session_id, safe="")

        status, payload = self._post_turn(
            session_id,
            "what should we prioritize next?",
        )
        self.assertEqual(status, 200, payload)
        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        self.assertTrue(str(resolution.get("clarification_prompt", "")).strip())

        status, session_payload = get_json(f"/interface/sessions/{encoded_session}")
        self.assertEqual(status, 200, session_payload)
        session = session_payload.get("session", {}) if isinstance(session_payload, dict) else {}
        context = session.get("context_json", {}) if isinstance(session, dict) else {}
        self.assertEqual(str(session.get("session_key", "")), session_id)
        self.assertEqual(str(session.get("channel", "")), "text")
        self.assertGreaterEqual(int(context.get("turn_count", 0) or 0), 2)
        self.assertEqual(
            str(context.get("last_user_input", "")),
            "what should we prioritize next?",
        )
        self.assertEqual(str(context.get("last_parsed_intent", "")), "discussion")
        self.assertEqual(str(context.get("last_internal_intent", "")), "speak_response")
        self.assertTrue(str(context.get("last_prompt", "")).strip())
        self.assertEqual(str(context.get("last_topic", "")), "priorities")
        self.assertTrue(bool(context.get("last_proposed_actions", [])))

        status, messages_payload = get_json(
            f"/interface/sessions/{encoded_session}/messages",
            {"limit": 20},
        )
        self.assertEqual(status, 200, messages_payload)
        messages = (
            messages_payload.get("messages", [])
            if isinstance(messages_payload, dict)
            else []
        )
        self.assertGreaterEqual(len(messages), 2, messages)
        relevant = [
            item
            for item in messages
            if isinstance(item, dict)
            and str(item.get("content", "")).strip()
            in {
                "what should we prioritize next?",
                str(context.get("last_prompt", "")).strip(),
            }
        ]
        self.assertGreaterEqual(len(relevant), 2, messages)

    def test_followup_retry_reuses_last_action_request_from_session_context(self) -> None:
        session_id = f"objective153-retry-{uuid.uuid4()}"
        encoded_session = urllib.parse.quote(session_id, safe="")

        status, first_payload = self._post_turn(session_id, "open the dashboard")
        self.assertEqual(status, 200, first_payload)

        status, followup_payload = self._post_turn(
            session_id,
            "do that again but slower",
        )
        self.assertEqual(status, 200, followup_payload)
        resolution = (
            followup_payload.get("resolution", {})
            if isinstance(followup_payload, dict)
            else {}
        )
        prompt = str(resolution.get("clarification_prompt", ""))
        self.assertIn("open the dashboard", prompt)
        self.assertIn("slower", prompt)

        status, session_payload = get_json(f"/interface/sessions/{encoded_session}")
        self.assertEqual(status, 200, session_payload)
        session = session_payload.get("session", {}) if isinstance(session_payload, dict) else {}
        context = session.get("context_json", {}) if isinstance(session, dict) else {}
        self.assertEqual(
            str(context.get("last_action_request", "")),
            "open the dashboard",
        )
        self.assertGreaterEqual(int(context.get("turn_count", 0) or 0), 4)
        self.assertIn(
            str(context.get("last_control_state", "")),
            {"active", "confirmed", "stopped"},
        )

    def test_confirm_followup_promotes_pending_session_action_into_goal(self) -> None:
        session_id = f"objective153-confirm-{uuid.uuid4()}"
        encoded_session = urllib.parse.quote(session_id, safe="")

        status, first_payload = self._post_turn(session_id, "open the dashboard")
        self.assertEqual(status, 200, first_payload)

        status, followup_payload = self._post_turn(
            session_id,
            "do that again but slower",
        )
        self.assertEqual(status, 200, followup_payload)

        status, confirm_payload = self._post_turn(session_id, "confirm")
        self.assertEqual(status, 200, confirm_payload)
        resolution = (
            confirm_payload.get("resolution", {})
            if isinstance(confirm_payload, dict)
            else {}
        )
        self.assertEqual(str(resolution.get("internal_intent", "")), "create_goal")
        self.assertEqual(str(resolution.get("outcome", "")), "auto_execute")
        self.assertTrue(int(resolution.get("goal_id", 0) or 0) > 0)
        self.assertIn(
            "retry open the dashboard at a slower pace",
            str(resolution.get("proposed_goal_description", "")),
        )
        self.assertIn(
            "Confirmed. I created a goal for:",
            str(resolution.get("clarification_prompt", "")),
        )

        status, session_payload = get_json(f"/interface/sessions/{encoded_session}")
        self.assertEqual(status, 200, session_payload)
        session = session_payload.get("session", {}) if isinstance(session_payload, dict) else {}
        context = session.get("context_json", {}) if isinstance(session, dict) else {}
        self.assertEqual(str(context.get("last_internal_intent", "")), "create_goal")
        self.assertEqual(str(context.get("last_control_state", "")), "confirmed")
        self.assertIn(
            "retry open the dashboard at a slower pace",
            str(context.get("pending_action_request", "")),
        )

    def test_revise_followup_replaces_pending_session_action_before_confirm(self) -> None:
        session_id = f"objective153-revise-{uuid.uuid4()}"

        status, first_payload = self._post_turn(session_id, "open the dashboard")
        self.assertEqual(status, 200, first_payload)

        status, revise_payload = self._post_turn(
            session_id,
            "change it to open the reports page",
        )
        self.assertEqual(status, 200, revise_payload)
        revise_resolution = (
            revise_payload.get("resolution", {})
            if isinstance(revise_payload, dict)
            else {}
        )
        self.assertIn(
            "updated the pending action",
            str(revise_resolution.get("clarification_prompt", "")).lower(),
        )

        status, confirm_payload = self._post_turn(session_id, "confirm")
        self.assertEqual(status, 200, confirm_payload)
        resolution = (
            confirm_payload.get("resolution", {})
            if isinstance(confirm_payload, dict)
            else {}
        )
        self.assertEqual(str(resolution.get("internal_intent", "")), "create_goal")
        self.assertIn(
            "open the reports page",
            str(resolution.get("proposed_goal_description", "")),
        )

    def test_cancel_followup_clears_pending_session_action_before_confirm(self) -> None:
        session_id = f"objective153-cancel-{uuid.uuid4()}"
        encoded_session = urllib.parse.quote(session_id, safe="")

        status, first_payload = self._post_turn(session_id, "open the dashboard")
        self.assertEqual(status, 200, first_payload)

        status, cancel_payload = self._post_turn(session_id, "cancel it")
        self.assertEqual(status, 200, cancel_payload)
        cancel_resolution = (
            cancel_payload.get("resolution", {})
            if isinstance(cancel_payload, dict)
            else {}
        )
        self.assertIn(
            "cancelled",
            str(cancel_resolution.get("clarification_prompt", "")).lower(),
        )

        status, confirm_payload = self._post_turn(session_id, "confirm")
        self.assertEqual(status, 200, confirm_payload)
        resolution = (
            confirm_payload.get("resolution", {})
            if isinstance(confirm_payload, dict)
            else {}
        )
        self.assertEqual(str(resolution.get("internal_intent", "")), "speak_response")
        self.assertEqual(str(resolution.get("outcome", "")), "store_only")
        self.assertFalse(bool(resolution.get("goal_id")))

        status, session_payload = get_json(f"/interface/sessions/{encoded_session}")
        self.assertEqual(status, 200, session_payload)
        session = session_payload.get("session", {}) if isinstance(session_payload, dict) else {}
        context = session.get("context_json", {}) if isinstance(session, dict) else {}
        self.assertEqual(str(context.get("last_control_state", "")), "cancelled")
        self.assertEqual(str(context.get("pending_action_request", "")), "")

    def test_pause_then_resume_controls_when_pending_action_can_be_confirmed(self) -> None:
        session_id = f"objective153-pause-{uuid.uuid4()}"
        encoded_session = urllib.parse.quote(session_id, safe="")

        status, first_payload = self._post_turn(session_id, "open the dashboard")
        self.assertEqual(status, 200, first_payload)

        status, pause_payload = self._post_turn(session_id, "pause")
        self.assertEqual(status, 200, pause_payload)
        pause_resolution = (
            pause_payload.get("resolution", {}) if isinstance(pause_payload, dict) else {}
        )
        self.assertIn("paused", str(pause_resolution.get("clarification_prompt", "")).lower())

        status, blocked_confirm_payload = self._post_turn(session_id, "confirm")
        self.assertEqual(status, 200, blocked_confirm_payload)
        blocked_resolution = (
            blocked_confirm_payload.get("resolution", {})
            if isinstance(blocked_confirm_payload, dict)
            else {}
        )
        self.assertEqual(str(blocked_resolution.get("internal_intent", "")), "speak_response")
        self.assertEqual(str(blocked_resolution.get("outcome", "")), "store_only")
        self.assertIn("say resume", str(blocked_resolution.get("clarification_prompt", "")).lower())

        status, session_payload = get_json(f"/interface/sessions/{encoded_session}")
        self.assertEqual(status, 200, session_payload)
        session = session_payload.get("session", {}) if isinstance(session_payload, dict) else {}
        context = session.get("context_json", {}) if isinstance(session, dict) else {}
        self.assertEqual(str(context.get("last_control_state", "")), "paused")
        self.assertEqual(str(context.get("pending_action_request", "")), "open the dashboard")

        status, resume_payload = self._post_turn(session_id, "resume")
        self.assertEqual(status, 200, resume_payload)

        status, confirm_payload = self._post_turn(session_id, "confirm")
        self.assertEqual(status, 200, confirm_payload)
        resolution = (
            confirm_payload.get("resolution", {})
            if isinstance(confirm_payload, dict)
            else {}
        )
        self.assertEqual(str(resolution.get("internal_intent", "")), "create_goal")
        self.assertEqual(str(resolution.get("outcome", "")), "auto_execute")
        self.assertTrue(int(resolution.get("goal_id", 0) or 0) > 0)

    def test_short_affirmation_approves_pending_action_from_clarification_state(self) -> None:
        session_id = f"objective153-affirm-{uuid.uuid4()}"

        status, first_payload = self._post_turn(session_id, "open the dashboard")
        self.assertEqual(status, 200, first_payload)

        status, approval_payload = self._post_turn(session_id, "yes")
        self.assertEqual(status, 200, approval_payload)
        resolution = (
            approval_payload.get("resolution", {})
            if isinstance(approval_payload, dict)
            else {}
        )
        self.assertEqual(str(resolution.get("internal_intent", "")), "create_goal")
        self.assertEqual(str(resolution.get("outcome", "")), "auto_execute")
        self.assertTrue(int(resolution.get("goal_id", 0) or 0) > 0)
        self.assertIn(
            "open the dashboard",
            str(resolution.get("proposed_goal_description", "")),
        )

    def test_precision_prompt_accepts_terse_status_followup(self) -> None:
        session_id = f"objective153-precision-{uuid.uuid4()}"

        status, first_payload = self._post_turn(session_id, "ok")
        self.assertEqual(status, 200, first_payload)
        first_resolution = (
            first_payload.get("resolution", {})
            if isinstance(first_payload, dict)
            else {}
        )
        self.assertEqual(
            str(first_resolution.get("reason", "")),
            "conversation_precision_prompt",
        )
        self.assertIn(
            "missing one detail",
            str(first_resolution.get("clarification_prompt", "")).lower(),
        )

        status, second_payload = self._post_turn(session_id, "status")
        self.assertEqual(status, 200, second_payload)
        second_resolution = (
            second_payload.get("resolution", {})
            if isinstance(second_payload, dict)
            else {}
        )
        self.assertEqual(
            str(second_resolution.get("reason", "")),
            "conversation_clarification_followup",
        )
        self.assertIn(
            "one-line status:",
            str(second_resolution.get("clarification_prompt", "")).lower(),
        )

    def test_precision_prompt_preserves_prior_topic_for_terse_after_followup(self) -> None:
        session_id = f"objective153-precision-topic-{uuid.uuid4()}"
        encoded_session = urllib.parse.quote(session_id, safe="")

        status, first_payload = self._post_turn(
            session_id,
            "what should we prioritize next?",
        )
        self.assertEqual(status, 200, first_payload)
        first_resolution = (
            first_payload.get("resolution", {})
            if isinstance(first_payload, dict)
            else {}
        )
        self.assertIn(
            "top priority today",
            str(first_resolution.get("clarification_prompt", "")).lower(),
        )

        status, second_payload = self._post_turn(session_id, "ok")
        self.assertEqual(status, 200, second_payload)
        second_resolution = (
            second_payload.get("resolution", {})
            if isinstance(second_payload, dict)
            else {}
        )
        self.assertEqual(
            str(second_resolution.get("reason", "")),
            "conversation_precision_prompt",
        )

        status, session_payload = get_json(f"/interface/sessions/{encoded_session}")
        self.assertEqual(status, 200, session_payload)
        session = session_payload.get("session", {}) if isinstance(session_payload, dict) else {}
        context = session.get("context_json", {}) if isinstance(session, dict) else {}
        self.assertEqual(str(context.get("last_topic", "")), "priorities")

        status, third_payload = self._post_turn(session_id, "after")
        self.assertEqual(status, 200, third_payload)
        third_resolution = (
            third_payload.get("resolution", {})
            if isinstance(third_payload, dict)
            else {}
        )
        self.assertEqual(
            str(third_resolution.get("reason", "")),
            "conversation_clarification_followup",
        )
        third_prompt = str(third_resolution.get("clarification_prompt", "")).lower()
        self.assertIn("after that", third_prompt)
        self.assertIn("regression", third_prompt)

    def test_precision_prompt_accepts_terse_recap_followup_from_prior_topic(self) -> None:
        session_id = f"objective153-precision-recap-{uuid.uuid4()}"

        status, first_payload = self._post_turn(
            session_id,
            "what should we prioritize next?",
        )
        self.assertEqual(status, 200, first_payload)

        status, second_payload = self._post_turn(session_id, "ok")
        self.assertEqual(status, 200, second_payload)
        second_resolution = (
            second_payload.get("resolution", {})
            if isinstance(second_payload, dict)
            else {}
        )
        self.assertEqual(
            str(second_resolution.get("reason", "")),
            "conversation_precision_prompt",
        )

        status, third_payload = self._post_turn(session_id, "recap")
        self.assertEqual(status, 200, third_payload)
        third_resolution = (
            third_payload.get("resolution", {})
            if isinstance(third_payload, dict)
            else {}
        )
        self.assertEqual(
            str(third_resolution.get("reason", "")),
            "conversation_clarification_followup",
        )
        third_prompt = str(third_resolution.get("clarification_prompt", "")).lower()
        self.assertIn("one line:", third_prompt)
        self.assertIn("stabilize routing", third_prompt)

    def test_direct_answer_followups_reuse_prior_topic_hints(self) -> None:
        session_id = f"objective153-direct-followups-{uuid.uuid4()}"

        status, first_payload = self._post_turn(
            session_id,
            "what should we prioritize next?",
        )
        self.assertEqual(status, 200, first_payload)

        status, second_payload = self._post_turn(session_id, "status")
        self.assertEqual(status, 200, second_payload)
        second_resolution = (
            second_payload.get("resolution", {})
            if isinstance(second_payload, dict)
            else {}
        )
        second_prompt = str(second_resolution.get("clarification_prompt", "")).lower()
        self.assertIn("routing", second_prompt)
        self.assertIn("handoff", second_prompt)

        status, third_payload = self._post_turn(session_id, "why")
        self.assertEqual(status, 200, third_payload)
        third_resolution = (
            third_payload.get("resolution", {})
            if isinstance(third_payload, dict)
            else {}
        )
        third_prompt = str(third_resolution.get("clarification_prompt", "")).lower()
        self.assertIn("routing", third_prompt)
        self.assertIn("handoff", third_prompt)

        status, fourth_payload = self._post_turn(session_id, "recap")
        self.assertEqual(status, 200, fourth_payload)
        fourth_resolution = (
            fourth_payload.get("resolution", {})
            if isinstance(fourth_payload, dict)
            else {}
        )
        fourth_prompt = str(fourth_resolution.get("clarification_prompt", "")).lower()
        self.assertIn("routing", fourth_prompt)
        self.assertIn("tests", fourth_prompt)

    def test_return_briefing_packages_goal_and_self_evolution_context(self) -> None:
        session_id = f"objective153-return-briefing-{uuid.uuid4()}"
        run_id = uuid.uuid4().hex[:8]

        strategy_ids = [
            self._create_strategy(run_id=run_id, zone_suffix="a"),
            self._create_strategy(run_id=run_id, zone_suffix="b"),
        ]
        for strategy_id in strategy_ids:
            self._deactivate_strategy(strategy_id=strategy_id, run_id=run_id)

        status, first_payload = self._post_turn(session_id, "open the dashboard")
        self.assertEqual(status, 200, first_payload)

        status, approval_payload = self._post_turn(session_id, "yes")
        self.assertEqual(status, 200, approval_payload)
        approval_resolution = (
            approval_payload.get("resolution", {})
            if isinstance(approval_payload, dict)
            else {}
        )
        self.assertEqual(str(approval_resolution.get("internal_intent", "")), "create_goal")

        status, briefing_payload = self._post_turn(session_id, "catch me up")
        self.assertEqual(status, 200, briefing_payload)
        briefing_resolution = (
            briefing_payload.get("resolution", {})
            if isinstance(briefing_payload, dict)
            else {}
        )
        self.assertEqual(str(briefing_resolution.get("outcome", "")), "store_only")
        prompt = str(briefing_resolution.get("clarification_prompt", "")).lower()
        self.assertIn("while you were away:", prompt)
        self.assertIn("current goal is", prompt)
        self.assertIn("open the dashboard", prompt)
        self.assertIn("recommended next step:", prompt)
        self.assertIn("self-evolution:", prompt)

    def test_return_briefing_reports_status_only_self_evolution_limit_through_full_bridge(self) -> None:
        session_id = f"objective153-return-briefing-status-only-{uuid.uuid4()}"

        with self._status_only_self_evolution_runtime() as base_url:
            status, first_payload = self._post_turn(
                session_id,
                "open the dashboard",
                base_url=base_url,
            )
            self.assertEqual(status, 200, first_payload)

            status, approval_payload = self._post_turn(
                session_id,
                "yes",
                base_url=base_url,
            )
            self.assertEqual(status, 200, approval_payload)
            approval_resolution = (
                approval_payload.get("resolution", {})
                if isinstance(approval_payload, dict)
                else {}
            )
            self.assertEqual(str(approval_resolution.get("internal_intent", "")), "create_goal")

            status, briefing_payload = self._post_turn(
                session_id,
                "catch me up",
                base_url=base_url,
            )
            self.assertEqual(status, 200, briefing_payload)
            briefing_resolution = (
                briefing_payload.get("resolution", {})
                if isinstance(briefing_payload, dict)
                else {}
            )
            self.assertEqual(str(briefing_resolution.get("outcome", "")), "store_only")
            prompt = str(briefing_resolution.get("clarification_prompt", "")).lower()
            self.assertIn("while you were away:", prompt)
            self.assertIn("current goal is", prompt)
            self.assertIn("open the dashboard", prompt)
            self.assertIn("self-evolution visibility is limited to status=quiet", prompt)
            self.assertIn("usable self-evolution summary or decision", prompt)
            self.assertNotIn("recommended next step:", prompt)

    def test_return_briefing_reports_self_evolution_unavailable_through_full_bridge(self) -> None:
        session_id = f"objective153-return-briefing-unavailable-{uuid.uuid4()}"

        with self._unavailable_self_evolution_runtime() as base_url:
            status, first_payload = self._post_turn(
                session_id,
                "open the dashboard",
                base_url=base_url,
            )
            self.assertEqual(status, 200, first_payload)

            status, approval_payload = self._post_turn(
                session_id,
                "yes",
                base_url=base_url,
            )
            self.assertEqual(status, 200, approval_payload)
            approval_resolution = (
                approval_payload.get("resolution", {})
                if isinstance(approval_payload, dict)
                else {}
            )
            self.assertEqual(str(approval_resolution.get("internal_intent", "")), "create_goal")

            status, briefing_payload = self._post_turn(
                session_id,
                "catch me up",
                base_url=base_url,
            )
            self.assertEqual(status, 200, briefing_payload)
            briefing_resolution = (
                briefing_payload.get("resolution", {})
                if isinstance(briefing_payload, dict)
                else {}
            )
            self.assertEqual(str(briefing_resolution.get("outcome", "")), "store_only")
            prompt = str(briefing_resolution.get("clarification_prompt", "")).lower()
            self.assertIn("while you were away:", prompt)
            self.assertIn("current goal is", prompt)
            self.assertIn("open the dashboard", prompt)
            self.assertIn("self-evolution guidance is currently unavailable", prompt)
            self.assertNotIn("recommended next step:", prompt)

    def test_return_briefing_reports_missing_goal_partial_catchup_through_full_bridge(self) -> None:
        session_id = f"objective153-return-briefing-missing-goal-{uuid.uuid4()}"

        with self._missing_goal_partial_catchup_runtime() as base_url:
            status, briefing_payload = self._post_turn(
                session_id,
                "catch me up",
                base_url=base_url,
            )
            self.assertEqual(status, 200, briefing_payload)
            briefing_resolution = (
                briefing_payload.get("resolution", {})
                if isinstance(briefing_payload, dict)
                else {}
            )
            self.assertEqual(str(briefing_resolution.get("outcome", "")), "store_only")
            prompt = str(briefing_resolution.get("clarification_prompt", "")).lower()
            self.assertIn("while you were away:", prompt)
            self.assertIn("i do not have a current active goal in the continuity state", prompt)
            self.assertIn("recommended next step:", prompt)
            self.assertIn("self-evolution:", prompt)
            self.assertIn("ranked backlog pressure", prompt)
            self.assertIn("partial catch-up only because the active-goal surface is unavailable", prompt)
            self.assertNotIn("current goal is", prompt)

    def test_return_briefing_reports_stale_goal_through_full_bridge(self) -> None:
        session_id = f"objective153-return-briefing-stale-goal-{uuid.uuid4()}"

        with self._stale_goal_runtime() as base_url:
            status, briefing_payload = self._post_turn(
                session_id,
                "catch me up",
                base_url=base_url,
            )
            self.assertEqual(status, 200, briefing_payload)
            briefing_resolution = (
                briefing_payload.get("resolution", {})
                if isinstance(briefing_payload, dict)
                else {}
            )
            self.assertEqual(str(briefing_resolution.get("outcome", "")), "store_only")
            prompt = str(briefing_resolution.get("clarification_prompt", "")).lower()
            self.assertIn("while you were away:", prompt)
            self.assertIn("active goal continuity may be stale", prompt)
            self.assertIn("open the dashboard", prompt)
            self.assertIn("36.5 hour(s) ago", prompt)
            self.assertIn("cannot honestly confirm", prompt)
            self.assertIn("self-evolution:", prompt)
            self.assertNotIn("current goal is", prompt)

    def test_return_briefing_reports_conflicting_continuity_through_full_bridge(self) -> None:
        session_id = f"objective153-return-briefing-conflicting-{uuid.uuid4()}"

        with self._conflicting_continuity_runtime() as base_url:
            status, briefing_payload = self._post_turn(
                session_id,
                "catch me up",
                base_url=base_url,
            )
            self.assertEqual(status, 200, briefing_payload)
            briefing_resolution = (
                briefing_payload.get("resolution", {})
                if isinstance(briefing_payload, dict)
                else {}
            )
            self.assertEqual(str(briefing_resolution.get("outcome", "")), "store_only")
            prompt = str(briefing_resolution.get("clarification_prompt", "")).lower()
            self.assertIn("while you were away:", prompt)
            self.assertIn("continuity inputs are not fully aligned", prompt)
            self.assertIn("last stored goal was finish overnight sync", prompt)
            self.assertIn("self-evolution is currently", prompt)
            self.assertIn("recommended next step:", prompt)
            self.assertIn("do not have enough aligned continuity state", prompt)
            self.assertNotIn("current goal is", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)