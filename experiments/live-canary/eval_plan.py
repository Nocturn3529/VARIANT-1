"""Canonical Phase 12 model order, boards, gates, and route coordinates.

This module contains no provider I/O and no runner side effects. Both live
canaries and the ordered sequence runner import it so model names, adapters,
case groups, and pass gates cannot drift independently.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Any


PLAN_SCHEMA = "variant1.astb.phase12-eval-plan.v2"


@dataclass(frozen=True)
class ModelSpec:
    key: str
    display_name: str
    mode: str
    provider: str
    model: str
    adapter: str
    tier: str
    mutation_authoring_gate: bool = False
    desktop_route_only_allowed: bool = False

    @property
    def cloud(self) -> bool:
        return self.mode == "cloud"

    def route(self) -> dict[str, str]:
        return {
            "mode": self.mode,
            "provider": self.provider,
            "model": self.model,
        }


MODEL_SPECS: tuple[ModelSpec, ...] = (
    ModelSpec(
        key="x-preview",
        display_name="OpenCode x-preview-f-free",
        mode="cloud",
        provider="opencode-zen",
        model="x-preview-f-free",
        adapter="openai.chat_completions",
        tier="frontier_unlimited",
        mutation_authoring_gate=True,
    ),
    ModelSpec(
        key="grok",
        display_name="Grok 4.6",
        mode="cloud",
        provider="xai",
        model="grok-4.6",
        adapter="xai.responses",
        tier="frontier",
        mutation_authoring_gate=True,
    ),
    ModelSpec(
        key="solar",
        display_name="Solar Pro 4 Free (Hermes/Nous)",
        mode="cloud",
        provider="hermes",
        model="upstage/solar-pro4:free",
        adapter="openai.chat_completions",
        tier="medium_cloud",
    ),
    ModelSpec(
        key="gemma",
        display_name="Gemma 4 31B (Ollama Cloud)",
        mode="cloud",
        provider="ollama",
        model="gemma4:31b-cloud",
        adapter="openai.chat_completions",
        tier="medium_cloud",
        mutation_authoring_gate=True,
    ),
    ModelSpec(
        key="qwen",
        display_name="Qwen 3.5 4B (local llama.cpp)",
        mode="local",
        provider="local",
        model="Qwen3.5-4B-BF16.gguf",
        adapter="llama_cpp.chat_completions",
        tier="small_local",
        desktop_route_only_allowed=True,
    ),
)

MODEL_ORDER: tuple[str, ...] = tuple(spec.key for spec in MODEL_SPECS)
MODEL_BY_KEY: dict[str, ModelSpec] = {spec.key: spec for spec in MODEL_SPECS}
MODEL_ROUTES: dict[str, dict[str, str]] = {
    spec.key: spec.route() for spec in MODEL_SPECS
}
EXPECTED_ADAPTER: dict[str, str] = {
    spec.key: spec.adapter for spec in MODEL_SPECS
}
CLOUD_MODEL_ORDER: tuple[str, ...] = tuple(
    spec.key for spec in MODEL_SPECS if spec.cloud
)


def frozen_kernel_path(backend: str | Path) -> Path:
    """Return the one-directory kernel executable paired with a frozen backend."""

    return Path(backend).resolve().parent / "kernel" / "Variant1Kernel.exe"


# Mutation-Off qualification covers each current category, immutable base
# controls, persistent Python state, returned/durable work, and external fixtures.
STATIC_CORE_CASES: tuple[str, ...] = ("S1", "D4", "F8", "MIX")
STATIC_DOMAIN_CASES: tuple[str, ...] = (
    "CMD",       # Build: command/Git plus patch/read evidence
    "ART",       # Build: versioned Artifact runtime
    "WEB",       # Explore: exact static fetch
    "BRW",       # Explore: visible browser-host protocol
    "DESK",      # Integrate -> Operate desktop fallback
    "KRN",       # Immutable session + persistent kernel continuity
    "CHILD",     # Coordinate children + Work jobs
    "MCP",       # Integrate connector schema lease and invocation
)

# Static controls with Mutation available must not draft or activate anything.
MUTATION_AVAILABLE_CONTROL_CASES: tuple[str, ...] = ("S1", "MIX")

# Two legal held-out shapes: create a vacancy and mutate an occupied seed.
MUTATION_GAP_CASES: tuple[str, ...] = ("GAPC", "GAPM")

ALL_CASES: tuple[str, ...] = tuple(dict.fromkeys(
    (*STATIC_CORE_CASES, *STATIC_DOMAIN_CASES, *MUTATION_GAP_CASES)
))
MANIFEST_SOURCES: tuple[str, ...] = (
    "chat", "subagent", "automation", "curator",
)


@dataclass(frozen=True)
class StageSpec:
    stage_id: str
    runner: str  # canary | manifests
    mutation: bool = False
    cases: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    required: bool = True
    purpose: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def stages_for_model(
    model_key: str,
    *,
    include_optional_mutation: bool = False,
) -> tuple[StageSpec, ...]:
    spec = MODEL_BY_KEY[model_key]
    stages: list[StageSpec] = [
        StageSpec(
            "provider-manifests",
            "manifests",
            sources=MANIFEST_SOURCES,
            purpose="Exact provider/model/adapter manifests across every model source.",
        ),
        StageSpec(
            "static-core-off",
            "canary",
            cases=STATIC_CORE_CASES,
            purpose="Mutation-Off static file/planning/fan-out/editing controls.",
        ),
        StageSpec(
            "static-domains-off",
            "canary",
            cases=STATIC_DOMAIN_CASES,
            purpose="Mutation-Off coverage of every current ASTB category and durable path.",
        ),
    ]
    if spec.cloud:
        stages.extend((
            StageSpec(
                "mutation-available-static-control",
                "canary",
                mutation=True,
                cases=MUTATION_AVAILABLE_CONTROL_CASES,
                purpose=(
                    "Mutation is available and may remain unused when no reusable "
                    "trajectory improvement emerges."
                ),
            ),
            StageSpec(
                "mutation-off-gap-control",
                "canary",
                cases=MUTATION_GAP_CASES,
                purpose="Held-out gaps fail closed without mutation or Python/shell bypass.",
            ),
        ))
    if spec.mutation_authoring_gate or (include_optional_mutation and spec.cloud):
        stages.append(StageSpec(
            "mutation-on-held-out",
            "canary",
            mutation=True,
            cases=MUTATION_GAP_CASES,
            required=spec.mutation_authoring_gate,
            purpose=(
                "Create/mutate, activate, reuse on a held-out variant, then reset to "
                "the immutable baseline."
            ),
        ))
    return tuple(stages)


def plan_document(*, include_optional_mutation: bool = False) -> dict[str, Any]:
    models = []
    for spec in MODEL_SPECS:
        models.append({
            **asdict(spec),
            "cloud": spec.cloud,
            "route": spec.route(),
            "stages": [
                stage.to_dict()
                for stage in stages_for_model(
                    spec.key,
                    include_optional_mutation=include_optional_mutation,
                )
            ],
        })
    document = {
        "schema": PLAN_SCHEMA,
        "model_order": list(MODEL_ORDER),
        "cloud_gate": {
            "models": list(CLOUD_MODEL_ORDER),
            "qwen_runs_only_after_all_cloud_required_stages_pass": True,
        },
        "repair_loop": {
            "canary_case_isolation": "one_backend_per_case",
            "resume_skips_passed_cases": True,
            "failed_case_retried_without_replaying_stage": True,
            "full_backend_suite_after_each_fix": False,
        },
        "cases": {
            "static_core": list(STATIC_CORE_CASES),
            "static_domains": list(STATIC_DOMAIN_CASES),
            "mutation_available_control": list(MUTATION_AVAILABLE_CONTROL_CASES),
            "mutation_gaps": list(MUTATION_GAP_CASES),
        },
        "models": models,
    }
    encoded = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    document["plan_sha256"] = hashlib.sha256(encoded).hexdigest()
    return document


def validate_plan() -> None:
    if MODEL_ORDER != ("x-preview", "grok", "solar", "gemma", "qwen"):
        raise RuntimeError("Phase 12 model order changed without an explicit plan revision")
    if len(MODEL_BY_KEY) != len(MODEL_SPECS):
        raise RuntimeError("Phase 12 model keys must be unique")
    if MODEL_ORDER[-1] != "qwen" or MODEL_BY_KEY["qwen"].cloud:
        raise RuntimeError("local Qwen must remain the final gated model")
    if set(MUTATION_GAP_CASES) != {"GAPC", "GAPM"}:
        raise RuntimeError("both legal mutation shapes must remain in the held-out board")
    if set(STATIC_CORE_CASES) & set(MUTATION_GAP_CASES):
        raise RuntimeError("static and held-out mutation case IDs must be disjoint")
    if not all(MODEL_BY_KEY[key].cloud for key in CLOUD_MODEL_ORDER):
        raise RuntimeError("the pre-Qwen gate may contain only cloud routes")


validate_plan()


__all__ = [
    "ALL_CASES",
    "CLOUD_MODEL_ORDER",
    "EXPECTED_ADAPTER",
    "frozen_kernel_path",
    "MANIFEST_SOURCES",
    "MODEL_BY_KEY",
    "MODEL_ORDER",
    "MODEL_ROUTES",
    "MODEL_SPECS",
    "MUTATION_AVAILABLE_CONTROL_CASES",
    "MUTATION_GAP_CASES",
    "PLAN_SCHEMA",
    "STATIC_CORE_CASES",
    "STATIC_DOMAIN_CASES",
    "ModelSpec",
    "StageSpec",
    "plan_document",
    "stages_for_model",
    "validate_plan",
]
