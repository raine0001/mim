from datetime import datetime

from typing import Any, Literal

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


class ConceptExtractRequest(BaseModel):
    actor: str = "workspace"
    source: str = "objective52"
    lookback_hours: int = Field(default=24, ge=1, le=720)
    min_evidence_count: int = Field(default=3, ge=2, le=500)
    max_concepts: int = Field(default=10, ge=1, le=100)
    metadata_json: dict = Field(default_factory=dict)


class ConceptAcknowledgeRequest(BaseModel):
    actor: str = "operator"
    reason: str = ""
    metadata_json: dict = Field(default_factory=dict)


class ConceptMemoryOut(BaseModel):
    concept_id: int
    source: str
    actor: str
    concept_type: str
    trigger_pattern: str
    evidence_count: int
    confidence: float
    affected_zones: list[str]
    affected_objects: list[str]
    affected_strategies: list[str]
    suggested_implications: list[str]
    evidence_summary: str
    status: str
    acknowledged_by: str
    acknowledged_at: datetime | None
    metadata_json: dict
    created_at: datetime


class DevelopmentPatternOut(BaseModel):
    pattern_id: int
    source: str
    actor: str
    pattern_type: str
    evidence_count: int
    confidence: float
    affected_component: str
    first_seen: datetime
    last_seen: datetime
    evidence_summary: str
    status: str
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


InputSource = Literal["text", "ui", "api", "voice", "vision"]


class NormalizedInputCreate(BaseModel):
    source: InputSource
    raw_input: str = ""
    parsed_intent: str = "unknown"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    target_system: str = "mim"
    requested_goal: str = ""
    safety_flags: list[str] = Field(default_factory=list)
    metadata_json: dict = Field(default_factory=dict)


class NormalizedInputOut(BaseModel):
    input_id: int
    source: InputSource
    raw_input: str
    parsed_intent: str
    confidence: float
    target_system: str
    requested_goal: str
    safety_flags: list[str]
    metadata_json: dict
    normalized: bool
    created_at: datetime


class TextInputAdapterRequest(BaseModel):
    text: str = Field(min_length=1)
    parsed_intent: str = "unknown"
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    target_system: str = "mim"
    requested_goal: str = ""
    safety_flags: list[str] = Field(default_factory=list)
    metadata_json: dict = Field(default_factory=dict)


class UiInputAdapterRequest(BaseModel):
    command: str = Field(min_length=1)
    parsed_intent: str = "unknown"
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    target_system: str = "mim"
    requested_goal: str = ""
    safety_flags: list[str] = Field(default_factory=list)
    metadata_json: dict = Field(default_factory=dict)


class ApiInputAdapterRequest(BaseModel):
    payload: dict = Field(default_factory=dict)
    raw_input: str = ""
    parsed_intent: str = "unknown"
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    target_system: str = "mim"
    requested_goal: str = ""
    safety_flags: list[str] = Field(default_factory=list)
    metadata_json: dict = Field(default_factory=dict)


class VoiceInputAdapterRequest(BaseModel):
    transcript: str = Field(min_length=1)
    parsed_intent: str = "unknown"
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    target_system: str = "mim"
    requested_goal: str = ""
    safety_flags: list[str] = Field(default_factory=list)
    metadata_json: dict = Field(default_factory=dict)


class SpeechOutputRequest(BaseModel):
    message: str = Field(min_length=1)
    voice_profile: str = "default"
    channel: str = "system"
    priority: str = "normal"
    metadata_json: dict = Field(default_factory=dict)


class SpeechOutputOut(BaseModel):
    status: str
    spoken_text: str
    output_action_id: int
    requested_text: str
    voice_profile: str
    channel: str
    priority: str
    delivery_status: str
    failure_reason: str
    metadata_json: dict


class VisionObservationRequest(BaseModel):
    raw_observation: str = Field(min_length=1)
    detected_labels: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    target_system: str = "mim"
    proposed_goal: str = ""
    safety_flags: list[str] = Field(default_factory=lambda: ["requires_confirmation"])
    metadata_json: dict = Field(default_factory=dict)


class LiveCameraObservationItem(BaseModel):
    object_label: str = Field(min_length=1)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    zone: str = "workspace"
    timestamp: datetime | None = None


class LiveCameraAdapterRequest(BaseModel):
    device_id: str = Field(min_length=1)
    source_type: str = "camera"
    session_id: str = ""
    is_remote: bool = False
    observations: list[LiveCameraObservationItem] = Field(default_factory=list)
    min_interval_seconds: int = Field(default=2, ge=0, le=60)
    duplicate_window_seconds: int = Field(default=20, ge=1, le=600)
    observation_confidence_floor: float = Field(default=0.5, ge=0.0, le=1.0)
    metadata_json: dict = Field(default_factory=dict)


class LiveMicAdapterRequest(BaseModel):
    device_id: str = Field(min_length=1)
    source_type: str = "microphone"
    session_id: str = ""
    is_remote: bool = False
    transcript: str = ""
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    timestamp: datetime | None = None
    min_interval_seconds: int = Field(default=1, ge=0, le=30)
    duplicate_window_seconds: int = Field(default=20, ge=1, le=600)
    transcript_confidence_floor: float = Field(default=0.45, ge=0.0, le=1.0)
    discard_low_confidence: bool = True
    metadata_json: dict = Field(default_factory=dict)


class PerceptionSourceOut(BaseModel):
    source_id: int
    source_type: str
    device_id: str
    session_id: str
    is_remote: bool
    status: str
    health_status: str
    last_seen_at: datetime | None
    last_accepted_at: datetime | None
    accepted_count: int
    dropped_count: int
    duplicate_count: int
    low_confidence_count: int
    min_interval_seconds: int
    duplicate_window_seconds: int
    confidence_floor: float
    metadata_json: dict
    created_at: datetime


class CapabilityRegistrationCreate(BaseModel):
    capability_name: str = Field(min_length=1, max_length=120)
    category: str = "action"
    description: str = ""
    requires_confirmation: bool = True
    enabled: bool = True
    safety_policy: dict = Field(default_factory=dict)


class CapabilityRegistrationOut(BaseModel):
    capability_id: int
    capability_name: str
    category: str
    description: str
    requires_confirmation: bool
    enabled: bool
    safety_policy: dict
    created_at: datetime


class EventResolutionOut(BaseModel):
    resolution_id: int
    input_event_id: int
    internal_intent: str
    confidence_tier: str
    outcome: str
    resolution_status: str
    safety_decision: str
    reason: str
    clarification_prompt: str
    escalation_reasons: list[str]
    capability_name: str
    capability_registered: bool
    capability_enabled: bool
    goal_id: int | None
    proposed_goal_description: str
    proposed_actions: list[dict]
    metadata_json: dict
    created_at: datetime


class PromoteEventToGoalRequest(BaseModel):
    requested_by: str = "gateway"
    force: bool = False


class ExecutionDispatchRequest(BaseModel):
    arguments_json: dict = Field(default_factory=dict)
    safety_mode: str = "standard"
    requested_executor: str = "tod"
    force: bool = False


class CapabilityExecutionOut(BaseModel):
    execution_id: int
    input_event_id: int
    resolution_id: int | None
    goal_id: int | None
    capability_name: str
    arguments_json: dict
    safety_mode: str
    requested_executor: str
    dispatch_decision: str
    trace_id: str = ""
    managed_scope: str = "global"
    status: str
    reason: str
    feedback_json: dict
    execution_truth: dict = Field(default_factory=dict)
    created_at: datetime


