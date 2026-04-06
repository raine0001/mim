#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4


REQUIRED_CAPABILITIES = [
    "operator_resolution_commitments",
    "operator_commitment_enforcement_monitoring",
    "operator_commitment_outcome_learning_loop",
]


class ProbeFailure(RuntimeError):
    pass


class Probe:
    def __init__(self) -> None:
        self.results: list[tuple[bool, str, str]] = []

    def check(self, ok: bool, label: str, detail: str) -> None:
        self.results.append((ok, label, detail))

    def require(self, ok: bool, label: str, detail: str) -> None:
        self.check(ok, label, detail)
        if not ok:
            raise ProbeFailure(detail)

    def summary(self) -> int:
        all_ok = all(ok for ok, _, _ in self.results)
        print(f"OBJECTIVE87_PROD_PROBE: {'PASS' if all_ok else 'FAIL'}")
        for ok, label, detail in self.results:
            state = "PASS" if ok else "FAIL"
            print(f"- {state}: {label} ({detail})")
        return 0 if all_ok else 1


def _json_dumps(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def request_json(
    *,
    base_url: str,
    method: str,
    path: str,
    timeout: float,
    payload: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    url = urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = _json_dumps(payload)
    request = urllib.request.Request(url=url, method=method.upper(), headers=headers, data=data)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            if not body.strip():
                return response.status, {}
            return response.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body) if body.strip() else {}
        except json.JSONDecodeError:
            parsed = {"raw": body}
        return exc.code, parsed
    except urllib.error.URLError as exc:
        raise ProbeFailure(f"request failed for {method.upper()} {url}: {exc}") from exc


def load_json_file(path: Path) -> tuple[bool, Any, str]:
    if not path.exists():
        return False, {}, f"missing file {path}"
    try:
        return True, json.loads(path.read_text(encoding="utf-8-sig")), str(path)
    except Exception as exc:
        return False, {}, f"failed to parse {path}: {exc}"


def normalize_text(value: Any) -> str:
    return str(value or "").strip()


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def parse_args() -> argparse.Namespace:
    root_dir = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Probe Objective 87 production readiness and optional end-to-end outcome propagation.",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--expected-objective", default="87")
    parser.add_argument("--expected-release-tag", default="objective-87")
    parser.add_argument("--expected-schema-version", default="")
    parser.add_argument(
        "--context-export-file",
        default=str(root_dir / "runtime/shared/MIM_CONTEXT_EXPORT.latest.json"),
    )
    parser.add_argument(
        "--tod-status-file",
        default=str(root_dir / "runtime/shared/TOD_INTEGRATION_STATUS.latest.json"),
    )
    parser.add_argument("--scope", default=f"objective87-prod-probe-{uuid4().hex[:8]}")
    parser.add_argument("--actor", default="objective87-prod-probe")
    parser.add_argument("--source", default="objective87-prod-probe")
    parser.add_argument("--target-status", default="satisfied")
    parser.add_argument(
        "--write-cycle",
        action="store_true",
        help="Create a temporary commitment, evaluate monitoring, resolve outcome, and verify downstream propagation.",
    )
    return parser.parse_args()


def verify_manifest(probe: Probe, manifest: dict[str, Any], args: argparse.Namespace) -> None:
    capabilities = manifest.get("capabilities") if isinstance(manifest.get("capabilities"), list) else []
    missing = [name for name in REQUIRED_CAPABILITIES if name not in capabilities]
    probe.check(
        not missing,
        "manifest advertises Objective 85-87 capabilities",
        f"missing={missing or 'none'}",
    )
    if args.expected_release_tag:
        probe.check(
            normalize_text(manifest.get("release_tag")) == args.expected_release_tag,
            "manifest release tag matches expected promotion target",
            f"actual={normalize_text(manifest.get('release_tag'))!r} expected={args.expected_release_tag!r}",
        )
    if args.expected_schema_version:
        probe.check(
            normalize_text(manifest.get("schema_version")) == args.expected_schema_version,
            "manifest schema version matches expected promotion target",
            f"actual={normalize_text(manifest.get('schema_version'))!r} expected={args.expected_schema_version!r}",
        )


