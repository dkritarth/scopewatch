import unittest

from src.cot_capture import OpenRouterReasoningCapture
from src.models import TraceType


class TestOpenRouterReasoningCapture(unittest.TestCase):
    def setUp(self):
        self.capture = OpenRouterReasoningCapture()

    def test_extracts_provider_reasoning(self):
        response = {
            "choices": [{"message": {"content": "Answer", "reasoning": "Provider reasoning"}}]
        }
        trace = self.capture.extract("Answer", "stealth/union-alpha", response)

        self.assertIsNotNone(trace)
        self.assertEqual(trace.trace_type, TraceType.THINKING_TOKENS)
        self.assertEqual(trace.raw_text, "Provider reasoning")

    def test_ignores_answer_text_without_provider_field(self):
        response = {"choices": [{"message": {"content": "Step one. Step two. Answer."}}]}
        self.assertIsNone(self.capture.extract(response["choices"][0]["message"]["content"], "stealth/union-alpha", response))

    def test_ignores_null_or_empty_reasoning(self):
        null_response = {"choices": [{"message": {"content": "Answer", "reasoning": None}}]}
        empty_response = {"choices": [{"message": {"content": "Answer", "reasoning": "   "}}]}

        self.assertIsNone(self.capture.extract("Answer", "stealth/union-alpha", null_response))
        self.assertIsNone(self.capture.extract("Answer", "stealth/union-alpha", empty_response))

    def test_labels_summary_details_separately(self):
        response = {"choices": [{"message": {"reasoning": "Summary", "reasoning_details": [
            {"type": "reasoning.summary", "summary": "Summary"}
        ]}}]}
        trace = self.capture.extract("", "model", response)
        self.assertEqual(trace.trace_type, TraceType.SUMMARY)

    def test_prefers_text_details_without_duplicate_legacy_text(self):
        response = {"choices": [{"message": {"reasoning": "duplicate", "reasoning_details": [
            {"type": "reasoning.summary", "summary": "Summary"},
            {"type": "reasoning.text", "text": "First"},
            {"type": "reasoning.encrypted", "data": "opaque"},
            {"type": "reasoning.text", "text": "Second"}
        ]}}]}
        trace = self.capture.extract("", "model", response)
        self.assertEqual(trace.raw_text, "First\nSecond")
        self.assertEqual(trace.trace_type, TraceType.THINKING_TOKENS)

    def test_encrypted_and_unknown_details_are_not_reasoning(self):
        for detail in [{"type": "reasoning.encrypted", "data": "opaque"},
                       {"type": "unknown", "text": "unknown"}, None]:
            with self.subTest(detail=detail):
                response = {"choices": [{"message": {"reasoning_details": [detail]}}]}
                self.assertIsNone(self.capture.extract("<thinking>answer</thinking>", "model", response))

    def test_malformed_metadata_stays_missing(self):
        for metadata in [[], "bad", 42, {"choices": [None]}, {"choices": [{}]},
                         {"choices": [{"message": None}]}]:
            with self.subTest(metadata=metadata):
                self.assertIsNone(self.capture.extract("", "model", metadata))
