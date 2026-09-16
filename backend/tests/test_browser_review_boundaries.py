import asyncio
import base64
from types import SimpleNamespace

import pytest

from browser_fabric import ManagedPlaywrightAdapter, EmbeddedBrowserAdapter, BrowserStaleReference


@pytest.mark.asyncio
async def test_context_page_event_cannot_duplicate_an_explicit_new_page(tmp_path):
    adapter = ManagedPlaywrightAdapter(str(tmp_path))
    class Page:
        url = "about:blank"
        def is_closed(self): return False
        async def title(self): return "new"
        async def evaluate(self, expression, arg=None): return arg
        def on(self, name, callback): pass
    page = Page()
    class Context:
        pages = []
        async def new_page(self):
            self.pages.append(page)
            adapter._page_created(page)
            await asyncio.sleep(0)
            return page
    adapter._context = Context()
    await adapter.new_page("requested-id")
    await asyncio.gather(*adapter._page_tasks)
    await adapter._discover_pages()
    targets = await adapter.targets()
    assert [target.backend_target_id for target in targets] == ["requested-id"]
    assert len(adapter._pages) == 1


@pytest.mark.asyncio
async def test_embedded_inventory_removes_closed_tabs_including_last_one():
    rows = [{"id": "a", "active": True}, {"id": "b"}]
    async def request(command):
        assert command["action"] == "tabs"
        return {"tabs": rows}
    adapter = EmbeddedBrowserAdapter(request)
    assert len(await adapter.targets()) == 2
    rows = [{"id": "b", "active": True}]
    assert len(await adapter.targets()) == 1
    assert adapter._target_ids == {"b"} and "a" not in adapter._states
    rows = []
    assert await adapter.targets() == ()
    assert adapter._target_ids == set() and adapter._active_id == "" and not adapter._states


@pytest.mark.asyncio
@pytest.mark.parametrize("navigate", [False, True])
async def test_managed_screenshot_is_bound_to_text_document(tmp_path, navigate):
    adapter = ManagedPlaywrightAdapter(str(tmp_path))
    class Page:
        url = "https://same.test"
        generation = 1
        def is_closed(self): return False
        async def evaluate(self, script, args=None):
            if args is None:
                return {"document_id": str(self.generation), "url": self.url}
            return {"title": "one", "url": self.url, "text": "first document", "html": "<p>first document</p>"}
        async def screenshot(self, **kwargs):
            if navigate: self.generation += 1
            return b"pixels"
    adapter._pages["one"] = Page()
    call = adapter.observe("one", max_chars=100, max_elements=10, include_html=True, include_screenshot=True)
    if navigate:
        with pytest.raises(BrowserStaleReference): await call
    else:
        assert (await call).html == "<p>first document</p>"


@pytest.mark.asyncio
@pytest.mark.parametrize("navigate", [False, True])
async def test_embedded_screenshot_does_not_mix_documents_at_the_same_url(navigate):
    generation = 1
    async def request(command):
        nonlocal generation
        action = command["action"]
        if action == "evaluate":
            return {"value": {"document_id": str(generation), "url": "https://same.test"}}
        if action == "read":
            return {"text": "first document", "url": "https://same.test", "title": "first", "elements": []}
        if action == "html": return {"html": "<p>first document</p>"}
        if action == "screenshot":
            if navigate: generation += 1
            return {"image": base64.b64encode(b"pixels").decode()}
        raise AssertionError(action)
    adapter = EmbeddedBrowserAdapter(request)
    adapter._target_ids.add("one")
    call = adapter.observe("one", max_chars=100, max_elements=10, include_html=True, include_screenshot=True)
    if navigate:
        with pytest.raises(BrowserStaleReference): await call
    else:
        result = await call
        assert result.text == "first document" and result.title == "first"
