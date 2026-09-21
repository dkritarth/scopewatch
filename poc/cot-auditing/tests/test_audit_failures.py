import json
import unittest
from unittest.mock import Mock

from src.cot_capture import DefaultReasoningCapture
from src.models import TaskScope, ScopeClassificationEnum
from src.pipeline import AuditPipeline
from src.scope_auditor import ScopeAuditor, MockAuditorBackend


class TestAuditFailures(unittest.TestCase):
    def setUp(self):
        self.backend = Mock()
        self.pipeline = AuditPipeline(DefaultReasoningCapture(), ScopeAuditor(self.backend))
        self.scope = TaskScope(task_description="Fix tests", blocked_paths=["/secrets/id_rsa"])

    def test_mock_does_not_classify_policy_text_as_trace(self):
        pipeline = AuditPipeline(DefaultReasoningCapture(), ScopeAuditor(MockAuditorBackend()))
        result = pipeline.process("<thinking>Inspect tests</thinking>", "synthetic", self.scope)
        self.assertEqual(result.status, ScopeClassificationEnum.IN_SCOPE)

    def test_invalid_results_hold_without_leaking_payloads(self):
        valid = {"status": "IN_SCOPE", "confidence": 0.8, "reason": "Assessment", "flagged_excerpts": []}
        invalid = ["PRIVATE invalid JSON", "null", "[]", "42", json.dumps({**valid, "status": "ALLOW"}),
                   json.dumps({**valid, "confidence": "0.8"}), json.dumps({**valid, "confidence": True}),
                   json.dumps({**valid, "confidence": 2}), json.dumps({**valid, "reason": ""}),
                   json.dumps({**valid, "confidence": float("nan")}),
                   json.dumps({**valid, "reason": "   "}),
                   json.dumps({**valid, "flagged_excerpts": ["PRIVATE invented evidence"]}),
                   json.dumps({**valid, "permission": "PRIVATE granted"})]
        for response in invalid:
            with self.subTest(response=response):
                self.backend.evaluate.return_value = response
                result = self.pipeline.process("<thinking>Inspect tests</thinking>", "synthetic", self.scope)
                self.assertEqual(result.status, ScopeClassificationEnum.HOLD)
                self.assertEqual(result.confidence, 0)
                self.assertNotIn("PRIVATE", result.model_dump_json())

    def test_timeout_holds_without_exception_text(self):
        self.backend.evaluate.side_effect = TimeoutError("PRIVATE trace")
        result = self.pipeline.process("<thinking>Inspect tests</thinking>", "synthetic", self.scope)
        self.assertEqual(result.status, ScopeClassificationEnum.HOLD)
        self.assertNotIn("PRIVATE", result.reason)

    def test_capture_failure_holds_without_calling_backend(self):
        capture = Mock()
        capture.extract.side_effect = ValueError("PRIVATE trace")
        result = AuditPipeline(capture, ScopeAuditor(self.backend)).process("", "synthetic", self.scope)
        self.assertEqual(result.status, ScopeClassificationEnum.HOLD)
        self.assertNotIn("PRIVATE", result.reason)
        self.backend.evaluate.assert_not_called()

    def test_exact_excerpts_are_preserved(self):
        self.backend.evaluate.return_value = json.dumps({"status": "DRIFTING", "confidence": 0.8,
            "reason": "Review required", "flagged_excerpts": ["Inspect tests"]})
        result = self.pipeline.process("<thinking>Inspect tests</thinking>", "synthetic", self.scope)
        self.assertEqual(result.status, ScopeClassificationEnum.DRIFTING)
        self.assertEqual(result.flagged_excerpts, ["Inspect tests"])

    def test_valid_hold_is_not_a_backend_failure(self):
        self.backend.evaluate.return_value = json.dumps({"status": "HOLD", "confidence": 0,
            "reason": "Insufficient evidence", "flagged_excerpts": []})
        result = self.pipeline.process("<thinking>Inspect tests</thinking>", "synthetic", self.scope)
        self.assertEqual(result.status, ScopeClassificationEnum.HOLD)
        self.assertEqual(result.reason, "Insufficient evidence")

    def test_empty_capture_does_not_call_backend(self):
        result = self.pipeline.process("<thinking> </thinking>", "synthetic", self.scope)
        self.assertEqual(result.status, ScopeClassificationEnum.HOLD)
        self.backend.evaluate.assert_not_called()

    def test_oversized_reasoning_holds_without_calling_backend(self):
        result = self.pipeline.process(
            f"<thinking>{'x' * 65_537}</thinking>", "synthetic", self.scope
        )
        self.assertEqual(result.status, ScopeClassificationEnum.HOLD)
        self.assertEqual(result.error_code, "INPUT_TOO_LARGE")
        self.assertEqual(result.confidence, 0)
        self.assertEqual(result.flagged_excerpts, [])
        self.backend.evaluate.assert_not_called()

    def test_oversized_scope_holds_without_calling_backend(self):
        scope = TaskScope(task_description="x" * 16_385)
        result = self.pipeline.process("<thinking>Inspect tests</thinking>", "synthetic", scope)
        self.assertEqual(result.status, ScopeClassificationEnum.HOLD)
        self.assertEqual(result.error_code, "INPUT_TOO_LARGE")
        self.backend.evaluate.assert_not_called()

    def test_oversized_serialized_prompt_holds_without_calling_backend(self):
        result = self.pipeline.process(
            f"<thinking>{'\U0001f600' * 65_536}</thinking>", "synthetic", self.scope
        )
        self.assertEqual(result.status, ScopeClassificationEnum.HOLD)
        self.assertEqual(result.error_code, "INPUT_TOO_LARGE")
        self.backend.evaluate.assert_not_called()
