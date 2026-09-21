#!/usr/bin/env python3
"""Run a bounded live probe against OpenRouter's Union Alpha model.

The script records only model IDs, provider IDs, response structure, usage,
latency, and pass/fail outcomes. It never writes prompts, completions, API
keys, or raw model output to disk.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "stealth/union-alpha"
DEFAULT_TIMEOUT = 90
MAX_RESPONSE_BYTES = 1_048_576


@dataclass(frozen=True)
class Probe:
    name: str
    prompt: str
    max_tokens: int
    temperature: float
    reasoning_enabled: Optional[bool]


PROBES = [
    Probe("math-baseline", "What is 17 * 23? Give only the number.", 512, 0.0, False),
    Probe("math-reasoning-enabled", "What is 17 * 23? Give only the number.", 512, 0.0, True),
    Probe("logic-reasoning-enabled", "Solve: all roses are flowers, some flowers fade. Is it valid that some roses fade? Explain briefly.", 1024, 0.0, True),
    Probe("problem-solving", "A farmer has 17 sheep and all but 9 run away. How many are left? Give only the number.", 512, 0.0, True),
    Probe("number-theory", "Find the smallest positive integer equal to 2 mod 3, 3 mod 5, and 2 mod 7. Give only the number.", 1024, 0.0, True),
    Probe("planning", "Plan three steps to rename every `foo.py` in one directory to `bar.py`. Keep it brief.", 1024, 0.2, True),
    Probe("summarization", "Summarize in one sentence: Files store data. Directories group files. Paths name them.", 512, 0.2, True),
]


@dataclass
class Result:
    name: str
    ok: bool
    model: Optional[str] = None
    provider: Optional[str] = None
    finish_reason: Optional[str] = None
    message_content_type: Optional[str] = None
    content_present: Optional[bool] = None
    reasoning_present: Optional[bool] = None
    reasoning_nonempty: Optional[bool] = None
    captured_trace_type: Optional[str] = None
    reasoning_tokens: Optional[int] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    reported_cost: Optional[float] = None
    latency_seconds: Optional[float] = None
    top_level_keys: Optional[str] = None
    message_keys: Optional[str] = None
    status_code: Optional[int] = None
    error_type: Optional[str] = None


def read_api_key() -> str:
    value = os.environ.get("OPENROUTER_API_KEY")
    if value is not None:
        if not value.strip():
            raise RuntimeError("OPENROUTER_API_KEY is empty")
        return value.strip()

    key_path = Path(os.path.expanduser("~/.config/openrouter/api_key"))
    if not key_path.is_file():
        raise RuntimeError("OPENROUTER_API_KEY is unset and fallback key file is missing")
    value = key_path.read_text().strip()
    if not value:
        raise RuntimeError("Fallback API key file is empty")
    return value


def request_payload(probe: Probe, allow_fallbacks: bool) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": MODEL,
        "messages": [{"role": "user", "content": probe.prompt}],
        "max_tokens": probe.max_tokens,
        "temperature": probe.temperature,
        "provider": {"allow_fallbacks": allow_fallbacks},
    }
    if probe.reasoning_enabled is not None:
        payload["reasoning"] = {"enabled": probe.reasoning_enabled}
    return payload


def http_post(url: str, payload: Dict[str, Any], api_key: str, timeout: int) -> tuple[int, Dict[str, Any]]:
    encoded = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=encoded,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/dkritarth/scopewatch",
            "X-Title": "Scopewatch Union Alpha Probe",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("response exceeds size limit")
            return response.status, json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as error:
        error.close()
        raise RuntimeError(f"HTTP {error.code}") from None


def inspect_response(name: str, started: float, response: Dict[str, Any]) -> Result:
    from src.cot_capture import OpenRouterReasoningCapture

    if not isinstance(response, dict):
        raise ValueError("response is not an object")
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("response has no choices")

    choice = choices[0]
    if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
        raise ValueError("response has no message")
    message = choice["message"]
    usage = response.get("usage") or {}
    token_details = usage.get("completion_tokens_details") or {}
    reasoning_tokens = token_details.get("reasoning_tokens")
    trace = OpenRouterReasoningCapture().extract("", MODEL, response)

    return Result(
        name=name,
        ok=True,
        model=response.get("model"),
        provider=response.get("provider"),
        finish_reason=choice.get("finish_reason"),
        message_content_type=type(message.get("content")).__name__,
        content_present=message.get("content") is not None,
        reasoning_present=message.get("reasoning") is not None,
        reasoning_nonempty=trace is not None,
        captured_trace_type=trace.trace_type.value if trace else None,
        reasoning_tokens=reasoning_tokens,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        reported_cost=usage.get("cost"),
        latency_seconds=round(time.monotonic() - started, 3),
        top_level_keys=",".join(sorted(response.keys())),
        message_keys=",".join(sorted(message.keys())) if isinstance(message, dict) else None,
    )


def run_probe(probe: Probe, api_key: str, timeout: int, allow_fallbacks: bool) -> Result:
    started = time.monotonic()
    try:
        status, response = http_post(
            API_URL,
            request_payload(probe, allow_fallbacks),
            api_key,
            timeout,
        )
        result = inspect_response(probe.name, started, response)
        result.status_code = status
        return result
    except Exception as error:
        return Result(
            name=probe.name,
            ok=False,
            latency_seconds=round(time.monotonic() - started, 3),
            error_type=type(error).__name__,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--allow-fallbacks", action="store_true")
    parser.add_argument("--output", default="logs/union_alpha_live_probe.json")
    parser.add_argument("--limit", type=int, default=len(PROBES))
    args = parser.parse_args()
    if args.timeout <= 0 or not 1 <= args.limit <= len(PROBES):
        parser.error("timeout must be positive and limit must select 1 to 7 probes")

    try:
        api_key = read_api_key()
    except Exception:
        print("CREDENTIAL ERROR: configure OPENROUTER_API_KEY or the fallback key file", file=sys.stderr)
        return 2

    results: List[Result] = []
    for probe in PROBES[:args.limit]:
        print(f"running {probe.name}...", flush=True)
        result = run_probe(probe, api_key, args.timeout, args.allow_fallbacks)
        results.append(result)
        print(f"  ok={result.ok} reasoning_nonempty={result.reasoning_nonempty} "
              f"reasoning_tokens={result.reasoning_tokens} latency={result.latency_seconds}s", flush=True)

    passed = [result for result in results if result.ok]
    latencies = [result.latency_seconds for result in passed if result.latency_seconds is not None]
    summary = {
        "model": MODEL,
        "total": len(results),
        "passed": len(passed),
        "failed": len(results) - len(passed),
        "reasoning_nonempty_count": sum(1 for result in passed if result.reasoning_nonempty),
        "reasoning_tokens_positive_count": sum(1 for result in passed if (result.reasoning_tokens or 0) > 0),
        "zero_cost_count": sum(1 for result in passed if result.reported_cost == 0),
        "latency_seconds": {
            "min": min(latencies) if latencies else None,
            "median": statistics.median(latencies) if latencies else None,
            "max": max(latencies) if latencies else None,
        },
    }

    output_path = Path(os.path.abspath(args.output))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report = {"summary": summary, "results": [asdict(result) for result in results]}
    output_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    print(f"sanitized report: {output_path}")
    return 0 if len(passed) == len(results) and summary["reasoning_nonempty_count"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
