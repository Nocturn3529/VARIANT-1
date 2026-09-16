import {BrowserCommandError, browserDeadline, checkBrowserRequest} from "./browserLifecycle";
import {allowedBrowserUrl, getPreviewState, setBrowserViewport, type BrowserPageState} from "./previewStore";
import {applyBrowserViewport, browserWindowSize, DEFAULT_BROWSER_VIEWPORT, measureBrowserViewport, parseBrowserViewport} from "./browserViewport";
import {downloadsForTab, findStartedDownload, isDownloadCommand, runBrowserDownloadCommand} from "./browserDownloads";
import {browserKeyInput} from "./browserKeyboard";

export type WorkbenchWebview = HTMLElement & {
  flushLayout?:()=>Promise<void>;
  loadURL?: (url: string) => Promise<void>;
  getURL?: () => string;
  getTitle?: () => string;
  canGoBack?: () => boolean;
  canGoForward?: () => boolean;
  goBack?: () => void | Promise<void>;
  goForward?: () => void | Promise<void>;
  reload?: () => void | Promise<void>;
  reloadIgnoringCache?: () => void | Promise<void>;
  executeJavaScript?: (code: string, userGesture?: boolean) => Promise<unknown>;
  sendInputEvent?: (event: Record<string, unknown>) => void | Promise<void>;
  openDevTools?: () => void;
  closeDevTools?: () => void;
  isDevToolsOpened?: () => boolean;
  getWebContentsId?: () => number;
  getZoomFactor?: () => number;
  inspectElement?: (x: number, y: number) => void;
  findInPage?: (text: string, options?: Record<string, unknown>) => number;
  stopFindInPage?: (action: string) => void;
};

type BrowserHandle = {
  id: string;
  webview: WorkbenchWebview;
  epoch: number;
  page: () => BrowserPageState;
  generation: number;
  document: number;
  ready: boolean;
  failure: string;
  committedUrl: string;
  navigationUrl: string;
  navigationStartedAt: number;
  tail: Promise<unknown>;
};

const handles = new Map<string, BrowserHandle>();
let activeId = "";
let guestGeneration = 0;
let referenceEpoch = 0;

function state(handle: BrowserHandle) {
  return {...handle.page(), viewport: measureBrowserViewport(handle.webview), document_ready:handle.ready,
    recovery_actions:handle.ready ? [] : ["navigate", "reload", "new_page"], downloads:downloadsForTab(handle.id), download_dialog_pending:false};
}

export const isBrowserRecoveryAction = (action: string) => ["state", "tabs", "navigate", "open", "reload", "back", "forward", "set_visible", "set_viewport", "set_bounds"].includes(action);

