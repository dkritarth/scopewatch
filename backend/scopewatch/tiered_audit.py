"""Opt-in, conservative routing over the existing reasoning-audit interface."""
from collections.abc import Awaitable, Callable
import os
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from scopewatch.budget_store import TokenLedger, is_mock_profile, reserve_per_audit
from scopewatch.providers.client import ProviderClient
from scopewatch.providers.loader import get_profile
from scopewatch.reasoning_audit import ReasoningAuditor, ReasoningAuditResult, ReasoningAuditVerdict
from scopewatch.schemas import AuditTierEvidence


class AuditTierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    tier: Literal["fast_screen", "strong_review"]
    profile: str = Field(min_length=1)
    timeout_s: float = Field(default=30, gt=0, le=120)
    max_tokens: int = Field(default=256, ge=1, le=16384)


def routing_mode() -> str:
    return os.environ.get("SCOPEWATCH_AUDITOR_ROUTING", "single").strip().lower()


def tier_configs() -> list[AuditTierConfig]:
    """Both profiles are explicit; a typo never silently selects a fallback."""
    return [AuditTierConfig(
        tier=tier,
        profile=os.environ[f"SCOPEWATCH_AUDITOR_{prefix}_PROFILE"].strip(),
        timeout_s=os.environ.get(f"SCOPEWATCH_AUDITOR_{prefix}_TIMEOUT_S", "30"),
        max_tokens=os.environ.get(f"SCOPEWATCH_AUDITOR_{prefix}_MAX_TOKENS", "256"),
    ) for tier, prefix in (("fast_screen", "FAST"), ("strong_review", "STRONG"))]


def failed(profile: str, model: str = "unknown", code: str = "AUDIT_ERROR") -> ReasoningAuditResult:
    return ReasoningAuditResult(
        verdict=ReasoningAuditVerdict.FAILED, explanation="Tier audit failed closed.",
        profile=profile, model=model, error_code=code)


class _TierBudgetExceeded(Exception):
    pass


async def run_tiered_audit(
    run_single: Callable[[ReasoningAuditor], Awaitable[ReasoningAuditResult]],
    tokens: TokenLedger,
) -> ReasoningAuditResult:
    start = time.monotonic()
    try:
        if routing_mode() != "two-tier":
            raise ValueError("Unknown audit routing mode")
        configs = tier_configs()
    except Exception:
        return failed("unresolved-tier-config", code="INVALID_TIER_CONFIG")

    results: list[ReasoningAuditResult] = []
    evidence: list[AuditTierEvidence] = []
    for config in configs:
        tier_start = time.monotonic()
        model = "unknown"
        attempted = False
        timeout_s = config.timeout_s
        reservation = 0
        reservation_profile = None
        resolved_profile = None
        client = None
        try:
            profile = get_profile(config.profile)
            model = profile.model
            resolved_profile = profile.name
            # Each tier has one request and no retries. Remove alternate token
            # options before applying the tier's explicit completion cap.
            body = dict(profile.auditor_body)
            extra = dict(profile.extra_body)
            for options in (body, extra):
                options.pop("max_completion_tokens", None)
                options.pop("max_tokens", None)
                options.pop("n", None)
                options.pop("best_of", None)
            body["max_tokens"] = config.max_tokens
            body["n"] = 1
            timeout_s = min(profile.timeout_s, config.timeout_s)
            profile = profile.model_copy(update={
                "timeout_s": timeout_s,
                "max_retries": 0, "auditor_body": body, "extra_body": extra,
            })
            if not is_mock_profile(profile.name, profile.base_url):
                estimate = max(reserve_per_audit(), config.max_tokens)
                if not tokens.check(profile.name, estimate=estimate).allowed:
                    raise _TierBudgetExceeded
                tokens.add(profile.name, estimate)
                reservation = estimate
                reservation_profile = profile.name
            client = ProviderClient(profile)
            auditor = ReasoningAuditor(provider=client, profile=profile.name,
                                       model=model, timeout_s=profile.timeout_s)
            attempted = True
            result = await run_single(auditor)
        except _TierBudgetExceeded:
            result = failed(config.profile, model, "TOKEN_BUDGET_EXCEEDED")
        except Exception:
            result = failed(config.profile, model)
        finally:
            try:
                if reservation:
                    tokens.add(reservation_profile, -reservation)
            except Exception:
                pass  # Metering cleanup must not discard the fail-closed result.
            try:
                if client is not None:
                    client.close()
            except Exception:
                pass  # Cleanup cannot relax or erase an attempted audit.
        result.profile = config.profile
        if not attempted:
            result.total_tokens = 0
        result.latency_ms = (time.monotonic() - tier_start) * 1000
        results.append(result)
        evidence.append(AuditTierEvidence(
            **result.model_dump(exclude={"tiers"}), tier=config.tier,
            attempted=attempted, configured_model=model,
            resolved_profile=resolved_profile,
            timeout_s=timeout_s,
            max_tokens=config.max_tokens))
        if result.verdict == ReasoningAuditVerdict.NO_CONCERN:
            break

    # A failed attempt remains FAILED even if a later reviewer reports clean.
    # A concern likewise survives. The per-tier records retain both opinions.
    selected = next((r for r in results if r.verdict == ReasoningAuditVerdict.FAILED), None)
    selected = selected or next((r for r in results if r.verdict == ReasoningAuditVerdict.CONCERN), results[0])
    return selected.model_copy(update={
        "tiers": evidence,
        "latency_ms": (time.monotonic() - start) * 1000,
        "total_tokens": sum(r.total_tokens for r in results) if all(r.total_tokens is not None for r in results) else None,
    })
