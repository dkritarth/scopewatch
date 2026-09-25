"""Scopewatch model-driven agent package.

Coordinates model-driven agent turns using only gateway-mediated tools.
"""

from scopewatch.agent.loop import AgentLoop, AgentRunResult
from scopewatch.agent.prompt import PROMPT_VERSION, build_system_prompt
from scopewatch.agent.tools import (
    GATEWAY_TOOL_DEFINITIONS,
    GatewayDispatcher,
    convert_tool_call_to_submit_request,
    get_gateway_tools,
)

__all__ = [
    "AgentLoop",
    "AgentRunResult",
    "PROMPT_VERSION",
    "build_system_prompt",
    "GATEWAY_TOOL_DEFINITIONS",
    "GatewayDispatcher",
    "convert_tool_call_to_submit_request",
    "get_gateway_tools",
]
