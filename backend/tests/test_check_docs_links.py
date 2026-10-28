"""Exercise the docs checker CLI against synthetic repository trees."""
from pathlib import Path
import subprocess
import sys

import pytest

CHECKER = Path(__file__).resolve().parents[2] / "scripts" / "check_docs_links.py"


def check(tmp_path: Path, text: str, files: dict[str, str] | None = None):
    (tmp_path / "README.md").write_text(text)
    for name, content in (files or {}).items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return subprocess.run(
        [sys.executable, str(CHECKER), "--root", str(tmp_path)],
        text=True, capture_output=True, check=False,
    )


@pytest.mark.parametrize("target", ["deploy/missing.md", "docs/submission/missing.md", "missing.md"])
def test_missing_markdown_targets_fail(tmp_path, target):
    result = check(tmp_path, f"[missing]({target})")
    assert result.returncode == 1, result.stdout
    assert f"broken ref: {target}" in result.stdout
    assert "not on main yet" not in result.stdout


def test_node_id_references_are_checked(tmp_path):
    result = check(tmp_path, "`backend/tests/missing.py::test_example`")
    assert result.returncode == 1, result.stdout
    assert "broken ref" in result.stdout


@pytest.mark.parametrize("claim", [
    "The configured model is Nemotron 70B.",
    "All 287 backend tests passed.",
    "Clone <REPO_URL>.",
    "The provider returns HTTP 401 and is currently unverified.",
    'No `deploy/` directory exists.',
])
def test_stale_claims_fail(tmp_path, claim):
    result = check(tmp_path, claim)
    assert result.returncode == 1, result.stdout
    assert "stale" in result.stdout or "unexpanded" in result.stdout


def test_history_does_not_excuse_another_current_claim(tmp_path):
    result = check(tmp_path, "The earlier credential problem was resolved.\nThe configured model is 70B.")
    assert result.returncode == 1, result.stdout


def test_current_deploy_files_are_checked_without_a_git_ref(tmp_path):
    result = check(tmp_path, "Deployment is on main.", {
        "deploy/compose.yaml": "services:\n  gateway:\n    volumes:\n      - /var/run/docker.sock:/var/run/docker.sock\n",
    })
    assert result.returncode == 1, result.stdout
    assert "unexpected docker.sock mount" in result.stdout
    assert "feature/42" not in result.stdout


def test_valid_templates_history_and_prior_art_urls_pass(tmp_path):
    result = check(tmp_path, '''[self]() and [whitespace](   ) and [section](#example)
[example](<HOSTED_URL>) and `<REPO_PATH>/README.md`
Create `poc/<name>/README.md` and run `backend/tests/example.py::test_case`.
Earlier calls returned HTTP 401 because of the wrong credential type; this was resolved.
See https://huggingface.co/nvidia/prior-art-70B and https://huggingface.co/meta-llama/example.
287 is a line number, as in `backend/tests/example.py:287`.
''', {"backend/tests/example.py": ""})
    assert result.returncode == 0, result.stdout + result.stderr


def test_angle_link_to_missing_file_fails(tmp_path):
    result = check(tmp_path, "[missing](<docs/missing.md>)")
    assert result.returncode == 1, result.stdout
    assert "broken ref" in result.stdout


def test_unknown_configured_model_claim_still_fails(tmp_path):
    result = check(tmp_path, 'Auditor model: `nvidia/invented-model`.')
    assert result.returncode == 1, result.stdout
    assert "invented model ID" in result.stdout


@pytest.mark.parametrize("claim", [
    "Nebius: HTTP 401 / currently unverified.",
    "Nebius is unverified, HTTP 401.",
    "The earlier issue was resolved.\nThe provider returns HTTP 401 and is unverified.",
])
def test_current_401_claim_is_not_hidden_by_history(tmp_path, claim):
    result = check(tmp_path, claim)
    assert result.returncode == 1, result.stdout
    assert "stale HTTP 401" in result.stdout


def test_deploy_templates_runner_and_private_local_env(tmp_path):
    result = check(tmp_path, "[deployment](deploy/compose.yaml)", {
        "deploy/compose.yaml": "services: {}\n",
        "deploy/.env.example": "NEBIUS_API_KEY=\nDEMO_TOKEN=change-me-to-a-long-random-value\n",
        "deploy/.env": "DEMO_TOKEN=synthetic-private-local-value\n",
        "deploy/executor-runner/compose.yaml": "volumes:\n  - /var/run/docker.sock:/var/run/docker.sock\n",
    })
    assert result.returncode == 0, result.stdout + result.stderr


def test_secret_findings_redact_values(tmp_path):
    value = "synthetic-key-value-for-checker"
    result = check(tmp_path, "Deploy source is scanned.", {
        "deploy/.env.example": f"NEBIUS_API_KEY={value}\n",
    })
    assert result.returncode == 1, result.stdout
    assert "possible secret" in result.stdout
    assert value not in result.stdout + result.stderr


def test_stale_provider_config_is_checked(tmp_path):
    result = check(tmp_path, "No stale claims here.", {
        "backend/config/providers.toml": '# Uses the 70B model\nmodel = "mock-rules-auditor"\n',
    })
    assert result.returncode == 1, result.stdout
    assert "backend/config/providers.toml: stale 70B" in result.stdout


def test_nested_docs_resolve_deploy_and_encoded_local_links(tmp_path):
    result = check(tmp_path, "[guide](docs/operations/judge-runbook.md)", {
        "docs/operations/judge-runbook.md": "[deploy](deploy/compose.yaml) and [space](<../file name.md>) and [encoded](../file%20name.md)",
        "docs/file name.md": "Synthetic local file.",
        "deploy/compose.yaml": "services: {}\n",
    })
    assert result.returncode == 0, result.stdout + result.stderr
