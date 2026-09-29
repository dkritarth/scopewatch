"""Pytest bootstrap for the MCP gateway prototype (stdlib only)."""

import sys
from pathlib import Path

POC_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = POC_ROOT.parent.parent

for candidate in (REPO_ROOT / "backend", POC_ROOT / "src"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))
