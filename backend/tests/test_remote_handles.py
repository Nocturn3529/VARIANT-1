from dataclasses import FrozenInstanceError, fields
import gc
import inspect
from pathlib import PureWindowsPath
from types import SimpleNamespace

import pytest

from kernel_runtime.worker_bridge import (
    REMOTE_HANDLE_DISPATCH_SCHEMA,
    KernelBridgeClient,
    Variant1BrowserObservation,
    Variant1CapabilityError,
    Variant1CommandResult,
    Variant1ConnectorSearchResult,
    Variant1McpResult,
    Variant1McpSchema,
    Variant1RemoteHandle,
    Variant1DesktopElements,
    Variant1DesktopViewResult,
    _decode_host_result,
    _encode_host_argument,
)


class RecordingBridge:
    def __init__(self):
        self.calls = []

    def invoke(self, descriptor, arguments):
        self.calls.append((descriptor, arguments))
        return {"ok": True}

    def invoke_async(self, descriptor, arguments, *, deadline_ms=None):
        self.calls.append((descriptor, arguments, deadline_ms))

        async def completed():
            return {"ok": True, "async": True}

        return completed()


def _envelope(**overrides):
    payload = {
        "service": "browser",
        "kind": "page",
        "id": "page-7",
        "generation": 3,
        "revision": 11,
        "metadata": {"title": "Example", "labels": ["active"]},
        "_dispatch": {
            "schema": REMOTE_HANDLE_DISPATCH_SCHEMA,
            "ref_id": "cap-remote-dispatch",
            "handler_revision": "remote-dispatch.v1",
            "catalog_release_id": "catalog-9",
            "category_id": "browser",
            "slot_id": "slot-browser",
            "slot_version": 4,
            "mount_revision": 8,
            # Unknown fields, especially credential-like ones, must never be
            # retained by or forwarded from the kernel handle.
            "secret": "not-retained",
        },
    }
    payload.update(overrides)
    return {"$variant1_handle": payload}


def test_valid_envelopes_decode_recursively_and_remain_immutable():
    bridge = RecordingBridge()
    result = _decode_host_result(
        {"plain": 7, "items": [_envelope(), {"nested": True}]},
        bridge,
    )

    handle = result["items"][0]
    assert result["plain"] == 7
    assert result["items"][1] == {"nested": True}
    assert isinstance(handle, Variant1RemoteHandle)
    assert handle.identity == {
        "service": "browser",
        "kind": "page",
        "id": "page-7",
        "generation": 3,
        "revision": 11,
    }
    metadata = handle.metadata
    metadata["labels"].append("mutated")
    assert handle.metadata == {"labels": ["active"], "title": "Example"}
    with pytest.raises(FrozenInstanceError):
        handle.revision = 12


def test_browser_page_display_uses_its_readable_title_metadata_key():
    handle = _decode_host_result(_envelope(
        metadata={'title':'Official downloads','state':'active'},
        methods={'schema':'variant1.remote-handle-methods.v1','items':[{'name':'observe','params':[]}]},
    ), RecordingBridge())
    assert "title='Official downloads'" in repr(handle)
    assert "name='Official downloads'" not in repr(handle)
    assert handle.title == handle['title'] == handle.get('title') == 'Official downloads'
    with pytest.raises(AttributeError):
        _ = handle.name


def test_remote_handles_encode_back_to_strict_json_identity_recursively():
    bridge = RecordingBridge()
    handle = _decode_host_result(_envelope(), bridge)

    encoded = _encode_host_argument({"target": handle, "items": (handle,)})

    assert encoded == {
        "target": handle.identity,
        "items": [handle.identity],
    }


def test_python_path_arguments_are_normalized_without_weakening_strict_json(tmp_path):
    from core_invariants import StrictJSONError, canonical_json

    path = tmp_path / 'notatka – próba.txt'
    windows = PureWindowsPath(r'C:\work\image.png')
    source = {'path':path,'argv':(windows,),'nested':{'destination':path}}
    encoded = _encode_host_argument(source)
    assert encoded == {'path':str(path),'argv':[str(windows)],'nested':{'destination':str(path)}}
    canonical_json(encoded)
    with pytest.raises(StrictJSONError):
        canonical_json(source)
    assert source['path'] is path


