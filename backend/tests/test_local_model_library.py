from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from model_runtime.local_models import LocalModelLibrary


REVISION = 'a' * 40
PAYLOAD = b'GGUF' + b'fixture model payload'


def stack(tmp_path, files=None, handler=None):
    files = files or {'tiny.gguf': PAYLOAD}
    requests = []
    def respond(request):
        requests.append(request)
        if handler:
            response = handler(request)
            if response is not None:
                return response
        if request.url.path.startswith('/api/models/'):
            return httpx.Response(200, json={'sha': REVISION, 'siblings': [
                {'rfilename': name, 'size': len(data), 'lfs': {'sha256': hashlib.sha256(data).hexdigest()}}
                for name, data in files.items()]})
        if request.url.path == '/api/models':
            return httpx.Response(200, json=[{'id': 'owner/model', 'downloads': 3}])
        name = request.url.path.split('/resolve/' + REVISION + '/')[-1]
        return httpx.Response(200, content=files[name])
    factory = lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(respond), **kwargs)
    engine = SimpleNamespace(ready=False, model='', mmproj='', runtime_id='llamacpp', stop=AsyncMock())
    models = SimpleNamespace(restart_engine=AsyncMock(return_value='switched'))
    host = SimpleNamespace(data_dir=str(tmp_path), router=SimpleNamespace(engine=engine, inference_runtime_id='llamacpp'),
                           require_runtime=lambda: SimpleNamespace(models=models))
    library = LocalModelLibrary(host, client_factory=factory, hardware_reader=lambda: {'ram_available_mb': 1000})
    library._headers = AsyncMock(return_value={})
    return library, requests, models


async def finish(library, job):
    task = library._tasks.get(job['id'])
    if task:
        await task
    return library.jobs[job['id']]


@pytest.mark.asyncio
async def test_catalog_download_activation_and_delete_use_one_library(tmp_path):
    library, requests, models = stack(tmp_path)
    assert (await library.search('tiny'))[0]['repo'] == 'owner/model'
    catalog = await library.files('owner/model')
    assert catalog['revision'] == REVISION
    job = await library.download('owner/model', ['tiny.gguf'], revision=REVISION, request_id='download-1')
    assert library.installed() == []  # .part/staging never becomes an installed model
    done = await finish(library, job)
    assert done['status'] == 'done'
    assert done['done_bytes'] == len(PAYLOAD)
    assert '/resolve/' + REVISION + '/' in str(requests[-1].url)
    row = (await library.snapshot())['installed'][0]
    assert row['managed_download'] and Path(row['path']).read_bytes() == PAYLOAD
    repeated = await library.download('owner/model', ['tiny.gguf'], revision=REVISION, request_id='download-1')
    assert repeated['id'] == job['id']
    await library.activate(row['id'])
    models.restart_engine.assert_awaited_once_with(row['path'], '')
    await library.delete(row['id'])
    assert library.installed() == []


@pytest.mark.asyncio
async def test_selected_projector_survives_publication_and_activation(tmp_path):
    library, _, models = stack(tmp_path, files={
        'tiny.gguf': PAYLOAD, 'other.gguf': PAYLOAD,
        'vision/mmproj-selected.gguf': PAYLOAD, 'vision/mmproj-other.gguf': PAYLOAD,
    })
    with pytest.raises(ValueError, match='Choose one model size'):
        await library.download('owner/model', ['tiny.gguf', 'other.gguf'])
    with pytest.raises(ValueError, match='at most one'):
        await library.download('owner/model', ['tiny.gguf', 'vision/mmproj-selected.gguf', 'vision/mmproj-other.gguf'])
    job = await library.download('owner/model', ['tiny.gguf', 'vision/mmproj-selected.gguf'])
    assert (await finish(library, job))['status'] == 'done'
    row, = library.installed()
    assert row['vision'] and Path(row['mmproj']).name == 'mmproj-selected.gguf'
    assert row['source']['projector'] == 'vision/mmproj-selected.gguf'
    await library.activate(row['id'])
    models.restart_engine.assert_awaited_once_with(row['path'], row['mmproj'])


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [b'GGUF' + b'x' * 100, b'not a model', PAYLOAD[:-1]])
async def test_bad_download_never_publishes(tmp_path, body):
    library, _, _ = stack(tmp_path, handler=lambda request: httpx.Response(200, content=body)
                           if '/resolve/' in request.url.path else None)
    job = await library.download('owner/model', ['tiny.gguf'], request_id='invalid')
    result = await finish(library, job)
    assert result['status'] == 'failed'
    assert library.installed() == []
    assert not (library.staging / job['id']).exists()


@pytest.mark.asyncio
async def test_split_models_require_every_part_and_only_first_is_selectable(tmp_path):
    paths = ['tiny-00001-of-00002.gguf', 'tiny-00002-of-00002.gguf']
    library, _, _ = stack(tmp_path, files=dict.fromkeys(paths, PAYLOAD))
    with pytest.raises(ValueError, match='every part'):
        await library.download('owner/model', paths[:1])
    job = await library.download('owner/model', paths)
    assert (await finish(library, job))['status'] == 'done'
    installed = library.installed()
    assert len(installed) == 1
    assert installed[0]['path'].endswith(paths[0])


