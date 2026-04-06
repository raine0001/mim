import asyncio
import os
from pathlib import Path


def objective85_database_url() -> str:
    configured = str(os.getenv("DATABASE_URL", "")).strip()
    if configured:
        return configured
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or not line.startswith("DATABASE_URL="):
                continue
            return line.split("=", 1)[1].strip()
    return "postgresql+asyncpg://postgres:postgres@localhost:5432/mim"


def cleanup_objective85_rows() -> None:
    asyncio.run(_cleanup_objective85_rows_async())


def cleanup_objective86_rows() -> None:
    asyncio.run(_cleanup_objective85_rows_async())


def cleanup_objective87_rows() -> None:
    asyncio.run(_cleanup_objective85_rows_async())


def cleanup_objective88_rows() -> None:
    asyncio.run(_cleanup_objective85_rows_async())


def age_objective88_preferences(*, managed_scope: str, hours: int) -> None:
    asyncio.run(_age_objective88_preferences_async(managed_scope=managed_scope, hours=hours))


async def _cleanup_objective85_rows_async() -> None:
    import asyncpg

    dsn = objective85_database_url().replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            """
            DO $$
            BEGIN
                IF to_regclass('public.workspace_operator_resolution_commitment_monitoring_profiles') IS NOT NULL THEN
                    DELETE FROM workspace_operator_resolution_commitment_monitoring_profiles
                    WHERE managed_scope LIKE 'objective85-%'
                       OR managed_scope LIKE 'objective86-%'
                       OR managed_scope LIKE 'objective87-%'
                       OR commitment_id IN (
                            SELECT id
                            FROM workspace_operator_resolution_commitments
                            WHERE managed_scope LIKE 'objective85-%'
                               OR managed_scope LIKE 'objective86-%'
                               OR managed_scope LIKE 'objective87-%'
                       );
                END IF;
            END $$
            """
        )
        await conn.execute(
            """
            DO $$
            BEGIN
                IF to_regclass('public.workspace_operator_resolution_commitment_outcome_profiles') IS NOT NULL THEN
                    DELETE FROM workspace_operator_resolution_commitment_outcome_profiles
                    WHERE managed_scope LIKE 'objective85-%'
                       OR managed_scope LIKE 'objective86-%'
                       OR managed_scope LIKE 'objective87-%'
                       OR commitment_id IN (
                            SELECT id
                            FROM workspace_operator_resolution_commitments
                            WHERE managed_scope LIKE 'objective85-%'
                               OR managed_scope LIKE 'objective86-%'
                               OR managed_scope LIKE 'objective87-%'
                       );
                END IF;
            END $$
            """
        )
        await conn.execute(
            """
            WITH target_states AS (
                SELECT id
                FROM workspace_stewardship_states
                WHERE managed_scope LIKE 'objective85-%'
                   OR managed_scope LIKE 'objective86-%'
                   OR managed_scope LIKE 'objective87-%'
            )
            DELETE FROM workspace_stewardship_cycles
            WHERE stewardship_id IN (SELECT id FROM target_states)
            """
        )
        await conn.execute(
            "DELETE FROM workspace_stewardship_states WHERE managed_scope LIKE 'objective85-%' OR managed_scope LIKE 'objective86-%' OR managed_scope LIKE 'objective87-%'"
        )
        await conn.execute(
            "DELETE FROM workspace_desired_environment_states WHERE scope_ref LIKE 'objective85-%' OR scope_ref LIKE 'objective86-%' OR scope_ref LIKE 'objective87-%'"
        )
        await conn.execute(
            "DELETE FROM workspace_execution_truth_governance_profiles WHERE managed_scope LIKE 'objective85-%' OR managed_scope LIKE 'objective86-%' OR managed_scope LIKE 'objective87-%'"
        )
        await conn.execute(
            "DELETE FROM workspace_autonomy_boundary_profiles WHERE scope LIKE 'objective85-%' OR scope LIKE 'objective86-%' OR scope LIKE 'objective87-%'"
        )
        await conn.execute(
            "DELETE FROM workspace_operator_resolution_commitments WHERE managed_scope LIKE 'objective85-%' OR managed_scope LIKE 'objective86-%' OR managed_scope LIKE 'objective87-%'"
        )
        await conn.execute(
            "DELETE FROM workspace_inquiry_questions WHERE trigger_evidence_json->>'managed_scope' LIKE 'objective85-%' OR trigger_evidence_json->>'managed_scope' LIKE 'objective86-%' OR trigger_evidence_json->>'managed_scope' LIKE 'objective87-%'"
        )
        await conn.execute(
            "DELETE FROM workspace_strategy_goals WHERE source LIKE 'objective85-%' OR source LIKE 'objective86-%' OR source LIKE 'objective87-%'"
        )
        await conn.execute(
            "DELETE FROM user_preferences WHERE source = 'objective88' OR preference_type LIKE 'operator_learned_preference:%'"
        )
    finally:
        await conn.close()


async def _age_objective88_preferences_async(*, managed_scope: str, hours: int) -> None:
    import asyncpg

    dsn = objective85_database_url().replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            """
            UPDATE user_preferences
            SET last_updated = NOW() - (($2::text || ' hours')::interval)
            WHERE preference_type LIKE 'operator_learned_preference:%'
              AND value->>'managed_scope' = $1
            """,
            str(managed_scope),
            str(int(hours)),
        )
    finally:
        await conn.close()