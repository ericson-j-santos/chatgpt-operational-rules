from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    exe = Path(sys.executable)
    base = Path(sys.base_prefix)
    prefix = Path(sys.prefix)
    result = {
        "executable": str(exe),
        "executable_exists": exe.is_file(),
        "base_prefix": str(base),
        "base_prefix_exists": base.is_dir(),
        "prefix": str(prefix),
        "prefix_exists": prefix.is_dir(),
        "python_version": sys.version.split()[0],
        "path_entry_count": len(os.environ.get("PATH", "").split(os.pathsep)),
        "secret_value_exposed": False,
    }
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
