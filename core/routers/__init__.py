from fastapi import APIRouter

from core.routers import (
    automation,
    autonomy_boundaries,
    constraint_learning,
    constraints,
    custody,
    decision_records,
    environment_strategy,
    execution_control,
    execution_truth_governance,
    gateway,
    health,
    horizon_planning,
    improvement,
    inquiry,
    interface,
    journal,
    maintenance,
    manifest,
    memory,
    mim_arm,
    mim_ui,
    objectives,
    operator,
    orchestration,
    policy_experiments,
    preferences,
    public_chat,
    reasoning,
    results,
    reviews,
    routing,
    safety_router,
    shell,
    self_awareness_router,
    tod_ui,
    services,
    state_bus,
    status,
    stewardship,
    strategy,
    tasks,
    tools,
    workspace,
)

api_router = APIRouter()
api_router.include_router(health.router, prefix="")
api_router.include_router(status.router, prefix="")
api_router.include_router(manifest.router, prefix="")
api_router.include_router(automation.router, prefix="/automation", tags=["automation"])
api_router.include_router(gateway.router, prefix="/gateway", tags=["gateway"])
api_router.include_router(operator.router, prefix="/operator", tags=["operator"])
api_router.include_router(objectives.router, prefix="/objectives", tags=["objectives"])
api_router.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
api_router.include_router(custody.router, prefix="", tags=["custody"])
api_router.include_router(results.router, prefix="/results", tags=["results"])
api_router.include_router(reviews.router, prefix="/reviews", tags=["reviews"])
api_router.include_router(routing.router, prefix="/routing", tags=["routing"])
api_router.include_router(journal.router, prefix="/journal", tags=["journal"])
api_router.include_router(memory.router, prefix="/memory", tags=["memory"])
api_router.include_router(reasoning.router, prefix="", tags=["reasoning"])
api_router.include_router(orchestration.router, prefix="", tags=["orchestration"])
api_router.include_router(state_bus.router, prefix="", tags=["state-bus"])
api_router.include_router(interface.router, prefix="", tags=["interface"])
api_router.include_router(mim_arm.router, prefix="", tags=["mim-arm"])
api_router.include_router(public_chat.router, prefix="", tags=["public-chat"])
api_router.include_router(mim_ui.router, prefix="", tags=["mim-ui"])
api_router.include_router(tod_ui.router, prefix="", tags=["tod-ui"])
api_router.include_router(shell.router, prefix="", tags=["shell"])
api_router.include_router(constraints.router, prefix="", tags=["constraints"])
api_router.include_router(
    constraint_learning.router, prefix="", tags=["constraints-learning"]
)
api_router.include_router(horizon_planning.router, prefix="", tags=["planning-horizon"])
api_router.include_router(
    environment_strategy.router, prefix="", tags=["planning-strategy"]
)
api_router.include_router(
    decision_records.router, prefix="", tags=["planning-decisions"]
)
api_router.include_router(improvement.router, prefix="", tags=["improvement"])
api_router.include_router(execution_control.router, prefix="", tags=["execution-control"])
api_router.include_router(
    execution_truth_governance.router, prefix="", tags=["execution-truth-governance"]
)
api_router.include_router(inquiry.router, prefix="", tags=["inquiry"])
api_router.include_router(policy_experiments.router, prefix="", tags=["improvement"])
api_router.include_router(maintenance.router, prefix="", tags=["maintenance"])
api_router.include_router(strategy.router, prefix="", tags=["strategy"])
api_router.include_router(preferences.router, prefix="", tags=["preferences"])
api_router.include_router(
    autonomy_boundaries.router, prefix="", tags=["autonomy-boundaries"]
)
api_router.include_router(stewardship.router, prefix="", tags=["stewardship"])
api_router.include_router(workspace.router, prefix="/workspace", tags=["workspace"])
api_router.include_router(tools.router, prefix="/tools", tags=["tools"])
api_router.include_router(services.router, prefix="/services", tags=["services"])
api_router.include_router(self_awareness_router.router, prefix="", tags=["self-awareness"])
api_router.include_router(safety_router.router, prefix="", tags=["safety"])