class ExecutionTruthV1(BaseModel):
    execution_id: int | None = Field(default=None, ge=1)
    capability_name: str = ""
    expected_duration_ms: int | None = Field(default=None, ge=0)
    actual_duration_ms: int | None = Field(default=None, ge=0)
    duration_delta_ratio: float | None = None
    retry_count: int = Field(default=0, ge=0)
    fallback_used: bool = False
    runtime_outcome: str = ""
    environment_shift_detected: bool = False
    simulation_match_status: Literal[
        "matched", "partial_match", "mismatch", "unknown"
    ] = "unknown"
    truth_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    published_at: datetime

    @field_validator("capability_name", "runtime_outcome")
    @classmethod
    def strip_string_fields(cls, value: str) -> str:
        return value.strip()

    @field_validator("duration_delta_ratio")
    @classmethod
    def validate_duration_delta_ratio(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return float(value)


class ExecutionFeedbackUpdateRequest(BaseModel):
    status: str = ""
    reason: str = ""
    runtime_outcome: str = ""
    recovery_state: str = ""
    execution_truth: ExecutionTruthV1 | None = None
    correlation_json: dict = Field(default_factory=dict)
    feedback_json: dict = Field(default_factory=dict)
    actor: str = "executor"


class ExecutionFeedbackOut(BaseModel):
    execution_id: int
    status: str
    reason: str
    feedback_json: dict
    execution_truth: dict = Field(default_factory=dict)


class CapabilityExecutionHandoffOut(BaseModel):
    execution_id: int
    goal_ref: dict
    action_ref: dict
    capability_name: str
    arguments_json: dict
    safety_mode: str
    requested_executor: str
    dispatch_decision: str
    status: str
    correlation_metadata: dict


class OperatorExecutionActionRequest(BaseModel):
    actor: str = "operator"
    reason: str = ""
    metadata_json: dict = Field(default_factory=dict)


class ExecutionOverrideRequest(BaseModel):
    actor: str = "operator"
    managed_scope: str = "global"
    override_type: Literal["hard_stop", "pause", "redirect"]
    reason: str = ""
    execution_id: int | None = Field(default=None, ge=1)
    trace_id: str = ""
    priority: str = "operator"
    metadata_json: dict = Field(default_factory=dict)

    @field_validator("managed_scope", "override_type", "priority")
    @classmethod
    def strip_override_fields(cls, value: str) -> str:
        return value.strip()


class ExecutionStabilityEvaluateRequest(BaseModel):
    actor: str = "system"
    source: str = "execution_control"
    managed_scope: str = Field(default="global", min_length=1, max_length=120)
    trace_id: str = ""
    metadata_json: dict = Field(default_factory=dict)


class ExecutionRecoveryEvaluateRequest(BaseModel):
    actor: str = "system"
    source: str = "execution_control"
    trace_id: str = ""
    execution_id: int | None = Field(default=None, ge=1)
    managed_scope: str = ""
    metadata_json: dict = Field(default_factory=dict)


class ExecutionRecoveryAttemptRequest(BaseModel):
    actor: str = "system"
    source: str = "execution_control"
    trace_id: str = ""
    execution_id: int | None = Field(default=None, ge=1)
    managed_scope: str = ""
    requested_decision: str = ""
    reason: str = ""
    operator_ack: bool = False
    metadata_json: dict = Field(default_factory=dict)


class ExecutionRecoveryOutcomeEvaluateRequest(BaseModel):
    actor: str = "system"
    source: str = "execution_control"
    trace_id: str = ""
    execution_id: int | None = Field(default=None, ge=1)
    managed_scope: str = ""
    metadata_json: dict = Field(default_factory=dict)


class ExecutionRecoveryLearningResetRequest(BaseModel):
    actor: str = "operator"
    managed_scope: str = Field(default="global", min_length=1, max_length=120)
    reason: str = ""
    capability_family: str = ""
    recovery_decision: str = ""
    metadata_json: dict = Field(default_factory=dict)


class ExecutionRecoveryPolicyTuningApplyRequest(BaseModel):
    actor: str = "operator"
    source: str = "execution_control"
    trace_id: str = ""
    execution_id: int | None = Field(default=None, ge=1)
    managed_scope: str = ""
    reason: str = ""
    duration_seconds: int | None = Field(default=1800, ge=1, le=604800)
    authority_level: str = "operator_required"
    metadata_json: dict = Field(default_factory=dict)


class ExecutionRecoveryPolicyCommitmentEvaluateRequest(BaseModel):
    actor: str = "workspace"
    source: str = "objective122"
    trace_id: str = ""
    execution_id: int | None = Field(default=None, ge=1)
    managed_scope: str = ""
    lookback_hours: int = Field(default=168, ge=1, le=720)
    target_status: str = ""
    metadata_json: dict = Field(default_factory=dict)


class ExecutionRecoveryPolicyCommitmentPreviewRequest(BaseModel):
    actor: str = "operator"
    source: str = "objective128"
    action: str = Field(default="apply", min_length=1, max_length=40)
    managed_scope: str = ""
    commitment_id: int | None = Field(default=None, ge=1)
    trace_id: str = ""
    execution_id: int | None = Field(default=None, ge=1)
    metadata_json: dict = Field(default_factory=dict)

    @field_validator("action")
    @classmethod
    def validate_preview_action(cls, value: str) -> str:
        normalized = value.strip().lower()
        allowed = {"apply", "reapply", "expire", "revoke", "reset"}
        if normalized not in allowed:
            raise ValueError("unsupported recovery commitment preview action")
        return normalized


class ExecutionStrategyPlanCreateRequest(BaseModel):
    actor: str = "system"
    source: str = "objective131"
    trace_id: str = ""
    intent_id: int | None = Field(default=None, ge=1)
    orchestration_id: int | None = Field(default=None, ge=1)
    execution_id: int | None = Field(default=None, ge=1)
    managed_scope: str = ""


class ExecutionStrategyPlanAdvanceRequest(BaseModel):
    actor: str = "system"
    source: str = "objective134"
    completed_step_key: str = Field(min_length=1, max_length=120)
    outcome: Literal["completed", "failed", "blocked", "skipped"] = "completed"
    observed_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    metadata_json: dict = Field(default_factory=dict)


class ExecutionStrategyPlanOut(BaseModel):
    strategy_plan_id: int
    trace_id: str
    intent_id: int | None = None
    orchestration_id: int | None = None
    execution_id: int | None = None
    source: str
    actor: str
    managed_scope: str
    status: str
    plan_family: str
    canonical_intent: str
    goal_summary: str
    primary_plan: list[dict] = Field(default_factory=list)
    alternative_plans: list[dict] = Field(default_factory=list)
    contingency_rules: list[dict] = Field(default_factory=list)
    coordination_domains: list[str] = Field(default_factory=list)
    continuation_state: dict = Field(default_factory=dict)
    explainability: dict = Field(default_factory=dict)
    confidence: float
    metadata_json: dict = Field(default_factory=dict)
    created_at: datetime


class ExecutionRecoveryLearningOut(BaseModel):
    recovery_learning_profile_id: int
    managed_scope: str
    capability_family: str
    capability_name: str
    recovery_decision: str
    learning_state: str
    escalation_decision: str
    rationale: str
    why_recovery_escalated_before_retry: str
    confidence: float
    sample_count: int
    recovered_count: int
    failed_again_count: int
    operator_required_count: int
    success_rate: float
    evidence_json: dict
    policy_effects_json: dict
    metadata_json: dict
    created_at: datetime


class ExecutionRecoveryDecisionOut(BaseModel):
    trace_id: str
    execution_id: int | None
    managed_scope: str
    execution_status: str
    dispatch_decision: str
    recovery_decision: str
    recommended_attempt_decision: str
    recovery_reason: str
    operator_action_required: bool
    recovery_allowed: bool
    resume_step_key: str
    attempt_number: int
    retry_pressure: int
    mitigation_state: str
    active_override_types: list[str]
    latest_attempt: dict = Field(default_factory=dict)
    latest_outcome: dict = Field(default_factory=dict)
    recovery_learning: dict = Field(default_factory=dict)
    why_recovery_escalated_before_retry: str = ""
    conflict_resolution: dict = Field(default_factory=dict)
    checkpoint_json: dict = Field(default_factory=dict)
    recovery_policy_tuning: dict = Field(default_factory=dict)


class ExecutionTraceEventOut(BaseModel):
    trace_event_id: int
    trace_id: str
    execution_id: int | None
    intent_id: int | None
    event_type: str
    event_stage: str
    causality_role: str
    summary: str
    payload_json: dict
    created_at: datetime


class ExecutionIntentOut(BaseModel):
    intent_id: int
    trace_id: str
    managed_scope: str
    intent_key: str
    lifecycle_status: str
    intent_type: str
    requested_goal: str
    capability_name: str
    arguments_json: dict
    context_json: dict
    last_execution_id: int | None
    resumption_count: int
    archived_at: datetime | None
    created_at: datetime


class ExecutionTaskOrchestrationOut(BaseModel):
    orchestration_id: int
    trace_id: str
    intent_id: int | None
    execution_id: int | None
    managed_scope: str
    orchestration_status: str
    current_step_key: str
    step_state_json: list[dict]
    checkpoint_json: dict
    retry_count: int
    rollback_state_json: dict
    metadata_json: dict
    created_at: datetime


class ExecutionOverrideOut(BaseModel):
    override_id: int
    trace_id: str
    execution_id: int | None
    managed_scope: str
    override_type: str
    reason: str
    status: str
    priority: str
    scope_json: dict
    metadata_json: dict
    created_at: datetime


class ExecutionStabilityOut(BaseModel):
    stability_id: int
    trace_id: str
    managed_scope: str
    status: str
    mitigation_state: str
    drift_score: float
    oscillation_score: float
    degradation_score: float
    metrics_json: dict
    triggers_json: list[dict]
    metadata_json: dict
    created_at: datetime


class ExecutionRecoveryAttemptOut(BaseModel):
    recovery_attempt_id: int
    trace_id: str
    execution_id: int | None
    managed_scope: str
    recovery_decision: str
    recovery_reason: str
    attempt_number: int
    resume_step_key: str
    source: str
    actor: str
    status: str
    result_json: dict
    metadata_json: dict
    recovery_policy_tuning: dict = Field(default_factory=dict)
    created_at: datetime


class ExecutionRecoveryOutcomeOut(BaseModel):
    recovery_outcome_id: int
    attempt_id: int | None
    trace_id: str
    execution_id: int | None
    managed_scope: str
    outcome_status: str
    outcome_reason: str
    learning_bias_json: dict
    outcome_score: float
    result_json: dict
    metadata_json: dict
    recovery_policy_tuning: dict = Field(default_factory=dict)
    created_at: datetime


class ExecutionTraceOut(BaseModel):
    trace_id: str
    managed_scope: str
    capability_name: str
    lifecycle_status: str
    current_stage: str
    root_execution_id: int | None
    root_intent_id: int | None
    causality_graph_json: dict
    metadata_json: dict
    created_at: datetime
    events: list[ExecutionTraceEventOut] = Field(default_factory=list)
    intent: ExecutionIntentOut | None = None
    orchestration: ExecutionTaskOrchestrationOut | None = None
    stability: ExecutionStabilityOut | None = None


class OperatorResolutionCommitmentCreateRequest(BaseModel):
    actor: str = "operator"
    managed_scope: str = Field(min_length=1, max_length=120)
    decision_type: str = Field(min_length=1, max_length=80)
    reason: str = ""
    recommendation_snapshot_json: dict = Field(default_factory=dict)
    commitment_family: str = ""
    authority_level: str = "governance_override"
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    expires_at: datetime | None = None
    duration_seconds: int | None = Field(default=None, ge=1, le=604800)
    provenance_json: dict = Field(default_factory=dict)
    downstream_effects_json: dict = Field(default_factory=dict)
    metadata_json: dict = Field(default_factory=dict)

    @field_validator("managed_scope", "decision_type", "authority_level")
    @classmethod
    def require_non_blank_commitment_fields(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must be non-empty")
        return cleaned


class OperatorResolutionCommitmentOut(BaseModel):
    commitment_id: int
    source: str
    created_by: str
    managed_scope: str
    commitment_family: str
    decision_type: str
    status: str
    reason: str
    recommendation_snapshot_json: dict
    authority_level: str
    confidence: float
    provenance_json: dict
    expires_at: datetime | None = None
    superseded_by_commitment_id: int | None = None
    downstream_effects_json: dict
    metadata_json: dict
    created_at: datetime
    active: bool
    expired: bool


class OperatorResolutionCommitmentMonitoringEvaluateRequest(BaseModel):
    actor: str = "workspace"
    source: str = "objective86"
    lookback_hours: int = Field(default=168, ge=1, le=720)
    metadata_json: dict = Field(default_factory=dict)


class OperatorResolutionCommitmentMonitoringOut(BaseModel):
    monitoring_id: int
    source: str
    actor: str
    commitment_id: int
    managed_scope: str
    status: str
    commitment_status: str
    monitoring_window_hours: int
    evidence_count: int
    stewardship_cycle_count: int
    maintenance_run_count: int
    inquiry_question_count: int
    blocked_auto_execution_count: int
    allowed_auto_execution_count: int
    potential_violation_count: int
    drift_score: float
    compliance_score: float
    health_score: float
    governance_state: str
    governance_decision: str
    governance_reason: str
    trigger_counts: dict
    trigger_evidence: dict
    recommended_actions: list[dict]
    reasoning: dict
    metadata_json: dict
    created_at: datetime


class OperatorResolutionCommitmentOutcomeEvaluateRequest(BaseModel):
    actor: str = "workspace"
    source: str = "objective87"
    lookback_hours: int = Field(default=168, ge=1, le=720)
    target_status: str = ""
    metadata_json: dict = Field(default_factory=dict)

    @field_validator("target_status")
    @classmethod
    def normalize_commitment_outcome_target_status(cls, value: str) -> str:
        normalized = value.strip().lower()
        allowed = {
            "",
            "satisfied",
            "abandoned",
            "ineffective",
            "harmful",
            "superseded",
        }
        if normalized not in allowed:
            raise ValueError("unsupported commitment outcome target status")
        return normalized


class OperatorResolutionCommitmentResolveRequest(BaseModel):
    actor: str = "operator"
    source: str = "objective87"
    target_status: str = Field(min_length=1, max_length=40)
    reason: str = ""
    lookback_hours: int = Field(default=168, ge=1, le=720)
    metadata_json: dict = Field(default_factory=dict)

    @field_validator("target_status")
    @classmethod
    def require_terminal_commitment_status(cls, value: str) -> str:
        normalized = value.strip().lower()
        allowed = {"satisfied", "abandoned", "ineffective", "harmful", "superseded"}
        if normalized not in allowed:
            raise ValueError("target_status must be a terminal commitment outcome")
        return normalized


class OperatorResolutionCommitmentOutcomeOut(BaseModel):
    outcome_id: int
    source: str
    actor: str
    commitment_id: int
    managed_scope: str
    commitment_family: str
    decision_type: str
    status: str
    commitment_status: str
    outcome_status: str
    outcome_reason: str
    evaluation_window_hours: int
    evidence_count: int
    monitoring_profile_count: int
    stewardship_cycle_count: int
    maintenance_run_count: int
    inquiry_question_count: int
    execution_count: int
    retry_count: int
    blocked_auto_execution_count: int
    allowed_auto_execution_count: int
    potential_violation_count: int
    governance_conflict_count: int
    effectiveness_score: float
    stability_score: float
    retry_pressure_score: float
    learning_confidence: float
    learning_signals: dict
    pattern_summary: dict
    recommended_actions: list[dict]
    reasoning: dict
    metadata_json: dict
    created_at: datetime


class WorkspaceObservationOut(BaseModel):
    observation_id: int
    timestamp: datetime
    zone: str
    detected_object: str
    confidence: float
    effective_confidence: float
    freshness_state: str
    source: str
    related_execution_id: int | None
    lifecycle_status: str
    observation_count: int
    metadata_json: dict


class WorkspaceObservationListOut(BaseModel):
    observations: list[WorkspaceObservationOut]


class WorkspaceObjectMemoryOut(BaseModel):
    object_memory_id: int
    canonical_name: str
    aliases: list[str]
    confidence: float
    effective_confidence: float
    zone: str
    first_seen_at: datetime
    last_seen_at: datetime
    status: str
    last_execution_id: int | None
    location_history: list[dict]
    metadata_json: dict


class WorkspaceObjectMemoryListOut(BaseModel):
    objects: list[WorkspaceObjectMemoryOut]


class WorkspaceObjectLibrarySummaryOut(BaseModel):
    total_objects: int
    promoted_objects: int
    semantic_objects: int
    active_objects: int
    uncertain_objects: int
    missing_objects: int
    stale_objects: int
    execution_backed_objects: int


class WorkspaceObjectLibraryEntryOut(WorkspaceObjectMemoryOut):
    promoted: bool
    library_score: float
    semantic_fields: list[str]
    promotion_reasons: list[str]


class WorkspaceObjectLibraryListOut(BaseModel):
    summary: WorkspaceObjectLibrarySummaryOut
    objects: list[WorkspaceObjectLibraryEntryOut]


class WorkspaceProposalActionRequest(BaseModel):
    actor: str = "operator"
    reason: str = ""
    metadata_json: dict = Field(default_factory=dict)


class WorkspaceProposalArbitrationOutcomeRecordRequest(BaseModel):
    actor: str = "tod"
    source: str = "objective88_2"
    proposal_id: int | None = Field(default=None, ge=1)
    proposal_type: str = ""
    related_zone: str = ""
    arbitration_decision: Literal[
        "won", "lost", "merged", "isolated", "suppressed", "superseded"
    ] = "won"
    arbitration_posture: str = "isolate"
    trust_chain_status: str = "verified"
    downstream_execution_outcome: str = ""
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    reason: str = ""
    conflict_context_json: dict = Field(default_factory=dict)
    commitment_state_json: dict = Field(default_factory=dict)
    metadata_json: dict = Field(default_factory=dict)


class WorkspaceProposalArbitrationOutcomeOut(BaseModel):
    outcome_id: int
    source: str
    actor: str
    proposal_id: int | None = None
    proposal_type: str
    related_zone: str
    arbitration_decision: str
    arbitration_posture: str
    trust_chain_status: str
    downstream_execution_outcome: str
    outcome_score: float
    confidence: float
    arbitration_reason: str
    conflict_context_json: dict
    commitment_state_json: dict
    metadata_json: dict
    created_at: datetime


class WorkspaceProposalArbitrationLearningOut(BaseModel):
    proposal_type: str
    related_zone: str
    sample_count: int
    win_count: int
    loss_count: int
    merged_count: int
    isolated_count: int
    weighted_success_rate: float
    confidence: float
    priority_bias: float
    suppression_recommended: bool
    learned_posture: str
    reasoning: str
    recent_outcomes: list[dict]
    applied: bool


class WorkspaceProposalPolicyPreferenceOut(BaseModel):
    profile_id: int | None = None
    managed_scope: str
    proposal_family: str
    proposal_type: str
    policy_state: str
    preference_direction: str
    convergence_confidence: float
    sample_count: int
    win_count: int
    loss_count: int
    merge_count: int
    weighted_success_rate: float
    recent_success_rate: float
    contradictory_recent_signal: bool = False
    stale_signal: bool = False
    suppression_threshold_met: bool = False
    policy_effects_json: dict
    evidence_summary_json: dict
    metadata_json: dict
    rationale: str
    applied: bool = False
    updated_at: datetime | None = None


class WorkspacePolicyConflictProfileOut(BaseModel):
    profile_id: int
    managed_scope: str
    decision_family: str
    proposal_type: str
    conflict_state: str
    winning_policy_source: str
    losing_policy_sources: list[str]
    precedence_rule: str
    conflict_confidence: float
    oscillation_count: int
    cooldown_until: datetime | None = None
    cooldown_active: bool = False
    resolution_reason_json: dict
    evidence_summary_json: dict
    candidate_policies_json: list[dict]
    policy_effects_json: dict
    metadata_json: dict
    updated_at: datetime | None = None


class UserPreferenceUpsertRequest(BaseModel):
    user_id: str = "operator"
    preference_type: str = Field(min_length=1)
    value: Any = None
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    source: str = "manual"


class UserPreferenceOut(BaseModel):
    user_id: str
    preference_type: str
    value: Any = None
    confidence: float
    source: str
    last_updated: datetime | None = None
    is_default: bool = False


class OperatorLearnedPreferenceConvergeRequest(BaseModel):
    actor: str = "workspace"
    source: str = "objective88"
    managed_scope: str = ""
    decision_type: str = ""
    commitment_family: str = ""
    lookback_hours: int = Field(default=720, ge=1, le=2160)
    min_evidence: int = Field(default=3, ge=1, le=20)


class OperatorLearnedPreferenceOut(BaseModel):
    preference_key: str
    managed_scope: str
    preference_family: str
    decision_type: str
    preference_status: str
    preference_direction: str
    strength_score: float
    confidence_score: float
    evidence_count: int
    success_count: int
    failure_count: int
    override_count: int
    conflict_state: str
    winning_rule: str
    policy_effects_json: dict
    evidence_summary_json: dict
    metadata_json: dict
    source: str
    last_updated: datetime | None = None
    age_hours: float = 0.0
    freshness_score: float = 0.0
    normalized_strength_score: float = 0.0
    effective_strength_score: float = 0.0
    arbitration_scope: str = ""
    arbitration_state: str = ""
    precedence_rule: str = ""
    arbitration_reasoning_json: dict = Field(default_factory=dict)


class WorkspaceProposalPriorityPolicyUpdateRequest(BaseModel):
    actor: str = "operator"
    reason: str = ""
    urgency_map: dict[str, float] = Field(default_factory=dict)
    zone_importance: dict[str, float] = Field(default_factory=dict)
    operator_preference: dict[str, float] = Field(default_factory=dict)
    age_saturation_minutes: int | None = Field(default=None, ge=1, le=1440)
    weights: dict[str, float] = Field(default_factory=dict)
    metadata_json: dict = Field(default_factory=dict)


class WorkspaceTargetResolveRequest(BaseModel):
    target_label: str = Field(min_length=1)
    preferred_zone: str = ""
    source: str = "api"
    unsafe_zones: list[str] = Field(default_factory=list)
    create_proposal: bool = True


class WorkspaceTargetConfirmRequest(BaseModel):
    actor: str = "operator"
    reason: str = ""
    metadata_json: dict = Field(default_factory=dict)


WorkspaceActionType = Literal[
    "observe",
    "rescan",
    "speak",
    "prepare_reach_plan",
    "request_confirmation",
]


class WorkspaceActionPlanCreateRequest(BaseModel):
    target_resolution_id: int = Field(ge=1)
    action_type: WorkspaceActionType = "prepare_reach_plan"
    source: str = "api"
    notes: str = ""
    motion_plan_overrides: dict = Field(default_factory=dict)
    metadata_json: dict = Field(default_factory=dict)


class WorkspaceActionPlanDecisionRequest(BaseModel):
    actor: str = "operator"
    reason: str = ""
    metadata_json: dict = Field(default_factory=dict)


class WorkspaceActionPlanHandoffRequest(BaseModel):
    actor: str = "operator"
    reason: str = ""
    requested_executor: str = "tod"
    metadata_json: dict = Field(default_factory=dict)


class WorkspaceActionPlanSimulationRequest(BaseModel):
    actor: str = "operator"
    reason: str = ""
    collision_risk_threshold: float = Field(default=0.45, ge=0.0, le=1.0)
    metadata_json: dict = Field(default_factory=dict)


WorkspaceExecutionCapability = Literal[
    "reach_target",
    "arm_move_safe",
]


class WorkspaceActionPlanExecuteRequest(BaseModel):
    actor: str = "operator"
    reason: str = ""
    requested_executor: str = "tod"
    capability_name: WorkspaceExecutionCapability = "reach_target"
    collision_risk_threshold: float = Field(default=0.45, ge=0.0, le=1.0)
    target_confidence_minimum: float = Field(default=0.7, ge=0.0, le=1.0)
    metadata_json: dict = Field(default_factory=dict)


class WorkspaceActionPlanAbortRequest(BaseModel):
    actor: str = "operator"
    reason: str = ""
    metadata_json: dict = Field(default_factory=dict)


class WorkspaceExecutionProposalCreateRequest(BaseModel):
    actor: str = "workspace"
    reason: str = ""
    metadata_json: dict = Field(default_factory=dict)


class WorkspaceExecutionProposalActionRequest(BaseModel):
    actor: str = "operator"
    reason: str = ""
    requested_executor: str = "tod"
    capability_name: WorkspaceExecutionCapability = "reach_target"
    collision_risk_threshold: float = Field(default=0.45, ge=0.0, le=1.0)
    target_confidence_minimum: float = Field(default=0.7, ge=0.0, le=1.0)
    metadata_json: dict = Field(default_factory=dict)


WorkspaceMonitoringTriggerMode = Literal[
    "interval",
    "freshness",
]


class WorkspaceMonitoringStartRequest(BaseModel):
    actor: str = "workspace"
    reason: str = ""
    trigger_mode: WorkspaceMonitoringTriggerMode = "interval"
    interval_seconds: int = Field(default=30, ge=1, le=3600)
    freshness_threshold_seconds: int = Field(default=900, ge=30, le=86400)
    cooldown_seconds: int = Field(default=10, ge=0, le=3600)
    max_scan_rate: int = Field(default=6, ge=1, le=120)
    priority_zones: list[str] = Field(default_factory=list)
    metadata_json: dict = Field(default_factory=dict)


class WorkspaceMonitoringStopRequest(BaseModel):
    actor: str = "workspace"
    reason: str = ""
    preserve_desired_running: bool = False
    metadata_json: dict = Field(default_factory=dict)


class WorkspaceAutonomyOverrideRequest(BaseModel):
    actor: str = "operator"
    reason: str = ""
    auto_execution_enabled: bool | None = None
    force_manual_approval: bool | None = None
    max_auto_actions_per_minute: int | None = Field(default=None, ge=1, le=120)
    max_auto_tasks_per_window: int | None = Field(default=None, ge=1, le=240)
    auto_window_seconds: int | None = Field(default=None, ge=10, le=3600)
    cooldown_between_actions_seconds: int | None = Field(default=None, ge=0, le=3600)
    capability_cooldown_seconds: dict[str, int] = Field(default_factory=dict)
    zone_action_limits: dict[str, int] = Field(default_factory=dict)
    restricted_zones: list[str] = Field(default_factory=list)
    auto_safe_confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    auto_preferred_confidence_threshold: float | None = Field(
        default=None, ge=0.0, le=1.0
    )
    low_risk_score_max: float | None = Field(default=None, ge=0.0, le=1.0)
    max_autonomy_retries: int | None = Field(default=None, ge=0, le=5)
    reset_auto_history: bool = False
    pause_monitoring_loop: bool = False
    metadata_json: dict = Field(default_factory=dict)


class WorkspaceAutonomousChainCreateRequest(BaseModel):
    actor: str = "workspace"
    reason: str = ""
    chain_type: str = "proposal_sequence"
    proposal_ids: list[int] = Field(default_factory=list)
    source: str = "objective36"
    step_policy_json: dict = Field(default_factory=dict)
    stop_on_failure: bool = True
    cooldown_seconds: int = Field(default=0, ge=0, le=3600)
    requires_approval: bool = True
    metadata_json: dict = Field(default_factory=dict)


class WorkspaceAutonomousChainAdvanceRequest(BaseModel):
    actor: str = "workspace"
    reason: str = ""
    force: bool = False
    metadata_json: dict = Field(default_factory=dict)


class WorkspaceAutonomousChainApprovalRequest(BaseModel):
    actor: str = "operator"
    reason: str = ""
    metadata_json: dict = Field(default_factory=dict)


class WorkspaceCapabilityChainCreateRequest(BaseModel):
    actor: str = "workspace"
    reason: str = ""
    chain_name: str = Field(min_length=1, max_length=160)
    chain_type: str = "safe_capability_chain"
    source: str = "objective42"
    steps: list[dict] = Field(default_factory=list)
    policy_json: dict = Field(default_factory=dict)
    stop_on_failure: bool = True
    escalate_on_failure: bool = True
    metadata_json: dict = Field(default_factory=dict)


class WorkspaceCapabilityChainAdvanceRequest(BaseModel):
    actor: str = "workspace"
    reason: str = ""
    force: bool = False
    metadata_json: dict = Field(default_factory=dict)


class WorkspaceHumanAwareSignalUpdateRequest(BaseModel):
    actor: str = "workspace"
    reason: str = ""
    human_in_workspace: bool | None = None
    human_near_target_zone: bool | None = None
    human_near_motion_path: bool | None = None
    shared_workspace_active: bool | None = None
    operator_present: bool | None = None
    occupied_zones: list[str] = Field(default_factory=list)
    high_proximity_zones: list[str] = Field(default_factory=list)
    metadata_json: dict = Field(default_factory=dict)


class ConstraintEvaluateRequest(BaseModel):
    actor: str = "workspace"
    source: str = "api"
    goal: dict = Field(default_factory=dict)
    action_plan: dict = Field(default_factory=dict)
    workspace_state: dict = Field(default_factory=dict)
    system_state: dict = Field(default_factory=dict)
    policy_state: dict = Field(default_factory=dict)
    metadata_json: dict = Field(default_factory=dict)


class ConstraintOutcomeRecordRequest(BaseModel):
    actor: str = "workspace"
    evaluation_id: int = Field(ge=1)
    result: str = "unknown"
    outcome_quality: float = Field(default=0.0, ge=0.0, le=1.0)
    metadata_json: dict = Field(default_factory=dict)


class ConstraintLearningGenerateProposalsRequest(BaseModel):
    actor: str = "workspace"
    source: str = "objective45"
    min_samples: int = Field(default=5, ge=1, le=1000)
    success_rate_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    max_proposals: int = Field(default=5, ge=1, le=50)
    metadata_json: dict = Field(default_factory=dict)


class HorizonPlanGoalCandidate(BaseModel):
    goal_key: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=200)
    priority: str = "normal"
    goal_type: str = "general"
    dependencies: list[str] = Field(default_factory=list)
    estimated_steps: int = Field(default=1, ge=1, le=100)
    expected_value: float = Field(default=0.5, ge=0.0, le=1.0)
    urgency: float = Field(default=0.5, ge=0.0, le=1.0)
    requires_fresh_map: bool = False
    requires_high_confidence: bool = False
    is_physical: bool = False
    metadata_json: dict = Field(default_factory=dict)


class HorizonPlanCreateRequest(BaseModel):
    actor: str = "workspace"
    source: str = "objective46"
    planning_horizon_minutes: int = Field(default=120, ge=10, le=1440)
    goal_candidates: list[HorizonPlanGoalCandidate] = Field(default_factory=list)
    expected_future_constraints: list[dict] = Field(default_factory=list)
    priority_policy: dict = Field(default_factory=dict)
    map_freshness_seconds: int = Field(default=0, ge=0, le=86400)
    object_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    human_aware_state: dict = Field(default_factory=dict)
    operator_preferences: dict = Field(default_factory=dict)
    metadata_json: dict = Field(default_factory=dict)


class HorizonCheckpointAdvanceRequest(BaseModel):
    actor: str = "workspace"
    reason: str = ""
    outcome: Literal[
        "checkpoint_reached",
        "needs_re_evaluation",
        "replanned",
        "complete",
    ] = "checkpoint_reached"
    checkpoint_id: int | None = Field(default=None, ge=1)
    metadata_json: dict = Field(default_factory=dict)


class HorizonFutureDriftRequest(BaseModel):
    actor: str = "workspace"
    reason: str = ""
    drift_type: str = Field(min_length=1, max_length=120)
    observed_value: str = ""
    metadata_json: dict = Field(default_factory=dict)


class EnvironmentStrategyCondition(BaseModel):
    condition_type: str = Field(min_length=1, max_length=120)
    target_scope: str = Field(default="workspace", max_length=160)
    severity: float = Field(default=0.5, ge=0.0, le=1.0)
    occurrence_count: int = Field(default=1, ge=1, le=10000)
    metadata_json: dict = Field(default_factory=dict)


class EnvironmentStrategyGenerateRequest(BaseModel):
    actor: str = "workspace"
    source: str = "objective47"
    observed_conditions: list[EnvironmentStrategyCondition] = Field(
        default_factory=list
    )
    min_severity: float = Field(default=0.4, ge=0.0, le=1.0)
    max_strategies: int = Field(default=5, ge=1, le=25)
    metadata_json: dict = Field(default_factory=dict)


class EnvironmentStrategyRoutineGenerateRequest(BaseModel):
    actor: str = "workspace"
    source: str = "objective48"
    lookback_hours: int = Field(default=24, ge=1, le=720)
    min_occurrence_count: int = Field(default=3, ge=2, le=500)
    max_strategies: int = Field(default=5, ge=1, le=25)
    metadata_json: dict = Field(default_factory=dict)


class EnvironmentStrategyResolveRequest(BaseModel):
    actor: str = "operator"
    reason: str = ""
    status: Literal[
        "active",
        "stable",
        "blocked",
        "completed",
        "superseded",
    ] = "stable"
    metadata_json: dict = Field(default_factory=dict)


class EnvironmentStrategyDeactivateRequest(BaseModel):
    actor: str = "operator"
    reason: str = ""
    metadata_json: dict = Field(default_factory=dict)


class DecisionRecordOut(BaseModel):
    decision_id: int
    decision_type: str
    source_context: dict
    relevant_state: dict
    preferences_applied: dict
    constraints_applied: list[dict]
    strategies_applied: list[dict]
    options_considered: list[dict]
    selected_option: dict
    decision_reason: str
    confidence: float
    result_quality: float
    resulting_goal_or_plan_id: str
    metadata_json: dict
    created_at: datetime


class ImprovementProposalGenerateRequest(BaseModel):
    actor: str = "workspace"
    source: str = "objective49"
    lookback_hours: int = Field(default=24, ge=1, le=720)
    min_occurrence_count: int = Field(default=3, ge=2, le=500)
    max_proposals: int = Field(default=8, ge=1, le=50)
    metadata_json: dict = Field(default_factory=dict)


class ImprovementProposalReviewRequest(BaseModel):
    actor: str = "operator"
    reason: str = ""
    metadata_json: dict = Field(default_factory=dict)


class ImprovementRecommendationGenerateRequest(BaseModel):
    actor: str = "workspace"
    source: str = "objective54"
    lookback_hours: int = Field(default=24, ge=1, le=720)
    min_occurrence_count: int = Field(default=2, ge=2, le=500)
    max_recommendations: int = Field(default=5, ge=1, le=50)
    include_existing_open_proposals: bool = True
    metadata_json: dict = Field(default_factory=dict)


class ImprovementRecommendationReviewRequest(BaseModel):
    actor: str = "operator"
    reason: str = ""
    metadata_json: dict = Field(default_factory=dict)


class ImprovementBacklogRefreshRequest(BaseModel):
    actor: str = "workspace"
    source: str = "objective55"
    lookback_hours: int = Field(default=24, ge=1, le=720)
    min_occurrence_count: int = Field(default=2, ge=2, le=500)
    max_items: int = Field(default=50, ge=1, le=500)
    auto_experiment_limit: int = Field(default=3, ge=0, le=50)
    metadata_json: dict = Field(default_factory=dict)


class ImprovementBacklogOut(BaseModel):
    improvement_id: int
    proposal_id: int
    recommendation_id: int | None
    priority_score: float
    proposal_type: str
    evidence_count: int
    risk_level: str
    impact_estimate: float
    evidence_strength: float
    affected_capabilities: list[str]
    operator_preference_weight: float
    governance_decision: str
    status: str
    why_ranked: str
    evidence_summary: str
    risk_summary: str
    reasoning: dict
    metadata_json: dict
    created_at: datetime


class CrossDomainReasoningBuildRequest(BaseModel):
    actor: str = "workspace"
    source: str = "objective56"
    lookback_hours: int = Field(default=24, ge=1, le=720)
    max_items_per_domain: int = Field(default=50, ge=1, le=200)
    metadata_json: dict = Field(default_factory=dict)


class CrossDomainReasoningOut(BaseModel):
    context_id: int
    source: str
    actor: str
    lookback_hours: int
    workspace_state: dict
    communication_state: dict
    external_information: dict
    development_state: dict
    self_improvement_state: dict
    reasoning_summary: str
    reasoning: dict
    confidence: float
    status: str
    metadata_json: dict
    created_at: datetime


class StrategyGoalBuildRequest(BaseModel):
    actor: str = "workspace"
    source: str = "objective57"
    lookback_hours: int = Field(default=24, ge=1, le=720)
    max_items_per_domain: int = Field(default=50, ge=1, le=200)
    max_goals: int = Field(default=5, ge=1, le=20)
    min_context_confidence: float = Field(default=0.4, ge=0.0, le=1.0)
    min_domains_required: int = Field(default=2, ge=1, le=10)
    min_cross_domain_links: int = Field(default=1, ge=0, le=20)
    generate_horizon_plans: bool = True
    generate_improvement_proposals: bool = True
    generate_maintenance_cycles: bool = False
    metadata_json: dict = Field(default_factory=dict)


class StrategyGoalOut(BaseModel):
    strategy_goal_id: int
    source: str
    actor: str
    strategy_type: str
    origin_context_id: int | None
    priority: str
    priority_score: float
    success_criteria: str
    status: str
    evidence_summary: str
    supporting_evidence: dict
    contributing_domains: list[str]
    ranking_factors: dict
    reasoning_summary: str
    reasoning: dict
    linked_horizon_plan_ids: list[int]
    linked_improvement_proposal_ids: list[int]
    linked_maintenance_run_ids: list[int]
    operator_recommendations: list[str]
    persistence_state: str
    review_status: str
    persistence_confidence: float
    surviving_sessions: int
    carry_forward_count: int
    last_reviewed_at: datetime | None
    review_notes: str
    metadata_json: dict
    created_at: datetime


class CrossDomainTaskOrchestrationBuildRequest(BaseModel):
    actor: str = "workspace"
    source: str = "objective63"
    lookback_hours: int = Field(default=24, ge=1, le=720)
    max_items_per_domain: int = Field(default=50, ge=1, le=200)
    min_context_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    min_domains_required: int = Field(default=2, ge=1, le=10)
    dependency_resolution_policy: Literal["ask", "defer", "replan", "escalate"] = "ask"
    collaboration_mode_preference: Literal[
        "auto", "autonomous", "assistive", "confirmation-first", "deferential"
    ] = "auto"
    task_kind: Literal["mixed", "physical", "informational"] = "mixed"
    action_risk_level: Literal["low", "medium", "high"] = "medium"
    communication_urgency_override: float | None = Field(default=None, ge=0.0, le=1.0)
    use_human_aware_signals: bool = False
    generate_goal: bool = True
    generate_horizon_plan: bool = True
    generate_improvement_proposals: bool = False
    metadata_json: dict = Field(default_factory=dict)


class CrossDomainTaskOrchestrationOut(BaseModel):
    orchestration_id: int
    source: str
    actor: str
    status: str
    orchestration_type: str
    origin_context_id: int | None
    lookback_hours: int
    priority_score: float
    priority_label: str
    collaboration_mode: str
    human_context_modifiers: dict
    collaboration_reasoning: dict
    contributing_domains: list[str]
    dependency_resolution: dict
    orchestration_reason: str
    reasoning: dict
    linked_goal_ids: list[int]
    linked_horizon_plan_ids: list[int]
    linked_improvement_proposal_ids: list[int]
    linked_inquiry_question_ids: list[int]
    downstream_artifacts: list[dict]
    metadata_json: dict
    created_at: datetime


class CollaborationModePreferenceRequest(BaseModel):
    actor: str = "operator"
    mode: Literal["autonomous", "assistive", "confirmation-first", "deferential"]
    reason: str = ""
    metadata_json: dict = Field(default_factory=dict)


class CollaborationStateOut(BaseModel):
    policy_version: str
    collaboration_mode: str
    communication_urgency: float
    interruption_likelihood: float
    operator_presence_score: float
    human_aware_signals: dict
    active_modifiers: list[str]
    reasoning: dict


class CollaborationNegotiationOptionOut(BaseModel):
    option_id: Literal[
        "continue_now",
        "defer_action",
        "rescan_first",
        "speak_summary_only",
        "request_confirmation_later",
    ]
    label: str
    description: str
    effect: str
    safety_class: str


class CollaborationNegotiationOut(BaseModel):
    negotiation_id: int
    source: str
    actor: str
    status: str
    resolution_status: str
    origin_orchestration_id: int | None
    origin_context_id: int | None
    origin_goal_id: int | None
    origin_horizon_plan_id: int | None
    trigger_type: str
    trigger_reason: str
    requested_decision: str
    options_presented: list[CollaborationNegotiationOptionOut]
    default_safe_path: str
    selected_option_id: str
    selected_option_label: str
    human_context_state: dict
    explainability: dict
    applied_effect: dict
    resolved_by: str
    resolved_at: datetime | None
    metadata_json: dict
    created_at: datetime


class CollaborationNegotiationRespondRequest(BaseModel):
    actor: str = "operator"
    option_id: Literal[
        "continue_now",
        "defer_action",
        "rescan_first",
        "speak_summary_only",
        "request_confirmation_later",
    ]
    reason: str = ""
    metadata_json: dict = Field(default_factory=dict)


class CollaborationPatternOut(BaseModel):
    pattern_id: int
    source: str
    actor: str
    pattern_type: str
    context_signature: str
    evidence_count: int
    confidence: float
    raw_confidence: float
    dominant_outcome: str
    affected_domains: list[str]
    status: str
    evidence_summary: str
    freshness: str
    decay_factor: float
    age_days: float
    context_match_score: float
    explainability: dict
    influence_profile: dict
    acknowledged_by: str
    acknowledged_at: datetime | None
    last_observed_at: datetime | None
    metadata_json: dict
    created_at: datetime


class CollaborationPatternAcknowledgeRequest(BaseModel):
    actor: str = "operator"
    reason: str = ""
    metadata_json: dict = Field(default_factory=dict)


class CollaborationProfileOut(BaseModel):
    profile_id: int
    source: str
    actor: str
    profile_type: str
    context_scope: str
    dominant_collaboration_mode: str
    supporting_pattern_ids: list[int]
    evidence_count: int
    confidence: float
    raw_confidence: float
    freshness: str
    status: str
    evidence_summary: str
    decay_factor: float
    age_days: float
    context_match_score: float
    explainability: dict
    influence_profile: dict
    last_observed_at: datetime | None
    metadata_json: dict
    created_at: datetime


class CollaborationProfileRecomputeRequest(BaseModel):
    actor: str = "operator"
    context_scope: str = ""
    limit: int = Field(default=50, ge=1, le=500)
    metadata_json: dict = Field(default_factory=dict)


class StateBusEventCreateRequest(BaseModel):
    actor: str = "workspace"
    source: str = "objective71"
    event_domain: Literal[
        "tod.runtime", "mim.perception", "mim.strategy", "mim.improvement", "mim.assist"
    ] = "mim.strategy"
    event_type: str = "state.updated"
    stream_key: str = "global"
    occurred_at: str = ""
    payload_json: dict = Field(default_factory=dict)
    metadata_json: dict = Field(default_factory=dict)


class StateBusEventOut(BaseModel):
    event_id: int
    source: str
    actor: str
    event_domain: str
    event_type: str
    stream_key: str
    sequence_id: int
    occurred_at: datetime
    payload_json: dict
    metadata_json: dict
    created_at: datetime


class StateBusSnapshotUpsertRequest(BaseModel):
    actor: str = "workspace"
    source: str = "objective71"
    state_payload_json: dict = Field(default_factory=dict)
    last_event_id: int | None = Field(default=None, ge=1)
    metadata_json: dict = Field(default_factory=dict)


class StateBusSnapshotOut(BaseModel):
    snapshot_id: int
    source: str
    actor: str
    snapshot_scope: str
    state_version: int
    state_payload_json: dict
    last_event_id: int | None
    last_event_sequence: int
    last_event_domain: str
    last_event_type: str
    metadata_json: dict
    updated_at: datetime
    created_at: datetime


class StateBusConsumerRegisterRequest(BaseModel):
    actor: str = "workspace"
    source: str = "objective72"
    status: Literal["active", "paused", "disabled"] = "active"
    subscription_domains: list[
        Literal[
            "tod.runtime",
            "mim.perception",
            "mim.strategy",
            "mim.improvement",
            "mim.assist",
        ]
    ] = Field(default_factory=list)
    subscription_event_types: list[str] = Field(default_factory=list)
    subscription_sources: list[str] = Field(default_factory=list)
    subscription_stream_keys: list[str] = Field(default_factory=list)
    metadata_json: dict = Field(default_factory=dict)


class StateBusConsumerOut(BaseModel):
    consumer_id: int
    source: str
    actor: str
    consumer_key: str
    status: str
    subscription: dict
    cursor_event_id: int
    cursor_occurred_at: datetime | None
    processed_event_ids: list[int]
    poll_count: int
    ack_count: int
    lag_count: int
    replay_from_snapshot_scope: str
    last_polled_at: datetime | None
    last_acked_at: datetime | None
    last_replayed_at: datetime | None
    metadata_json: dict
    updated_at: datetime
    created_at: datetime


class StateBusConsumerPollRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=500)


