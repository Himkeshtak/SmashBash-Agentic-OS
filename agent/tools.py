class ToolDispatcher:
    """Dispatch approved tool requests to their implementations."""

    def __init__(self, bridge=None):
        self.bridge = bridge

        if bridge is None:
            self.tools = {
                "list_files": self._mock_list_files,
                "read_file": self._mock_read_file,
            }
        else:
            self.tools = {
                "list_files": bridge.list_files,
                "read_file": bridge.read_file,
            }

    @staticmethod
    def _mock_list_files():
        return {
            "ok": True,
            "files": ["README", "report.txt", "inbox.txt"],
            "source": "mock",
        }

    @staticmethod
    def _mock_read_file(path):
        return {
            "ok": True,
            "path": path,
            "content": f"Mock content for {path}",
            "source": "mock",
        }

    def execute(self, action):
        tool_name = action["tool"]
        args = action["args"]

        if tool_name not in self.tools:
            raise ValueError(f"Unregistered tool: {tool_name}")

        return self.tools[tool_name](**args)