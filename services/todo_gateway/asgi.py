import os

from services.todo_gateway.gateway import create_app_from_env

app = create_app_from_env()

# Existing deployments retain their behavior. Schema migration is explicit.
triage_mode = os.environ.get("TODO_TRIAGE_MODE", "disabled")
if triage_mode not in {"disabled", "dev"}:
    raise RuntimeError("TODO_TRIAGE_MODE must be disabled or dev")
if triage_mode == "dev":
    if os.environ.get("TODO_TRIAGE_ENVIRONMENT") != "DEV":
        raise RuntimeError("triage reservations require an explicitly authorized DEV environment")
    from services.todo_gateway.repository import PostgresQueueRepository
    from services.todo_gateway.triage_reservation import install_routes

    install_routes(app, PostgresQueueRepository(os.environ["DATABASE_URL"]), os.environ["TODO_GATEWAY_TOKEN"])
