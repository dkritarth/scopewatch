#!/usr/bin/env python3
"""Synthetic provider probe for Nebius Token Factory and OpenRouter.

Verifies:
1. Basic chat completion connectivity.
2. Raw reasoning extraction (reasoning_content, reasoning, or <think> tags).
3. Tool calling combined with reasoning.
4. Structured JSON output for the scope auditor.

Safety invariants:
- Never prints, logs, or persists API keys.
- Sends strictly synthetic, harmless prompts and tool schemas.
- Includes a --mock mode for offline validation without live API credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional


SYNTHETIC_TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read contents of an approved synthetic invoice file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path to file, e.g. invoices/vendor_a.txt",
                    }
                },
                "required": ["path"],
            },
        },
    }
]

AUDITOR_SCHEMA_EXAMPLE = {
    "type": "json_object"
}


@dataclass
class ProbeResult:
    provider: str
    model: str
    probe_name: str
    status: str
    latency_ms: float
    has_reasoning: bool
    reasoning_field: Optional[str]
    has_tool_calls: bool
    is_valid_json: bool
    error: Optional[str] = None


def make_request(
    url: str,
    api_key: str,
    payload: Dict[str, Any],
    extra_headers: Optional[Dict[str, str]] = None,
    timeout: int = 45,
) -> Dict[str, Any]:
    """Execute an HTTP POST request to an OpenAI-compatible endpoint."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        return json.loads(body.decode("utf-8"))


def run_mock_probes(provider: str, model: str) -> List[ProbeResult]:
    """Simulate probe responses for local verification without external network or keys."""
    results = []

    # 1. Basic reasoning probe
    results.append(
        ProbeResult(
            provider=provider,
            model=model,
            probe_name="basic_reasoning",
            status="SUCCESS",
            latency_ms=120.5,
            has_reasoning=True,
            reasoning_field="reasoning_content",
            has_tool_calls=False,
            is_valid_json=False,
        )
    )

    # 2. Tool calling with reasoning probe
    results.append(
        ProbeResult(
            provider=provider,
            model=model,
            probe_name="tool_calling_with_reasoning",
            status="SUCCESS",
            latency_ms=180.2,
            has_reasoning=True,
            reasoning_field="reasoning_content",
            has_tool_calls=True,
            is_valid_json=False,
        )
    )

    # 3. Structured JSON output probe
    results.append(
        ProbeResult(
            provider=provider,
            model=model,
            probe_name="structured_json_auditor",
            status="SUCCESS",
            latency_ms=145.0,
            has_reasoning=False,
            reasoning_field=None,
            has_tool_calls=False,
            is_valid_json=True,
        )
    )

    return results


