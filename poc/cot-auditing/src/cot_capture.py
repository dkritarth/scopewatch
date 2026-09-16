import re
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from .models import ReasoningTrace, TraceType

class ReasoningCapture(ABC):
    """Base class for capturing reasoning traces from model outputs."""

    @abstractmethod
    def extract(
        self, raw_output: str, source_model: str, metadata: Optional[Dict[str, Any]] = None
    ) -> Optional[ReasoningTrace]:
        """Extract reasoning trace from raw text or metadata."""
        pass

class DefaultReasoningCapture(ReasoningCapture):
    """Concrete implementation parsing common reasoning formats."""

    def extract(
        self, raw_output: str, source_model: str, metadata: Optional[Dict[str, Any]] = None
    ) -> Optional[ReasoningTrace]:
        
        # Check for Claude style thinking XML tags
        thinking_match = re.search(r"<thinking>(.*?)(?:</thinking>|$)", raw_output, re.DOTALL)
        if thinking_match and thinking_match.group(1).strip():
            return ReasoningTrace(
                raw_text=thinking_match.group(1).strip(),
                source_model=source_model,
                trace_type=TraceType.THINKING_TOKENS
            )
            
        # Check for explicit tool rationale in metadata
        if isinstance(metadata, dict) and isinstance(metadata.get("tool_rationale"), str) and metadata["tool_rationale"].strip():
            return ReasoningTrace(
                raw_text=metadata["tool_rationale"].strip(),
                source_model=source_model,
                trace_type=TraceType.TOOL_RATIONALE
            )
            
        # Check for summary XML tags
        summary_match = re.search(r"<summary>(.*?)(?:</summary>|$)", raw_output, re.DOTALL)
        if summary_match and summary_match.group(1).strip():
            return ReasoningTrace(
                raw_text=summary_match.group(1).strip(),
                source_model=source_model,
                trace_type=TraceType.SUMMARY
            )
            
        return None


class OpenRouterReasoningCapture(ReasoningCapture):
    """Capture only provider-exposed OpenRouter reasoning fields.

    A model's visible answer can include a self-authored explanation, but that
    is not the same thing as provider reasoning tokens. This adapter therefore
    reads the structured OpenRouter response fields and leaves the answer text
    uncaptured.
    """

    def extract(
        self,
        raw_output: str,
        source_model: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[ReasoningTrace]:
        if not isinstance(metadata, dict):
            return None
        choices = metadata.get("choices")
        if not isinstance(choices, list) or not choices:
            return None

        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            return None

        message = first_choice.get("message")
        if not isinstance(message, dict):
            return None

        details = message.get("reasoning_details")
        if isinstance(details, list):
            for detail_type, field, trace_type in [
                ("reasoning.text", "text", TraceType.THINKING_TOKENS),
                ("reasoning.summary", "summary", TraceType.SUMMARY),
            ]:
                parts = [detail[field].strip() for detail in details
                         if isinstance(detail, dict) and detail.get("type") == detail_type
                         and isinstance(detail.get(field), str) and detail[field].strip()]
                if parts:
                    return ReasoningTrace(raw_text="\n".join(parts), source_model=source_model,
                                          trace_type=trace_type)

        reasoning = message.get("reasoning")
        if not isinstance(reasoning, str) or not reasoning.strip():
            return None

        return ReasoningTrace(
            raw_text=reasoning.strip(),
            source_model=source_model,
            trace_type=TraceType.THINKING_TOKENS,
        )
