# MIM Self-Awareness Quick Start Guide

## What's New

MIM can now **monitor its own health**, **identify optimization opportunities**, and **execute self-improvements** with governance and auditability.

## Getting Started

### 1. Start the MIM Server

```bash
cd /home/testpilot/mim
.venv/bin/python -m uvicorn core.app:app --host 127.0.0.1 --port 18001
```

### 2. Check MIM's Health Status

```bash
# Simple health summary
curl -s http://127.0.0.1:18001/mim/self/health | jq .

# Detailed diagnostics
curl -s http://127.0.0.1:18001/mim/self/health/detailed | jq .

# Get optimization recommendations
curl -s http://127.0.0.1:18001/mim/self/recommendations | jq .
```

### 3. Record a Health Metric

```bash
curl -X POST http://127.0.0.1:18001/mim/self/health/record-metric \
  -H "Content-Type: application/json" \
  -d '{
    "memory_percent": 45.2,
    "cpu_percent": 28.5,
    "api_latency_ms": 125.3,
    "api_error_rate": 0.005,
    "cache_hit_rate": 0.72
  }' | jq .
```

## Key Concepts

### Health Status Levels

- **`healthy`** - All systems normal
- **`suboptimal`** - Optimization opportunities (non-urgent)  
- **`degraded`** - Performance issues detected
- **`critical`** - Multiple urgent issues

### Optimization Workflow

```
Recommendation → Proposal → Approval (if needed) → Execution → Audit Trail
```

Both low-risk (auto-execute) and high-risk (requires approval) optimizations are supported.

## API Endpoints Summary

### Diagnostics

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/mim/self/health` | GET | Current health status |
| `/mim/self/health/detailed` | GET | Full diagnostics with trends |
| `/mim/self/recommendations` | GET | Suggested optimizations |
| `/mim/self/health/record-metric` | POST | Record a metric sample |

### Optimization Governance

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/mim/self/optimize/propose` | POST | Create optimization proposal |
| `/mim/self/optimize/proposals` | GET | List all proposals |
| `/mim/self/optimize/proposals/{id}` | GET | Get proposal details |
| `/mim/self/optimize/proposals/{id}/approve` | POST | Operator approval |
| `/mim/self/optimize/proposals/{id}/reject` | POST | Operator rejection |
| `/mim/self/optimize/proposals/{id}/execute` | POST | Execute optimization |
| `/mim/self/optimize/proposals/{id}/rollback` | POST | Reverse optimization |

## Python Integration Examples

### Record Metrics

```python
import httpx
import asyncio

async def record_metric():
    async with httpx.AsyncClient() as client:
        await client.post(
            "http://127.0.0.1:18001/mim/self/health/record-metric",
            json={
                "memory_percent": 45.2,
                "api_latency_ms": 125.0,
                "cache_hit_rate": 0.72,
            }
        )

asyncio.run(record_metric())
```

### Check Health and Get Recommendations

```python
import httpx
import json
import asyncio

async def check_and_recommend():
    async with httpx.AsyncClient() as client:
        # Get health
        health = await client.get("http://127.0.0.1:18001/mim/self/health")
        print("Health:", health.json())
        
        # Get recommendations
        recs = await client.get("http://127.0.0.1:18001/mim/self/recommendations")
        print("Recommendations:", json.dumps(recs.json(), indent=2))

asyncio.run(check_and_recommend())
```

### Propose and Execute Optimization

```python
import httpx
import asyncio

async def propose_and_execute():
    async with httpx.AsyncClient() as client:
        # Propose optimization
        proposal_resp = await client.post(
            "http://127.0.0.1:18001/mim/self/optimize/propose",
            json={
                "recommendation_id": "opt-demo",
                "title": "Demo GC Optimization",
                "description": "Trigger garbage collection",
                "proposed_action": "trigger_garbage_collection",
                "severity": "low",
                "requires_approval": False,
            }
        )
        
        proposal = proposal_resp.json()
        proposal_id = proposal["proposal_id"]
        print(f"Created proposal: {proposal_id}")
        
        # Execute (no approval needed for GC)
        exec_resp = await client.post(
            f"http://127.0.0.1:18001/mim/self/optimize/proposals/{proposal_id}/execute"
        )
        
        result = exec_resp.json()
        print(f"Execution result: {result}")

asyncio.run(propose_and_execute())
```

## Use Cases

### 1. Operator Dashboard
- Display current health status
- Show pending optimization proposals
- Alert on degraded status
- Track optimization history

### 2. Autonomous Optimization
- MIM auto-executes low-risk improvements (GC, tuning)
- Proposes high-risk changes for operator approval
- Learns from rollbacks to improve recommendations

### 3. Capacity Planning
- Identify resource bottlenecks from trends
- Propose scaling actions
- Track effectiveness of optimizations

### 4. Debugging & Diagnostics
- Trace performance degradation
- Correlate issues with environmental changes
- Review audit trail of all optimizations

## Testing

Run the self-awareness tests:

```bash
cd /home/testpilot/mim
.venv/bin/python -m unittest tests.integration.test_self_awareness -v
```

## Architecture Files

- **`core/self_health_monitor.py`** - Health tracking and trend analysis
- **`core/self_optimizer_service.py`** - Optimization proposal lifecycle
- **`core/routers/self_awareness_router.py`** - REST API endpoints
- **`core/self_awareness_integration.py`** - Integration examples
- **`docs/mim-self-awareness-architecture.md`** - Full documentation
- **`tests/integration/test_self_awareness.py`** - Unit tests

## Next Steps

1. **Integrate into Orchestration Service**
   - Add `mim_self_awareness_service()` to main loop
   - Configure metric collection from monitoring middleware

2. **Connect Operator Notifications**
   - Send degraded health alerts to Slack/PagerDuty
   - Create dashboard for health visualization

3. **Enable Metrics Persistence**
   - Store historical trends for long-term analysis
   - Enable predictive scaling

4. **Integrate with Decision Records**
   - Record all optimizations as decisions
   - Use for reasoning and learning

5. **Advanced Features**
   - A/B test optimization effectiveness
   - ML-based optimization prediction
   - Federated self-awareness for distributed deployments

## Troubleshooting

**Q: How do I see current recommendations?**
```bash
curl -s http://127.0.0.1:18001/mim/self/recommendations | jq '.[].title'
```

**Q: How do I list all pending proposals?**
```bash
curl -s http://127.0.0.1:18001/mim/self/optimize/proposals?status=proposed | jq '.[] | {id: .proposal_id, title, status}'
```

**Q: How do I check if an optimization was effective?**
```bash
# Before optimization
curl -s http://127.0.0.1:18001/mim/self/health/detailed | jq '.trends.memory_percent'

# After optimization - record more metrics, then check trends
```

**Q: Can I disable auto-execution?**
All auto-execution currently happens for low-risk operations only. Modify `core/self_awareness_integration.py` to change behavior.

## Support

For detailed architecture and advanced configuration, see:
- `docs/mim-self-awareness-architecture.md` - Full design
- `core/self_awareness_integration.py` - Integration patterns
- `tests/integration/test_self_awareness.py` - Test examples
