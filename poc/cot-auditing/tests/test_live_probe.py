import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from scripts import live_union_alpha_probe as probe


class TestLiveProbe(unittest.TestCase):
    def test_writes_nested_report_without_reasoning(self):
        response = {
            "choices": [{"message": {"content": "PRIVATE ANSWER", "reasoning": None}}],
            "usage": {"completion_tokens_details": {"reasoning_tokens": 0}},
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "nested" / "report.json"
            with patch.object(probe, "PROBES", probe.PROBES[:1]), patch.object(
                probe, "read_api_key", return_value="PRIVATE KEY"
            ), patch.object(probe, "http_post", return_value=(200, response)), patch(
                "sys.argv", ["probe", "--output", str(output)]
            ), patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(probe.main(), 1)
            report_text = output.read_text()
            report = json.loads(report_text)
            self.assertEqual(report["summary"]["passed"], 1)
            self.assertEqual(report["summary"]["reasoning_nonempty_count"], 0)
            self.assertNotIn("PRIVATE", report_text)

    def test_exception_details_are_not_retained(self):
        with patch.object(probe, "http_post", side_effect=ValueError("PRIVATE BODY")):
            result = probe.run_probe(probe.PROBES[0], "PRIVATE KEY", 1, False)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "ValueError")
        self.assertNotIn("PRIVATE", json.dumps(probe.asdict(result)))

    def test_http_error_body_is_not_exposed(self):
        error = urllib.error.HTTPError(
            probe.API_URL, 429, "Too Many Requests", {}, io.BytesIO(b"PRIVATE BODY")
        )
        with patch.object(probe.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as raised:
                probe.http_post(probe.API_URL, {}, "PRIVATE KEY", 1)
        self.assertEqual(str(raised.exception), "HTTP 429")

    def test_oversized_response_is_rejected_with_a_bounded_read(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b"x" * 1_048_577
        with patch.object(probe.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "response exceeds size limit"):
                probe.http_post(probe.API_URL, {}, "PRIVATE KEY", 1)
        response.read.assert_called_once_with(1_048_577)

    def test_provider_reasoning_is_detected_without_retaining_text(self):
        response = {
            "choices": [{"message": {"content": "PRIVATE ANSWER", "reasoning": "PRIVATE TRACE"}}]
        }
        result = probe.inspect_response("synthetic", probe.time.monotonic(), response)
        self.assertTrue(result.reasoning_nonempty)
        self.assertNotIn("PRIVATE", json.dumps(probe.asdict(result)))

    def test_summary_details_are_measured_by_the_actual_adapter(self):
        response = {"choices": [{"message": {"reasoning_details": [
            {"type": "reasoning.summary", "summary": "PRIVATE SUMMARY"}
        ]}}], "usage": {"completion_tokens_details": None}}
        result = probe.inspect_response("synthetic", probe.time.monotonic(), response)
        self.assertTrue(result.reasoning_nonempty)
        self.assertEqual(result.captured_trace_type, "SUMMARY")
        self.assertIsNone(result.reasoning_tokens)
        self.assertNotIn("PRIVATE", json.dumps(probe.asdict(result)))

    def test_whitespace_environment_key_is_rejected(self):
        with patch.dict(probe.os.environ, {"OPENROUTER_API_KEY": " "}):
            with self.assertRaises(RuntimeError):
                probe.read_api_key()
