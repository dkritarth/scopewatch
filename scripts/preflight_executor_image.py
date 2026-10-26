#!/usr/bin/env python3
"""Preflight check for the coding-demo executor image (issue #302)."""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
from scopewatch.executor_docker import preflight_executor_image  # noqa: E402

ok, message = preflight_executor_image()
print(message)
sys.exit(0 if ok else 1)