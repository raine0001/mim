# MIM Self-Awareness and Self-Optimization

## Overview

MIM now has built-in **self-awareness** and **self-optimization** capabilities. This enables MIM to:

1. **Monitor its own operational state** - Track memory, CPU, latency, error rates, and resource pools
2. **Perform self-diagnostics** - Identify performance degradation and resource bottlenecks
3. **Propose self-improvements** - Generate optimization recommendations based on detected issues
4. **Execute bounded self-optimization** - Apply approved optimizations with governance and auditability
5. **Reverse ineffective changes** - Rollback optimizations if they prove counterproductive

This creates a feedback loop where MIM continuously becomes more aware of and responsive to its own operational needs.

## Architecture

### Self-Health Monitor (`core/self_health_monitor.py`)

Continuously tracks MIM's operational metrics and health trends:

- **Metric Collection**: Records samples of memory, CPU, API latency, error rates, database connections, state bus lag, cache hit rates, worker queue depth
- **Trend Analysis**: Analyzes windows of historical data to detect patterns (stable, increasing, decreasing)
- **Degradation Detection**: Identifies when metrics cross health thresholds (e.g., memory >80%, latency >200ms)
- **Recommendation Generation**: Generates optimization recommendations when degradation is detected

**Key Concepts:**
- Health metrics tracked in sliding windows (default: 300 seconds)
- Trends calculated from sample history with statistical analysis
- Degradation flagged when metrics exceed health thresholds
- Auto-generates recommendations during health analysis

### Self-Optimizer Service (`core/self_optimizer_service.py`)

Manages optimization proposals with full governance and auditability:

- **Proposal Lifecycle**: Created → Proposed → [Approved/Rejected] → [Executing] → [Completed/Failed/RolledBack]
- **Approval Gating**: High-impact optimizations require operator approval before execution
- **Audit Trail**: Tracks all state transitions, decisions, and results
- **Reversibility**: Each optimization tracks its rollback action for reversal if needed
- **Persistence**: All proposals and state changes persisted to disk for recovery

**Key Concepts:**
- Proposals bridge health diagnostics to action
- Approval gates prevent harmful unilateral changes
- Audit trail enables accountability and debugging
- Rollback functionality mitigates risk of ineffective optimizations

### Self-Awareness API Router (`core/routers/self_awareness_router.py`)

Exposes MIM's self-awareness via REST endpoints for introspection and governance:

**Diagnostics Endpoints:**
- `GET /mim/self/health` - Current health status summary
- `GET /mim/self/health/detailed` - Full health report with all trends
- `GET /mim/self/recommendations` - Current optimization recommendations
- `POST /mim/self/health/record-metric` - Record a health metric sample

**Optimization Governance Endpoints:**
- `POST /mim/self/optimize/propose` - Create an optimization proposal
- `GET /mim/self/optimize/proposals` - List all proposals (filterable by status/severity)
- `GET /mim/self/optimize/proposals/{id}` - Get proposal details
- `POST /mim/self/optimize/proposals/{id}/approve` - Approve for execution
- `POST /mim/self/optimize/proposals/{id}/reject` - Reject proposal
- `POST /mim/self/optimize/proposals/{id}/execute` - Execute approved optimization
- `POST /mim/self/optimize/proposals/{id}/rollback` - Reverse a completed optimization

## Health Status Model

### Status Levels

MIM's overall health is categorized as:

- **`healthy`** - All metrics normal, no concerns detected
- **`suboptimal`** - Optimization opportunities identified, but no urgent issues
- **`degraded`** - One or more metrics showing degradation, some high-severity issues
- **`critical`** - Multiple high-severity issues or widespread degradation

### Metrics Monitored

| Metric | Threshold (Alert) | Unit | Typical Range |
|--------|-------------------|------|----------------|
| Memory Usage | >80% | percent | 10-60% |
| CPU Usage | >90% | percent | 5-50% |
| API Latency | >200ms | milliseconds | 10-150ms |
| API Error Rate | >5% | percent | 0-2% |
| Cache Hit Rate | <50% | percent | 60-95% |
| State Bus Lag | >5000ms | milliseconds | 0-1000ms |
| DB Connections Used | >80% of pool | percent | 20-60% |
| Worker Queue Depth | >100 | count | 0-50 |

## Optimization Categories

### Memory (`memory`)
- **Trigger**: Memory >80% and trending upward
- **Action**: Trigger garbage collection, flush non-essential caches
- **Benefit**: Recover 10-20% memory footprint
- **Approval**: Not required (safe operation)

### Latency (`latency`)
- **Trigger**: API latency >200ms and trending upward
- **Action**: Increase worker pool size
- **Benefit**: Reduce latency by 20-30%
- **Approval**: **Required** (affects resource consumption)

### Cache (`cache`)
- **Trigger**: Cache hit rate <50%
- **Action**: Expand cache size
- **Benefit**: Improve hit rate to 65-75%, reduce DB load ~20%
- **Approval**: **Required** (affects memory)

### Resource (`resource`)
- **Trigger**: State bus lag >5000ms
- **Action**: Reduce batch timeout, process events more frequently
- **Benefit**: Reduce lag to <1000ms
- **Approval**: Not required (configuration tuning)

### CPU (`cpu`)
- **Trigger**: CPU consistently >80%
- **Action**: Load shedding, defer non-critical operations
- **Benefit**: Reduce CPU to <70%
- **Approval**: **Required** (affects functionality)

## Integration Patterns

### Continuous Monitoring Loop

