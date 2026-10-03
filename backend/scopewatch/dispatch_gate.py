"""Shared pre-dispatch authorization gate for the executor family (issue #105).

One module owns the pre-dispatch decision. Every executor backend calls
:func:`authorize_dispatch` before touching Docker, a socket, or the
filesystem, and none of them restates the approval-status or outcome rules.
The rules used to be written out four times and drifted: issue #104 fixed
three drifted copies (a ``CONSUMED`` approval executing on the remote
backend, a ``run_command`` digest mismatch, and a missing outcome check in
the runner). A fifth backend now satisfies the cross-backend contract
(``backend/tests/test_executor_gate_consistency.py``) by construction:
it calls this gate and renders the verdict in its own terms.

The gate returns a *verdict*, not a receipt. It never raises a backend
exception and never builds an ``ExecutionReceipt``, so the gateway backends
map a refusal to ``ExecutionSecurityError`` or a ``NOT_EXECUTED`` receipt,
while the ``executor-runner`` sidecar maps the same verdict to an HTTP
status. The rules live here; the rendering stays with each backend.

Stdlib only, on purpose. ``deploy/executor-runner/runner.py`` is stdlib-only
by design and runs in a self-contained image, so this module imports nothing
from ``scopewatch``: no pydantic models, no enums. Each argument may be a
plain mapping (the runner's JSON dicts) or an attribute object (the
gateway's pydantic models), and ``outcome``/``status`` may be plain strings
or enum members. The runner loads *this file* — ``deploy/executor-runner``'s
Dockerfile copies ``backend/scopewatch/dispatch_gate.py`` into the image, and
``runner.py`` resolves that path at import time; there is no second copy in
the repository.

What is shared, and what deliberately is not
--------------------------------------------

Shared here (written exactly once, for every backend):

1. A decision must exist. No decision, no execution.
2. ``DENY`` is final. No approval and no backend can override it.
3. Only ``ALLOW`` and ``HOLD`` authorize anything. Any other outcome —
   unknown, lowercase, empty — fails closed.
4. ``HOLD`` requires an approval whose status is exactly ``APPROVED`` and
   whose ``action_request_id`` is the action being dispatched. ``CONSUMED``,
   ``PENDING``, ``DENIED``, ``EXPIRED``, missing, and mismatched approvals all
   refuse, so single-use replay is refused in memory on every backend.
5. ``network_request`` never executes.

Backend-specific, deliberately kept out of the gate:

- **Staged-target and ``run_command`` revalidation.** They need the
  workspace, the task scope, and ``pathlib`` resolution semantics that differ
  per backend (the local backend resolves in place, Docker revalidates against
  the staged copy, the runner against its own ``/workspace`` mount).
- **Stored-approval verification and the run-lifecycle check.** They need
  SQLite and ``ScopewatchRepository``, which the runner does not have. The
  gateway backends re-verify the in-memory verdict against stored state when
  the caller passes a store handle (``db_path``); the issue suggested a
  ``store`` parameter here and it was declined for that reason: the runner
  could not use it, so it would not be a shared rule.
- **Operation routing.** The local backend refuses ``run_command`` as Docker-only
  defence in depth. That is routing, not authorization.
- **The runner's signed-protocol checks.** Bearer token, dispatch signature,
  one-shot token with TTL, action digest, the decision-to-action binding, the
  approval's run/decision binding, and the "``ALLOW`` carries no approval"
  hygiene rule stay in ``runner.py``: they validate an attacker-reachable wire
  payload that the gateways construct rather than receive, and the gateways
  have nothing equivalent to check.
- **Decision-to-action binding on the gateway backends.** The runner binds the
  decision to the action it received. The three gateway backends never did, and
  five existing tests deliberately pair an action with another action's
  decision to reach executor-level path handling. Adding the binding to the
  gate would change which actions execute, which issue #105 rules out; it is
  filed as a separate follow-up rather than smuggled in here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

# Verdict kinds.
PROCEED = "proceed"  # caller may run its backend-specific checks, then dispatch
DENY = "deny"  # policy DENY: final, never executes, no approval can override
REFUSE = "refuse"  # fail closed: the caller refuses without dispatching

# Machine-readable refusal codes. Backends map these to their own vocabulary
# (the runner to HTTP statuses and messages, the gateway backends to
# ``ExecutionSecurityError`` reasons and ``NOT_EXECUTED`` receipts).
NO_DECISION = "NO_DECISION"
OUTCOME_REFUSED = "OUTCOME_REFUSED"
APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
NETWORK_FORBIDDEN = "NETWORK_FORBIDDEN"
POLICY_DENY = "POLICY_DENY"
OK = "OK"


@dataclass(frozen=True)
class DispatchVerdict:
    """The single pre-dispatch decision shared by the executor family.

    ``outcome`` is the policy outcome the verdict was reached for, or ``None``
    when there was no decision to read. Callers whose own backend-specific
    checks branch on ``HOLD`` read it here instead of re-reading the decision.
    """

    kind: str
    code: str
    reason: str
    outcome: Optional[str] = None

    @property
    def proceed(self) -> bool:
        """True when the caller may continue to backend-specific checks."""
        return self.kind == PROCEED


def _field(obj: Any, name: str) -> Any:
    """Read ``name`` from a mapping (runner dicts) or an object (models)."""
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _text(value: Any) -> Optional[str]:
    """Unwrap a plain string or string enum member; None when absent."""
    if value is None:
        return None
    raw = getattr(value, "value", value)
    return raw if isinstance(raw, str) else None


def authorize_dispatch(
    action: Any,
    decision: Any,
    approval: Optional[Any] = None,
) -> DispatchVerdict:
    """Decide whether ``action`` may proceed to backend-specific dispatch.

    ``action`` needs ``id`` and ``operation``; ``decision`` needs ``outcome``;
    ``approval`` (read only for ``HOLD``) needs ``status`` and
    ``action_request_id``. Any of them may be a mapping or an attribute
    object, and ``outcome``/``status`` may be plain strings or enum members.
    """
    if decision is None:
        return DispatchVerdict(
            REFUSE,
            NO_DECISION,
            "Direct execution without policy evidence is prohibited.",
            None,
        )

    outcome = _text(_field(decision, "outcome"))
    if outcome == "DENY":
        return DispatchVerdict(
            DENY,
            POLICY_DENY,
            "Policy outcome was DENY; execution blocked.",
            outcome,
        )
    if outcome not in ("ALLOW", "HOLD"):
        return DispatchVerdict(
            REFUSE,
            OUTCOME_REFUSED,
            "Policy outcome does not authorize execution.",
            outcome,
        )

    action_id = _text(_field(action, "id"))
    if outcome == "HOLD":
        # Only APPROVED authorizes. CONSUMED, PENDING, DENIED, EXPIRED,
        # missing, and approvals bound to another action all refuse: single-use
        # means a spent approval cannot authorize a replay.
        approval_status = _text(_field(approval, "status"))
        approval_action_id = _text(_field(approval, "action_request_id"))
        if not action_id or approval_status != "APPROVED" or approval_action_id != action_id:
            return DispatchVerdict(
                REFUSE,
                APPROVAL_REQUIRED,
                "Held action requires valid approved status to execute.",
                outcome,
            )

    if _text(_field(action, "operation")) == "network_request":
        return DispatchVerdict(
            REFUSE,
            NETWORK_FORBIDDEN,
            "Network requests are forbidden in synthetic executor.",
            outcome,
        )

    return DispatchVerdict(
        PROCEED, OK, "Authorized for backend-specific dispatch.", outcome
    )