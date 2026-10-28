import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.check_evaluation_artifacts import check_artifacts
from scripts.evaluate_reasoning_audit import evaluate_reasoning_auditor
from scripts.evaluation_evidence import ROOT

MANIFEST = ROOT / "backend/fixtures/eval/v2/manifest.json"


def test_historical_manifest_keeps_unavailable_sources_unverified():
    manifest = ROOT / "docs/evaluation/artifacts.json"
    check_artifacts(manifest)
    entries = json.loads(manifest.read_text())["artifacts"]
    assert len(entries) == 4
    assert all(e["status"] == "missing" and e["source_sha256"] is None for e in entries)


def test_missing_artifact_cannot_claim_an_existing_source(tmp_path):
    manifest = {"schema_version": 1, "artifacts": [{
        "id": "invented", "source_file": "docs/evaluation.md", "status": "missing",
        "source_sha256": None, "verification": "documented_only_unverified"}]}
    path = tmp_path / "artifacts.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="disagrees with disk"):
        check_artifacts(path)


def test_cli_export_check_and_failures_never_echo_private_input(tmp_path):
    source = tmp_path / "source.json"
    output = tmp_path / "public.json"
    report = evaluate_reasoning_auditor(profile="mock", split="dev", quiet=True,
                                      cases_dir=str(MANIFEST.parent))
    report["results"][0]["explanation"] = "SYNTHETIC_PRIVATE_TEXT"
    source.write_text(json.dumps(report))
    command = [sys.executable, "-m", "scripts.evaluation_evidence"]
    def run(*args):
        import os
        return subprocess.run(command + list(args), cwd=ROOT,
                              env={**os.environ, "PYTHONPATH": str(ROOT / "backend")},
                              capture_output=True, text=True)
    result = run("export", str(source), "--manifest", str(MANIFEST), "--output", str(output))
    assert result.returncode == 0, result.stderr
    assert "SYNTHETIC_PRIVATE_TEXT" not in output.read_text()
    assert run("check", str(output), "--manifest", str(MANIFEST)).returncode == 0
    published = json.loads(output.read_text())
    published["results"][0]["error_code"] = "SYNTHETIC_PRIVATE_TEXT"
    output.write_text(json.dumps(published))
    result = run("check", str(output), "--manifest", str(MANIFEST))
    assert result.returncode == 1
    assert "SYNTHETIC_PRIVATE_TEXT" not in result.stdout + result.stderr
    # Failed export must not create an output file or overwrite the source.
    report["metadata"]["profile"] = "SYNTHETIC_PRIVATE_TEXT"
    source.write_text(json.dumps(report))
    failed = tmp_path / "failed.json"
    result = run("export", str(source), "--manifest", str(MANIFEST), "--output", str(failed))
    assert result.returncode == 1
    assert not failed.exists()
    assert "SYNTHETIC_PRIVATE_TEXT" not in result.stdout + result.stderr