class StateBusConsumerAckRequest(BaseModel):
    actor: str = "workspace"
    event_ids: list[int] = Field(default_factory=list)
    metadata_json: dict = Field(default_factory=dict)


class StateBusConsumerReplayRequest(BaseModel):
    actor: str = "workspace"
    from_event_id: int | None = Field(default=None, ge=0)
    from_snapshot_scope: str = ""
    metadata_json: dict = Field(default_factory=dict)


class StateBusMimCoreStepRequest(BaseModel):
    actor: str = "mim-core"
    limit: int = Field(default=50, ge=1, le=200)
    metadata_json: dict = Field(default_factory=dict)


class StateBusReactionStepRequest(BaseModel):
    actor: str = "mim-reactor"
    limit: int = Field(default=50, ge=1, le=200)
    metadata_json: dict = Field(default_factory=dict)


class InterfaceSessionUpsertRequest(BaseModel):
    actor: str = "operator"
    source: str = "objective74"
    channel: Literal["text", "voice", "camera", "api"] = "text"
    status: Literal["active", "paused", "closed"] = "active"
    context_json: dict = Field(default_factory=dict)
    metadata_json: dict = Field(default_factory=dict)


class InterfaceSessionOut(BaseModel):
    session_id: int
    source: str
    actor: str
    session_key: str
    channel: str
    status: str
    last_input_at: datetime | None
    last_output_at: datetime | None
    context_json: dict
    metadata_json: dict
    updated_at: datetime
    created_at: datetime


