"""Explicit Chromium profile discovery and owned persistent snapshots.

Profile discovery reads labels only. Login-store copies happen only when a
selected personal profile is opened. The source browser is never closed here.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
from typing import Any
import uuid

from .models import BrowserUnavailable


class BrowserProfileRequired(BrowserUnavailable):
    def __init__(self, state: str, message: str):
        super().__init__(message)
        self.state = state


def _identity(prefix: str, path: Path) -> str:
    return prefix + hashlib.sha256(os.path.normcase(str(path.resolve())).encode()).hexdigest()[:24]


def discover_browsers() -> list[dict[str, Any]]:
    local = os.environ.get('LOCALAPPDATA')
    if not local:
        return []
    roots = [Path(value) for value in [os.environ.get('PROGRAMFILES'), os.environ.get('PROGRAMFILES(X86)'), local] if value]
    families = [
        ('Google Chrome', 'Google/Chrome/Application/chrome.exe', 'Google/Chrome/User Data'),
        ('Microsoft Edge', 'Microsoft/Edge/Application/msedge.exe', 'Microsoft/Edge/User Data'),
        ('Brave', 'BraveSoftware/Brave-Browser/Application/brave.exe', 'BraveSoftware/Brave-Browser/User Data'),
        ('Chromium', 'Chromium/Application/chrome.exe', 'Chromium/User Data'),
    ]
    found = []
    for label, executable_suffix, data_suffix in families:
        executable = next((root / executable_suffix for root in roots if (root / executable_suffix).is_file()), None)
        data = Path(local) / data_suffix
        if executable is None or not (data / 'Local State').is_file():
            continue
        try:
            state = json.loads((data / 'Local State').read_text(encoding='utf-8'))
            cache = (state.get('profile') or {}).get('info_cache') or {}
        except (OSError, ValueError, TypeError):
            continue
        profiles = []
        for directory, entry in cache.items():
            if not isinstance(directory, str) or Path(directory).name != directory or directory in {'.', '..', 'System Profile', 'Guest Profile'}:
                continue
            source = data / directory
            if not source.is_dir() or not source.resolve().is_relative_to(data.resolve()):
                continue
            name = entry.get('name') if isinstance(entry, dict) else None
            profiles.append({'id': _identity('personal_', source), 'label': str(name or directory), 'directory_name': directory})
        found.append({'id': _identity('browser_', data), 'label': label, 'executable': str(executable), 'user_data_dir': str(data), 'profiles': profiles})
    return found


def selected_source(browsers: list[dict[str, Any]], selection: dict[str, Any]) -> dict[str, str]:
    browser = next((item for item in browsers if item['id'] == selection.get('browser_id')), None)
    profile = next((item for item in browser['profiles'] if item['id'] == selection.get('profile_id')), None) if browser else None
    if browser is None or profile is None:
        raise BrowserProfileRequired('selection_required', 'The selected browser profile is unavailable. Choose the intended profile in Browser settings.')
    return {key: str(value) for key, value in {
        'browser_id': browser['id'], 'profile_id': profile['id'], 'label': profile['label'],
        'executable': browser['executable'], 'user_data_dir': browser['user_data_dir'], 'directory_name': profile['directory_name'],
    }.items()}


_PROFILE_FILES = ('Preferences', 'Secure Preferences', 'Bookmarks', 'Cookies', 'Login Data', 'Login Data For Account', 'Web Data', 'Network/Cookies')
_PROFILE_DIRECTORIES = ('Local Storage', 'IndexedDB', 'Session Storage', 'Storage')


def _prepare_snapshot(source: dict[str, str], destination: str, stop: threading.Event) -> None:
    root = Path(destination).resolve()
    parent = root.parent.resolve()
    src_root = Path(source['user_data_dir']).resolve()
    src = (src_root / source['directory_name']).resolve()
    if not src.is_dir() or not src.is_relative_to(src_root) or root == src_root or root.is_relative_to(src_root):
        raise BrowserProfileRequired('selection_required', 'The selected profile path is unavailable.')
    marker = root / '.variant1-profile-snapshot.json'
    if marker.is_file():
        saved = json.loads(marker.read_text(encoding='utf-8'))
        if saved.get('profile_id') == source['profile_id'] and (root / 'Default').is_dir():
            return
        raise BrowserProfileRequired('selection_required', 'The browser copy belongs to a different selected profile.')
    # A live Windows browser can exclusively lock these files. Check before
    # starting a copy, and report a specific recoverable state.
    for rel in ('Network/Cookies', 'Cookies', 'Login Data', 'Login Data For Account'):
        path = src / rel
        if path.is_file():
            try:
                with path.open('rb'):
                    pass
            except PermissionError as exc:
                raise BrowserProfileRequired('profile_locked', 'Close the selected source browser, including its background instance, then retry. VARIANT-1 will not close it for you.') from exc

    stage = parent / (root.name + '-snapshot-' + uuid.uuid4().hex)
    if not stage.resolve().is_relative_to(parent) or stage.resolve() in {parent, src_root, src}:
        raise BrowserUnavailable('Invalid managed snapshot destination')
    stage.mkdir(parents=True)
    (stage / 'Default').mkdir()
    def copy_file(path: Path, target: Path) -> None:
        if stop.is_set():
            raise InterruptedError('Browser profile copy cancelled')
        if not path.resolve().is_relative_to(src):
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        with path.open('rb') as incoming, target.open('wb') as outgoing:
            while chunk := incoming.read(1024 * 1024):
                if stop.is_set():
                    raise InterruptedError('Browser profile copy cancelled')
                outgoing.write(chunk)
    try:
        for rel in _PROFILE_FILES:
            path = src / rel
            if path.is_file():
                copy_file(path, stage / 'Default' / rel)
        for rel in _PROFILE_DIRECTORIES:
            path = src / rel
            if path.is_dir():
                for walk_root, dirs, files in os.walk(path, followlinks=False):
                    dirs[:] = [name for name in dirs if not (Path(walk_root) / name).is_symlink()
                               and (Path(walk_root) / name).resolve().is_relative_to(src)
                               and not os.path.isjunction(Path(walk_root) / name)]
                    for name in files:
                        original = Path(walk_root) / name
                        copy_file(original, stage / 'Default' / original.relative_to(src))
        state = json.loads((src_root / 'Local State').read_text(encoding='utf-8'))
        entry = ((state.get('profile') or {}).get('info_cache') or {}).get(source['directory_name'], {})
        local_state = {'os_crypt': state.get('os_crypt', {}), 'profile': {
            'info_cache': {'Default': entry}, 'last_used': 'Default', 'last_active_profiles': ['Default'],
        }}
        (stage / 'Local State').write_text(json.dumps(local_state), encoding='utf-8')
        if stop.is_set():
            raise InterruptedError('Browser profile copy cancelled')
        root.mkdir(parents=True, exist_ok=True)
        # Only this unlaunched, owned profile is populated. Marker is committed
        # last; partial copies are refreshed on the next explicit retry.
        for walk_root, dirs, files in os.walk(stage):
            for name in files:
                if stop.is_set():
                    raise InterruptedError('Browser profile copy cancelled')
                path = Path(walk_root) / name
                target = root / path.relative_to(stage)
                if not target.resolve().is_relative_to(root):
                    raise BrowserUnavailable('Snapshot destination escaped its owned profile')
                target.parent.mkdir(parents=True, exist_ok=True)
                path.replace(target)
        if stop.is_set():
            raise InterruptedError('Browser profile copy cancelled')
        marker.write_text(json.dumps({'profile_id': source['profile_id'], 'browser_id': source['browser_id']}), encoding='utf-8')
    except PermissionError as exc:
        raise BrowserProfileRequired('profile_locked', 'The selected profile is locked. Close its source browser and retry.') from exc
    finally:
        if stage.resolve().is_relative_to(parent) and stage.resolve() != parent:
            shutil.rmtree(stage)


async def prepare_snapshot(source: dict[str, str], destination: str) -> None:
    stop = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(_prepare_snapshot, source, destination, stop))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        stop.set()
        try:
            await task
        except Exception:
            pass
        raise
    except (OSError, ValueError) as exc:
        raise BrowserProfileRequired('connection_failed', f'The selected profile copy could not be prepared: {exc}') from exc
