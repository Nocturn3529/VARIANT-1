"""Network-free router fixture using the production route-binding contract."""
from types import SimpleNamespace
from llm_router import LLMRouter


class RouteAwareRouter:
    bind_model_route = LLMRouter.bind_model_route
    bound_model_route = LLMRouter.bound_model_route

    def __init__(self, *, mode="cloud"):
        self.mode = mode
        self.cloud_provider = "openai-codex"
        self.model_name = "gpt-5.6-luna"
        self.cfg = {}
        self.engine_ready = True

    def get_cloud_model(self, provider):
        return self.model_name

    def provider_profile(self, provider):
        return SimpleNamespace(reasoning_efforts=("low", "medium", "high", "xhigh", "max"))

    def cloud_route_ready(self):
        return True
