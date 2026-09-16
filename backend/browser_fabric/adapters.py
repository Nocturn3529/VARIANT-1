"""Browser Fabric adapters for managed Playwright and the embedded Main Deck.

Adapters own live browser objects.  They accept durable backend target IDs and
return JSON-shaped observations/results so the fabric can persist authority
before exposing reconstructable handles to callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
import base64
from dataclasses import dataclass, field
import inspect
import os
import re
import uuid
from typing import Any, Awaitable, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from .models import (
    BrowserStaleReference,
    BrowserUnavailable,
    BrowserUnsupported,
    BrowserValidationError,
    TargetRecord,
    json_value,
)
from .provisioning import BrowserProvisionError, ensure_chromium_runtime
from .viewport import image_dimensions, viewport_request
from .settings import DEFAULTS, connection_error_detail, is_private_url


def navigation_url(value: Any) -> str:
    """Match the embedded browser's navigation contract, including local sites."""
    url = str(value or "about:blank").strip()
    try:
        parsed = urlsplit(url)
        if url == "about:blank" or (
            parsed.scheme.lower() in {"http", "https"} and parsed.hostname
            and not parsed.username and not parsed.password
            and not any(ord(char) < 32 for char in url)
        ):
            return url
    except ValueError:
        pass
    raise BrowserValidationError("browser navigation requires http(s) or about:blank")


MANAGED_CAPABILITIES = frozenset({
    "navigate", "back", "forward", "reload", "observe", "text", "html", "title",
    "screenshot", "click", "fill", "select", "hover", "keys", "wait", "evaluate",
    "tabs", "downloads", "trace", "set_viewport",
})
EMBEDDED_CAPABILITIES = frozenset({
    "navigate", "back", "forward", "reload", "observe", "text", "html", "title",
    "screenshot", "click", "fill", "select", "hover", "keys", "wait",
    "evaluate", "tabs", "downloads", "set_viewport",
})

_MANAGED_ELEMENT_RE = re.compile(r"^mf_[0-9a-f]{12}_\d+$")
_EMBEDDED_ELEMENT_RE = re.compile(r"^b\d+-\d+$")
_MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
_PAGE_MARKER_PREFIX = "__variant1_fabric_target__:"
_DOCUMENT_IDENTITY = """(() => {
  const key = Symbol.for('variant1.browser.document');
  if (!document[key]) Object.defineProperty(document, key, {
    value: (globalThis.crypto?.randomUUID?.() || String(performance.timeOrigin) + ':' + Math.random())
  });
  return {document_id: document[key], url: String(location.href), viewport: {width: innerWidth, height: innerHeight}};
})()"""


def _same_document(before: Any, after: Any) -> None:
    if (not isinstance(before, Mapping) or not isinstance(after, Mapping)
            or not before.get("document_id") or before != after):
        raise BrowserStaleReference("page navigated during observation; collect fresh evidence")


def normalize_embedded_target(raw: Any) -> str:
    """Normalize one reference emitted by the visible Electron browser host."""

    value = str(raw or "").strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1].strip()
    if not _EMBEDDED_ELEMENT_RE.fullmatch(value):
        raise BrowserValidationError(
            "target must come from the latest browser observation, for example [b2-4]"
        )
    return value


@dataclass(frozen=True, slots=True)
class AdapterTarget:
    backend_target_id: str
    title: str = ""
    url: str = ""
    active: bool = False
    viewport: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdapterObservation:
    title: str
    url: str
    text: str
    html: str = ""
    elements: tuple[Mapping[str, Any], ...] = ()
    screenshot: bytes = b""
    viewport: Mapping[str, Any] = field(default_factory=dict)
    document: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdapterDownload:
    suggested_filename: str
    url: str
    payload: bytes = b""
    path: str = ""
    sha256: str = ""
    bytes_count: int = 0
    download_id: str = ""


@dataclass(frozen=True, slots=True)
class AdapterResult:
    value: Any = None
    title: str = ""
    url: str = ""
    navigated: bool = False
    screenshot: bytes = b""
    download: AdapterDownload | None = None
    targets: tuple[AdapterTarget, ...] = ()


class BrowserAdapter(ABC):
    kind = "abstract"
    capabilities: frozenset[str] = frozenset()

    def require(self, operation: str) -> None:
        if operation not in self.capabilities:
            raise BrowserUnsupported(operation, self.kind)

    def set_download_sink(self, sink: Callable[..., Awaitable[Any]]) -> None:
        """Attach the host-owned durable download recorder when supported."""

    async def collect_downloads(self, backend_target_id: str) -> None:
        """Flush completed native downloads; event-driven adapters already do so."""

    def download_progress(self, backend_target_id: str) -> list[dict[str, Any]]:
        """Return the most recent native progress without starting another action."""
        return []

    async def document_status(self, backend_target_id: str) -> dict[str, Any]:
        """Optional authoritative document readiness; absence is unknown."""
        return {}

    @abstractmethod
    async def launch(self, targets: Sequence[TargetRecord]) -> tuple[AdapterTarget, ...]: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def targets(self) -> tuple[AdapterTarget, ...]: ...

    async def new_page(self, backend_target_id: str, url: str = "") -> AdapterTarget:
        raise BrowserUnsupported("tabs", self.kind)

    async def close_page(self, backend_target_id: str) -> None:
        raise BrowserUnsupported("tabs", self.kind)

    async def activate_page(self, backend_target_id: str) -> AdapterTarget:
        raise BrowserUnsupported("tabs", self.kind)

    @abstractmethod
    async def observe(
        self,
        backend_target_id: str,
        *,
        max_chars: int,
        max_elements: int,
        include_html: bool,
        include_screenshot: bool,
    ) -> AdapterObservation: ...

    @abstractmethod
    async def perform(
        self, backend_target_id: str, action: str, params: Mapping[str, Any]
    ) -> AdapterResult: ...

    async def start_trace(self, options: Mapping[str, Any]) -> None:
        raise BrowserUnsupported("trace", self.kind)

    async def stop_trace(self, path: str) -> None:
        raise BrowserUnsupported("trace", self.kind)


