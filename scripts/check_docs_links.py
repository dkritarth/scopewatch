#!/usr/bin/env python3
"""Docs link + claim checker for Scopewatch (issue #46).

Verifies writable docs without touching deploy/ or docs/submission/:
- relative markdown links and backticked file refs resolve on disk
- no localhost URLs in committed docs except explicit local-demo allowlist
- no invented model IDs (must appear in backend/config/providers.toml)
- no secrets (API keys, tokens, private keys) in docs or deploy/ (read-only)
- no unqualified guarantee claims outside the gateway

Usage:
  python3 scripts/check_docs_links.py
  python3 scripts/check_docs_links.py --root /path/to/repo
  python3 scripts/check_docs_links.py --deploy-ref origin/feature/42-vm-deploy

Exit 0 when all checks pass, 1 with a failure list otherwise.
Documented in BUILDING.md and docs/operations/judge-runbook.md.
"""

from __future__ import annotations

import argparse
import base64
import pathlib
import re
import subprocess
import sys

# Docs this checker owns on main. docs/submission/* is read-only:
# PR #72 owns it, so we verify consistency but never edit it here.
OWNED_DOCS = [
    "README.md",
    "BUILDING.md",
    "docs/hackathon.md",
    "docs/operations/judge-runbook.md",
    "docs/ARCHITECTURE.md",
    "docs/evaluation.md",
    "docs/README.md",
]

# Read-only consistency check against PR #72 drafts (never edited here).
SUBMISSION_DOCS = [
    "docs/submission/demo-script.md",
    "docs/submission/description.md",
    "docs/submission/nebius-nvidia.md",
    "docs/submission/testing-instructions.md",
]

# Local-demo URLs that are explicitly allowed (judge runs on their own machine).
LOCAL_DEMO_ALLOWLIST = [
    "http://127.0.0.1:8000",
    "http://localhost:8000",
]

# Placeholders a human must fill (issue #43/#45/#47) — allowed, never flagged.
PLACEHOLDERS = ["<HOSTED_URL>", "<YOUTUBE_URL>", "<REPO_URL>", "<DOMAIN>", "<TOKEN>"]

# Banned guarantee phrases outside the gateway (claim honesty, AGENTS.md hard line 5).
BANNED_CLAIMS = [
    "guaranteed detection",
    "guaranteed prevention",
    "guarantees detection",
    "guarantees prevention",
    "100% secure",
    "unbreakable",
    "military-grade",
    "never fails",
    "always prevents",
]