class InterfaceMessageCreateRequest(BaseModel):
    actor: str = "operator"
    source: str = "objective74"
    direction: Literal["inbound", "outbound", "system"] = "inbound"
    role: Literal["operator", "mim", "tod", "system"] = "operator"
    content: str = ""
    parsed_intent: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    requires_approval: bool = False
    metadata_json: dict = Field(default_factory=dict)


class InterfaceMessageOut(BaseModel):
    message_id: int
    session_id: int
    source: str
    actor: str
    direction: str
    role: str
    content: str
    parsed_intent: str
    confidence: float
    requires_approval: bool
    delivery_status: str
    metadata_json: dict
    created_at: datetime


class InterfaceApprovalRequest(BaseModel):
    actor: str = "operator"
    source: str = "objective74"
    message_id: int | None = Field(default=None, ge=1)
    decision: Literal["approved", "rejected", "deferred"] = "approved"
    reason: str = ""
    metadata_json: dict = Field(default_factory=dict)


class InterfaceApprovalOut(BaseModel):
    approval_id: int
    session_id: int
    message_id: int | None
    source: str
    actor: str
    decision: str
    reason: str
    metadata_json: dict
    created_at: datetime


class StrategyGoalPersistenceRecomputeRequest(BaseModel):
    actor: str = "workspace"
    source: str = "objective59"
    lookback_hours: int = Field(default=168, ge=1, le=2160)
    min_support_count: int = Field(default=2, ge=1, le=100)
    min_persistence_confidence: float = Field(default=0.55, ge=0.0, le=1.0)
    limit: int = Field(default=500, ge=1, le=2000)
    metadata_json: dict = Field(default_factory=dict)


