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
    if os.environ.get("TODO_TRIAGE_PUBLISH_MODE", "disabled") == "dev":
        from services.todo_gateway.triage_pipeline import install_pipeline as install_routes
    elif os.environ.get("TODO_TRIAGE_PUBLISH_MODE", "disabled") == "disabled":
        from services.todo_gateway.triage_reservation import install_routes
    else:
        raise RuntimeError("TODO_TRIAGE_PUBLISH_MODE must be disabled or dev")

    install_routes(app, PostgresQueueRepository(os.environ["DATABASE_URL"]), os.environ["TODO_GATEWAY_TOKEN"])

elif os.environ.get("TODO_TRIAGE_PUBLISH_MODE", "disabled") != "disabled":
    raise RuntimeError("triage publishing requires the reservation DEV gate")