# Secret patterns (synthetic only in repo; real keys come from env/.env, gitignored).
# NOTE: env-value patterns use [ ]* (same line only) so empty assignments
# like `NEBIUS_API_KEY=\nOPENROUTER_API_KEY=` never match across lines.
SECRET_PATTERNS = [
    (r"sk-or-[A-Za-z0-9]{8,}", "OpenRouter-style key"),
    (r"sk-[A-Za-z0-9]{16,}", "sk- API key"),
    (r"NEBIUS_API_KEY[ ]*=[ ]*['\"]?[A-Za-z0-9\-_\.]{12,}", "NEBIUS_API_KEY with value"),
    (r"OPENROUTER_API_KEY[ ]*=[ ]*['\"]?sk-", "OPENROUTER_API_KEY with value"),
    (r"DEMO_TOKEN[ ]*=[ ]*['\"]?[A-Za-z0-9\-_\.]{16,}", "DEMO_TOKEN with real value"),
    (r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", "private key block"),
    (r"xox[bap]-", "Slack token"),
    (r"ghp_[A-Za-z0-9]{20,}", "GitHub token"),
]

# Values that are obviously placeholders, never real secrets.
PLACEHOLDER_VALUES = ("...", "…", "change-me", "EXAMPLE", "xxx", "<", "sk-or-...")


def load_allowed_models(root: pathlib.Path) -> set[str]:
    """Model IDs configured in provider profiles (core code never hard-codes them)."""
    models: set[str] = set()
    toml_path = root / "backend" / "config" / "providers.toml"
    if toml_path.exists():
        for line in toml_path.read_text(encoding="utf-8").splitlines():
            m = re.search(r'model\s*=\s*"([^"]+)"', line)
            if m:
                models.add(m.group(1).strip())
    models.add("mock-rules-auditor")
    return models


def extract_link_targets(text: str) -> list[str]:
    """Markdown [text](target) link targets (skip http, #, <placeholder>)."""
    targets: list[str] = []
    for m in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", text):
        t = m.group(1).strip().split()[0].split("#")[0].strip()
        if not t or t.startswith(("http://", "https://", "mailto:", "#", "<")):
            continue
        # Strip query strings for local paths.
        t = t.split("?")[0]
        if t:
            targets.append(t)
    return targets


def extract_backticked_refs(text: str) -> list[str]:
    """Backticked `path` or `path:line` refs that look like repo files."""
    refs: list[str] = []
    for m in re.finditer(r"`([^`]+)`", text):
        inner = m.group(1).strip()
        # Take first token (handles `path:line` + trailing prose).
        token = inner.split()[0] if inner else ""
        token = token.rstrip(".,;:")
        if re.search(r"\.(py|json|md|toml|html|js|css|yaml|yml|sh)(:\d+(-\d+)?)?$", token):
            # Skip placeholders and URLs.
            if "<" in token or token.startswith("http"):
                continue
            refs.append(token)
    return refs


ROOT_RELATIVE_PREFIXES = ("docs/", "backend/", "frontend/", "demo/", "scripts/",
                            "poc/", ".agents/", ".github/", "LICENSE", "README.md",
                            "BUILDING.md", "AGENTS.md")


def resolve_doc_ref(root: pathlib.Path, file_dir: pathlib.Path, file_part: str) -> pathlib.Path | None:
    """Resolve a doc link: try file-dir-relative, then repo-root-relative.

    This repo writes root-relative links (e.g. `docs/adr/...` inside
    docs/hackathon.md), so a dir-relative-only check false-positives.
    """
    if file_part.startswith("/"):
        return None
    # Repo convention: `./scripts/...` means root-relative, not dir-relative.
    if file_part.startswith("./"):
        return (root / file_part[2:]).resolve()
    # Explicit ../ stays dir-relative.
    if file_part.startswith("../"):
        return (file_dir / file_part).resolve()
    # Explicit ./ or ../ or bare dir-relative.
    dir_candidate = (file_dir / file_part).resolve()
    try:
        dir_candidate.relative_to(root.resolve())
        if dir_candidate.exists():
            return dir_candidate
    except ValueError:
        pass
    # Root-relative (repo convention for docs/ backend/ frontend/ demo/ scripts/).
    if file_part.split("/")[0] + "/" in ROOT_RELATIVE_PREFIXES or file_part in ROOT_RELATIVE_PREFIXES:
        return (root / file_part).resolve()
    return dir_candidate


def check_file_links(root: pathlib.Path, rel: str, errors: list[str]) -> None:
    path = root / rel
    if not path.exists():
        # judge-runbook is NEW in this PR; missing before patch is expected.
        # Report as info, not failure, when running on base.
        if rel == "docs/operations/judge-runbook.md":
            print(f"INFO: {rel} not present (added by #46 close-out).")
            return
        errors.append(f"{rel}: file itself missing")
        return
    text = path.read_text(encoding="utf-8")
    file_dir = path.parent
    for target in extract_link_targets(text) + extract_backticked_refs(text):
        # Strip :line suffix for existence check.
        file_part = re.split(r":\d+", target)[0]
        # Skip bare filenames without a slash (shorthand, e.g. `06_invoice_injection.json`).
        if "/" not in file_part and not file_part.startswith("."):
            continue
        candidate = resolve_doc_ref(root, file_dir, file_part)
        if candidate is None:
            continue
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            errors.append(f"{rel}: link escapes repo: {target}")
            continue
        if not candidate.exists():
            # docs/submission/* does not exist on main until PR #72 merges;
            # references to it from owned docs are forward pointers, not breakage.
            if str(file_part).startswith("docs/submission/") or "docs/submission/" in str(file_part):
                print(f"INFO: {rel}: forward pointer to {target} (PR #72, not on main yet).")
                continue
            # deploy/* does not exist on main until PR #79 merges; same treatment.
            if str(file_part).startswith("deploy/") or str(file_part).startswith("./deploy/"):
                print(f"INFO: {rel}: forward pointer to {target} (PR #79, not on main yet).")
                continue
            errors.append(f"{rel}: broken ref: {target}")


def check_localhost_urls(root: pathlib.Path, rel: str, errors: list[str]) -> None:
    path = root / rel
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines(), start=1):
        if any(ph in line for ph in PLACEHOLDERS):
            continue
        for m in re.finditer(r"https?://(?:localhost|127\.0\.0\.1)(?::\d+)?(?:/[^\s)]*)?", line):
            url = m.group(0)
            if any(url.startswith(a) for a in LOCAL_DEMO_ALLOWLIST):
                continue
            errors.append(f"{rel}:{i}: localhost URL outside local-demo allowlist: {url}")


