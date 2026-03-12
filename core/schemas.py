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
    status: str
    reason: str
    feedback_json: dict
    created_at: datetime


class ExecutionFeedbackUpdateRequest(BaseModel):
    status: str = ""
    reason: str = ""
    runtime_outcome: str = ""
    recovery_state: str = ""
    correlation_json: dict = Field(default_factory=dict)
    feedback_json: dict = Field(default_factory=dict)
    actor: str = "executor"


class ExecutionFeedbackOut(BaseModel):
    execution_id: int
    status: str
    reason: str
    feedback_json: dict


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


class WorkspaceProposalActionRequest(BaseModel):
    actor: str = "operator"
    reason: str = ""
    metadata_json: dict = Field(default_factory=dict)


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
    auto_preferred_confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
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
    observed_conditions: list[EnvironmentStrategyCondition] = Field(default_factory=list)
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
