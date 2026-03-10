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
    release_tag: str
    config_profile: str
    git_sha: str
    build_timestamp: str
    repo_signature: str
    capabilities: list[str]
    recent_changes: list[str]
    last_updated_at: datetime
    generated_at: datetime
    endpoints: list[str]
    objects: dict[str, list[str]]


class RoutingMetricCreate(BaseModel):
    task_id: int | None = None
    objective_id: int | None = None
    selected_engine: str
    fallback_engine: str = ""
    fallback_used: bool = False
    routing_source: str = "tod.invoke-engine"
    routing_confidence: float = 0.0
    policy_version: str = "routing-policy-v1"
    engine_version: str = "unknown"
    routing_selection_reason: str = ""
    routing_final_outcome: str = "unknown"
    latency_ms: int = 0
    result_category: str = "unknown"
    failure_category: str = ""
    review_outcome: str = "unknown"
    blocked_pre_invocation: bool = False
    metadata_json: dict = Field(default_factory=dict)

    @field_validator("failure_category")
    @classmethod
    def validate_failure_category(cls, value: str) -> str:
        allowed = {
            "",
            "contract_drift_breaking",
            "execution_error",
            "validation_failure",
            "timeout",
            "review_rejection",
            "no_eligible_engine",
        }
        if value not in allowed:
            raise ValueError("unknown failure_category")
        return value


class RoutingMetricOut(BaseModel):
    metric_id: int
    task_id: int | None
    objective_id: int | None
    timestamp: datetime
    selected_engine: str
    fallback_engine: str
    fallback_used: bool
    routing_source: str
    routing_confidence: float
    policy_version: str
    engine_version: str
    routing_selection_reason: str
    routing_final_outcome: str
    latency_ms: int
    result_category: str
    failure_category: str
    review_outcome: str
    blocked_pre_invocation: bool
    metadata_json: dict


class GoalCreate(BaseModel):
    objective_id: int | None = Field(default=None, ge=1)
    task_id: int | None = Field(default=None, ge=1)
    goal_type: str = "task_execution"
    goal_description: str = Field(min_length=1)
    requested_by: str = "tod"
    priority: str = "normal"
    status: str = "new"


class GoalOut(BaseModel):
    goal_id: int
    objective_id: int | None
    task_id: int | None
    goal_type: str
    goal_description: str
    requested_by: str
    priority: str
    status: str
    created_at: datetime


class SnapshotInput(BaseModel):
    state_type: str = "json"
    state_payload: dict = Field(default_factory=dict)


class ActionCreate(BaseModel):
    goal_id: int = Field(ge=1)
    engine: str
    action_type: str
    input_ref: str = ""
    expected_state_delta: dict = Field(default_factory=dict)
    validation_method: str
    sequence_index: int = Field(default=1, ge=1)
    depends_on_action_id: int | None = Field(default=None, ge=1)
    parent_action_id: int | None = Field(default=None, ge=1)
    status: str = "completed"
    pre_state: SnapshotInput
    post_state: SnapshotInput


class ActionOut(BaseModel):
    action_id: int
    goal_id: int
    engine: str
    action_type: str
    input_ref: str
    expected_state_delta: dict
    validation_method: str
    sequence_index: int
    depends_on_action_id: int | None
    parent_action_id: int | None
    retry_of_action_id: int | None
    retry_count: int
    replaced_action_id: int | None
    replacement_action_id: int | None
    recovery_classification: str
    chain_event: str
    started_at: datetime
    completed_at: datetime | None
    status: str


class StateSnapshotOut(BaseModel):
    snapshot_id: int
    goal_id: int
    action_id: int
    snapshot_phase: str
    state_type: str
    state_payload: dict
    captured_at: datetime


class ValidationResultOut(BaseModel):
    validation_id: int
    goal_id: int
    action_id: int
    validation_method: str
    validation_status: str
    validation_details: dict
    validated_at: datetime


class GoalCustodyOut(BaseModel):
    goal: GoalOut
    actions: list[ActionOut]
    snapshots: list[StateSnapshotOut]
    validations: list[ValidationResultOut]


class GoalPlanUpsert(BaseModel):
    ordered_action_ids: list[int] = Field(default_factory=list)
    current_step_index: int = Field(default=0, ge=0)


class GoalPlanOut(BaseModel):
    goal_id: int
    ordered_action_ids: list[int]
    current_step_index: int
    derived_status: str


class GoalTimelineItem(BaseModel):
    action: ActionOut
    snapshots: list[StateSnapshotOut]
    validations: list[ValidationResultOut]


class GoalStatusOut(BaseModel):
    goal_id: int
    derived_status: str
    total_steps: int
    completed_steps: int
    failed_steps: int
    blocked_steps: int
    retried_steps: int
    skipped_steps: int
    recovered_steps: int
    manual_intervention_steps: int


class ActionRetryCreate(BaseModel):
    engine: str | None = None
    action_type: str | None = None
    input_ref: str = ""
    expected_state_delta: dict = Field(default_factory=dict)
    validation_method: str = "expected_delta_compare"
    status: str = "retried"
    recovery_classification: str = "recovered_partial"
    pre_state: SnapshotInput
    post_state: SnapshotInput


class ActionSkipCreate(BaseModel):
    reason: str = "manual_skip"
    continue_to_next_step: bool = True


class ActionReplaceCreate(BaseModel):
    engine: str
    action_type: str
    input_ref: str = ""
    expected_state_delta: dict = Field(default_factory=dict)
    validation_method: str = "expected_delta_compare"
    status: str = "completed"
    recovery_classification: str = "recovered"
    pre_state: SnapshotInput
    post_state: SnapshotInput


class GoalResumeCreate(BaseModel):
    recovery_classification: str = "recovered"
