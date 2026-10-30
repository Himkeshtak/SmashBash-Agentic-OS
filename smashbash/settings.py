"""Read local configuration without executing .env content or logging credentials."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .model import TransactionError


def read_env(path: Path) -> dict[str, str]:
    if path.is_symlink():
        raise TransactionError("env file must not be a symbolic link")
    if not path.exists():
        return {}
    if not path.is_file() or path.stat().st_size > 65536:
        raise TransactionError("env file is not a supported regular configuration file")
    values = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeError:
        raise TransactionError("env file must use UTF-8 encoding") from None
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
            raise TransactionError(f"invalid or duplicate env setting on line {number}")
        if value.startswith(('"', "'")):
            quote = value[0]
            end = value.find(quote, 1)
            if end < 0 or (value[end + 1:].strip() and not value[end + 1:].lstrip().startswith("#")):
                raise TransactionError(f"invalid quoted env setting on line {number}")
            value = value[1:end]  # Literal text: no interpolation, escapes, or shell evaluation.
        else:
            value = value.split(" #", 1)[0].rstrip()
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise TransactionError(f"control character in env setting on line {number}")
        values[key] = value
    return values


def integer(values, key, default, minimum, maximum):
    raw = values.get(key, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise TransactionError(f"{key} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise TransactionError(f"{key} is outside its supported range")
    return value


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    base_url: str
    api_key: str = field(default="", repr=False)
    response_format: str = "json_object"
    timeout_seconds: int = 60
    max_tokens: int = 4096
    free_only: bool = True

    def validate(self):
        if self.provider not in {"openrouter", "ollama"}:
            raise TransactionError("LLM_PROVIDER must be openrouter or ollama")
        if (not self.model or len(self.model) > 200
                or any(ord(c) < 33 or ord(c) > 126 for c in self.model)):
            raise TransactionError("LLM_MODEL must name a model available at the selected provider")
        try:
            url = urlsplit(self.base_url)
            port = url.port
        except ValueError as exc:
            raise TransactionError("invalid LLM_BASE_URL") from exc
        if url.username or url.password or url.query or url.fragment:
            raise TransactionError("LLM_BASE_URL must not contain credentials, query parameters, or fragments")
        if self.provider == "openrouter":
            if url.scheme != "https" or url.hostname != "openrouter.ai" or port not in {None, 443} or url.path.rstrip("/") != "/api/v1":
                raise TransactionError("OpenRouter requires https://openrouter.ai/api/v1")
            if self.free_only and self.model != "openrouter/free" and not self.model.endswith(":free"):
                raise TransactionError("OPENROUTER_FREE_ONLY blocks this model; select openrouter/free or a :free model")
        elif (url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1"}
              or url.path.rstrip("/") != "/v1"):
            raise TransactionError("local Ollama requires a loopback HTTP URL ending in /v1")
        if self.response_format not in {"json_object", "json_schema", "none"}:
            raise TransactionError("LLM_RESPONSE_FORMAT must be json_object, json_schema, or none")
        if not 1 <= self.timeout_seconds <= 300 or not 256 <= self.max_tokens <= 16384:
            raise TransactionError("invalid LLM timeout or token budget")
        if any(ord(c) < 33 or ord(c) > 126 for c in self.api_key):
            raise TransactionError("API key contains unsupported characters")


@dataclass(frozen=True)
class AgentSettings:
    llm: LLMConfig
    image: Path
    policy: Path
    state_dir: Path
    kernel: Path
    qemu: str
    principal: str

    @classmethod
    def load(cls, env_file: Path, *, environ=None):
        values = read_env(env_file)
        values.update(os.environ if environ is None else environ)  # Environment takes precedence.
        provider = values.get("LLM_PROVIDER", "openrouter")
        local = provider == "ollama"
        free = values.get("OPENROUTER_FREE_ONLY", "true").lower()
        if free not in {"true", "false"}:
            raise TransactionError("OPENROUTER_FREE_ONLY must be true or false")
        llm = LLMConfig(
            provider=provider,
            model=values.get("LLM_MODEL", "" if local else "openrouter/free"),
            base_url=values.get("LLM_BASE_URL", "http://127.0.0.1:11434/v1" if local else "https://openrouter.ai/api/v1"),
            api_key="" if local else values.get("OPENROUTER_API_KEY", ""),
            response_format=values.get("LLM_RESPONSE_FORMAT", "json_object"),
            timeout_seconds=integer(values, "LLM_TIMEOUT_SECONDS", 60, 1, 300),
            max_tokens=integer(values, "LLM_MAX_TOKENS", 4096, 256, 16384),
            free_only=free == "true",
        )
        llm.validate()
        root = env_file.absolute().parent

        def path(key, default):
            result = Path(values.get(key, default))
            return result if result.is_absolute() else root / result

        principal = values.get("SMASHBASH_PRINCIPAL", "local-user")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", principal):
            raise TransactionError("invalid trusted SMASHBASH_PRINCIPAL")
        return cls(llm, path("SMASHBASH_IMAGE", "data/device.img"),
                   path("SMASHBASH_POLICY", "config/policy.example.json"),
                   path("SMASHBASH_STATE_DIR", ".smashbash"),
                   path("SMASHBASH_KERNEL", "xv6/kernel/kernel"),
                   values.get("SMASHBASH_QEMU", "qemu-system-riscv64"), principal)