```python
# Run periodically (e.g., every 5 seconds) in orchestration service
async def mim_self_awareness_pulse():
    """Record current metrics and analyze health."""
    import psutil
    import os
    
    # Gather current metrics
    process = psutil.Process(os.getpid())
    memory_info = process.memory_info()
    
    metric_data = RecordMetricRequest(
        memory_mb=memory_info.rss // (1024 * 1024),
        memory_percent=process.memory_percent(),
        cpu_percent=process.cpu_percent(interval=0.1),
        api_latency_ms=await get_recent_api_latency(),
        api_error_rate=await get_recent_error_rate(),
        cache_hit_rate=await get_cache_hit_rate(),
        state_bus_lag_ms=await get_state_bus_lag(),
        worker_queue_depth=get_worker_queue_size(),
    )
    
    # Record metric
    await httpx.AsyncClient().post(
        "http://127.0.0.1:18001/mim/self/health/record-metric",
        json=metric_data.dict()
    )
    
    # Analyze and generate recommendations
    health = await httpx.AsyncClient().get(
        "http://127.0.0.1:18001/mim/self/health"
    )
    
    if health.status_code.status == "critical":
        # Alert operator
        logger.critical(f"MIM health critical: {health}")
    
    return health
```

### Autonomous Optimization Execution

```python
# MIM autonomously proposes and may execute low-risk optimizations
async def mim_consider_self_optimizations():
    """Propose optimizations from health diagnostics."""
    
    # Get recommendations
    recommendations = await httpx.AsyncClient().get(
        "http://127.0.0.1:18001/mim/self/recommendations"
    )
    
    for rec in recommendations.json():
        # Create proposal
        proposal = await httpx.AsyncClient().post(
            "http://127.0.0.1:18001/mim/self/optimize/propose",
            json={
                "recommendation_id": rec["recommendation_id"],
                "title": rec["title"],
                "description": rec["description"],
                "proposed_action": rec["proposed_action"],
                "severity": rec["severity"],
                "requires_approval": rec["requires_approval"],
            }
        )
        
        if not rec.get("requires_approval"):
            # Auto-execute low-risk optimizations
            proposal_id = proposal.json()["proposal_id"]
            await httpx.AsyncClient().post(
                f"http://127.0.0.1:18001/mim/self/optimize/proposals/{proposal_id}/execute"
            )
        else:
            # Await operator approval for high-impact changes
            logger.info(f"Optimization proposal awaiting approval: {proposal}")
```

### Operator Dashboard Integration

Dashboard queries MIM's self-awareness endpoints to display:
- Current health status and trend
- Recent metrics and degradation warnings
- Pending optimization proposals requiring approval
- Completed/failed optimization history with rollback capability

## Governance Model

### Approval Gating

Optimizations are categorized by risk:

- **No approval required** (auto-executable):
  - Garbage collection
  - Cache flushing
  - State bus timing adjustments
  - Status reporting

- **Approval required** (operator gate):
  - Worker pool scaling (affects resource usage)
  - Cache size expansion (affects memory)
  - Load shedding (affects functionality)
  - Feature toggles (affects behavior)

### Audit and Accountability

Every optimization creates an immutable audit trail:

```json
{
  "proposal_id": "opt-opt-mem-gc-1711723450000",
  "status": "completed",
  "audit_trail": [
    {
      "timestamp": "2024-03-29T10:30:50Z",
      "event": "created",
      "data": {}
    },
    {
      "timestamp": "2024-03-29T10:30:52Z",
      "event": "execution_started",
      "data": {}
    },
    {
      "timestamp": "2024-03-29T10:30:55Z",
      "event": "execution_completed",
      "data": {
        "objects_collected": 1247,
        "timestamp": "2024-03-29T10:30:55Z"
      }
    }
  ]
}
```

## Testing Self-Awareness

### Manual Health Checks

```bash
# Get current health status
curl http://127.0.0.1:18001/mim/self/health -s | jq .

# Get detailed health report
curl http://127.0.0.1:18001/mim/self/health/detailed -s | jq .

# Get optimization recommendations
curl http://127.0.0.1:18001/mim/self/recommendations -s | jq .

# List all optimization proposals
curl http://127.0.0.1:18001/mim/self/optimize/proposals -s | jq .
```

### Recording Metrics Programmatically

```python
import httpx

async def test_self_awareness():
    client = httpx.AsyncClient()
    
    # Record a metric sample
    response = await client.post(
        "http://127.0.0.1:18001/mim/self/health/record-metric",
        json={
            "memory_percent": 45.2,
            "cpu_percent": 28.5,
            "api_latency_ms": 125.3,
            "api_error_rate": 0.01,
            "cache_hit_rate": 0.72,
        }
    )
    print("Metric recorded:", response.json())
    
    # Get health summary
    health = await client.get("http://127.0.0.1:18001/mim/self/health")
    print("Health status:", health.json())
```

## Future Enhancements

1. **Predictive Scaling**: Use historical trends to proactively scale resources before degradation occurs
2. **A/B Testing Optimizations**: Test optimization effectiveness with metrics before/after
3. **Constraint-Based Optimization**: Factor in operational constraints (budget, SLA) when proposing changes
4. **Machine Learning**: Train models on optimization history to predict effectiveness
5. **Federated Self-Awareness**: MIM instances sharing health/optimization insights in distributed deployments
6. **Integration with Decision Records**: All optimizations recorded as decision records for history and reasoning
7. **Self-Correction Learning**: Learn from rollbacks to improve future optimization proposals

## Key Principles

1. **Transparency** - All self-awareness and optimizations are observable and auditable
2. **Safety** - High-impact changes are operator-gated; all changes are reversible
3. **Autonomy** - MIM continuously improves within approved boundaries
4. **Accountability** - Complete audit trail of all diagnostic and optimization decisions
5. **Efficiency** - Low-overhead monitoring that doesn't consume excessive resources
6. **Adaptability** - Health thresholds and optimization strategies can be tuned based on operational needs