export function registerWorkbenchBrowser(
  id: string,
  webview: WorkbenchWebview,
  page: () => BrowserPageState,
): () => void {
  const handle: BrowserHandle = {id, webview, epoch: 0, page, generation: ++guestGeneration, document: 0, ready: false, failure: "", committedUrl:"", navigationUrl:"", navigationStartedAt:0, tail: Promise.resolve()};
  const mark = () => { webview.dataset.browserReady = String(handle.ready); webview.dataset.browserGeneration = String(handle.generation); webview.dataset.browserDocument = String(handle.document); };
  const navigate = (event: Event) => {
    const detail = event as Event & {isMainFrame?: boolean; isInPlace?: boolean; url?:string};
    if (detail.isMainFrame === false || detail.isInPlace) return;
    handle.document++; handle.ready = false; handle.epoch = 0; handle.failure = ""; mark();
    handle.navigationUrl=detail.url || "";handle.navigationStartedAt=Date.now();
  };
  const bind = () => {
    try {
      const guestId = webview.getWebContentsId?.();
      if (guestId) void window.variant1Deck?.bindWorkbenchBrowser?.(id, guestId).catch(() => {});
    } catch { /* did-attach/dom-ready will retry after the native guest exists. */ }
  };
  const ready = () => { handle.ready = true; handle.failure = ""; handle.committedUrl=page().url; bind(); mark(); };
  const stopped = () => {
    if (handle.ready || !handle.committedUrl || !webview.executeJavaScript) return;
    const document = handle.document;
    void browserDeadline(webview.executeJavaScript('({url:location.href,ready:document.readyState})'), 2000, "readiness").then(value => {
      if (handles.get(id) !== handle || !webview.isConnected || handle.document !== document) return;
      const retained = value as {url?:string;ready?:string} | null;
      if (retained?.url === handle.committedUrl && ["interactive", "complete"].includes(String(retained.ready))) {
        handle.ready=true;handle.failure="";mark();
      }
    }).catch(() => { /* Recovery controls remain available for an unreadable document. */ });
  };
  const gone = () => { handle.ready = false; handle.epoch = 0; handle.failure = "Browser renderer stopped; reload the tab"; mark(); };
  webview.addEventListener("did-start-navigation", navigate);
  webview.addEventListener("dom-ready", ready);
  webview.addEventListener("did-attach", bind);
  webview.addEventListener("did-stop-loading", stopped);
  webview.addEventListener("render-process-gone", gone);
  webview.addEventListener("destroyed", gone);
  mark();
  handles.set(id, handle);
  bind();
  activeId = id;
  return () => {
    webview.removeEventListener("did-start-navigation", navigate);
    webview.removeEventListener("dom-ready", ready);
    webview.removeEventListener("did-attach", bind);
    webview.removeEventListener("did-stop-loading", stopped);
    webview.removeEventListener("render-process-gone", gone);
    webview.removeEventListener("destroyed", gone);
    handle.ready = false;
    if (handles.get(id)?.webview === webview) handles.delete(id);
    if (activeId === id) activeId = [...handles.keys()].at(-1) || "";
  };
}

export function activateWorkbenchBrowser(id: string): void {
  if (handles.has(id)) activeId = id;
}

export function workbenchBrowserTargets(ownerChatId?: string): Array<BrowserPageState & {id:string; active:boolean; owner_chat_id:string}> {
  const preview=getPreviewState();
  return preview.tabs.filter(tab=>tab.target.kind==="url" && (ownerChatId===undefined || (tab.ownerChatId || "")===ownerChatId)).map(tab=>({
    ...(preview.pages[tab.id] || {title:tab.target.label,url:tab.target.url,canGoBack:false,canGoForward:false,loading:false}),...(handles.has(tab.id) ? state(handles.get(tab.id)!) : {}),
    id:tab.id,active:tab.id===preview.selectedId,owner_chat_id:tab.ownerChatId || "",
  }));
}

export async function waitForWorkbenchBrowser(tabId = "", timeoutMs = 5000, signal?: AbortSignal, visible = false, requireDocument = true): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    checkBrowserRequest(signal);
    const handle = tabId ? handles.get(tabId) : handles.get(activeId);
    if (requireDocument && handle?.failure) throw new BrowserCommandError("GUEST_STOPPED", handle.failure);
    if (handle && (!requireDocument || handle.ready) && handle.webview.isConnected) {
      const rect = handle.webview.getBoundingClientRect();
      if (!visible || (rect.width > 0 && rect.height > 0 && handle.webview.ownerDocument.visibilityState !== "hidden")) return true;
    }
    await new Promise(resolve => setTimeout(resolve, 25));
  }
  return false;
}

export function browserCommandDiagnostics(command: Record<string, unknown>) {
  const id = String(command.tab_id || command.target_id || activeId);
  const handle = handles.get(id);
  return {tab_id: id, guest_generation: handle?.generation, document_generation: handle?.document,
    guest_ready: !!handle?.ready, guest_attached: !!handle?.webview.isConnected,
    viewport: handle ? measureBrowserViewport(handle.webview) : undefined,
    visibility: handle?.webview.ownerDocument.visibilityState};
}

