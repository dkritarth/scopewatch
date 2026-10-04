#!/usr/bin/env python3
"""Synthetic provider probe for the configured Nebius and OpenRouter profiles.

Verifies, against the provider profile the gateway actually ships:
1. Basic chat completion connectivity.
2. Provider-exposed reasoning extraction, classified honestly: a raw provider
   field is a trace, a summary block is a summary, and text the model wrote into
   `content` is neither (ADR-0001 decision 5).
3. Tool calling combined with reasoning.
4. Structured JSON output for the scope auditor, sent with the profile's
   configured `auditor_body`.

Every request body is built from `backend/config/providers.toml` via
`scopewatch.providers.loader.get_profile`, mirroring `ProviderClient.complete`
and `ProviderClient.audit_chat`, so the probe measures the shipped profile
rather than a hand-written approximation of it. No model ID is hard-coded here.

Safety invariants:
- Never prints, logs, or persists API keys.
- Never prints provider response text: only field names, character counts,
  `finish_reason`, and token counts are reported.
- Sends strictly synthetic, harmless prompts and tool schemas.
- Reads API keys only from the environment variable the profile names.
- The `mock` profile (and `--mock`) runs the offline path with no key and no
  network, and every simulated result is labelled `SIMULATED`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

# The probe lives in scripts/spikes/ and is run as `python3 scripts/spikes/...`
# from the repository root, so make `scopewatch` importable without PYTHONPATH.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_DIR = _REPO_ROOT / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from scopewatch.providers.loader import get_profile  # noqa: E402
from scopewatch.providers.profile import ProviderProfile  # noqa: E402


# Probe CLI slug -> provider profile in backend/config/providers.toml. The slug
# names the *route*; the profile is the single source for model, base URL, key
# environment variable, reasoning control, and auditor body.
PROVIDER_TARGETS: Dict[str, str] = {
    "nebius": "nebius-demo",
    "openrouter": "openrouter-dev",
    "mock": "mock",
}

# Headers OpenRouter asks callers to identify themselves with. Not configuration
# the gateway needs, so it stays here rather than becoming a profile field.
OPENROUTER_EXTRA_HEADERS = {
    "HTTP-Referer": "https://github.com/dkritarth/scopewatch",
    "X-Title": "Scopewatch Synthetic Probe",
}

# Reasoning-field vocabulary shared with scopewatch.providers.client.
RAW_REASONING_FIELDS = ("reasoning_content", "reasoning")
RAW_DETAIL_TYPES = {"reasoning.text"}
SUMMARY_DETAIL_TYPES = {"reasoning.summary"}

PROVENANCE_RAW_TRACE = "PROVIDER_EXPOSED_TRACE"
PROVENANCE_SUMMARY = "AGENT_AUTHORED_SUMMARY"
PROVENANCE_UNAVAILABLE = "UNAVAILABLE"

AUDITOR_MAX_TOKENS = 256
AUDITOR_TEMPERATURE = 0.0
AGENT_MAX_TOKENS = 512
AGENT_TEMPERATURE = 0.0

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
    reasoning_provenance: str = PROVENANCE_UNAVAILABLE
    reasoning_detail_type: Optional[str] = None
    reasoning_text_chars: int = 0
    reasoning_note: Optional[str] = None
    finish_reason: Optional[str] = None
    completion_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    simulated: bool = False


@dataclass(frozen=True)
class ReasoningObservation:
    """What reasoning a provider actually returned, labelled by provenance."""

    provenance: str
    detail_type: Optional[str]
    text_chars: int
    note: Optional[str]


def resolve_target(slug: str) -> ProviderProfile:
    """Load the provider profile this probe measures, from providers.toml."""
    return get_profile(PROVIDER_TARGETS[slug])


def is_offline_profile(profile: ProviderProfile) -> bool:
    """True when a profile needs neither a key nor the network."""
    return profile.base_url.startswith("mock://") or not profile.api_key_env


def build_request_payload(
    profile: ProviderProfile,
    messages: List[Dict[str, Any]],
    *,
    auditor: bool = False,
) -> Dict[str, Any]:
    """Build the request body the gateway would send for this profile.

    Mirrors `ProviderClient.complete` (model + messages, then `extra_body`, then
    `reasoning`), and for `auditor=True` the `audit_chat` overrides on top, where
    `auditor_body` wins over the agent-facing `reasoning` control. `max_tokens`
    and `response_format` are set first so a profile can override them, which is
    what makes the auditor cap and the reasoning-off setting observable.
    """
    payload: Dict[str, Any] = {
        "model": profile.model,
        "messages": messages,
    }
    if profile.extra_body:
        payload.update(profile.extra_body)
    if profile.reasoning_param:
        payload["reasoning"] = profile.reasoning_param
    if auditor:
        payload["response_format"] = AUDITOR_SCHEMA_EXAMPLE
        payload["max_tokens"] = AUDITOR_MAX_TOKENS
        payload.update(profile.auditor_body)
    return payload


def extract_provider_reasoning(message: Dict[str, Any]) -> tuple[bool, Optional[str]]:
    """Report reasoning only when the provider exposes a dedicated field."""
    for field in ("reasoning_content", "reasoning", "reasoning_details"):
        if message.get(field):
            return True, field
    return False, None


def _detail_text(detail: Dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = detail.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _classify_block(block: Any) -> ReasoningObservation:
    """Classify one `reasoning_details` block by its own declared shape."""
    if not isinstance(block, dict):
        return ReasoningObservation(PROVENANCE_UNAVAILABLE, None, 0, None)

    detail_type = block.get("type")
    if isinstance(detail_type, str) and detail_type:
        if detail_type in RAW_DETAIL_TYPES:
            text = _detail_text(block, "text")
        elif detail_type in SUMMARY_DETAIL_TYPES:
            text = _detail_text(block, "summary")
        else:
            # Encrypted, redacted, or undocumented: never fall back to its text.
            return ReasoningObservation(PROVENANCE_UNAVAILABLE, detail_type, 0, None)
        provenance = (
            PROVENANCE_RAW_TRACE if detail_type in RAW_DETAIL_TYPES else PROVENANCE_SUMMARY
        )
    else:
        # An untyped single-object payload keeps the meaning of its field name.
        text = _detail_text(block, "text")
        provenance = PROVENANCE_RAW_TRACE
        detail_type = "reasoning.text"
        if not text:
            text = _detail_text(block, "summary")
            provenance = PROVENANCE_SUMMARY
            detail_type = "reasoning.summary"

    if not text:
        return ReasoningObservation(PROVENANCE_UNAVAILABLE, detail_type, 0, None)
    return ReasoningObservation(provenance, detail_type, len(text), None)


def _classify_details(details: Any) -> ReasoningObservation:
    """Classify `reasoning_details` by block type, as the gateway client does.

    Only `reasoning.text` is a raw trace. `reasoning.summary` is a summary. An
    unrecognised or untyped block stays `UNAVAILABLE` rather than falling back
    to whatever text it carries.
    """
    blocks = details if isinstance(details, list) else [details]
    observations = [_classify_block(block) for block in blocks]

    for provenance in (PROVENANCE_RAW_TRACE, PROVENANCE_SUMMARY):
        matches = [o for o in observations if o.provenance == provenance and o.text_chars]
        if matches:
            return ReasoningObservation(
                provenance,
                matches[0].detail_type,
                sum(o.text_chars for o in matches),
                None,
            )
    # Keep the first detail type seen so a reviewer can see why it is unavailable.
    first_typed = next((o.detail_type for o in observations if o.detail_type), None)
    return ReasoningObservation(PROVENANCE_UNAVAILABLE, first_typed, 0, None)


def _missing_reasoning_note(message: Dict[str, Any], usage: Dict[str, Any]) -> str:
    """Say why reasoning is absent, without implying a trace that is not there.

    The common live case: the provider returns `reasoning_content` and
    `reasoning` as explicit nulls and writes the model's thinking into
    `content`. Per ADR-0001 decision 5 that text is the answer channel, not a
    provider-exposed trace, so it is reported as UNAVAILABLE and the billed
    reasoning tokens are shown as the reason the caller saw no JSON.
    """
    content = message.get("content")
    content_chars = len(content) if isinstance(content, str) else 0
    details = usage.get("completion_tokens_details") if isinstance(usage, dict) else None
    reasoning_tokens = details.get("reasoning_tokens") if isinstance(details, dict) else None

    if isinstance(reasoning_tokens, int) and reasoning_tokens > 0:
        return (
            f"no reasoning field returned; provider billed {reasoning_tokens} reasoning "
            f"token(s) and content is {content_chars} char(s). Text inside `content` "
            "is not a provider-exposed reasoning trace (ADR-0001 decision 5)."
        )
    if content_chars:
        return (
            f"no reasoning field returned; provider reported 0 reasoning tokens "
            f"(content {content_chars} char(s) is the answer channel, not a trace)."
        )
    return "no reasoning field returned by the provider (UNAVAILABLE)."


def classify_provider_reasoning(
    message: Dict[str, Any],
    usage: Optional[Dict[str, Any]] = None,
) -> ReasoningObservation:
    """Label the reasoning a provider returned, honestly and by response shape.

    `PROVIDER_EXPOSED_TRACE` only for a recognised raw field or a
    `reasoning.text` block; `AGENT_AUTHORED_SUMMARY` for `reasoning.summary`;
    `UNAVAILABLE` for everything else, including reasoning the provider billed
    into `content`. `content` is never mined.
    """
    usage_dict = usage if isinstance(usage, dict) else {}

    # Stricter than the field-presence predicate above, which accepts any truthy
    # value: a provider field holding only whitespace is not a trace.
    for field in RAW_REASONING_FIELDS:
        text = message.get(field)
        if isinstance(text, str) and text.strip():
            return ReasoningObservation(PROVENANCE_RAW_TRACE, field, len(text.strip()), None)

    if not extract_provider_reasoning(message)[0]:
        return ReasoningObservation(
            PROVENANCE_UNAVAILABLE, None, 0, _missing_reasoning_note(message, usage_dict)
        )
    return _classify_details(message.get("reasoning_details"))


def make_request(
    url: str,
    api_key: str,
    payload: Dict[str, Any],
    extra_headers: Optional[Dict[str, str]] = None,
    timeout: float = 45.0,
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


def _usage_facts(data: Dict[str, Any]) -> tuple[Optional[str], Optional[int], Optional[int]]:
    """Extract `finish_reason` and token counts. Never the response body."""
    choice = (data.get("choices") or [{}])[0]
    usage = data.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    return (
        choice.get("finish_reason"),
        usage.get("completion_tokens"),
        details.get("reasoning_tokens"),
    )


def run_mock_probes(provider: str, model: str) -> List[ProbeResult]:
    """Simulate probe responses for local verification without external network or keys.

    These are fabricated values, not measurements, so every result is marked
    `simulated` and printed as `[SIMULATED]`. They prove the reporting path
    works, nothing about the provider.
    """
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
            reasoning_provenance=PROVENANCE_RAW_TRACE,
            reasoning_detail_type="reasoning_content",
            reasoning_text_chars=640,
            finish_reason="stop",
            completion_tokens=5,
            reasoning_tokens=None,
            simulated=True,
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
            reasoning_provenance=PROVENANCE_RAW_TRACE,
            reasoning_detail_type="reasoning_content",
            reasoning_text_chars=640,
            finish_reason="tool_calls",
            completion_tokens=5,
            reasoning_tokens=None,
            simulated=True,
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
            reasoning_provenance=PROVENANCE_UNAVAILABLE,
            reasoning_detail_type=None,
            reasoning_text_chars=0,
            reasoning_note=(
                "simulated: the profile's auditor reasoning control leaves room for the "
                "JSON verdict inside the token cap (0 reasoning tokens billed)."
            ),
            finish_reason="stop",
            completion_tokens=66,
            reasoning_tokens=0,
            simulated=True,
        )
    )

    return results


def _observation_fields(observation: ReasoningObservation) -> Dict[str, Any]:
    return {
        "has_reasoning": observation.provenance != PROVENANCE_UNAVAILABLE,
        "reasoning_field": observation.detail_type,
        "reasoning_provenance": observation.provenance,
        "reasoning_detail_type": observation.detail_type,
        "reasoning_text_chars": observation.text_chars,
        "reasoning_note": observation.note,
    }


def run_live_probes(
    provider: str,
    profile: ProviderProfile,
    api_key: str,
    extra_headers: Optional[Dict[str, str]] = None,
) -> List[ProbeResult]:
    """Run real HTTP probes against an OpenAI-compatible endpoint.

    Every request body comes from `build_request_payload`, so what is measured
    is the shipped profile. The request timeout is the profile's own
    `timeout_s`, not a hard-coded guess.
    """
    results = []
    endpoint = f"{profile.base_url.rstrip('/')}/chat/completions"
    timeout = profile.timeout_s

    def send(payload: Dict[str, Any]) -> Dict[str, Any]:
        return make_request(endpoint, api_key, payload, extra_headers, timeout=timeout)

    def record(probe_name: str, latency: float, **fields: Any) -> None:
        results.append(
            ProbeResult(
                provider=provider,
                model=profile.model,
                probe_name=probe_name,
                latency_ms=latency,
                **fields,
            )
        )

    def failure(probe_name: str, latency: float, error: str) -> None:
        record(
            probe_name,
            latency,
            status="FAILED",
            has_reasoning=False,
            reasoning_field=None,
            has_tool_calls=False,
            is_valid_json=False,
            error=error,
        )

    # Probe 1: Basic completion with reasoning inspection
    t0 = time.perf_counter()
    p1_payload = build_request_payload(
        profile,
        [
            {
                "role": "user",
                "content": "Solve: A synthetic invoice lists 3 items at $12 each. What is the total? Think step by step.",
            }
        ],
    )
    p1_payload["max_tokens"] = AGENT_MAX_TOKENS
    p1_payload["temperature"] = AGENT_TEMPERATURE
    try:
        data = send(p1_payload)
        latency = (time.perf_counter() - t0) * 1000
        message = (data.get("choices") or [{}])[0].get("message", {})
        finish_reason, completion_tokens, reasoning_tokens = _usage_facts(data)
        record(
            "basic_reasoning",
            latency,
            status="SUCCESS",
            has_tool_calls=False,
            is_valid_json=False,
            finish_reason=finish_reason,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            **_observation_fields(classify_provider_reasoning(message, data.get("usage"))),
        )
    except Exception as e:
        failure("basic_reasoning", (time.perf_counter() - t0) * 1000, str(e))

    # Probe 2: Tool calling with reasoning
    t0 = time.perf_counter()
    p2_payload = build_request_payload(
        profile,
        [
            {
                "role": "system",
                "content": "You are an automated invoice assistant. When requested to read a file, call read_file. Reason before acting.",
            },
            {
                "role": "user",
                "content": "Please read invoices/vendor_a.txt to verify invoice status.",
            },
        ],
    )
    p2_payload["tools"] = SYNTHETIC_TOOL_SCHEMA
    p2_payload["tool_choice"] = "auto"
    p2_payload["max_tokens"] = AGENT_MAX_TOKENS
    p2_payload["temperature"] = AGENT_TEMPERATURE
    try:
        data = send(p2_payload)
        latency = (time.perf_counter() - t0) * 1000
        message = (data.get("choices") or [{}])[0].get("message", {})
        finish_reason, completion_tokens, reasoning_tokens = _usage_facts(data)
        record(
            "tool_calling_with_reasoning",
            latency,
            status="SUCCESS",
            has_tool_calls=bool(message.get("tool_calls")),
            is_valid_json=False,
            finish_reason=finish_reason,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            **_observation_fields(classify_provider_reasoning(message, data.get("usage"))),
        )
    except Exception as e:
        failure("tool_calling_with_reasoning", (time.perf_counter() - t0) * 1000, str(e))

    # Probe 3: Structured JSON Output (Auditor mode), with the profile's
    # configured auditor_body. Without it the reasoning control is left at the
    # provider default and the model can spend the whole token cap thinking.
    t0 = time.perf_counter()
    p3_payload = build_request_payload(
        profile,
        [
            {
                "role": "system",
                "content": "Assess synthetic scope. Respond ONLY with a JSON object containing keys: status, confidence, reason.",
            },
            {
                "role": "user",
                "content": "Action: read_file('invoices/vendor_a.txt'). Scope: inspect approved vendor invoices.",
            },
        ],
        auditor=True,
    )
    p3_payload["temperature"] = AUDITOR_TEMPERATURE
    try:
        data = send(p3_payload)
        latency = (time.perf_counter() - t0) * 1000
        message = (data.get("choices") or [{}])[0].get("message", {})
        content = message.get("content")
        finish_reason, completion_tokens, reasoning_tokens = _usage_facts(data)

        is_valid = False
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict) and "status" in parsed:
                is_valid = True
        except Exception:
            is_valid = False

        record(
            "structured_json_auditor",
            latency,
            status="SUCCESS" if is_valid else "MALFORMED_OUTPUT",
            has_tool_calls=False,
            is_valid_json=is_valid,
            finish_reason=finish_reason,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            **_observation_fields(classify_provider_reasoning(message, data.get("usage"))),
        )
    except Exception as e:
        failure("structured_json_auditor", (time.perf_counter() - t0) * 1000, str(e))

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Synthetic Provider Probe for Scopewatch")
    parser.add_argument(
        "--provider",
        choices=[*PROVIDER_TARGETS, "all"],
        default="all",
        help="Provider to probe ('mock' runs the offline path; 'all' probes every target)",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Force the offline mock path for the selected providers (no key, no network)",
    )
    args = parser.parse_args()

    selected = list(PROVIDER_TARGETS) if args.provider == "all" else [args.provider]

    print("=" * 60)
    print("Scopewatch Provider Probe")
    print("Verifying Nemotron reasoning, tool calls, and structured output")
    print("=" * 60)

    all_results: List[ProbeResult] = []
    for slug in selected:
        try:
            profile = resolve_target(slug)
        except Exception as e:  # noqa: BLE001 - report and keep going
            print(f"[ERROR] {slug}: could not load provider profile ({type(e).__name__}).")
            continue

        print(f"[PROFILE] {slug} -> profile '{profile.name}' ({profile.model})")
        if profile.auditor_body:
            print(
                f"[PROFILE] auditor_body: {json.dumps(profile.auditor_body, sort_keys=True)}"
            )
        else:
            print("[PROFILE] auditor_body: {} (none configured)")

        if args.mock or is_offline_profile(profile):
            print(f"[MOCK MODE] Simulating probe execution for {slug}...")
            all_results.extend(run_mock_probes(slug, profile.model))
            continue

        key = os.getenv(profile.api_key_env or "", "").strip()
        if not key:
            print(
                f"[SKIP] {slug}: {profile.api_key_env} not set in environment "
                f"(profile '{profile.name}')."
            )
            continue

        print(f"[PROBE] Running {slug} probes against {profile.base_url}...")
        extra_headers = OPENROUTER_EXTRA_HEADERS if slug == "openrouter" else None
        all_results.extend(
            run_live_probes(
                provider=slug,
                profile=profile,
                api_key=key,
                extra_headers=extra_headers,
            )
        )

    if not all_results:
        print("\nNo probes executed. For an offline run use --provider mock or --mock.")
        return 0

    print("\nProbe Results:")
    print("-" * 60)
    for r in all_results:
        tag = " [SIMULATED]" if r.simulated else ""
        print(f"[{r.provider}]{tag} {r.probe_name} ({r.model}):")
        print(f"  Status: {r.status} | Latency: {r.latency_ms:.1f}ms")
        print(
            f"  Reasoning: {r.reasoning_provenance}"
            f" (field: {r.reasoning_detail_type}, {r.reasoning_text_chars} chars)"
        )
        if r.reasoning_note:
            print(f"  Reasoning note: {r.reasoning_note}")
        tokens = f"  Completion tokens: {r.completion_tokens}"
        if r.reasoning_tokens is not None:
            tokens += f" (reasoning tokens: {r.reasoning_tokens})"
        print(f"{tokens} | finish_reason: {r.finish_reason}")
        print(f"  Tool calls: {r.has_tool_calls} | Valid JSON: {r.is_valid_json}")
        if r.error:
            print(f"  Error: {r.error}")
        print()

    failures = [r for r in all_results if r.status != "SUCCESS"]
    print(f"Probe run completed: {len(all_results) - len(failures)}/{len(all_results)} SUCCESS.")
    if failures:
        print("Failed probes: " + ", ".join(f"{r.provider}/{r.probe_name}={r.status}" for r in failures))
    return 0


if __name__ == "__main__":
    sys.exit(main())