def test_remote_save_accepts_pathlib_before_authenticated_serialization(tmp_path):
    admission = {'schema':'variant1.kernel-execution-admission.v1',
                 'execution_id':'path-save','outer_tool_call_id':'outer-path','generation':2}
    bridge = KernelBridgeClient(host='127.0.0.1',port=1,secret=b'path-test-secret',
                                nonce='path-nonce',generation=2,
                                kernel=SimpleNamespace(current_admission=lambda:dict(admission)))
    signed = []
    def capture(payload):
        _request_id, envelope = bridge._signed_request(payload)
        signed.append(envelope)
        return {'ok':True,'result':{'saved':True}}
    bridge._roundtrip = capture
    image = _decode_host_result(_envelope(service='artifacts',kind='blob',methods={
        'schema':'variant1.remote-handle-methods.v1','items':[{
            'name':'save','description':'Save the image.',
            'params':[{'name':'path','type':'string','required':True}],
            'returns':'object',
        }],
    }), bridge)
    path = tmp_path/'próba.png'
    bridge.bind_execution_origin()
    try:
        assert image.save(path) == {'saved':True}
    finally:
        bridge.reset_execution_origin()
    assert signed[0]['args']['arguments']['path'] == str(path)
    assert signed[0]['args']['method'] == 'save'


def test_browser_observation_display_is_bounded_but_mapping_stays_complete():
    bridge = RecordingBridge()
    elements = [
        {
            "backend_ref": f"b1-{index}",
            "role": "button",
            "name": "Control " + ("x" * 200),
        }
        for index in range(100)
    ]
    result = _decode_host_result({
        "schema": "variant1.browser-observation-result.v1",
        "snapshot": {
            "title": "Example",
            "url": "https://example.test",
            "elements": elements,
            "text_excerpt": "page " * 10_000,
        },
        "text": "page text",
        "html": "<button>Send</button>",
        "elements": [],
        "element_refs": elements,
    }, bridge)

    assert isinstance(result, Variant1BrowserObservation)
    assert len(result["snapshot"]["elements"]) == 100
    assert result.snapshot is result["snapshot"]
    assert result.elements is result["elements"]
    assert result.title == "Example"
    assert result.url == "https://example.test"
    assert result.url == result["url"] == result.get("url")
    assert result.title == result["title"] == result.get("title")
    assert "url" in result
    assert result.text == "page text"
    assert result.html == "<button>Send</button>"
    assert len(repr(result)) < 16_000
    assert "84 more element" in repr(result)
    assert "methods=[" not in repr(result)


def test_browser_local_filter_preserves_exact_handle_and_dispatch_without_observation():
    bridge = RecordingBridge()
    envelopes = [_envelope(kind='element', id=f'page:b-{i}', metadata={
        'name': ('Straße archive' if i == 55 else f'Navigation {i}'), 'role': 'link',
    }, methods={'schema':'variant1.remote-handle-methods.v1', 'items':[{'name':'click','params':[]}]})
        for i in range(80)]
    view = _decode_host_result({
        'schema':'variant1.browser-observation-result.v1', 'elements':envelopes,
        'snapshot':{'elements':[{'name':f'raw-{i}'} for i in range(80)]},
    }, bridge)
    original = view.elements[55]
    matches = view.find_elements(name='STRASSE', role='LINK')
    assert matches.total == 1 and matches[0] is original
    assert 'elements[55]' in repr(matches) and 'Straße archive' in repr(matches)
    assert 'methods=[click]' in repr(matches)
    assert '.find_elements(' in repr(view)
    assert bridge.calls == []
    assert matches[0].click() == {'ok':True}
    assert len(bridge.calls) == 1
    assert bridge.calls[0][1]['handle'] == original.identity
    assert len(view.elements) == 80 and view.elements[55] is original


def test_browser_local_filter_is_paged_bounded_and_does_not_trust_raw_refs():
    bridge = RecordingBridge()
    handles = [_decode_host_result(_envelope(kind='element', id=f'page:b-{i}',
                metadata={'role':'button','name':'Apply\n'+'x'*800}), bridge) for i in range(130)]
    view = Variant1BrowserObservation({'elements':handles + [{'role':'button','name':'Apply'}]})
    first = view.find_elements(name='apply', limit=2)
    second = view.find_elements(role='button', offset=2, limit=2)
    assert first.total == second.total == 130
    assert first[0] is handles[0] and second[0] is handles[2]
    assert 'offset=2' in repr(first) and len(repr(first)) < 750
    assert len(repr(first).splitlines()) == 5
    assert len(handles[0].name) == 806
    assert view.find_elements(name='missing') == []
    assert view.find_elements(role='button', offset=200) == []
    assert bridge.calls == []
    for kwargs in ({}, {'name':'Apply','limit':0}, {'role':'button','limit':101},
                   {'name':'Apply','offset':-1}, {'name':'Apply','limit':True}):
        with pytest.raises(ValueError):
            view.find_elements(**kwargs)
    for kwargs in ({'name':4}, {'role':4}):
        with pytest.raises(TypeError):
            view.find_elements(**kwargs)


