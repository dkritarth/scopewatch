from typing import Optional, Dict, Any

from .models import TaskScope, ScopeClassification, ScopeClassificationEnum
from .cot_capture import ReasoningCapture
from .scope_auditor import ScopeAuditor
from .diagnostics import AuditError, AuditErrorCode

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
        
        try:
            trace = self.capture.extract(raw_output, source_model, metadata)
        except Exception:
            return ScopeClassification(status=ScopeClassificationEnum.HOLD, confidence=0.0,
                                       reason="Reasoning capture failed.", error_code=AuditErrorCode.CAPTURE_ERROR)
        if not trace:
            return ScopeClassification(
                status=ScopeClassificationEnum.HOLD,
                confidence=0.0,
                reason="No reasoning available to audit.",
                error_code=AuditErrorCode.MISSING_REASONING,
                flagged_excerpts=[]
            )

        try:
            return self.auditor.audit(task_scope, trace)
        except AuditError as error:
            return ScopeClassification(
                status=ScopeClassificationEnum.HOLD,
                confidence=0.0,
                reason=str(AuditError(error.error_code)),
                error_code=error.error_code,
                flagged_excerpts=[]
            )
        except Exception:
            return ScopeClassification(
                status=ScopeClassificationEnum.HOLD,
                confidence=0.0,
                reason="Auditor error or timeout.",
                error_code=AuditErrorCode.AUDITOR_ERROR,
                flagged_excerpts=[]
            )
