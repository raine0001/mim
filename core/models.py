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