def test_browser_metadata_is_readable_without_masking_page_key_method():
    bridge = RecordingBridge()
    from browser_fabric.handles import _PAGE_METHODS
    handle = _decode_host_result(_envelope(
        metadata={"name": "Download", "role": "link", "keys": "metadata collision"},
        methods={"schema": "variant1.remote-handle-methods.v1", "items": _PAGE_METHODS},
    ), bridge)
    assert handle.name == handle["name"] == handle.get("name") == "Download"
    assert handle.role == "link"
    assert handle.get("missing", "fallback") == "fallback"
    assert "name" in dir(handle)
    assert handle.keys("Enter") == {"ok": True}
    assert bridge.calls[-1][1]["method"] == "keys"
    assert bridge.calls[-1][1]["arguments"] == {"keys": "Enter"}


def test_browser_action_has_consistent_flat_fields_and_original_record():
    bridge = RecordingBridge()
    action = _decode_host_result({
        "schema": "variant1.browser-action-view.v1",
        "surface": "VARIANT-1 in-app browser", "browser_kind": "embedded",
        "url": "https://example.test", "page": _envelope(),
        "result": {"value": {"count": 4}, "operation_id": "op-7",
                   "download_state": "not_observed_yet", "download": None},
    }, bridge)
    assert action.value == action["value"] == action.get("value") == {"count": 4}
    assert action.value is action.result["value"]
    assert action.operation_id == "op-7"
    assert action.page.id == "page-7"
    assert "not_observed_yet" in repr(action)
    assert "VARIANT-1 in-app browser" in repr(action)
    action["value"] = "large value" * 10000
    action["downloads"] = [{"url": "long url" * 10000} for _ in range(100)]
    assert len(repr(action)) < 5000
    assert len(action.downloads) == 100
    assert len(action.value) == 110000


def test_desktop_element_page_is_list_first_and_mapping_compatible():
    bridge = RecordingBridge()
    result = _decode_host_result({
        "schema": "variant1.desktop-elements.v1",
        "observation_id": "obs-1",
        "window_id": "win-1",
        "total": 2,
        "elements": [_envelope(kind="element", id="element-1"), "plain"],
    }, bridge)

    assert isinstance(result, Variant1DesktopElements)
    assert len(result) == 2
    assert isinstance(result[0], Variant1RemoteHandle)
    assert result[:1] == [result[0]]
    assert result["elements"] is result
    assert result["window_id"] == "win-1"


def test_desktop_view_result_has_bounded_display_and_complete_controls():
    bridge = RecordingBridge()
    controls = [
        {"id": index, "role": "Button", "name": f"Control {index}"}
        for index in range(100)
    ]
    result = _decode_host_result({
        "schema": "variant1.desktop-view-result.v2",
        "window_id": "win-1",
        "mode": "uia",
        "action": {
            "name": "click",
            "input_sent": None,
            "error": "delivery outcome unknown",
        },
        "controls": controls,
        "control_page": {"returned": 100, "total": 100, "has_more": False},
    }, bridge)

    assert isinstance(result, Variant1DesktopViewResult)
    assert len(result["controls"]) == 100
    assert result.controls is result["controls"]
    assert result.window_id == "win-1"
    assert result.action_status is None
    assert result.input_sent is None
    assert result.action_error == "delivery outcome unknown"
    assert {"action_status", "input_sent", "action_error"} <= set(dir(result))
    assert len(repr(result).splitlines()) == 19
    assert "84 more control(s)" in repr(result)


def test_desktop_view_decoder_wraps_only_the_current_schema():
    bridge = RecordingBridge()

    current = _decode_host_result({
        "schema": "variant1.desktop-view-result.v2",
        "window_id": "win-current",
        "controls": [],
    }, bridge)
    retired_view = _decode_host_result({
        "schema": "variant1.desktop-view-result.v1",
        "window_id": "win-old",
        "controls": [],
    }, bridge)
    retired_focus = _decode_host_result({
        "schema": "variant1.desktop-focus-result.v1",
        "window_id": "win-old",
        "controls": [],
    }, bridge)

    assert isinstance(current, Variant1DesktopViewResult)
    assert type(retired_view) is dict
    assert type(retired_focus) is dict