def run_live_probes(
    provider: str,
    base_url: str,
    model: str,
    api_key: str,
    extra_headers: Optional[Dict[str, str]] = None,
) -> List[ProbeResult]:
    """Run real HTTP probes against an OpenAI-compatible endpoint."""
    results = []
    endpoint = f"{base_url.rstrip('/')}/chat/completions"

    # Probe 1: Basic completion with reasoning inspection
    t0 = time.perf_counter()
    p1_payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": "Solve: A synthetic invoice lists 3 items at $12 each. What is the total? Think step by step.",
            }
        ],
        "max_tokens": 512,
        "temperature": 0.0,
    }
    try:
        data = make_request(endpoint, api_key, p1_payload, extra_headers)
        latency = (time.perf_counter() - t0) * 1000
        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})

        reasoning_field = None
        has_reasoning = False
        if "reasoning_content" in msg and msg["reasoning_content"]:
            has_reasoning = True
            reasoning_field = "reasoning_content"
        elif "reasoning" in msg and msg["reasoning"]:
            has_reasoning = True
            reasoning_field = "reasoning"
        elif "<think>" in msg.get("content", ""):
            has_reasoning = True
            reasoning_field = "<think>_tag_in_content"

        results.append(
            ProbeResult(
                provider=provider,
                model=model,
                probe_name="basic_reasoning",
                status="SUCCESS",
                latency_ms=latency,
                has_reasoning=has_reasoning,
                reasoning_field=reasoning_field,
                has_tool_calls=False,
                is_valid_json=False,
            )
        )
    except Exception as e:
        latency = (time.perf_counter() - t0) * 1000
        results.append(
            ProbeResult(
                provider=provider,
                model=model,
                probe_name="basic_reasoning",
                status="FAILED",
                latency_ms=latency,
                has_reasoning=False,
                reasoning_field=None,
                has_tool_calls=False,
                is_valid_json=False,
                error=str(e),
            )
        )

    # Probe 2: Tool calling with reasoning
    t0 = time.perf_counter()
    p2_payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are an automated invoice assistant. When requested to read a file, call read_file. Reason before acting.",
            },
            {
                "role": "user",
                "content": "Please read invoices/vendor_a.txt to verify invoice status.",
            },
        ],
        "tools": SYNTHETIC_TOOL_SCHEMA,
        "tool_choice": "auto",
        "max_tokens": 512,
        "temperature": 0.0,
    }
    try:
        data = make_request(endpoint, api_key, p2_payload, extra_headers)
        latency = (time.perf_counter() - t0) * 1000
        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})

        has_tool_calls = bool(msg.get("tool_calls"))
        reasoning_field = None
        has_reasoning = False
        if "reasoning_content" in msg and msg["reasoning_content"]:
            has_reasoning = True
            reasoning_field = "reasoning_content"
        elif "reasoning" in msg and msg["reasoning"]:
            has_reasoning = True
            reasoning_field = "reasoning"
        elif "<think>" in (msg.get("content") or ""):
            has_reasoning = True
            reasoning_field = "<think>_tag_in_content"

        results.append(
            ProbeResult(
                provider=provider,
                model=model,
                probe_name="tool_calling_with_reasoning",
                status="SUCCESS",
                latency_ms=latency,
                has_reasoning=has_reasoning,
                reasoning_field=reasoning_field,
                has_tool_calls=has_tool_calls,
                is_valid_json=False,
            )
        )
    except Exception as e:
        latency = (time.perf_counter() - t0) * 1000
        results.append(
            ProbeResult(
                provider=provider,
                model=model,
                probe_name="tool_calling_with_reasoning",
                status="FAILED",
                latency_ms=latency,
                has_reasoning=False,
                reasoning_field=None,
                has_tool_calls=False,
                is_valid_json=False,
                error=str(e),
            )
        )

    # Probe 3: Structured JSON Output (Auditor mode)
    t0 = time.perf_counter()
    p3_payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "Assess synthetic scope. Respond ONLY with a JSON object containing keys: status, confidence, reason.",
            },
            {
                "role": "user",
                "content": "Action: read_file('invoices/vendor_a.txt'). Scope: inspect approved vendor invoices.",
            },
        ],
        "response_format": AUDITOR_SCHEMA_EXAMPLE,
        "max_tokens": 256,
        "temperature": 0.0,
    }
    try:
        data = make_request(endpoint, api_key, p3_payload, extra_headers)
        latency = (time.perf_counter() - t0) * 1000
        choice = data.get("choices", [{}])[0]
        content = choice.get("message", {}).get("content", "")

        is_valid = False
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict) and "status" in parsed:
                is_valid = True
        except Exception:
            is_valid = False

        results.append(
            ProbeResult(
                provider=provider,
                model=model,
                probe_name="structured_json_auditor",
                status="SUCCESS" if is_valid else "MALFORMED_OUTPUT",
                latency_ms=latency,
                has_reasoning=False,
                reasoning_field=None,
                has_tool_calls=False,
                is_valid_json=is_valid,
            )
        )
    except Exception as e:
        latency = (time.perf_counter() - t0) * 1000
        results.append(
            ProbeResult(
                provider=provider,
                model=model,
                probe_name="structured_json_auditor",
                status="FAILED",
                latency_ms=latency,
                has_reasoning=False,
                reasoning_field=None,
                has_tool_calls=False,
                is_valid_json=False,
                error=str(e),
            )
        )

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Synthetic Provider Probe for Scopewatch")
    parser.add_argument(
        "--provider",
        choices=["nebius", "openrouter", "all"],
        default="all",
        help="Provider to probe",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Run offline mock probe suite without calling remote APIs",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("Scopewatch Provider Probe")
    print("Verifying Nemotron reasoning, tool calls, and structured output")
    print("=" * 60)

    nebius_key = os.getenv("NEBIUS_API_KEY", "").strip()
    openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip()

    all_results: List[ProbeResult] = []

    if args.mock:
        print("[MOCK MODE] Simulating probe execution for both providers...")
        if args.provider in ("nebius", "all"):
            all_results.extend(run_mock_probes("nebius", "nvidia/llama-3.1-nemotron-70b-instruct"))
        if args.provider in ("openrouter", "all"):
            all_results.extend(run_mock_probes("openrouter", "nvidia/llama-3.1-nemotron-70b-instruct"))
    else:
        # Check keys
        if args.provider in ("nebius", "all"):
            if not nebius_key:
                print("[SKIP] Nebius Token Factory: NEBIUS_API_KEY not set in environment.")
            else:
                print("[PROBE] Running Nebius Token Factory probes...")
                nebius_results = run_live_probes(
                    provider="nebius",
                    base_url="https://api.tokenfactory.nebius.com/v1",
                    model="nvidia/llama-3.1-nemotron-70b-instruct",
                    api_key=nebius_key,
                )
                all_results.extend(nebius_results)

        if args.provider in ("openrouter", "all"):
            if not openrouter_key:
                print("[SKIP] OpenRouter: OPENROUTER_API_KEY not set in environment.")
            else:
                print("[PROBE] Running OpenRouter probes...")
                openrouter_results = run_live_probes(
                    provider="openrouter",
                    base_url="https://openrouter.ai/api/v1",
                    model="nvidia/llama-3.1-nemotron-70b-instruct",
                    api_key=openrouter_key,
                    extra_headers={
                        "HTTP-Referer": "https://github.com/dkritarth/scopewatch",
                        "X-Title": "Scopewatch Synthetic Probe",
                    },
                )
                all_results.extend(openrouter_results)

    if not all_results:
        print("\nNo probes executed. To run offline, pass --mock. To run live, set NEBIUS_API_KEY or OPENROUTER_API_KEY.")
        return 0

    print("\nProbe Results:")
    print("-" * 60)
    for r in all_results:
        print(f"[{r.provider}] {r.probe_name} ({r.model}):")
        print(f"  Status: {r.status} | Latency: {r.latency_ms:.1f}ms")
        print(f"  Reasoning detected: {r.has_reasoning} (field: {r.reasoning_field})")
        print(f"  Tool calls: {r.has_tool_calls} | Valid JSON: {r.is_valid_json}")
        if r.error:
            print(f"  Error: {r.error}")
        print()

    print("Probe run completed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
