from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Objective(Base, TimestampMixin):
    __tablename__ = "objectives"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[str] = mapped_column(String(40), default="normal")
    constraints_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    success_criteria: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(String(40), default="new")


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    objective_id: Mapped[int | None] = mapped_column(ForeignKey("objectives.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(200), index=True)
    details: Mapped[str] = mapped_column(Text, default="")
    dependencies: Mapped[list[int]] = mapped_column(JSON, default=list)
    acceptance_criteria: Mapped[str] = mapped_column(Text, default="")
    assigned_to: Mapped[str] = mapped_column(String(120), default="unassigned")
    state: Mapped[str] = mapped_column(String(40), default="queued")


class TaskResult(Base, TimestampMixin):
    __tablename__ = "task_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    result: Mapped[str] = mapped_column(Text)
    files_changed: Mapped[list[str]] = mapped_column(JSON, default=list)
    tests_run: Mapped[list[str]] = mapped_column(JSON, default=list)
    test_results: Mapped[str] = mapped_column(Text, default="")
    failures: Mapped[list[str]] = mapped_column(JSON, default=list)
    recommendations: Mapped[str] = mapped_column(Text, default="")


class TaskReview(Base, TimestampMixin):
    __tablename__ = "task_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    reviewer: Mapped[str] = mapped_column(String(120), default="tod")
    status: Mapped[str] = mapped_column(String(50), default="pending")
    notes: Mapped[str] = mapped_column(Text, default="")
    continue_allowed: Mapped[bool] = mapped_column(default=False)
    escalate_to_user: Mapped[bool] = mapped_column(default=False)


class ExecutionJournal(Base, TimestampMixin):
    __tablename__ = "execution_journal"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(200))
    target_type: Mapped[str] = mapped_column(String(80), default="system")
    target_id: Mapped[str] = mapped_column(String(120), default="")
    idempotency_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    result: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class MemoryEntry(Base, TimestampMixin):
    __tablename__ = "memory_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    memory_class: Mapped[str] = mapped_column(String(60), index=True)
    content: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class MemoryLink(Base, TimestampMixin):
    __tablename__ = "memory_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_memory_id: Mapped[int] = mapped_column(ForeignKey("memory_entries.id", ondelete="CASCADE"))
    target_memory_id: Mapped[int] = mapped_column(ForeignKey("memory_entries.id", ondelete="CASCADE"))
    relation: Mapped[str] = mapped_column(String(80), default="related")


class UserPreference(Base, TimestampMixin):
    __tablename__ = "user_preferences"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(String(120), default="operator", index=True)
    preference_type: Mapped[str] = mapped_column(String(120), index=True)
    value: Mapped[object] = mapped_column(JSON, default=dict)
    confidence: Mapped[float] = mapped_column(default=0.0)
    source: Mapped[str] = mapped_column(String(80), default="manual")
    last_updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class Tool(Base, TimestampMixin):
    __tablename__ = "tools"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(default=True)


class ToolInvocation(Base, TimestampMixin):
    __tablename__ = "tool_invocations"

    id: Mapped[int] = mapped_column(primary_key=True)
    tool_id: Mapped[int] = mapped_column(ForeignKey("tools.id", ondelete="CASCADE"))
    actor: Mapped[str] = mapped_column(String(120), default="system")
    input_json: Mapped[dict] = mapped_column(JSON, default=dict)
    output_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(40), default="ok")


class Service(Base, TimestampMixin):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    status: Mapped[str] = mapped_column(String(40), default="starting")
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dependency_map: Mapped[dict] = mapped_column(JSON, default=dict)


class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")


class Actor(Base, TimestampMixin):
    __tablename__ = "actors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    role: Mapped[str] = mapped_column(String(80), default="user")
    identity_metadata: Mapped[dict] = mapped_column(JSON, default=dict)