_SNAPSHOT_FUNCTION = r"""(options) => {
  const marker = 'data-variant1-fabric-ref';
  const selectors = [
    'a[href]', 'button', 'input', 'textarea', 'select', 'summary',
    '[contenteditable="true"]', '[role="button"]', '[role="link"]',
    '[role="checkbox"]', '[role="radio"]', '[role="tab"]', '[role="menuitem"]',
    '[role="textbox"]', '[role="combobox"]', '[role="option"]', '[tabindex]'
  ].join(',');
  const compact = value => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = el => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none'
      && Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
  };
  const roleOf = el => {
    const explicit = compact(el.getAttribute('role'));
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button' || tag === 'summary') return 'button';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'select') return 'combobox';
    if (tag === 'input') {
      const type = compact(el.getAttribute('type')).toLowerCase();
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (['button', 'submit', 'reset'].includes(type)) return 'button';
      return 'textbox';
    }
    return tag || 'control';
  };
  const nameOf = el => compact(
    el.getAttribute('aria-label') || el.getAttribute('alt') || el.getAttribute('title')
    || el.getAttribute('placeholder')
    || (el.labels && el.labels.length ? Array.from(el.labels).map(x => x.innerText).join(' ') : '')
    || el.innerText || (el.type === 'password' ? '' : el.value)
  ).slice(0, 500);
  const controls = [];
  const seen = new Set();
  for (const el of document.querySelectorAll(selectors)) {
    if (controls.length >= options.maxElements || seen.has(el) || !visible(el)) continue;
    seen.add(el);
    const existingRef = compact(el.getAttribute(marker));
    const ref = /^mf_[0-9a-f]{12}_\d+$/.test(existingRef)
      ? existingRef
      : options.token + '_' + (controls.length + 1);
    if (ref !== existingRef) el.setAttribute(marker, ref);
    const rect = el.getBoundingClientRect();
    const role = roleOf(el);
    const editable = el.matches('input,textarea,select,[contenteditable="true"],[role="textbox"]');
    const actions = ['click', 'hover'];
    if (editable) actions.push('fill', 'keys');
    if (el.tagName.toLowerCase() === 'select') actions.push('select');
    controls.push({
      backend_ref: ref,
      role,
      name: nameOf(el),
      input_type: el.tagName.toLowerCase() === 'input' ? String(el.type || '') : '',
      text: compact(el.innerText).slice(0, 2000),
      value: el.type === 'password' ? '' : ('value' in el ? String(el.value || '') : '').slice(0, 2000),
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
      checked: ('checked' in el ? !!el.checked : null),
      selected: ('selected' in el ? !!el.selected : null),
      visible: true,
      editable,
      bbox: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
      actions
    });
  }
  return {
    title: String(document.title || ''),
    url: String(location.href || ''),
    text: (document.body ? String(document.body.innerText || '') : '').slice(0, options.maxChars),
    html: options.includeHtml ? String(document.documentElement?.outerHTML || '') : '',
    elements: controls,
    viewport: {width: innerWidth, height: innerHeight, device_scale_factor: devicePixelRatio}
  };
}"""


