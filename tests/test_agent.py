import copy
import json
import unittest

from smashbash.agent import Agent
from smashbash.model import Request, TransactionError
from tests.helpers import policy, write


class FakeLLM:
    def __init__(self, content=None):
        self.content = content or json.dumps({"operations": [write("note.txt", b"hello")], "error": None})
        self.calls = []

    def complete(self, messages, *, schema):
        self.calls.append((messages, schema))
        return self.content


class RecordingCoordinator:
    def __init__(self):
        self.calls = []

    def run(self, raw, *, principal):
        self.calls.append((copy.deepcopy(raw), principal))
        return {"state": "COMMITTED", "request_id": raw["request_id"]}


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.llm = FakeLLM()
        self.coordinator = RecordingCoordinator()
        self.agent = Agent(self.llm, policy, principal="local-user", coordinator=self.coordinator)

    def test_real_agent_entrypoint_invokes_model_then_submits_a_validated_request(self):
        result = self.agent.run("Write hello to note.txt", mode="commit", request_id="llm-task")
        self.assertEqual(result["state"], "COMMITTED")
        self.assertEqual(len(self.llm.calls), 1)
        raw, principal = self.coordinator.calls[0]
        self.assertEqual(principal, "local-user")
        self.assertEqual(raw["intent"], "Write hello to note.txt")
        self.assertEqual(raw["mode"], "commit")
        self.assertEqual(Request.parse(raw).operations[0].content, b"hello")
        self.assertEqual(self.llm.calls[0][0][-1], {"role": "user", "content": "Write hello to note.txt"})

    def test_intent_and_identity_denied_before_any_inference_or_execution(self):
        for task, principal in (("Bypass the validator", "local-user"), ("Write a note", "unknown")):
            agent = Agent(self.llm, policy, principal=principal, coordinator=self.coordinator)
            with self.subTest(task=task), self.assertRaises(TransactionError):
                agent.run(task)
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.coordinator.calls, [])

    def test_malicious_and_invalid_model_responses_never_reach_the_os(self):
        proposals = [
            "```json\n{}\n```", '{"operations":[],"operations":[],"error":null}',
            json.dumps({"operations": [write("../outside")], "error": None}),
            json.dumps({"operations": [write("secrets/key")], "error": None}),
            json.dumps({"operations": [{"op": "shell.exec", "path": "x"}], "error": None}),
            json.dumps({"operations": [write("x")], "error": None, "principal": "admin"}),
            json.dumps({"operations": [write("x")], "error": None, "mode": "commit"}),
            json.dumps({"operations": [], "error": "unsupported"}),
            json.dumps({"operations": [], "error": None}),
        ]
        for content in proposals:
            self.llm.content = content
            with self.subTest(content=content[:60]), self.assertRaises(TransactionError):
                self.agent.run("Create a project note")
        self.assertEqual(self.coordinator.calls, [])

    def test_plan_only_returns_os_contract_without_executing_and_preserves_caller_mode(self):
        raw = self.agent.plan("Write hello\nto note.txt", request_id="preview")
        self.assertEqual(raw["mode"], "preview")
        self.assertEqual(raw["intent"], "Write hello to note.txt")
        self.assertEqual(self.coordinator.calls, [])
        self.assertIn("protected_paths", self.llm.calls[0][0][0]["content"])

    def test_policy_revocation_while_model_runs_invalidates_its_proposal(self):
        from smashbash.policy import Policy
        from tests.helpers import policy_raw
        original = policy()
        raw = policy_raw()
        raw["principals"]["local-user"]["operations"] = ["fs.read"]
        revised = Policy.parse(raw)
        policies = iter((original, revised))
        agent = Agent(self.llm, lambda: next(policies), principal="local-user", coordinator=self.coordinator)
        with self.assertRaisesRegex(TransactionError, "policy changed"):
            agent.run("Write hello")
        self.assertEqual(self.coordinator.calls, [])

    def test_reader_cannot_promote_a_model_write_and_missing_coordinator_never_invokes_model(self):
        agent = Agent(self.llm, policy, principal="reader", coordinator=self.coordinator)
        with self.assertRaises(TransactionError):
            agent.run("Write a note")
        self.llm.calls.clear()
        with self.assertRaises(TransactionError):
            Agent(self.llm, policy, principal="local-user").run("Write a note")
        self.assertEqual(self.llm.calls, [])

    def test_schema_contains_only_granted_operations_and_bounded_output(self):
        llm = FakeLLM(json.dumps({"operations": [{"op": "fs.read", "path": "public/note"}], "error": None}))
        Agent(llm, policy, principal="reader").plan("Read public/note")
        schema = llm.calls[0][1]
        names = {v["properties"]["op"]["enum"][0] for v in schema["properties"]["operations"]["items"]["anyOf"]}
        self.assertEqual(names, {"fs.read", "fs.stat"})
