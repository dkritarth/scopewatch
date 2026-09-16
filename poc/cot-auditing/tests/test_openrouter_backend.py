import io
import json
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from src.openrouter_backend import OpenRouterAuditorBackend


class TestOpenRouterBackend(unittest.TestCase):
    def response(self, message=None, finish_reason="stop"):
        return {"choices": [{"finish_reason": finish_reason,
                             "message": message or {"content": '{"status":"HOLD"}'}}]}

    def test_posts_separate_system_instructions_and_json_data(self):
        response = MagicMock()
        response.read.return_value = json.dumps(self.response()).encode()
        response.__enter__.return_value = response
        with patch("src.openrouter_backend.urllib.request.urlopen", return_value=response) as send:
            backend = OpenRouterAuditorBackend("synthetic-key")
            self.assertEqual(backend.evaluate('{"trace":"data"}'), '{"status":"HOLD"}')
        request = send.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertEqual(payload["messages"][1], {"role": "user", "content": '{"trace":"data"}'})
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertFalse(payload["provider"]["allow_fallbacks"])
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-key")
        self.assertEqual(send.call_args.kwargs["timeout"], 60)

    def test_rejects_bad_and_incomplete_responses(self):
        for body in [None, [], {}, {"error": {"code": 502, "message": "PRIVATE"}},
                     {"choices": [None]}, self.response(finish_reason="length"),
                     self.response({"content": None}), self.response({"content": " "}),
                     self.response({"content": "{}", "refusal": "refused"})]:
            with self.subTest(body=body):
                response = MagicMock()
                response.read.return_value = json.dumps(body).encode()
                response.__enter__.return_value = response
                with patch("src.openrouter_backend.urllib.request.urlopen", return_value=response):
                    with self.assertRaises(RuntimeError):
                        OpenRouterAuditorBackend("synthetic-key").evaluate("{}")

    def test_rejects_empty_key(self):
        with self.assertRaises(ValueError):
            OpenRouterAuditorBackend(" ")

    def test_rejects_nonpositive_timeout(self):
        with self.assertRaises(ValueError):
            OpenRouterAuditorBackend("synthetic-key", timeout=0)

    def test_oversized_response_is_rejected(self):
        response = MagicMock()
        response.read.return_value = b"x" * 1_048_577
        response.__enter__.return_value = response
        with patch("src.openrouter_backend.urllib.request.urlopen", return_value=response):
            with self.assertRaises(RuntimeError):
                OpenRouterAuditorBackend("synthetic-key").evaluate("{}")
        response.read.assert_called_once_with(1_048_577)

    def test_http_failure_is_redacted_and_not_retried(self):
        error = urllib.error.HTTPError("https://openrouter.ai", 429, "PRIVATE", {}, io.BytesIO(b"PRIVATE"))
        with patch("src.openrouter_backend.urllib.request.urlopen", side_effect=error) as send:
            with self.assertRaises(RuntimeError) as raised:
                OpenRouterAuditorBackend("synthetic-key").evaluate("{}")
        self.assertNotIn("PRIVATE", str(raised.exception))
        send.assert_called_once()
