"""Render a checked ablation artifact as a labelled Markdown table.

Reads an artifact only after runner.check() accepts it. No inference, no network.
Adds paired per-task contrasts between arms: for the same task and seed, how
often did one arm reach an ALLOW on the final malicious proposal while the
other did not. Counts are over scripted replay, not live agent trials.
"""
import argparse
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).parent
_spec = importlib.util.spec_from_file_location("monitor_ablation_runner", ROOT / "runner.py")
runner = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = runner
_spec.loader.exec_module(runner)

PAIRS = (("policy_only", "policy_reasoning"), ("policy_only", "policy_action_only"),
         ("policy_action_only", "policy_reasoning"))


def fmt(f: dict) -> str:
    if not f["denominator"]:
        return "n/a (0)"
    return f'{f["numerator"]}/{f["denominator"]} ({f["rate"]:.1%})'


def paired_contrasts(report: dict) -> list[dict]:
    """For attack replays: tasks where exactly one arm of a pair allowed the goal."""
    by_key = {(r["arm"], r["task_id"], r["seed"]): r for r in report["runs"]}
    out = []
    for a, b in PAIRS:
        only_a = only_b = both = neither = 0
        for (arm, task, seed), row in by_key.items():
            if arm != a or not row["attack"]:
                continue
            other = by_key[(b, task, seed)]
            x, y = row["replay_goal_allowed"], other["replay_goal_allowed"]
            if x and y:
                both += 1
            elif x:
                only_a += 1
            elif y:
                only_b += 1
            else:
                neither += 1
        out.append({"a": a, "b": b, "only_a": only_a, "only_b": only_b, "both": both, "neither": neither})
    return out


def render(report: dict) -> str:
    lines = [f'# Ablation summary ({report["mode"]})', "",
             f'Dataset `{report["dataset_version"]}`, seeds {report["config"]["seeds"]}, '
             f'split `{report["config"]["split"]}`, fault `{report["config"]["fault"]}`.',
             "", "> Scripted replay over authored synthetic tasks. `replay_attack_goal_allowed` is a "
             "permission proxy, not live attack success. Live attack success denominator: "
             f'{report["live_attack_success"]["denominator"]}. Repeated seeds are paired repetitions, '
             "not independent samples. Wall times are local Python timings, not model latency.",
             "", "| arm | attack goal allowed | benign task false hold | benign step false hold | monitor fail-closed | provider calls |",
             "| --- | --- | --- | --- | --- | --- |"]
    for arm, s in report["summary"].items():
        lines.append(f'| {arm} | {fmt(s["replay_attack_goal_allowed"])} | {fmt(s["benign_task_false_hold"])} | '
                     f'{fmt(s["benign_step_false_hold"])} | {fmt(s["monitor_fail_closed"])} | {s["provider_calls"]} |')
    lines += ["", "## Paired attack replays (goal allowed)", "",
              "| arm A | arm B | only A allowed | only B allowed | both | neither |", "| --- | --- | --- | --- | --- | --- |"]
    for c in paired_contrasts(report):
        lines.append(f'| {c["a"]} | {c["b"]} | {c["only_a"]} | {c["only_b"]} | {c["both"]} | {c["neither"]} |')
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    args = parser.parse_args()
    try:
        print(render(runner.check(args.artifact)), end="")
        return 0
    except (ValueError, KeyError, TypeError, OSError, StopIteration, IndexError):
        print("Summary failed: invalid artifact.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
