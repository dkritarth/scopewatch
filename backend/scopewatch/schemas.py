"""Pydantic schemas for Scopewatch domain objects and API messages."""

from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field

from scopewatch.models import (
    ApprovalStatus,
    EventType,
    ExecutionStatus,
    PolicyOutcome,
    ReasonCode,
    ReasoningProvenance,
    RunStatus,
    format_policy_version,
)


class TaskScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    task_description: str = Field(min_length=1)
    allowed_paths: list[str] = Field(default_factory=list)
    blocked_paths: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    allowed_operations: list[str] = Field(default_factory=list)
    allowed_network_destinations: list[str] = Field(default_factory=list)
    requires_approval: list[str] = Field(default_factory=list)
    allowed_commands: list[list[str]] = Field(default_factory=list)
    commands_requiring_approval: list[list[str]] = Field(default_factory=list)
    created_at: str


class Run(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    task_scope: TaskScope
    status: RunStatus = RunStatus.ACTIVE
    created_at: str
    updated_at: str
    synthetic: bool = True
    interception_coverage: str = (
        "This local baseline mediates only actions submitted through its "
        "synthetic demo gateway. It does not intercept arbitrary host or agent operations."
    )
    reasoning_availability: str = (
        "Provider traces unavailable in local baseline. Agent-authored summaries or "
        "synthetic fixtures are labeled explicitly."
    )
    prompt_version: Optional[str] = None
    # Issue #117: the run's own workspace directory on the gateway host,
    # persisted so submit/approve/execute resolve the identity from the stored
    # run instead of process state (it must survive a restart). Optional only
    # for rows written before per-run workspaces existed; the service resolves
    # those lazily and stores the result.
    #
    # DELIBERATELY NOT PART OF THE API CONTRACT. This model is the storage
    # model and the persistence layer reads it, but it is also the FastAPI
    # `response_model` for every run endpoint, so the field used to ride along
    # in every run response as a host path. Two reasons to keep it out of
    # responses (not just out of *evidence*, which already excludes it):
    #   * a host filesystem path is not information a reviewer needs, and
    #     publishing it tells every reader where the gateway's writable
    #     directory layout is;
    #   * `/api/v1/runs` is a public read with no token, so this is a
    #     disclosure surface rather than an internal one.
    # `RunResponse` (below) is what the API returns. Keep both in sync when
    # adding a field, and prefer adding to `RunResponse` alone unless the
    # service genuinely needs to persist the value.
    workspace_path: Optional[str] = None


class RunResponse(BaseModel):
    """The public shape of a run. Omits the host-side ``workspace_path`` (#117).

    Kept separate from :class:`Run` on purpose rather than filtering fields per
    endpoint, so "what a caller can see about a run" is one readable list and
    adding a host path to the storage model cannot silently publish it.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    task_scope: TaskScope
    status: RunStatus = RunStatus.ACTIVE
    created_at: str
    updated_at: str
    synthetic: bool = True
    interception_coverage: str
    reasoning_availability: str
    prompt_version: Optional[str] = None

    @classmethod
    def from_run(cls, run: Run) -> "RunResponse":
        return cls(
            id=run.id,
            name=run.name,
            task_scope=run.task_scope,
            status=run.status,
            created_at=run.created_at,
            updated_at=run.updated_at,
            synthetic=run.synthetic,
            interception_coverage=run.interception_coverage,
            reasoning_availability=run.reasoning_availability,
            prompt_version=run.prompt_version,
        )


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    id: str
    run_id: str
    tool: str
    operation: str
    resource: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    requested_by: str = "synthetic-agent"
    requested_at: str
    reasoning_summary: Optional[str] = None
    exposed_reasoning_trace: Optional[str] = None
    reasoning_provenance: ReasoningProvenance = ReasoningProvenance.UNAVAILABLE
    # #116: the raw provenance label the submitting caller asserted, kept only
    # when the gateway could not verify it. Never a second source of truth.
    caller_claimed_provenance: Optional[ReasoningProvenance] = None
    turn_id: Optional[str] = None
    reasoning_audit_id: Optional[str] = None


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    action_request_id: str
    outcome: PolicyOutcome
    reason_code: ReasonCode
    explanation: str
    matched_rule: str
    decided_at: str
    deterministic: bool = True
    reasoning_audit_id: Optional[str] = None
    # Identity of the deterministic policy implementation and rule set that
    # produced this decision (issue #119), independent of the data
    # ``schema_version``. See scopewatch.models for update semantics.
    #
    # The default is None, not the current version, so the field fails closed:
    # `evaluate_policy` stamps the identity at its single public entry point,
    # and anything that builds a decision without going through the engine --
    # above all a read of a row written before policy versions existed -- ends
    # up visibly unknown instead of silently claiming today's revision.
    # Repository read paths therefore pass the stored value through verbatim.
    policy_version: Optional[str] = None

    @property
    def policy_version_label(self) -> str:
        """Display label: the stored identity, or "unknown (legacy)"."""
        return format_policy_version(self.policy_version)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    run_id: str
    action_request_id: str
    policy_decision_id: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: str
    expires_at: str
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None
    resolution_reason: Optional[str] = None
    approval_token_version: int = 1
    operation: Optional[str] = None
    resource: Optional[str] = None
    tool: Optional[str] = None
    # Policy identity evaluated for the held action (issue #119), resolved
    # from the bound policy decision on every read. It is deliberately not a
    # second stored column: an approval authorizes one stored decision, so the
    # decision stays the single source of truth and the two cannot disagree.
    # None means the decision predates policy-version tracking.
    policy_version: Optional[str] = None


class ExecutionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    action_request_id: str
    status: ExecutionStatus
    started_at: str
    completed_at: str
    executor: str = "synthetic-workspace-executor"
    sanitized_result: Optional[dict[str, Any]] = None
    error_code: Optional[str] = None
    resource: str
    operation: str


class EvidenceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int
    id: str
    run_id: str
    event_type: EventType
    timestamp: str
    actor: str
    summary: str
    action_request_id: Optional[str] = None
    policy_decision_id: Optional[str] = None
    approval_request_id: Optional[str] = None
    execution_receipt_id: Optional[str] = None
    turn_id: Optional[str] = None
    details: dict[str, Any] = Field(default_factory=dict)
    synthetic: bool = True


class ReasoningAuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    run_id: str
    turn_id: str
    trace_hash: str
    verdict: str
    concern_type: Optional[str] = None
    flagged_excerpts: list[str] = Field(default_factory=list)
    explanation: str
    model: str
    profile: str
    latency_ms: float = 0.0
    error_code: Optional[str] = None
    audited_at: str


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    task_scope: TaskScope
    prompt_version: Optional[str] = None


class UpdateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_version: Optional[str] = None


class SubmitActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    operation: str
    resource: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    requested_by: Optional[str] = "synthetic-agent"
    reasoning_summary: Optional[str] = None
    exposed_reasoning_trace: Optional[str] = None
    reasoning_provenance: Optional[ReasoningProvenance] = None
    turn_id: Optional[str] = None


class ActionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_request: ActionRequest
    policy_decision: PolicyDecision
    approval_request: Optional[ApprovalRequest] = None
    execution_receipt: Optional[ExecutionReceipt] = None
    events: list[EvidenceEvent] = Field(default_factory=list)
    reasoning_audit: Optional[ReasoningAuditRecord] = None
    reasoning_audit_id: Optional[str] = None


class ResolveApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution_reason: Optional[str] = None


class ApprovalResolutionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_request: ApprovalRequest
    execution_receipt: Optional[ExecutionReceipt] = None
    events: list[EvidenceEvent] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str
    database: str
    version: str
    # True when demo guards are engaged (DEMO_TOKEN set). Public, non-secret:
    # the dashboard uses it to offer the reviewer-token control only where a
    # token is actually required (#115). Never carries the token itself.
    demo_mode: bool = False
