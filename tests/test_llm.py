import io
import json
import unittest
import urllib.error
import urllib.request
from dataclasses import replace

from smashbash.llm import LLMClient, MAX_RESPONSE_BYTES, NoRedirect
from smashbash.model import TransactionError
from smashbash.settings import LLMConfig


class Response:
    status = 200

    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, length):
        return self.data[:length]


class Endpoint:
    def __init__(self, data=None, error=None):
        self.data = data or json.dumps({"choices": [{"finish_reason": "stop", "message": {
            "content": '{"operations":[],"error":"unsupported"}'}}]}).encode()
        self.error = error
        self.requests = []

    def open(self, request, *, timeout):
        self.requests.append(request)
        if self.error:
            raise self.error
        return Response(self.data)


def config():
    return LLMConfig("openrouter", "openrouter/free", "https://openrouter.ai/api/v1", "test-key-not-a-real-secret")


class LLMTests(unittest.TestCase):
    def test_cloud_invocation_uses_expected_endpoint_auth_and_no_paid_fallback(self):
        client = LLMClient(config())
        endpoint = Endpoint()
        client.opener = endpoint
        client.complete([{"role": "user", "content": "Write a note"}], schema={})
        sent = endpoint.requests[0]
        body = json.loads(sent.data)
        self.assertEqual(sent.full_url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(sent.get_header("Authorization"), "Bearer test-key-not-a-real-secret")
        self.assertEqual(body["model"], "openrouter/free")
        self.assertNotIn("models", body)
        self.assertFalse(body["stream"])
        self.assertEqual(body["response_format"], {"type": "json_object"})

    def test_local_invocation_does_not_require_or_send_a_cloud_api_key(self):
        client = LLMClient(LLMConfig("ollama", "local-model", "http://127.0.0.1:11434/v1"))
        endpoint = Endpoint()
        client.opener = endpoint
        client.complete([], schema={})
        self.assertIsNone(endpoint.requests[0].get_header("Authorization"))
        self.assertEqual(endpoint.requests[0].full_url, "http://127.0.0.1:11434/v1/chat/completions")

    def test_missing_credentials_block_inference(self):
        client = LLMClient(replace(config(), api_key=""))
        endpoint = Endpoint()
        client.opener = endpoint
        with self.assertRaisesRegex(TransactionError, "OPENROUTER_API_KEY"):
            client.complete([], schema={})
        self.assertEqual(endpoint.requests, [])

    def test_error_messages_do_not_echo_provider_body_or_secret(self):
        for status in (401, 402, 429, 500, 302):
            secret = "test-secret-never-log-this"
            error = urllib.error.HTTPError("https://openrouter.ai/api/v1/chat/completions", status,
                                          secret, {}, io.BytesIO(secret.encode()))
            client = LLMClient(config())
            client.opener = Endpoint(error=error)
            with self.subTest(status=status), self.assertRaises(TransactionError) as raised:
                client.complete([], schema={})
            self.assertNotIn(secret, str(raised.exception))

    def test_truncation_tool_calls_and_malformed_responses_are_rejected(self):
        envelopes = [{}, {"choices": []}, {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]},
                     {"choices": [{"finish_reason": "stop", "message": {"content": "{}", "tool_calls": [{}]}}]},
                     {"choices": [{"finish_reason": "stop", "message": {"content": None}}]}]
        for value in envelopes:
            client = LLMClient(config())
            client.opener = Endpoint(data=json.dumps(value).encode())
            with self.subTest(value=value), self.assertRaises(TransactionError):
                client.complete([], schema={})
        client.opener = Endpoint(data=b"x" * (MAX_RESPONSE_BYTES + 1))
        with self.assertRaisesRegex(TransactionError, "byte budget"):
            client.complete([], schema={})

    def test_redirects_are_not_followed(self):
        with self.assertRaises(urllib.error.HTTPError):
            NoRedirect().redirect_request(urllib.request.Request("https://openrouter.ai/api/v1/chat/completions"),
                                          None, 302, "redirect", {}, "https://other.example/")

    def test_schema_mode_supplies_the_schema(self):
        client = LLMClient(replace(config(), response_format="json_schema"))
        endpoint = Endpoint()
        client.opener = endpoint
        schema = {"type": "object"}
        client.complete([], schema=schema)
        self.assertEqual(json.loads(endpoint.requests[0].data)["response_format"]["json_schema"]["schema"], schema)
