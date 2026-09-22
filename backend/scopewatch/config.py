"""Configuration settings for Scopewatch baseline."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_DATA_DIR = BASE_DIR / "runtime-data"
DEFAULT_WORKSPACE_DIR = BASE_DIR / "demo" / "workspace"

DB_PATH = Path(os.environ.get("SCOPEWATCH_DB_PATH", str(DEFAULT_DATA_DIR / "scopewatch.db")))
WORKSPACE_ROOT = Path(os.environ.get("SCOPEWATCH_WORKSPACE_ROOT", str(DEFAULT_WORKSPACE_DIR)))
DEMO_REVIEWER_ID = "demo-reviewer (synthetic)"
DEFAULT_EXPIRY_SECONDS = int(os.environ.get("SCOPEWATCH_APPROVAL_TTL", "300"))
MAX_READ_BYTES = 256 * 1024  # 256 KiB
MAX_WRITE_BYTES = 64 * 1024  # 64 KiB
