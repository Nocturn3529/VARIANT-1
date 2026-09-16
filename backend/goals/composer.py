"""Explicit composer-goal admission over the existing durable GoalService."""
from __future__ import annotations

import asyncio
import hashlib
import weakref
from .models import GoalConflict, GoalValidationError
from work_fabric.models import WorkActor


def submission_goal_id(chat_id: str, request_id: str) -> str:
    return 'goal_' + hashlib.sha256(f'composer-goal-v1\0{chat_id}\0{request_id}'.encode()).hexdigest()[:32]


class ComposerGoals:
    def __init__(self, service):
        self.service = service
        self._locks = weakref.WeakValueDictionary()

    def _continuation_admission(self, goal_id, state):
        request = state.get('continuation_request') or {}
        request_id = request.get('request_id')
        if not request_id:
            return None
        receipt = {'request_id': request_id, 'step_id': request.get('step_id'),
                   'previous_attempt': request.get('previous_attempt'), 'status': 'staged',
                   'basis': 'work_job', 'job_id': None, 'job_status': None}
        try:
            from .supervisor import GOAL_SUPERVISOR_JOB
            goal = self.service.repository.require_goal(goal_id)
            job = self.service.work.jobs.get_by_idempotency(
                owner_kind='goal', owner_id=goal_id, kind=GOAL_SUPERVISOR_JOB,
                idempotency_key=f'goal-supervise:{goal_id}:continue:{request_id}',
            )
            if job is not None:
                if job.scope != self.service.repository._scope(goal) or job.input_manifest.get('goal_id') != goal_id:
                    return {**receipt, 'status': 'unknown', 'reason': 'job_identity_mismatch'}
                receipt.update(status='admitted', job_id=job.job_id, job_status=job.status)
        except Exception:
            # A read-side receipt lookup must not turn an already committed
            # continuation into a rejected action or invented acceptance.
            return {**receipt, 'status': 'unknown', 'reason': 'receipt_lookup_failed'}
        return receipt

    def snapshot(self, goal_id):
        result = self.service.snapshot(goal_id)
        policy = result['goal']['completion_policy']
        cleanup=result['state'].get('resource_cleanup') or {'status':'not_requested','complete':False}
        outcome=result['state'].get('objective_outcome') or {'status':'unreported','basis':None,'independently_verified':False}
        return {**result, 'submission_request_id': policy.get('submission_request_id'),
                'completion_basis': policy.get('completion_basis', 'declared_criteria'),
                'objective_outcome':outcome,'cleanup':cleanup,
                'termination':result['state'].get('termination'),
                'continuation_admission':self._continuation_admission(goal_id,result['state']),
                'capabilities': {'pause_scheduling': True, 'pause_active_work': False,
                                 'resume': True, 'cancel': True,
                                 'continue':result['goal']['status']=='blocked',
                                 'retry_cleanup':cleanup.get('status') in {'pending','failed'},
                                 'finish':result['goal']['status']!='archived',
                                 'archive':result['goal']['status'] in {'succeeded','failed','cancelled'} and cleanup.get('status') not in {'pending','failed'}}}

    async def continue_goal(self, chat_id, goal_id, request_id, expected_version, message=''):
        if not request_id or not isinstance(message,str) or len(message)>20000:
            raise GoalValidationError('A request_id and at most 20000 characters of guidance are required')
        lock=self._locks.setdefault(chat_id,asyncio.Lock())
        async with lock:
            goal=self.service.repository.require_goal(goal_id)
            if goal.owner_chat_id!=chat_id or goal.completion_policy.get('entrypoint')!='composer_goal':
                raise GoalConflict('Goal is not owned by this composer chat')
            previous=self.service.repository.state_get(goal_id,'continuation_request') or {}
            repeated=previous.get('request_id')==request_id
            if repeated and previous.get('message')!=message:
                raise GoalConflict('Continuation request_id has different guidance')
            steps=self.service.repository.list_steps(goal_id)
            step=next((s for s in steps if s.step_id==previous.get('step_id')),None) if repeated else next((s for s in steps if s.status in {'blocked','failed'}),None)
            if repeated and step and step.attempt_count>previous['previous_attempt']:
                return self.snapshot(goal_id)
            if repeated and step and step.status in {'retry_scheduled','ready','pending'}:
                if goal.terminal:return self.snapshot(goal_id)
                if goal.status=='blocked':
                    goal=self.service.repository.transition_goal(goal_id,'running',expected_version=goal.version,
                        actor=WorkActor('user','composer'),correlation_id=request_id)
                self.service.supervisor.enqueue(goal_id,reason='recovered_user_continuation',dedupe_key='continue:'+request_id)
                return self.snapshot(goal_id)
            if goal.terminal or step is None or goal.status!='blocked':
                raise GoalConflict('Only a blocked goal with unfinished work can be continued')
            if not repeated:
                if goal.version!=expected_version:
                    raise GoalConflict('Goal version changed; refresh before continuing')
                goal=self.service.repository.state_set(goal_id,'continuation_request',
                    {'request_id':request_id,'message':message,'step_id':step.step_id,'previous_attempt':step.attempt_count},
                    expected_version=goal.version,actor=WorkActor('user','composer'),correlation_id=request_id)
            goal,_=self.service.repository.retry_step(goal_id,step.step_id,expected_version=goal.version,
                continuation=True,actor=WorkActor('user','composer'),correlation_id=request_id)
            goal=self.service.repository.transition_goal(goal_id,'running',expected_version=goal.version,
                actor=WorkActor('user','composer'),correlation_id=request_id)
            self.service.supervisor.enqueue(goal_id,reason='user_continuation',dedupe_key='continue:'+request_id)
            return self.snapshot(goal_id)

    def current(self, chat_id, request_id=''):
        if request_id:
            goal = self.service.get(submission_goal_id(chat_id, request_id))
            if goal is None:
                return None
            if goal.owner_chat_id != chat_id or goal.completion_policy.get('submission_request_id') != request_id:
                raise GoalConflict('Submission identity does not match this chat')
        else:
            goal = self.service.repository.latest_composer_goal(chat_id)
            if goal is None:
                return None
            if goal.status=='archived':
                return None
        return self.snapshot(goal.goal_id)

    async def submit(self, chat_id, request_id, objective):
        objective = str(objective or '').strip()
        if not chat_id or not request_id or len(request_id) > 512:
            raise GoalValidationError('session_id and request_id are required')
        if not objective or len(objective) > 32000:
            raise GoalValidationError('Goal objective must contain 1–32000 characters')
        if 'agent' not in self.service.executor.supported_kinds:
            raise GoalValidationError('Goal agent handler is unavailable')
        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            goal_id = submission_goal_id(chat_id, request_id)
            goal = await asyncio.to_thread(self.service.get, goal_id)
            if goal is not None:
                if (goal.owner_chat_id != chat_id or goal.objective != objective
                        or goal.completion_policy.get('submission_request_id') != request_id):
                    raise GoalConflict('request_id already belongs to a different submission')
            else:
                current = await asyncio.to_thread(self.service.repository.active_composer_goal, chat_id)
                if current is not None:
                    raise GoalConflict('This chat already has an active goal; inspect or stop it first')
                goal = await asyncio.to_thread(self.service.create,
                    title=objective.splitlines()[0][:120], objective=objective,
                    owner_chat_id=chat_id, goal_id=goal_id, correlation_id=request_id,
                    completion_policy={'entrypoint': 'composer_goal',
                                       'submission_request_id': request_id,
                                       'completion_basis': 'structured_child_report',
                                       'auto_continue': True})
            # A retry after a lost reply or a crash between durable boundaries
            # continues the same goal. It never creates a second child/goal.
            if goal.status == 'draft':
                if not self.service.repository.list_steps(goal_id):
                    goal = await asyncio.to_thread(self.service.plan, goal_id,
                        [{'step_id': goal_id + ':execute', 'kind': 'agent',
                          'instructions': objective, 'required': True}],
                        expected_version=goal.version, correlation_id=request_id)
                goal = self.service.start(goal_id, expected_version=goal.version,
                                          correlation_id=request_id, enqueue=False)
            if goal.status == 'queued':
                self.service.supervisor.enqueue(goal_id, reason='submission_recovered',
                                                dedupe_key=f'composer-submit:{goal_id}')
            return self.snapshot(goal_id)
