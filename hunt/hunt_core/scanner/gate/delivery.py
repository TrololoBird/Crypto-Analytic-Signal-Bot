"""
Backward-compat alias — same interface as ``gate.__init__``.

Lazy imports throughout the codebase still reference
``hunt_core.scanner.gate.delivery.<name>`` after the
scanner -> hunter refactor.
"""

from hunt_core.scanner.gate import *  # noqa: F401, F403