class RoutingExecutionMetric(Base, TimestampMixin):
    __tablename__ = "routing_execution_metrics"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int | None] = mapped_column(nullable=True)
    objective_id: Mapped[int | None] = mapped_column(nullable=True)
    selected_engine: Mapped[str] = mapped_column(String(120), index=True)
    fallback_engine: Mapped[str] = mapped_column(String(120), default="")
    fallback_used: Mapped[bool] = mapped_column(default=False)
    routing_source: Mapped[str] = mapped_column(String(120), default="tod.invoke-engine")
    routing_confidence: Mapped[float] = mapped_column(default=0.0)
    policy_version: Mapped[str] = mapped_column(String(80), default="routing-policy-v1")
    engine_version: Mapped[str] = mapped_column(String(120), default="unknown")
    routing_selection_reason: Mapped[str] = mapped_column(Text, default="")
    routing_final_outcome: Mapped[str] = mapped_column(String(40), default="unknown")
    latency_ms: Mapped[int] = mapped_column(default=0)
    result_category: Mapped[str] = mapped_column(String(80), default="unknown")
    failure_category: Mapped[str] = mapped_column(String(120), default="")
    review_outcome: Mapped[str] = mapped_column(String(40), default="unknown")
    blocked_pre_invocation: Mapped[bool] = mapped_column(default=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class RoutingEngineSummary(Base, TimestampMixin):
    __tablename__ = "routing_engine_summaries"

    id: Mapped[int] = mapped_column(primary_key=True)
    engine_name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    runs: Mapped[int] = mapped_column(default=0)
    pass_rate: Mapped[float] = mapped_column(default=0.0)
    review_correction_rate: Mapped[float] = mapped_column(default=0.0)
    blocked_rate: Mapped[float] = mapped_column(default=0.0)
    avg_latency_ms: Mapped[float] = mapped_column(default=0.0)
    fallback_rate: Mapped[float] = mapped_column(default=0.0)
    weighted_recent_score: Mapped[float] = mapped_column(default=0.0)
    sample_window: Mapped[int] = mapped_column(default=200)


class Goal(Base, TimestampMixin):
    __tablename__ = "goals"

    id: Mapped[int] = mapped_column(primary_key=True)
    objective_id: Mapped[int | None] = mapped_column(ForeignKey("objectives.id", ondelete="SET NULL"), nullable=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    goal_type: Mapped[str] = mapped_column(String(80), default="task_execution")
    goal_description: Mapped[str] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(String(120), default="tod")
    priority: Mapped[str] = mapped_column(String(40), default="normal")
    status: Mapped[str] = mapped_column(String(40), default="new")


class Action(Base):
    __tablename__ = "actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    goal_id: Mapped[int] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    engine: Mapped[str] = mapped_column(String(120), default="unknown")
    action_type: Mapped[str] = mapped_column(String(120), default="execute")
    input_ref: Mapped[str] = mapped_column(Text, default="")
    expected_state_delta: Mapped[dict] = mapped_column(JSON, default=dict)
    validation_method: Mapped[str] = mapped_column(String(120), default="hint")
    sequence_index: Mapped[int] = mapped_column(default=1, index=True)
    depends_on_action_id: Mapped[int | None] = mapped_column(nullable=True)
    parent_action_id: Mapped[int | None] = mapped_column(nullable=True)
    retry_of_action_id: Mapped[int | None] = mapped_column(nullable=True)
    retry_count: Mapped[int] = mapped_column(default=0)
    replaced_action_id: Mapped[int | None] = mapped_column(nullable=True)
    replacement_action_id: Mapped[int | None] = mapped_column(nullable=True)
    recovery_classification: Mapped[str] = mapped_column(String(40), default="")
    chain_event: Mapped[str] = mapped_column(String(40), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="started")


class GoalPlan(Base):
    __tablename__ = "goal_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    goal_id: Mapped[int] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), unique=True, index=True)
    ordered_action_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    current_step_index: Mapped[int] = mapped_column(default=0)
    derived_status: Mapped[str] = mapped_column(String(40), default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StateSnapshot(Base):
    __tablename__ = "state_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    goal_id: Mapped[int] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    action_id: Mapped[int] = mapped_column(ForeignKey("actions.id", ondelete="CASCADE"), index=True)
    snapshot_phase: Mapped[str] = mapped_column(String(20), index=True)
    state_type: Mapped[str] = mapped_column(String(80), default="json")
    state_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ValidationResult(Base):
    __tablename__ = "validation_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    goal_id: Mapped[int] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    action_id: Mapped[int] = mapped_column(ForeignKey("actions.id", ondelete="CASCADE"), index=True)
    validation_method: Mapped[str] = mapped_column(String(120), default="hint")
    validation_status: Mapped[str] = mapped_column(String(40), default="unknown")
    validation_details: Mapped[dict] = mapped_column(JSON, default=dict)
    validated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InputEvent(Base, TimestampMixin):
    __tablename__ = "input_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(20), index=True)
    raw_input: Mapped[str] = mapped_column(Text, default="")
    parsed_intent: Mapped[str] = mapped_column(String(120), default="unknown")
    confidence: Mapped[float] = mapped_column(default=0.0)
    target_system: Mapped[str] = mapped_column(String(120), default="mim")
    requested_goal: Mapped[str] = mapped_column(Text, default="")
    safety_flags: Mapped[list[str]] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    normalized: Mapped[bool] = mapped_column(default=True)


class InputEventResolution(Base, TimestampMixin):
    __tablename__ = "input_event_resolutions"

    id: Mapped[int] = mapped_column(primary_key=True)
    input_event_id: Mapped[int] = mapped_column(ForeignKey("input_events.id", ondelete="CASCADE"), unique=True, index=True)
    internal_intent: Mapped[str] = mapped_column(String(80), index=True)
    confidence_tier: Mapped[str] = mapped_column(String(20), default="unknown")
    outcome: Mapped[str] = mapped_column(String(40), default="requires_confirmation")
    resolution_status: Mapped[str] = mapped_column(String(40), default="requires_confirmation")
    safety_decision: Mapped[str] = mapped_column(String(40), default="requires_confirmation")
    reason: Mapped[str] = mapped_column(Text, default="")
    clarification_prompt: Mapped[str] = mapped_column(Text, default="")
    escalation_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    capability_name: Mapped[str] = mapped_column(String(120), default="")
    capability_registered: Mapped[bool] = mapped_column(default=False)
    capability_enabled: Mapped[bool] = mapped_column(default=False)
    goal_id: Mapped[int | None] = mapped_column(ForeignKey("goals.id", ondelete="SET NULL"), nullable=True)
    proposed_goal_description: Mapped[str] = mapped_column(Text, default="")
    proposed_actions: Mapped[list[dict]] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class CapabilityRegistration(Base, TimestampMixin):
    __tablename__ = "capability_registrations"

    id: Mapped[int] = mapped_column(primary_key=True)
    capability_name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(60), default="action")
    description: Mapped[str] = mapped_column(Text, default="")
    requires_confirmation: Mapped[bool] = mapped_column(default=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    safety_policy: Mapped[dict] = mapped_column(JSON, default=dict)


class SpeechOutputAction(Base, TimestampMixin):
    __tablename__ = "speech_output_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    requested_text: Mapped[str] = mapped_column(Text)
    voice_profile: Mapped[str] = mapped_column(String(80), default="default")
    channel: Mapped[str] = mapped_column(String(80), default="system")
    priority: Mapped[str] = mapped_column(String(20), default="normal")
    delivery_status: Mapped[str] = mapped_column(String(40), default="queued")
    failure_reason: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class CapabilityExecution(Base, TimestampMixin):
    __tablename__ = "capability_executions"

    id: Mapped[int] = mapped_column(primary_key=True)
    input_event_id: Mapped[int] = mapped_column(ForeignKey("input_events.id", ondelete="CASCADE"), index=True)
    resolution_id: Mapped[int | None] = mapped_column(ForeignKey("input_event_resolutions.id", ondelete="SET NULL"), nullable=True)
    goal_id: Mapped[int | None] = mapped_column(ForeignKey("goals.id", ondelete="SET NULL"), nullable=True)
    capability_name: Mapped[str] = mapped_column(String(120), index=True)
    arguments_json: Mapped[dict] = mapped_column(JSON, default=dict)
    safety_mode: Mapped[str] = mapped_column(String(40), default="standard")
    requested_executor: Mapped[str] = mapped_column(String(120), default="tod")
    dispatch_decision: Mapped[str] = mapped_column(String(40), default="requires_confirmation")
    status: Mapped[str] = mapped_column(String(40), default="pending")
    reason: Mapped[str] = mapped_column(Text, default="")
    feedback_json: Mapped[dict] = mapped_column(JSON, default=dict)


class WorkspaceObservation(Base, TimestampMixin):
    __tablename__ = "workspace_observations"

    id: Mapped[int] = mapped_column(primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    zone: Mapped[str] = mapped_column(String(120), index=True)
    label: Mapped[str] = mapped_column(String(160), index=True)
    confidence: Mapped[float] = mapped_column(default=0.0)
    source: Mapped[str] = mapped_column(String(40), default="vision")
    execution_id: Mapped[int | None] = mapped_column(ForeignKey("capability_executions.id", ondelete="SET NULL"), nullable=True, index=True)
    lifecycle_status: Mapped[str] = mapped_column(String(40), default="active", index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    observation_count: Mapped[int] = mapped_column(default=1)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class WorkspaceObjectMemory(Base, TimestampMixin):
    __tablename__ = "workspace_object_memories"

    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_name: Mapped[str] = mapped_column(String(160), index=True)
    candidate_labels: Mapped[list[str]] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(default=0.0)
    zone: Mapped[str] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(40), default="active", index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    last_execution_id: Mapped[int | None] = mapped_column(ForeignKey("capability_executions.id", ondelete="SET NULL"), nullable=True, index=True)
    location_history: Mapped[list[dict]] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class WorkspaceZone(Base, TimestampMixin):
    __tablename__ = "workspace_zones"

    id: Mapped[int] = mapped_column(primary_key=True)
    zone_name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(160), default="")
    hazard_level: Mapped[int] = mapped_column(default=0)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class WorkspaceZoneRelation(Base, TimestampMixin):
    __tablename__ = "workspace_zone_relations"

    id: Mapped[int] = mapped_column(primary_key=True)
    from_zone_id: Mapped[int] = mapped_column(ForeignKey("workspace_zones.id", ondelete="CASCADE"), index=True)
    to_zone_id: Mapped[int] = mapped_column(ForeignKey("workspace_zones.id", ondelete="CASCADE"), index=True)
    relation_type: Mapped[str] = mapped_column(String(60), index=True)
    confidence: Mapped[float] = mapped_column(default=1.0)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class WorkspaceObjectRelation(Base, TimestampMixin):
    __tablename__ = "workspace_object_relations"

    id: Mapped[int] = mapped_column(primary_key=True)
    subject_object_id: Mapped[int] = mapped_column(ForeignKey("workspace_object_memories.id", ondelete="CASCADE"), index=True)
    object_object_id: Mapped[int] = mapped_column(ForeignKey("workspace_object_memories.id", ondelete="CASCADE"), index=True)
    relation_type: Mapped[str] = mapped_column(String(60), default="near", index=True)
    relation_status: Mapped[str] = mapped_column(String(40), default="active", index=True)
    confidence: Mapped[float] = mapped_column(default=0.0)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    source_execution_id: Mapped[int | None] = mapped_column(ForeignKey("capability_executions.id", ondelete="SET NULL"), nullable=True, index=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class WorkspaceProposal(Base, TimestampMixin):
    __tablename__ = "workspace_proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_type: Mapped[str] = mapped_column(String(80), index=True)
    title: Mapped[str] = mapped_column(String(220))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    confidence: Mapped[float] = mapped_column(default=0.0)
    priority_score: Mapped[float] = mapped_column(default=0.0, index=True)
    priority_reason: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(80), default="workspace_state")
    related_zone: Mapped[str] = mapped_column(String(120), default="", index=True)
    related_object_id: Mapped[int | None] = mapped_column(ForeignKey("workspace_object_memories.id", ondelete="SET NULL"), nullable=True, index=True)
    source_execution_id: Mapped[int | None] = mapped_column(ForeignKey("capability_executions.id", ondelete="SET NULL"), nullable=True, index=True)
    trigger_json: Mapped[dict] = mapped_column(JSON, default=dict)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class WorkspaceTargetResolution(Base, TimestampMixin):
    __tablename__ = "workspace_target_resolutions"

    id: Mapped[int] = mapped_column(primary_key=True)
    requested_target: Mapped[str] = mapped_column(String(160), index=True)
    requested_zone: Mapped[str] = mapped_column(String(120), default="", index=True)
    match_outcome: Mapped[str] = mapped_column(String(40), default="no_match", index=True)
    policy_outcome: Mapped[str] = mapped_column(String(60), default="target_not_found", index=True)
    status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    confidence: Mapped[float] = mapped_column(default=0.0)
    related_object_id: Mapped[int | None] = mapped_column(ForeignKey("workspace_object_memories.id", ondelete="SET NULL"), nullable=True, index=True)
    candidate_object_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    suggested_actions: Mapped[list[str]] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(80), default="api")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class WorkspaceActionPlan(Base, TimestampMixin):
    __tablename__ = "workspace_action_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    target_resolution_id: Mapped[int] = mapped_column(ForeignKey("workspace_target_resolutions.id", ondelete="CASCADE"), index=True)
    target_label: Mapped[str] = mapped_column(String(160), index=True)
    target_zone: Mapped[str] = mapped_column(String(120), default="", index=True)
    action_type: Mapped[str] = mapped_column(String(80), index=True)
    safety_mode: Mapped[str] = mapped_column(String(60), default="operator_controlled")
    planning_outcome: Mapped[str] = mapped_column(String(80), default="plan_requires_review", index=True)
    status: Mapped[str] = mapped_column(String(40), default="pending_approval", index=True)
    steps_json: Mapped[list[dict]] = mapped_column(JSON, default=list)
    motion_plan_json: Mapped[dict] = mapped_column(JSON, default=dict)
    simulation_outcome: Mapped[str] = mapped_column(String(80), default="not_run", index=True)
    simulation_status: Mapped[str] = mapped_column(String(40), default="not_run", index=True)
    simulation_json: Mapped[dict] = mapped_column(JSON, default=dict)
    simulation_gate_passed: Mapped[bool] = mapped_column(default=False)
    execution_capability: Mapped[str] = mapped_column(String(120), default="")
    execution_status: Mapped[str] = mapped_column(String(40), default="not_started", index=True)
    execution_id: Mapped[int | None] = mapped_column(ForeignKey("capability_executions.id", ondelete="SET NULL"), nullable=True, index=True)
    execution_json: Mapped[dict] = mapped_column(JSON, default=dict)
    abort_status: Mapped[str] = mapped_column(String(40), default="not_aborted", index=True)
    abort_reason: Mapped[str] = mapped_column(Text, default="")
    queued_task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(80), default="api")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class WorkspaceMonitoringState(Base, TimestampMixin):
    __tablename__ = "workspace_monitoring_states"

    id: Mapped[int] = mapped_column(primary_key=True)
    desired_running: Mapped[bool] = mapped_column(default=False)
    runtime_status: Mapped[str] = mapped_column(String(40), default="stopped", index=True)
    scan_trigger_mode: Mapped[str] = mapped_column(String(40), default="interval")
    interval_seconds: Mapped[int] = mapped_column(default=30)
    freshness_threshold_seconds: Mapped[int] = mapped_column(default=900)
    cooldown_seconds: Mapped[int] = mapped_column(default=10)
    max_scan_rate: Mapped[int] = mapped_column(default=6)
    priority_zones: Mapped[list[str]] = mapped_column(JSON, default=list)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    scan_count: Mapped[int] = mapped_column(default=0)
    last_scan_reason: Mapped[str] = mapped_column(String(120), default="")
    last_deltas_json: Mapped[list[dict]] = mapped_column(JSON, default=list)
    last_proposal_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    last_snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class WorkspaceAutonomousChain(Base, TimestampMixin):
    __tablename__ = "workspace_autonomous_chains"

    id: Mapped[int] = mapped_column(primary_key=True)
    chain_type: Mapped[str] = mapped_column(String(80), default="proposal_sequence", index=True)
    status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    source: Mapped[str] = mapped_column(String(80), default="objective36")
    trigger_reason: Mapped[str] = mapped_column(Text, default="")
    step_proposal_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    step_policy_json: Mapped[dict] = mapped_column(JSON, default=dict)
    stop_on_failure: Mapped[bool] = mapped_column(default=True)
    cooldown_seconds: Mapped[int] = mapped_column(default=0)
    requires_approval: Mapped[bool] = mapped_column(default=True)
    approved_by: Mapped[str] = mapped_column(String(120), default="")
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_advanced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_step_index: Mapped[int] = mapped_column(default=0)
    completed_step_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    failed_step_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    audit_trail_json: Mapped[list[dict]] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class WorkspaceCapabilityChain(Base, TimestampMixin):
    __tablename__ = "workspace_capability_chains"

    id: Mapped[int] = mapped_column(primary_key=True)
    chain_name: Mapped[str] = mapped_column(String(160), index=True)
    chain_type: Mapped[str] = mapped_column(String(80), default="safe_capability_chain", index=True)
    status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    source: Mapped[str] = mapped_column(String(80), default="objective42")
    policy_json: Mapped[dict] = mapped_column(JSON, default=dict)
    steps_json: Mapped[list[dict]] = mapped_column(JSON, default=list)
    current_step_index: Mapped[int] = mapped_column(default=0)
    completed_step_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    failed_step_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    stop_on_failure: Mapped[bool] = mapped_column(default=True)
    escalate_on_failure: Mapped[bool] = mapped_column(default=True)
    last_advanced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    audit_trail_json: Mapped[list[dict]] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class WorkspaceInterruptionEvent(Base, TimestampMixin):
    __tablename__ = "workspace_interruption_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    execution_id: Mapped[int | None] = mapped_column(ForeignKey("capability_executions.id", ondelete="SET NULL"), nullable=True, index=True)
    action_plan_id: Mapped[int | None] = mapped_column(ForeignKey("workspace_action_plans.id", ondelete="SET NULL"), nullable=True, index=True)
    chain_id: Mapped[int | None] = mapped_column(ForeignKey("workspace_autonomous_chains.id", ondelete="SET NULL"), nullable=True, index=True)
    interruption_type: Mapped[str] = mapped_column(String(80), index=True)
    source: Mapped[str] = mapped_column(String(80), default="operator")
    requested_outcome: Mapped[str] = mapped_column(String(40), default="require_operator_decision")
    applied_outcome: Mapped[str] = mapped_column(String(40), default="require_operator_decision")
    status: Mapped[str] = mapped_column(String(40), default="active", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(120), default="workspace")
    resolved_by: Mapped[str] = mapped_column(String(120), default="")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class WorkspaceReplanSignal(Base, TimestampMixin):
    __tablename__ = "workspace_replan_signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    execution_id: Mapped[int | None] = mapped_column(ForeignKey("capability_executions.id", ondelete="SET NULL"), nullable=True, index=True)
    action_plan_id: Mapped[int | None] = mapped_column(ForeignKey("workspace_action_plans.id", ondelete="SET NULL"), nullable=True, index=True)
    chain_id: Mapped[int | None] = mapped_column(ForeignKey("workspace_autonomous_chains.id", ondelete="SET NULL"), nullable=True, index=True)
    signal_type: Mapped[str] = mapped_column(String(80), index=True)
    predicted_outcome: Mapped[str] = mapped_column(String(60), default="continue_monitor", index=True)
    confidence: Mapped[float] = mapped_column(default=0.0)
    source: Mapped[str] = mapped_column(String(80), default="predictive_monitor")
    status: Mapped[str] = mapped_column(String(40), default="active", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(120), default="workspace")
    resolved_by: Mapped[str] = mapped_column(String(120), default="")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class ConstraintEvaluation(Base, TimestampMixin):
    __tablename__ = "constraint_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(80), default="api", index=True)
    actor: Mapped[str] = mapped_column(String(120), default="workspace")
    goal_json: Mapped[dict] = mapped_column(JSON, default=dict)
    action_plan_json: Mapped[dict] = mapped_column(JSON, default=dict)
    workspace_state_json: Mapped[dict] = mapped_column(JSON, default=dict)
    system_state_json: Mapped[dict] = mapped_column(JSON, default=dict)
    policy_state_json: Mapped[dict] = mapped_column(JSON, default=dict)
    decision: Mapped[str] = mapped_column(String(60), default="allowed", index=True)
    violations_json: Mapped[list[dict]] = mapped_column(JSON, default=list)
    warnings_json: Mapped[list[dict]] = mapped_column(JSON, default=list)
    recommended_next_step: Mapped[str] = mapped_column(String(120), default="execute")
    confidence: Mapped[float] = mapped_column(default=0.0)
    explanation_json: Mapped[dict] = mapped_column(JSON, default=dict)
