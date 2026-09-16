"""Bounded authoritative child roster, separate from model-facing tool output."""
import json

class ChildInspection:
    def inspection_snapshot(self,chat_id,*,limit=200,child_id=''):
        cap=max(1,min(500,int(limit)))
        base='''WITH RECURSIVE descendants AS (
            SELECT * FROM astb_child_handle WHERE parent_chat_id=?
            UNION SELECT c.* FROM astb_child_handle c JOIN descendants p ON c.parent_chat_id=p.child_chat_id
        ) '''
        with self._lock,self._connect() as conn:
            conn.execute('BEGIN')
            revision=conn.execute('SELECT revision FROM astb_child_clock WHERE id=1').fetchone()[0]
            totals=conn.execute(base+'''SELECT COUNT(*) AS total,
                SUM(status IN ('queued','running')) AS active,
                SUM(json_extract(outcome_json,'$.status')='blocked') AS blocked FROM descendants''',(chat_id,)).fetchone()
            query=base+'''SELECT c.*,p.child_id AS parent_child_id FROM descendants c
                LEFT JOIN astb_child_handle p ON p.child_chat_id=c.parent_chat_id'''
            args=[chat_id]
            if child_id:query+=' WHERE c.child_id=?';args.append(child_id)
            query+=' ORDER BY c.created_at,c.child_id LIMIT ?';args.append(cap)
            rows=conn.execute(query,args).fetchall()
        children=[]
        for row in rows:
            outcome=json.loads(row['outcome_json'] or '{}') or {'status':'unreported','basis':None,'independently_verified':False}
            value={key:row[key] for key in ('child_id','parent_chat_id','child_chat_id','name','status','created_at','started_at','completed_at','updated_at','run_generation')}
            try:
                usage=json.loads(row['usage_json'] or '{}')
                if not isinstance(usage,dict):usage={}
            except (TypeError,ValueError):
                usage={}
            deletion_state=str(row['deletion_state'] or '')
            cleanup=(
                {
                    'status':deletion_state,
                    'complete':False,
                    'error':str(row['deletion_error'] or '') or None,
                }
                if deletion_state else None
            )
            value.update(parent_child_id=None if row['parent_chat_id']==chat_id else row['parent_child_id'],
                task=row['task_text'][:2000],run_id=row['outcome_run_id'] or None,outcome=outcome,
                model_route=json.loads(row['model_route_json'] or '{}'),cleanup=cleanup,
                usage=usage,usage_rollup_state=str(row['usage_rollup_state'] or ''),
                usage_rollup_error=str(row['usage_rollup_error'] or ''),
                work_job_id=str(row['work_job_id'] or ''),error=str(row['error'] or ''))
            if child_id:
                from .children import reported_child_text
                report=reported_child_text(row['result_text'] or '')
                value.update(report=report[:16000],report_truncated=len(report)>16000)
            children.append(value)
        return {'revision':int(revision),'total':totals['total'],'active':int(totals['active'] or 0),
                'blocked':int(totals['blocked'] or 0),'truncated':not child_id and totals['total']>len(children),'children':children}
