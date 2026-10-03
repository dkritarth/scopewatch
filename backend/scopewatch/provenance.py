"""Reasoning provenance trust rules (#116, #120).

Two separate questions decide which provenance label a stored action carries.

1. *What kind of text is this?* The provider client answers this first, from
   the response shape alone: a recognized raw reasoning field yields
   ``PROVIDER_EXPOSED_TRACE``, a provider-authored summary yields
   ``AGENT_AUTHORED_SUMMARY``, and an unknown or encrypted variant yields
   ``UNAVAILABLE`` instead of falling back to raw text (#120). The detail type
   that decided the label travels on ``ChatResult.reasoning_detail_type`` so
   the distinction survives normalization.

2. *Who says so?* The gateway API cannot tell a trace captured in-process by
   the provider client from a label typed into a JSON body. Only the first may
   carry a verified label. A submission that presents the capture credential
   (``SCOPEWATCH_CAPTURE_TOKEN``, issued by the operator to in-process capture
   integrations only) is recorded as claimed. Every other submission is
   recorded as an unverified caller assertion, and its raw claim is kept
   separately in ``ActionRequest.caller_claimed_provenance`` for diagnostics.

Failing closed: with no credential configured nothing authenticates, so no
submission can acquire a verified provider-trace label. This module decides
labels only. It never relaxes a decision: provenance is evidence metadata, and
reasoning can still only escalate.
"""

import hmac
import os
from typing import Optional

from scopewatch.models import ReasoningProvenance

CAPTURE_TOKEN_ENV_VAR = "SCOPEWATCH_CAPTURE_TOKEN"
CAPTURE_TOKEN_HEADER = "X-Scopewatch-Capture-Token"

# Claims that assert an origin the gateway cannot confirm without the capture
# credential. Everything else a caller may claim is self-limiting: it never
# tells a reviewer that reasoning came straight from a provider.
UNVERIFIED_CLAIM_LABELS: dict[ReasoningProvenance, ReasoningProvenance] = {
    ReasoningProvenance.PROVIDER_EXPOSED_TRACE: (
        ReasoningProvenance.CALLER_ASSERTED_PROVIDER_TRACE
    ),
    ReasoningProvenance.AGENT_AUTHORED_SUMMARY: ReasoningProvenance.CALLER_ASSERTED_SUMMARY,
}


def configured_capture_token() -> Optional[str]:
    """Return the operator-issued capture credential, or None when unset."""
    return (os.environ.get(CAPTURE_TOKEN_ENV_VAR) or "").strip() or None


def capture_credential_verified(
    configured: Optional[str], provided: Optional[str]
) -> bool:
    """Constant-time check of a submitted capture credential.

    Fails closed: an unset or blank credential on either side never verifies.
    """
    expected = (configured or "").strip()
    offered = (provided or "").strip()
    if not expected or not offered:
        return False
    return hmac.compare_digest(expected, offered)


def provenance_for_submission(
    claimed: ReasoningProvenance, capture_verified: bool
) -> ReasoningProvenance:
    """Label stored for a submission whose provenance was claimed by its caller.

    An authenticated capture keeps the claim. An unauthenticated one keeps any
    self-limiting claim and is relabelled as a caller assertion otherwise.
    """
    if capture_verified:
        return claimed
    return UNVERIFIED_CLAIM_LABELS.get(claimed, claimed)


def has_caller_claim(action_provenance: ReasoningProvenance) -> bool:
    """True when a submission asserted something beyond "no reasoning present"."""
    return action_provenance != ReasoningProvenance.UNAVAILABLE