class StrategyGoalReviewRequest(BaseModel):
    actor: str = "operator"
    decision: Literal["carry_forward", "activate", "defer", "archive"] = "carry_forward"
    reason: str = ""
    evidence_json: dict = Field(default_factory=dict)
    metadata_json: dict = Field(default_factory=dict)


class StrategyGoalReviewOut(BaseModel):
    review_id: int
    strategy_goal_id: int
    actor: str
    decision: str
    reason: str
    resulting_persistence_state: str
    resulting_review_status: str
    evidence_json: dict
    metadata_json: dict
    created_at: datetime


class AdaptiveAutonomyBoundaryEvaluateRequest(BaseModel):
    actor: str = "workspace"
    source: str = "objective58"
    scope: str = "global"
    lookback_hours: int = Field(default=24, ge=1, le=720)
    min_samples: int = Field(default=3, ge=1, le=500)
    apply_recommended_boundaries: bool = False
    hard_ceiling_overrides: dict = Field(default_factory=dict)
    evidence_inputs_override: dict = Field(default_factory=dict)
    metadata_json: dict = Field(default_factory=dict)


class AdaptiveAutonomyBoundaryProfileOut(BaseModel):
    boundary_id: int
    profile_id: int
    scope: str
    source: str
    actor: str
    profile_status: str
    current_level: str
    confidence: float
    evidence_inputs: dict
    last_adjusted: datetime | None
    adjustment_reason: str
    lookback_hours: int
    sample_count: int
    success_rate: float
    escalation_rate: float
    retry_rate: float
    interruption_rate: float
    memory_delta_rate: float
    current_boundaries: dict
    recommended_boundaries: dict
    applied_boundaries: dict
    adaptation_summary: str
    adaptation_reasoning: dict
    metadata_json: dict
    created_at: datetime


