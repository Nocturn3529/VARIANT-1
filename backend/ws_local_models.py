"""Request-correlated settings operations on the owned local-model library."""
from __future__ import annotations


def register(on):
    @on('local-models:get', 'local-models:search', 'local-models:files', 'local-models:download',
        'local-models:cancel', 'local-models:activate', 'local-models:eject', 'local-models:delete')
    async def command(srv, websocket, session, msg):
        operation = str(msg['type']).split(':', 1)[1]
        request_id = str(msg.get('request_id') or '')
        response = {'type': 'local-models:result', 'request_id': request_id,
                    'operation': operation, 'ok': False}
        library = srv.local_models
        try:
            if operation in {'download', 'cancel', 'activate', 'eject', 'delete'} and not request_id:
                raise ValueError('A request ID is required for model changes.')
            if operation == 'get':
                result = await library.snapshot()
            elif operation == 'search':
                result = {'items': await library.search(msg.get('query', ''), msg.get('limit', 20))}
            elif operation == 'files':
                result = await library.files(msg.get('repo', ''), msg.get('revision', 'main'))
            elif operation == 'download':
                result = await library.download(msg.get('repo', ''), msg.get('paths'),
                                                revision=msg.get('revision', 'main'), request_id=request_id)
            elif operation == 'cancel':
                result = {'cancelled': await library.cancel(str(msg.get('job_id') or ''))}
            elif operation == 'activate':
                result = await library.activate(str(msg.get('model_id') or ''))
            elif operation == 'eject':
                result = await library.eject()
            else:
                result = await library.delete(str(msg.get('model_id') or ''))
            response.update(ok=True, result=result)
        except Exception as exc:
            response['error'] = {'code': 'local_models_operation_failed', 'message': str(exc)}
        await websocket.send_json(response)
