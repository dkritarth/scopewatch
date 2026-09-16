from enum import Enum
from datetime import datetime, timezone
from typing import List

from pydantic import BaseModel, Field

class TraceType(str, Enum):
    """Types of reasoning traces that can be captured."""
    THINKING_TOKENS = "THINKING_TOKENS"
    SUMMARY = "SUMMARY"
    TOOL_RATIONALE = "TOOL_RATIONALE"

class ScopeClassificationEnum(str, Enum):
    """Possible outcomes of a scope audit."""
    IN_SCOPE = "IN_SCOPE"
    DRIFTING = "DRIFTING"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    HOLD = "HOLD"

class TaskScope(BaseModel):
    """Defines the boundaries of an assigned task."""
    task_description: str
    allowed_paths: List[str] = Field(default_factory=list)
    allowed_tools: List[str] = Field(default_factory=list)
    blocked_paths: List[str] = Field(default_factory=list)

class ReasoningTrace(BaseModel):
    """Represents captured thinking or reasoning from the model."""
    raw_text: str
    source_model: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    trace_type: TraceType

class ScopeClassification(BaseModel):
    """The result of an audit against a task scope."""
    status: ScopeClassificationEnum
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    flagged_excerpts: List[str] = Field(default_factory=list)