class ExecutionTruthGovernanceEvaluateRequest(BaseModel):
    actor: str = "workspace"
    source: str = "objective81"
    managed_scope: str = "global"
    lookback_hours: int = Field(default=24, ge=1, le=720)
    metadata_json: dict = Field(default_factory=dict)


class ExecutionTruthGovernanceOut(BaseModel):
    governance_id: int
    source: str
    actor: str
    managed_scope: str
    status: str
    lookback_hours: int
    execution_count: int
    signal_count: int
    confidence: float
    governance_state: str
    governance_decision: str
    governance_reason: str
    trigger_counts: dict
    trigger_evidence: dict
    downstream_actions: dict
    reasoning: dict
    execution_truth_summary: dict
    metadata_json: dict
    created_at: datetime


class ImprovementRecommendationOut(BaseModel):
    recommendation_id: int
    source: str
    actor: str
    proposal_id: int
    experiment_id: int
    recommendation_type: str
    recommendation_summary: str
    baseline_metrics: dict
    experimental_metrics: dict
    comparison: dict
    status: str
    review_reason: str
    reviewed_by: str
    reviewed_at: datetime | None
    metadata_json: dict
    created_at: datetime
    latest_artifact: "ImprovementArtifactOut | None" = None


class ImprovementArtifactOut(BaseModel):
    artifact_id: int
    proposal_id: int
    artifact_type: str
    status: str
    candidate_payload: dict
    metadata_json: dict
    created_at: datetime


