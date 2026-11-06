"""Site exports must represent one committed database snapshot."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import threading

import pytest

from scopewatch.db import init_db
from scopewatch.schemas import SubmitActionRequest, TaskScope
from scopewatch.service import ScopewatchService

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("site_export_snapshot", ROOT / "scripts/export_site_demo.py")
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)


@pytest.mark.parametrize("approve", [True, False], ids=["approve", "deny"])
def test_export_retains_snapshot_during_approval_settlement(tmp_path, monkeypatch, approve):
    monkeypatch.setenv("SCOPEWATCH_REASONING_AUDIT", "false")
    monkeypatch.setenv("SCOPEWATCH_AGENT_PROFILE", "mock")
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "local")
    database = tmp_path / "gateway.db"
    init_db(database)
    workspace = tmp_path / "workspace"
    (workspace / "outputs").mkdir(parents=True)
    service = ScopewatchService(db_path=database, workspace_root=workspace)
    scenario = json.loads(next((ROOT / "demo/scenarios").glob("06_*.json")).read_text())
    run, _ = service.create_run(name=scenario["name"], task_scope=TaskScope(
        schema_version="1", task_description="Write a synthetic output",
        allowed_paths=["outputs"], allowed_tools=["workspace"],
        allowed_operations=["write_text"], requires_approval=["write_text"],
        created_at="2026-11-05T00:00:00Z"))
    response = asyncio.run(service.submit_action(run.id, SubmitActionRequest(
        tool="workspace", operation="write_text", resource="outputs/recorded.txt",
        arguments={"content": "synthetic output"})))
    assert response.approval_request is not None
    assert response.execution_receipt is None

    approval_read = threading.Event()
    settled = threading.Event()
    real_evaluate = exporter.evaluate_policy

    def pause_after_approval_read(*args, **kwargs):
        # The final action has no remaining outer cursor rows to retain an
        # implicit SQLite read transaction. This SELECT needs an explicit one.
        approval_read.set()
        assert settled.wait(10), "settlement did not complete"
        return real_evaluate(*args, **kwargs)

    monkeypatch.setattr(exporter, "evaluate_policy", pause_after_approval_read)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(exporter.export, database)
        try:
            assert approval_read.wait(10), "export did not reach the approval read"
            asyncio.run(service.resolve_approval(response.approval_request.id, approve))
        finally:
            settled.set()
        exported = pending.result(timeout=10)["runs"][0]["actions"][0]
    assert exported["approval"] == "PENDING"
    assert exported["execution"] is None

    monkeypatch.setattr(exporter, "evaluate_policy", real_evaluate)
    fresh = exporter.export(database)["runs"][0]["actions"][0]
    assert fresh["approval"] == ("CONSUMED" if approve else "DENIED")
    assert fresh["execution"]["status"] == ("EXECUTED" if approve else "NOT_EXECUTED")
