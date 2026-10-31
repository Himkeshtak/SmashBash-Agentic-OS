import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from smashbash.model import TransactionError
from smashbash.settings import AgentSettings, LLMConfig, read_env


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / ".env"

    def tearDown(self):
        self.temp.cleanup()

    def test_defaults_env_precedence_and_relative_runtime_paths(self):
        self.path.write_text("LLM_MODEL=some/model:free\nOPENROUTER_API_KEY=fixture-secret\n")
        settings = AgentSettings.load(self.path, environ={"LLM_MODEL": "openrouter/free"})
        self.assertEqual(settings.llm.model, "openrouter/free")
        self.assertEqual(settings.image, self.root / "data/device.img")
        self.assertNotIn("fixture-secret", repr(settings))

    def test_quotes_and_literal_values_do_not_execute_or_expand(self):
        self.path.write_text('# comment\nNAME="C:\\Some Folder\\file" # note\nVALUE=$(not-a-command)\n')
        values = read_env(self.path)
        self.assertEqual(values["NAME"], "C:\\Some Folder\\file")
        self.assertEqual(values["VALUE"], "$(not-a-command)")

    def test_invalid_env_errors_do_not_reveal_values(self):
        for content in ("KEY=secret\nKEY=another-secret", 'KEY="secret', "secret-no-equals"):
            self.path.write_text(content)
            with self.subTest(content=content[:3]), self.assertRaises(TransactionError) as raised:
                read_env(self.path)
            self.assertNotIn("secret", str(raised.exception))

    def test_free_only_and_endpoint_safety(self):
        baseline = LLMConfig("openrouter", "openrouter/free", "https://openrouter.ai/api/v1")
        for changes in ({"model": "paid/model"}, {"base_url": "http://openrouter.ai/api/v1"},
                        {"base_url": "https://attacker.example/api/v1"},
                        {"base_url": "https://user:secret@openrouter.ai/api/v1"},
                        {"base_url": "https://openrouter.ai/api/v1?api_key=secret"},
                        {"provider": "ollama", "model": "local", "base_url": "http://remote.example:11434/v1"}):
            with self.subTest(changes=changes), self.assertRaises(TransactionError):
                replace(baseline, **changes).validate()
        replace(baseline, provider="ollama", model="local", base_url="http://localhost:11434/v1").validate()

    def test_ollama_does_not_load_the_openrouter_key(self):
        self.path.write_text("LLM_PROVIDER=ollama\nLLM_MODEL=local\nOPENROUTER_API_KEY=not-sent\n")
        settings = AgentSettings.load(self.path, environ={})
        self.assertEqual(settings.llm.api_key, "")
        self.assertEqual(settings.llm.base_url, "http://127.0.0.1:11434/v1")