def test_desktop_control_filter_finds_hidden_fields_without_copying_targets():
    controls = [{'id':i, 'role':'TreeItem', 'name':f'Folder {i}'} for i in range(60)]
    controls.extend([
        {'id':80,'role':'Text','name':'File name:'},
        {'id':81,'role':'Edit','name':'File name:','ref':'exact-ref','bounds':[1,2,3,4]},
        {'id':82,'role':'Edit','name':'Other field'},
        {'id':83,'role':'Edit','name':'Straße'},
    ])
    view = Variant1DesktopViewResult({'controls':controls,'window_id':'win-1'})
    matches = view.find_controls(name='FILE NAME', role='edit')
    assert len(matches) == matches.total == 1
    assert matches[0] is view.controls[61]
    assert matches[0]['ref'] == 'exact-ref'
    assert 'controls[61]' in repr(matches)
    assert 'Folder' not in repr(matches)
    assert 'find_controls' in repr(view)
    assert view.find_controls(name='STRASSE')[0] is controls[63]
    assert view.find_controls(name='missing') == []
    assert len(view.controls) == 64
    assert "controls[61] target=81 Edit 'File name:'" in repr(view)
    assert sum(line.startswith('  controls[') for line in repr(view).splitlines()) == 16


def test_desktop_preview_keeps_diverse_late_fields_and_original_target_identity():
    controls = [{'id': i, 'role': 'TreeItem', 'name': f'Folder {i}'} for i in range(45)]
    controls.extend({'id': i, 'role': 'Edit', 'name': 'Name', 'value': f'row-{i}'} for i in range(45, 65))
    controls.extend([
        {'id': 100, 'role': 'Edit', 'name': 'Hidden field', 'offscreen': True},
        {'id': 101, 'role': 'Edit', 'name': 'Destination', 'value': 'chosen.txt', 'offscreen': False},
        {'id': 102, 'role': 'ComboBox', 'name': 'Format', 'value': 'Text'},
        {'id': 103, 'role': 'Document', 'name': 'Body', 'value': 'ready'},
    ])
    for control in controls:
        control['bounds'] = [-900, -500, -700, -450]
    view = Variant1DesktopViewResult({'controls': controls})
    rendered = repr(view)
    rows = [line for line in rendered.splitlines() if line.startswith('  controls[')]
    assert len(rows) == 16
    assert 'target=100' not in rendered
    assert "controls[66] target=101 Edit 'Destination' value='chosen.txt'" in rendered
    assert "target=102 ComboBox 'Format' value='Text'" in rendered
    assert "target=103 Document 'Body' value='ready'" in rendered
    assert sum(" Edit 'Name'" in line for line in rows) == 1
    assert view.controls is controls
    assert view.find_controls(name='Destination')[0] is controls[66]
    assert repr(view) == rendered


def test_desktop_preview_large_form_retains_navigation_and_bounded_value_evidence():
    controls = [{'id': i, 'role': 'Button', 'name': f'Action {i}'} for i in range(40)]
    controls.extend({'id': i, 'role': 'Edit', 'name': f'Field {i}', 'value': 'x' * 20000}
                    for i in range(40, 100))
    for control in controls:
        control['bounds'] = [1, 2, 101, 22]
    view = Variant1DesktopViewResult({'controls': controls})
    rendered = repr(view)
    rows = [line for line in rendered.splitlines() if line.startswith('  controls[')]
    assert len(rows) == 16
    assert sum(' Button ' in line for line in rows) == 12
    assert sum(' Edit ' in line for line in rows) == 4
    assert len(rendered) < 5000
    assert len(view.controls[-1]['value']) == 20000
    assert len(view.find_controls(role='Edit')) == 20


def test_desktop_preview_unnamed_fields_and_invalid_geometry_stay_bounded():
    controls = [{'id': i, 'role': 'Button', 'name': f'Action {i}'} for i in range(40)]
    controls.extend([
        {'id': 40, 'role': 'Edit', 'name': 'No geometry'},
        {'id': 41, 'role': 'Edit', 'name': 'Zero area', 'bounds': [0, 0, 0, 0]},
        {'id': 42, 'role': 'Edit', 'name': 'Invalid', 'bounds': [0, 0, float('nan'), 2]},
    ])
    controls.extend({'id': i, 'role': 'Edit', 'name': '', 'bounds': [-500, -60, -200, -30]}
                    for i in range(43, 60))
    view = Variant1DesktopViewResult({'controls': controls})
    rendered = repr(view)
    rows = [line for line in rendered.splitlines() if line.startswith('  controls[')]
    assert len(rows) == 16
    assert sum(' Edit ' in line for line in rows) == 4
    assert 'controls[43] target=43' in rendered
    assert 'controls[46] target=46' in rendered
    assert 'controls[47]' not in rendered
    assert 'No geometry' not in rendered and 'Zero area' not in rendered and 'Invalid' not in rendered
    assert view.controls is controls


