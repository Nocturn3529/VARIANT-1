"""Browser selection and user-resolvable startup state owned by Browser Fabric."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
from typing import Any, Callable
import uuid

from core_invariants import canonical_json
from .models import BrowserConflict, BrowserUnavailable, BrowserNotFound, BrowserValidationError
from .personal_profiles import BrowserProfileRequired, discover_browsers, selected_source
from .settings import MODES, normalize_options, settings_catalog


@dataclass
class _Pending:
    operation_id: str = field(default_factory=lambda: 'browser_wait_' + uuid.uuid4().hex)
    event: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: bool = False


class BrowserPreferences:
    def __init__(self, fabric: Any, *, discover: Callable | None = None):
        self.fabric = fabric
        self.store = fabric.store
        self.discover = discover or discover_browsers
        self.publisher = None
        self._pending: dict[str, _Pending] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._active: set[str] = set()
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._closing = False

    def default(self, fallback: str = 'embedded') -> dict:
        row = self.store.browser_preference('@default')
        return {'selection': row['selection'] or {'mode': fallback}, 'revision': row['revision']}

    def effective(self, chat_id: str, fallback: str = 'embedded') -> dict:
        row = self.store.browser_preference(chat_id)
        return {**row, 'selection': row['selection'] or self.default(fallback)['selection'],
                'selection_source': 'chat' if row['selection'] else 'default'}

    async def settings(self) -> dict:
        browsers = await asyncio.to_thread(self.discover)
        return {'default': self.default(), 'catalog': settings_catalog(), 'browsers': [
            {key: browser[key] for key in ('id', 'label', 'profiles')} for browser in browsers
        ]}

    def recordings(self, chat_id: str) -> list[dict]:
        """Expose recordings only through their durable owning chat/profile."""
        from pathlib import Path
        rows = []
        seen = set()
        for session_id in self.store.session_ids_for_chat(chat_id, include_closed=True):
            session = self.fabric.session(session_id)
            profile = self.store.get_profile(session.profile_id)
            root = Path(profile.user_data_dir) / 'recordings' / session_id
            for path in root.glob('*.webm'):
                resolved = path.resolve()
                if not resolved.is_relative_to(root.resolve()) or str(resolved) in seen:
                    continue
                seen.add(str(resolved))
                stat = path.stat()
                rows.append({'name': path.name, 'path': str(resolved), 'bytes': stat.st_size,
                             'modified_at': stat.st_mtime, 'session_id': session_id,
                             'complete': session_id not in self.fabric._adapters})
        return sorted(rows, key=lambda row: row['modified_at'], reverse=True)

    @staticmethod
    def normalize(selection: Any) -> dict:
        if not isinstance(selection, dict) or selection.get('mode') not in MODES:
            raise BrowserValidationError('Choose embedded, managed, personal, CDP, or cloud browser mode.')
        result = {'mode': selection['mode'], **normalize_options(selection)}
        if result['mode'] == 'personal':
            for key in ('browser_id', 'profile_id'):
                if not isinstance(selection.get(key), str) or not selection[key].strip():
                    raise BrowserProfileRequired('selection_required', 'Choose a browser and profile explicitly.')
                result[key] = selection[key].strip()
        return result

    def state(self, chat_id: str) -> dict:
        row = self.effective(chat_id)
        status = dict(row['state'])
        pending = self._pending.get(chat_id)
        if status.get('pending_operation_id') and pending is None:
            status = {'state': 'connection_failed', 'message': 'The previous browser operation was interrupted. Resume the task to retry.', 'actions': ['select_profile']}
        elif status.get('browser_session_id') and status.get('state') in {'ready', 'connecting', 'login_required'} and chat_id not in self._active:
            if status['browser_session_id'] not in self.fabric._adapters:
                status = {**status, 'state': 'connection_failed', 'message': 'The browser session needs to reconnect. Resume the task to reopen the same profile.', 'actions': ['select_profile']}
        return {'type': 'browser:state', 'chat_id': chat_id,
                'state': 'idle', 'message': '', 'actions': [], **status,
                'selection': row['selection'], 'selection_source': row['selection_source'], 'revision': row['revision']}

    async def _publish(self, chat_id: str) -> None:
        if self.publisher is not None:
            try:
                await self.publisher(self.state(chat_id))
            except Exception:
                pass  # State remains authoritative and can be fetched on reconnect.

    async def refresh_state(self, chat_id: str) -> dict:
        """Check embedded host inventory without creating tabs or replaying work."""
        from work_fabric.scope import WorkScope

        if chat_id in self._pending or chat_id in self._active:
            return self.state(chat_id)
        row = self.effective(chat_id)
        if row['selection']['mode'] != 'embedded':
            return self.state(chat_id)
        scope = WorkScope(chat_id=chat_id)
        session = self.fabric.session_for_owner(owner_kind='chat', owner_id=chat_id, scope=scope)
        if session is None or not self.matches(session, row['selection'], chat_id):
            await self._state(chat_id, 'idle', 'No browser session is open for this chat.')
            return self.state(chat_id)
        try:
            session = await self.fabric.reconcile_current_page(
                session.session_id, scope=scope, create_if_empty=False,
            )
        except BrowserUnavailable as exc:
            await self.connection_failed(chat_id, session, exc)
            return self.state(chat_id)
        target = (self.fabric.store.get_target(session.current_target_id)
                  if session.current_target_id else None)
        if target is None:
            await self._state(chat_id, 'idle', 'No browser tab is open for this chat.',
                              browser_session_id=session.session_id)
        else:
            await self._state(chat_id, 'ready' if target.url and target.url != 'about:blank' else 'connecting',
                              browser_session_id=session.session_id)
        return self.state(chat_id)

    async def _state(self, chat_id: str, state: str, message: str = '', **fields) -> None:
        prior = self.store.browser_preference(chat_id)['state']
        retained = {key: prior[key] for key in ('adopted_profile_id',) if key in prior}
        self.store.update_browser_preference(chat_id, state={'state': state, 'message': message, 'actions': [], **retained, **fields})
        await self._publish(chat_id)

    async def set_selection(self, scope: str, chat_id: str, selection: Any, expected_revision: int | None = None) -> dict:
        if scope not in {'default', 'chat'} or (scope == 'chat' and not chat_id):
            raise BrowserValidationError('Browser selection needs a default scope or a chat ID.')
        chosen = self.normalize(selection)
        if chosen['mode'] == 'personal':
            selected_source(await asyncio.to_thread(self.discover), chosen)
        if scope == 'chat' and chat_id in self._active and chat_id not in self._pending:
            raise BrowserConflict('Wait for the current browser operation before changing its profile.')
        owner = '@default' if scope == 'default' else chat_id
        row = self.store.update_browser_preference(owner, selection=chosen,
            state={'state': 'idle', 'message': '', 'actions': []}, expected_revision=expected_revision)
        if scope == 'chat':
            pending = self._pending.get(chat_id)
            if pending:
                pending.event.set()
            await self._publish(chat_id)
        return {'scope': scope, 'chat_id': chat_id if scope == 'chat' else '',
                'selection': chosen, 'revision': row['revision']}

    async def resolve(self, chat_id: str, operation_id: str, action: str) -> None:
        pending = self._pending.get(chat_id)
        if pending is None or pending.operation_id != operation_id:
            raise BrowserConflict('This browser operation is no longer waiting. Refresh its state.')
        if action not in {'retry', 'cancel'}:
            raise BrowserValidationError('Unsupported browser recovery action.')
        pending.cancelled = action == 'cancel'
        pending.event.set()

    async def _wait(self, chat_id: str, error: BrowserUnavailable) -> None:
        from capability_broker import current_capability_invocation
        if self._closing:
            raise asyncio.CancelledError()
        pending = self._pending.setdefault(chat_id, _Pending())
        pending.event.clear()
        state = getattr(error, 'state', 'connection_failed')
        await self._state(chat_id, state, str(error), pending_operation_id=pending.operation_id,
                          actions=['select_profile', 'retry', 'cancel'])
        context = current_capability_invocation()
        waiter = getattr(context, 'user_wait', None)
        if waiter is not None:
            waiter.set()
        try:
            await pending.event.wait()
            if pending.cancelled:
                raise asyncio.CancelledError()
        finally:
            if waiter is not None:
                waiter.clear()
            self._pending.pop(chat_id, None)

    @staticmethod
    def selection_key(selection: dict) -> str:
        return hashlib.sha256(canonical_json(selection).encode()).hexdigest()

    def adopt(self, chat_id: str, session: Any) -> None:
        """Pin an explicitly opened legacy/profile handle without replacing it."""
        if chat_id and self.store.browser_preference(chat_id)['selection'] is None:
            self.store.update_browser_preference(chat_id, selection={'mode': session.kind},
                state={'state': 'connecting', 'browser_session_id': session.session_id, 'adopted_profile_id': session.profile_id}, expected_revision=0)

    def matches(self, session: Any, selection: dict, chat_id: str = '') -> bool:
        if selection['mode'] == 'embedded':
            return session.kind == 'embedded' and session.scope.chat_id == chat_id
        if session.kind != 'managed':
            return False
        if session.metadata.get('selection_key') == self.selection_key(selection):
            return True
        adopted = self.store.browser_preference(chat_id)['state'].get('adopted_profile_id') if chat_id else None
        return selection == {'mode': 'managed'} and bool(adopted) and adopted == session.profile_id

    async def session(self, chat_id: str, *, scope: Any, metadata: dict, current_session_id: str = '', fallback: str = 'embedded', open_if_needed: bool = True):
        async with self._locks.setdefault(chat_id, asyncio.Lock()):
            if self._closing:
                raise asyncio.CancelledError()
            self._active.add(chat_id)
            self._active_tasks[chat_id] = asyncio.current_task()
            try:
                row = self.effective(chat_id, fallback)
                if row['selection_source'] == 'default':
                    self.store.update_browser_preference(chat_id, selection=row['selection'], state={'state': 'idle'}, expected_revision=0)
                while True:
                    selection = self.effective(chat_id, fallback)['selection']
                    try:
                        source = selected_source(await asyncio.to_thread(self.discover), selection) if selection['mode'] == 'personal' else None
                        saved = self.store.browser_preference(chat_id)['state']
                        candidates = [current_session_id, saved.get('browser_session_id')]
                        prior = self.fabric.session_for_owner(owner_kind='chat', owner_id=chat_id, scope=scope)
                        if prior is not None:
                            candidates.append(prior.session_id)
                        mismatched = []
                        for candidate in dict.fromkeys(filter(None, candidates)):
                            try:
                                session = self.fabric.session(candidate)
                            except BrowserNotFound:
                                continue
                            if session.state == 'closed':
                                continue
                            if self.matches(session, selection, chat_id):
                                session = await self.fabric.acquire_session(candidate, scope=scope)
                                if session.kind == 'embedded' and session.metadata.get('selection_key') != self.selection_key(selection):
                                    from .adapters import EMBEDDED_CAPABILITIES
                                    caps = EMBEDDED_CAPABILITIES if selection.get('evaluate_enabled', True) else EMBEDDED_CAPABILITIES - {'evaluate'}
                                    session = self.store.update_session(session.session_id,
                                        metadata={**session.metadata, 'selection_key': self.selection_key(selection), 'browser_settings': selection},
                                        capabilities=sorted(caps))
                                    adapter = self.fabric._adapters.get(session.session_id)
                                    if adapter is not None:
                                        adapter.capabilities = caps
                                target = (self.fabric.store.get_target(session.current_target_id)
                                          if session.current_target_id else None)
                                state = 'ready' if target and target.url and target.url != 'about:blank' else 'connecting'
                                await self._state(chat_id, state, browser_session_id=session.session_id)
                                return session
                            mismatched.append(session)
                        if not open_if_needed:
                            raise BrowserUnavailable('No browser session is open for the selected profile.')
                        for old in mismatched:
                            if old.state != 'closed':
                                await self.fabric.close_session(old.session_id, scope=scope)
                        await self._state(chat_id, 'connecting', 'Opening the selected browser profile.')
                        options = {'kind': 'embedded' if selection['mode'] == 'embedded' else 'managed',
                                   'scope': scope, 'headless': not selection.get('headed', True),
                                   'metadata': {**metadata, 'selection_key': self.selection_key(selection),
                                                'browser_settings': selection}}
                        if selection['mode'] != 'embedded':
                            name = 'selected-' + self.selection_key(selection)[:16]
                            profile = self.fabric.create_profile(name, kind='managed', persistent=True, scope=scope,
                                metadata={'personal_source': source} if source is not None else {})
                            options['profile_id'] = profile.profile_id
                        session = await self.fabric.open_session(**options)
                        await self._state(chat_id, 'connecting', 'Browser opened; waiting for page navigation.', browser_session_id=session.session_id)
                        return session
                    except BrowserUnavailable as exc:
                        if not open_if_needed:
                            raise
                        await self._wait(chat_id, exc)
                        current_session_id = ''
            except asyncio.CancelledError:
                await self._state(chat_id, 'cancelled', 'Browser operation cancelled.')
                raise
            finally:
                self._active.discard(chat_id)
                self._active_tasks.pop(chat_id, None)
                self._pending.pop(chat_id, None)

    async def observation(self, chat_id: str, observation: Any) -> None:
        if not chat_id:
            return
        # A chat without an explicit override still owns the effective default
        # selection.  Requiring a stored chat selection here discarded the
        # first successful observation from the default embedded browser and
        # could leave the connection banner permanently in ``connecting``.
        selection = self.effective(chat_id)['selection']
        session = self.fabric.session(observation.session_id)
        if not self.matches(session, selection, chat_id):
            return
        # A visible password field is an observation, not a prohibition on
        # using the returned page handles or completing an authorized login.
        needs_login = any(item.visible and (str(item.role).lower() in {'password', 'passwordbox'}
                          or str(item.input_type).lower() == 'password') for item in observation.elements)
        state = 'login_required' if needs_login else ('ready' if observation.url and observation.url != 'about:blank' else 'connecting')
        await self._state(chat_id, state, 'The page presents a login form.' if needs_login else '', browser_session_id=session.session_id)

    async def connection_failed(self, chat_id: str, session: Any, error: Exception) -> None:
        if not chat_id:
            return
        selection = self.effective(chat_id)['selection']
        if self.matches(session, selection, chat_id):
            await self._state(chat_id, 'connection_failed', str(error), browser_session_id=session.session_id, actions=['select_profile'])

    async def delete_chat(self, chat_id: str) -> None:
        pending = self._pending.get(chat_id)
        if pending:
            pending.cancelled = True
            pending.event.set()
        task = self._active_tasks.get(chat_id)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self._locks.setdefault(chat_id, asyncio.Lock()):
            self.store.delete_browser_preference(chat_id)

    async def close(self) -> None:
        self._closing = True
        for pending in self._pending.values():
            pending.cancelled = True
            pending.event.set()
        tasks = [task for task in self._active_tasks.values() if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
