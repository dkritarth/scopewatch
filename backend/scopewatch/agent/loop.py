"""Agent loop coordinating turns with model provider and gateway-mediated tools.

INVARIANT:
The agent NEVER touches the executor or filesystem directly!
All operations are mediated strictly through the Scopewatch gateway API.
"""

from datetime import datetime, timezone
import json
import time
from typing import Any, Optional
import uuid
import httpx
from pydantic import BaseModel, Field

from scopewatch.agent.prompt import PROMPT_VERSION, build_system_prompt
from scopewatch.agent.tools import (
    GatewayDispatcher,
    convert_tool_call_to_submit_request,
    get_gateway_tools,
)
from scopewatch.models import ApprovalStatus, PolicyOutcome
from scopewatch.providers.client import ChatResult, ProviderClient
from scopewatch.providers.loader import get_agent_profile
from scopewatch.schemas import ActionResponse, Run


class AgentRunResult(BaseModel):
    """Summary of an agent run lifecycle, actions, decisions, and outcome."""

    run_id: str
    status: str  # "COMPLETED" or "FAILED"
    turns: int = 0
    total_tool_calls: int = 0
    actions: list[ActionResponse] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    final_response: Optional[str] = None
    error: Optional[str] = None
    prompt_version: str = PROMPT_VERSION
    messages: list[dict[str, Any]] = Field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "turns": self.turns,
            "total_tool_calls": self.total_tool_calls,
            "decisions": self.decisions,
            "final_response": self.final_response,
            "error": self.error,
            "prompt_version": self.prompt_version,
        }


