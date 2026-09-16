"""Provider routing and closed-modal identity regressions from PB09."""
import ctypes
from dataclasses import replace
from types import SimpleNamespace

import pytest

from desktop_fabric import AdapterDispatch, DesktopElement, DesktopStaleReference, WindowsDesktopAdapter
from test_desktop_fabric import FakeDesktopAdapter, runtime


@pytest.mark.asyncio
@pytest.mark.parametrize('class_name,role,bounds,delivery,expected', [
    ('Qt6111QWindowIcon', 'Edit', (1, 2, 30, 40), 'auto', 'physical'),
    ('qt515QWindowIcon', 'Edit', (1, 2, 30, 40), 'auto', 'physical'),
    ('Qt6111QWindowIcon', 'Edit', None, 'auto', 'semantic'),
    ('Qt6111QWindowIcon', 'Spinner', (1, 2, 30, 40), 'auto', 'semantic'),
    ('Notepad', 'Edit', (1, 2, 30, 40), 'auto', 'semantic'),
    ('#32770', 'Edit', (1, 2, 30, 40), 'auto', 'semantic'),
    ('Qt6111QWindowIcon', 'Edit', (1, 2, 30, 40), 'semantic', 'semantic'),
])
async def test_qt_edit_uses_real_typing_without_changing_other_providers(class_name, role, bounds, delivery, expected):
    calls = []
    class Adapter(WindowsDesktopAdapter):
        def _assert_live(self, *args, **kwargs):
            pass
        async def _physical_dispatch(self, *args, **kwargs):
            calls.append('physical')
            return AdapterDispatch(True, 'physical.text')
        async def _semantic_dispatch(self, *args, **kwargs):
            calls.append('semantic')
            return AdapterDispatch(True, 'uia.value')
    adapter = Adapter(desktop_control=object(), catalog=object())
    window = replace(FakeDesktopAdapter().window, class_name=class_name)
    element = DesktopElement(element_ref='edit', observation_id='before',
        window_id=window.window_id, window_generation=1, element_generation=1,
        role=role, patterns=('value',), bounds=bounds)
    result = await adapter.dispatch(window, action='set_value', delivery=delivery,
                                    element=element, arguments={'value': '#101820'})
    assert calls == [expected]  # choose once before any input; never semantic-then-retry
    assert result.metadata['selected_delivery'] == expected


@pytest.mark.parametrize('foreground,pid,still_live,expected', [
    (1234, 42, False, True),
    (1234, 42, True, False),
    (9999, 42, False, False),
    (1234, 55, False, False),
])
def test_closed_dialog_returns_only_to_recorded_owner(monkeypatch, foreground, pid, still_live, expected):
    class User32:
        def GetForegroundWindow(self): return foreground
        def GetWindowThreadProcessId(self, hwnd, pointer): pointer._obj.value = pid
        def IsWindow(self, hwnd): return still_live
    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(user32=User32()), raising=False)
    adapter = WindowsDesktopAdapter(desktop_control=object(), catalog=object())
    monkeypatch.setattr(adapter, 'validate_window', lambda w: {'live': still_live, 'foreground': False})
    monkeypatch.setattr(adapter, '_owned_hwnd', lambda *args: False)
    dialog = replace(FakeDesktopAdapter().window, hwnd=5678, owner_hwnd=1234)
    state = adapter.post_action_target(dialog)
    assert state['returned_to_owner'] is expected
    assert state['accepted'] is expected


@pytest.mark.asyncio
@pytest.mark.parametrize('same_process_generation', [True, False])
async def test_returned_owner_is_bound_only_in_same_process_generation(tmp_path, same_process_generation):
    fake = FakeDesktopAdapter()
    fabric, _ = runtime(tmp_path, fake)
    dialog = replace(fake.window, hwnd=5678, window_id='dialog', owner_hwnd=fake.window.hwnd,
                     pid_started_at=fake.window.pid_started_at if same_process_generation else 99)
    fake.post_action_target = lambda w: {'returned_to_owner': True, 'foreground_hwnd': fake.window.hwnd,
                                        'accepted': True}
    after, state = await fabric._post_action_window(dialog)
    if same_process_generation:
        assert after.window_id == fake.window.window_id
        assert state['related_window_id'] == after.window_id
    else:
        assert after is dialog and state['related_window_unresolved']


@pytest.mark.asyncio
@pytest.mark.parametrize('return_to_owner', [True, False])
async def test_async_modal_close_rechecks_observation_once_without_replaying_input(tmp_path, return_to_owner):
    class ClosingAdapter(FakeDesktopAdapter):
        def __init__(self):
            super().__init__()
            self.parent = self.window
            self.window = replace(self.parent, window_id='dialog', hwnd=5678, owner_hwnd=self.parent.hwnd)
            self.dispatched = False
            self.vanished = False
            self.post_checks = 0
            self.observed_windows = []
        def catalog(self, **kwargs):
            return [self.app], [self.parent] + ([] if self.vanished else [self.window])
        def validate_window(self, window):
            live = window.hwnd == self.parent.hwnd or (window.hwnd == self.window.hwnd and not self.vanished)
            return {'live': live, 'foreground': live, 'reason': 'identity_match' if live else 'hwnd_missing'}
        def post_action_target(self, window):
            self.post_checks += 1
            returning = self.vanished and return_to_owner
            return {'accepted': not self.vanished or returning, 'returned_to_owner': returning,
                    'foreground_hwnd': self.parent.hwnd if returning else self.window.hwnd}
        async def dispatch(self, *args, **kwargs):
            self.dispatches += 1
            self.dispatched = True
            return AdapterDispatch(True, 'physical.keyboard', metadata={'selected_delivery':'physical'})
        async def observe(self, window, **kwargs):
            self.observed_windows.append(window.window_id)
            if self.dispatched and window.window_id == 'dialog':
                self.vanished = True
                raise DesktopStaleReference('modal vanished during observation')
            return await super().observe(window, **kwargs)
    fake = ClosingAdapter()
    fabric, _ = runtime(tmp_path, fake)
    result = await fabric.act('dialog', 'click', target={'role':'Button','name':'Save'}, include_image_evidence=False)
    assert fake.dispatches == 1 and fake.post_checks == 2
    assert fake.observed_windows.count('dialog') == 2  # before input and failed after
    if return_to_owner:
        assert result.state == 'verified'
        assert result.dispatch['post_action_owner_recheck'] is True
        assert fake.observed_windows.count(fake.parent.window_id) == 1
        assert fabric.repository.get_observation(result.after_observation_id).window_id == fake.parent.window_id
    else:
        assert result.state == 'unknown_effect'
        assert fake.parent.window_id not in fake.observed_windows
