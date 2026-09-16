"""Framework-neutral agent execution and snapshot errors."""


class DurableCheckpointUnavailable(RuntimeError):
    """A snapshot-required run cannot guarantee process-restart resume."""