def test_desktop_display_exposes_empty_value_state_and_folded_text_without_refresh():
    controls = [
        {'id': 1, 'role': 'Edit', 'name': 'Find', 'value': ''},
        {'id': 2, 'role': 'Edit', 'name': 'Replace', 'value': 'ready\nnext'},
        {'id': 3, 'role': 'CheckBox', 'name': 'Enabled', 'state': 'on'},
        {'id': 4, 'role': 'ListItem', 'name': 'report.txt', 'text': 'Type: Text · Size: 66 B'},
        {'id': 5, 'role': 'Edit', 'name': 'Elsewhere', 'offscreen': True},
    ]
    view = Variant1DesktopViewResult({'controls': controls})
    rendered = repr(view)
    assert "Edit 'Find' value=''" in rendered
    assert "value='ready\\nnext'" in rendered
    assert "state='on'" in rendered
    assert "text='Type: Text · Size: 66 B'" in rendered
    assert "Edit 'Elsewhere' [offscreen]" in rendered
    assert "'value': ''" in repr(view.find_controls(name='Find'))
    assert len(rendered.splitlines()) == 6


def test_desktop_action_status_matches_its_display_and_keeps_mapping_precedence():
    view = Variant1DesktopViewResult({'action':{'name':'type_text','status':'no_effect','input_sent':True}})
    assert view.status == view.action_status == 'no_effect'
    assert 'status' in dir(view)
    assert "status='no_effect'" in repr(view)
    view['status'] = 'explicit_top_level_status'
    assert view.status == 'explicit_top_level_status'
    assert view.action_status == 'no_effect'


def test_missing_post_action_observation_does_not_recommend_the_old_window():
    closed = Variant1DesktopViewResult({
        'window_id':'old-window','action':{'name':'click','status':'unknown_effect','input_sent':True},
        'observation_status':'unavailable','text_included':False,'controls':[],
        'observation_id':None,'fresh_observation_required':True,
    })
    text=repr(closed)
    assert 'Reacquire a live window' in text and 'computer.list_windows()' in text
    assert 'computer.observe(window=view.window' not in text
    assert closed.status == 'unknown_effect' and closed.input_sent is True
    live = Variant1DesktopViewResult({'window_id':'live','text_included':False,'controls':[]})
    assert 'computer.observe(window=view.window, include_text=True)' in repr(live)


def test_desktop_control_filter_pages_matches_with_bounded_display():
    controls = [{'id':i,'role':'Edit','name':'value'+'x'*500} for i in range(150)]
    view = Variant1DesktopViewResult({'controls':controls})
    first = view.find_controls(role='Edit', limit=2)
    second = view.find_controls(role='Edit', offset=2, limit=2)
    assert first.total == second.total == 150
    assert first[0] is controls[0]
    assert second[0] is controls[2]
    assert 'list[dict]' in repr(second)
    assert '[0]' in repr(second) and 'view.controls[2]' in repr(second)
    assert "'id': 2" in repr(second)
    assert 'matches=' not in repr(second) and 'target=' not in repr(second)
    assert not hasattr(second, 'matches')
    assert 'offset=2' in repr(first)
    assert len(repr(first)) < 650
    assert len(controls[0]['name']) == 505
    assert view.find_controls(role='Edit', offset=150) == []
    for kwargs in ({}, {'role':'Edit','limit':0}, {'role':'Edit','limit':101}, {'role':'Edit','offset':-1}):
        with pytest.raises(ValueError):
            view.find_controls(**kwargs)
    with pytest.raises(TypeError):
        view.find_controls(name=4)


def test_bytes_path_arguments_remain_an_explicit_error():
    class BytesPath:
        def __fspath__(self):
            return b'not-a-text-path'
    with pytest.raises(TypeError, match='text path'):
        _encode_host_argument({'path':BytesPath()})


