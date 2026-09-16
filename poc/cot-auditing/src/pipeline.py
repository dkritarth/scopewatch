from typing import Optional, Dict, Any

from .models import TaskScope, ScopeClassification, ScopeClassificationEnum
from .cot_capture import ReasoningCapture
from .scope_auditor import ScopeAuditor

class AuditPipeline:
    """Wires reasoning capture and scope auditing together."""

    def __init__(self, capture: ReasoningCapture, auditor: ScopeAuditor):
        self.capture = capture
        self.auditor = auditor

    def process(
        self,
        raw_output: str,
        source_model: str,
        task_scope: TaskScope,
        metadata: Optional[Dict[str, Any]] = None
    ) -> ScopeClassification:
        """Extract reasoning from model output and audit it against the task scope."""
        
        trace = self.capture.extract(raw_output, source_model, metadata)
        if not trace:
            return ScopeClassification(
                status=ScopeClassificationEnum.HOLD,
                confidence=0.0,
                reason="No reasoning available to audit.",
                flagged_excerpts=[]
            )

        try:
            # We wrap the auditor call to catch malformed responses and errors
            return self.auditor.audit(task_scope, trace)
        except ValueError as ve:
            # Handles json decode errors or validation errors
            return ScopeClassification(
                status=ScopeClassificationEnum.HOLD,
                confidence=0.0,
                reason=f"Malformed auditor response: {str(ve)}",
                flagged_excerpts=[]
            )
        except Exception as e:
            # Handles general timeout or connection errors
            return ScopeClassification(
                status=ScopeClassificationEnum.HOLD,
                confidence=0.0,
                reason=f"Auditor error or timeout: {str(e)}",
                flagged_excerpts=[]
            )
