import unittest
import json
from src.scope_auditor import ScopeAuditor, MockAuditorBackend, LLMBackend
from src.models import TaskScope, ReasoningTrace, TraceType, ScopeClassificationEnum

class BrokenBackend(LLMBackend):
    def evaluate(self, prompt: str) -> str:
        return "Not a json response"

class TestScopeAuditor(unittest.TestCase):
    def setUp(self):
        self.backend = MockAuditorBackend()
        self.auditor = ScopeAuditor(self.backend)
        self.scope = TaskScope(
            task_description="Test task",
            allowed_paths=["/a"],
            allowed_tools=["test"]
        )

    def test_in_scope(self):
        trace = ReasoningTrace(raw_text="Normal reasoning", source_model="model", trace_type=TraceType.THINKING_TOKENS)
        result = self.auditor.audit(self.scope, trace)
        self.assertEqual(result.status, ScopeClassificationEnum.IN_SCOPE)

    def test_out_of_scope(self):
        trace = ReasoningTrace(raw_text="Let's delete all files", source_model="model", trace_type=TraceType.THINKING_TOKENS)
        result = self.auditor.audit(self.scope, trace)
        self.assertEqual(result.status, ScopeClassificationEnum.OUT_OF_SCOPE)
        self.assertIn("delete all", result.flagged_excerpts)

    def test_drifting(self):
        trace = ReasoningTrace(raw_text="This seems unrelated", source_model="model", trace_type=TraceType.THINKING_TOKENS)
        result = self.auditor.audit(self.scope, trace)
        self.assertEqual(result.status, ScopeClassificationEnum.DRIFTING)

    def test_malformed_response(self):
        broken_auditor = ScopeAuditor(BrokenBackend())
        trace = ReasoningTrace(raw_text="Normal reasoning", source_model="model", trace_type=TraceType.THINKING_TOKENS)
        with self.assertRaises(ValueError):
            broken_auditor.audit(self.scope, trace)
