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
    details: dict[str, Any] = Field(default_factory=dict)
    synthetic: bool = True


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    task_scope: TaskScope


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


class ActionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_request: ActionRequest
    policy_decision: PolicyDecision
    approval_request: Optional[ApprovalRequest] = None
    execution_receipt: Optional[ExecutionReceipt] = None
    events: list[EvidenceEvent] = Field(default_factory=list)


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