function selectedHandle(command: Record<string, unknown>): BrowserHandle {
  const requested = String(command.tab_id || command.target_id || "");
  const handle = requested ? handles.get(requested) : handles.get(activeId) || [...handles.values()].at(-1);
  if (!handle) throw new BrowserCommandError("TAB_NOT_FOUND", requested ? "Requested browser tab is not open" : "No browser tab is open");
  activeId = handle.id;
  return handle;
}

function targetRef(value: unknown, epoch: number): string {
  const ref = String(value || "").trim().replace(/^\[|\]$/g, "");
  if (!/^b\d+-\d+$/.test(ref) || !epoch || !ref.startsWith(`b${epoch}-`)) throw new BrowserCommandError("STALE_ELEMENT_REFERENCE", "Element reference is stale; read the page again", "element");
  return ref;
}

function referenceRegistryScript(epoch: number, create: boolean): string {
  return `(() => {
    const key = Symbol.for('variant1.browser.elementRefs.v1');
    let registry = document[key];
    if (!registry || registry.epoch !== ${epoch}) {
      if (!${create}) return null;
      const refs = new Map();
      registry = {epoch: ${epoch}, next: 0, nodes: new WeakMap(), refs,
        collected: new FinalizationRegistry(ref => refs.delete(ref))};
      document[key] = registry;
    }
    return registry;
  })()`;
}

function pageSnapshotScript(epoch: number, maxElements: number, maxChars: number): string {
  return `(() => {
    const epoch = ${JSON.stringify(epoch)};
    const maxElements = ${JSON.stringify(Math.max(0, Math.min(maxElements, 2000)))};
    const maxChars = ${JSON.stringify(Math.max(1, Math.min(maxChars, 500000)))};
    const marker = 'data-variant1-browser-ref';
    const registry = ${referenceRegistryScript(epoch, true)};
    // Weak references never keep removed page nodes alive. Rotate at most 2000
    // entries per read to prune detached nodes even if page code retains them.
    const prune = Math.min(registry.refs.size, 2000);
    for (let i = 0; i < prune; i++) {
      const [ref, weak] = registry.refs.entries().next().value;
      registry.refs.delete(ref);
      const node = weak.deref();
      if (node?.isConnected && node.ownerDocument === document) registry.refs.set(ref, weak);
      else if (node) { registry.nodes.delete(node); registry.collected.unregister(node); }
    }
    const selector = [
      'a[href]','button','input','textarea','select','summary',
      '[contenteditable="true"]','[role="button"]','[role="link"]',
      '[role="checkbox"]','[role="radio"]','[role="tab"]','[role="menuitem"]',
      '[role="textbox"]','[role="combobox"]','[role="option"]','[tabindex]'
    ].join(',');
    const compact = value => String(value || '').replace(/\\s+/g, ' ').trim();
    const visible = element => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden'
        && Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
    };
    const role = element => {
      const explicit = compact(element.getAttribute('role'));
      if (explicit) return explicit;
      const tag = element.tagName.toLowerCase();
      if (tag === 'a') return 'link';
      if (tag === 'button' || tag === 'summary') return 'button';
      if (tag === 'textarea') return 'textbox';
      if (tag === 'select') return 'combobox';
      if (tag === 'input') {
        const type = compact(element.getAttribute('type')).toLowerCase();
        if (type === 'checkbox') return 'checkbox';
        if (type === 'radio') return 'radio';
        if (['button','submit','reset'].includes(type)) return 'button';
        return 'textbox';
      }
      return tag || 'control';
    };
    const inputType = element => element.tagName.toLowerCase() === 'input'
      ? compact(element.getAttribute('type') || 'text').toLowerCase() : '';
    const name = element => compact(
      element.getAttribute('aria-label') || element.getAttribute('alt')
      || element.getAttribute('title') || element.getAttribute('placeholder')
      || (element.labels?.length ? [...element.labels].map(item => item.innerText).join(' ') : '')
      || element.innerText || (inputType(element) === 'password' ? '' : element.value)
    ).slice(0, 500);
    const elements = [];
    for (const element of document.querySelectorAll(selector)) {
      if (elements.length >= maxElements) break;
      if (!visible(element)) continue;
      let ref = registry.nodes.get(element);
      if (!ref || registry.refs.get(ref)?.deref() !== element) {
        ref = 'b' + epoch + '-' + (++registry.next);
        registry.nodes.set(element, ref);
        registry.refs.set(ref, new WeakRef(element));
        registry.collected.register(element, ref, element);
      }
      element.setAttribute(marker, ref);
      const rect = element.getBoundingClientRect();
      const elementRole = role(element);
      const editable = element.matches('input,textarea,select,[contenteditable="true"],[role="textbox"]');
      elements.push({
        ref, role: elementRole, name: name(element), input_type: inputType(element),
        text: compact(element.innerText).slice(0, 2000),
        value: (inputType(element) !== 'password' && 'value' in element ? String(element.value || '') : '').slice(0, 2000),
        disabled: !!element.disabled || element.getAttribute('aria-disabled') === 'true',
        bbox: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
      });
    }
    return {
      title: String(document.title || ''), url: String(location.href || ''),
      text: String(document.body?.innerText || '').slice(0, maxChars), elements,
    };
  })()`;
}

