import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.execution_readiness_service import load_latest_execution_readiness


class ExecutionReadinessServiceTest(unittest.TestCase):
    def test_ignores_stale_readiness_superseded_by_newer_same_task_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            task_result_path = tmp_path / "TOD_MIM_TASK_RESULT.latest.json"
            command_status_path = tmp_path / "TOD_MIM_COMMAND_STATUS.latest.json"

            task_result_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-02T18:51:35Z",
                        "request_id": "objective-2900-task-7117",
                        "task_id": "objective-2900-task-7117",
                        "objective_id": "2900",
                        "execution_readiness": {
                            "status": "stale",
                            "source": "artifact_stale",
                            "detail": "Execution readiness artifact is older than policy allows.",
                            "valid": False,
                            "execution_allowed": False,
                            "policy_outcome": "degrade",
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            command_status_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-02T18:51:36Z",
                        "request_id": "objective-2900-task-7117",
                        "task_id": "objective-2900-task-7117",
                        "objective_id": "2900",
                        "status": "already_processed",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            with patch(
                "core.execution_readiness_service.settings.execution_readiness_task_result_path",
                str(task_result_path),
            ), patch(
                "core.execution_readiness_service.settings.execution_readiness_command_status_path",
                str(command_status_path),
            ):
                readiness = load_latest_execution_readiness(
                    action="mim_ui_state",
                    capability_name="mim_ui_state",
                    managed_scope="global",
                    requested_executor="tod",
                )

        self.assertEqual(readiness["status"], "missing")
        self.assertEqual(readiness["source"], "readiness_signal_unavailable")

    def test_uses_latest_readiness_when_not_superseded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            task_result_path = tmp_path / "TOD_MIM_TASK_RESULT.latest.json"
            command_status_path = tmp_path / "TOD_MIM_COMMAND_STATUS.latest.json"

            task_result_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-02T18:51:35Z",
                        "request_id": "objective-2900-task-7117",
                        "task_id": "objective-2900-task-7117",
                        "objective_id": "2900",
                        "execution_readiness": {
                            "status": "stale",
                            "source": "artifact_stale",
                            "detail": "Execution readiness artifact is older than policy allows.",
                            "valid": False,
                            "execution_allowed": False,
                            "policy_outcome": "degrade",
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            command_status_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-02T18:51:34Z",
                        "request_id": "mim-day-02-live-resume-refresh-20260502",
                        "task_id": "objective-2900-task-7117",
                        "objective_id": "2900",
                        "status": "already_processed",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            with patch(
                "core.execution_readiness_service.settings.execution_readiness_task_result_path",
                str(task_result_path),
            ), patch(
                "core.execution_readiness_service.settings.execution_readiness_command_status_path",
                str(command_status_path),
            ):
                readiness = load_latest_execution_readiness(
                    action="mim_ui_state",
                    capability_name="mim_ui_state",
                    managed_scope="global",
                    requested_executor="tod",
                )

        self.assertEqual(readiness["status"], "stale")
        self.assertEqual(readiness["artifact_name"], "TOD_MIM_TASK_RESULT.latest.json")

    def test_newer_mismatched_task_does_not_supersede_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            task_result_path = tmp_path / "TOD_MIM_TASK_RESULT.latest.json"
            command_status_path = tmp_path / "TOD_MIM_COMMAND_STATUS.latest.json"

            task_result_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-02T18:51:35Z",
                        "request_id": "objective-2900-task-7117",
                        "task_id": "objective-2900-task-7117",
                        "objective_id": "2900",
                        "execution_readiness": {
                            "status": "valid",
                            "source": "fresh",
                            "detail": "Same-task readiness remains valid.",
                            "valid": True,
                            "execution_allowed": True,
                            "policy_outcome": "allow",
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            command_status_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-02T18:51:36Z",
                        "request_id": "objective-2900-task-9999",
                        "task_id": "objective-2900-task-9999",
                        "objective_id": "2900",
                        "status": "already_processed",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            with patch(
                "core.execution_readiness_service.settings.execution_readiness_task_result_path",
                str(task_result_path),
            ), patch(
                "core.execution_readiness_service.settings.execution_readiness_command_status_path",
                str(command_status_path),
            ):
                readiness = load_latest_execution_readiness(
                    action="mim_ui_state",
                    capability_name="mim_ui_state",
                    managed_scope="global",
                    requested_executor="tod",
                )

        self.assertEqual(readiness["status"], "valid")
        self.assertEqual(readiness["artifact_name"], "TOD_MIM_TASK_RESULT.latest.json")