def check_model_ids(root: pathlib.Path, rel: str, allowed: set[str], errors: list[str]) -> None:
    path = root / rel
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    # nvidia/, meta-llama/, openai/ style IDs must be configured or explicitly provisional.
    for i, line in enumerate(text.splitlines(), start=1):
        for m in re.finditer(r"(nvidia/[A-Za-z0-9\-\._/]+|meta-llama/[A-Za-z0-9\-\._/]+)", line):
            mid = m.group(1).rstrip(".,;:`'\"")
            if mid not in allowed:
                errors.append(f"{rel}:{i}: invented model ID not in providers.toml: {mid}")
        # Bare mock IDs other than the configured one are suspect.
        for m in re.finditer(r"mock-[A-Za-z0-9\-_]+", line):
            mid = m.group(0)
            if mid not in allowed:
                errors.append(f"{rel}:{i}: unknown mock model ID: {mid}")


def check_banned_claims(root: pathlib.Path, rel: str, errors: list[str]) -> None:
    path = root / rel
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8").lower()
    for phrase in BANNED_CLAIMS:
        if phrase in text:
            # Allow the sentence that denies the guarantee (honest scope statement).
            # Flag only positive claims. Heuristic: flag unless "not", "no", or
            # "never claim" appears within 80 chars before the phrase.
            for m in re.finditer(re.escape(phrase), text):
                window = text[max(0, m.start() - 80): m.start()]
                if re.search(r"\b(not|no|never|without|does not|do not)\b", window):
                    continue
                errors.append(f"{rel}: unqualified guarantee claim: '{phrase}'")
                break


def check_secrets_in_text(rel: str, text: str, errors: list[str]) -> None:
    for pattern, label in SECRET_PATTERNS:
        for m in re.finditer(pattern, text):
            matched = m.group(0)
            # Placeholder values (..., change-me, <...>, EXAMPLE) are never real secrets.
            if any(pv in matched for pv in PLACEHOLDER_VALUES):
                continue
            context = text[max(0, m.start() - 40): m.end() + 10]
            if any(pv in context for pv in ("change-me", "EXAMPLE", "...", "<HOSTED", "<REPO", "<TOKEN")):
                continue
            snippet = matched[:40]
            if snippet in ("NEBIUS_API_KEY=", "OPENROUTER_API_KEY=", "DEMO_TOKEN="):
                continue
            # Empty assignments (KEY= with nothing after) are safe.
            if re.fullmatch(r"[A-Z_]+=\s*", matched):
                continue
            errors.append(f"{rel}: possible secret ({label}): {snippet}...")
            break


def check_secrets_owned(root: pathlib.Path, rel: str, errors: list[str]) -> None:
    path = root / rel
    if not path.exists():
        return
    check_secrets_in_text(rel, path.read_text(encoding="utf-8"), errors)


