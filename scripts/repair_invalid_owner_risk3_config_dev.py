from __future__ import annotations

import json
from pathlib import Path

from scripts.owner_risk3_gateway import default_config_path


def main() -> int:
    path = default_config_path()
    if not path.exists():
        print(json.dumps({"status": "no_existing_config"}, sort_keys=True))
        return 0
    backup = path.with_name("owner-risk3-exceptions.local.invalid-20260917.json")
    if backup.exists():
        backup = path.with_name("owner-risk3-exceptions.local.invalid-20260917-2.json")
    path.replace(backup)
    print(json.dumps({"status": "backed_up", "backup": str(backup)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
