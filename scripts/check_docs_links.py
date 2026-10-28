#!/usr/bin/env python3
"""Docs link + claim checker for Scopewatch (issue #46).

Verifies current documentation and deployment files without modifying them:
- relative markdown links and backticked file refs resolve on disk
- no localhost URLs in committed docs except explicit local-demo allowlist
- no invented model IDs (must appear in backend/config/providers.toml)
- no secrets (API keys, tokens, private keys) in docs or deploy/ (read-only)
- no unqualified guarantee claims outside the gateway

Usage:
  python3 scripts/check_docs_links.py
  python3 scripts/check_docs_links.py --root /path/to/repo

Exit 0 when all checks pass, 1 with a failure list otherwise.
Documented in BUILDING.md and docs/operations/judge-runbook.md.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
from urllib.parse import unquote

# Current documentation covered by the checker.
OWNED_DOCS = [
    "README.md",
    "BUILDING.md",
    "docs/hackathon.md",
    "docs/operations/judge-runbook.md",
    "docs/ARCHITECTURE.md",
    "docs/evaluation.md",
    "docs/README.md",
]

# Submission documentation is checked with the same rules.
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
PLACEHOLDERS = ["<HOSTED_URL>", "<YOUTUBE_URL>", "<DOMAIN>", "<TOKEN>"]

# Stale claims the submission docs once made (issue #217). The third field
# marks patterns whose honest historical explanation may stay: a match is
# skipped only when its own line describes the historical credential problem.
BANNED_STALE_CLAIMS = [
    (r"\b70B\b", "stale 70B model claim (configured model is Nemotron 3.5 Lightning)", False),
    (r"\b287\s+(?:backend\s+)?(?:tests?|passed|collected)\b", "stale 287 test count claim", False),
    (r"<REPO_URL>", "unexpanded <REPO_URL> placeholder (repo is public)", False),
    (r"no [`']deploy/`", "stale 'no deploy/ directory' claim", False),
    (r"(?:returned|returns)\s+HTTP\s+401|HTTP\s+401[^\n.]*unverified|unverified[^\n.]*HTTP\s+401", "stale HTTP 401 unverified claim (resolved with Token Factory key)", True),
]
HISTORY_WORDS = ("earlier", "diagnosed", "resolved", "wrong credential", "replacement")


def check_stale_claims(root: pathlib.Path, rel: str, errors: list[str]) -> None:
    path = root / rel
    if not path.exists():
        return
    text = re.sub(r"https?://[^\s<>]+", "", path.read_text(encoding="utf-8"))
    for pat, label, history_ok in BANNED_STALE_CLAIMS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            if history_ok:
                start = text.rfind("\n", 0, m.start()) + 1
                end = text.find("\n", m.end())
                window = text[start:end if end != -1 else len(text)].lower()
                if any(w in window for w in HISTORY_WORDS):
                    continue
            errors.append(f"{rel}: {label}: '{m.group(0)}'")


# Banned guarantee phrases outside the gateway (AGENTS.md hard line 5).
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
    """Extract inline Markdown local paths, including angle-wrapped paths."""
    targets: list[str] = []
    for match in re.finditer(r"\[[^\]]*\]\(([^)]*)\)", text):
        raw = match.group(1).strip()
        if not raw:
            continue  # Empty links refer to the current document.
        if re.fullmatch(r"<[A-Z_]+>", raw):
            continue
        target = raw[1:raw.index(">")] if raw.startswith("<") and ">" in raw else raw.split()[0]
        if re.match(r"(?:https?://|mailto:|#)", target):
            continue
        if target in PLACEHOLDERS or re.search(r"<[A-Z_]+>", target):
            continue
        target = unquote(target.split("#", 1)[0].split("?", 1)[0])
        if target:
            targets.append(target)
    return targets


def extract_backticked_refs(text: str) -> list[str]:
    """Backticked `path` or `path:line` refs that look like repo files."""
    refs: list[str] = []
    for m in re.finditer(r"`([^`]+)`", text):
        inner = m.group(1).strip()
        # Take first token (handles `path:line` + trailing prose).
        token = inner.split()[0] if inner else ""
        token = token.rstrip(".,;:").split("::", 1)[0]
        if re.search(r"\.(py|json|md|toml|html|js|css|yaml|yml|sh)(:\d+(-\d+)?)?$", token):
            # Skip placeholders and URLs.
            if "<" in token or token.startswith("http"):
                continue
            refs.append(token)
    return refs


ROOT_RELATIVE_PREFIXES = ("deploy/", "docs/", "backend/", "frontend/", "demo/", "scripts/",
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
        errors.append(f"{rel}: file itself missing")
        return
    text = path.read_text(encoding="utf-8")
    file_dir = path.parent
    link_targets = extract_link_targets(text)
    for target in link_targets + extract_backticked_refs(text):
        # Strip :line suffix for existence check.
        file_part = re.split(r":\d+", target.split("::", 1)[0])[0]
        # Skip bare filenames without a slash (shorthand, e.g. `06_invoice_injection.json`).
        if target not in link_targets and "/" not in file_part and not file_part.startswith("."):
            continue
        # Glob patterns (e.g. `demo/scenarios/10_*.json`) resolve against disk via glob.
        if "*" in file_part:
            matches = list(root.glob(file_part)) or list(file_dir.glob(file_part))
            if matches:
                continue
            errors.append(f"{rel}: broken glob ref: {target}")
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
        line = re.sub(r"https?://[^\s<>]+", "", line)
        for m in re.finditer(r"(nvidia/[A-Za-z0-9\-\._/]+|meta-llama/[A-Za-z0-9\-\._/]+)", line):
            mid = m.group(1).rstrip(".,;:`'\"")
            if mid not in allowed:
                errors.append(f"{rel}:{i}: invented model ID not in providers.toml: {mid}")
        # Bare mock IDs other than the configured one are suspect.
        for m in re.finditer(r"mock-[A-Za-z0-9\-_]+", line):
            mid = m.group(0)
            # Prose descriptors like "mock-agent", "mock-profile", etc. are not model IDs.
            if mid in {"mock-agent", "mock-profile", "mock-mode", "mock-transport", "mock-rules"}:
                continue
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
            errors.append(f"{rel}: possible secret ({label}); value redacted")
            break


def check_secrets_owned(root: pathlib.Path, rel: str, errors: list[str]) -> None:
    path = root / rel
    if not path.exists():
        return
    check_secrets_in_text(rel, path.read_text(encoding="utf-8"), errors)


def check_deploy_dir(root: pathlib.Path, errors: list[str]) -> None:
    """Inspect deployment source on disk; never read local credentials or caches."""
    deploy_dir = root / "deploy"
    if not deploy_dir.exists():
        return
    for path in sorted(deploy_dir.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.name.startswith("test_"):
            continue
        if any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(deploy_dir).parts):
            if path != deploy_dir / ".env.example":
                continue
        if path.suffix not in {".md", ".yaml", ".yml", ".sh", ".py", ".toml", ".example"} and path.name not in {"Dockerfile", "Caddyfile"}:
            continue
        rel = path.relative_to(root).as_posix()
        content = path.read_text(encoding="utf-8")
        check_secrets_in_text(rel, content, errors)
        # Socket mounts must never appear outside the dedicated runner.
        if not path.is_relative_to(deploy_dir / "executor-runner"):
            if re.search(r"docker\.sock\s*:", content):  # assert absence of mounts
                errors.append(f"{rel}: unexpected docker.sock mount outside runner; must not be present")


def main() -> int:
    ap = argparse.ArgumentParser(description="Scopewatch docs link/claim checker (#46).")
    ap.add_argument("--root", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parent.parent)
    ap.add_argument("--skip-deploy", action="store_true", help="skip the read-only deploy/ check")
    args = ap.parse_args()
    root = args.root.resolve()

    errors: list[str] = []
    allowed = load_allowed_models(root)
    print(f"Allowed models from providers.toml: {sorted(allowed)}")

    docs_to_check = [d for d in OWNED_DOCS if (root / d).exists()]
    # Include submission docs when present.
    for d in SUBMISSION_DOCS:
        if (root / d).exists():
            docs_to_check.append(d)

    for rel in docs_to_check:
        check_file_links(root, rel, errors)
        check_localhost_urls(root, rel, errors)
        check_model_ids(root, rel, allowed, errors)
        check_banned_claims(root, rel, errors)
        check_stale_claims(root, rel, errors)
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
                    # DOMAIN/DEMO_TOKEN live in deploy/.env.example, not root.
                    # Allow them only as placeholders or with an explicit deploy pointer.
                    if var in ("DOMAIN", "DEMO_TOKEN") and "deploy/.env.example" in text:
                        continue
                    errors.append(f"{rel}: env var {var} not in .env.example")

    check_stale_claims(root, "backend/config/providers.toml", errors)

    if not args.skip_deploy:
        check_deploy_dir(root, errors)

    if errors:
        print(f"\nFAIL: {len(errors)} problem(s):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("\nPASS: docs links, model IDs, URLs, claims, and secrets all clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
