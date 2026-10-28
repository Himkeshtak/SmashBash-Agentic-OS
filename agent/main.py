import argparse

from validator import validate_action, ValidationError
from tools import ToolDispatcher
from bridge import QEMUBridge, BridgeError


class Agent:
    def __init__(self, dispatcher):
        self.dispatcher = dispatcher

    def plan(self, request):
        """Convert a user request into a structured tool call."""

        request = request.strip()

        if request.lower() in {"list", "list files", "show files"}:
            return {
                "tool": "list_files",
                "args": {},
            }

        if request.lower().startswith("read "):
            path = request[5:].strip()

            return {
                "tool": "read_file",
                "args": {"path": path},
            }

        raise ValueError(
            "Unsupported request. Try 'list' or 'read README'."
        )

    def run(self, request):
        """Plan, validate, execute, and return an observation."""

        try:
            action = self.plan(request)
            print(f"\nProposed action: {action}")

            validate_action(action)
            print("Validation: PASSED")

            result = self.dispatcher.execute(action)

            print(f"Observation: {result}")
            return result

        except (ValueError, ValidationError, BridgeError) as exc:
            print(f"Request rejected or failed: {exc}")

            return {
                "ok": False,
                "error": str(exc),
            }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--qemu",
        action="store_true",
        help="Connect the agent to xv6 running in QEMU.",
    )
    args = parser.parse_args()

    bridge = None

    try:
        if args.qemu:
            bridge = QEMUBridge()
            bridge.start()

            print("Connected to xv6.")
            dispatcher = ToolDispatcher(bridge=bridge)
        else:
            print("Running with mock tools.")
            dispatcher = ToolDispatcher()

        agent = Agent(dispatcher)

        print("Smash Bash Agent")
        print("Commands: list, read README, quit")

        while True:
            try:
                request = input("\nYou> ").strip()
            except (EOFError, KeyboardInterrupt):
                break

            if request.lower() in {"quit", "exit"}:
                break

            agent.run(request)

    except BridgeError as exc:
        print(f"Bridge startup failed: {exc}")

    finally:
        if bridge is not None:
            bridge.close()

        print("\nAgent stopped.")


if __name__ == "__main__":
    main()