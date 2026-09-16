"""Structured, generation-bound child reports; execution status is separate."""
from __future__ import annotations
import json
import time
from tools import ToolError

REPORT_OUTCOME_METHOD = {
    'name': 'report_outcome',
    'description': ('Report this child objective as completed, blocked, or continuing. '
                    'This records an agent claim, not independent verification or an execution Stop. '
                    'Report once before the final response; identical retries are safe. '
                    'Ordinary prose never marks a goal complete.'),
    'effect_class': 'external_side_effect',
    'params': {
        'status': {'type': 'string', 'required': True, 'enum': ['completed', 'blocked', 'continuing']},
        'summary': {'type': 'string', 'required': True},
        'evidence_refs': {'type': 'array', 'required': False, 'items': {'type': 'string'}},
    },
}

class ChildOutcomes:
    def bind_outcome_run(self,child_id,generation,run_id):
        """Called by the native worker action boundary, not by the model."""
        revision = None
        parent_chat_id = ''
        with self._lock,self._connect() as conn:
            row=conn.execute(
                'SELECT parent_chat_id,status,run_generation,outcome_run_id '
                'FROM astb_child_handle WHERE child_id=?',
                (str(child_id),),
            ).fetchone()
            if (row is None or str(row['status']) != 'running'
                    or int(row['run_generation']) != int(generation)):
                raise ToolError('Child generation is no longer active')
            if str(row['outcome_run_id'] or '') == str(run_id):
                return
            parent_chat_id=str(row['parent_chat_id'])
            changed=conn.execute(
                "UPDATE astb_child_handle SET outcome_run_id=? WHERE child_id=? "
                "AND run_generation=? AND status='running' "
                "AND outcome_run_id=?",
                (str(run_id),child_id,int(generation),str(row['outcome_run_id'] or '')),
            )
            if changed.rowcount!=1:raise ToolError('Child generation is no longer active')
            revision=self._clock_revision(conn)
        self._notify_committed_change(parent_chat_id,str(child_id),revision)

    def report_outcome(self, chat_id, run_id, *, status, summary, evidence_refs=None):
        if status not in {'completed','blocked','continuing'}:
            raise ToolError('outcome status must be completed, blocked, or continuing')
        if not isinstance(summary,str) or not summary.strip() or len(summary)>4000:
            raise ToolError('outcome summary must contain 1–4000 characters')
        refs=[] if evidence_refs is None else evidence_refs
        if not isinstance(refs,list) or len(refs)>32 or any(not isinstance(r,str) or not r.strip() or len(r)>2000 for r in refs):
            raise ToolError('evidence_refs must contain at most 32 nonempty reference strings')
        revision = None
        parent_chat_id = ''
        with self._lock,self._connect() as conn:
            row=conn.execute('SELECT * FROM astb_child_handle WHERE child_chat_id=?',(str(chat_id),)).fetchone()
            if row is None or row['status']!='running':
                raise ToolError('session.report_outcome is available only in the active child run')
            # The broker admission supplies chat/run identity, never model arguments.
            current_run=str(row['outcome_run_id'] or '')
            if not run_id or current_run!=str(run_id):
                raise ToolError('outcome report belongs to a stale child run')
            existing=json.loads(row['outcome_json'] or '{}')
            if existing:
                if (existing['status']==status and existing['summary']==summary.strip()
                        and existing['evidence_refs']==refs):
                    return existing
                raise ToolError('This child generation already reported an outcome; restart creates a new outcome slot')
            value={'schema':'variant1.child-outcome.v1','status':status,
                   'summary':summary.strip(),'evidence_refs':list(refs),
                   'basis':'agent_report','independently_verified':False,
                   'child_id':row['child_id'],'generation':row['run_generation'],
                   'run_id':str(run_id),'reported_at':time.time()}
            changed=conn.execute(
                'UPDATE astb_child_handle SET outcome_json=?,updated_at=? '
                'WHERE child_id=?',
                (json.dumps(value,ensure_ascii=False),time.time(),row['child_id']),
            )
            if changed.rowcount == 1:
                revision=self._clock_revision(conn)
                parent_chat_id=str(row['parent_chat_id'])
        if revision is not None:
            self._notify_committed_change(
                parent_chat_id, str(value['child_id']), revision
            )
        return value

    def outcome_for_chat(self, chat_id):
        with self._lock,self._connect() as conn:
            row=conn.execute('SELECT outcome_json FROM astb_child_handle WHERE child_chat_id=?',(str(chat_id),)).fetchone()
        return json.loads(row[0]) if row and row[0] else None
