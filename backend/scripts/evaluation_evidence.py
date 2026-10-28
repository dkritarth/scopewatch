"""Offline, allowlisted publication of synthetic evaluation reports.

Raw reports are inputs only. Published summaries are recomputed, including
Wilson intervals; no trace, explanation, tool argument or arbitrary error
string is copied. Historical reports are never reconstructed from prose.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from scopewatch.providers.loader import get_profile
from scopewatch.reasoning_audit import AuditConcernType, AuditErrorCode
from scripts.evaluate_reasoning_audit import compute_metrics, compute_file_hash

ROOT = Path(__file__).resolve().parents[2]


def wilson_interval(numerator: int, denominator: int) -> dict[str, Any]:
    """Two-sided 95% Wilson score interval, without continuity correction."""
    if not 0 <= numerator <= denominator:
        raise ValueError("invalid binomial counts")
    result = {"numerator": numerator, "denominator": denominator,
              "method": "wilson", "confidence": 0.95,
              "estimate": None, "lower": None, "upper": None}
    if denominator:
        z = 1.959963984540054
        p = numerator / denominator
        scale = 1 + z * z / denominator
        centre = (p + z * z / (2 * denominator)) / scale
        radius = z * math.sqrt(p * (1 - p) / denominator + z * z / (4 * denominator**2)) / scale
        result.update(estimate=p, lower=round(max(0, centre - radius), 6),
                      upper=round(min(1, centre + radius), 6))
    return result


def confidence_intervals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """FAILED is a separate outcome, never silently removed from denominators."""
    concerns = [r for r in rows if r["expected_verdict"] == "CONCERN"]
    clean = [r for r in rows if r["expected_verdict"] == "NO_CONCERN"]
    return {
        "false_negative_rate": wilson_interval(sum(r["actual_verdict"] == "NO_CONCERN" for r in concerns), len(concerns)),
        "false_hold_rate": wilson_interval(sum(r["actual_verdict"] == "CONCERN" for r in clean), len(clean)),
        "failure_rate": wilson_interval(sum(r["actual_verdict"] == "FAILED" for r in rows), len(rows)),
        "accuracy": wilson_interval(sum(r["actual_verdict"] == r["expected_verdict"] for r in rows), len(rows)),
    }


class PublicationRow(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    case_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}$")
    category: Literal["clean", "drift", "injection_following", "exfiltration_intent", "policy_evasion", "ambiguous"]
    expected_verdict: Literal["CONCERN", "NO_CONCERN"]
    actual_verdict: Literal["CONCERN", "NO_CONCERN", "FAILED"]
    expected_concern_type: str | None = None
    concern_type: str | None = None
    match: bool
    latency_ms: float = Field(ge=0)
    error_code: str | None
    flagged_excerpts_count: int = Field(ge=0)
    total_tokens: int | None = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)


def load_manifest(path: Path) -> dict[str, Any]:
    """Validate dataset pins before trusting any synthetic-content declaration."""
    manifest = json.loads(path.read_text())
    if manifest["schema_version"] != 1 or manifest["content_origin"] != "synthetic":
        raise ValueError("unsupported dataset manifest")
    if not re.fullmatch(r"v[1-9][0-9]*", manifest["dataset_version"]):
        raise ValueError("invalid dataset version")
    if manifest["split_status"] not in {"spent", "frozen_unmeasured"}:
        raise ValueError("unsupported split status")
    seen: set[str] = set()
    for split, pin in manifest["splits"].items():
        if split not in {"dev", "heldout"}:
            raise ValueError("unsupported split")
        relative = Path(pin["file"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("dataset pin must use a repository-relative path")
        file = ROOT / relative
        if not file.resolve().is_relative_to(ROOT) or file.is_symlink():
            raise ValueError("dataset must be inside repository")
        cases = json.loads(file.read_text())
        if compute_file_hash(file) != pin["sha256"] or len(cases) != pin["count"]:
            raise ValueError("dataset pin mismatch")
        for case in cases:
            if case["case_id"] in seen:
                raise ValueError("duplicate dataset case id")
            seen.add(case["case_id"])
    if set(manifest["splits"]) != {"dev", "heldout"}:
        raise ValueError("both split pins are required")
    return manifest


def export_report(source: dict[str, Any], manifest_path: Path) -> dict[str, Any]:
    """Strip unknown fields, validate values, bind rows to the synthetic dataset.

    A manifest is a maintainer's synthetic-content assertion, not an automated
    proof that arbitrary inputs are public. Review the fixture before declaring
    it synthetic. Provider identity must match the current profile configuration.
    """
    manifest = load_manifest(manifest_path)
    meta = source["metadata"]
    split = meta["dataset_split"]
    pin = manifest["splits"][split]
    if meta["dataset_hash"] != pin["sha256"]:
        raise ValueError("report dataset hash mismatch")
    profile = get_profile(meta["profile"])
    if meta["model"] != profile.model:
        raise ValueError("report model differs from configured profile")
    timestamp = datetime.fromisoformat(meta["evaluation_date"].replace("Z", "+00:00"))
    if timestamp.utcoffset() is None:
        raise ValueError("evaluation date must include timezone")
    prompt_version = meta["prompt_version"]
    if not isinstance(prompt_version, str) or not re.fullmatch(r"v[0-9.]+-hardened-[a-f0-9]{8}", prompt_version):
        raise ValueError("invalid prompt version")
    if type(meta["seed"]) is not int:
        raise ValueError("seed must be integer")
    settings = meta.get("auditor_settings")
    if not isinstance(settings, dict) or set(settings) != {"max_trace_chars", "audit_timeout_s"}:
        raise ValueError("auditor settings required")
    for value in settings.values():
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
            raise ValueError("invalid auditor settings")
    if split == "heldout" and any(v is not None for v in settings.values()):
        raise ValueError("heldout tuning settings refused")
    cases = {c["case_id"]: c for c in json.loads((ROOT / pin["file"]).read_text())}
    rows = []
    for original in source["results"]:
        row = PublicationRow.model_validate({k: v for k, v in original.items() if k in PublicationRow.model_fields}).model_dump()
        case = cases.get(row["case_id"])
        if case is None or any(row[k] != case.get(k) for k in ("category", "expected_verdict", "expected_concern_type")):
            raise ValueError("result differs from pinned dataset")
        if row["match"] != (row["expected_verdict"] == row["actual_verdict"]):
            raise ValueError("incorrect match flag")
        if row["error_code"] not in {None, *(e.value for e in AuditErrorCode)}:
            raise ValueError("unknown error code")
        if row["error_code"] is not None and row["actual_verdict"] != "FAILED":
            raise ValueError("audit errors require a FAILED verdict")
        if row["concern_type"] not in {None, *(e.value for e in AuditConcernType)}:
            raise ValueError("unknown concern type")
        rows.append(row)
    if len(rows) != len(cases) or {r["case_id"] for r in rows} != set(cases) or meta["dataset_cases_count"] != len(rows):
        raise ValueError("results must cover each pinned case exactly once")
    metrics = compute_metrics(rows)
    if source["summary_metrics"] != metrics:
        raise ValueError("source summary differs from recomputed metrics; preserve historical source separately")
    return {
        "schema_version": 1,
        "evidence_kind": "mock_structural" if profile.name == "mock" else "provider_classifier",
        "content_origin": "synthetic",
        "dataset_version": manifest["dataset_version"],
        "split_status": manifest["split_status"],
        "metadata": {"evaluation_date": timestamp.isoformat(), "profile": profile.name,
                     "model": profile.model, "prompt_version": prompt_version,
                     "dataset_split": split, "dataset_file": pin["file"], "dataset_hash": pin["sha256"],
                     "dataset_cases_count": len(rows), "seed": meta["seed"], "auditor_settings": settings},
        "summary_metrics": metrics,
        "confidence_intervals": confidence_intervals(rows),
        "results": rows,
    }


def check_export(report: dict[str, Any], manifest_path: Path) -> None:
    """Exact reconstruction rejects unknown fields at every publication level."""
    if report != export_report(report, manifest_path):
        raise ValueError("publication differs from sanitized recomputation")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["export", "check"])
    parser.add_argument("report", type=Path)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = json.loads(args.report.read_text())
        if args.command == "check":
            check_export(report, args.manifest)
        else:
            if args.output is None or args.output.resolve() == args.report.resolve():
                raise ValueError("export needs a separate output path")
            exported = export_report(report, args.manifest)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(exported, indent=2, allow_nan=False) + "\n")
    except Exception:
        # Never echo validation inputs, provider strings, paths or trace text.
        print("EVIDENCE CHECK FAILED: invalid input or dataset/profile pin; no export written", file=sys.stderr)
        return 1
    print("EVIDENCE CHECK PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
