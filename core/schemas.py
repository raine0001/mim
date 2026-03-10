from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class HealthResponse(BaseModel):
    status: str


class ObjectiveCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    priority: str = "normal"
    constraints: list[str] = Field(default_factory=list)
    success_criteria: str = ""
    status: str = "new"


class ObjectiveOut(BaseModel):
    objective_id: int
    title: str
    description: str
    priority: str
    constraints: list[str]
    success_criteria: str
    status: str
    created_at: datetime


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    scope: str = ""
    dependencies: list[int] = Field(default_factory=list)
    acceptance_criteria: str = ""
    assigned_to: str = "unassigned"
    status: str = "queued"
    objective_id: int | None = None


class TaskOut(BaseModel):
    task_id: int
    objective_id: int | None
    title: str
    scope: str
    dependencies: list[int]
    acceptance_criteria: str
    status: str
    assigned_to: str
    created_at: datetime


class ResultCreate(BaseModel):
    task_id: int
    summary: str
    files_changed: list[str] = Field(default_factory=list)
    tests_run: list[str] = Field(default_factory=list)
    test_results: str = ""
    failures: list[str] = Field(default_factory=list)
    recommendations: str = ""


class ResultOut(BaseModel):
    result_id: int
    task_id: int
    summary: str
    files_changed: list[str]
    tests_run: list[str]
    test_results: str
    failures: list[str]
    recommendations: str
    created_at: datetime


class ReviewCreate(BaseModel):
    task_id: int
    decision: str
    rationale: str = ""
    continue_allowed: bool = False
    escalate_to_user: bool = False


class ReviewOut(BaseModel):
    review_id: int
    task_id: int
    decision: str
    rationale: str
    continue_allowed: bool
    escalate_to_user: bool
    created_at: datetime


class JournalOut(BaseModel):
    entry_id: int
    actor: str
    action: str
    target_type: str
    target_id: str
    idempotency_key: str | None = None
    summary: str
    timestamp: datetime


class JournalCreate(BaseModel):
    actor: str
    action: str
    target_type: str = "system"
    target_id: str = ""
    idempotency_key: str | None = None
    summary: str
    metadata_json: dict = Field(default_factory=dict)

    @field_validator("actor", "action", "summary")
    @classmethod
    def require_non_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must be non-empty")
        return cleaned


class MemoryCreate(BaseModel):
    memory_class: str
    content: str
    summary: str = ""
    metadata_json: dict = Field(default_factory=dict)


class MemoryOut(BaseModel):
    id: int
    memory_class: str
    content: str
    summary: str
    metadata_json: dict
    created_at: datetime


class ManifestResponse(BaseModel):
    system_name: str
    system_version: str
    manifest_version: str
    contract_version: str
    schema_version: str
    app_name: str
    app_version: str
    environment: str
    repo_signature: str
    capabilities: list[str]
    recent_changes: list[str]
    last_updated_at: datetime
    generated_at: datetime
    endpoints: list[str]
    objects: dict[str, list[str]]