def test_method_calls_use_the_embedded_versioned_dispatch_descriptor():
    bridge = RecordingBridge()
    handle = _decode_host_result(_envelope(), bridge)

    assert handle.navigate(url="https://example.com") == {"ok": True}
    descriptor, arguments = bridge.calls[0]
    assert descriptor == {
        "schema": REMOTE_HANDLE_DISPATCH_SCHEMA,
        "ref_id": "cap-remote-dispatch",
        "handler_revision": "remote-dispatch.v1",
        "catalog_release_id": "catalog-9",
        "category_id": "browser",
        "slot_id": "slot-browser",
        "slot_version": 4,
        "mount_revision": 8,
    }
    assert arguments == {
        "handle": handle.identity,
        "method": "navigate",
        "arguments": {"url": "https://example.com"},
    }
    assert "secret" not in repr(handle)
    assert "secret" not in {item.name for item in fields(handle)}


@pytest.mark.asyncio
async def test_remote_handle_methods_expose_the_same_explicit_awaitable_path():
    bridge = RecordingBridge()
    handle = _decode_host_result(_envelope(), bridge)

    result = await handle.navigate.async_(
        url="https://example.com/async",
        _deadline_ms=2500,
    )

    assert result == {"ok": True, "async": True}
    descriptor, arguments, deadline_ms = bridge.calls[0]
    assert descriptor["ref_id"] == "cap-remote-dispatch"
    assert arguments == {
        "handle": handle.identity,
        "method": "navigate",
        "arguments": {"url": "https://example.com/async"},
    }
    assert deadline_ms == 2500


def test_malformed_or_sensitive_envelopes_remain_ordinary_json():
    bridge = RecordingBridge()
    incomplete = {"$variant1_handle": {"service": "browser"}}
    sensitive = _envelope(metadata={"access_token": "do-not-store"})
    wrong_schema = _envelope()
    wrong_schema["$variant1_handle"]["_dispatch"]["schema"] = "unknown.v9"

    assert _decode_host_result(incomplete, bridge) == incomplete
    assert _decode_host_result(sensitive, bridge) == sensitive
    assert _decode_host_result(wrong_schema, bridge) == wrong_schema


def test_bridge_invoke_and_batch_decode_host_results_without_affecting_json():
    admission = {
        "schema": "variant1.kernel-execution-admission.v1",
        "execution_id": "execution-1",
        "outer_tool_call_id": "outer-1",
        "generation": 2,
    }
    kernel = SimpleNamespace(current_admission=lambda: dict(admission))
    bridge = KernelBridgeClient(
        host="127.0.0.1",
        port=1,
        secret=b"bridge-secret",
        nonce="nonce-1",
        generation=2,
        kernel=kernel,
    )
    bridge._roundtrip = lambda payload: (
        {"ok": True, "results": [_envelope(), {"plain": True}]}
        if payload["op"] == "invoke_many"
        else {"ok": True, "result": {"value": _envelope(), "plain": 5}}
    )

    bridge.bind_execution_origin()
    try:
        single = bridge.invoke({"ref_id": "source"}, {})
        batch = bridge.invoke_many([({"ref_id": "source"}, {})])
    finally:
        bridge.reset_execution_origin()

    assert isinstance(single["value"], Variant1RemoteHandle)
    assert single["plain"] == 5
    assert isinstance(batch[0], Variant1RemoteHandle)
    assert batch[1] == {"plain": True}


def test_handle_detaches_when_its_live_bridge_is_gone():
    bridge = RecordingBridge()
    handle = _decode_host_result(_envelope(), bridge)
    del bridge
    gc.collect()

    with pytest.raises(Variant1CapabilityError) as error:
        handle.refresh()
    assert error.value.code == "remote_handle_detached"