def verify_shared_files(probe: Probe, args: argparse.Namespace) -> None:
    context_ok, context_payload, context_detail = load_json_file(Path(args.context_export_file))
    probe.check(context_ok, "context export file available", context_detail)
    if context_ok:
        probe.check(
            normalize_text(context_payload.get("objective_active")) == args.expected_objective,
            "context export objective matches expected objective",
            f"actual={normalize_text(context_payload.get('objective_active'))!r} expected={args.expected_objective!r}",
        )
        if args.expected_release_tag:
            probe.check(
                normalize_text(context_payload.get("release_tag")) == args.expected_release_tag,
                "context export release tag matches expected promotion target",
                f"actual={normalize_text(context_payload.get('release_tag'))!r} expected={args.expected_release_tag!r}",
            )

    tod_ok, tod_payload, tod_detail = load_json_file(Path(args.tod_status_file))
    probe.check(tod_ok, "TOD integration status file available", tod_detail)
    if tod_ok:
        probe.check(
            bool(tod_payload.get("compatible", False)),
            "TOD integration contract is compatible",
            f"compatible={bool(tod_payload.get('compatible', False))}",
        )
        alignment = as_dict(tod_payload.get("objective_alignment"))
        probe.check(
            bool(alignment.get("aligned", False)),
            "TOD objective alignment is coherent",
            f"alignment={alignment}",
        )
        live_task = as_dict(tod_payload.get("live_task_request"))
        if normalize_text(live_task.get("normalized_objective_id")):
            probe.check(
                normalize_text(live_task.get("normalized_objective_id")) == args.expected_objective,
                "TOD live task objective matches expected objective",
                f"actual={normalize_text(live_task.get('normalized_objective_id'))!r} expected={args.expected_objective!r}",
            )


