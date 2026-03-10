# MIM v1 Core Architecture

MIM v1 is a local-first intelligence server with optional integrations.

## Stack

- FastAPI
- PostgreSQL
- SQLAlchemy (async)
- Alembic (ready for migrations)

## Core API endpoints

- `GET /` and `GET /health`
- `GET /status`
- `GET /manifest`
- `POST/GET /objectives`
- `POST/GET /tasks`
- `POST/GET /results`
- `POST/GET /reviews`
- `POST/GET /journal`
- `POST/GET /memory`
- `POST/GET /tools`
- `POST/GET /services`
- `POST /services/{service_id}/heartbeat`

## Database tables

- objectives
- tasks
- task_results
- task_reviews
- execution_journal
- memory_entries
- memory_links
- tools
- tool_invocations
- services
- projects
- actors

## Startup

1. `cp config/.env.example .env`
2. Update `DATABASE_URL` for your local PostgreSQL credentials/database.
3. `source .venv/bin/activate`
4. `python scripts/init_db.py`
5. `uvicorn core.app:app --host 0.0.0.0 --port 8000`