class ManagedPlaywrightAdapter(BrowserAdapter):
    """One persistent Chromium context with durable target-ID rebinding."""

    kind = "managed"
    capabilities = MANAGED_CAPABILITIES

    def __init__(
        self,
        profile_dir: str,
        *,
        headless: bool = True,
        playwright_factory: Callable[[], Any] | None = None,
        launch_options: Mapping[str, Any] | None = None,
        personal_source: Mapping[str, Any] | None = None,
        browser_settings: Mapping[str, Any] | None = None,
        cloud_lease: Any = None,
    ) -> None:
        self.profile_dir = os.path.abspath(profile_dir)
        self.headless = bool(headless)
        self.playwright_factory = playwright_factory
        self.launch_options = dict(launch_options or {})
        self.personal_source = dict(personal_source or {})
        self.browser_settings = {**DEFAULTS, **dict(browser_settings or {})}
        self.cloud_lease = cloud_lease
        self._remote_browser = None
        self._owns_context = True
        self._private_route = None
        if not self.browser_settings['evaluate_enabled']:
            self.capabilities = self.capabilities - {'evaluate'}
        self._viewport_modes: dict[str, str] = {}
        self._playwright: Any = None
        self._context: Any = None
        self._pages: dict[str, Any] = {}
        self._page_registration_lock = asyncio.Lock()
        self._page_tasks: set[asyncio.Task] = set()
        self._active_id = ""
        self._preferred_active_id = ""
        self._trace_active = False
        self._download_sink: Callable[..., Awaitable[Any]] | None = None
        self._download_pages: set[int] = set()
        self._download_tasks: set[asyncio.Task[Any]] = set()
        self._active_operations: dict[str, str] = {}
        self._explicit_download_operations: set[str] = set()

    def set_download_sink(self, sink: Callable[..., Awaitable[Any]]) -> None:
        self._download_sink = sink

    def set_current_target_id(self, target_id: str) -> None:
        self._preferred_active_id = str(target_id or "")

    @staticmethod
    async def _page_marker(page: Any) -> str:
        try:
            value = str(await page.evaluate("() => String(window.name || '')") or "")
        except Exception:
            return ""
        return (
            value[len(_PAGE_MARKER_PREFIX):]
            if value.startswith(_PAGE_MARKER_PREFIX)
            else ""
        )

    @staticmethod
    async def _stamp_page(page: Any, backend_target_id: str) -> None:
        try:
            await page.evaluate(
                "value => { window.name = value; return window.name; }",
                _PAGE_MARKER_PREFIX + str(backend_target_id),
            )
        except Exception:
            pass

    async def _register_external_page(self, page: Any) -> None:
        async with self._page_registration_lock:
            if self._context is None or any(existing is page for existing in self._pages.values()):
                return
            backend_id = self._new_backend_id()
            self._pages[backend_id] = page
            await self._stamp_page(page, backend_id)
            self._attach_page_events(backend_id, page)

    def _page_created(self, page: Any) -> None:
        task = asyncio.create_task(self._register_external_page(page))
        self._page_tasks.add(task)
        task.add_done_callback(self._page_tasks.discard)

    def _attach_page_events(self, backend_id: str, page: Any) -> None:
        marker = id(page)
        if marker in self._download_pages:
            return
        self._download_pages.add(marker)
        on = getattr(page, "on", None)
        if not callable(on):
            return

        async def resolve_dialog(dialog):
            if self.browser_settings['dialog_policy'] == 'auto_accept':
                await dialog.accept()
            else:
                await dialog.dismiss()

        def dialog_seen(dialog):
            if not callable(getattr(dialog, 'dismiss', None)):
                return
            task = asyncio.create_task(resolve_dialog(dialog))
            self._page_tasks.add(task)
            task.add_done_callback(self._page_tasks.discard)
        on('dialog', dialog_seen)

        def download_started(item: Any) -> None:
            operation_id = self._active_operations.get(str(backend_id), "")
            if operation_id in self._explicit_download_operations:
                return
            task = asyncio.create_task(
                self._capture_download(str(backend_id), operation_id, item),
                name=f"browser-download:{backend_id}",
            )
            self._download_tasks.add(task)
            task.add_done_callback(self._download_tasks.discard)

        on("download", download_started)

    async def _capture_download(
        self, backend_id: str, operation_id: str, item: Any,
    ) -> None:
        try:
            path = await item.path()
            if not path:
                raise BrowserUnavailable(
                    "browser download completed without a readable path"
                )
            file_size = await asyncio.to_thread(os.path.getsize, path)
            if file_size > _MAX_DOWNLOAD_BYTES:
                delete = getattr(item, "delete", None)
                if callable(delete):
                    await delete()
                raise BrowserValidationError(
                    "browser download exceeds 256 MiB"
                )

            def read_download() -> bytes:
                with open(path, "rb") as handle:
                    payload = handle.read(_MAX_DOWNLOAD_BYTES + 1)
                if len(payload) > _MAX_DOWNLOAD_BYTES:
                    raise BrowserValidationError(
                        "browser download exceeds 256 MiB"
                    )
                return payload

            download = AdapterDownload(
                suggested_filename=str(
                    getattr(item, "suggested_filename", "") or "download"
                ),
                url=str(getattr(item, "url", "") or ""),
                payload=await asyncio.to_thread(read_download),
            )
            if self._download_sink is not None:
                await self._download_sink(backend_id, operation_id, download)
        except Exception as exc:
            del exc

    async def _perform_with_expected_download(
        self,
        page: Any,
        action: str,
        params: Mapping[str, Any],
        timeout_ms: int,
    ) -> tuple[Any, AdapterDownload]:
        declared_sizes: dict[str, int] = {}

        def response_seen(response: Any) -> None:
            try:
                headers = getattr(response, "headers", {}) or {}
                raw = headers.get("content-length") or headers.get("Content-Length")
                if raw is not None:
                    declared_sizes[str(getattr(response, "url", "") or "")] = int(raw)
            except (TypeError, ValueError):
                return

        on = getattr(page, "on", None)
        if callable(on):
            on("response", response_seen)
        try:
            async with page.expect_download(timeout=timeout_ms) as pending:
                value = await self._perform_core(page, action, params)
            item = await pending.value
            item_url = str(getattr(item, "url", "") or "")
            if declared_sizes.get(item_url, 0) > _MAX_DOWNLOAD_BYTES:
                cancel = getattr(item, "cancel", None)
                if callable(cancel):
                    await cancel()
                raise BrowserValidationError("browser download exceeds 256 MiB")
            path = await item.path()
            if not path:
                raise BrowserUnavailable(
                    "browser download completed without a readable path"
                )
            file_size = await asyncio.to_thread(os.path.getsize, path)
            if file_size > _MAX_DOWNLOAD_BYTES:
                delete = getattr(item, "delete", None)
                if callable(delete):
                    await delete()
                raise BrowserValidationError("browser download exceeds 256 MiB")

            def read_download() -> bytes:
                with open(path, "rb") as handle:
                    payload = handle.read(_MAX_DOWNLOAD_BYTES + 1)
                if len(payload) > _MAX_DOWNLOAD_BYTES:
                    raise BrowserValidationError(
                        "browser download exceeds 256 MiB"
                    )
                return payload

            download = AdapterDownload(
                suggested_filename=str(
                    getattr(item, "suggested_filename", "") or "download"
                ),
                url=item_url,
                payload=await asyncio.to_thread(read_download),
            )
            return value, download
        finally:
            remove = getattr(page, "remove_listener", None) or getattr(page, "off", None)
            if callable(remove):
                remove("response", response_seen)

    async def _start_runtime(self) -> Any:
        if self.playwright_factory is None:
            try:
                if (not self.personal_source and not self.launch_options.get('executable_path')
                        and not self.browser_settings.get('executable_path')
                        and not self.browser_settings.get('cdp_url') and self.cloud_lease is None):
                    await ensure_chromium_runtime()
            except BrowserProvisionError as exc:
                raise BrowserUnavailable(
                    f"managed Chromium is unavailable: {exc}"
                ) from exc
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise BrowserUnavailable(
                    "Playwright is not installed; install the Python package and Chromium runtime"
                ) from exc
            candidate = async_playwright()
        else:
            candidate = self.playwright_factory()
        if inspect.isawaitable(candidate):
            candidate = await candidate
        if hasattr(candidate, "chromium"):
            return candidate
        starter = getattr(candidate, "start", None)
        if not callable(starter):
            raise BrowserUnavailable("Playwright factory did not return a runtime or startable manager")
        runtime = starter()
        return await runtime if inspect.isawaitable(runtime) else runtime

    async def launch(self, targets: Sequence[TargetRecord]) -> tuple[AdapterTarget, ...]:
        if self._context is not None:
            return await self.targets()
        if self.personal_source:
            from .personal_profiles import BrowserProfileRequired, prepare_snapshot
            if not os.path.isfile(str(self.personal_source.get('executable') or '')):
                raise BrowserProfileRequired('selection_required', 'The selected browser executable is unavailable. Choose a browser again.')
            await prepare_snapshot(self.personal_source, self.profile_dir)
        os.makedirs(self.profile_dir, exist_ok=True)
        self._playwright = await self._start_runtime()
        options = dict(self.launch_options)
        options.setdefault("headless", self.headless)
        if self.browser_settings.get('executable_path'):
            options['executable_path'] = self.browser_settings['executable_path']
        if self.browser_settings['record_sessions']:
            options['record_video_dir'] = os.path.join(self.profile_dir, 'recordings', self.browser_settings.get('_recording_id', 'direct'))
        if self.personal_source:
            options['executable_path'] = self.personal_source['executable']
            options['args'] = [*options.get('args', []), '--profile-directory=Default', '--no-first-run', '--no-default-browser-check']
            options['ignore_default_args'] = ['--use-mock-keychain', '--password-store=basic']
        try:
            endpoint = (await self.cloud_lease.connect() if self.cloud_lease is not None
                        else self.browser_settings.get('cdp_url'))
            if endpoint:
                self._remote_browser = await self._playwright.chromium.connect_over_cdp(
                    endpoint, timeout=self.browser_settings['command_timeout_s'] * 1000)
                contexts = list(self._remote_browser.contexts)
                if len(contexts) > 1:
                    raise BrowserUnavailable('The CDP endpoint has multiple browser contexts; use an endpoint with one explicit profile.')
                self._owns_context = not contexts
                self._context = contexts[0] if contexts else await self._remote_browser.new_context()
            else:
                self._context = await self._playwright.chromium.launch_persistent_context(
                    self.profile_dir, **options
                )
            if not self.browser_settings['allow_private_urls']:
                async def private_route(route):
                    if await is_private_url(route.request.url):
                        await route.abort('blockedbyclient')
                    else:
                        await route.continue_()
                self._private_route = private_route
                await self._context.route('**/*', private_route)
        except Exception as exc:
            await self.close()
            if isinstance(exc, BrowserUnavailable):
                raise
            # Connection errors can contain a signed remote endpoint.
            raise BrowserUnavailable(f"Browser connection failed: {connection_error_detail(exc)}") from exc

        pages = list(getattr(self._context, "pages", ()) or ())
        wanted = [
            record for record in targets
            if str(record.state or "") not in {"closed", "orphaned"}
        ]
        unclaimed = list(pages)
        markers = {id(page): await self._page_marker(page) for page in pages}

        # First recover the explicit marker stored in that top-level tab.
        for record in wanted:
            matches = [
                page for page in unclaimed
                if markers.get(id(page)) == record.backend_target_id
            ]
            if len(matches) == 1:
                page = matches[0]
                self._pages[record.backend_target_id] = page
                unclaimed.remove(page)

        # Old sessions predate markers. Use URL only when it is unique; an
        # ambiguous pair must never be rebound by list position.
        for record in wanted:
            if record.backend_target_id in self._pages:
                continue
            expected_url = str(record.url or "")
            matches = [
                page for page in unclaimed
                if expected_url
                and str(getattr(page, "url", "") or "") == expected_url
            ]
            if len(matches) == 1:
                page = matches[0]
                self._pages[record.backend_target_id] = page
                unclaimed.remove(page)

        # Recreate any durable page that still has no unambiguous live owner.
        for record in wanted:
            if record.backend_target_id in self._pages:
                continue
            page = await self._context.new_page()
            self._pages[record.backend_target_id] = page
            if record.url and record.url != "about:blank":
                try:
                    await page.goto(
                        navigation_url(record.url), wait_until="domcontentloaded",
                        timeout=self.browser_settings['navigation_timeout_s'] * 1000
                    )
                except Exception:
                    pass
            await self._stamp_page(page, record.backend_target_id)

        for page in unclaimed:
            backend_id = self._new_backend_id()
            self._pages[backend_id] = page
            await self._stamp_page(page, backend_id)
        if not self._pages:
            backend_id = self._new_backend_id()
            page = await self._context.new_page()
            self._pages[backend_id] = page
            await self._stamp_page(page, backend_id)
        for backend_id, page in self._pages.items():
            self._attach_page_events(backend_id, page)
            record = next((item for item in wanted if item.backend_target_id == backend_id), None)
            if record is not None and getattr(record, 'viewport', {}).get('mode') in {'fixed', 'explicit'}:
                request = viewport_request(record.viewport)
                await page.set_viewport_size({key: request[key] for key in ('width', 'height')})
                self._viewport_modes[backend_id] = 'fixed'
        on = getattr(self._context, "on", None)
        if callable(on):
            on("page", self._page_created)
        self._active_id = (
            self._preferred_active_id
            if self._preferred_active_id in self._pages
            else next(iter(self._pages))
        )
        return await self.targets()

    @staticmethod
    def _new_backend_id() -> str:
        return "pw_" + uuid.uuid4().hex

    async def _page(self, backend_target_id: str) -> Any:
        page = self._pages.get(backend_target_id)
        if page is None or bool(getattr(page, "is_closed", lambda: False)()):
            raise BrowserUnavailable(f"managed page {backend_target_id!r} is not live")
        return page

    async def _describe(self, backend_target_id: str, page: Any) -> AdapterTarget:
        title = ""
        try:
            title = str(await page.title() or "")
        except Exception:
            pass
        return AdapterTarget(
            backend_target_id=backend_target_id,
            title=title,
            url=str(getattr(page, "url", "") or ""),
            active=backend_target_id == self._active_id,
            viewport=self._viewport(page, backend_target_id),
        )

    def _viewport(self, page: Any, backend_target_id: str) -> dict[str, Any]:
        size = getattr(page, 'viewport_size', None)
        if not isinstance(size, Mapping):
            return {}
        return {**size, 'mode': self._viewport_modes.get(backend_target_id, 'auto'),
                'device_scale_factor': self.launch_options.get('device_scale_factor', 1)}

    async def _discover_pages(self) -> None:
        for page in list(getattr(self._context, "pages", ()) or ()):
            await self._register_external_page(page)
        for backend_id, page in list(self._pages.items()):
            try:
                closed = bool(page.is_closed())
            except Exception:
                closed = False
            if closed:
                self._pages.pop(backend_id, None)

    async def targets(self) -> tuple[AdapterTarget, ...]:
        if self._context is None:
            return ()
        await self._discover_pages()
        return tuple([
            await self._describe(backend_id, page)
            for backend_id, page in list(self._pages.items())
        ])

    async def new_page(self, backend_target_id: str, url: str = "") -> AdapterTarget:
        self.require("tabs")
        url = navigation_url(url) if url else ""
        if self._context is None:
            raise BrowserUnavailable("managed browser is not launched")
        if backend_target_id in self._pages:
            raise BrowserValidationError("backend target ID is already live")
        async with self._page_registration_lock:
            if backend_target_id in self._pages:
                raise BrowserValidationError("backend target ID is already live")
            page = await self._context.new_page()
            self._pages[backend_target_id] = page
            await self._stamp_page(page, backend_target_id)
            self._attach_page_events(backend_target_id, page)
        self._active_id = backend_target_id
        if url:
            await page.goto(url, wait_until="domcontentloaded", timeout=self.browser_settings['navigation_timeout_s'] * 1000)
        return await self._describe(backend_target_id, page)

    async def close_page(self, backend_target_id: str) -> None:
        page = await self._page(backend_target_id)
        await page.close()
        self._pages.pop(backend_target_id, None)
        if self._active_id == backend_target_id:
            self._active_id = next(iter(self._pages), "")

    async def activate_page(self, backend_target_id: str) -> AdapterTarget:
        page = await self._page(backend_target_id)
        bring = getattr(page, "bring_to_front", None)
        if callable(bring):
            await bring()
        self._active_id = backend_target_id
        return await self._describe(backend_target_id, page)

    @staticmethod
    def _locator(page: Any, backend_ref: str) -> Any:
        if not _MANAGED_ELEMENT_RE.fullmatch(str(backend_ref or "")):
            raise BrowserValidationError("element reference is not a managed observation reference")
        return page.locator(f'[data-variant1-fabric-ref="{backend_ref}"]')

    async def observe(
        self,
        backend_target_id: str,
        *,
        max_chars: int,
        max_elements: int,
        include_html: bool,
        include_screenshot: bool,
    ) -> AdapterObservation:
        page = await self._page(backend_target_id)
        identity = await page.evaluate(_DOCUMENT_IDENTITY) if include_screenshot else None
        token = "mf_" + uuid.uuid4().hex[:12]
        raw = await page.evaluate(
            _SNAPSHOT_FUNCTION,
            {"token": token, "maxChars": max_chars, "maxElements": max_elements, "includeHtml": include_html},
        )
        raw = raw if isinstance(raw, Mapping) else {}
        html = str(raw.get("html") or "") if include_html else ""
        screenshot = bytes(await page.screenshot(full_page=False)) if include_screenshot else b""
        if include_screenshot:
            _same_document(identity, await page.evaluate(_DOCUMENT_IDENTITY))
        return AdapterObservation(
            title=str(raw.get("title") or ""),
            url=str(raw.get("url") or getattr(page, "url", "") or ""),
            text=str(raw.get("text") or "")[:max_chars],
            html=html,
            elements=tuple(
                item for item in (raw.get("elements") or ()) if isinstance(item, Mapping)
            )[:max_elements],
            screenshot=screenshot,
            viewport={**self._viewport(page, backend_target_id), **dict(raw.get('viewport') or {})},
        )

    async def _perform_core(self, page: Any, action: str, params: Mapping[str, Any]) -> Any:
        settings = getattr(self, 'browser_settings', DEFAULTS)
        timeout_key = ('click_timeout_s' if action == 'click' else
                       'navigation_timeout_s' if action in {'navigate', 'back', 'forward', 'reload'} else 'command_timeout_s')
        default_timeout_ms = settings[timeout_key] * 1000
        timeout_ms = max(
            1,
            min(int(
                params.get("timeout_ms", params.get("timeout"))
                or default_timeout_ms
            ), 300_000),
        )
        backend_ref = str(params.get("backend_ref") or "")
        locator = self._locator(page, backend_ref) if backend_ref else None
        if locator is not None:
            count = await locator.count()
            if count == 0:
                raise BrowserStaleReference(
                    "observed element no longer exists in this page document"
                )
            if count != 1:
                raise BrowserValidationError("observed element reference is ambiguous")
        if action == "navigate":
            response = await page.goto(
                navigation_url(params.get("url")), wait_until=str(params.get("wait_until") or "domcontentloaded"),
                timeout=timeout_ms,
            )
            return self._response_value(response)
        if action == "back":
            response = await page.go_back(wait_until="domcontentloaded", timeout=timeout_ms)
            return self._response_value(response)
        if action == "forward":
            response = await page.go_forward(wait_until="domcontentloaded", timeout=timeout_ms)
            return self._response_value(response)
        if action == "reload":
            response = await page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
            return self._response_value(response)
        if action == "click":
            if locator is None:
                raise BrowserValidationError("click requires an ElementRef")
            click_options: dict[str, Any] = {"timeout": timeout_ms}
            if "force" in params:
                click_options["force"] = bool(params.get("force"))
            position = params.get("position")
            if position is not None:
                if not isinstance(position, Mapping):
                    raise BrowserValidationError("click position must be an x/y mapping")
                try:
                    click_options["position"] = {
                        "x": float(position["x"]),
                        "y": float(position["y"]),
                    }
                except (KeyError, TypeError, ValueError) as exc:
                    raise BrowserValidationError(
                        "click position must contain numeric x and y"
                    ) from exc
            if params.get("button") is not None:
                click_options["button"] = str(params["button"])
            click_count = params.get("click_count", params.get("count"))
            if click_count is not None:
                click_options["click_count"] = max(1, int(click_count))
            if params.get("delay") is not None:
                click_options["delay"] = max(0.0, float(params["delay"]))
            return await locator.click(**click_options)
        if action == "fill":
            if locator is None:
                raise BrowserValidationError("fill requires an ElementRef")
            return await locator.fill(str(params.get("text") or ""), timeout=timeout_ms)
        if action == "select":
            if locator is None:
                raise BrowserValidationError("select requires an ElementRef")
            values = params.get("values", params.get("value", ""))
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, Sequence):
                raise BrowserValidationError("select values must be a string or sequence")
            return await locator.select_option([str(item) for item in values], timeout=timeout_ms)
        if action == "hover":
            if locator is None:
                raise BrowserValidationError("hover requires an ElementRef")
            return await locator.hover(timeout=timeout_ms)
        if action == "keys":
            keys = str(params.get("keys") or "")
            if not keys or len(keys) > 500:
                raise BrowserValidationError("keys must contain between 1 and 500 characters")
            if locator is not None:
                return await locator.press(keys, timeout=timeout_ms)
            return await page.keyboard.press(keys)
        if action == "wait":
            condition = str(params.get("condition") or "timeout")
            if condition == "timeout":
                await asyncio.sleep(min(timeout_ms, 30_000) / 1000.0)
                return {"condition": condition, "elapsed_ms": min(timeout_ms, 30_000)}
            if condition == "load_state":
                state = str(params.get("state") or "domcontentloaded")
                await page.wait_for_load_state(state, timeout=timeout_ms)
                return {"condition": condition, "state": state}
            if condition == "url":
                expected = str(params.get("url") or "")
                await page.wait_for_url(expected, timeout=timeout_ms)
                return {"condition": condition, "url": str(getattr(page, "url", ""))}
            if condition == "element":
                if locator is None:
                    raise BrowserValidationError("element wait requires an ElementRef")
                state = str(params.get("state") or "visible")
                await locator.wait_for(state=state, timeout=timeout_ms)
                return {"condition": condition, "state": state}
            raise BrowserValidationError(f"unknown wait condition {condition!r}")
        if action == "evaluate":
            expression = str(params.get("expression") or "")
            if not expression or len(expression) > 65_536:
                raise BrowserValidationError("evaluate expression must contain 1 to 65536 characters")
            return json_value(await page.evaluate(expression, json_value(params.get("arg"))))
        if action == "screenshot":
            identity = await page.evaluate(_DOCUMENT_IDENTITY)
            result = bytes(await page.screenshot(
                full_page=bool(params.get("full_page", False)),
                type=str(params.get("type") or "png"),
            ))
            _same_document(identity, await page.evaluate(_DOCUMENT_IDENTITY))
            return result
        if action == 'set_viewport':
            request = viewport_request(params)
            size = {key: request[key] for key in ('width', 'height')} if request['mode'] == 'fixed' else {'width': 1280, 'height': 800}
            await page.set_viewport_size(size)
            return {'viewport': {**size, 'mode': request['mode']}}
        raise BrowserUnsupported(action, self.kind)

    @staticmethod
    def _response_value(response: Any) -> Any:
        if response is None:
            return None
        return {
            "url": str(getattr(response, "url", "") or ""),
            "status": int(getattr(response, "status", 0) or 0),
            "ok": bool(getattr(response, "ok", False)),
        }

    async def perform(
        self, backend_target_id: str, action: str, params: Mapping[str, Any]
    ) -> AdapterResult:
        self.require(action)
        page = await self._page(backend_target_id)
        self._attach_page_events(backend_target_id, page)
        before_url = str(getattr(page, "url", "") or "")
        operation_id = str(params.get("_operation_id") or "")
        expect_download = bool(params.get("expect_download"))
        if expect_download and action not in {"click", "keys"}:
            raise BrowserValidationError(
                "expect_download is supported for click and keys"
            )
        self._active_operations[backend_target_id] = operation_id
        if expect_download:
            self._explicit_download_operations.add(operation_id)
        try:
            if expect_download:
                timeout_ms = max(
                    1, min(int(params.get("timeout_ms") or 30_000), 120_000)
                )
                value, download = await self._perform_with_expected_download(
                    page, action, params, timeout_ms
                )
            else:
                value = await self._perform_core(page, action, params)
                download = None
        finally:
            self._active_operations.pop(backend_target_id, None)
            self._explicit_download_operations.discard(operation_id)
        after_url = str(getattr(page, "url", "") or "")
        title = ""
        try:
            title = str(await page.title() or "")
        except Exception:
            pass
        screenshot = value if action == "screenshot" and isinstance(value, bytes) else b""
        if screenshot:
            value = {"captured": True, "bytes": len(screenshot), **image_dimensions(screenshot), 'viewport': self._viewport(page, backend_target_id)}
        if action == 'set_viewport':
            self._viewport_modes[backend_target_id] = viewport_request(params)['mode']
        return AdapterResult(
            value=json_value(value), title=title, url=after_url,
            navigated=(action in {"navigate", "back", "forward", "reload"} or after_url != before_url),
            screenshot=screenshot, download=download, targets=await self.targets(),
        )

    async def start_trace(self, options: Mapping[str, Any]) -> None:
        self.require("trace")
        if self._context is None or self._trace_active:
            raise BrowserValidationError("trace is already recording or browser is not live")
        await self._context.tracing.start(
            screenshots=bool(options.get("screenshots", True)),
            snapshots=bool(options.get("snapshots", True)),
            sources=bool(options.get("sources", True)),
            title=str(options.get("title") or "VARIANT-1 Browser Trace")[:500],
        )
        self._trace_active = True

    async def stop_trace(self, path: str) -> None:
        self.require("trace")
        if self._context is None or not self._trace_active:
            raise BrowserValidationError("no trace is recording")
        try:
            await self._context.tracing.stop(path=path)
        finally:
            self._trace_active = False

    async def close(self) -> None:
        try:
            for task in tuple(self._page_tasks):
                task.cancel()
            await asyncio.gather(*self._page_tasks, return_exceptions=True)
            self._page_tasks.clear()
            if self._context is not None:
                if self._private_route is not None:
                    await self._context.unroute('**/*', self._private_route)
                    self._private_route = None
                if self._owns_context:
                    await self._context.close()
        finally:
            try:
                if self._playwright is not None:
                    stop = getattr(self._playwright, "stop", None)
                    if callable(stop):
                        result = stop()
                        if inspect.isawaitable(result):
                            await result
            finally:
                self._context = None
                self._playwright = None
                self._pages.clear()
                self._active_id = ""
                self._trace_active = False
                for task in tuple(self._download_tasks):
                    if not task.done():
                        task.cancel()
                self._download_tasks.clear()
                self._download_pages.clear()
                self._active_operations.clear()
                self._explicit_download_operations.clear()
                self._remote_browser = None
                if self.cloud_lease is not None:
                    await self.cloud_lease.close()