class AgentLoop:
    """Model-driven agent loop mediated strictly by the Scopewatch gateway."""

    def __init__(
        self,
        run_id: str,
        task_description: Optional[str] = None,
        provider_client: Optional[Any] = None,
        dispatcher: Optional[GatewayDispatcher] = None,
        base_url: str = "http://localhost:8000",
        http_client: Optional[httpx.Client] = None,
        transport: Optional[httpx.BaseTransport] = None,
        max_turns: int = 20,
        max_tool_calls: int = 50,
        wall_clock_timeout_s: float = 120.0,
        approval_timeout_s: float = 30.0,
        poll_interval_s: float = 0.5,
        requested_by: str = "scopewatch-agent",
    ) -> None:
        self.run_id = run_id
        self.max_turns = max_turns
        self.max_tool_calls = max_tool_calls
        self.wall_clock_timeout_s = wall_clock_timeout_s
        self.approval_timeout_s = approval_timeout_s
        self.poll_interval_s = poll_interval_s
        self.requested_by = requested_by

        if dispatcher is not None:
            self.dispatcher = dispatcher
        else:
            self.dispatcher = GatewayDispatcher(
                base_url=base_url,
                http_client=http_client,
                transport=transport,
            )

        if task_description:
            self.task_description = task_description
        else:
            # Query gateway for run details to extract task description
            resp = self.dispatcher._client.get(f"/api/v1/runs/{run_id}")
            if resp.status_code == 200:
                run_obj = Run.model_validate(resp.json())
                self.task_description = run_obj.task_scope.task_description
            else:
                self.task_description = "Execute the assigned workspace task."

        if provider_client is not None:
            self.provider_client = provider_client
        else:
            profile = get_agent_profile()
            self.provider_client = ProviderClient(profile)

    def run(self) -> AgentRunResult:
        """Run the model-driven agent loop to completion or failure."""
        start_time = time.time()
        turns = 0
        total_tool_calls = 0
        recorded_actions: list[ActionResponse] = []
        recorded_decisions: list[str] = []

        system_prompt = build_system_prompt(self.task_description)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"Task: {self.task_description}\n"
                    "Please inspect the workspace and perform the required operations."
                ),
            },
        ]

        tools = get_gateway_tools()

        while True:
            # Check turn limit
            if turns >= self.max_turns:
                err = f"Maximum turns limit reached ({self.max_turns})."
                self.dispatcher.fail_run(self.run_id, reason=err)
                return AgentRunResult(
                    run_id=self.run_id,
                    status="FAILED",
                    turns=turns,
                    total_tool_calls=total_tool_calls,
                    actions=recorded_actions,
                    decisions=recorded_decisions,
                    error=err,
                    messages=messages,
                )

            # Check wall clock timeout
            if (time.time() - start_time) > self.wall_clock_timeout_s:
                err = f"Wall clock timeout reached ({self.wall_clock_timeout_s}s)."
                self.dispatcher.fail_run(self.run_id, reason=err)
                return AgentRunResult(
                    run_id=self.run_id,
                    status="FAILED",
                    turns=turns,
                    total_tool_calls=total_tool_calls,
                    actions=recorded_actions,
                    decisions=recorded_decisions,
                    error=err,
                    messages=messages,
                )

            # Query model provider
            chat_result: ChatResult = self.provider_client.complete(
                messages=messages,
                tools=tools,
            )
            turns += 1

            turn_id = f"turn-{uuid.uuid4()}"
            reasoning_trace = chat_result.reasoning_text
            reasoning_provenance = chat_result.reasoning_provenance

            # Append assistant turn to conversation
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if chat_result.content:
                assistant_msg["content"] = chat_result.content
            if chat_result.tool_calls:
                assistant_msg["tool_calls"] = chat_result.tool_calls
            if reasoning_trace:
                assistant_msg["reasoning_content"] = reasoning_trace
            messages.append(assistant_msg)

            # If no tool calls produced, task is finished
            if not chat_result.tool_calls:
                self.dispatcher.complete_run(self.run_id)
                return AgentRunResult(
                    run_id=self.run_id,
                    status="COMPLETED",
                    turns=turns,
                    total_tool_calls=total_tool_calls,
                    actions=recorded_actions,
                    decisions=recorded_decisions,
                    final_response=chat_result.content,
                    messages=messages,
                )

            # Process all tool calls from this turn
            for tool_call in chat_result.tool_calls:
                # Check tool call limit
                if total_tool_calls >= self.max_tool_calls:
                    err = f"Maximum tool calls limit reached ({self.max_tool_calls})."
                    self.dispatcher.fail_run(self.run_id, reason=err)
                    return AgentRunResult(
                        run_id=self.run_id,
                        status="FAILED",
                        turns=turns,
                        total_tool_calls=total_tool_calls,
                        actions=recorded_actions,
                        decisions=recorded_decisions,
                        error=err,
                        messages=messages,
                    )

                # Check wall clock timeout
                if (time.time() - start_time) > self.wall_clock_timeout_s:
                    err = f"Wall clock timeout reached ({self.wall_clock_timeout_s}s)."
                    self.dispatcher.fail_run(self.run_id, reason=err)
                    return AgentRunResult(
                        run_id=self.run_id,
                        status="FAILED",
                        turns=turns,
                        total_tool_calls=total_tool_calls,
                        actions=recorded_actions,
                        decisions=recorded_decisions,
                        error=err,
                        messages=messages,
                    )

                total_tool_calls += 1
                func = tool_call.get("function", {})
                tool_name = func.get("name", "")
                tool_args = func.get("arguments", {})

                submit_req = convert_tool_call_to_submit_request(
                    tool_name=tool_name,
                    tool_arguments=tool_args,
                    turn_id=turn_id,
                    exposed_reasoning_trace=reasoning_trace,
                    reasoning_provenance=reasoning_provenance,
                    reasoning_summary=chat_result.content,
                    requested_by=self.requested_by,
                )

                action_resp = self.dispatcher.submit_action(self.run_id, submit_req)
                recorded_actions.append(action_resp)
                outcome = action_resp.policy_decision.outcome
                decision_str = outcome.value if hasattr(outcome, "value") else str(outcome)
                recorded_decisions.append(decision_str)

                tool_call_id = tool_call.get("id", f"call_{total_tool_calls}")

                if decision_str == PolicyOutcome.ALLOW.value:
                    receipt = action_resp.execution_receipt
                    result_data = (
                        receipt.sanitized_result
                        if receipt and receipt.sanitized_result is not None
                        else {"status": "EXECUTED"}
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": json.dumps(result_data),
                    })

                elif decision_str == PolicyOutcome.DENY.value:
                    reason_code = action_resp.policy_decision.reason_code
                    code_val = (
                        reason_code.value
                        if hasattr(reason_code, "value")
                        else str(reason_code)
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": json.dumps({
                            "status": "DENIED",
                            "reason_code": code_val,
                            "explanation": action_resp.policy_decision.explanation,
                        }),
                    })

                elif decision_str == PolicyOutcome.HOLD.value:
                    approval_req = action_resp.approval_request
                    approval_id = approval_req.id if approval_req else None
                    poll_start = time.time()
                    resolved = False
                    tool_content = ""

                    while (time.time() - poll_start) < self.approval_timeout_s:
                        if (time.time() - start_time) > self.wall_clock_timeout_s:
                            err = f"Wall clock timeout reached ({self.wall_clock_timeout_s}s) while awaiting approval."
                            self.dispatcher.fail_run(self.run_id, reason=err)
                            return AgentRunResult(
                                run_id=self.run_id,
                                status="FAILED",
                                turns=turns,
                                total_tool_calls=total_tool_calls,
                                actions=recorded_actions,
                                decisions=recorded_decisions,
                                error=err,
                                messages=messages,
                            )

                        approvals = self.dispatcher.list_approvals(self.run_id)
                        matching = None
                        for app in approvals:
                            if approval_id and app.id == approval_id:
                                matching = app
                                break
                            elif app.action_request_id == action_resp.action_request.id:
                                matching = app
                                break

                        if matching:
                            if matching.status in (
                                ApprovalStatus.APPROVED,
                                ApprovalStatus.CONSUMED,
                                "APPROVED",
                                "CONSUMED",
                            ):
                                act_data = self.dispatcher.get_action(
                                    self.run_id, action_resp.action_request.id
                                )
                                receipt = act_data.execution_receipt
                                result_data = (
                                    receipt.sanitized_result
                                    if receipt and receipt.sanitized_result is not None
                                    else {"status": "EXECUTED"}
                                )
                                tool_content = json.dumps(result_data)
                                resolved = True
                                break
                            elif matching.status in (ApprovalStatus.DENIED, "DENIED"):
                                tool_content = json.dumps({
                                    "status": "DENIED",
                                    "explanation": (
                                        matching.resolution_reason
                                        or "Action was denied by human reviewer."
                                    ),
                                })
                                resolved = True
                                break
                            elif matching.status in (ApprovalStatus.EXPIRED, "EXPIRED"):
                                tool_content = json.dumps({
                                    "status": "DENIED",
                                    "explanation": "Approval request expired before review.",
                                })
                                resolved = True
                                break

                        time.sleep(self.poll_interval_s)

                    if not resolved:
                        tool_content = json.dumps({
                            "status": "TIMEOUT",
                            "explanation": f"Approval timed out after {self.approval_timeout_s}s waiting for review.",
                        })

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": tool_content,
                    })
