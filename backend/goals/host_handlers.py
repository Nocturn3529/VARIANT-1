"""Concrete host adapters for injectable durable-goal step kinds."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from execution_hosts import ExecutionNotFound, ExecutionOwner
from file_paths import effective_path
from project_context import chat_project_context
from work_fabric.models import WorkActor
from work_fabric.scope import WorkScope, coerce_work_scope

from .executor import StepExecutionContext, StepExecutionResult
from .models import GoalConflict


class GoalHostHandlers:
    """Bind GoalService to existing kernel, execution, child, and Git services."""

    def __init__(self, host: Any, goal_service: Any) -> None:
        self.host = host
        self.goals = goal_service

    def handlers(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "python": self.python,
            "process": self.process,
            "child": self.child,
            "integration": self.integration,
        }

    def wait_resolvers(self) -> dict[str, Any]:
        return {
            "execution_process": self.resolve_process,
            "child_session": self.resolve_child,
        }

    async def cancel_goal_resources(self, goal: Any, reason: str = ""):
        """Settle exact goal resources, including terminal child descendants."""
        from .resource_cleanup import GoalChildRoot, cleanup_goal_descendant_resources
        runtime=self._runtime()
        manager=getattr(runtime.catalog,'children',None)
        roots=[];issues=[];direct_processes=[]
        for effect in self.goals.repository.list_effects(goal.goal_id):
            response=dict(effect.response or {});request=dict(effect.request or {})
            try:
                if effect.kind=='process.start':
                    # The deterministic owner is known before start_process
                    # returns, including the crash window with no response.
                    process_id='goalproc_'+__import__('hashlib').sha256(
                        effect.effect_id.encode('utf-8')).hexdigest()[:32]
                    try:
                        owned=runtime.execution.processes.get(process_id)
                    except ExecutionNotFound:
                        legacy_id=str(response.get('process_id') or '')
                        if not legacy_id or legacy_id==process_id:continue
                        owned=runtime.execution.processes.get(legacy_id)
                        process_id=legacy_id
                    if owned.owner.kind!='goal' or owned.owner.owner_id!=goal.goal_id:
                        raise RuntimeError('Process effect does not own the recorded process')
                    record=await runtime.execution.stop_process(process_id,force=True)
                    if record.live:raise RuntimeError('Managed process remains live after Stop')
                    direct_processes.append(process_id)
                elif effect.kind=='kernel.execute':
                    # The durable goal owner, not mutable effect response data,
                    # is the only authority for an interrupt target.
                    chat_id=str(goal.owner_chat_id or '')
                    if not chat_id:raise RuntimeError('Goal has no owner chat for kernel cleanup')
                    await runtime.kernel.interrupt(chat_id,intent='stop')
                elif effect.kind in {'agent.spawn','child.spawn'}:
                    child_id=response.get('child_id') or request.get('resume_child_id')
                    if not child_id:
                        child_id='goalchild_'+__import__('hashlib').sha256(effect.effect_id.encode()).hexdigest()[:32]
                    if manager is None:raise RuntimeError('Child manager unavailable during goal cleanup')
                    try:child=manager.inspect(goal.owner_chat_id,child_id)
                    except LookupError:
                        if not response.get('child_id') and not request.get('resume_child_id'):continue
                        raise
                    roots.append(GoalChildRoot(child_id=child_id,parent_chat_id=goal.owner_chat_id,
                        child_chat_id=child['child_chat_id'],effect_id=effect.effect_id))
            except Exception as exc:
                issues.append({'phase':'direct_effect','resource_id':effect.effect_id,'error':str(exc),'retryable':True})
        result=await cleanup_goal_descendant_resources(goal_id=goal.goal_id,roots=roots,
            child_manager=manager,execution=runtime.execution,kernel=runtime.kernel)
        payload=result.to_dict()
        payload['stopped_process_ids']=list(dict.fromkeys(payload['stopped_process_ids']+direct_processes))
        payload['issues']+=issues
        payload['complete']=payload['complete'] and not issues
        return payload

    @staticmethod
    def _scope(context: StepExecutionContext) -> WorkScope:
        scope = coerce_work_scope(context.scope)
        return scope.with_updates(
            chat_id=(scope.chat_id or str(context.goal.owner_chat_id or "")),
            goal_id=context.goal.goal_id,
            step_id=context.step.step_id,
        )

    def _goal(self, goal_id: str):
        return self.goals.repository.require_goal(goal_id)

    def _runtime(self):
        return self.host.require_runtime()

    def _effect(
        self,
        context: StepExecutionContext,
        *,
        kind: str,
        request: Mapping[str, Any],
    ):
        goal = self._goal(context.goal.goal_id)
        return self.goals.repository.record_effect(
            goal.goal_id,
            context.step.step_id,
            expected_version=goal.version,
            kind=kind,
            idempotency_key=(
                f"goal:{goal.goal_id}:step:{context.step.step_id}:"
                f"attempt:{context.attempt.attempt}:{kind}"
            ),
            request=dict(request),
            status="planned",
            actor=WorkActor("system", "goal-host"),
        )

    def _effect_update(
        self,
        effect: Any,
        status: str,
        *,
        response: Mapping[str, Any] | None = None,
        error: str = "",
    ) -> None:
        goal = self._goal(effect.goal_id)
        self.goals.repository.update_effect(
            effect.effect_id,
            expected_version=goal.version,
            status=status,
            response=dict(response or {}),
            error=str(error or "")[:4000],
            actor=WorkActor("system", "goal-host"),
        )

    def _settle_interrupted_effect(self, effect: Any, *, cancelled: bool, error: str) -> None:
        """Do not leave an interrupted kernel effect looking actively dispatched."""
        for _ in range(2):
            current = next((item for item in self.goals.repository.list_effects(effect.goal_id)
                            if item.effect_id == effect.effect_id), None)
            if current is None or current.status != "dispatched":
                # Explicit goal cancellation already terminalizes its effects.
                return
            try:
                self._effect_update(
                    current, "cancelled" if cancelled else "unknown_effect", error=error,
                )
                return
            except GoalConflict:
                continue

    def _project_roots(self, scope: WorkScope) -> tuple[str, ...]:
        return chat_project_context(self.host, scope.chat_id).roots

    async def python(self, context: StepExecutionContext) -> StepExecutionResult:
        config = dict(context.step.config or {})
        code = str(config.get("code") or "")
        if not code:
            return StepExecutionResult(
                status="blocked",
                error="python step requires config.code",
            )
        chat_id = str(context.goal.owner_chat_id or "")
        if config.get("runtime_chat_id") and str(config["runtime_chat_id"]) != chat_id:
            return StepExecutionResult(
                status="blocked", error="python step must use its goal's owning chat",
            )
        if not chat_id:
            return StepExecutionResult(
                status="blocked",
                error="python step requires an owner runtime chat",
            )
        scope = self._scope(context).with_updates(chat_id=chat_id)
        roots = self._project_roots(scope)
        from kernel_runtime.integration import preserve_persistent_kernel_workspace
        roots, kernel_work_scope = preserve_persistent_kernel_workspace(
            self.host.require_runtime().kernel,
            chat_id,
            roots,
            scope.to_dict(),
        )
        effect = self._effect(
            context,
            kind="kernel.execute",
            request={"chat_id": chat_id, "code_sha256": __import__("hashlib").sha256(
                code.encode("utf-8")
            ).hexdigest()},
        )
        if effect.status != "planned":
            return StepExecutionResult(
                status="blocked",
                error=(
                    "kernel effect was already dispatched; automatic replay is refused "
                    f"({effect.status})"
                ),
                metadata={"effect_id": effect.effect_id, "effect_status": effect.status},
            )
        self._effect_update(effect, "dispatched", response={"chat_id": chat_id})
        try:
            result = await self.host.require_runtime().kernel.execute(
                chat_id=chat_id,
                code=code,
                run_id=(
                    f"goal:{context.goal.goal_id}:step:{context.step.step_id}:"
                    f"attempt:{context.attempt.attempt}"
                ),
                outer_tool_call_id=effect.effect_id,
                workspace_roots=roots,
                work_scope=kernel_work_scope,
                cancellation=context.cancellation_requested,
                timeout_s=float(config.get("timeout_s") or 0) or None,
            )
        except asyncio.CancelledError:
            self._settle_interrupted_effect(
                effect, cancelled=context.cancellation_requested(),
                error="kernel step cancelled during execution",
            )
            raise
        except Exception as exc:
            self._settle_interrupted_effect(
                effect, cancelled=False, error=f"kernel execution interrupted: {exc}",
            )
            raise
        if context.cancellation_requested():
            self._settle_interrupted_effect(
                effect, cancelled=True, error="goal cancelled during kernel execution",
            )
            return StepExecutionResult(status="cancelled", error="goal cancelled")
        status = "succeeded" if result.ok else "failed"
        self._effect_update(
            effect,
            status,
            response={
                "execution_id": result.execution_id,
                "kernel_generation": result.generation,
                "result_ref": result.result_ref,
                "ledger_sequence": result.ledger_sequence,
            },
            error=result.error_message,
        )
        return StepExecutionResult(
            status=status,
            result_ref=result.result_ref,
            error=result.error_message,
            metadata={
                "execution_id": result.execution_id,
                "kernel_generation": result.generation,
                "cell_ledger_sequence": result.ledger_sequence,
            },
        )

    async def process(self, context: StepExecutionContext) -> StepExecutionResult:
        config = dict(context.step.config or {})
        command = config.get("command")
        if not isinstance(command, (str, list, tuple)) or not command:
            return StepExecutionResult(
                status="blocked", error="process step requires config.command"
            )
        if bool(config.get("tty")):
            return StepExecutionResult(
                status="blocked",
                error="durable goal process steps require tty=false",
            )
        scope = self._scope(context)
        roots = self._project_roots(scope)
        cwd = effective_path(
            str(config.get("cwd") or "."), default_cwd=roots[0],
        )
        effect = self._effect(
            context,
            kind="process.start",
            request={
                "command": list(command) if not isinstance(command, str) else command,
                "cwd": cwd,
                "restart": str(config.get("restart") or "never"),
            },
        )
        if effect.status != "planned":
            return StepExecutionResult(
                status="blocked",
                error=f"process effect cannot be automatically replayed from {effect.status}",
                metadata={"effect_id": effect.effect_id},
            )
        process_id = "goalproc_" + __import__("hashlib").sha256(
            effect.effect_id.encode("utf-8")
        ).hexdigest()[:32]
        self._effect_update(effect, "dispatched", response={"process_id": process_id})
        record = await self._runtime().execution.start_process(
            command,
            owner=ExecutionOwner("goal", context.goal.goal_id, scope),
            cwd=cwd,
            environment=dict(config.get("environment") or {}),
            environment_profile_id=str(config.get("environment_profile_id") or ""),
            shell=bool(config.get("shell")),
            restart=str(config.get("restart") or "never"),
            max_attempts=max(1, int(config.get("max_attempts") or 1)),
            restart_delay_s=max(0.0, float(config.get("restart_delay_s") or 0.25)),
            health_check=dict(config.get("health_check") or {}),
            tty=False,
            process_id=process_id,
        )
        if context.cancellation_requested():
            self._runtime().execution.processes.stop(record.process_id, force=True)
            self._settle_interrupted_effect(
                effect, cancelled=True, error="goal cancelled after process admission",
            )
            return StepExecutionResult(status="cancelled", error="goal cancelled")
        # The start boundary is dispatched, not terminal: resolve_process owns
        # the eventual exit/health result and its final effect transition.
        return StepExecutionResult(
            status="waiting",
            wait_source="execution_process",
            wait_matcher={
                "process_id": record.process_id,
                "effect_id": effect.effect_id,
            },
            metadata={"process_id": record.process_id},
        )

    async def _spawn_child(
        self,
        context: StepExecutionContext,
        *,
        source: str,
    ) -> StepExecutionResult:
        parent_chat = str(context.goal.owner_chat_id or "")
        manager = self.host.require_runtime().catalog.children
        if not parent_chat or manager is None:
            return StepExecutionResult(
                status="blocked",
                error=f"{source} step requires owner chat and child runtime",
            )
        config = dict(context.step.config or {})
        all_effects=self.goals.repository.list_effects(context.goal.goal_id)
        current_key=f'goal:{context.goal.goal_id}:step:{context.step.step_id}:attempt:{context.attempt.attempt}:{source}.spawn'
        saved_effect=next((e for e in all_effects if e.idempotency_key==current_key),None)
        prior_children = [e for e in all_effects
                          if e.step_id==context.step.step_id and e.kind==f'{source}.spawn'
                          and e.idempotency_key != f'goal:{context.goal.goal_id}:step:{context.step.step_id}:attempt:{context.attempt.attempt}:{source}.spawn'
                          and (e.response or {}).get('child_id')]
        previous = prior_children[-1] if prior_children else None
        resume_child = manager.inspect(parent_chat,previous.response['child_id']) if previous and saved_effect is None else None
        follow_up=self.goals.repository.state_get(context.goal.goal_id,'continuation_request') or {}
        resume_message='Continue the same goal from retained Python state and saved work. '
        if previous:
            resume_message += str((previous.response.get('outcome') or {}).get('summary') or '')
        if follow_up.get('previous_attempt')==context.attempt.attempt-1:
            resume_message += '\nUser follow-up: '+str(follow_up.get('message') or '')
        effect = saved_effect or self._effect(
            context,
            kind=f"{source}.spawn",
            request={
                "parent_chat_id": parent_chat,
                "task": context.step.instructions,
                "context": str(config.get("context") or "")[:40_000],
                'resume_child_id':resume_child.get('child_id') if resume_child else None,
                'resume_generation':resume_child.get('run_generation') if resume_child else None,
                'resume_message':resume_message if resume_child else '',
            },
        )
        if effect.status != "planned":
            return StepExecutionResult(
                status="blocked",
                error=f"{source} effect cannot be automatically replayed from {effect.status}",
                metadata={"effect_id": effect.effect_id},
            )
        identity_digest = __import__("hashlib").sha256(
            effect.effect_id.encode("utf-8")
        ).hexdigest()[:32]
        child_id = "goalchild_" + identity_digest
        child_chat_id = "goalchildchat_" + identity_digest
        if context.cancellation_requested():
            return StepExecutionResult(status='cancelled',error='goal cancelled before child admission')
        admitted_child_id = str(effect.request.get('resume_child_id') or child_id)
        self._effect_update(effect, "dispatched", response={
            "child_id": admitted_child_id,
            "child_chat_id": child_chat_id if admitted_child_id == child_id else "",
        })
        if effect.request.get('resume_child_id'):
            child=await manager.restart(parent_chat,effect.request['resume_child_id'],
                expected_generation=effect.request['resume_generation'],request_id=effect.effect_id,
                message=effect.request['resume_message'])
        else:
            goal_context=str(config.get('context') or '')
            if context.goal.completion_policy.get('entrypoint')=='composer_goal':
                goal_context += ('\nThis is an explicitly requested durable goal. Before your final response, '
                    'record exactly one structured outcome with session.report_outcome(status="completed", '
                    'summary="what was verified", evidence_refs=[]), or status="blocked" with the needed input, '
                    'or status="continuing" to request another turn in this same persistent child. '
                    'Do not report completed while the objective remains unfinished. A final prose response alone '
                    'does not complete the goal. Ordinary Python and provided capabilities remain available.')
            child = await manager.spawn(
                parent_chat, task=context.step.instructions,
                name=str(config.get("name") or source)[:80], context=goal_context[:40_000],
                child_id=child_id, child_chat_id=child_chat_id,
                fresh_catalog=context.goal.completion_policy.get('entrypoint')=='composer_goal',
            )
        child_id = str(child.get("child_id") or child_id)
        if context.cancellation_requested():
            await manager.cancel(parent_chat, child_id)
            self._settle_interrupted_effect(
                effect, cancelled=True, error="goal cancelled after child admission",
            )
            return StepExecutionResult(status="cancelled", error="goal cancelled")
        if context.goal.completion_policy.get('entrypoint')=='composer_goal':
            current=self._goal(context.goal.goal_id)
            self.goals.repository.state_set(current.goal_id,'objective_outcome',
                {'status':'unreported','basis':None,'independently_verified':False,
                 'child_id':child_id,'generation':int(child.get('run_generation') or 1),
                 'step_id':context.step.step_id,'execution_status':child.get('status')},
                expected_version=current.version,actor=WorkActor('system','goal-host'))
        # Child completion, not spawn admission, terminalizes this effect in
        # resolve_child. Its deterministic ID is already in the dispatched
        # response for cancellation/crash cleanup.
        return StepExecutionResult(
            status="waiting",
            wait_source="child_session",
            wait_matcher={
                "parent_chat_id": parent_chat,
                "child_id": child_id,
                "effect_id": effect.effect_id,
                'run_generation':int(child.get('run_generation') or 1),
            },
            metadata={"child_id": child_id, "source": source},
        )

    async def agent(self, context: StepExecutionContext) -> StepExecutionResult:
        return await self._spawn_child(context, source="agent")

    async def child(self, context: StepExecutionContext) -> StepExecutionResult:
        return await self._spawn_child(context, source="child")

    async def integration(self, context: StepExecutionContext) -> StepExecutionResult:
        config = dict(context.step.config or {})
        required = (
            "review_id", "integration_root", "target_branch",
            "expected_target_oid", "expected_review_version",
        )
        missing = [name for name in required if not config.get(name)]
        if missing:
            return StepExecutionResult(
                status="blocked",
                error="integration step missing: " + ", ".join(missing),
            )
        scope = self._scope(context)
        effect = self._effect(
            context,
            kind="review.integrate_fast_forward",
            request={name: config.get(name) for name in required},
        )
        if effect.status != "planned":
            return StepExecutionResult(
                status="blocked",
                error=f"integration effect cannot be automatically replayed from {effect.status}",
                metadata={"effect_id": effect.effect_id},
            )
        if context.cancellation_requested():
            return StepExecutionResult(status="cancelled", error="goal cancelled before integration")
        self._effect_update(effect, "dispatched")
        try:
            review = await asyncio.to_thread(
                self._runtime().coding.review.integrate_fast_forward,
                str(config["review_id"]),
                integration_root=str(config["integration_root"]),
                target_branch=str(config["target_branch"]),
                expected_target_oid=str(config["expected_target_oid"]),
                expected_revision=int(config["expected_review_version"]),
                scope=scope,
                idempotency_key=effect.effect_id,
            )
        except Exception as exc:
            self._effect_update(effect, "failed", error=str(exc))
            return StepExecutionResult(status="failed", error=str(exc))
        payload = review.to_dict()
        artifact = self.host.require_runtime().session_artifacts.put_json(
            payload,
            kind="goal_integration_result",
            scope=str(scope.chat_id or f"goal:{context.goal.goal_id}"),
        )
        self._effect_update(
            effect,
            "succeeded",
            response={"review_id": review.review_id, "artifact_ref": artifact.ref},
        )
        return StepExecutionResult(status="succeeded", result_ref=str(artifact.ref))

    def resolve_process(self, wait: Any) -> dict[str, Any] | None:
        process_id = str(wait.matcher.get("process_id") or "")
        if not process_id:
            return {"status": "failed", "error": "process wait has no process_id"}
        try:
            record = self._runtime().execution.processes.get(process_id)
        except Exception as exc:
            return {"status": "failed", "error": str(exc)}
        if record.live:
            return None
        page = self._runtime().execution.processes.logs(
            process_id,
            after_cursor=0,
            max_bytes=1024 * 1024,
            max_frames=1000,
            prefer_artifact_refs=True,
        )
        summary = {
            "schema": "variant1.goal-process-result.v1",
            "process": record.to_dict(),
            "output": page.to_dict(),
        }
        uncertain = record.state == "unknown_effect"
        artifact = self.host.require_runtime().session_artifacts.put_json(
            summary,
            kind="goal_process_result",
            scope=str(record.owner.scope.chat_id or f"goal:{wait.goal_id}"),
        )
        success = record.state == "exited" and record.exit_code == 0
        effect_id = str(wait.matcher.get("effect_id") or "")
        if effect_id:
            effect = next(
                item for item in self.goals.repository.list_effects(wait.goal_id)
                if item.effect_id == effect_id
            )
            target = (
                "succeeded" if success else
                "unknown_effect" if uncertain else "failed"
            )
            if effect.status == "dispatched":
                self._effect_update(
                    effect,
                    target,
                    response={
                        "process_id": process_id,
                        "exit_code": record.exit_code,
                        "artifact_ref": artifact.ref,
                    },
                    error="" if success else f"process ended in {record.state}",
                )
            elif effect.status != target:
                raise RuntimeError(
                    f"process effect {effect.effect_id} is {effect.status}, expected {target}"
                )
        return {
            "status": (
                "succeeded" if success else "blocked" if uncertain else "failed"
            ),
            "artifact_ref": str(artifact.ref),
            "process_id": process_id,
            "exit_code": record.exit_code,
            "error": "" if success else f"process ended in {record.state}",
        }

    def resolve_child(self, wait: Any) -> dict[str, Any] | None:
        parent = str(wait.matcher.get("parent_chat_id") or "")
        child_id = str(wait.matcher.get("child_id") or "")
        manager = self.host.require_runtime().catalog.children
        if manager is None or not parent or not child_id:
            return {"status": "failed", "error": "child wait identity is incomplete"}
        try:
            child = manager.inspect(parent, child_id)
        except Exception as exc:
            return {"status": "failed", "error": str(exc)}
        status = str(child.get("status") or "")
        expected_generation=wait.matcher.get('run_generation')
        if expected_generation is not None and int(child.get('run_generation') or 1)!=int(expected_generation):
            return {'status':'blocked','error':'Child generation changed outside this goal attempt; inspect before continuing'}
        if status in {"queued", "running"}:
            return None
        success = status == "completed"
        goal=self.goals.repository.require_goal(wait.goal_id)
        strict=goal.completion_policy.get('entrypoint')=='composer_goal'
        outcome=dict(child.get('outcome') or {'status':'unreported','basis':None,'independently_verified':False})
        if strict:
            if outcome.get('generation') not in (None,int(child.get('run_generation') or 1)):
                outcome={'status':'unreported','summary':'Outcome belongs to another generation'}
            projection={**outcome,'child_id':child_id,'step_id':wait.step_id,
                        'generation':int(child.get('run_generation') or 1),
                        'execution_status':status,'basis':outcome.get('basis'),
                        'independently_verified':False}
            if self.goals.repository.state_get(goal.goal_id,'objective_outcome')!=projection:
                self.goals.repository.state_set(goal.goal_id,'objective_outcome',projection,
                    expected_version=goal.version,actor=WorkActor('system','goal-host'))
            objective_success=success and outcome.get('status')=='completed'
        else:
            objective_success=success
        artifact_ref = str(child.get("artifact_ref") or "")
        if not artifact_ref:
            artifact = self.host.require_runtime().session_artifacts.put_json(
                child,
                kind="goal_child_result",
                scope=parent,
            )
            artifact_ref = str(artifact.ref)
        effect_id = str(wait.matcher.get("effect_id") or "")
        if effect_id:
            effect = next(
                item for item in self.goals.repository.list_effects(wait.goal_id)
                if item.effect_id == effect_id
            )
            target = "succeeded" if success else "failed"
            if effect.status == "dispatched":
                self._effect_update(
                    effect,
                    target,
                    response={
                        "child_id": child_id,
                        "status": status,
                        "artifact_ref": artifact_ref,
                        'run_generation':int(child.get('run_generation') or 1),
                        'outcome':outcome,
                    },
                    error="" if success else str(child.get("error") or status),
                )
            elif effect.status != target:
                raise RuntimeError(
                    f"child effect {effect.effect_id} is {effect.status}, expected {target}"
                )
        return {
            "status": "succeeded" if objective_success else 'blocked' if strict and success else "failed",
            "artifact_ref": artifact_ref,
            "child_id": child_id,
            "error": "" if objective_success else str(outcome.get('summary') or 'No explicit completed objective outcome was reported') if strict and success else str(child.get("error") or status),
        }


def build_goal_host_handlers(host: Any, goal_service: Any) -> GoalHostHandlers:
    return GoalHostHandlers(host, goal_service)


__all__ = ["GoalHostHandlers", "build_goal_host_handlers"]