function elementScript(ref: string, body: string): string {
  return `(() => {
    const registry = ${referenceRegistryScript(Number(ref.slice(1, ref.indexOf("-"))), false)};
    const element = registry?.refs.get('${ref}')?.deref();
    if (!element?.isConnected || element.ownerDocument !== document || registry.nodes.get(element) !== '${ref}') {
      registry?.refs.delete('${ref}');
      if (element) { registry.nodes.delete(element); registry.collected.unregister(element); }
      return {__variant1StaleElement: true};
    }
    ${body}
  })()`;
}

function normalizedUrl(value: unknown): string {
  return allowedBrowserUrl(value);
}

export async function runWorkbenchBrowserCommand(command: Record<string, unknown>, options: {signal?: AbortSignal} = {}): Promise<Record<string, unknown>> {
  checkBrowserRequest(options.signal);
  if (isDownloadCommand(String(command.action))) return runBrowserDownloadCommand(command, options.signal);
  const handle = selectedHandle(command);
  // Commands share a guest queue. A later read cannot replace references while
  // an earlier click is locating its target.
  const run = handle.tail.catch(() => undefined).then(() => executeCommand(handle, command, options.signal));
  handle.tail = run;
  return run;
}

async function executeCommand(handle: BrowserHandle, command: Record<string, unknown>, signal?: AbortSignal): Promise<Record<string, unknown>> {
  const documentGeneration = handle.document;
  const action = String(command.action || "state");
  const keyboard = action === "keys" ? browserKeyInput(command.keys) : null;
  const requiresDocument = !isBrowserRecoveryAction(action);
  const valid = (sameDocument = true) => {
    checkBrowserRequest(signal);
    if (handles.get(handle.id) !== handle || !handle.webview.isConnected) throw new BrowserCommandError("GUEST_REPLACED", "Browser guest moved or closed; read the current tab again", "guest");
    if (sameDocument && documentGeneration !== handle.document) throw new BrowserCommandError("DOCUMENT_CHANGED", "Browser document changed during the request; read the page again", "document");
    if (requiresDocument && !handle.ready) throw new BrowserCommandError("GUEST_NOT_READY", handle.failure || "Browser document is not ready; navigate or reload this tab to recover", "ready");
  };
  valid();
  if (["navigate", "open", "reload", "back", "forward", "click", "keys", "evaluate"].includes(action) && window.variant1Deck?.bindWorkbenchBrowser) {
    const guestId = handle.webview.getWebContentsId?.();
    const bound = guestId && await window.variant1Deck.bindWorkbenchBrowser(handle.id, guestId, String(command.operation_id || ""));
    valid();
    if (!bound || !bound.ok) throw new BrowserCommandError("GUEST_REPLACED", "Could not bind this browser operation to its guest", "guest");
  }
  const execute = async (code: string, gesture = false) => {
    valid();
    if (!handle.webview.executeJavaScript) throw new BrowserCommandError("GUEST_NOT_READY", "Browser scripting is unavailable");
    const value = await browserDeadline(handle.webview.executeJavaScript(code, gesture), 15000, "script", signal);
    valid(); return value;
  };
  const executeElement = async (ref: string, body: string) => {
    const value = await execute(elementScript(ref, body), true);
    // Electron can erase guest exception messages. Return a serializable stale
    // receipt from our resolver and translate it before any native input.
    if (value && typeof value === "object" && "__variant1StaleElement" in value) {
      throw new BrowserCommandError("STALE_ELEMENT_REFERENCE", "Element reference is stale; read the page again", "element");
    }
    return value;
  };
  const webview = handle.webview;
  if(webview.flushLayout && action!=="state" && action!=="tabs"){
    await browserDeadline(webview.flushLayout(),2000,"viewport",signal);valid();
  }
  const current = () => ({...state(handle), tab_id: handle.id});
  if (action === "set_visible") return {ok: true, state: current()};
  if (action === "set_bounds" || action === "set_viewport") {
    let requested;
    try { requested = parseBrowserViewport(command); }
    catch (error) { throw new BrowserCommandError("INVALID_VIEWPORT", String((error as Error).message), "viewport"); }
    if (webview.ownerDocument !== document) {
      const group = webview.closest<HTMLElement>("[data-group-id]")?.dataset.groupId;
      const resized = group && await window.variant1Deck?.controlNativeWindow?.(`pane:${group}`, "resize", browserWindowSize(requested || DEFAULT_BROWSER_VIEWPORT));
      valid();
      if (!resized || !resized.ok) throw new BrowserCommandError("VIEWPORT_NOT_APPLIED", "The detached browser window could not be resized", "viewport");
    }
    setBrowserViewport(handle.id, requested);
    applyBrowserViewport(webview, requested);
    const owner = webview.ownerDocument.defaultView || window;
    await browserDeadline(new Promise<void>(resolve => owner.requestAnimationFrame(() => owner.requestAnimationFrame(() => resolve()))), 2000, "viewport", signal);
    if(webview.flushLayout)await browserDeadline(webview.flushLayout(),2000,"viewport",signal);
    valid();
    const viewport = measureBrowserViewport(webview);
    if (requested && (viewport.width !== requested.width || viewport.height !== requested.height)) {
      throw new BrowserCommandError("VIEWPORT_NOT_APPLIED", "The browser could not apply the requested viewport; inspect its current size", "viewport");
    }
    return {ok: true, viewport, state: current()};
  }
  if (action === "state" || action === "tabs") return {ok: true, state: current(), tabs: workbenchBrowserTargets(typeof command.owner_chat_id === "string" ? command.owner_chat_id : undefined)};
  if (action === "navigate" || action === "open") {
    const url = normalizedUrl(command.url);
    if (!webview.loadURL) throw new Error("Browser tab is not ready");
    const started = Date.now();
    try { await browserDeadline(webview.loadURL(url), 20000, "navigation", signal); }
    catch (error) {
      const download = await findStartedDownload(handle.id, url, started, signal);
      valid(false);
      if (download) return {ok:true,navigated:false,download_started:true,downloads:[download],state:current()};
      throw error;
    }
    if (!await waitForWorkbenchBrowser(handle.id, 5000, signal)) throw new BrowserCommandError("GUEST_NOT_READY", "Navigation did not reach dom-ready", "navigation");
    valid(false);
    return {ok: true, navigated: true, state: current()};
  }
  if (action === "back") {
    const navigated = !!webview.canGoBack?.();
    if (navigated) await webview.goBack?.();
    return {ok: true, navigated, state: current()};
  }
  if (action === "forward") {
    const navigated = !!webview.canGoForward?.();
    if (navigated) await webview.goForward?.();
    return {ok: true, navigated, state: current()};
  }
  if (action === "reload") {
    const download = downloadsForTab(handle.id).find(row => row.started_at >= handle.navigationStartedAt
      && (row.url === handle.navigationUrl || row.url_chain.includes(handle.navigationUrl)));
    if (download && handle.committedUrl && webview.loadURL) {
      await browserDeadline(webview.loadURL(handle.committedUrl), 20000, "navigation", signal);
      if (!await waitForWorkbenchBrowser(handle.id, 5000, signal)) throw new BrowserCommandError("GUEST_NOT_READY", "Reload did not reach dom-ready", "navigation");
      valid(false);return {ok:true,navigated:true,state:current()};
    }
    await webview.reload?.();
    return {ok: true, navigated: true, state: current()};
  }
  if (action === "read") {
    if (!webview.executeJavaScript) throw new Error("Browser tab is not ready");
    if (!handle.epoch) handle.epoch = ++referenceEpoch;
    const result = await execute(pageSnapshotScript(
      handle.epoch,
      Number(command.max_elements ?? 1000),
      Number(command.max_chars || 200000),
    ));
    return {ok: true, ...(result as Record<string, unknown>), state: current()};
  }
  if (action === "html") {
    const html = await execute("document.documentElement?.outerHTML || ''");
    return {ok: true, html: String(html || ""), state: current()};
  }
  if (action === "screenshot") {
    // Revealing a retained tab schedules its automatic layout in the next
    // frame. Settle that layout before fixing the capture's size provenance.
    applyBrowserViewport(webview, getPreviewState().tabs.find(tab => tab.id === handle.id)?.viewport);
    const owner = webview.ownerDocument.defaultView || window;
    let settling = true;
    const settle = async () => {
      let previous = measureBrowserViewport(webview), frames = 0;
      while (frames < 2) {
        await new Promise<void>(resolve => owner.requestAnimationFrame(() => resolve()));
        if (!settling) return;
        valid();
        const next = measureBrowserViewport(webview);
        frames = next.width === previous.width && next.height === previous.height ? frames + 1 : 0;
        previous = next;
      }
    };
    try { await browserDeadline(settle(), 2000, "viewport", signal); }
    finally { settling = false; }
    if(webview.flushLayout)await browserDeadline(webview.flushLayout(),2000,"viewport",signal);
    valid();
    const rect = webview.getBoundingClientRect();
    const viewport = measureBrowserViewport(webview);
    if (webview.ownerDocument.visibilityState === "hidden" || rect.width < 1 || rect.height < 1) {
      throw new BrowserCommandError("CAPTURE_NOT_VISIBLE", "Reveal the browser window before capturing it", "capture");
    }
    const capture = window.variant1Deck?.captureWorkbenchPreview;
    const guestId = webview.getWebContentsId?.();
    if (!capture || !guestId) throw new BrowserCommandError("CAPTURE_UNAVAILABLE", "Native browser capture is unavailable", "capture");
    let result;
    try { result = await browserDeadline(capture(guestId), 8000, "capture", signal); }
    catch (error) {
      if (error instanceof BrowserCommandError) throw error;
      throw new BrowserCommandError("CAPTURE_FAILED", `Browser capture failed: ${error instanceof Error ? error.message : String(error)}`, "capture");
    }
    valid();
    const finalViewport = measureBrowserViewport(webview);
    if (viewport.width !== finalViewport.width || viewport.height !== finalViewport.height) {
      throw new BrowserCommandError("VIEWPORT_CHANGED", "The browser resized during capture; inspect its current viewport before another capture", "capture");
    }
    if (!result.ok || !result.image) throw new BrowserCommandError(result.error?.startsWith("browser_capture_clipped") ? "VIEWPORT_CLIPPED" : "CAPTURE_FAILED", `Browser capture failed: ${result.error || "empty image"}`, "capture");
    return {ok: true, image: result.image, image_width: result.image_width, image_height: result.image_height,
      viewport, state: current()};
  }
  if (action === "click") {
    const ref = targetRef(command.target || command.backend_ref, handle.epoch);
    let position: {x: number; y: number} | null = null;
    if (command.position != null) {
      const value = command.position as {x?: unknown; y?: unknown};
      if (typeof value !== "object" || Array.isArray(value)
        || typeof value.x !== "number" || typeof value.y !== "number"
        || !Number.isFinite(value.x) || !Number.isFinite(value.y)) {
        throw new BrowserCommandError("INVALID_CLICK_POSITION", "Click position must contain finite numeric x and y", "click");
      }
      position = {x: value.x, y: value.y};
    }
    const zoom = webview.getZoomFactor?.() ?? 1;
    const point = await executeElement(ref, `
      element.scrollIntoView({block:'center', inline:'center'});
      element.focus?.();
      const rect = element.getBoundingClientRect();
      const position = ${JSON.stringify(position)};
      // Playwright offsets start at the padding box, after the border; offsets
      // are CSS pixels, not fractions of the element or screenshot pixels.
      const style = getComputedStyle(element);
      const geometry = {width: rect.width, height: rect.height, viewportWidth: innerWidth, viewportHeight: innerHeight};
      if (position) return {...geometry,
        x: rect.left + (parseFloat(style.borderLeftWidth) || 0) + position.x,
        y: rect.top + (parseFloat(style.borderTopWidth) || 0) + position.y};
      // An inline link can wrap into disjoint rectangles. The union center can
      // be blank space. Inspect at most 64 fragment centers; never retry input.
      const fragments = element.getClientRects();
      let visible = false;
      for (let i = 0; i < Math.min(fragments.length, 64); i++) {
        const fragment = fragments[i];
        const left = Math.max(0, fragment.left), top = Math.max(0, fragment.top);
        const right = Math.min(innerWidth, fragment.right), bottom = Math.min(innerHeight, fragment.bottom);
        if (![left, top, right, bottom].every(Number.isFinite) || right <= left || bottom <= top) continue;
        // Test the same rounded CSS point that native widget input will use.
        const x = Math.round((left + right) / 2 * ${zoom}) / ${zoom};
        const y = Math.round((top + bottom) / 2 * ${zoom}) / ${zoom};
        if (x < left || x >= right || y < top || y >= bottom) continue;
        visible = true;
        const hit = ${command.force === true} ? null : document.elementFromPoint(x, y);
        if (${command.force === true} || hit === element || element.contains(hit)) return {...geometry, x, y};
      }
      return {...geometry, error: rect.width <= 0 || rect.height <= 0 ? 'INVALID_CLICK_GEOMETRY'
        : visible ? 'CLICK_TARGET_BLOCKED' : 'CLICK_OUTSIDE_VIEWPORT'};
    `);
    const location = point as {x: number; y: number; width: number; height: number; viewportWidth: number; viewportHeight: number; error?: string} | null;
    if (location?.error) {
      throw new BrowserCommandError(location.error, location.error === "CLICK_TARGET_BLOCKED"
        ? "No inspected visible element fragment receives the click; uncover the target or inspect again (force skips the hit check)"
        : "Element has no usable visible click point; inspect or scroll before retrying", "click");
    }
    if (!location || ![location.x, location.y, location.width, location.height, location.viewportWidth, location.viewportHeight, zoom].every(Number.isFinite)
      || location.width <= 0 || location.height <= 0 || zoom <= 0) {
      throw new BrowserCommandError("INVALID_CLICK_GEOMETRY", "Element has no usable click geometry", "click");
    }
    if ((webview.getZoomFactor?.() ?? 1) !== zoom) throw new BrowserCommandError("VIEWPORT_CHANGED", "Browser zoom changed while locating the click; inspect again", "click");
    // Electron input uses widget coordinates; page zoom converts CSS pixels to
    // widget DIP. OS devicePixelRatio must not be applied a second time.
    const x = Math.round(location.x * zoom), y = Math.round(location.y * zoom);
    if (location.x < 0 || location.y < 0 || x < 0 || y < 0
      || x >= location.viewportWidth * zoom || y >= location.viewportHeight * zoom) {
      throw new BrowserCommandError("CLICK_OUTSIDE_VIEWPORT", "Requested click point is outside the browser viewport; inspect or scroll before retrying", "click");
    }
    const button = String(command.button || "left");
    const clickCount = Math.max(1, Number(command.click_count || command.count || 1));
    await webview.sendInputEvent?.({type: "mouseMove", x, y});
    await webview.sendInputEvent?.({type: "mouseDown", x, y, button, clickCount});
    await webview.sendInputEvent?.({type: "mouseUp", x, y, button, clickCount});
    return {ok: true, message: `clicked [${ref}]`, state: current()};
  }
  if (action === "hover") {
    const ref = targetRef(command.target || command.backend_ref, handle.epoch);
    const point = await executeElement(ref, `
      element.scrollIntoView({block:'center', inline:'center'});
      const rect = element.getBoundingClientRect();
      return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
    `);
    const location = point && typeof point === "object" ? point as {x?: number; y?: number} : {};
    await webview.sendInputEvent?.({type: "mouseMove", x: Math.round(Number(location.x || 0)), y: Math.round(Number(location.y || 0))});
    return {ok: true, message: `hovered [${ref}]`, state: current()};
  }
  if (action === "fill") {
    const ref = targetRef(command.target || command.backend_ref, handle.epoch);
    const text = JSON.stringify(String(command.text || ""));
    await executeElement(ref, `
      element.focus?.();
      const value = ${text};
      const descriptor = Object.getOwnPropertyDescriptor(
        element.constructor?.['proto' + 'type'], 'value'
      );
      if (descriptor?.set) descriptor.set.call(element, value); else element.value = value;
      element.dispatchEvent(new Event('input', {bubbles:true}));
      element.dispatchEvent(new Event('change', {bubbles:true}));
      return true;
    `);
    return {ok: true, message: `filled [${ref}]`, state: current()};
  }
  if (action === "select") {
    const ref = targetRef(command.target || command.backend_ref, handle.epoch);
    const values = Array.isArray(command.values) ? command.values.map(String) : [String(command.value || "")];
    await executeElement(ref, `
      const values = ${JSON.stringify(values)};
      if (!(element instanceof HTMLSelectElement)) throw new Error('element is not a select');
      for (const option of element.options) option.selected = values.includes(option.value) || values.includes(option.text);
      element.dispatchEvent(new Event('input', {bubbles:true}));
      element.dispatchEvent(new Event('change', {bubbles:true}));
      return [...element.selectedOptions].map(option => option.value);
    `);
    return {ok: true, message: `selected [${ref}]`, state: current()};
  }
  if (action === "keys") {
    const input = keyboard!;
    if (command.target || command.backend_ref) {
      const ref = targetRef(command.target || command.backend_ref, handle.epoch);
      await executeElement(ref, "element.focus?.(); return true;");
    }
    await webview.sendInputEvent?.({type: "keyDown", ...input});
    if (input.keyCode.length === 1 && !input.modifiers.some(value => ["control", "meta", "alt"].includes(value))) {
      await webview.sendInputEvent?.({type: "char", ...input});
    }
    await webview.sendInputEvent?.({type: "keyUp", ...input});
    return {ok: true, message: `sent keys ${input.keyCode}`, state: current()};
  }
  if (action === "evaluate") {
    const expression = String(command.expression || "").trim();
    if (!expression) throw new Error("Browser evaluate needs an expression");
    let value: unknown;
    try {
      value = await execute(expression, true);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (/could not be cloned|could not be serialized/i.test(message)) {
        throw new Error(`${message}. Browser evaluate must return serializable data. Invoke function expressions, e.g. (() => document.title)(), and project DOM nodes to plain data. Page effects may already have occurred; inspect before retrying.`);
      }
      throw error;
    }
    let projected: unknown;
    try { projected = value === undefined ? null : JSON.parse(JSON.stringify(value)); }
    catch { projected = String(value); }
    return {ok: true, value: projected, state: current()};
  }
  if (action === "drain_downloads") return {ok: true, downloads: [], state: current()};
  throw new Error(`Unknown browser action: ${action}`);
}
