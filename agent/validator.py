import re


ALLOWED_TOOLS = {
    "list_files": set(),
    "read_file": {"path"},
}

PATH_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


class ValidationError(ValueError):
    """Raised when a tool request violates the validation policy."""


def validate_action(action):
    """Validate a structured tool request."""

    if not isinstance(action, dict):
        raise ValidationError("Action must be a dictionary.")

    if set(action.keys()) != {"tool", "args"}:
        raise ValidationError(
            "Action must contain exactly 'tool' and 'args'."
        )

    tool_name = action["tool"]
    args = action["args"]

    if not isinstance(tool_name, str) or tool_name not in ALLOWED_TOOLS:
        raise ValidationError(f"Tool not allowed: {tool_name}")

    if not isinstance(args, dict):
        raise ValidationError("'args' must be a dictionary.")

    required_args = ALLOWED_TOOLS[tool_name]

    if set(args.keys()) != required_args:
        raise ValidationError(
            f"Invalid arguments for tool '{tool_name}'."
        )

    if tool_name == "read_file":
        path = args["path"]

        if not isinstance(path, str):
            raise ValidationError("Path must be a string.")

        # Permit an optional leading slash.
        normalized_path = path.lstrip("/")

        if not normalized_path:
            raise ValidationError("Path cannot be empty.")

        if not PATH_PATTERN.fullmatch(normalized_path):
            raise ValidationError("Invalid path format.")

        if normalized_path in {".", ".."}:
            raise ValidationError("Invalid path.")

        args["path"] = normalized_path

    return True