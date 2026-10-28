"""Check availability declarations, including deliberately missing historical files."""

import argparse
import json
from pathlib import Path
import sys

from scripts.evaluation_evidence import ROOT, check_export, export_report
from scripts.evaluate_reasoning_audit import compute_file_hash


def check_artifacts(path: Path) -> None:
    manifest = json.loads(path.read_text())
    if manifest["schema_version"] != 1:
        raise ValueError("unsupported artifact manifest")
    seen = set()
    for item in manifest["artifacts"]:
        if item["id"] in seen:
            raise ValueError("duplicate artifact id")
        seen.add(item["id"])
        source = ROOT / item["source_file"]
        if not source.resolve().is_relative_to(ROOT):
            raise ValueError("source outside repository")
        if item["status"] == "missing":
            if source.exists() or item.get("source_sha256") is not None:
                raise ValueError("missing artifact declaration disagrees with disk")
            if item["verification"] != "documented_only_unverified":
                raise ValueError("missing reports cannot be verified")
        elif item["status"] == "available":
            if compute_file_hash(source) != item["source_sha256"]:
                raise ValueError("source artifact hash mismatch")
            publication = ROOT / item["publication_file"]
            dataset_manifest = ROOT / item["dataset_manifest"]
            if not all(p.resolve().is_relative_to(ROOT) for p in (publication, dataset_manifest)):
                raise ValueError("publication outside repository")
            published = json.loads(publication.read_text())
            check_export(published, dataset_manifest)
            if published != export_report(json.loads(source.read_text()), dataset_manifest):
                raise ValueError("publication does not match available source")
        else:
            raise ValueError("unsupported availability status")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    try:
        check_artifacts(args.manifest)
    except Exception:
        print("ARTIFACT CHECK FAILED: declaration does not match available evidence", file=sys.stderr)
        return 1
    print("ARTIFACT CHECK PASSED; missing reports remain unverified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