class ImprovementProposalOut(BaseModel):
    proposal_id: int
    source: str
    actor: str
    proposal_type: str
    trigger_pattern: str
    evidence_summary: str
    evidence: dict
    affected_component: str
    suggested_change: str
    confidence: float
    safety_class: str
    risk_summary: str
    test_recommendation: str
    status: str
    review_reason: str
    metadata_json: dict
    created_at: datetime
    latest_artifact: ImprovementArtifactOut | None = None


class MaintenanceCycleRequest(BaseModel):
    actor: str = "workspace"
    source: str = "objective50"
    stale_after_seconds: int = Field(default=900, ge=1, le=86400)
    max_strategies: int = Field(default=5, ge=1, le=50)
    max_actions: int = Field(default=5, ge=1, le=50)
    auto_execute: bool = True
    metadata_json: dict = Field(default_factory=dict)


class StewardshipCycleRequest(BaseModel):
    actor: str = "workspace"
    source: str = "objective60"
    managed_scope: str = "global"
    stale_after_seconds: int = Field(default=900, ge=60, le=172800)
    lookback_hours: int = Field(default=168, ge=1, le=2160)
    max_strategies: int = Field(default=5, ge=1, le=50)
    max_actions: int = Field(default=5, ge=1, le=50)
    auto_execute: bool = True
    force_degraded: bool = False
    target_environment_state: dict = Field(default_factory=dict)
    metadata_json: dict = Field(default_factory=dict)


class DesiredEnvironmentStateOut(BaseModel):
    desired_state_id: int | None = None
    scope: str = "workspace"
    scope_ref: str = "global"
    target_conditions: dict = Field(default_factory=dict)
    priority: float = 0.0
    strategy_link: dict = Field(default_factory=dict)
    created_from: str = ""


class StewardshipOut(BaseModel):
    stewardship_id: int
    source: str
    actor: str
    status: str
    linked_desired_state_id: int | None
    desired_state: DesiredEnvironmentStateOut
    target_environment_state: dict
    managed_scope: str
    maintenance_priority: str
    current_health: float
    last_cycle: datetime | None
    next_cycle: datetime | None
    cycle_count: int
    linked_strategy_goal_ids: list[int]
    linked_maintenance_run_ids: list[int]
    linked_strategy_types: list[str]
    linked_autonomy_boundary_id: int | None
    last_decision_summary: str
    current_metrics: dict
    metadata_json: dict
    created_at: datetime


class StewardshipCycleOut(BaseModel):
    cycle_id: int
    stewardship_id: int
    source: str
    actor: str
    pre_health: float
    post_health: float
    improvement_delta: float
    degraded_signals: list[dict]
    selected_actions: list[dict]
    decision: dict
    integration_evidence: dict
    assessment: dict
    verification: dict
    maintenance_run_id: int | None
    improved: bool
    metadata_json: dict
    created_at: datetime


class InquiryQuestionGenerateRequest(BaseModel):
    actor: str = "workspace"
    source: str = "objective62"
    lookback_hours: int = Field(default=24, ge=1, le=720)
    max_questions: int = Field(default=10, ge=1, le=100)
    min_soft_friction_count: int = Field(default=3, ge=2, le=50)
    metadata_json: dict = Field(default_factory=dict)


class InquiryQuestionAnswerRequest(BaseModel):
    actor: str = "operator"
    selected_path_id: str = Field(min_length=1)
    answer_json: dict = Field(default_factory=dict)
    metadata_json: dict = Field(default_factory=dict)


class InquiryQuestionOut(BaseModel):
    question_id: int
    source: str
    actor: str
    status: str
    trigger_type: str
    uncertainty_type: str
    originating_goal_id: int | None
    originating_strategy_id: int | None
    originating_plan_id: int | None
    why_answer_matters: str
    waiting_decision: str
    no_answer_behavior: str
    candidate_answer_paths: list[dict]
    urgency: str
    priority: str
    safe_default_if_unanswered: str
    trigger_evidence: dict
    selected_path_id: str
    answer_json: dict
    applied_effect_json: dict
    answered_by: str
    answered_at: datetime | None
    metadata_json: dict
    decision_state: str = ""
    decision_reason: str = ""
    policy_evidence_score: float = 0.0
    cooldown_active: bool = False
    cooldown_remaining_seconds: int = 0
    duplicate_suppressed: bool = False
    recent_answer_reused: bool = False
    allowed_answer_effects: list[str] = Field(default_factory=list)
    created_at: datetime