def test_declared_handle_methods_are_discoverable_bound_and_rich():
    bridge = RecordingBridge()
    handle = _decode_host_result(_envelope(methods={
        "schema": "variant1.remote-handle-methods.v1",
        "items": [{
            "name": "navigate",
            "description": "Navigate this page to a URL.",
            "params": [
                {"name": "url", "type": "str", "required": True},
                {
                    "name": "timeout_s", "type": "float",
                    "required": False, "default": 30.0,
                },
            ],
            "returns": "page",
        }],
    }), bridge)

    assert "navigate" in dir(handle)
    assert handle.methods[0]["description"] == "Navigate this page to a URL."
    assert handle.methods() == ["navigate"]
    described = handle.describe("navigate")
    assert described["description"] == "Navigate this page to a URL."
    assert described["signature"] == (
        "navigate(url: 'str', timeout_s: 'float' = 30.0) -> 'page'"
    )
    overview = handle.describe()
    assert overview['kind'] == 'browser.page'
    assert overview['method_signatures'] == {'navigate': described['signature']}
    assert overview['metadata_type'] == 'dict property'
    assert overview['metadata_fields'] == ['labels', 'title']
    assert bridge.calls == []
    overview['metadata_fields'].clear()
    assert handle.describe()['metadata_fields'] == ['labels', 'title']
    with pytest.raises(AttributeError, match='available: navigate'):
        handle.describe('missing')
    assert str(inspect.signature(handle.navigate)) == (
        "(url: 'str', timeout_s: 'float' = 30.0) -> 'page'"
    )
    assert inspect.getdoc(handle.navigate) == "Navigate this page to a URL."
    assert handle.navigate.describe() == described
    assert handle.navigate.documentation() == described
    assert "url: 'str'" in repr(handle.navigate)
    assert ".describe()" in repr(handle.navigate)
    with pytest.raises(AttributeError, match="available: navigate"):
        _ = handle.navigte
    with pytest.raises(
        TypeError, match="missing a required argument: 'url'"
    ):
        handle.navigate()

    assert handle.navigate(url="https://example.test") == {"ok": True}
    bundle = handle._repr_mimebundle_()
    assert bundle["application/json"]["methods"] == ["navigate"]
    assert bundle["application/json"]["metadata"]["title"] == "Example"


def test_connector_handle_repr_directs_schema_first_without_hiding_methods():
    bridge = RecordingBridge()
    handle = _decode_host_result(_envelope(
        service="connectors",
        kind="mcp",
        metadata={
            "name": "benchmark_capability",
            "server_id": "benchmark-server",
        },
        methods={
            "schema": "variant1.remote-handle-methods.v1",
            "items": [
                {
                    "name": "schema",
                    "description": "Inspect the exact schema.",
                    "params": [],
                    "returns": "dict",
                },
                {
                    "name": "invoke",
                    "description": "Invoke with exact arguments.",
                    "params": [{
                        "name": "arguments", "type": "object", "required": False,
                    }],
                    "returns": "any",
                },
            ],
        },
    ), bridge)

    text = repr(handle)
    assert "methods=[schema(), invoke()]" in text
    assert "name='benchmark_capability'" in text
    assert "server='benchmark-server'" in text
    assert "schema() before invoke()" in text
    assert "page-7" not in text
    assert "generation=" not in text
    assert "revision=" not in text


def test_schema_ready_connector_repr_exposes_explicit_terminal_option():
    handle = _decode_host_result(_envelope(
        service="connectors",
        kind="mcp",
        metadata={
            "name": "benchmark_capability",
            "server_id": "benchmark-server",
            "schema_included": True,
        },
        methods={
            "schema": "variant1.remote-handle-methods.v1",
            "items": [{
                "name": "invoke",
                "params": [
                    {"name": "arguments", "type": "object", "required": False},
                    {
                        "name": "conclude", "type": "bool",
                        "required": False, "default": False,
                    },
                ],
            }],
        },
    ), RecordingBridge())

    text = repr(handle)
    assert "schema() before invoke()" not in text
    assert "invoke(arguments=..., conclude=False)" in text


