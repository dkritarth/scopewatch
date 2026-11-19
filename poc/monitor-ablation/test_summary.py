"""Summary renderer: labels, paired counts, and refusal of tampered artifacts."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location("monitor_summary", ROOT / "summary.py")
summary = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = summary
spec.loader.exec_module(summary)


def test_paired_counts_partition_attack_replays():
    report = summary.runner.run(seeds=[17])
    for c in summary.paired_contrasts(report):
        assert c["only_a"] + c["only_b"] + c["both"] + c["neither"] == 12
    po = summary.paired_contrasts(report)[0]
    # reasoning arm never allows a goal that policy-only blocked: monitor only escalates
    assert po["only_b"] == 0


def test_render_labels_proxy_and_zero_live_denominator():
    text = summary.render(summary.runner.run(seeds=[17]))
    assert "not live attack success" in text
    assert "Live attack success denominator: 0" in text
    assert "policy_action_only" in text


def test_cli_checks_before_rendering(tmp_path):
    good = tmp_path / "a.json"
    good.write_text(json.dumps(summary.runner.run(seeds=[17]), allow_nan=False))
    ok = subprocess.run([sys.executable, str(ROOT / "summary.py"), str(good)], capture_output=True, text=True)
    assert ok.returncode == 0 and "Ablation summary" in ok.stdout
    data = json.loads(good.read_text())
    data["summary"]["policy_only"]["replay_attack_goal_allowed"]["numerator"] = 0
    bad = tmp_path / "b.json"
    bad.write_text(json.dumps(data))
    fail = subprocess.run([sys.executable, str(ROOT / "summary.py"), str(bad)], capture_output=True, text=True)
    assert fail.returncode == 1 and fail.stdout == ""
