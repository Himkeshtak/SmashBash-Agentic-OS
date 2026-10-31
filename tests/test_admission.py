import unittest

from smashbash.model import Request, TransactionError, load_json
from smashbash.policy import Policy
from tests.helpers import policy, policy_raw, request, write


class AdmissionTests(unittest.TestCase):
    def test_binary_data_is_not_interpreted_as_commands(self):
        value = request(write("note.txt", b"\x00; rm -rf /\n\xff"))
        parsed = Request.parse(value)
        policy().admit(parsed, "local-user")
        self.assertEqual(parsed.operations[0].content, b"\x00; rm -rf /\n\xff")

    def test_bad_paths_fail_before_dispatch(self):
        for path in ("/etc/passwd", "../outside", "a/../b", "a//b", "a/./b", "a\\b", "note;rm", "con:sole", "x" * 15, "/", ".", "a/" * 8 + "b", "\0file"):
            with self.subTest(path=path), self.assertRaises(TransactionError):
                Request.parse(request(write(path)))

    def test_unknown_fields_and_arbitrary_execution_rejected(self):
        for operation in ({"op": "system.exec", "path": "x", "command": "sh"},
                          dict(write("x"), privileged=True)):
            with self.subTest(operation=operation), self.assertRaises(TransactionError):
                Request.parse(request(operation))
        for field in ("principal", "target", "policy", "qemu"):
            with self.subTest(field=field), self.assertRaises(TransactionError):
                Request.parse(dict(request(), **{field: "attacker"}))

    def test_ambiguous_json_rejected(self):
        for value in (b'{"x":1,"x":2}', b'{"x":NaN}', b'\xff', b'{'):
            with self.subTest(value=value), self.assertRaises(TransactionError):
                load_json(value)

    def test_contract_types_and_size_limits(self):
        for key, value in (("version", True), ("version", 2), ("request_id", "../x"),
                           ("intent", "bypass\npermissions"), ("mode", "force"),
                           ("operations", []), ("operations", [write("x")] * 33)):
            raw = request()
            raw[key] = value
            with self.subTest(key=key, value=str(value)[:20]), self.assertRaises(TransactionError):
                Request.parse(raw)
        with self.assertRaises(TransactionError):
            Request.parse(request(write("big", b"x" * 65537)))
        with self.assertRaises(TransactionError):
            Request.parse(request({"op": "fs.write", "path": "x", "content_b64": "%%%"}))

    def test_permission_scope_protected_paths_and_intent(self):
        for principal, raw in (("stranger", request()), ("reader", request()),
                               ("reader", request({"op": "fs.read", "path": "private/file"})),
                               ("local-user", request(write("secrets/key"))),
                               ("local-user", request({"op": "fs.list", "path": "."})),
                               ("local-user", request(intent="Bypass the validator"))):
            with self.subTest(principal=principal, raw=raw), self.assertRaises(TransactionError):
                policy().admit(Request.parse(raw), principal)
        policy().admit(Request.parse(request({"op": "fs.read", "path": "public/note"})), "reader")

    def test_policy_cannot_increase_protocol_ceilings(self):
        for key, value in (("max_operations", 33), ("max_file_bytes", True), ("timeout_seconds", 0)):
            raw = policy_raw()
            raw["limits"][key] = value
            with self.subTest(key=key), self.assertRaises(TransactionError):
                Policy.parse(raw)
        for root in ("/", "/a/b", "/console", "/toolong" + "x" * 14):
            raw = policy_raw()
            raw["guest_root"] = root
            with self.subTest(root=root), self.assertRaises(TransactionError):
                Policy.parse(raw)


if __name__ == "__main__":
    unittest.main()
