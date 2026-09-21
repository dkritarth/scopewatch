import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import evaluate_auditor
from scripts.check_auditor_report import check_report
from src.models import ScopeClassification


class TestEvaluationCLI(unittest.TestCase):
    def test_missing_credentials_have_a_redacted_exit(self):
        with patch("sys.argv", ["evaluate", "--live"]), patch(
            "sys.stdout", new_callable=io.StringIO
        ) as output, patch.object(evaluate_auditor, "read_api_key", side_effect=RuntimeError("PRIVATE")):
            self.assertEqual(evaluate_auditor.main(), 2)
            self.assertNotIn("PRIVATE", output.getvalue())

    def test_saved_report_rejects_hold_for_rejected_unsafe_read(self):
        fixtures = Path(evaluate_auditor.__file__).resolve().parents[1] / "fixtures" / "auditor_cases.json"
        cases = json.loads(fixtures.read_text())
        report = {"results": [{"id": case["id"], "actual": case["expected"], "error_code": None}
                              for case in cases]}
        check_report(report)
        row = next(row for row in report["results"] if row["id"] == "reject-unsafe-read")
        row["actual"] = "HOLD"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            output.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, "reject-unsafe-read: expected IN_SCOPE, got HOLD"):
                check_report(json.loads(output.read_text()))

    def test_offline_default_matches_development_cases_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "nested" / "report.json"
            with patch("sys.argv", ["evaluate", "--output", str(output)]), patch(
                "sys.stdout", new_callable=io.StringIO
            ), patch.object(evaluate_auditor, "OpenRouterAuditorBackend") as backend:
                self.assertEqual(evaluate_auditor.main(), 0)
                backend.assert_not_called()
            report = json.loads(output.read_text())
            self.assertEqual(report["backend"], "mock")
            self.assertEqual(report["cases"], 6)
            self.assertEqual(report["matches"], 6)

    def test_live_backend_errors_are_redacted_and_count_as_mismatches(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            with patch("sys.argv", ["evaluate", "--live", "--output", str(output)]), patch(
                "sys.stdout", new_callable=io.StringIO
            ), patch.object(evaluate_auditor, "read_api_key", return_value="PRIVATE KEY"), patch.object(
                evaluate_auditor, "OpenRouterAuditorBackend"
            ) as backend:
                backend.return_value.evaluate.side_effect = TimeoutError("PRIVATE TRACE")
                self.assertEqual(evaluate_auditor.main(), 1)
            text = output.read_text()
            self.assertNotIn("PRIVATE", text)
            report = json.loads(text)
            self.assertEqual(report["matches"], 0)
            self.assertTrue(all(row["actual"] == "HOLD" for row in report["results"]))
            self.assertTrue(all(row["error_code"] == "TRANSPORT_ERROR" for row in report["results"]))

    def test_expected_hold_matches_only_when_model_result_is_valid(self):
        task = dict(description="Synthetic", allowed_paths=[], blocked_paths=[], allowed_tools=[])
        cases = [dict(id="synthetic", expected="HOLD", trace="Synthetic trace")]
        for failed in (False, True):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "report.json"
                with patch("sys.argv", ["evaluate", "--output", str(output)]), patch(
                    "sys.stdout", new_callable=io.StringIO
                ), patch.object(evaluate_auditor.json, "loads", side_effect=[[task], cases]), patch.object(
                    evaluate_auditor.ScopeAuditor, "audit",
                    return_value=ScopeClassification(status="HOLD", confidence=0, reason="Insufficient evidence"),
                    side_effect=RuntimeError("PRIVATE") if failed else None,
                ):
                    self.assertEqual(evaluate_auditor.main(), int(failed))
                report = json.loads(output.read_text())
                self.assertEqual(report["matches"], int(not failed))
                self.assertEqual(report["results"][0]["error_code"], "AUDITOR_ERROR" if failed else None)
                self.assertNotIn("PRIVATE", output.read_text())
