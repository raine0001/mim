"""Example integration of self-awareness into MIM orchestration.

This shows how to integrate self-monitoring and self-optimization into the
orchestration service's lifecycle and decision-making loops.

See mim-self-awareness-architecture.md for full documentation.
"""

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Constants
SELF_AWARENESS_BASE_URL = "http://127.0.0.1:18001"
HEALTH_CHECK_INTERVAL = 5.0  # seconds
RECOMMENDATION_CHECK_INTERVAL = 15.0  # seconds


async def get_current_metrics() -> dict[str, Any]:
    """Gather current MIM operational metrics.
    
    In practice, these would come from:
    - psutil for process metrics (memory, CPU)
    - FastAPI middleware for API latency/errors
    - Database connection pool stats
    - State bus consumer lag
    - Cache statistics
    - Worker pool queue depth
    """
    import os
    import psutil
    
    try:
        process = psutil.Process(os.getpid())
        mem = process.memory_info()
        
        return {
            "memory_mb": mem.rss // (1024 * 1024),
            "memory_percent": process.memory_percent(),
            "cpu_percent": process.cpu_percent(interval=0.1),
            # In production, fetch these from monitoring middleware/services
            "api_latency_ms": 45.2,  # stub
            "api_error_rate": 0.005,  # stub
            "cache_hit_rate": 0.72,  # stub
            "state_bus_lag_ms": 250,  # stub
            "worker_queue_depth": 8,  # stub
        }
    except Exception as e:
        logger.error(f"Failed to gather metrics: {e}")
        return {}


async def record_self_health_metric() -> dict[str, Any]:
    """Record a health metric sample with MIM's self-health monitor.
    
    Called periodically to build up health trends and enable diagnostics.
    """
    metrics = await get_current_metrics()
    if not metrics:
        return {}
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SELF_AWARENESS_BASE_URL}/mim/self/health/record-metric",
                json=metrics,
                timeout=5.0,
            )
            result = response.json()
            logger.debug(f"Health metric recorded: {result}")
            return result
    except Exception as e:
        logger.warning(f"Failed to record health metric: {e}")
        return {}


