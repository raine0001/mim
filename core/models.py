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
