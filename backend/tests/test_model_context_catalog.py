from types import SimpleNamespace
import pytest
from model_runtime.context import catalog_context_windows, context_limit_tokens, projection_budget_tokens


def test_exact_catalog_metadata_is_scoped_to_provider_and_model():
    windows = catalog_context_windows([
        {"id": "upstage/solar-mini4", "context_length": 524288},
        {"id": "stepfun/step-5-preview:free", "context_length": 1000000},
        {"name": "models/gemini-test", "inputTokenLimit": 600000},
    ])
    router = SimpleNamespace(cfg={}, _model_context_windows={"hermes": windows})
    route = {"mode": "cloud", "provider": "hermes", "model": "upstage/solar-mini4:free"}
    assert context_limit_tokens(router, route) == 0  # Paid row does not qualify the alias.
    assert projection_budget_tokens(router, route) == 32768
    route["model"] = "stepfun/step-5-preview:free"
    assert context_limit_tokens(router, route) == 1000000
    route["provider"] = "another-provider"
    assert context_limit_tokens(router, route) == 0
    router.cfg = {"cloud": {"context_windows": {"another-provider/stepfun/step-5-preview:free": 131072}}}
    assert context_limit_tokens(router, route) == 131072


@pytest.mark.parametrize("value", [True, -1, 0, 0.5, "524288", float("nan"), 2**63, None])
def test_catalog_does_not_invent_capacity_from_invalid_metadata(value):
    assert catalog_context_windows([{"id": "model", "context_length": value}]) == {}


def test_explicit_override_wins_over_fresh_catalog():
    router = SimpleNamespace(cfg={"cloud": {"context_windows": {"hermes/model": 16000}}},
                             _model_context_windows={"hermes": {"model": 524288}})
    assert context_limit_tokens(router, {"mode": "cloud", "provider": "hermes", "model": "model"}) == 16000
