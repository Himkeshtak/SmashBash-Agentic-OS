"""Real OpenRouter / local Ollama inference using their chat-completions HTTP API."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .model import TransactionError
from .settings import LLMConfig

MAX_RESPONSE_BYTES = 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise urllib.error.HTTPError(request.full_url, code, "redirect refused", headers, fp)


class LLMClient:
    def __init__(self, config: LLMConfig):
        config.validate()
        self.config = config
        handlers = [NoRedirect()]
        if config.provider == "ollama":
            handlers.append(urllib.request.ProxyHandler({}))
        self.opener = urllib.request.build_opener(*handlers)

    def complete(self, messages: list[dict], *, schema: dict) -> str:
        config = self.config
        if config.provider == "openrouter" and not config.api_key:
            raise TransactionError("Add your own OPENROUTER_API_KEY to the local .env file, or configure Ollama")
        payload = {"model": config.model, "messages": messages, "stream": False,
                   "max_tokens": config.max_tokens, "temperature": 0}
        if config.response_format == "json_schema":
            payload["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "filesystem_plan", "strict": True, "schema": schema}}
        elif config.response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "SmashBash-Agent/0.1"}
        if config.provider == "openrouter":
            headers["Authorization"] = "Bearer " + config.api_key
        request = urllib.request.Request(config.base_url.rstrip("/") + "/chat/completions",
                                         data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with self.opener.open(request, timeout=config.timeout_seconds) as response:
                if response.status != 200:
                    raise TransactionError("LLM provider returned an unexpected HTTP status")
                data = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            exc.close()
            reasons = {401: "authentication failed; check your local API key",
                       402: "provider credits are unavailable for this request",
                       429: "provider rate limit reached; try again later"}
            reason = reasons.get(exc.code, "request failed; check model availability and response-format support")
            raise TransactionError(f"LLM HTTP {exc.code}: {reason}") from None
        except (urllib.error.URLError, OSError):
            raise TransactionError("LLM connection failed or timed out; check the configured provider/server") from None
        if len(data) > MAX_RESPONSE_BYTES:
            raise TransactionError("LLM response exceeded its byte budget")
        try:
            document = json.loads(data)
            choice = document["choices"][0]
            message = choice["message"]
            content = message.get("content")
            if choice.get("finish_reason") != "stop" or message.get("refusal"):
                raise TransactionError("LLM response was incomplete or refused; no operations will execute")
            if not isinstance(content, str) or not content.strip() or message.get("tool_calls"):
                raise TransactionError("LLM did not return a complete JSON operation plan")
            return content
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise TransactionError("LLM returned an invalid chat-completion envelope") from None
