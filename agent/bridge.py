"""Compatibility imports. Execution always targets a private xv6 image."""

from smashbash.bridge import Prepared, QEMUBridge, QEMUConfig
from smashbash.model import TransactionError as BridgeError

__all__ = ["BridgeError", "Prepared", "QEMUBridge", "QEMUConfig"]
