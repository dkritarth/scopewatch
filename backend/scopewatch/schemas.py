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