def check_deploy_ref(root: pathlib.Path, ref: str, errors: list[str]) -> None:
    """Read-only: grep deploy/ at <ref> for secrets and absent docker.sock mounts.

    NEVER edits deploy/ (owned by PR #79). Runs `git show <ref>:<path>`.
    """
    try:
        files_out = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", ref, "deploy/"],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        print(f"INFO: deploy check skipped (git error: {exc}).")
        return
    if files_out.returncode != 0:
        print(f"INFO: deploy ref {ref} not available locally; run `git fetch origin` first.")
        return
    files = [f for f in files_out.stdout.splitlines() if f.strip()]
    if not files:
        print(f"INFO: no deploy/ files at {ref} yet.")
        return
    print(f"Checking {len(files)} deploy/ files at {ref} (read-only)...")
    for f in files:
        show = subprocess.run(
            ["git", "show", f"{ref}:{f}"],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        if show.returncode != 0:
            continue
        content = show.stdout
        # Secrets: same patterns, but allow change-me placeholders and empty values.
        tmp: list[str] = []
        check_secrets_in_text(f"[{ref}]{f}", content, tmp)
        # Filter known-safe placeholders committed in .env.example.
        tmp = [e for e in tmp if "change-me" not in e and "demo.example.com" not in e]
        errors.extend(tmp)
        # docker.sock must not be mounted (issue #42 requirement).
        if "docker.sock" in content and "explanatory" not in content.lower():  # absence check
            # RUNBOOK.md legitimately discusses absent docker.sock in prose ("contain zero
            # ... mounts (verify: grep ... returns nothing ...)"). Only flag actual
            # mount syntax.
            if re.search(r"volumes:\s*\n.*docker\.sock|/var/run/docker\.sock\s*:", content):
                errors.append(f"[{ref}]{f}: unexpected docker.sock mount found; socket mounts must never be present (issue #42 forbids it)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Scopewatch docs link/claim checker (#46).")
    ap.add_argument("--root", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parent.parent)
    ap.add_argument("--deploy-ref", default="origin/feature/42-vm-deploy",
                    help="git ref for read-only deploy/ secret check (default: %(default)s)")
    ap.add_argument("--skip-deploy", action="store_true", help="skip the read-only deploy/ check")
    args = ap.parse_args()
    root = args.root.resolve()

    errors: list[str] = []
    allowed = load_allowed_models(root)
    print(f"Allowed models from providers.toml: {sorted(allowed)}")

    docs_to_check = [d for d in OWNED_DOCS if (root / d).exists()]
    # Also consistency-read PR #72 drafts if present (never edited here).
    for d in SUBMISSION_DOCS:
        if (root / d).exists():
            docs_to_check.append(d)

    for rel in docs_to_check:
        check_file_links(root, rel, errors)
        check_localhost_urls(root, rel, errors)
        check_model_ids(root, rel, allowed, errors)
        check_banned_claims(root, rel, errors)
        check_secrets_owned(root, rel, errors)

    # README + BUILDING + judge-runbook must document the same env names as .env.example.
    env_example = root / ".env.example"
    if env_example.exists():
        example_vars = set(re.findall(r"^([A-Z_]+)=", env_example.read_text(), re.M))
        for rel in ["README.md", "BUILDING.md", "docs/operations/judge-runbook.md"]:
            p = root / rel
            if not p.exists():
                continue
            text = p.read_text()
            for var in re.findall(r"\b(SCOPEWATCH_[A-Z_]+|NEBIUS_API_KEY|OPENROUTER_API_KEY|DEMO_TOKEN|DOMAIN)\b", text):
                if var not in example_vars and var not in ("DOMAIN", "DEMO_TOKEN"):
                    # DOMAIN/DEMO_TOKEN live in deploy/.env.example (PR #79), not root.
                    # Allow them only as placeholders or with an explicit deploy pointer.
                    if var in ("DOMAIN", "DEMO_TOKEN") and "deploy/.env.example" in text:
                        continue
                    errors.append(f"{rel}: env var {var} not in .env.example")

    if not args.skip_deploy:
        check_deploy_ref(root, args.deploy_ref, errors)

    if errors:
        print(f"\nFAIL: {len(errors)} problem(s):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("\nPASS: docs links, model IDs, URLs, claims, and secrets all clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
