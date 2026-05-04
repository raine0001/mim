import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
EXPORT_SCRIPT = ROOT / "scripts" / "export_mim_context.py"
GATE_SCRIPT = ROOT / "scripts" / "validate_mim_tod_gate.sh"


def load_export_module():
    spec = importlib.util.spec_from_file_location("export_mim_context", EXPORT_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def workspace_manifest_sources(export_module):
    sources = list(export_module.WORKSPACE_RUNTIME_MANIFEST_SOURCES)
    if not sources:
        raise AssertionError("expected at least one workspace runtime manifest source")
    current_runtime_manifest = sources[0]
    workspace_runtime_manifest = (
        sources[-1]
        if len(sources) > 1
        else "http://127.0.0.1:18003/manifest"
    )
    return current_runtime_manifest, workspace_runtime_manifest


class Objective75InterfaceHardeningTest(unittest.TestCase):
    def _write_shared_truth_bundle(self, shared_dir: Path) -> None:
        handshake_payload = {
            "truth": {
                "objective_active": "75",
                "schema_version": "2026-03-12-68",
                "release_tag": "objective-75",
            }
        }
        manifest_payload = {
            "manifest": {
                "schema_version": "2026-03-12-68",
                "release_tag": "objective-75",
            }
        }
        (shared_dir / "MIM_TOD_HANDSHAKE_PACKET.latest.json").write_text(
            json.dumps(handshake_payload, indent=2) + "\n",
            encoding="utf-8",
        )
        (shared_dir / "MIM_MANIFEST.latest.json").write_text(
            json.dumps(manifest_payload, indent=2) + "\n",
            encoding="utf-8",
        )

    def test_build_payload_bundle_prefers_intended_workspace_manifest_truth_over_stale_prod(
        self,
    ) -> None:
        export_module = load_export_module()
        current_runtime_manifest, workspace_runtime_manifest = workspace_manifest_sources(
            export_module
        )
        prod_runtime_manifest = export_module.PROD_RUNTIME_MANIFEST_SOURCE

        stale_prod_manifest = {
            "schema_version": "2026-03-12-67",
            "release_tag": "objective-22-fallback-timeouts",
            "contract_version": "tod-mim-shared-contract-v1",
            "capabilities": ["manifest"],
        }
        workspace_manifest = {
            "schema_version": "2026-03-12-67",
            "release_tag": "workspace-dev",
            "contract_version": "tod-mim-shared-contract-v1",
            "capabilities": ["manifest", "status"],
        }

        def fake_fetch(url: str, timeout: float = 2.5):
            if url == current_runtime_manifest:
                return None
            if url == workspace_runtime_manifest:
                return workspace_manifest
            if url == prod_runtime_manifest:
                return stale_prod_manifest
            if url.endswith("/health"):
                return {"status": "ok"}
            return None

        with patch.object(
            export_module,
            "WORKSPACE_RUNTIME_MANIFEST_SOURCES",
            [current_runtime_manifest, workspace_runtime_manifest],
        ):
            with patch.object(
                export_module,
                "_parse_objective_index",
                return_value=("75", "implemented", "80", "implemented", "80", "implemented"),
            ):
                with patch.object(
                    export_module,
                    "_parse_objective_docs",
                    return_value=("75", "implemented", "80", "implemented", "implemented"),
                ):
                    with patch.object(
                        export_module,
                        "_latest_live_task_request_signal",
                        return_value={
                            "source": "runtime/shared/MIM_TOD_TASK_REQUEST.latest.json",
                            "objective": None,
                            "task_id": "",
                            "available": False,
                        },
                    ):
                        with patch.object(export_module, "_fetch_json", side_effect=fake_fetch):
                            payload, manifest = export_module.build_payload_bundle()

        source_of_truth = payload["source_of_truth"]
        self.assertEqual(
            source_of_truth["manifest_base_source_used"],
            workspace_runtime_manifest,
        )
        self.assertEqual(
            source_of_truth["objective_target"]["source"],
            "docs/objective-80-execution-truth-convergence.md",
        )
        self.assertEqual(payload["objective_active"], "80")
        self.assertEqual(payload["schema_version"], "2026-03-12-68")
        self.assertEqual(payload["release_tag"], "objective-80")
        self.assertEqual(manifest["schema_version"], "2026-03-12-68")
        self.assertEqual(manifest["release_tag"], "objective-80")
        self.assertIn(
            "applied in-flight objective target metadata",
            source_of_truth["manifest_source_selection_reason"],
        )

    def test_build_payload_bundle_promotes_live_task_request_when_ahead_of_docs(self) -> None:
        export_module = load_export_module()
        current_runtime_manifest, workspace_runtime_manifest = workspace_manifest_sources(
            export_module
        )

        workspace_manifest = {
            "schema_version": "2026-03-12-67",
            "release_tag": "workspace-dev",
            "contract_version": "tod-mim-shared-contract-v1",
            "capabilities": ["manifest", "status"],
        }

        def fake_fetch(url: str, timeout: float = 2.5):
            if url == current_runtime_manifest:
                return None
            if url == workspace_runtime_manifest:
                return workspace_manifest
            if url.endswith("/health"):
                return {"status": "ok"}
            return None

        with patch.object(
            export_module,
            "WORKSPACE_RUNTIME_MANIFEST_SOURCES",
            [current_runtime_manifest, workspace_runtime_manifest],
        ):
            with patch.object(
                export_module,
                "_parse_objective_index",
                return_value=("80", "implemented", "81", "implemented", "81", "implemented"),
            ):
                with patch.object(
                    export_module,
                    "_parse_objective_docs",
                    return_value=("80", "implemented", "81", "implemented", "implemented"),
                ):
                    with patch.object(
                        export_module,
                        "_active_formal_program_truth",
                        return_value=None,
                    ):
                        with patch.object(
                            export_module,
                            "_live_initiative_truth",
                            return_value=None,
                        ):
                            with patch.object(
                                export_module,
                                "_latest_live_task_request_signal",
                                return_value={
                                    "source": "runtime/shared/MIM_TOD_TASK_REQUEST.latest.json",
                                    "objective": "88",
                                    "task_id": "objective-88-task-001",
                                    "available": True,
                                },
                            ):
                                with patch.object(export_module, "_fetch_json", side_effect=fake_fetch):
                                    payload, _ = export_module.build_payload_bundle()

        self.assertEqual(payload["objective_active"], "88")
        self.assertEqual(payload["current_next_objective"], "88")
        self.assertEqual(
            payload["source_of_truth"]["objective_active_source"],
            "live_task_request",
        )

    def test_build_payload_bundle_ignores_continuous_dispatch_loop_for_objective_authority(self) -> None:
        export_module = load_export_module()
        current_runtime_manifest, workspace_runtime_manifest = workspace_manifest_sources(
            export_module
        )

        workspace_manifest = {
            "schema_version": "2026-03-12-67",
            "release_tag": "workspace-dev",
            "contract_version": "tod-mim-shared-contract-v1",
            "capabilities": ["manifest", "status"],
        }

        def fake_fetch(url: str, timeout: float = 2.5):
            if url == current_runtime_manifest:
                return None
            if url == workspace_runtime_manifest:
                return workspace_manifest
            if url.endswith("/health"):
                return {"status": "ok"}
            return None

        with patch.object(
            export_module,
            "WORKSPACE_RUNTIME_MANIFEST_SOURCES",
            [current_runtime_manifest, workspace_runtime_manifest],
        ):
            with patch.object(
                export_module,
                "_parse_objective_index",
                return_value=("152", "promoted_verified", "2912", "implemented", "2912", "implemented"),
            ):
                with patch.object(
                    export_module,
                    "_parse_objective_docs",
                    return_value=("152", "promoted_verified", "2912", "implemented", "implemented"),
                ):
                    with patch.object(
                        export_module,
                        "_active_formal_program_truth",
                        return_value={
                            "objective": "2900",
                            "execution_state": "executing",
                            "source": "runtime/formal_program_drive_response.json",
                        },
                    ):
                        with patch.object(
                            export_module,
                            "_live_initiative_truth",
                            return_value=None,
                        ):
                            with patch.object(
                                export_module,
                                "_latest_live_task_request_signal",
                                return_value={
                                    "source": "runtime/shared/MIM_TOD_TASK_REQUEST.latest.json",
                                    "objective": "2912",
                                    "task_id": "objective-2912-task-008",
                                    "available": True,
                                    "source_service": "continuous_task_dispatch",
                                    "title": "Continuous dispatch sample 8",
                                    "scope": "Execute one standard MIM->TOD loop cycle and publish ACK/RESULT.",
                                    "objective_authority_eligible": False,
                                    "suppression_reason": "non_authoritative_continuous_dispatch_loop",
                                },
                            ):
                                with patch.object(export_module, "_fetch_json", side_effect=fake_fetch):
                                    payload, _ = export_module.build_payload_bundle()

        self.assertEqual(payload["objective_in_flight"], "2900")
        self.assertEqual(payload["objective_active"], "2900")
        self.assertEqual(payload["current_next_objective"], "2900")
        self.assertEqual(
            payload["source_of_truth"]["objective_active_source"],
            "formal_program_truth",
        )
        self.assertFalse(
            payload["source_of_truth"]["live_task_request_signal"]["objective_authority_eligible"]
        )
        self.assertEqual(
            payload["source_of_truth"]["live_task_request_signal"]["suppression_reason"],
            "non_authoritative_continuous_dispatch_loop",
        )

    def test_build_payload_bundle_honors_objective_authority_reset_ceiling(self) -> None:
        export_module = load_export_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir)
            authority_reset_path = shared_dir / "OBJECTIVE_AUTHORITY_RESET.latest.json"
            authority_reset_path.write_text(
                json.dumps(
                    {
                        "objective_ceiling": "152",
                        "rewrite_completion_history": False,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.object(
                export_module,
                "AUTHORITY_RESET_ARTIFACT_CANDIDATES",
                (authority_reset_path,),
            ):
                with patch.object(
                    export_module,
                    "_parse_objective_index",
                    return_value=(
                        "152",
                        "promoted_verified",
                        "170",
                        "implemented",
                        "171",
                        "implemented",
                    ),
                ):
                    with patch.object(
                        export_module,
                        "_parse_objective_docs",
                        return_value=(
                            "152",
                            "promoted_verified",
                            "170",
                            "implemented",
                            "implemented",
                        ),
                    ):
                        with patch.object(
                            export_module,
                            "_latest_live_task_request_signal",
                            return_value={
                                "source": "runtime/shared/MIM_TOD_TASK_REQUEST.latest.json",
                                "objective": "170",
                                "task_id": "objective-170-task-001",
                                "available": True,
                            },
                        ):
                            with patch.object(
                                export_module,
                                "_fetch_json",
                                return_value={"status": "ok"},
                            ):
                                payload, manifest = export_module.build_payload_bundle()

        self.assertEqual(payload["objective_active"], "152")
        self.assertIsNone(payload["objective_in_flight"])
        self.assertEqual(payload["current_next_objective"], "152")
        self.assertEqual(
            payload["source_of_truth"]["objective_active_source"],
            "objective_authority_reset",
        )
        self.assertEqual(
            payload["source_of_truth"]["objective_target"]["objective"],
            "152",
        )
        self.assertEqual(manifest["release_tag"], "objective-152")
        self.assertEqual(
            payload["source_of_truth"]["objective_authority_reset"]["objective_ceiling"],
            "152",
        )

    def test_build_payload_bundle_infers_authority_reset_from_publication_boundary(self) -> None:
        export_module = load_export_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir)
            boundary_path = shared_dir / "MIM_TOD_PUBLICATION_BOUNDARY.latest.json"
            boundary_path.write_text(
                json.dumps(
                    {
                        "authoritative_request": {
                            "objective_id": "objective-152",
                            "request_id": "objective-152-task-001",
                        }
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.object(export_module, "DEFAULT_OUTPUT_DIR", shared_dir):
                with patch.object(
                    export_module,
                    "_parse_objective_index",
                    return_value=(
                        "152",
                        "promoted_verified",
                        "170",
                        "implemented",
                        "171",
                        "implemented",
                    ),
                ):
                    with patch.object(
                        export_module,
                        "_parse_objective_docs",
                        return_value=(
                            "152",
                            "promoted_verified",
                            "170",
                            "implemented",
                            "implemented",
                        ),
                    ):
                        with patch.object(
                            export_module,
                            "_latest_live_task_request_signal",
                            return_value={
                                "source": "runtime/shared/MIM_TOD_TASK_REQUEST.latest.json",
                                "objective": None,
                                "task_id": "",
                                "available": False,
                            },
                        ):
                            with patch.object(
                                export_module,
                                "_fetch_json",
                                return_value={"status": "ok"},
                            ):
                                payload, manifest = export_module.build_payload_bundle(
                                    output_dir=shared_dir
                                )

        self.assertEqual(payload["objective_active"], "152")
        self.assertIsNone(payload["objective_in_flight"])
        self.assertEqual(payload["current_next_objective"], "152")
        self.assertEqual(
            payload["source_of_truth"]["objective_active_source"],
            "objective_authority_reset",
        )
        self.assertEqual(
            payload["source_of_truth"]["objective_authority_reset"]["objective_ceiling"],
            "152",
        )
        self.assertEqual(
            payload["source_of_truth"]["objective_authority_reset"]["inferred_from"],
            "publication_boundary_authoritative_request",
        )
        self.assertEqual(manifest["release_tag"], "objective-152")

    def test_build_payload_bundle_skips_boundary_reset_for_completed_promotion_ready_request(self) -> None:
        export_module = load_export_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir)
            boundary_path = shared_dir / "MIM_TOD_PUBLICATION_BOUNDARY.latest.json"
            boundary_path.write_text(
                json.dumps(
                    {
                        "authoritative_request": {
                            "objective_id": "objective-152",
                            "request_id": "objective-152-task-smoke-20260418214904",
                        }
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_TASK_STATUS_REVIEW.latest.json").write_text(
                json.dumps(
                    {
                        "state": "completed",
                        "task": {
                            "active_task_id": "objective-152-task-smoke-20260418214904",
                            "authoritative_task_id": "objective-152-task-smoke-20260418214904",
                            "objective_id": "152",
                        },
                        "gate": {"pass": True, "promotion_ready": True},
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.object(export_module, "DEFAULT_OUTPUT_DIR", shared_dir):
                with patch.object(
                    export_module,
                    "_parse_objective_index",
                    return_value=(
                        "152",
                        "promoted_verified",
                        "170",
                        "implemented",
                        "153",
                        "implemented",
                    ),
                ):
                    with patch.object(
                        export_module,
                        "_parse_objective_docs",
                        return_value=(
                            "152",
                            "promoted_verified",
                            "170",
                            "implemented",
                            "implemented",
                        ),
                    ):
                        with patch.object(
                            export_module,
                            "_latest_live_task_request_signal",
                            return_value={
                                "source": "runtime/shared/MIM_TOD_TASK_REQUEST.latest.json",
                                "objective": "152",
                                "task_id": "objective-152-task-smoke-20260418214904",
                                "available": True,
                            },
                        ):
                            with patch.object(
                                export_module,
                                "_fetch_json",
                                return_value={"status": "ok"},
                            ):
                                payload, _ = export_module.build_payload_bundle(
                                    output_dir=shared_dir
                                )

        self.assertEqual(payload["objective_active"], "153")
        self.assertEqual(payload["current_next_objective"], "153")
        self.assertIsNone(payload["source_of_truth"]["objective_authority_reset"])
        self.assertTrue(
            payload["source_of_truth"]["live_task_request_signal"]["terminal_completed_request"]
        )
        self.assertEqual(
            payload["source_of_truth"]["terminal_request_review"]["reason"],
            "completed_gate_passing_request",
        )

    def test_build_payload_bundle_suppresses_completed_request_when_stale_guard_has_newer_objective(
        self,
    ) -> None:
        export_module = load_export_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir)
            (shared_dir / "MIM_TASK_STATUS_REVIEW.latest.json").write_text(
                json.dumps(
                    {
                        "state": "completed",
                        "task": {
                            "active_task_id": "objective-216-task-008",
                            "request_task_id": "objective-216-task-008",
                            "objective_id": "216",
                        },
                        "gate": {"pass": True, "promotion_ready": True},
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_MIM_COMMAND_STATUS.latest.json").write_text(
                json.dumps(
                    {
                        "request_id": "objective-216-task-008",
                        "task_id": "objective-216-task-008",
                        "stale_guard": {
                            "detected": True,
                            "decision": "stale_request_ignored",
                            "reason": "higher_authoritative_task_ordinal_active",
                            "objective_id": "220",
                            "current_request": {
                                "request_id": "objective-220-task-008",
                                "task_id": "objective-220-task-008",
                            },
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.object(
                export_module,
                "_parse_objective_index",
                return_value=(
                    "152",
                    "promoted_verified",
                    "153",
                    "implemented",
                    "153",
                    "implemented",
                ),
            ):
                with patch.object(
                    export_module,
                    "_parse_objective_docs",
                    return_value=(
                        "152",
                        "promoted_verified",
                        "153",
                        "implemented",
                        "implemented",
                    ),
                ):
                    with patch.object(
                        export_module,
                        "_latest_live_task_request_signal",
                        return_value={
                            "source": "runtime/shared/MIM_TOD_TASK_REQUEST.latest.json",
                            "objective": "216",
                            "task_id": "objective-216-task-008",
                            "available": True,
                        },
                    ):
                        with patch.object(
                            export_module,
                            "_active_formal_program_truth",
                            return_value={
                                "objective": "200",
                                "execution_state": "executing",
                                "source": "runtime/formal_program_drive_response.json",
                            },
                        ):
                            with patch.object(
                                export_module,
                                "_live_initiative_truth",
                                return_value=None,
                            ):
                                with patch.object(
                                    export_module,
                                    "_fetch_json",
                                    return_value={"status": "ok"},
                                ):
                                    payload, _ = export_module.build_payload_bundle(
                                        output_dir=shared_dir
                                    )

        self.assertEqual(payload["objective_active"], "220")
        self.assertEqual(payload["current_next_objective"], "220")
        self.assertEqual(
            payload["source_of_truth"]["objective_active_source"],
            "command_status_stale_guard",
        )
        self.assertTrue(
            payload["source_of_truth"]["live_task_request_signal"]["terminal_completed_request"]
        )
        self.assertEqual(
            payload["source_of_truth"]["live_task_request_signal"]["authoritative_objective"],
            "220",
        )
        self.assertEqual(
            payload["source_of_truth"]["terminal_request_review"]["reason"],
            "stale_guard_higher_authoritative_request",
        )

    def test_build_payload_bundle_prefers_output_dir_over_fallback_authority_reset(self) -> None:
        export_module = load_export_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output_dir = root / "shared"
            fallback_dir = root / "fallback"
            output_dir.mkdir(parents=True, exist_ok=True)
            fallback_dir.mkdir(parents=True, exist_ok=True)

            (fallback_dir / "OBJECTIVE_AUTHORITY_RESET.latest.json").write_text(
                json.dumps(
                    {
                        "objective_ceiling": "152",
                        "rewrite_completion_history": False,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.object(
                export_module,
                "AUTHORITY_RESET_ARTIFACT_CANDIDATES",
                (fallback_dir / "OBJECTIVE_AUTHORITY_RESET.latest.json",),
            ):
                with patch.object(
                    export_module,
                    "_parse_objective_index",
                    return_value=(
                        "152",
                        "promoted_verified",
                        "170",
                        "implemented",
                        "171",
                        "implemented",
                    ),
                ):
                    with patch.object(
                        export_module,
                        "_parse_objective_docs",
                        return_value=(
                            "152",
                            "promoted_verified",
                            "170",
                            "implemented",
                            "implemented",
                        ),
                    ):
                        with patch.object(
                            export_module,
                            "_latest_live_task_request_signal",
                            return_value={
                                "source": "runtime/shared/MIM_TOD_TASK_REQUEST.latest.json",
                                "objective": None,
                                "task_id": "",
                                "available": False,
                            },
                        ):
                            with patch.object(
                                export_module,
                                "_live_initiative_truth",
                                return_value=None,
                            ):
                                with patch.object(
                                    export_module,
                                    "_latest_live_task_request_signal",
                                    return_value={
                                        "source": "runtime/shared/MIM_TOD_TASK_REQUEST.latest.json",
                                        "objective": None,
                                        "task_id": "",
                                        "available": False,
                                    },
                                ):
                                    with patch.object(
                                        export_module,
                                        "_live_initiative_truth",
                                        return_value=None,
                                    ):
                                        with patch.object(
                                            export_module,
                                            "_fetch_json",
                                            return_value={"status": "ok"},
                                        ):
                                            payload, _ = export_module.build_payload_bundle(
                                                output_dir=output_dir
                                            )

        self.assertEqual(payload["objective_active"], "170")
        self.assertEqual(payload["current_next_objective"], "170")
        self.assertEqual(
            payload["source_of_truth"]["objective_active_source"],
            "objective_index_or_docs",
        )
        self.assertIsNone(payload["source_of_truth"]["objective_authority_reset"])

    def test_build_payload_bundle_ignores_inactive_authority_reset_metadata(self) -> None:
        export_module = load_export_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "shared"
            output_dir.mkdir(parents=True, exist_ok=True)
            isolated_formal_response = Path(tmp_dir) / "missing_formal_program_drive_response.json"

            (output_dir / "OBJECTIVE_AUTHORITY_RESET.latest.json").write_text(
                json.dumps(
                    {
                        "active": False,
                        "authoritative_objective": "216",
                        "metadata": {
                            "rollback_to_objective": "216",
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"FORMAL_PROGRAM_RESPONSE_PATH": str(isolated_formal_response)}):
                with patch.object(
                    export_module,
                    "_parse_objective_index",
                    return_value=(
                        "152",
                        "promoted_verified",
                        "170",
                        "implemented",
                        "171",
                        "implemented",
                    ),
                ):
                    with patch.object(
                        export_module,
                        "_parse_objective_docs",
                        return_value=(
                            "152",
                            "promoted_verified",
                            "170",
                            "implemented",
                            "implemented",
                        ),
                    ):
                        with patch.object(
                            export_module,
                            "_latest_live_task_request_signal",
                            return_value={
                                "source": "runtime/shared/MIM_TOD_TASK_REQUEST.latest.json",
                                "objective": None,
                                "task_id": "",
                                "available": False,
                            },
                        ):
                            with patch.object(
                                export_module,
                                "_live_initiative_truth",
                                return_value=None,
                            ):
                                with patch.object(
                                    export_module,
                                    "_fetch_json",
                                    return_value={"status": "ok"},
                                ):
                                    payload, _ = export_module.build_payload_bundle(
                                        output_dir=output_dir
                                    )

        self.assertEqual(payload["objective_active"], "170")
        self.assertEqual(payload["current_next_objective"], "170")
        self.assertIsNone(payload["source_of_truth"]["objective_authority_reset"])

    def test_build_payload_bundle_overrides_stale_runtime_schema_with_static_schema(self) -> None:
        export_module = load_export_module()
        current_runtime_manifest, workspace_runtime_manifest = workspace_manifest_sources(
            export_module
        )
        prod_runtime_manifest = export_module.PROD_RUNTIME_MANIFEST_SOURCE

        workspace_manifest = {
            "schema_version": "2026-03-12-67",
            "release_tag": "workspace-dev",
            "contract_version": "tod-mim-shared-contract-v1",
            "capabilities": ["manifest", "status"],
        }

        def fake_fetch(url: str, timeout: float = 2.5):
            if url == current_runtime_manifest:
                return None
            if url == workspace_runtime_manifest:
                return workspace_manifest
            if url == prod_runtime_manifest:
                return None
            if url.endswith("/health"):
                return {"status": "ok"}
            return None

        with patch.object(
            export_module,
            "WORKSPACE_RUNTIME_MANIFEST_SOURCES",
            [current_runtime_manifest, workspace_runtime_manifest],
        ):
            with patch.object(export_module, "_fetch_json", side_effect=fake_fetch):
                payload, manifest = export_module.build_payload_bundle()

        self.assertEqual(payload["schema_version"], "2026-03-24-70")
        self.assertEqual(manifest["schema_version"], "2026-03-24-70")
        self.assertIn(
            "overrode stale runtime/shared schema metadata with newer static schema_version 2026-03-24-70",
            payload["source_of_truth"]["manifest_source_selection_reason"],
        )

    def test_write_exports_refreshes_bridge_artifacts_from_same_truth_bundle(
        self,
    ) -> None:
        export_module = load_export_module()
        payload = {
            "exported_at": "2026-03-23T20:30:00Z",
            "objective_active": "75",
            "latest_completed_objective": "74",
            "current_next_objective": "75",
            "schema_version": "2026-03-12-68",
            "release_tag": "objective-75",
            "blockers": [],
            "verification": {
                "regression_status": "PASS",
                "regression_tests": "74/74",
                "prod_promotion_status": "SUCCESS",
                "prod_smoke_status": "PASS",
            },
            "source_of_truth": {
                "manifest_source_used": "core/manifest.py",
                "objective_index": "docs/objective-index.md",
            },
        }
        manifest = {
            "schema_version": "2026-03-12-68",
            "release_tag": "objective-75",
            "contract_version": "tod-mim-shared-contract-v1",
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            export_module.write_exports(
                payload, manifest, output_dir, mirror_root=False
            )

            context_payload = json.loads(
                (output_dir / "MIM_CONTEXT_EXPORT.latest.json").read_text(
                    encoding="utf-8"
                )
            )
            manifest_payload = json.loads(
                (output_dir / "MIM_MANIFEST.latest.json").read_text(encoding="utf-8")
            )
            handshake_payload = json.loads(
                (output_dir / "MIM_TOD_HANDSHAKE_PACKET.latest.json").read_text(
                    encoding="utf-8"
                )
            )
            alignment_payload = json.loads(
                (output_dir / "MIM_TOD_ALIGNMENT_REQUEST.latest.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(context_payload["objective_active"], "75")
            self.assertEqual(
                manifest_payload["manifest"]["schema_version"], "2026-03-12-68"
            )
            self.assertEqual(
                handshake_payload["truth"]["objective_active"],
                context_payload["objective_active"],
            )
            self.assertEqual(
                handshake_payload["truth"]["latest_completed_objective"],
                context_payload["latest_completed_objective"],
            )
            self.assertEqual(
                handshake_payload["truth"]["current_next_objective"],
                context_payload["current_next_objective"],
            )
            self.assertEqual(
                handshake_payload["truth"]["schema_version"],
                manifest_payload["manifest"]["schema_version"],
            )
            self.assertEqual(
                handshake_payload["truth"]["release_tag"],
                manifest_payload["manifest"]["release_tag"],
            )
            self.assertEqual(
                alignment_payload["mim_truth"]["objective_active"],
                context_payload["objective_active"],
            )
            self.assertEqual(
                alignment_payload["success_criteria"]["tod_current_objective"],
                context_payload["objective_active"],
            )
            self.assertIn(str(output_dir), alignment_payload["requested_actions"][0])

    def test_validate_gate_defaults_to_objective_75(self) -> None:
        status_payload = {
            "mim_schema": "2026-03-12-68",
            "compatible": True,
            "mim_handshake": {
                "available": True,
                "objective_active": "75",
                "schema_version": "2026-03-12-68",
                "release_tag": "objective-75",
            },
            "objective_alignment": {
                "status": "aligned",
                "tod_current_objective": "75",
                "mim_objective_active": "75",
            },
            "mim_refresh": {
                "copied_manifest": True,
                "source_manifest": "runtime/shared/MIM_MANIFEST.latest.json",
                "source_handshake_packet": "runtime/shared/MIM_TOD_HANDSHAKE_PACKET.latest.json",
                "failure_reason": "",
            },
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir)
            self._write_shared_truth_bundle(shared_dir)
            (shared_dir / "TOD_INTEGRATION_STATUS.latest.json").write_text(
                json.dumps(status_payload, indent=2) + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["bash", str(GATE_SCRIPT)],
                cwd=ROOT,
                env={**os.environ, "SHARED_DIR": str(shared_dir)},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(
                completed.returncode, 0, completed.stdout + completed.stderr
            )
            self.assertIn("GATE: PASS", completed.stdout)
            self.assertIn("tod objective == 75", completed.stdout)
            self.assertIn("mim objective == 75", completed.stdout)

    def test_validate_gate_rejects_stale_default_objective(self) -> None:
        status_payload = {
            "mim_schema": "2026-03-12-67",
            "compatible": True,
            "mim_handshake": {
                "available": True,
                "objective_active": "74",
                "schema_version": "2026-03-12-67",
                "release_tag": "objective-74",
            },
            "objective_alignment": {
                "status": "aligned",
                "tod_current_objective": "74",
                "mim_objective_active": "74",
            },
            "mim_refresh": {
                "copied_manifest": True,
                "source_manifest": "runtime/shared/MIM_MANIFEST.latest.json",
                "source_handshake_packet": "runtime/shared/MIM_TOD_HANDSHAKE_PACKET.latest.json",
                "failure_reason": "",
            },
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir)
            self._write_shared_truth_bundle(shared_dir)
            (shared_dir / "TOD_INTEGRATION_STATUS.latest.json").write_text(
                json.dumps(status_payload, indent=2) + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["bash", str(GATE_SCRIPT)],
                cwd=ROOT,
                env={**os.environ, "SHARED_DIR": str(shared_dir)},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(
                completed.returncode, 0, completed.stdout + completed.stderr
            )
            self.assertIn("GATE: FAIL", completed.stdout)
            self.assertIn("tod objective == 75", completed.stdout)
            self.assertIn("mim objective == 75", completed.stdout)

    def test_validate_gate_rejects_stale_refresh_substate_despite_alignment(
        self,
    ) -> None:
        status_payload = {
            "mim_schema": "2026-03-12-67",
            "compatible": True,
            "mim_handshake": {
                "available": False,
                "objective_active": "",
                "schema_version": "",
                "release_tag": "",
            },
            "objective_alignment": {
                "status": "in_sync",
                "tod_current_objective": "75",
                "mim_objective_active": "75",
            },
            "mim_refresh": {
                "copied_manifest": False,
                "source_manifest": "",
                "source_handshake_packet": "",
                "failure_reason": "",
            },
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir)
            self._write_shared_truth_bundle(shared_dir)
            (shared_dir / "TOD_INTEGRATION_STATUS.latest.json").write_text(
                json.dumps(status_payload, indent=2) + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["bash", str(GATE_SCRIPT)],
                cwd=ROOT,
                env={**os.environ, "SHARED_DIR": str(shared_dir)},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(
                completed.returncode, 0, completed.stdout + completed.stderr
            )
            self.assertIn("GATE: FAIL", completed.stdout)
            self.assertIn(
                "canonical refresh evidence matches shared handshake/manifest truth",
                completed.stdout,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
