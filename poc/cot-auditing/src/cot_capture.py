import re
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any

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
        if thinking_match:
            return ReasoningTrace(
                raw_text=thinking_match.group(1).strip(),
                source_model=source_model,
                trace_type=TraceType.THINKING_TOKENS
            )
            
        # Check for explicit tool rationale in metadata
        if metadata and "tool_rationale" in metadata:
            return ReasoningTrace(
                raw_text=str(metadata["tool_rationale"]),
                source_model=source_model,
                trace_type=TraceType.TOOL_RATIONALE
            )
            
        # Check for summary XML tags
        summary_match = re.search(r"<summary>(.*?)(?:</summary>|$)", raw_output, re.DOTALL)
        if summary_match:
            return ReasoningTrace(
                raw_text=summary_match.group(1).strip(),
                source_model=source_model,
                trace_type=TraceType.SUMMARY
            )
            
        return None