class PolicyExperimentRunRequest(BaseModel):
    actor: str = "workspace"
    source: str = "objective51"
    proposal_id: int | None = Field(default=None, ge=1)
    experiment_type: str = "policy_adjustment_sandbox"
    lookback_hours: int = Field(default=24, ge=1, le=720)
    sandbox_mode: str = "shadow_evaluation"
    metadata_json: dict = Field(default_factory=dict)


class PolicyExperimentOut(BaseModel):
    experiment_id: int
    source: str
    actor: str
    proposal_id: int | None
    experiment_type: str
    sandbox_mode: str
    status: str
    baseline_metrics: dict
    experimental_metrics: dict
    comparison: dict
    recommendation: str
    recommendation_reason: str
    metadata_json: dict
    created_at: datetime


WorkspaceInterruptionType = Literal[
    "human_detected_in_workspace",
    "operator_pause",
    "operator_stop",
    "new_obstacle_detected",
    "target_confidence_drop",
    "workspace_state_changed",
    "safety_policy_interrupt",
]


class WorkspaceExecutionPauseRequest(BaseModel):
    actor: str = "operator"
    source: str = "operator"
    interruption_type: WorkspaceInterruptionType = "operator_pause"
    reason: str = ""
    metadata_json: dict = Field(default_factory=dict)


class WorkspaceExecutionStopRequest(BaseModel):
    actor: str = "operator"
    source: str = "operator"
    interruption_type: WorkspaceInterruptionType = "operator_stop"
    reason: str = ""
    metadata_json: dict = Field(default_factory=dict)


class WorkspaceExecutionResumeRequest(BaseModel):
    actor: str = "operator"
    source: str = "operator"
    reason: str = ""
    safety_ack: bool = False
    conditions_restored: bool = False
    metadata_json: dict = Field(default_factory=dict)


WorkspacePredictiveSignalType = Literal[
    "object_moved",
    "object_missing",
    "confidence_drop",
    "zone_state_changed",
    "new_obstacle_detected",
    "target_no_longer_valid",
]


WorkspacePredictiveOutcome = Literal[
    "continue_monitor",
    "pause_and_resimulate",
    "require_replan",
    "abort_chain",
]


class WorkspaceExecutionPredictChangeRequest(BaseModel):
    actor: str = "workspace"
    source: str = "predictive_monitor"
    signal_type: WorkspacePredictiveSignalType = "zone_state_changed"
    predicted_outcome: WorkspacePredictiveOutcome = "continue_monitor"
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    reason: str = ""
    metadata_json: dict = Field(default_factory=dict)


class WorkspaceActionPlanReplanRequest(BaseModel):
    actor: str = "operator"
    reason: str = ""
    signal_id: int | None = Field(default=None, ge=1)
    force: bool = False
    motion_plan_overrides: dict = Field(default_factory=dict)
    metadata_json: dict = Field(default_factory=dict)


class AutomationWebSessionCreateRequest(BaseModel):
    carrier_id: str = ""
    session_key: str = ""
    start_url: str = ""
    simulation_mode: bool | None = None
    headless: bool | None = None
    metadata_json: dict = Field(default_factory=dict)


class AutomationWebNavigateRequest(BaseModel):
    url: str = Field(min_length=1)
    wait_for: str = "domcontentloaded"
    timeout_seconds: int = Field(default=20, ge=1, le=180)


class AutomationWebActionRequest(BaseModel):
    action: Literal["click", "type", "wait_for", "press", "select", "detect"]
    selector: str = ""
    text: str = ""
    key: str = ""
    value: str = ""
    timeout_seconds: int = Field(default=20, ge=1, le=180)
    metadata_json: dict = Field(default_factory=dict)


class AutomationAuthResolveRequest(BaseModel):
    session_id: int | None = None
    carrier_id: str = ""
    username: str = ""
    password: str = ""
    mfa_code: str = ""
    pause_if_mfa_detected: bool = True
    metadata_json: dict = Field(default_factory=dict)


class AutomationAuthChallengeActionRequest(BaseModel):
    actor: str = "operator"
    mfa_code: str = ""
    reason: str = ""
    metadata_json: dict = Field(default_factory=dict)


class AutomationNavigationExecuteRequest(BaseModel):
    session_id: int
    carrier_id: str = ""
    steps: list[dict] = Field(default_factory=list)
    stop_on_failure: bool = True
    start_step_index: int = Field(default=0, ge=0)
    metadata_json: dict = Field(default_factory=dict)


class AutomationFileDetectRequest(BaseModel):
    session_id: int | None = None
    run_id: int | None = None
    carrier_id: str = ""
    selector: str = ""
    expected_name_pattern: str = ""
    source_url: str = ""
    metadata_json: dict = Field(default_factory=dict)


class AutomationFileDownloadRequest(BaseModel):
    session_id: int | None = None
    run_id: int | None = None
    artifact_id: int | None = None
    carrier_id: str = ""
    url: str = ""
    file_name: str = ""
    content_base64: str = ""
    metadata_json: dict = Field(default_factory=dict)


class AutomationPlaybookUpsertRequest(BaseModel):
    carrier_id: str = Field(min_length=1)
    enabled: bool = True
    login_method: str = "username_password"
    navigation_steps: list[dict] = Field(default_factory=list)
    report_location_logic: dict = Field(default_factory=dict)
    parsing_rules: dict = Field(default_factory=dict)
    recovery_rules: dict = Field(default_factory=dict)
    metadata_json: dict = Field(default_factory=dict)


class AutomationPlaybookRefineRequest(BaseModel):
    actor: str = "operator"
    patch: dict = Field(default_factory=dict)
    reason: str = ""


class AutomationRecoveryEvaluateRequest(BaseModel):
    carrier_id: str = ""
    run_id: int | None = None
    failure_type: str = ""
    detail: str = ""
    retries_attempted: int = Field(default=0, ge=0, le=20)
    metadata_json: dict = Field(default_factory=dict)


class AutomationRecoveryRetryRequest(BaseModel):
    run_id: int | None = None
    carrier_id: str = ""
    strategy: str = "auto"
    reason: str = ""
    metadata_json: dict = Field(default_factory=dict)


class AutomationEmailPollRequest(BaseModel):
    source: Literal["imap", "simulation"] = "imap"
    mailbox: str = "INBOX"
    limit: int = Field(default=20, ge=1, le=200)
    subject_contains: str = ""
    sender_contains: str = ""
    simulation_messages: list[dict] = Field(default_factory=list)
    metadata_json: dict = Field(default_factory=dict)


class AutomationEmailExtractMfaRequest(BaseModel):
    carrier_id: str = ""
    challenge_key: str = ""
    lookback_minutes: int = Field(default=15, ge=1, le=1440)
    sender_contains: str = ""
    subject_contains: str = ""


class AutomationCalendarGoogleAuthUrlRequest(BaseModel):
    state: str = ""
    scopes: list[str] = Field(
        default_factory=lambda: ["https://www.googleapis.com/auth/calendar.events"]
    )
    prompt: str = "consent"
    access_type: Literal["online", "offline"] = "offline"
    include_granted_scopes: bool = True


class AutomationCalendarGoogleExchangeCodeRequest(BaseModel):
    code: str = Field(min_length=1)
    redirect_uri: str = ""
    code_verifier: str = ""


class AutomationCalendarReminderCreateRequest(BaseModel):
    source: Literal["google", "simulation"] = "simulation"
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    start_at: datetime
    end_at: datetime | None = None
    timezone: str = ""
    calendar_id: str = "primary"
    reminder_minutes: list[int] = Field(default_factory=lambda: [30])
    attendees: list[str] = Field(default_factory=list)
    access_token: str = ""
    refresh_token: str = ""
    metadata_json: dict = Field(default_factory=dict)


class AutomationRunCreateRequest(BaseModel):
    run_key: str = ""
    triggered_by: str = "operator"
    carriers: list[str] = Field(default_factory=list)
    metadata_json: dict = Field(default_factory=dict)


class AutomationRunCarrierStatusUpdateRequest(BaseModel):
    carrier_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    retries: int = Field(default=0, ge=0, le=50)
    requires_human_action: bool = False
    last_error: str = ""
    last_step_index: int = -1
    metadata_json: dict = Field(default_factory=dict)


class AutomationReconciliationEvaluateRequest(BaseModel):
    carrier_id: str = ""
    current_totals: dict = Field(default_factory=dict)
    previous_totals: dict = Field(default_factory=dict)
    expected_carriers: list[str] = Field(default_factory=list)
    present_carriers: list[str] = Field(default_factory=list)
    anomaly_threshold_pct: float = Field(default=25.0, ge=0.0, le=500.0)
    metadata_json: dict = Field(default_factory=dict)
