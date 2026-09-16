"""Chat-owned child inspection using canonical records, never parent trace merging."""
import asyncio

def register(on):
    @on('children:snapshot:get','children:detail:get')
    async def inspect_children(srv,websocket,session,msg):
        sid=str(msg.get('session_id') or '');rid=str(msg.get('request_id') or '')
        try:
            runtime=srv.require_runtime()
            if not sid or not rid or runtime.sessions.get_session(sid) is None:
                raise ValueError('A known session_id and request_id are required')
            manager=runtime.catalog.children
            if manager is None:raise RuntimeError('Child runtime is unavailable')
            detail=msg.get('type')=='children:detail:get'
            child_id=str(msg.get('child_id') or '') if detail else ''
            if detail and not child_id:raise ValueError('child_id is required')
            snapshot=await asyncio.to_thread(manager.inspection_snapshot,sid,
                limit=int(msg.get('limit') or 200),child_id=child_id)
            if detail:
                if not snapshot['children']:raise LookupError('Child is not owned by this chat')
                child=snapshot['children'][0]
                activity=await asyncio.to_thread(runtime.work.repository.chat_operation_activity,
                    child['child_chat_id'],limit=int(msg.get('limit') or 50))
                value={'type':'children:detail','schema':'variant1.child-detail.v1',
                    'revision':snapshot['revision'],'child':child,'activity':activity['items'],
                    'activity_revision':activity['revision'],'truncated':activity['truncated'],
                    'activity_provenance':'canonical_work_operations'}
            else:
                value={'type':'children:snapshot','schema':'variant1.children-snapshot.v1',**snapshot}
            await websocket.send_json({**value,'session_id':sid,'request_id':rid})
        except Exception as exc:
            await websocket.send_json({'type':'children:rejected','session_id':sid,'request_id':rid,'error':str(exc)})