def verify_read_only_runtime(probe: Probe, args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    status, health = request_json(base_url=args.base_url, method="GET", path="/health", timeout=args.timeout)
    probe.require(status == 200, "health endpoint reachable", f"status={status}")
    probe.check(
        normalize_text(as_dict(health).get("status")) == "ok",
        "health status is ok",
        f"payload={health}",
    )

    status, manifest = request_json(base_url=args.base_url, method="GET", path="/manifest", timeout=args.timeout)
    probe.require(status == 200, "manifest endpoint reachable", f"status={status}")
    verify_manifest(probe, as_dict(manifest), args)

    status, commitments_payload = request_json(
        base_url=args.base_url,
        method="GET",
        path="/operator/resolution-commitments?limit=5",
        timeout=args.timeout,
    )
    probe.check(
        status == 200,
        "operator resolution commitments endpoint reachable",
        f"status={status} payload={commitments_payload}",
    )

    status, ui_state = request_json(base_url=args.base_url, method="GET", path="/mim/ui/state", timeout=args.timeout)
    probe.check(status == 200, "UI state endpoint reachable", f"status={status}")
    operator_reasoning = as_dict(as_dict(ui_state).get("operator_reasoning"))
    probe.check(
        bool(operator_reasoning),
        "UI exposes operator reasoning payload",
        f"keys={sorted(operator_reasoning.keys()) if operator_reasoning else []}",
    )
    return as_dict(manifest), as_dict(ui_state)


def verify_write_cycle(probe: Probe, args: argparse.Namespace) -> None:
    create_payload = {
        "actor": args.actor,
        "managed_scope": args.scope,
        "decision_type": "defer_autonomy_escalation",
        "reason": "Objective 87 production probe temporary commitment",
        "recommendation_snapshot_json": {
            "decision": "defer_autonomy_escalation",
            "source": "objective87_prod_probe",
            "managed_scope": args.scope,
        },
        "commitment_family": "stability_guardrail",
        "authority_level": "governance_override",
        "confidence": 0.95,
        "duration_seconds": 3600,
        "provenance_json": {"probe": True, "objective": "87"},
        "downstream_effects_json": {
            "strategy": "observe_outcome_influence",
            "autonomy": "observe_boundary_adaptation",
            "stewardship": "observe_scope_coherence",
        },
        "metadata_json": {"objective87_prod_probe": True, "managed_scope": args.scope},
    }
    status, created = request_json(
        base_url=args.base_url,
        method="POST",
        path="/operator/resolution-commitments",
        timeout=args.timeout,
        payload=create_payload,
    )
    probe.require(status == 200, "temporary Objective 87 probe commitment created", f"status={status} payload={created}")
    commitment = as_dict(created.get("commitment"))
    commitment_id = int(commitment.get("commitment_id", 0) or 0)
    probe.require(commitment_id > 0, "commitment id returned", f"commitment={commitment}")
    probe.check(
        normalize_text(commitment.get("managed_scope")) == args.scope,
        "created commitment uses probe scope",
        f"managed_scope={normalize_text(commitment.get('managed_scope'))!r}",
    )

    status, monitored = request_json(
        base_url=args.base_url,
        method="POST",
        path=f"/operator/resolution-commitments/{commitment_id}/monitoring/evaluate",
        timeout=args.timeout,
        payload={
            "actor": args.actor,
            "source": args.source,
            "lookback_hours": 168,
            "metadata_json": {"objective87_prod_probe": True, "managed_scope": args.scope},
        },
    )
    probe.require(status == 200, "commitment monitoring evaluated", f"status={status} payload={monitored}")
    monitoring = as_dict(monitored.get("monitoring"))
    probe.check(
        isinstance(monitoring.get("health_score"), (int, float)),
        "monitoring health score present",
        f"health_score={monitoring.get('health_score')!r}",
    )

    status, resolved = request_json(
        base_url=args.base_url,
        method="POST",
        path=f"/operator/resolution-commitments/{commitment_id}/resolve",
        timeout=args.timeout,
        payload={
            "actor": args.actor,
            "source": args.source,
            "target_status": args.target_status,
            "reason": "Objective 87 production probe terminal resolution",
            "lookback_hours": 168,
            "metadata_json": {"objective87_prod_probe": True, "managed_scope": args.scope},
        },
    )
    probe.require(status == 200, "commitment resolved to terminal outcome", f"status={status} payload={resolved}")
    outcome = as_dict(resolved.get("outcome"))
    outcome_id = int(outcome.get("outcome_id", 0) or 0)
    probe.require(outcome_id > 0, "terminal outcome id returned", f"outcome={outcome}")
    probe.check(
        normalize_text(outcome.get("outcome_status")) == args.target_status,
        "terminal outcome status matches target",
        f"outcome_status={normalize_text(outcome.get('outcome_status'))!r}",
    )
    probe.check(
        isinstance(outcome.get("effectiveness_score"), (int, float))
        and isinstance(outcome.get("stability_score"), (int, float)),
        "outcome fitness proxies present",
        (
            f"effectiveness_score={outcome.get('effectiveness_score')!r} "
            f"stability_score={outcome.get('stability_score')!r}"
        ),
    )

    status, strategy_payload = request_json(
        base_url=args.base_url,
        method="POST",
        path="/strategy/goals/build",
        timeout=args.timeout,
        payload={
            "actor": args.actor,
            "source": args.source,
            "lookback_hours": 48,
            "max_items_per_domain": 50,
            "max_goals": 4,
            "min_context_confidence": 0.0,
            "min_domains_required": 1,
            "min_cross_domain_links": 0,
            "generate_horizon_plans": False,
            "generate_improvement_proposals": False,
            "generate_maintenance_cycles": False,
            "metadata_json": {"objective87_prod_probe": True, "managed_scope": args.scope},
        },
    )
    probe.require(status == 200, "strategy build endpoint succeeded", f"status={status}")
    goals = strategy_payload.get("goals") if isinstance(strategy_payload.get("goals"), list) else []
    first_goal = as_dict(goals[0]) if goals else {}
    probe.check(
        "operator_resolution_outcome" in as_dict(first_goal.get("reasoning")),
        "strategy reasoning reflects commitment outcome influence",
        f"first_goal_reasoning_keys={sorted(as_dict(first_goal.get('reasoning')).keys())}",
    )

    status, autonomy_payload = request_json(
        base_url=args.base_url,
        method="POST",
        path="/autonomy/boundaries/recompute",
        timeout=args.timeout,
        payload={
            "actor": args.actor,
            "source": args.source,
            "scope": args.scope,
            "lookback_hours": 168,
            "min_samples": 1,
            "apply_recommended_boundaries": False,
            "metadata_json": {"objective87_prod_probe": True, "managed_scope": args.scope},
        },
    )
    probe.require(status == 200, "autonomy boundary recompute endpoint succeeded", f"status={status}")
    boundary = as_dict(autonomy_payload.get("boundary"))
    probe.check(
        "operator_resolution_outcome" in as_dict(boundary.get("adaptation_reasoning")),
        "autonomy reasoning reflects commitment outcome influence",
        f"adaptation_reasoning_keys={sorted(as_dict(boundary.get('adaptation_reasoning')).keys())}",
    )

    status, stewardship_payload = request_json(
        base_url=args.base_url,
        method="POST",
        path="/stewardship/cycle",
        timeout=args.timeout,
        payload={
            "actor": args.actor,
            "source": args.source,
            "managed_scope": args.scope,
            "lookback_hours": 168,
            "max_strategies": 5,
            "max_actions": 5,
            "auto_execute": False,
            "force_degraded": False,
            "metadata_json": {"objective87_prod_probe": True, "managed_scope": args.scope},
        },
    )
    probe.require(status == 200, "stewardship cycle endpoint succeeded", f"status={status}")
    stewardship = as_dict(stewardship_payload.get("stewardship"))
    cycle = as_dict(stewardship_payload.get("cycle"))
    probe.check(
        normalize_text(stewardship.get("managed_scope")) == args.scope,
        "stewardship cycle persisted probe scope",
        f"managed_scope={normalize_text(stewardship.get('managed_scope'))!r}",
    )
    probe.check(
        int(cycle.get("cycle_id", 0) or 0) > 0,
        "stewardship cycle id returned",
        f"cycle_id={cycle.get('cycle_id')!r}",
    )

    status, ui_state = request_json(base_url=args.base_url, method="GET", path="/mim/ui/state", timeout=args.timeout)
    probe.require(status == 200, "UI state reachable after write-cycle probe", f"status={status}")
    operator_reasoning = as_dict(as_dict(ui_state).get("operator_reasoning"))
    ui_outcome = as_dict(operator_reasoning.get("commitment_outcome"))
    probe.check(
        int(ui_outcome.get("outcome_id", 0) or 0) == outcome_id,
        "UI operator reasoning exposes the latest probe outcome",
        f"ui_outcome_id={ui_outcome.get('outcome_id')!r} expected={outcome_id!r}",
    )
    ui_autonomy = as_dict(operator_reasoning.get("autonomy"))
    ui_stewardship = as_dict(operator_reasoning.get("stewardship"))
    probe.check(
        bool(ui_autonomy),
        "UI operator reasoning includes autonomy snapshot",
        f"keys={sorted(ui_autonomy.keys()) if ui_autonomy else []}",
    )
    probe.check(
        bool(ui_stewardship),
        "UI operator reasoning includes stewardship snapshot",
        f"keys={sorted(ui_stewardship.keys()) if ui_stewardship else []}",
    )
    if normalize_text(ui_stewardship.get("managed_scope")):
        probe.check(
            normalize_text(ui_stewardship.get("managed_scope")) == args.scope,
            "UI stewardship snapshot is coherent with probe scope",
            f"managed_scope={normalize_text(ui_stewardship.get('managed_scope'))!r}",
        )


def main() -> int:
    args = parse_args()
    probe = Probe()
    try:
        verify_read_only_runtime(probe, args)
        verify_shared_files(probe, args)
        if args.write_cycle:
            verify_write_cycle(probe, args)
    except ProbeFailure as exc:
        probe.check(False, "probe aborted", str(exc))
    return probe.summary()


if __name__ == "__main__":
    raise SystemExit(main())