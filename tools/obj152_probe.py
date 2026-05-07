from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
MIM_ARM_PATH = ROOT / "core" / "routers" / "mim_arm.py"
TARGET_REQUEST_SHA = "de7aba22e71cb1e7f2b652a707d7e11c7aadab19819e7f29eff609badc0c9704"
TARGET_TRIGGER_SHA = "50fba8938972d4c04a1b1d709c42c1edf43482217ad8c30bece102b1bf52aa6c"


def _stub_module(name: str, **attrs: object) -> None:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


def _load_mim_arm_module():
    class DummyHealthMonitor:
        @staticmethod
        def get_health_summary() -> dict[str, object]:
            return {"status": "healthy"}

    class DummyModel:
        pass

    _stub_module("core.db", get_db=lambda: None)
    _stub_module("core.execution_trace_service", append_execution_trace_event=lambda *args, **kwargs: None)
    _stub_module(
        "core.execution_lane_service",
        TARGET_MIM_ARM="mim_arm",
        build_execution_target_profile=lambda *args, **kwargs: {},
        submit_execution_request=lambda *args, **kwargs: {},
    )
    _stub_module("core.journal", write_journal=lambda *args, **kwargs: None)
    _stub_module(
        "core.mim_arm_dispatch_telemetry",
        record_dispatch_telemetry_from_publish=lambda **kwargs: {},
        refresh_dispatch_telemetry_record=lambda *args, **kwargs: {},
    )
    _stub_module("core.primitive_request_recovery_service", load_authoritative_request_status=lambda *args, **kwargs: {})
    _stub_module(
        "core.models",
        CapabilityExecution=DummyModel,
        CapabilityRegistration=DummyModel,
        ExecutionTaskOrchestration=DummyModel,
        InputEvent=DummyModel,
        InputEventResolution=DummyModel,
    )
    routers_package = types.ModuleType("core.routers")
    routers_package.__path__ = []
    gateway_module = types.ModuleType("core.routers.gateway")
    self_awareness_module = types.ModuleType("core.routers.self_awareness_router")
    self_awareness_module.health_monitor = DummyHealthMonitor()
    routers_package.gateway = gateway_module
    routers_package.self_awareness_router = self_awareness_module
    sys.modules["core.routers"] = routers_package
    sys.modules["core.routers.gateway"] = gateway_module
    sys.modules["core.routers.self_awareness_router"] = self_awareness_module
    _stub_module("core.task_orchestrator", to_execution_task_orchestration_out=lambda *args, **kwargs: {})

    spec = importlib.util.spec_from_file_location("obj152_mim_arm_probe_module", MIM_ARM_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to create import spec for core/routers/mim_arm.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_probe(cases: list[dict[str, object]], request_sequence: int, trigger_sequence: int, dump_dir: Path | None = None) -> list[dict[str, object]]:
    mim_arm = _load_mim_arm_module()
    execution = SimpleNamespace(
        id=777,
        capability_name="mim_arm.execute_safe_home",
        requested_executor="tod",
        arguments_json={"target_pose": "safe_home"},
    )

    original_bridge = mim_arm._bridge_meta
    original_objective = mim_arm._active_objective_metadata
    original_audit = mim_arm._audit_tod_bridge_write
    original_remote_publish = mim_arm.TOD_REMOTE_PUBLISH_SCRIPT

    mim_arm._audit_tod_bridge_write = lambda **kwargs: None
    mim_arm._active_objective_metadata = lambda shared_root: {
        "objective_id": "152",
        "objective_ref": "objective-152",
        "release_tag": "objective-152",
        "schema_version": "2026-03-24-70",
    }
    mim_arm.TOD_REMOTE_PUBLISH_SCRIPT = Path("/definitely/not/here")

    results: list[dict[str, object]] = []
    try:
        bridge_state = {"count": 0}

        def fake_bridge_meta(*, shared_root: Path, service_name: str, instance_id: str) -> dict[str, object]:
            bridge_state["count"] += 1
            sequence = request_sequence if bridge_state["count"] == 1 else trigger_sequence
            return {
                "SEQUENCE": str(sequence),
                "EMITTED_AT": "2026-04-08T16:00:30Z",
                "SOURCE_HOST": "MIM",
                "SOURCE_SERVICE": service_name,
                "SOURCE_INSTANCE_ID": instance_id,
            }

        mim_arm._bridge_meta = fake_bridge_meta

        for case in cases:
            bridge_state["count"] = 0
            with tempfile.TemporaryDirectory() as temp_dir:
                shared_root = Path(temp_dir)
                mim_arm.publish_mim_arm_execution_to_tod(
                    execution=execution,
                    status=case["status"],
                    shared_root=shared_root,
                )
                request_bytes = (shared_root / "MIM_TOD_TASK_REQUEST.latest.json").read_bytes()
                trigger_bytes = (shared_root / "MIM_TO_TOD_TRIGGER.latest.json").read_bytes()
                if dump_dir is not None:
                    dump_dir.mkdir(parents=True, exist_ok=True)
                    (dump_dir / f"{case['name']}.request.json").write_bytes(request_bytes)
                    (dump_dir / f"{case['name']}.trigger.json").write_bytes(trigger_bytes)

            request_sha = hashlib.sha256(request_bytes).hexdigest()
            trigger_sha = hashlib.sha256(trigger_bytes).hexdigest()
            results.append(
                {
                    "name": case["name"],
                    "request_size": len(request_bytes),
                    "trigger_size": len(trigger_bytes),
                    "request_sha": request_sha,
                    "trigger_sha": trigger_sha,
                    "request_match": request_sha == TARGET_REQUEST_SHA,
                    "trigger_match": trigger_sha == TARGET_TRIGGER_SHA,
                }
            )
    finally:
        mim_arm._bridge_meta = original_bridge
        mim_arm._active_objective_metadata = original_objective
        mim_arm._audit_tod_bridge_write = original_audit
        mim_arm.TOD_REMOTE_PUBLISH_SCRIPT = original_remote_publish

    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-sequence", type=int, default=1111258)
    parser.add_argument("--trigger-sequence", type=int, default=1111259)
    parser.add_argument("--dump-dir")
    args = parser.parse_args()

    cases = [
        {
            "name": "dev_pose_all_true",
            "status": {
                "arm_online": True,
                "current_pose": [116, 62, 62, 95, 53, 91],
                "mode": "development",
                "camera_online": True,
                "serial_ready": True,
                "estop_ok": True,
                "tod_execution_allowed": True,
                "motion_allowed": True,
            },
        },
        {
            "name": "dev_pose_motion_blocked",
            "status": {
                "arm_online": True,
                "current_pose": [116, 62, 62, 95, 53, 91],
                "mode": "development",
                "camera_online": True,
                "serial_ready": True,
                "estop_ok": True,
                "tod_execution_allowed": True,
                "motion_allowed": False,
            },
        },
        {
            "name": "unknown_unknown_blocked",
            "status": {
                "arm_online": True,
                "current_pose": "unknown",
                "mode": "unknown",
                "camera_online": False,
                "serial_ready": False,
                "estop_ok": None,
                "tod_execution_allowed": False,
                "motion_allowed": False,
            },
        },
        {
            "name": "scan_idle_ready",
            "status": {
                "arm_online": True,
                "current_pose": "scan_pose",
                "mode": "idle",
                "camera_online": True,
                "serial_ready": True,
                "estop_ok": True,
                "tod_execution_allowed": True,
                "motion_allowed": False,
            },
        },
        {
            "name": "scan_development_ready",
            "status": {
                "arm_online": True,
                "current_pose": "scan_pose",
                "mode": "development",
                "camera_online": True,
                "serial_ready": True,
                "estop_ok": True,
                "tod_execution_allowed": True,
                "motion_allowed": False,
            },
        },
    ]

    dump_dir = Path(args.dump_dir).resolve() if args.dump_dir else None
    print(json.dumps(run_probe(cases, args.request_sequence, args.trigger_sequence, dump_dir=dump_dir), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())