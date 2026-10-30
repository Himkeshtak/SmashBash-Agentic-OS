import base64
import unittest

from smashbash.filesystem import Node, changed_paths, evaluate, validate_tree
from smashbash.model import Request, TransactionError
from tests.helpers import policy, request, write


class FilesystemTests(unittest.TestCase):
    def test_read_own_writes_append_rename_and_empty_directory_removal(self):
        before = {".": Node("directory")}
        raw = request({"op": "fs.mkdir", "path": "notes"}, write("notes/a", b"one"),
                      write("notes/a", b"two", "fs.append"),
                      {"op": "fs.read", "path": "notes/a"},
                      {"op": "fs.rename", "path": "notes/a", "destination": "notes/b"},
                      {"op": "fs.list", "path": "notes"},
                      {"op": "fs.stat", "path": "notes/b"},
                      {"op": "fs.remove", "path": "notes/b"},
                      {"op": "fs.remove", "path": "notes"})
        after, observations = evaluate(before, Request.parse(raw), policy())
        self.assertEqual(after, before)
        self.assertEqual(base64.b64decode(observations[3]["content_b64"]), b"onetwo")
        self.assertEqual(observations[5]["entries"], ["b"])
        self.assertEqual(observations[6]["size"], 6)
        self.assertEqual(changed_paths(before, after), [])

    def test_invalid_semantics_fail_without_modifying_baseline(self):
        before = {".": Node("directory"), "dir": Node("directory"), "dir/a": Node("file", b"x")}
        for raw in (request(write("missing/a")), request(write("dir")),
                    request({"op": "fs.remove", "path": "dir"}),
                    request({"op": "fs.rename", "path": "dir", "destination": "new"}),
                    request({"op": "fs.mkdir", "path": "dir"})):
            with self.subTest(raw=raw), self.assertRaises(TransactionError):
                evaluate(before, Request.parse(raw), policy())
        self.assertEqual(before["dir/a"].data, b"x")

    def test_final_quotas_and_special_nodes(self):
        for tree in ({".": Node("directory"), "x": Node("file", b"a" * 32769)},
                     {".": Node("directory"), "x": Node("device")},
                     {".": Node("directory"), "missing/x": Node("file")},
                     {".": Node("directory", b"payload")}):
            with self.subTest(tree=str(tree)[:50]), self.assertRaises(TransactionError):
                validate_tree(tree, policy())

    def test_append_enforces_result_size(self):
        before = {".": Node("directory"), "a": Node("file", b"a" * 32768)}
        with self.assertRaises(TransactionError):
            evaluate(before, Request.parse(request(write("a", b"x", "fs.append"))), policy())