@pytest.mark.asyncio
async def test_cancel_before_first_instruction_and_after_stream_starts(tmp_path):
    started = asyncio.Event()
    class Stalled(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            await asyncio.Event().wait()
            yield PAYLOAD
    library, _, _ = stack(tmp_path, handler=lambda request: httpx.Response(200, stream=Stalled())
                           if '/resolve/' in request.url.path else None)
    first = await library.download('owner/model', ['tiny.gguf'], request_id='before')
    assert await library.cancel(first['id'])
    second = await library.download('owner/model', ['tiny.gguf'], request_id='during')
    await started.wait()
    assert await library.cancel(second['id'])
    assert library.jobs[second['id']]['status'] == 'cancelled'
    assert library.installed() == []
    assert not (library.staging / second['id']).exists()


def test_restart_marks_unfinished_jobs_interrupted(tmp_path):
    path = tmp_path / 'models' / 'download-jobs.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'jobs': [{'id': 'old', 'status': 'running'}]}), encoding='utf-8')
    library, _, _ = stack(tmp_path)
    assert library.jobs['old']['status'] == 'interrupted'
    assert json.loads(path.read_text())['jobs'][0]['status'] == 'interrupted'


@pytest.mark.asyncio
async def test_model_change_failure_and_selected_delete_are_truthful(tmp_path):
    library, _, models = stack(tmp_path)
    job = await library.download('owner/model', ['tiny.gguf'])
    await finish(library, job)
    row = library.installed()[0]
    models.restart_engine.return_value = 'rolled_back'
    with pytest.raises(RuntimeError, match='rolled_back'):
        await library.activate(row['id'])
    library.host.router.engine.model = row['path']
    library.host.router.engine.ready = True
    with pytest.raises(ValueError, match='Eject'):
        await library.delete(row['id'])
    assert Path(row['path']).exists()
    await library.eject()
    library.host.router.engine.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_identity_conflict_does_not_start_a_second_download(tmp_path):
    library, _, _ = stack(tmp_path)
    job = await library.download('owner/model', ['tiny.gguf'], request_id='one')
    with pytest.raises(ValueError, match='different selection'):
        await library.download('other/model', ['tiny.gguf'], request_id='one')
    await library.cancel(job['id'])


@pytest.mark.asyncio
async def test_settings_websocket_always_correlates_success_and_failure(tmp_path):
    import ws_local_models
    library, _, _ = stack(tmp_path)
    handlers = {}
    def register(*names):
        def decorate(fn):
            handlers.update(dict.fromkeys(names, fn))
            return fn
        return decorate
    ws_local_models.register(register)
    socket = SimpleNamespace(send_json=AsyncMock())
    await handlers['local-models:get'](SimpleNamespace(local_models=library), socket, None,
                                      {'type': 'local-models:get', 'request_id': 'snapshot'})
    assert socket.send_json.await_args.args[0]['ok']
    assert socket.send_json.await_args.args[0]['request_id'] == 'snapshot'
    await handlers['local-models:activate'](SimpleNamespace(local_models=library), socket, None,
                                           {'type': 'local-models:activate', 'request_id': 'unknown', 'model_id': 'missing'})
    assert not socket.send_json.await_args.args[0]['ok']
    assert socket.send_json.await_args.args[0]['request_id'] == 'unknown'


@pytest.mark.asyncio
@pytest.mark.parametrize('save_fails', [False, True])
async def test_deleting_ejected_selected_model_commits_or_restores_selection(tmp_path, save_fails):
    from tests.test_engine_manager_lifecycle import _Router, _Engine
    library, _, _ = stack(tmp_path)
    job = await library.download('owner/model', ['tiny.gguf'])
    await finish(library, job)
    row = library.installed()[0]
    engine = _Engine(model=row['path'], mmproj='', ready=False)
    router = _Router(engine)
    library.host.router = router
    if save_fails:
        original_save = router.save_config
        def failed_save(*, strict=False):
            if strict:
                raise OSError('settings write failed')
            return original_save(strict=strict)
        router.save_config = failed_save
        with pytest.raises(OSError, match='settings write failed'):
            await library.delete(row['id'])
        assert Path(row['path']).exists()
        assert router.cfg['local']['model'] == row['path']
        assert engine.model == row['path']
    else:
        await library.delete(row['id'])
        assert not Path(row['path']).exists()
        assert router.cfg['local']['model'] == ''
        assert engine.model == ''


@pytest.mark.asyncio
async def test_shutdown_while_catalog_is_loading_cannot_spawn_late_download(tmp_path):
    library, _, _ = stack(tmp_path)
    original = library.files
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(*args):
        entered.set()
        await release.wait()
        return await original(*args)
    library.files = delayed
    pending = asyncio.create_task(library.download('owner/model', ['tiny.gguf']))
    await entered.wait()
    await library.shutdown()
    release.set()
    with pytest.raises(RuntimeError, match='shutting down'):
        await pending
    assert not library._tasks and not library.jobs


def test_corrupt_download_history_does_not_prevent_model_inventory(tmp_path):
    path = tmp_path / 'models' / 'download-jobs.json'
    path.parent.mkdir()
    path.write_text('{incomplete json', encoding='utf-8')
    library, _, _ = stack(tmp_path)
    assert library.state_warning
    assert library.installed() == []
