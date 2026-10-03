#!/usr/bin/env python3
"""Verify every COPY source path in the repo's Dockerfiles survives .dockerignore.

Docker builds fail loudly if a COPY source is excluded from the context, so
this is a cheap structural guard for a file that is easy to get wrong.
"""
import re
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_patterns(di: pathlib.Path) -> list[str]:
    return [
        line.strip()
        for line in di.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def make_matcher(patterns: list[str]):
    """Approximate .dockerignore matching; later patterns win, `!` negates."""

    def ignored(path: str) -> bool:
        verdict = False
        for pat in patterns:
            neg = pat.startswith("!")
            raw = pat[1:] if neg else pat
            base = raw.rstrip("/")
            if (
                path == base
                or path.startswith(base + "/")
                or pathlib.PurePosixPath(path).match(base)
            ):
                verdict = not neg
        return verdict

    return ignored


def copy_sources(dockerfile: pathlib.Path) -> list[str]:
    out = []
    for line in dockerfile.read_text().splitlines():
        m = re.match(r"^(?:COPY|ADD)\s+(.*)$", line.strip())
        if not m:
            continue
        parts = m.group(1).split()
        parts = [p for p in parts if not p.startswith("--")]
        # Every COPY has >=2 tokens; the last is the destination.
        for src in parts[:-1]:
            out.append(src)
    return out


def main() -> int:
    di = ROOT / ".dockerignore"
    if not di.is_file():
        print("no .dockerignore", file=sys.stderr)
        return 0
    ignored = make_matcher(load_patterns(di))

    dockerfiles = sorted(ROOT.glob("**/Dockerfile*"))
    if not dockerfiles:
        print("no Dockerfiles found", file=sys.stderr)
        return 0

    failures = []
    for df in dockerfiles:
        for src in copy_sources(df):
            if ignored(src):
                failures.append((df.relative_to(ROOT), src))

    # .env must never reach the daemon.
    assert ignored(".env"), ".env must be excluded from the build context"

    if failures:
        print("EXCLUDED COPY SOURCES (build would break):")
        for df, src in failures:
            print(f"  {df}: {src}")
        return 1

    print(f"OK: all COPY sources reachable in {len(dockerfiles)} Dockerfiles; .env excluded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())