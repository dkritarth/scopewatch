from enum import Enum


class AuditErrorCode(str, Enum):
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    INCOMPLETE_RESPONSE = "INCOMPLETE_RESPONSE"
    REFUSED_RESPONSE = "REFUSED_RESPONSE"
    MALFORMED_JSON = "MALFORMED_JSON"
    MALFORMED_SHAPE = "MALFORMED_SHAPE"
    INVALID_CLASSIFICATION = "INVALID_CLASSIFICATION"
    UNGROUNDED_EXCERPTS = "UNGROUNDED_EXCERPTS"
    INPUT_TOO_LARGE = "INPUT_TOO_LARGE"
    CAPTURE_ERROR = "CAPTURE_ERROR"
    MISSING_REASONING = "MISSING_REASONING"
    AUDITOR_ERROR = "AUDITOR_ERROR"


class AuditError(RuntimeError):
    """Diagnostic containing only a locally defined code, never provider text."""

    def __init__(self, error_code: AuditErrorCode):
        self.error_code = AuditErrorCode(error_code)
        super().__init__(f"Audit failed: {self.error_code.value}.")
