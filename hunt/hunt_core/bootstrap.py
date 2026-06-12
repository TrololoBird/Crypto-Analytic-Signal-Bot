"""Bootstrap hunt_core on import path (delegates to hunt_watch during migration)."""

from hunt_watch.bootstrap import bootstrap

__all__ = ["bootstrap"]