def test_mcp_and_connector_views_keep_mapping_values_but_remove_display_duplication():
    bridge = RecordingBridge()
    payload = {
        "session_id": "session-42",
        "ticket_id": "TK-1",
        "fragments": ["amber", "cedar"],
        "values": [2, 3],
    }
    encoded = (
        '{"session_id":"session-42","ticket_id":"TK-1",'
        '"fragments":["amber","cedar"],"values":[2,3]}'
    )
    result = _decode_host_result({
        "schema": "variant1.mcp-result.v2",
        "content": [{"type": "text", "text": encoded}],
        "structured_content": {"result": encoded},
        "is_error": False,
        "metadata": {},
    }, bridge)

    assert isinstance(result, Variant1McpResult)
    assert result.ok is True
    assert result.data == payload
    assert result["data"] == payload
    assert result.get("data") == payload
    assert result["session_id"] == "session-42"
    assert result.get("session_id") == "session-42"
    assert "session_id" in result
    assert result.get("missing", "fallback") == "fallback"
    assert result["raw"]["structured_content"] == {"result": encoded}
    assert result.raw()["structured_content"] == {"result": encoded}
    assert "data" in result and "raw" in result
    assert result["structured_content"] == {"result": encoded}
    assert repr(result).count("ticket_id") == 1
    assert "structured_content" not in repr(result)
    assert "metadata" not in repr(result)

    schema = _decode_host_result({
        "descriptor": {
            "name": "benchmark_capability",
            "description": "Use the benchmark capability.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "ticket_id": {"type": "string", "default": ""},
                },
                "required": ["action"],
            },
            "outputSchema": {
                "type": "object",
                "properties": {"result": {"type": "string"}},
            },
        },
        "lease": {
            "name": "benchmark_capability",
            "server_id": "benchmark-server",
            "schema_digest": "digest-do-not-project",
        },
        "name": "benchmark_capability",
        "schema_digest": "digest-do-not-project",
    }, bridge)

    assert isinstance(schema, Variant1McpSchema)
    assert schema["schema_digest"] == "digest-do-not-project"
    schema_text = repr(schema)
    assert "MCPToolSchema(name='benchmark_capability')" in schema_text
    assert "action: string (required)" in schema_text
    assert "ticket_id: string (default='')" in schema_text
    assert "digest-do-not-project" not in schema_text
    assert "lease" not in schema_text

    search = _decode_host_result({
        "mcp": [_envelope(
            service="connectors",
            kind="mcp",
            metadata={
                "name": "benchmark_capability",
                "server_id": "benchmark-server",
            },
        )],
        "plugins": [],
        "top_match": {
            "handle": _envelope(
                service="connectors",
                kind="mcp",
                metadata={
                    "name": "benchmark_capability",
                    "server_id": "benchmark-server",
                },
            ),
            "schema": {
                "descriptor": {
                    "name": "benchmark_capability",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"action": {"type": "string"}},
                        "required": ["action"],
                    },
                },
                "lease": {"name": "benchmark_capability"},
            },
        },
        "guidance": "Long internal guidance remains programmatically available.",
    }, bridge)

    assert isinstance(search, Variant1ConnectorSearchResult)
    assert search["guidance"].startswith("Long internal")
    assert isinstance(search.mcp[0], Variant1RemoteHandle)
    search_text = repr(search)
    assert "[0]" in search_text
    assert "benchmark_capability" in search_text
    assert "top_match [0] (schema included):" in search_text
    assert "action: string (required)" in search_text
    assert "ready: .invoke(arguments={...}, conclude=False)" in search_text
    assert "handle=<Variant1Handle" not in search_text
    assert repr(search.top_match).count("action: string (required)") == 1
    assert "Long internal guidance" not in search_text
    assert "page-7" not in search_text


def test_command_result_keeps_full_mapping_but_projects_output_once():
    bridge = RecordingBridge()
    result = _decode_host_result({
        "schema": "variant1.command-result.v1",
        "ok": True,
        "text": "$ tool\n--- stdout ---\nready",
        "stdout": "ready",
        "stderr": "",
        "process": _envelope(service="execution", kind="process"),
        "exit_code": 0,
        "duration_s": 0.125,
        "output_cursor": 9,
        "artifact_refs": [],
        "truncated": False,
    }, bridge)

    assert isinstance(result, Variant1CommandResult)
    assert result["text"].startswith("$ tool")
    assert result["stdout"] == "ready"
    assert result.stdout == "ready"
    assert result.stderr == ""
    assert result.exit_code == 0
    assert result.duration_s == 0.125
    assert result.output_cursor == 9
    assert result.raw["output_cursor"] == 9
    assert result.raw()["output_cursor"] == 9
    assert callable(result.raw)
    assert {"stdout", "stderr", "exit_code", "output_cursor"} <= set(dir(result))
    with pytest.raises(AttributeError):
        _ = result.missing_field
    assert isinstance(result["process"], Variant1RemoteHandle)
    displayed = repr(result)
    assert displayed.count("ready") == 1
    assert "CommandResult(ok=True, exit_code=0" in displayed
    assert "schema" not in displayed
    assert "output_cursor" not in displayed


def test_connector_primary_match_delegates_method_discovery():
    bridge = RecordingBridge()
    search = _decode_host_result({
        "mcp": [],
        "plugins": [],
        "top_match": {
            "handle": _envelope(
                service="connectors",
                kind="mcp",
                methods={
                    "schema": "variant1.remote-handle-methods.v1",
                    "items": [
                        {"name": "schema", "params": []},
                        {"name": "invoke", "params": []},
                    ],
                },
            ),
            "schema": {"name": "benchmark_capability"},
        },
    }, bridge)

    assert search.top_match.methods() == ["schema", "invoke"]
