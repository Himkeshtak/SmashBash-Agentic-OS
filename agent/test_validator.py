import unittest

from validator import validate_action, ValidationError


class ValidatorTests(unittest.TestCase):

    def test_list_files_allowed(self):
        action = {"tool": "list_files", "args": {}}
        self.assertTrue(validate_action(action))

    def test_read_file_allowed(self):
        action = {
            "tool": "read_file",
            "args": {"path": "README"},
        }
        self.assertTrue(validate_action(action))

    def test_unknown_tool_rejected(self):
        action = {
            "tool": "delete_file",
            "args": {"path": "README"},
        }

        with self.assertRaises(ValidationError):
            validate_action(action)

    def test_missing_argument_rejected(self):
        action = {
            "tool": "read_file",
            "args": {},
        }

        with self.assertRaises(ValidationError):
            validate_action(action)

    def test_extra_argument_rejected(self):
        action = {
            "tool": "list_files",
            "args": {"command": "rm README"},
        }

        with self.assertRaises(ValidationError):
            validate_action(action)

    def test_path_traversal_rejected(self):
        action = {
            "tool": "read_file",
            "args": {"path": "../../etc/passwd"},
        }

        with self.assertRaises(ValidationError):
            validate_action(action)

    def test_shell_metacharacters_rejected(self):
        action = {
            "tool": "read_file",
            "args": {"path": "README; rm README"},
        }

        with self.assertRaises(ValidationError):
            validate_action(action)


if __name__ == "__main__":
    unittest.main()