"""Live transcript-economy service for VARIANT-1 agent products.

``transcript_economy`` owns the deterministic transforms.  This service owns
the process dependencies needed by those transforms: the selected model's
context budget, provider token counting, and provenance evidence for successful
compaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import TYPE_CHECKING, Callable

import llm_router
import transcript_economy
from run_context import current_run_context

if TYPE_CHECKING:
    from app_host import AppHost


StopPredicate = Callable[[], bool]


@dataclass
class TranscriptService:
    """Budget, counting, trimming, and compaction for live transcripts."""

    host: "AppHost"
    _compaction_retries: dict = field(default_factory=dict, init=False, repr=False)

    def approx_tokens(self, messages: list) -> int:
        return transcript_economy.approx_tokens(messages)

    async def count_prompt_tokens(
        self,
        messages: list,
        *,
        tools: list | None = None,
        image_b64=None,
    ) -> int | None:
        return await transcript_economy.count_prompt_tokens(
            messages,
            count_tokens=self.host.router.count_tokens,
            count_rendered_prompt=getattr(
                self.host.router, "count_prompt_tokens", None),
            tools=tools,
            image_b64=image_b64,
        )

    def compress_threshold(self) -> int:
        try:
            mode = getattr(self.host.router, "mode", "local")
            ctx = int(self.host.router.projection_budget_tokens() or 0)
        except Exception:
            mode, ctx = "local", 0
        return transcript_economy.ctx_compress_threshold(mode=mode, ctx_size=ctx)

    async def compress_messages(
        self,
        messages: list,
        protect_first: int = 2,
        protect_last: int = 6,
        should_stop: StopPredicate | None = None,
    ) -> list:
        """Compact transcript and record one provenance-tagged progress event."""
        if len(messages) <= protect_first + protect_last + 2:
            return messages

        async def _complete(prompt, **kwargs):
            return await llm_router.complete(self.host.router, prompt, **kwargs)

        goal_context = ""
        chat_id = ""
        retry_owner = ""
        try:
            rctx = current_run_context()
            if rctx is not None:
                chat_id = str(
                    getattr(getattr(rctx, "work_scope", None), "chat_id", "") or ""
                )
                retry_owner = chat_id or str(getattr(rctx, "run_id", "") or "")
                goal_id = str(
                    getattr(getattr(rctx, "work_scope", None), "goal_id", "") or ""
                )
                if goal_id:
                    goal = self.host.require_runtime().goals.get(goal_id)
                    if goal is not None:
                        goal_context = (
                            f"Goal: {goal.title}\nObjective: {goal.objective}"
                        )[:1400]
        except Exception as exc:
            print(f"[compress] goal context probe failed: {exc}", flush=True)

        retry_state = None
        if retry_owner:
            router = self.host.router
            mode = str(getattr(router, "mode", "") or "")
            model_for = getattr(router, "active_model_name", None)
            model = str(model_for(mode) or "") if callable(model_for) else ""
            provider = str(getattr(router, "cloud_provider", "") or "")
            profile_for = getattr(router, "provider_profile", None)
            profile = profile_for(provider) if callable(profile_for) else None
            from model_runtime.request_policy import project_reasoning_policy

            # Fingerprint the resolved minimum, not arbitrary plugin profile
            # objects. Diagnostics/backoff bookkeeping must not stop an agent
            # before its first model call when optional metadata is unavailable.
            minimum = {}
            try:
                effort = project_reasoning_policy(router, profile, model, minimum, 0)
                policy = hashlib.sha256(json.dumps({"effort": effort, "minimum": minimum},
                    sort_keys=True).encode("utf-8")).hexdigest()
            except Exception:
                policy = "minimum_metadata_unavailable"
            key = (retry_owner, mode, provider, model, policy)
            if key not in self._compaction_retries:
                if len(self._compaction_retries) >= 128:
                    self._compaction_retries.pop(next(iter(self._compaction_retries)))
                self._compaction_retries[key] = transcript_economy.CompactionRetryState()
            retry_state = self._compaction_retries[key]

        compacted = await transcript_economy.compress_messages(
            messages,
            complete=_complete,
            protect_first=protect_first,
            protect_last=protect_last,
            should_stop=should_stop,
            goal_context=goal_context,
            retry_state=retry_state,
        )
        # A category can change many turns after the original system prompt was
        # built. After compaction, refresh only its Working environment from
        # authoritative runtime state so a lossy summary cannot freeze an old
        # mount or invent capability availability.
        if chat_id and compacted is not messages and compacted:
            current_environment = self.host.require_runtime().catalog.runtime_prompt(
                chat_id, "", allow_auto_mount=False,
            )
            for message in compacted:
                if str(message.get("role") or "") != "system":
                    continue
                content = str(message.get("content") or "")
                marker = "\n\n## Working environment"
                split_at = content.find(marker)
                prefix = content[:split_at] if split_at >= 0 else content.rstrip()
                message["content"] = prefix.rstrip() + "\n\n" + current_environment
                break
        return compacted
