"""seed_demo.py must exit non-zero when a selected scenario was not seeded."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED = REPO_ROOT / "scripts" / "seed_demo.py"


def _seed(tmp_path: Path, scenarios: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SEED), "--db-path", str(tmp_path / "t.db"),
         "--workspace-root", str(tmp_path / "ws"), "--scenarios-dir", str(scenarios)],
        capture_output=True, text=True, check=False,
    )


def test_unparseable_scenario_fails_the_seed(tmp_path: Path) -> None:
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    shutil.copy(REPO_ROOT / "demo/scenarios/01_safe_audit.json", scenarios)
    (scenarios / "02_broken.json").write_text("{broken", encoding="utf-8")
    result = _seed(tmp_path, scenarios)
    assert result.returncode != 0, result.stdout
    assert "02_broken.json" in result.stdout + result.stderr


def test_no_matching_scenarios_fails_the_seed(tmp_path: Path) -> None:
    scenarios = tmp_path / "empty"
    scenarios.mkdir()
    assert _seed(tmp_path, scenarios).returncode != 0


def test_clean_seed_still_succeeds(tmp_path: Path) -> None:
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    shutil.copy(REPO_ROOT / "demo/scenarios/01_safe_audit.json", scenarios)
    assert _seed(tmp_path, scenarios).returncode == 0
