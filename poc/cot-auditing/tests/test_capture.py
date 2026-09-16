import unittest
from src.cot_capture import DefaultReasoningCapture
from src.models import TraceType

class TestReasoningCapture(unittest.TestCase):
    def setUp(self):
        self.capture = DefaultReasoningCapture()

    def test_extract_thinking_tokens(self):
        raw_output = "<thinking>\nThis is a thinking block.\n</thinking>"
        trace = self.capture.extract(raw_output, source_model="model-1")
        self.assertIsNotNone(trace)
        self.assertEqual(trace.trace_type, TraceType.THINKING_TOKENS)
        self.assertEqual(trace.raw_text, "This is a thinking block.")

    def test_extract_summary(self):
        raw_output = "Some text <summary>Here is a summary</summary>"
        trace = self.capture.extract(raw_output, source_model="model-1")
        self.assertIsNotNone(trace)
        self.assertEqual(trace.trace_type, TraceType.SUMMARY)
        self.assertEqual(trace.raw_text, "Here is a summary")

    def test_extract_tool_rationale(self):
        trace = self.capture.extract("No tags here", source_model="model-1", metadata={"tool_rationale": "Tool logic"})
        self.assertIsNotNone(trace)
        self.assertEqual(trace.trace_type, TraceType.TOOL_RATIONALE)
        self.assertEqual(trace.raw_text, "Tool logic")

    def test_empty_or_missing(self):
        trace = self.capture.extract("No tags here", source_model="model-1")
        self.assertIsNone(trace)
