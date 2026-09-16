"""Opt-in local-only headless browser check; no user browser/profile is opened."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import threading

import pytest


pytestmark = pytest.mark.skipif(os.environ.get('VARIANT1_BROWSER_NATIVE_TEST') != '1', reason='opt-in native browser fixture')


@pytest.mark.asyncio
async def test_headless_recording_dialog_and_cdp_disconnect(tmp_path, monkeypatch):
    from playwright.async_api import async_playwright
    from browser_fabric.adapters import ManagedPlaywrightAdapter
    chrome = Path(os.environ['VARIANT1_BROWSER_TEST_CHROME'])
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'<html><title>Local fixture</title><button onclick="alert(\'hello\');document.title=\'Accepted\'">Dialog</button></html>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    driver = await async_playwright().start()
    owner = ManagedPlaywrightAdapter(str(tmp_path / 'owned'), headless=True, playwright_factory=lambda: driver,
        launch_options={'executable_path': str(chrome), 'args': ['--remote-debugging-port=0']},
        browser_settings={'record_sessions': True, '_recording_id': 'native-test', 'dialog_policy': 'auto_accept'})
    attached = None
    try:
        pages = await owner.launch(())
        target = pages[0].backend_target_id
        await owner.perform(target, 'navigate', {'url': f'http://127.0.0.1:{server.server_port}/'})
        observed = await owner.observe(target, max_chars=1000, max_elements=10, include_html=False, include_screenshot=False)
        button = next(element for element in observed.elements if element.get('tag') == 'button' or element.get('role') == 'button')
        await owner.perform(target, 'click', {'backend_ref': button['backend_ref']})
        assert (await owner._pages[target].title()) == 'Accepted'
        port = int((tmp_path / 'owned' / 'DevToolsActivePort').read_text().splitlines()[0])
        remote_driver = await async_playwright().start()
        attached = ManagedPlaywrightAdapter(str(tmp_path / 'attached'), playwright_factory=lambda: remote_driver,
            browser_settings={'cdp_url': f'http://127.0.0.1:{port}'})
        assert await attached.launch(())
        await attached.close()
        attached = None
        assert await owner._pages[target].title() == 'Accepted'
    finally:
        if attached:
            await attached.close()
        await owner.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    videos = list((tmp_path / 'owned' / 'recordings' / 'native-test').glob('*.webm'))
    assert videos and all(path.stat().st_size > 100 for path in videos)
