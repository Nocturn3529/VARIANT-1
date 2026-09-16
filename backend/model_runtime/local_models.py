"""Hugging Face discovery and owned GGUF downloads for the canonical local runtime."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import time
from urllib.parse import quote
import uuid

import httpx

from . import engine_manager


_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SPLIT = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.I)
_SHA = re.compile(r"^[a-fA-F0-9]{40}$")
_TERMINAL = {"done", "failed", "cancelled", "interrupted"}
_LOG = logging.getLogger(__name__)


def _file_path(value):
    text = str(value or "")
    path = PurePosixPath(text)
    if (not text or path.is_absolute() or any(part in {"", ".", ".."} for part in text.split('/'))
            or any(char in text for char in '\\:\x00') or not text.lower().endswith('.gguf')):
        raise ValueError("Choose a repository-relative GGUF file.")
    return text


def _model_id(path):
    return 'local_' + hashlib.sha256(os.path.normcase(os.path.abspath(path)).encode()).hexdigest()[:24]


def _atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_suffix('.tmp')
    staged.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    staged.replace(path)


class LocalModelLibrary:
    def __init__(self, host, *, client_factory=None, hardware_reader=None):
        self.host = host
        self.root = Path(host.data_dir).resolve() / 'models'
        self.library = self.root / 'user' / 'huggingface'
        self.staging = self.root / '.downloads'
        self.state_path = self.root / 'download-jobs.json'
        self.client_factory = client_factory or httpx.AsyncClient
        self.hardware_reader = hardware_reader
        self._lock = asyncio.Lock()
        self._tasks = {}
        self._closed = False
        self.jobs = {}
        self.revision = 0
        self._hardware = None
        self._hardware_at = 0.0
        self.state_warning = ''
        if self.state_path.is_file():
            try:
                state = json.loads(self.state_path.read_text(encoding='utf-8'))
                if not isinstance(state, dict) or not isinstance(state.get('jobs', []), list):
                    raise ValueError('invalid download history')
            except (OSError, ValueError) as exc:
                self.state_warning = 'Download history could not be read; installed model files are still available.'
                _LOG.warning('Local model download history unavailable: %s', type(exc).__name__)
                state = {}
            raw_revision = state.get('revision', 0)
            self.revision = raw_revision if type(raw_revision) is int and raw_revision >= 0 else 0
            self.jobs = {row['id']: row for row in state.get('jobs', [])
                         if isinstance(row, dict) and isinstance(row.get('id'), str) and isinstance(row.get('status'), str)}
            changed = False
            for job in self.jobs.values():
                if job['status'] not in _TERMINAL:
                    target_id = str(job.get('target', ''))
                    manifest = self.library / target_id / 'variant1-download.json'
                    committed = False
                    if re.fullmatch('[a-f0-9]{24}', target_id) and manifest.is_file():
                        try:
                            saved = json.loads(manifest.read_text(encoding='utf-8'))
                            committed = all(saved.get(key) == job.get(key) for key in ('repo', 'revision', 'paths'))
                        except (OSError, ValueError):
                            pass
                    if committed:
                        job.update(status='done', phase='done', error='', done_bytes=job.get('total_bytes', 0))
                    else:
                        job.update(status='interrupted', error='Download interrupted by application restart; partial data was not published.')
                        stage = self.staging / job['id']
                        if (re.fullmatch(r'download_[a-f0-9]{32}', job['id']) and stage.exists()
                                and stage.resolve().is_relative_to(self.staging.resolve())):
                            try:
                                shutil.rmtree(stage)
                            except OSError:
                                self.state_warning = 'An interrupted partial download could not be removed.'
                    changed = True
            if changed:
                try:
                    self._save()
                except OSError:
                    self.state_warning = 'Recovered download state could not be saved.'

    def _save(self):
        self.revision += 1
        while len(self.jobs) > 100:
            old = next((key for key, job in self.jobs.items() if job['status'] in _TERMINAL), None)
            if old is None:
                break
            self.jobs.pop(old)
        _atomic_json(self.state_path, {'revision': self.revision, 'jobs': list(self.jobs.values())})

    async def _json(self, path, *, params=None):
        headers = await self._headers()
        async with self.client_factory(timeout=20, trust_env=False, follow_redirects=False) as client:
            response = await client.get('https://huggingface.co' + path, params=params, headers=headers)
        if response.status_code != 200:
            raise ValueError(f"Hugging Face returned HTTP {response.status_code}.")
        return response.json()

    async def _headers(self):
        from service_credentials import secret
        token = await secret(self.host.router, 'models', 'huggingface', env_vars=('HF_TOKEN',))
        return {'Authorization': 'Bearer ' + token} if token else {}

    async def search(self, query='', limit=20):
        data = await self._json('/api/models', params={
            'search': str(query).strip()[:200], 'filter': 'gguf', 'sort': 'downloads',
            'direction': -1, 'limit': max(1, min(int(limit), 50)),
        })
        if not isinstance(data, list):
            raise ValueError('Hugging Face returned an invalid model catalog.')
        return [{'repo': row['id'], 'downloads': row.get('downloads', 0),
                 'likes': row.get('likes', 0), 'gated': bool(row.get('gated')),
                 'updated': row.get('lastModified', '')}
                for row in data if isinstance(row, dict) and _REPO.fullmatch(str(row.get('id', '')))]

    async def files(self, repo, revision='main'):
        if not _REPO.fullmatch(str(repo)):
            raise ValueError('Repository must have the form owner/name.')
        data = await self._json('/api/models/' + quote(repo, safe='/') + '/revision/' + quote(str(revision), safe=''),
                                params={'blobs': 'true'})
        if not isinstance(data, dict) or not _SHA.fullmatch(str(data.get('sha', ''))):
            raise ValueError('The repository did not return an immutable revision.')
        files = []
        for raw in data.get('siblings', []):
            name = str(raw.get('rfilename', ''))
            if not name.lower().endswith('.gguf'):
                continue
            name = _file_path(name)
            lfs = raw.get('lfs') or {}
            files.append({'path': name, 'bytes': int(raw.get('size') or lfs.get('size') or 0),
                          'sha256': str(lfs.get('sha256') or ''),
                          'projector': 'mmproj' in PurePosixPath(name).name.casefold()})
        groups = {}
        for row in files:
            match = _SPLIT.search(row['path'])
            stem = row['path'][:match.start()] if match else row['path']
            groups.setdefault(stem, []).append(row)
        variants = []
        for name, parts in groups.items():
            parts.sort(key=lambda item: item['path'])
            match = _SPLIT.search(parts[0]['path'])
            complete = (not match or ([int(_SPLIT.search(part['path']).group(1)) for part in parts]
                                       == list(range(1, int(match.group(2)) + 1))
                                       and all(_SPLIT.search(part['path']).group(2) == match.group(2) for part in parts)))
            variants.append({'label': PurePosixPath(name).name, 'paths': [p['path'] for p in parts],
                             'bytes': sum(p['bytes'] for p in parts), 'complete': complete,
                             'projector': all(p['projector'] for p in parts)})
        return {'repo': repo, 'revision': data['sha'], 'files': files, 'variants': variants}

    def installed(self):
        engine = self.host.router.engine
        active = os.path.normcase(os.path.abspath(getattr(engine, 'model', '') or '.'))
        rows = []
        for row in engine_manager.scan_models(str(self.root.parent)):
            path = Path(row['path'])
            manifest = next((parent / 'variant1-download.json' for parent in path.parents
                             if parent.is_relative_to(self.library) and (parent / 'variant1-download.json').is_file()), None)
            owned = None
            if manifest:
                owned = json.loads(manifest.read_text(encoding='utf-8'))
                if owned.get('projector'):
                    projector = manifest.parent.joinpath(*PurePosixPath(_file_path(owned['projector'])).parts)
                    if projector.is_file() and projector.resolve().is_relative_to(manifest.parent.resolve()):
                        row = {**row, 'mmproj': str(projector), 'vision': True}
            rows.append({**row, 'id': _model_id(path), 'managed_download': bool(owned),
                         'source': owned or {}, 'active': bool(getattr(engine, 'ready', False)) and os.path.normcase(str(path)) == active})
        return rows

    async def snapshot(self):
        from .hardware import telemetry
        if self._hardware is None or time.monotonic() - self._hardware_at > 30:
            self._hardware = await asyncio.to_thread(self.hardware_reader or telemetry)
            self._hardware_at = time.monotonic()
        hardware = dict(self._hardware)
        return {'revision': self.revision, 'hardware': hardware, 'installed': self.installed(),
                'jobs': [dict(row) for row in self.jobs.values()],
                'runtime': self.host.runtime_installer.local_runtime_status() if getattr(self.host, 'runtime_installer', None) else {},
                'warning': self.state_warning,
                'supported_actions': ['search', 'files', 'download', 'cancel', 'activate', 'eject', 'delete']}

    async def download(self, repo, paths, *, revision='main', request_id=''):
        if self._closed:
            raise RuntimeError('Local model downloads are shutting down.')
        if not isinstance(paths, list) or not paths:
            raise ValueError('Choose at least one GGUF file.')
        chosen = sorted({_file_path(path) for path in paths})
        async with self._lock:
            for job in self.jobs.values():
                if request_id and job.get('request_id') == request_id:
                    if job['repo'] != repo or sorted(job['paths']) != chosen or job['requested_revision'] != revision:
                        raise ValueError('Download request ID already belongs to a different selection.')
                    return dict(job)
            catalog = await self.files(repo, revision)
            if self._closed:
                raise RuntimeError('Local model downloads are shutting down.')
            available = {row['path']: row for row in catalog['files']}
            if any(path not in available for path in chosen):
                raise ValueError('A selected file is missing from this repository revision.')
            for variant in catalog['variants']:
                if set(variant['paths']) & set(chosen) and (not variant['complete'] or not set(variant['paths']).issubset(chosen)):
                    raise ValueError('A split GGUF must include every part.')
            selected_variants = [variant for variant in catalog['variants'] if set(variant['paths']).issubset(chosen)]
            primary = [variant for variant in selected_variants if not variant['projector']]
            projectors = [variant for variant in selected_variants if variant['projector']]
            if len(primary) != 1 or len(projectors) > 1:
                raise ValueError('Choose one model size and at most one optional vision projector per download.')
            identity = hashlib.sha256(json.dumps([repo, catalog['revision'], chosen]).encode()).hexdigest()[:24]
            if any(job.get('target') == identity and job['status'] not in _TERMINAL for job in self.jobs.values()):
                raise ValueError('This model selection is already downloading.')
            if (self.library / identity).exists():
                raise ValueError('This model selection is already installed.')
            self.staging.mkdir(parents=True, exist_ok=True)
            total = sum(available[path]['bytes'] for path in chosen)
            if not total or any(available[path]['bytes'] <= 0 for path in chosen):
                raise ValueError('Download sizes are unavailable; refresh the repository files.')
            if shutil.disk_usage(self.staging).free < total + (64 << 20):
                raise ValueError('There is not enough free space for this download.')
            job = {'id': 'download_' + uuid.uuid4().hex, 'request_id': request_id, 'repo': repo,
                   'paths': chosen, 'revision': catalog['revision'], 'requested_revision': revision,
                   'target': identity, 'status': 'queued', 'phase': 'queued', 'done_bytes': 0,
                   'projector': projectors[0]['paths'][0] if projectors else '',
                   'total_bytes': total, 'started_at': time.time(), 'error': ''}
            self.jobs[job['id']] = job
            try:
                self._save()
            except BaseException:
                self.jobs.pop(job['id'], None)
                raise
            task = asyncio.create_task(self._download(job, available), name=job['id'])
            self._tasks[job['id']] = task
            task.add_done_callback(lambda done: self._download_finished(job['id'], done))
            return dict(job)

    def _download_finished(self, job_id, task):
        self._tasks.pop(job_id, None)
        if not task.cancelled() and task.exception() is not None:
            _LOG.error('Local model download settlement failed: %s', type(task.exception()).__name__)
            self.state_warning = 'A download result could not be saved. Refresh the installed model list before retrying.'

    async def _download(self, job, available):
        stage = self.staging / job['id']
        try:
            stage.mkdir(parents=True, exist_ok=False)
            job.update(status='running', phase='downloading')
            self._save()
            headers = await self._headers()
            async with self.client_factory(timeout=httpx.Timeout(60, connect=15), trust_env=False,
                                           follow_redirects=True) as client:
                for path in job['paths']:
                    dest = stage.joinpath(*PurePosixPath(path).parts)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    url = 'https://huggingface.co/' + quote(job['repo'], safe='/') + '/resolve/' + job['revision'] + '/' + quote(path, safe='/')
                    digest, size = hashlib.sha256(), 0
                    async with client.stream('GET', url, headers=headers) as response:
                        if response.status_code != 200:
                            raise ValueError(f"Model download returned HTTP {response.status_code}.")
                        with dest.open('xb') as stream:
                            async for chunk in response.aiter_bytes(1 << 20):
                                size += len(chunk)
                                if size > available[path]['bytes']:
                                    raise ValueError('Downloaded file exceeds the catalog size.')
                                stream.write(chunk)
                                digest.update(chunk)
                                job['done_bytes'] += len(chunk)
                                await asyncio.sleep(0)
                            stream.flush()
                            os.fsync(stream.fileno())
                    if size != available[path]['bytes']:
                        raise ValueError('Model download ended before the expected size.')
                    expected = available[path]['sha256']
                    if expected and digest.hexdigest().casefold() != expected.casefold():
                        raise ValueError('Model download checksum did not match the catalog.')
                    with dest.open('rb') as stream:
                        if stream.read(4) != b'GGUF':
                            raise ValueError('Downloaded file is not a GGUF model.')
            async with self._lock:
                job['phase'] = 'publishing'
                self._save()
                manifest = {'repo': job['repo'], 'revision': job['revision'], 'paths': job['paths'], 'projector': job['projector']}
                _atomic_json(stage / 'variant1-download.json', manifest)
                target = self.library / job['target']
                self.library.mkdir(parents=True, exist_ok=True)
                if not stage.resolve().is_relative_to(self.staging.resolve()) or not target.resolve().is_relative_to(self.library.resolve()):
                    raise ValueError('Model publication path changed.')
                stage.rename(target)
                job.update(status='done', phase='done', finished_at=time.time())
                self._save()
        except asyncio.CancelledError:
            job.update(status='cancelled', phase='cancelled', error='Download cancelled.', finished_at=time.time())
            self._save()
        except Exception as exc:
            job.update(status='failed', phase='failed', error=str(exc)[:500], finished_at=time.time())
            self._save()
        finally:
            if stage.exists() and stage.resolve().is_relative_to(self.staging.resolve()):
                shutil.rmtree(stage)

    async def cancel(self, job_id):
        task = self._tasks.get(job_id)
        if task is None or task.done():
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        job = self.jobs[job_id]
        if job['status'] not in _TERMINAL:  # cancelled before the coroutine's first instruction
            job.update(status='cancelled', phase='cancelled', error='Download cancelled.')
            self._save()
        return job['status'] == 'cancelled'

    async def activate(self, model_id):
        async with self._lock:
            row = next((row for row in self.installed() if row['id'] == model_id), None)
            if row is None:
                raise ValueError('This installed model is no longer available.')
            if self.host.router.inference_runtime_id != 'llamacpp':
                raise ValueError('Select the managed llama.cpp runtime before activating a GGUF model.')
            result = await self.host.require_runtime().models.restart_engine(row['path'], row['mmproj'])
            if result != 'switched':
                raise RuntimeError(f"Model activation did not complete ({result}).")
            return {'status': result, 'id': model_id}

    async def eject(self):
        async with self._lock:
            return await engine_manager.eject_local_model(self.host.router)

    async def delete(self, model_id):
        async with self._lock:
            row = next((row for row in self.installed() if row['id'] == model_id), None)
            if not row or not row['managed_download']:
                raise ValueError('Only models downloaded by VARIANT-1 can be deleted here.')
            target = next(parent for parent in Path(row['path']).parents
                          if parent.parent == self.library)
            await engine_manager.remove_local_model_directory(self.host.router, str(target),
                library_root=str(self.library), staging_root=str(self.staging))
            self._save()
            return {'deleted': model_id}

    async def shutdown(self):
        self._closed = True
        for job_id in tuple(self._tasks):
            await self.cancel(job_id)
