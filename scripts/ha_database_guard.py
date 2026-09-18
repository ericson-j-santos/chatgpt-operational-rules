from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlparse

ENABLED = {"1", "true", "yes", "enabled", "on"}


def ha_mode_enabled(env: Mapping[str, str]) -> bool:
    return str(env.get("HA_MODE", "")).strip().casefold() in ENABLED


def validate_ha_database(env: Mapping[str, str]) -> None:
    if not ha_mode_enabled(env):
        return

    dsn = str(env.get("DATABASE_URL", "")).strip()
    if not dsn:
        raise RuntimeError("HA_MODE requires DATABASE_URL")

    parsed = urlparse(dsn)
    host = (parsed.hostname or "").casefold()
    database = (parsed.path or "").lstrip("/")
    required_suffix = str(env.get("HA_DATABASE_REQUIRED_HOST_SUFFIX", ".neon.tech")).strip().casefold()
    required_name = str(env.get("HA_DATABASE_REQUIRED_NAME", "todo_global_bus_dev_ha")).strip()

    if not host:
        raise RuntimeError("HA_MODE DATABASE_URL host is invalid")
    if required_suffix and not host.endswith(required_suffix):
        raise RuntimeError("HA_MODE refuses non-canonical control-plane database host")
    if required_name and database != required_name:
        raise RuntimeError("HA_MODE refuses non-canonical control-plane database name")
