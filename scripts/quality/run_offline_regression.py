"""Run the complete offline suite with this interpreter; never skip missing deps."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    required = ("pytest", "pydantic", "pypdf", "pypdfium2", "PIL", "neo4j", "fastapi", "uvicorn", "httpx")
    missing = [name for name in required if importlib.util.find_spec(name) is None]
    if missing:
        print("Missing test dependencies: " + ", ".join(missing), file=sys.stderr)
        print('Install the project with ".[dev,vision,graph,web]" in its dedicated environment.', file=sys.stderr)
        return 2
    environment = os.environ.copy()
    environment["WT_OFFLINE_TESTS"] = "1"
    environment["PYTHONPATH"] = str(root / "src")
    return subprocess.run([sys.executable, "-m", "pytest", str(root / "tests"), *sys.argv[1:]], cwd=root, env=environment).returncode


if __name__ == "__main__":
    raise SystemExit(main())

