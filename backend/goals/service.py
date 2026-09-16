"""Stable goal domain facade and factory."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
import weakref
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import background_tasks
from core_invariants import exhaustive_keyset_pages
from work_fabric.models import WorkActor
from work_fabric.service import WorkService

from .executor import StepExecutor, StepHandler
from .models import GoalRecord, GoalTransitionError,GoalConflict
from .repository import GoalRepository
from .verification import VerificationReport, verify_goal


class GoalService:
    def __init__(
        self,
        repository: GoalRepository,
        *,
        work: WorkService,
        handlers: Mapping[str, StepHandler] | None = None,
        wait_resolvers: Mapping[str, Callable[[Any], Any]] | None = None,
        register_work_handler: bool = True,
    ) -> None:
        self.repository = repository
        self.work = work
        self.executor = StepExecutor(handlers)
        self.wait_resolvers: dict[str, Callable[[Any], Any]] = {
            str(name): resolver
            for name, resolver in dict(wait_resolvers or {}).items()
            if str(name).strip() and callable(resolver)
        }
        self._cancellation_handler: Callable[[GoalRecord, str], Any] | None = None
        self._cleanup_locks=weakref.WeakValueDictionary()
        from .supervisor import GoalSupervisor
        self.supervisor = GoalSupervisor(self, executor=self.executor)
        from .composer import ComposerGoals
        self.composer = ComposerGoals(self)
        if register_work_handler:
            self.supervisor.register_work_handler()

    def register_wait_resolver(self, source: str, resolver: Callable[[Any], Any]) -> None:
        clean = str(source or "").strip()
        if not clean or not callable(resolver):
            raise ValueError("wait resolver source and callable are required")
        self.wait_resolvers[clean] = resolver

    def register_cancellation_handler(
        self, handler: Callable[[GoalRecord, str], Any],
    ) -> None:
        if not callable(handler):
            raise ValueError("goal cancellation handler must be callable")
        self._cancellation_handler = handler

    async def _cancel_owned_resources(self, goal: GoalRecord, reason: str, token: str='') -> None:
        lock=self._cleanup_locks.setdefault(goal.goal_id,asyncio.Lock())
        async with lock:
            pending=self.repository.state_get(goal.goal_id,'resource_cleanup') or {}
            token=token or str(pending.get('token') or '')
            if pending.get('token','')!=token:return
            if pending.get('complete') is True:return
            try:
                handler=self._cancellation_handler
                value=handler(goal,str(reason or '')) if handler else {'complete':not self.repository.list_effects(goal.goal_id)}
                if inspect.isawaitable(value):value=await value
                if hasattr(value,'to_dict'):value=value.to_dict()
                result=dict(value or {'complete':True})
                result.update(status='complete' if result.get('complete') is True else 'failed',updated_at=time.time())
            except asyncio.CancelledError:
                # Pending is already durable; a later explicit cleanup can retry.
                raise
            except Exception as exc:
                result={'status':'failed','complete':False,'updated_at':time.time(),
                        'issues':[{'phase':'cleanup','error':str(exc),'retryable':True}]}
            result['token']=token
            for _ in range(3):
                latest=self.repository.require_goal(goal.goal_id)
                current=self.repository.state_get(goal.goal_id,'resource_cleanup') or {}
                if current.get('token','')!=token:return
                try:
                    self.repository.state_set(goal.goal_id,'resource_cleanup',result,
                        expected_version=latest.version,actor=WorkActor('system','goal-cleanup'))
                    return
                except GoalConflict:
                    continue
            # Persisted pending is truthful if concurrent writers prevent the
            # receipt CAS; the same canonical cleanup remains safe to retry.

    @staticmethod
    def _actor(actor: str = "user") -> WorkActor:
        return WorkActor("user" if actor == "user" else "system", actor or "user")

    def create(
        self,
        *,
        title: str,
        objective: str,
        owner_chat_id: str = "",
        workspace_id: str = "",
        constraints: Sequence[Any] = (),
        success_criteria: Sequence[Mapping[str, Any]] = (),
        completion_policy: Mapping[str, Any] | None = None,
        priority: int = 0,
        budget: Mapping[str, Any] | None = None,
        deadline: float = 0.0,
        initial_state: Mapping[str, Any] | None = None,
        goal_id: str = "",
        correlation_id: str = "",
        actor: str = "user",
    ) -> GoalRecord:
        return self.repository.create_goal(
            title=title, objective=objective, owner_chat_id=owner_chat_id,
            workspace_id=workspace_id,
            constraints=constraints, success_criteria=success_criteria,
            completion_policy=completion_policy, priority=priority,
            budget_limits=dict(budget or {}), deadline=deadline,
            initial_state=initial_state, goal_id=goal_id,
            actor=self._actor(actor), correlation_id=correlation_id,
        )

    def get(self, goal_id: str) -> GoalRecord | None:
        return self.repository.get_goal(goal_id)

    def list(self, **filters: Any) -> list[GoalRecord]:
        return self.repository.list_goals(**filters)

    def snapshot(self, goal_id: str) -> dict[str, Any]:
        goal = self.repository.require_goal(goal_id)
        dependencies = self.repository.dependencies(goal_id)
        steps = self.repository.list_steps(goal_id)
        injectable = {"agent", "python", "process", "child", "integration"}
        configured = set(self.executor.supported_kinds)
        return {
            "schema": "variant1.goal-snapshot.v1",
            "goal": goal.to_dict(),
            "steps": [step.to_dict(dependencies=dependencies.get(step.step_id, ()))
                      for step in steps],
            "attempts": [item.to_dict() for item in self.repository.list_attempts(goal_id)],
            "state": self.repository.state_get(goal_id),
            "attention": [item.to_dict() for item in self.repository.list_attention(goal_id)],
            "waits": [item.to_dict() for item in self.repository.list_waits(goal_id)],
            "effects": [item.to_dict() for item in self.repository.list_effects(goal_id)],
            "artifacts": [item.to_dict() for item in self.repository.list_artifacts(goal_id)],
            "event_cursor": self.repository.event_cursor(goal_id),
            "runtime_disclosure": {
                "native_state_machine_only": True,
                "configured_step_handlers": sorted(configured),
                "missing_step_handlers": sorted(
                    {step.kind for step in steps if step.kind in injectable} - configured
                ),
                "waits_hold_execution_resources": False,
            },
        }

    def plan(
        self, goal_id: str, steps: Sequence[Mapping[str, Any]], *, expected_version: int,
        correlation_id: str = "", actor: str = "user",
    ) -> GoalRecord:
        return self.repository.plan_goal(
            goal_id, steps, expected_version=expected_version,
            actor=self._actor(actor), correlation_id=correlation_id,
        )

    def start(
        self, goal_id: str, *, expected_version: int,
        correlation_id: str = "", actor: str = "user", enqueue: bool = True,
    ) -> GoalRecord:
        if not self.repository.list_steps(goal_id):
            raise GoalTransitionError("goal must have a plan before start")
        goal = self.repository.transition_goal(
            goal_id, "queued", expected_version=expected_version,
            actor=self._actor(actor), correlation_id=correlation_id,
        )
        if enqueue:
            self.supervisor.enqueue(goal.goal_id, reason="started")
        return goal

    def pause(
        self, goal_id: str, *, expected_version: int, reason: str,
        correlation_id: str = "", actor: str = "user",
    ) -> GoalRecord:
        return self.repository.transition_goal(
            goal_id, "paused", expected_version=expected_version, reason=reason,
            actor=self._actor(actor), correlation_id=correlation_id,
        )

    def resume(
        self, goal_id: str, *, expected_version: int,
        correlation_id: str = "", actor: str = "user", enqueue: bool = True,
    ) -> GoalRecord:
        goal = self.repository.require_goal(goal_id)
        target = "queued" if goal.status == "paused" else "running"
        goal = self.repository.transition_goal(
            goal_id, target, expected_version=expected_version,
            actor=self._actor(actor), correlation_id=correlation_id,
        )
        if enqueue:
            self.supervisor.enqueue(goal.goal_id, reason="resumed")
        return goal

    def cancel(
        self, goal_id: str, *, expected_version: int, reason: str = "",
        correlation_id: str = "", actor: str = "user",
    ) -> GoalRecord:
        goal = self.repository.cancel_goal(
            goal_id,
            expected_version=expected_version,
            reason=reason,
            actor=self._actor(actor),
            correlation_id=correlation_id,
        )
        token=(self.repository.state_get(goal_id,'resource_cleanup') or {}).get('token','')
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self._cancel_owned_resources(goal, reason,token))
        else:
            background_tasks.spawn(
                self._cancel_owned_resources(goal, reason,token),
                name=f"goal-cancel:{goal.goal_id}",
            )
        return goal

    async def cancel_async(
        self, goal_id: str, *, expected_version: int, reason: str = "",
        correlation_id: str = "", actor: str = "user",
    ) -> GoalRecord:
        existing=self.repository.require_goal(goal_id)
        if existing.status in {'succeeded','failed','archived'}:
            goal=self.repository.state_set(goal_id,'resource_cleanup',
                {'status':'pending','complete':False,'requested_at':time.time(),'reason':reason,
                 'token':f'{correlation_id}:{existing.version+1}'},
                expected_version=expected_version,actor=self._actor(actor),correlation_id=correlation_id)
        else:
            goal = self.repository.cancel_goal(
                goal_id,
                expected_version=expected_version,
                reason=reason,
                actor=self._actor(actor),
                correlation_id=correlation_id,
            )
        token=(self.repository.state_get(goal_id,'resource_cleanup') or {}).get('token','')
        await self._cancel_owned_resources(goal, reason,token)
        return self.repository.require_goal(goal_id)

    async def request_cancel_async(self, goal_id: str, *, owner_chat_id: str,
                                   observed_version: int, correlation_id: str,
                                   reason: str = '') -> GoalRecord:
        """Admit an explicit owned Stop atomically despite scheduler progress."""
        goal = self.repository.cancel_goal(
            goal_id, expected_version=observed_version, owner_chat_id=owner_chat_id,
            reason=reason, actor=self._actor('user'), correlation_id=correlation_id,
        )
        key = 'cancel_request:' + hashlib.sha256(correlation_id.encode()).hexdigest()
        intent = self.repository.state_get(goal_id, key)
        await self._cancel_owned_resources(goal, reason, intent['cleanup_token'])
        return self.repository.require_goal(goal_id)

    async def finish_async(self,goal_id,*,expected_version,correlation_id=''):
        goal=await self.cancel_async(goal_id,expected_version=expected_version,
                                     reason='user_finished',correlation_id=correlation_id)
        cleanup=self.repository.state_get(goal_id,'resource_cleanup') or {}
        if cleanup.get('complete') is not True:
            return goal
        goal=self.repository.state_set(goal_id,'termination',
            {'kind':'user_finished','at':time.time(),'objective_achieved':None},
            expected_version=goal.version,actor=WorkActor('user','composer'),correlation_id=correlation_id)
        return self.archive(goal_id,expected_version=goal.version)

    def archive(self, goal_id: str, *, expected_version: int, actor: str = "user") -> GoalRecord:
        goal=self.repository.require_goal(goal_id)
        cleanup=self.repository.state_get(goal_id,'resource_cleanup') or {}
        if cleanup.get('status') in {'pending','failed'}:
            raise GoalTransitionError('Resource cleanup must finish before removing this goal')
        if not goal.terminal:
            raise GoalTransitionError('Stop or end this goal before removing it')
        if cleanup.get('complete') is not True and any(e.kind in {'agent.spawn','child.spawn','process.start','kernel.execute'} for e in self.repository.list_effects(goal_id)):
            raise GoalTransitionError('Settle goal-owned resources before removing this goal')
        return self.repository.transition_goal(
            goal_id, "archived", expected_version=expected_version,
            actor=self._actor(actor),
        )

    async def archive_async(self,goal_id,*,expected_version):
        goal=self.repository.require_goal(goal_id)
        if not goal.terminal:raise GoalTransitionError('Stop or end this goal before removing it')
        cleanup=self.repository.state_get(goal_id,'resource_cleanup') or {}
        if cleanup.get('complete') is not True:
            goal=await self.cancel_async(goal_id,expected_version=expected_version,reason='remove_goal')
            if (self.repository.state_get(goal_id,'resource_cleanup') or {}).get('complete') is not True:
                return goal
            expected_version=goal.version
        return self.archive(goal_id,expected_version=expected_version)

    async def delete_chat(self, chat_id: str) -> int:
        """Cancel nonterminal goals owned by one deleted chat."""

        owner_chat = str(chat_id or "").strip()
        if not owner_chat:
            return 0
        cancelled = 0
        failures: list[Exception] = []
        # UI goal listings are capped. A stable keyset walk also ensures a
        # busy goal's version conflict cannot skip its siblings.
        goals = exhaustive_keyset_pages(
            lambda cursor, size: self.repository.scan_goals(
                after_goal_id=str(cursor or ""), limit=size,
            ),
            lambda goal: goal.goal_id,
        )
        for original in goals:
            if original.owner_chat_id != owner_chat:
                continue
            for attempt in range(3):
                goal = self.repository.require_goal(original.goal_id)
                cleanup = self.repository.state_get(goal.goal_id, "resource_cleanup") or {}
                if goal.status in {"succeeded", "failed", "archived"} or (
                    goal.status == "cancelled" and cleanup.get("complete") is True
                ):
                    break
                try:
                    await self.cancel_async(
                        goal.goal_id, expected_version=goal.version,
                        reason="chat_deleted", actor="system",
                    )
                    cancelled += 1
                    break
                except GoalConflict as exc:
                    if attempt == 2:
                        failures.append(exc)
                except Exception as exc:
                    failures.append(exc)
                    break
        if failures:
            raise ExceptionGroup("some deleted-chat goals could not be cancelled", failures)
        return cancelled

    def retry(
        self, goal_id: str, step_id: str, *, expected_version: int,
        delay_s: float = 0.0, actor: str = "user", enqueue: bool = True,
    ) -> GoalRecord:
        goal, _ = self.repository.retry_step(
            goal_id, step_id, expected_version=expected_version,
            delay_s=delay_s, actor=self._actor(actor),
        )
        if goal.status == "blocked":
            goal = self.repository.transition_goal(
                goal.goal_id, "running", expected_version=goal.version,
                actor=self._actor(actor),
            )
        if enqueue:
            self.supervisor.enqueue(goal.goal_id, reason="step_retry")
        return goal

    def state_get(self, goal_id: str, key: str | None = None) -> Any:
        return self.repository.state_get(goal_id, key)

    def state_set(
        self, goal_id: str, key: str, value: Any, *, expected_version: int,
        actor: str = "user",
    ) -> GoalRecord:
        if key in {'objective_outcome','resource_cleanup','termination','continuation_request'} or key.startswith('cancel_request:'):
            raise GoalTransitionError('This state key is maintained by the canonical goal lifecycle')
        return self.repository.state_set(
            goal_id, key, value, expected_version=expected_version,
            actor=self._actor(actor),
        )

    def record_evidence(
        self, goal_id: str, criterion_id: str, evidence: Mapping[str, Any], *,
        expected_version: int, actor: str = "system",
    ) -> GoalRecord:
        current = self.repository.state_get(goal_id, "evidence") or {}
        merged = dict(current) if isinstance(current, Mapping) else {}
        merged[str(criterion_id)] = dict(evidence)
        return self.repository.state_set(
            goal_id, "evidence", merged, expected_version=expected_version,
            actor=self._actor(actor),
        )

    def attach(
        self, goal_id: str, artifact_ref: str, *, expected_version: int,
        role: str = "evidence", step_id: str = "", metadata: Mapping[str, Any] | None = None,
        actor: str = "user",
    ):
        return self.repository.attach_artifact(
            goal_id, artifact_ref, expected_version=expected_version, role=role,
            step_id=step_id, metadata=metadata, actor=self._actor(actor),
        )

    def wait_for(
        self, goal_id: str, step_id: str, *, expected_version: int,
        source: str, matcher: Mapping[str, Any] | None = None, wake_at: float = 0.0,
        actor: str = "user",
    ):
        wait = self.repository.create_wait(
            goal_id, step_id, expected_version=expected_version, source=source,
            matcher=matcher, wake_at=wake_at, actor=self._actor(actor),
        )
        if wait.wake_at:
            self.supervisor.enqueue(
                wait.goal_id, reason="time_wait", available_at=wait.wake_at
            )
        return wait

    def request_input(
        self, goal_id: str, step_id: str, *, expected_version: int,
        prompt: str, schema: Mapping[str, Any] | None = None, actor: str = "user",
    ):
        return self.repository.request_input(
            goal_id, step_id, expected_version=expected_version, prompt=prompt,
            schema=schema, actor=self._actor(actor),
        )

    def answer_input(
        self, attention_id: str, response: Any, *, expected_version: int,
        expected_attention_version: int = 1, actor: str = "user", enqueue: bool = True,
    ) -> GoalRecord:
        goal, _ = self.repository.respond_input(
            attention_id, response, expected_version=expected_version,
            expected_attention_version=expected_attention_version,
            actor=self._actor(actor),
        )
        if enqueue:
            self.supervisor.enqueue(goal.goal_id, reason="input_answered")
        return goal

    def dismiss_input(
        self,
        attention_id: str,
        *,
        expected_attention_version: int = 1,
        actor: str = "user",
        enqueue: bool = True,
    ) -> GoalRecord:
        goal, _ = self.repository.dismiss_input(
            attention_id,
            expected_attention_version=expected_attention_version,
            actor=self._actor(actor),
        )
        if enqueue:
            self.supervisor.enqueue(goal.goal_id, reason="input_dismissed")
        return goal

    def wake(
        self, *, source: str, event: Mapping[str, Any], actor: str = "system",
    ) -> list[str]:
        from .conditions import event_matches
        awakened: list[str] = []
        goals = exhaustive_keyset_pages(
            lambda cursor, size: self.repository.scan_goals(
                after_goal_id=str(cursor or ""), limit=size
            ),
            lambda goal: goal.goal_id,
        )
        for goal in goals:
            for wait in self.repository.list_waits(goal.goal_id, status="pending"):
                if wait.source == source and event_matches(wait.matcher, event):
                    current = self.repository.require_goal(goal.goal_id)
                    current, _wait, _step = self.repository.satisfy_wait(
                        wait.wait_id, expected_version=current.version, result=dict(event),
                        event_id=str(event.get("event_id") or ""), actor=self._actor(actor),
                    )
                    self.supervisor.enqueue(current.goal_id, reason=f"wake:{source}")
                    awakened.append(current.goal_id)
        return sorted(set(awakened))

    def verification_report(self, goal_id: str) -> VerificationReport:
        goal = self.repository.require_goal(goal_id)
        evidence = self.repository.state_get(goal_id, "evidence") or {}
        return verify_goal(
            goal, self.repository.list_steps(goal_id),
            evidence=evidence if isinstance(evidence, Mapping) else {},
            artifacts=self.repository.list_artifacts(goal_id),
        )

    def verify(
        self, goal_id: str, *, expected_version: int, complete: bool = True,
        actor: str = "user",
    ) -> tuple[VerificationReport, GoalRecord]:
        goal = self.repository.require_goal(goal_id)
        if goal.version != int(expected_version):
            from .models import GoalConflict
            raise GoalConflict(f"goal version changed ({goal.version} != {expected_version})")
        report = self.verification_report(goal_id)
        if complete and report.passed and goal.status not in {"succeeded", "archived"}:
            goal = self.repository.transition_goal(
                goal_id, "succeeded", expected_version=goal.version,
                actor=self._actor(actor), event_type="goal.verified_succeeded",
            )
        return report, goal


def create_goal_service(
    work: WorkService,
    *,
    handlers: Mapping[str, StepHandler] | None = None,
    wait_resolvers: Mapping[str, Callable[[Any], Any]] | None = None,
    register_work_handler: bool = True,
) -> GoalService:
    if not isinstance(work, WorkService):
        raise TypeError("create_goal_service requires the process-owned WorkService")
    return GoalService(
        GoalRepository(work.repository), work=work, handlers=handlers,
        wait_resolvers=wait_resolvers,
        register_work_handler=register_work_handler,
    )


__all__ = ["GoalService", "create_goal_service"]
