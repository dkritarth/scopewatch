import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, Mock, patch

from scripts.check_auditor_report import check_report
from src.cot_capture import DefaultReasoningCapture
from src.models import TaskScope
from src.openrouter_backend import OpenRouterAuditorBackend
from src.pipeline import AuditPipeline
from src.scope_auditor import ScopeAuditor


class TestDiagnostics(unittest.TestCase):
    def run_pipeline(self, backend):
        return AuditPipeline(DefaultReasoningCapture(), ScopeAuditor(backend)).process(
            '<thinking>Inspect tests</thinking>', 'synthetic', TaskScope(task_description='Fix tests')
        )

    def test_classification_error_codes(self):
        valid = dict(status='HOLD', confidence=0.8, reason='Assessment', flagged_excerpts=[])
        cases = [
            ('PRIVATE invalid JSON', 'MALFORMED_JSON'),
            ('[]', 'MALFORMED_SHAPE'),
            (json.dumps({**valid, 'status': 'PRIVATE'}), 'INVALID_CLASSIFICATION'),
            (json.dumps({**valid, 'confidence': True}), 'INVALID_CLASSIFICATION'),
            (json.dumps({**valid, 'reason': ' '}), 'INVALID_CLASSIFICATION'),
            (json.dumps({**valid, 'error_code': None}), 'INVALID_CLASSIFICATION'),
            (json.dumps({**valid, 'flagged_excerpts': ['PRIVATE']}), 'UNGROUNDED_EXCERPTS'),
            (json.dumps({**valid, 'flagged_excerpts': [' ']}), 'UNGROUNDED_EXCERPTS'),
        ]
        for response, code in cases:
            with self.subTest(code=code, response=response):
                result = self.run_pipeline(Mock(evaluate=Mock(return_value=response)))
                self.assertEqual(result.error_code, code)
                self.assertEqual(result.status, 'HOLD')
                self.assertEqual(result.confidence, 0)
                self.assertEqual(result.flagged_excerpts, [])
                self.assertNotIn('PRIVATE', result.model_dump_json())
        self.assertIsNone(self.run_pipeline(Mock(evaluate=Mock(return_value=json.dumps(valid)))).error_code)

    def test_provider_error_codes_survive_pipeline(self):
        cases = [
            (b'PRIVATE', 'MALFORMED_JSON'),
            (b'\xff', 'MALFORMED_JSON'),
            (b'[]', 'MALFORMED_SHAPE'),
            (b'{"choices": [null]}', 'MALFORMED_SHAPE'),
            (b'{"error": {"message": "PRIVATE"}}', 'PROVIDER_ERROR'),
            (b'{"error": {}}', 'PROVIDER_ERROR'),
            (json.dumps({'choices': [{'finish_reason': 'length', 'message': {'content': 'PRIVATE'}}]}).encode(), 'INCOMPLETE_RESPONSE'),
            (json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': '{}', 'refusal': 'PRIVATE'}}]}).encode(), 'REFUSED_RESPONSE'),
            (json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': None}}]}).encode(), 'MALFORMED_SHAPE'),
        ]
        for body, code in cases:
            with self.subTest(code=code, body=body):
                response = MagicMock()
                response.__enter__.return_value = response
                response.read.return_value = body
                with patch('src.openrouter_backend.urllib.request.urlopen', return_value=response) as send:
                    result = self.run_pipeline(OpenRouterAuditorBackend('synthetic'))
                self.assertEqual(result.error_code, code)
                self.assertEqual(result.status, 'HOLD')
                self.assertNotIn('PRIVATE', result.model_dump_json())
                send.assert_called_once()

    def test_transport_error_code_and_exception_redaction(self):
        with patch('src.openrouter_backend.urllib.request.urlopen', side_effect=TimeoutError('PRIVATE')):
            backend = OpenRouterAuditorBackend('synthetic')
            with self.assertRaises(RuntimeError) as raised:
                backend.evaluate('PRIVATE')
            self.assertEqual(raised.exception.error_code, 'TRANSPORT_ERROR')
            self.assertNotIn('PRIVATE', repr(raised.exception))
            self.assertEqual(self.run_pipeline(backend).error_code, 'TRANSPORT_ERROR')

    def test_checker_rejects_untrusted_shapes_without_echoing_values(self):
        reports = [None, [], {}, {'results': None}, {'results': {}}, {'results': [None]},
                   {'results': [{'id': ['PRIVATE']}]}, {'results': [{'id': 'PRIVATE'}]}]
        for report in reports:
            with self.subTest(report=report), self.assertRaises(ValueError) as raised:
                check_report(report)
            self.assertNotIn('PRIVATE', str(raised.exception))

    def test_checker_ignores_untrusted_match_fields_and_rejects_invalid_rows(self):
        cases = json.loads((Path(__file__).resolve().parents[1] / 'fixtures/auditor_cases.json').read_text())
        rows = [dict(id=case['id'], actual=case['expected'], error_code=None) for case in cases]
        check_report(dict(results=rows, matches='PRIVATE', cases=0))
        corruptions = [
            rows[:-1], rows + [rows[0]], rows[:-1] + [rows[0]],
            [None] + rows[1:],
            [{**rows[0], 'id': ['PRIVATE']}] + rows[1:],
            [{**rows[0], 'id': 'PRIVATE'}] + rows[1:],
            [{**rows[0], 'actual': 'PRIVATE'}] + rows[1:],
            [{**rows[0], 'actual': []}] + rows[1:],
            [{'id': rows[0]['id'], 'error_code': None}] + rows[1:],
            [{'id': rows[0]['id'], 'actual': rows[0]['actual']}] + rows[1:],
            [{**rows[0], 'error_code': False}] + rows[1:],
            [{**rows[0], 'error_code': 'PRIVATE'}] + rows[1:],
            [{**rows[0], 'error_code': 'TRANSPORT_ERROR'}] + rows[1:],
            [{**rows[0], 'error_type': 'PRIVATE'}] + rows[1:],
            [{**rows[0], 'actual': 'HOLD', 'expected': 'HOLD', 'match': True}] + rows[1:],
        ]
        for corrupt in corruptions:
            with self.subTest(rows=corrupt):
                with self.assertRaises(ValueError) as raised:
                    check_report(dict(results=corrupt, matches=len(rows), cases=len(rows)))
                self.assertNotIn('PRIVATE', str(raised.exception))

    def test_checker_distinguishes_model_hold_from_fallback_hold(self):
        with patch('scripts.check_auditor_report.Path.read_text', return_value='[{"id":"synthetic","expected":"HOLD"}]'):
            row = dict(id='synthetic', actual='HOLD', error_code=None, match=True)
            check_report(dict(results=[row]))
            row['error_code'] = 'TRANSPORT_ERROR'
            with self.assertRaises(ValueError):
                check_report(dict(results=[row]))