class EmbeddedBrowserAdapter(BrowserAdapter):
    """RPC adapter for the Deck's visible Hermes-style browser tabs."""

    kind = "embedded"
    capabilities = EMBEDDED_CAPABILITIES

    def __init__(self, request: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
                 *, owner_chat_id: str = "") -> None:
        if request is None:
            from browser_fabric.interactive import request_host
            request = request_host
        self._request = request
        self.owner_chat_id = str(owner_chat_id or "")
        self._target_ids: set[str] = set()
        self._active_id = ""
        self._states: dict[str, dict[str, Any]] = {}
        self._download_sink: Callable[..., Awaitable[Any]] | None = None
        self._download_progress: dict[str, list[dict[str, Any]]] = {}

    def set_download_sink(self, sink: Callable[..., Awaitable[Any]]) -> None:
        self._download_sink = sink

    @staticmethod
    def _state(result: Mapping[str, Any]) -> dict[str, Any]:
        nested = result.get("state")
        return dict(nested) if isinstance(nested, Mapping) else dict(result)

    async def _call(self, action: str, **params: Any) -> dict[str, Any]:
        try:
            result = await self._request({"action": action, **params,
                                          "owner_chat_id": self.owner_chat_id})
        except BrowserUnsupported:
            raise
        except Exception as exc:
            if "element reference is stale" in str(exc).casefold():
                raise BrowserStaleReference(str(exc)) from exc
            raise BrowserUnavailable(f"embedded browser request failed: {exc}") from exc
        if not isinstance(result, Mapping):
            raise BrowserUnavailable("embedded browser returned a malformed response")
        if "ok" in result and not result["ok"]:
            raise BrowserUnavailable(str(result.get("error") or "embedded browser command failed"))
        current = self._state(result)
        target_id = str(
            current.get("tab_id") or params.get("tab_id")
            or params.get("target_id") or self._active_id or ""
        )
        if target_id and action != "tabs":
            self._states.setdefault(target_id, {}).update(current)
            self._target_ids.add(target_id)
            if bool(current.get("active", True)):
                self._active_id = target_id
        return dict(result)

    async def launch(self, targets: Sequence[TargetRecord]) -> tuple[AdapterTarget, ...]:
        # The renderer's owned tab inventory survives guest remounts independently
        # of this Python adapter. Adopt it before considering durable restore IDs.
        live = await self.targets()
        if live:
            return live
        for target in targets:
            target_id = str(target.backend_target_id)
            await self._call(
                "new_page", tab_id=target_id,
                url=str(target.url or "about:blank"),
            )
            self._target_ids.add(target_id)
            self._active_id = target_id
            if getattr(target, 'viewport', {}).get('mode') in {'fixed', 'explicit'}:
                await self._call('set_viewport', tab_id=target_id, **viewport_request(target.viewport))
        return await self.targets()

    async def targets(self) -> tuple[AdapterTarget, ...]:
        result = await self._call("tabs")
        rows = result.get("tabs") if isinstance(result, Mapping) else ()
        targets: list[AdapterTarget] = []
        if not isinstance(rows, list):
            raise BrowserUnavailable("embedded browser did not return an authoritative tab inventory")
        live_ids = set()
        self._active_id = ""
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            target_id = str(raw.get("id") or raw.get("tab_id") or "")
            if not target_id:
                continue
            live_ids.add(target_id)
            self._states.setdefault(target_id, {}).update(dict(raw))
            if bool(raw.get("active")):
                self._active_id = target_id
            targets.append(AdapterTarget(
                backend_target_id=target_id,
                title=str(raw.get("title") or ""),
                url=str(raw.get("url") or ""),
                active=bool(raw.get("active")),
                viewport=dict(raw.get('viewport') or {}),
            ))
        self._target_ids = live_ids
        self._states = {key: value for key, value in self._states.items() if key in live_ids}
        return tuple(targets)

    def _cached_targets(self) -> tuple[AdapterTarget, ...]:
        return tuple(AdapterTarget(
            backend_target_id=target_id,
            title=str(self._states.get(target_id, {}).get("title") or ""),
            url=str(self._states.get(target_id, {}).get("url") or ""),
            active=(target_id == self._active_id),
            viewport=dict(self._states.get(target_id, {}).get('viewport') or {}),
        ) for target_id in self._target_ids)

    async def new_page(self, backend_target_id: str, url: str = "") -> AdapterTarget:
        target_id = str(backend_target_id)
        result = await self._call(
            "new_page", tab_id=target_id, url=str(url or "about:blank")
        )
        self._target_ids.add(target_id)
        self._active_id = target_id
        raw = result.get("target") if isinstance(result.get("target"), Mapping) else {}
        return AdapterTarget(
            backend_target_id=target_id,
            title=str(raw.get("title") or ""),
            url=str(raw.get("url") or url or "about:blank"),
            active=True,
        )

    async def close_page(self, backend_target_id: str) -> None:
        target_id = str(backend_target_id)
        await self._call("close_page", tab_id=target_id)
        self._target_ids.discard(target_id)
        self._states.pop(target_id, None)
        if self._active_id == target_id:
            self._active_id = next(iter(self._target_ids), "")

    async def activate_page(self, backend_target_id: str) -> AdapterTarget:
        target_id = str(backend_target_id)
        if target_id not in self._target_ids:
            raise BrowserUnavailable("embedded target is not live")
        result = await self._call("activate_page", tab_id=target_id)
        self._active_id = target_id
        raw = result.get("target") if isinstance(result.get("target"), Mapping) else {}
        return AdapterTarget(
            backend_target_id=target_id,
            title=str(raw.get("title") or self._states.get(target_id, {}).get("title") or ""),
            url=str(raw.get("url") or self._states.get(target_id, {}).get("url") or ""),
            active=True,
        )

    async def observe(
        self,
        backend_target_id: str,
        *,
        max_chars: int,
        max_elements: int,
        include_html: bool,
        include_screenshot: bool,
    ) -> AdapterObservation:
        if backend_target_id not in self._target_ids:
            raise BrowserUnavailable("embedded target is not live")
        await self._drain_downloads("", backend_target_id)
        identity = None
        if include_html or include_screenshot:
            proof = await self._call("evaluate", tab_id=backend_target_id, expression=_DOCUMENT_IDENTITY)
            identity = proof.get("value")
        result = await self._call(
            "read", tab_id=backend_target_id,
            max_elements=max_elements, max_chars=max_chars,
        )
        html = ""
        screenshot = b""
        if include_html:
            html_result = await self._call("html", tab_id=backend_target_id)
            html = str(html_result.get("html") or "")
        if include_screenshot:
            shot = await self._call("screenshot", tab_id=backend_target_id)
            try:
                screenshot = base64.b64decode(str(shot.get("image") or ""), validate=True)
            except Exception as exc:
                raise BrowserUnavailable("embedded browser returned an invalid screenshot") from exc
        if include_html or include_screenshot:
            proof = await self._call("evaluate", tab_id=backend_target_id, expression=_DOCUMENT_IDENTITY)
            _same_document(identity, proof.get("value"))
        elements: list[Mapping[str, Any]] = []
        for raw in result.get("elements") or ():
            if not isinstance(raw, Mapping):
                continue
            ref = str(raw.get("ref") or "")
            if not _EMBEDDED_ELEMENT_RE.fullmatch(ref):
                continue
            editable = str(raw.get("role") or "") in {"textbox", "combobox"}
            elements.append({
                "backend_ref": ref,
                "role": str(raw.get("role") or "control"),
                "name": str(raw.get("name") or ""),
                "input_type": str(raw.get("input_type") or ""),
                "disabled": bool(raw.get("disabled")),
                "visible": True,
                "editable": editable,
                "actions": (
                    ["click", "fill", "keys"]
                    if editable else ["click", "keys"]
                ),
            })
        state = self._state(result)
        snapshot_url = str(result.get("url") or state.get("url") or "")
        if result.get("url") and state.get("url") and result["url"] != state["url"]:
            raise BrowserStaleReference("page navigated during text observation")
        return AdapterObservation(
            title=str(result.get("title") or state.get("title") or ""),
            url=snapshot_url,
            text=str(result.get("text") or "")[:max_chars], html=html,
            elements=tuple(elements[:max_elements]), screenshot=screenshot,
            viewport=dict(result.get('viewport') or state.get('viewport') or {}),
        )

    async def collect_downloads(self, backend_target_id: str) -> None:
        await self._drain_downloads("", backend_target_id)

    def download_progress(self, backend_target_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self._download_progress.get(backend_target_id, ())]

    async def document_status(self, backend_target_id: str) -> dict[str, Any]:
        await self.targets()
        state = self._states.get(backend_target_id, {})
        return {key: state[key] for key in ("document_ready", "url") if key in state}

    async def _drain_downloads(
        self, operation_id: str, backend_target_id: str,
    ) -> None:
        if self._download_sink is None:
            return
        result = await self._call(
            "drain_downloads", tab_id=backend_target_id
        )
        for raw in result.get("downloads") or ():
            if not isinstance(raw, Mapping) or str(raw.get("status") or raw.get("state") or "") != "completed":
                continue
            download_id = str(raw.get("download_id") or "")
            if not download_id or not raw.get("path"):
                continue
            committed = await self._download_sink(
                backend_target_id,
                str(raw.get("operation_id") or operation_id),
                AdapterDownload(
                    suggested_filename=str(
                        raw.get("suggested_filename") or "download"
                    ),
                    url=str(raw.get("url") or ""),
                    path=str(raw["path"]),
                    sha256=str(raw.get("sha256") or ""),
                    bytes_count=int(raw.get("bytes") or 0),
                    download_id=download_id,
                ),
            )
            if committed:
                await self._call("ack_downloads", tab_id=backend_target_id,
                                 download_ids=[download_id])
        # Completion and progress are separate native projections. A download
        # can be active after the initiating browser action has returned.
        progress = await self._call("downloads", tab_id=backend_target_id)
        self._download_progress[backend_target_id] = [
            {key: row[key] for key in (
                "download_id", "operation_id", "status", "suggested_filename",
                "url", "bytes", "total_bytes", "error", "started_at",
            ) if key in row}
            for row in progress.get("downloads") or ()
            if isinstance(row, Mapping) and row.get("status") != "stored"
        ]

    async def perform(
        self, backend_target_id: str, action: str, params: Mapping[str, Any]
    ) -> AdapterResult:
        self.require(action)
        if backend_target_id not in self._target_ids:
            raise BrowserUnavailable("embedded target is not live")
        cached = self._states.get(backend_target_id, {})
        before_url = str(cached.get("url") or "")
        if action == "wait":
            condition = str(params.get("condition") or "timeout")
            if condition != "timeout":
                raise BrowserUnsupported("wait:" + condition, self.kind, "Electron host exposes timeout waits only")
            timeout_ms = max(0, min(int(params.get("timeout_ms") or 1000), 30_000))
            await asyncio.sleep(timeout_ms / 1000.0)
            result: dict[str, Any] = {"condition": condition, "elapsed_ms": timeout_ms}
        elif action == "screenshot":
            result = await self._call(
                "screenshot", tab_id=backend_target_id
            )
        else:
            payload: dict[str, Any] = {"tab_id": backend_target_id}
            if action == "navigate":
                payload["url"] = str(params.get("url") or "")
            elif action in {"click", "fill", "select", "hover"}:
                ref = normalize_embedded_target(params.get("backend_ref"))
                payload["target"] = ref
                if action == "fill":
                    payload["text"] = str(params.get("text") or "")
                elif action == "select":
                    values = params.get("values", params.get("value", ""))
                    payload["values"] = (
                        [values] if isinstance(values, str)
                        else [str(item) for item in values or ()]
                    )
                else:
                    for key in (
                        "position", "force", "timeout_ms", "button",
                        "click_count", "count", "delay",
                    ):
                        if key in params:
                            payload[key] = params[key]
            elif action == "keys":
                payload["keys"] = str(params.get("keys") or "")
                if params.get("backend_ref"):
                    payload["target"] = normalize_embedded_target(
                        params.get("backend_ref")
                    )
            elif action == "evaluate":
                payload["expression"] = str(params.get("expression") or "")
                if "arg" in params:
                    payload["arg"] = json_value(params.get("arg"))
            elif action == 'set_viewport':
                payload.update(viewport_request(params))
            if params.get("_operation_id"):
                payload["operation_id"] = str(params["_operation_id"])
            result = await self._call(action, **payload)
        await self._drain_downloads(
            str(params.get("_operation_id") or ""), backend_target_id
        )
        screenshot = b""
        if action == "screenshot":
            try:
                screenshot = base64.b64decode(str(result.get("image") or ""), validate=True)
            except Exception as exc:
                raise BrowserUnavailable("embedded browser returned an invalid screenshot") from exc
            value: Any = {"captured": True, "bytes": len(screenshot), **image_dimensions(screenshot),
                          'viewport': dict(result.get('viewport') or self._state(result).get('viewport') or {})}
        elif action == "evaluate":
            value = result.get("value")
        else:
            value = result.get("message") or result.get("value") or {
                key: item for key, item in result.items() if key != "state"
            }
        state = self._state(result)
        cached = self._states.get(backend_target_id, {})
        after_url = str(state.get("url") or cached.get("url") or "")
        return AdapterResult(
            value=json_value(value), title=str(
                state.get("title") or cached.get("title") or ""
            ),
            url=after_url,
            navigated=(
                bool(result.get("navigated"))
                if "navigated" in result or action in {"back", "forward", "reload"}
                else action == "navigate" or after_url != before_url
            ),
            screenshot=screenshot, targets=self._cached_targets(),
        )

    async def close(self) -> None:
        # The workbench owns visible browser tabs. Releasing a Fabric lease
        # must not close pages the user may still be inspecting.
        self._target_ids.clear()
        self._states.clear()
        self._active_id = ""


__all__ = [
    "AdapterDownload", "AdapterObservation", "AdapterResult", "AdapterTarget",
    "BrowserAdapter", "EMBEDDED_CAPABILITIES", "EmbeddedBrowserAdapter",
    "MANAGED_CAPABILITIES", "ManagedPlaywrightAdapter",
]