async def get_health_status() -> dict[str, Any]:
    """Fetch MIM's current health status from self-awareness service."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SELF_AWARENESS_BASE_URL}/mim/self/health",
                timeout=5.0,
            )
            health = response.json()
            return health
    except Exception as e:
        logger.warning(f"Failed to fetch health status: {e}")
        return {}


async def check_health_and_alert() -> None:
    """Check health status and alert if degraded.
    
    Can be integrated into operator notifications/dashboards.
    """
    health = await get_health_status()
    if not health:
        return
    
    status = health.get("status")
    
    if status == "critical":
        logger.critical(f"MIM health CRITICAL: {health}")
        # Trigger operator alerts (PagerDuty, Slack, etc.)
        await notify_operator(severity="critical", health=health)
        
    elif status == "degraded":
        logger.warning(f"MIM health DEGRADED: {health}")
        await notify_operator(severity="warning", health=health)


async def consider_auto_optimizations() -> None:
    """Check for optimization recommendations and auto-execute safe ones.
    
    This is the autonomous self-improvement loop where MIM proposes and
    executes optimizations within approved boundaries.
    """
    try:
        async with httpx.AsyncClient() as client:
            # Get current recommendations
            response = await client.get(
                f"{SELF_AWARENESS_BASE_URL}/mim/self/recommendations",
                timeout=5.0,
            )
            recommendations = response.json()
            
            if not recommendations:
                logger.debug("No optimization recommendations at this time")
                return
            
            logger.info(f"Found {len(recommendations)} optimization recommendations")
            
            for rec in recommendations:
                await process_recommendation(client, rec)
                
    except Exception as e:
        logger.error(f"Failed to consider optimizations: {e}")


async def process_recommendation(client: httpx.AsyncClient, rec: dict[str, Any]) -> None:
    """Process a single optimization recommendation.
    
    Creates proposal and either auto-executes (low-risk) or alerts operator (high-risk).
    """
    try:
        # Create proposal from recommendation
        response = await client.post(
            f"{SELF_AWARENESS_BASE_URL}/mim/self/optimize/propose",
            json={
                "recommendation_id": rec["recommendation_id"],
                "title": rec["title"],
                "description": rec["description"],
                "proposed_action": rec["proposed_action"],
                "severity": rec["severity"],
                "requires_approval": rec.get("requires_approval", True),
            },
            timeout=5.0,
        )
        
        if response.status_code != 200:
            logger.error(f"Failed to create proposal: {response.text}")
            return
        
        proposal = response.json()
        proposal_id = proposal["proposal_id"]
        
        if not proposal.get("requires_approval"):
            # Auto-execute low-risk optimizations
            logger.info(f"Auto-executing low-risk optimization: {proposal_id}")
            exec_response = await client.post(
                f"{SELF_AWARENESS_BASE_URL}/mim/self/optimize/proposals/{proposal_id}/execute",
                timeout=10.0,
            )
            
            if exec_response.status_code == 200:
                result = exec_response.json()
                logger.info(f"Optimization completed: {result}")
            else:
                logger.error(f"Optimization execution failed: {exec_response.text}")
        else:
            # Notify operator for approval
            logger.info(f"Optimization proposal requires approval: {proposal_id}")
            await notify_operator(
                severity="info",
                message=f"Optimization proposal requires approval: {proposal['title']}",
                proposal=proposal,
            )
    
    except Exception as e:
        logger.error(f"Error processing recommendation: {e}")


async def monitor_optimization_proposals() -> None:
    """Periodically monitor pending optimization proposals and their status.
    
    Can be used to enforce approval deadlines or track effectiveness.
    """
    try:
        async with httpx.AsyncClient() as client:
            # Get proposed optimizations awaiting approval
            response = await client.get(
                f"{SELF_AWARENESS_BASE_URL}/mim/self/optimize/proposals?status=proposed",
                timeout=5.0,
            )
            
            proposed = response.json()
            if proposed:
                logger.info(f"{len(proposed)} optimizations awaiting approval")
                for prop in proposed:
                    logger.debug(f"  - {prop['title']} (severity: {prop['severity']})")
            
            # Get recently completed optimizations
            response = await client.get(
                f"{SELF_AWARENESS_BASE_URL}/mim/self/optimize/proposals?status=completed",
                timeout=5.0,
            )
            completed = response.json()
            if completed:
                logger.debug(f"{len(completed)} optimizations recently completed")
    
    except Exception as e:
        logger.warning(f"Failed to monitor proposals: {e}")


async def mim_self_awareness_service() -> None:
    """Main service loop for MIM self-awareness.
    
    Runs in background, periodically:
    1. Records health metrics
    2. Evaluates health status
    3. Considers optimizations
    4. Monitors proposal lifecycle
    
    Integrate by calling this in orchestration service's main loop.
    """
    logger.info("Starting MIM self-awareness service")
    
    try:
        while True:
            # Record health metric
            await record_self_health_metric()
            
            # Check health status (every 5 seconds)
            await check_health_and_alert()
            
            # Periodically check recommendations and propose optimizations
            if int(asyncio.get_event_loop().time()) % 15 == 0:
                await consider_auto_optimizations()
                await monitor_optimization_proposals()
            
            # Sleep until next cycle
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
    
    except asyncio.CancelledError:
        logger.info("MIM self-awareness service stopped")
    except Exception as e:
        logger.error(f"Unexpected error in self-awareness service: {e}")
        # Re-raise to trigger restart/alerting
        raise


async def notify_operator(
    severity: str,
    message: str | None = None,
    health: dict[str, Any] | None = None,
    proposal: dict[str, Any] | None = None,
) -> None:
    """Notify operator of health issues or optimization proposals.
    
    Stub for integration with alerting systems (Slack, PagerDuty, email, etc.)
    """
    notification = {
        "severity": severity,
        "message": message,
        "health": health,
        "proposal": proposal,
    }
    logger.info(f"OPERATOR NOTIFICATION: {notification}")
    
    # Integration points:
    # - Send to Slack webhook
    # - Create PagerDuty incident
    # - Send email
    # - Update dashboard
    # - Record to decision log


# ============================================================================
# Integration Example: Adding to Orchestration Service
# ============================================================================

async def example_orchestration_integration():
    """Show how to integrate self-awareness into orchestration service.
    
    In core/orchestration_service.py or similar:
    
    ```python
    import asyncio
    from core.self_awareness_integration import mim_self_awareness_service
    
    class OrchestrationService:
        async def run(self):
            # Start all background services
            tasks = [
                asyncio.create_task(self.main_orchestration_loop()),
                asyncio.create_task(mim_self_awareness_service()),
                # ... other services
            ]
            
            await asyncio.gather(*tasks)
    ```
    """
    pass


# ============================================================================
# Dashboard Query Examples
# ============================================================================

async def dashboard_health_status() -> dict[str, Any]:
    """Query health for dashboard display."""
    return await get_health_status()


async def dashboard_optimization_history() -> list[dict[str, Any]]:
    """Get optimization proposal history for dashboard."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SELF_AWARENESS_BASE_URL}/mim/self/optimize/proposals",
                timeout=5.0,
            )
            return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch optimization history: {e}")
        return []


async def dashboard_detailed_health() -> dict[str, Any]:
    """Get detailed health report for dashboard."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SELF_AWARENESS_BASE_URL}/mim/self/health/detailed",
                timeout=5.0,
            )
            return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch detailed health: {e}")
        return {}


if __name__ == "__main__":
    # Example: Run self-awareness service standalone
    logging.basicConfig(level=logging.DEBUG)
    asyncio.run(mim_self_awareness_service())
