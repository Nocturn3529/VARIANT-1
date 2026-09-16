"""Typed provider errors used by credential and provider failover."""

from model_runtime.llama_server import LocalEngineError


class ProviderRequestError(LocalEngineError):
    def __init__(
        self,
        provider: str,
        message: str,
        *,
        status_code: int = 0,
        retry_after_seconds: float | None = None,
    ):
        super().__init__(message)
        self.provider = provider
        self.status_code = int(status_code or 0)
        try:
            retry_after = (
                None
                if retry_after_seconds is None
                else max(0.0, float(retry_after_seconds))
            )
        except (TypeError, ValueError):
            retry_after = None
        self.retry_after_seconds = retry_after
        # Set only by cloud orchestration after it proves that no visible text,
        # committed tool call, or other causal model output escaped the failed
        # attempt. The agent layer may replay that logical turn once.
        self.clean_turn_replay_safe = False
        self.model_output_observed = False
