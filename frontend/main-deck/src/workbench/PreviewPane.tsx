import {attachRetainedBrowser,retainedBrowser} from "./retainedBrowserView";
import {retainChatDraft} from "../chat/stateCore";
import {FilesPanel} from "../context/FilesPanel";
import {Icon} from "../ui/Icon";
import {useSurfaceDocument} from "../ui/SurfaceDocument";
import {detachWorkbenchPane, isWorkbenchPaneVisible, useWorkbenchState} from "./workbenchStore";
import {useAppState} from "../state/appStore";
import {applyBrowserViewport, measureBrowserViewport} from "./browserViewport";
import {findStartedDownload, runBrowserDownloadCommand, useBrowserDownloads} from "./browserDownloads";
import {createElement, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode} from "react";
import type {RuntimeApi} from "../types";
import {PdfPreview} from "./PdfPreview";
import {watchPath} from "./watchPath";
import {
  beginFileEdit, cancelFileEdit, loadFileDocument, saveFileDocument,
  updateFileDraft, useFileDocument,
} from "./fileDocumentStore";
import runtimeLib, {type InlineToken, type MdBlock} from "../chat/runtimeLib";
import {
  activateWorkbenchBrowser,
  registerWorkbenchBrowser,
  runWorkbenchBrowserCommand,
  type WorkbenchWebview,
} from "./browserBridge";
import {
  allowedBrowserUrl,
  getPreviewState,
  noteBrowserPage,
  openBrowser,
  requestBrowserNavigation,
  openOutputPreview,
  setPreviewDirty,
  usePreviewState,
  type BrowserPageState,
  type PreviewTab,
} from "./previewStore";

type GuestEvent = Event & {
  url?: string;
  errorDescription?: string;
  errorCode?: number;
  isMainFrame?: boolean;
  level?: number;
  message?: string;
  line?: number;
  sourceId?: string;
  x?: number;
  y?: number;
  linkURL?: string;
  selectionText?: string;
  params?: {x?: number; y?: number; linkURL?: string; selectionText?: string; isEditable?: boolean};
};

function readGuest<T>(read: (() => T) | undefined, fallback: T): T {
  if (!read) return fallback;
  try { return read(); } catch { return fallback; }
}

function browserPage(webview: WorkbenchWebview | null, fallback: string, loading: boolean): BrowserPageState {
  return {
    // Electron may emit an early loading event before the guest's dom-ready
    // boundary. Its synchronous accessors throw in that short interval.
    title: readGuest(webview?.getTitle?.bind(webview), ""),
    url: readGuest(webview?.getURL?.bind(webview), fallback),
    canGoBack: readGuest(webview?.canGoBack?.bind(webview), false),
    canGoForward: readGuest(webview?.canGoForward?.bind(webview), false),
    loading,
  };
}

function normalizeAddress(value: string): string {
  return allowedBrowserUrl(value);
}

function BrowserPreview({tab, api}: {tab: PreviewTab; api: RuntimeApi | null}) {
  const ownerDocument = useSurfaceDocument();
  const ownerWindow = ownerDocument.defaultView || window;
  const preview = usePreviewState();
  const downloads = useBrowserDownloads().filter(row => row.tab_id === tab.id);
  const workbench = useWorkbenchState();
  const app = useAppState();
  const surfaceRef = useRef<HTMLElement | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [viewport, setViewport] = useState<ReturnType<typeof measureBrowserViewport> | null>(null);
  const hostRef = useRef<HTMLDivElement | null>(null);
  const webviewRef = useRef<WorkbenchWebview | null>(null);
  const transferUrl = useRef(preview.pages[tab.id]?.url || tab.target.url || "about:blank");
  const appliedNavigation = useRef(tab.navigation?.revision || 0);
  const [address, setAddress] = useState(preview.pages[tab.id]?.url || tab.target.url);
  const [loading, setLoading] = useState(false);
  const loadingRef = useRef(false);
  const [failure, setFailure] = useState("");
  const [captureBusy, setCaptureBusy] = useState(false);
  const [viewportBusy, setViewportBusy] = useState(false);
  const [captureError, setCaptureError] = useState("");
  const [consoleOpen, setConsoleOpen] = useState(false);
  const [findOpen, setFindOpen] = useState(false);
  const [findQuery, setFindQuery] = useState("");
  const [consoleLines, setConsoleLines] = useState<Array<{level: number; text: string}>>([]);
  const [guestMenu, setGuestMenu] = useState<null | {x: number; y: number; guestX: number; guestY: number; link: string; selection: string}>(null);
  const page = preview.pages[tab.id] || {
    title: "", url: tab.target.url, canGoBack: false, canGoForward: false, loading: false,
  };

  const managed=!!api?.workbenchBrowser && !!api?.onWorkbenchBrowserEvent;
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const retained=managed && api ? retainedBrowser(tab.id,transferUrl.current,api,tab.navigation?.revision || 0) : null;
    const guest = retained?.guest || host.ownerDocument.createElement("webview") as WorkbenchWebview;
    if (tab.navigation && tab.navigation.revision !== appliedNavigation.current) transferUrl.current = tab.navigation.url;
    appliedNavigation.current = retained ? retained.navigationRevision : tab.navigation?.revision || 0;
    guest.className = "workbench-browser__guest";
    if(!retained){
      guest.setAttribute("partition", "persist:variant1-preview");
      guest.setAttribute("src", transferUrl.current);
      guest.setAttribute("webpreferences", "contextIsolation=yes,nodeIntegration=no,sandbox=yes,webSecurity=yes");
    }
    const sync = () => {
      const next = browserPage(guest, tab.target.url, loadingRef.current);
      if (guest.isConnected && next.url) transferUrl.current = next.url;
      setAddress(next.url);
      noteBrowserPage(tab.id, next);

    };
    const start = () => { loadingRef.current = true; setLoading(true); setFailure(""); sync(); };
    const stop = () => { loadingRef.current = false; setLoading(false); sync(); };
    const failed = (event: Event) => {
      const detail = event as GuestEvent;
      if (detail.isMainFrame === false || detail.errorCode === -3) return;
      loadingRef.current = false;
      setLoading(false);
      setFailure(detail.errorDescription || "The page could not be loaded.");
      sync();
    };
    const consoleMessage = (event: Event) => {
      const detail = event as GuestEvent;
      setConsoleLines(lines => [...lines, {
        level: Number(detail.level || 0),
        text: `${detail.message || ""}${detail.sourceId ? ` · ${detail.sourceId}:${detail.line || 0}` : ""}`,
      }].slice(-200));
    };
    const newWindow = (event: Event) => {
      event.preventDefault();
      const detail = event as GuestEvent;
      if (detail.url) openBrowser(detail.url, {newTab: true,ownerChatId:tab.ownerChatId});
    };
    const contextMenu = (event: Event) => {
      event.preventDefault();
      const detail = event as GuestEvent;
      const params = detail.params || detail;
      const hostRect = host.getBoundingClientRect();
      const guestX = Number(params.x || 0);
      const guestY = Number(params.y || 0);
      setGuestMenu({
        x: Math.min(ownerWindow.innerWidth - 210, Math.max(6, hostRect.left + guestX)),
        y: Math.min(ownerWindow.innerHeight - 250, Math.max(6, hostRect.top + guestY)),
        guestX,
        guestY,
        link: String(params.linkURL || ""),
        selection: String(params.selectionText || ""),
      });
    };
    guest.addEventListener("did-start-loading", start);
    guest.addEventListener("did-stop-loading", stop);
    guest.addEventListener("did-navigate", sync);
    guest.addEventListener("did-navigate-in-page", sync);
    guest.addEventListener("page-title-updated", sync);
    guest.addEventListener("did-fail-load", failed);
    guest.addEventListener("console-message", consoleMessage);
    guest.addEventListener("new-window", newWindow);
    guest.addEventListener("context-menu", contextMenu);
    host.appendChild(guest);
    const hostDocument = host.ownerDocument;
    const beforeMove = (event: Event) => {
      const mount = (event as CustomEvent<{mount: HTMLElement}>).detail?.mount;
      if (!mount?.contains(host)) return;
      transferUrl.current = readGuest(guest.getURL?.bind(guest), transferUrl.current) || transferUrl.current;
      // Electron webviews must reattach to the new embedder. Remove before
      // adoption so the old src cannot issue an extra navigation on the way.
      guest.remove();
    };
    if(!retained)hostDocument.addEventListener("variant1:surface-will-move", beforeMove);
    webviewRef.current = guest;
    const measure = () => {
      if (!host.parentElement?.clientWidth || !host.parentElement.clientHeight) return;
      applyBrowserViewport(guest, getPreviewState().tabs.find(item => item.id === tab.id)?.viewport);
      const next = measureBrowserViewport(guest);
      setViewport(previous => previous && JSON.stringify(previous) === JSON.stringify(next) ? previous : next);
    };
    let measureFrame = 0;
    const resizeObserver = new ResizeObserver(() => {
      if (!measureFrame) measureFrame = ownerWindow.requestAnimationFrame(() => { measureFrame = 0; measure(); });
    });
    if (host.parentElement) resizeObserver.observe(host.parentElement);
    measure();
    const unregister = retained ? ()=>{} : registerWorkbenchBrowser(tab.id, guest, () => browserPage(guest, tab.target.url, loadingRef.current));
    const detach=retained && api ? attachRetainedBrowser(tab.id,host,api) : ()=>{};
    activateWorkbenchBrowser(tab.id);
    return () => {
      resizeObserver.disconnect();
      if (measureFrame) ownerWindow.cancelAnimationFrame(measureFrame);
      hostDocument.removeEventListener("variant1:surface-will-move", beforeMove);
      detach();unregister();
      guest.removeEventListener("did-start-loading", start);
      guest.removeEventListener("did-stop-loading", stop);
      guest.removeEventListener("did-navigate", sync);
      guest.removeEventListener("did-navigate-in-page", sync);
      guest.removeEventListener("page-title-updated", sync);
      guest.removeEventListener("did-fail-load", failed);
      guest.removeEventListener("console-message", consoleMessage);
      guest.removeEventListener("new-window", newWindow);
      guest.removeEventListener("context-menu", contextMenu);
      guest.remove();
      webviewRef.current = null;
    };
  }, [tab.id, ownerDocument]);

  useLayoutEffect(() => {
    const guest = webviewRef.current;
    if (guest) { applyBrowserViewport(guest, tab.viewport); setViewport(measureBrowserViewport(guest)); }
  }, [tab.viewport, ownerDocument]);

  const visible = isWorkbenchPaneVisible(`preview:${tab.id}`, workbench);
  useEffect(() => { if (!visible || app.view !== "chat") setExpanded(false); }, [visible, app.view]);
  useLayoutEffect(() => {
    const surface = surfaceRef.current;
    if (!surface) return;
    if (expanded) surface.showPopover?.();
    if (!expanded) return;
    surface.querySelector<HTMLButtonElement>("[data-browser-expand]")?.focus({preventScroll: true});
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !ownerDocument.querySelector("dialog[open]")) {
        event.preventDefault(); event.stopImmediatePropagation(); setExpanded(false);
      }
    };
    ownerDocument.addEventListener("keydown", escape, true);
    return () => ownerDocument.removeEventListener("keydown", escape, true);
  }, [expanded, ownerDocument]);

  useEffect(() => {
    const intent = tab.navigation;
    const guest = webviewRef.current;
    if (!guest || !intent || intent.revision === appliedNavigation.current) return;
    let disposed = false;
    const apply = () => {
      if (disposed || intent.revision === appliedNavigation.current) return;
      // A request can arrive between mounting the element and Electron
      // attaching its guest. Keep the intent until that guest can navigate.
      if (guest.getWebContentsId && !readGuest(guest.getWebContentsId.bind(guest), 0)) return;
      try {
        const started = Date.now();
        const pending = runWorkbenchBrowserCommand({action:"navigate",tab_id:tab.id,url:intent.url});
        appliedNavigation.current = intent.revision;
        if(managed && api)retainedBrowser(tab.id,transferUrl.current,api).navigationRevision=intent.revision;
        transferUrl.current = intent.url;
        setAddress(intent.url);
        void pending?.catch(async error => {
          const download = await findStartedDownload(tab.id, intent.url, started);
          if (!disposed) setFailure(download ? "" : String(error));
        });
      } catch (error) { if (!disposed) setFailure(String(error)); }
    };
    guest.addEventListener("did-attach", apply);
    guest.addEventListener("dom-ready", apply);
    apply();
    return () => { disposed = true; guest.removeEventListener("did-attach", apply); guest.removeEventListener("dom-ready", apply); };
  }, [tab.navigation?.revision, ownerDocument]);

  useEffect(() => {
    if (!guestMenu) return;
    const close = () => setGuestMenu(null);
    ownerWindow.addEventListener("pointerdown", close, {once: true});
    return () => ownerWindow.removeEventListener("pointerdown", close);
  }, [guestMenu, ownerDocument]);

  useEffect(() => {
    if (preview.selectedId !== tab.id) return;
    activateWorkbenchBrowser(tab.id);
    webviewRef.current?.focus();
  }, [tab.id, preview.selectedId]);

  function navigate(): void {
    const url = normalizeAddress(address);
    setAddress(url);
    requestBrowserNavigation(tab.id, url);
  }

  function toggleDevTools(): void {
    const guest = webviewRef.current;
    if (!guest) return;
    if (guest.isDevToolsOpened?.()) guest.closeDevTools?.();
    else guest.openDevTools?.();
  }

  function recover(action: "back" | "forward" | "reload"): void {
    setFailure("");
    void runWorkbenchBrowserCommand({action,tab_id:tab.id}).catch(error => setFailure(error instanceof Error ? error.message : String(error)));
  }

  function popOut(): void {
    setExpanded(false);
    detachWorkbenchPane(`preview:${tab.id}`);
  }

  async function capture(): Promise<void> {
    if (captureBusy) return;
    setCaptureBusy(true); setCaptureError("");
    try {
    const result = await runWorkbenchBrowserCommand({action: "screenshot", tab_id: tab.id});
    const image = String(result.image || "");
    if (!image) return;
    openOutputPreview({
      source: `browser:${tab.id}:screenshot`,
      url: `data:image/png;base64,${image}`,
      label: `${page.title || "Browser"} screenshot`,
      mediaType: "image/png",
    });
    } catch (error) { setCaptureError(error instanceof Error ? error.message : String(error)); }
    finally { setCaptureBusy(false); }
  }

  return <section ref={surfaceRef} popover={expanded ? "manual" : undefined} className={`workbench-browser${expanded ? " is-expanded" : ""}`} onPointerDown={() => activateWorkbenchBrowser(tab.id)}>
    <form className="workbench-browser__bar" onSubmit={event => { event.preventDefault(); navigate(); }}>
      <button type="button" aria-label="Back" title="Back" disabled={!page.canGoBack} onClick={() => recover("back")}><Icon name="back"/></button>
      <button type="button" aria-label="Forward" title="Forward" disabled={!page.canGoForward} onClick={() => recover("forward")}><Icon name="forward"/></button>
      <button type="button" aria-label="Reload" title="Reload" onClick={() => recover("reload")}><Icon name="refresh"/></button>
      <input aria-label="Address" value={address} onChange={event => setAddress(event.target.value)} onFocus={event => event.currentTarget.select()}/>
      <button type="button" aria-label="Copy URL" title="Copy URL" onClick={() => void ownerWindow.navigator.clipboard.writeText(page.url || address)}><Icon name="windows"/></button>
      <button type="button" aria-label="Capture screenshot" title="Capture screenshot" disabled={captureBusy} onClick={() => void capture()}><Icon name="image"/></button>
      <button type="button" aria-label="Find in page" title="Find in page" aria-pressed={findOpen} onClick={() => setFindOpen(value => !value)}><Icon name="search"/></button>
      <button type="button" aria-label="New browser tab" title="New browser tab" onClick={() => openBrowser("about:blank", {newTab: true,ownerChatId:tab.ownerChatId})}><Icon name="plus"/></button>
      <button type="button" aria-label="Toggle console" title="Console" aria-pressed={consoleOpen} onClick={() => setConsoleOpen(value => !value)}><Icon name="terminal"/></button>
      <button type="button" aria-label="Toggle DevTools" title="DevTools" onClick={toggleDevTools}><Icon name="code"/></button>
      {ownerDocument === document ? <button type="button" aria-label="Pop out browser" title={managed ? "Open in a separate window" : "Open in a separate window (reloads the browser page)"} disabled={!api?.supportsNativeWindows} onClick={popOut}><Icon name="popout"/></button> : null}
    </form>
    <div className="workbench-browser__viewport">
      <select aria-label="Browser viewport" disabled={viewportBusy || loading} value={tab.viewport ? `${tab.viewport.width}x${tab.viewport.height}` : "auto"} onChange={event => {
        const [width, height] = event.target.value.split("x").map(Number);
        const request = event.target.value === "auto" ? {mode: "auto"} : {width, height};
        setViewportBusy(true); setCaptureError("");
        void runWorkbenchBrowserCommand({action: "set_viewport", tab_id: tab.id, ...request})
          .catch(error => setCaptureError(error instanceof Error ? error.message : String(error)))
          .finally(() => setViewportBusy(false));
      }}>
        <option value="auto">Automatic · min 800 × 480</option>
        <option value="1280x720">Desktop · 1280 × 720</option>
        <option value="1024x768">Tablet · 1024 × 768</option>
        <option value="390x844">Mobile · 390 × 844</option>
        {tab.viewport && !["1280x720", "1024x768", "390x844"].includes(`${tab.viewport.width}x${tab.viewport.height}`)
          ? <option value={`${tab.viewport.width}x${tab.viewport.height}`}>{tab.viewport.width} × {tab.viewport.height}</option> : null}
      </select>
      {viewport ? <span title="Rendered page size in CSS pixels">{viewport.width} × {viewport.height}</span> : null}
      <button type="button" data-browser-expand aria-label={expanded ? "Restore browser pane" : "Expand browser"} aria-pressed={expanded} onClick={() => setExpanded(value => !value)}>
        <Icon name={expanded ? "panels" : "maximize"}/><span>{expanded ? "Restore" : "Expand"}</span>
      </button>
      {viewport && (viewport.visible_width < viewport.width || viewport.visible_height < viewport.height) ? <small>Scroll to see the full page</small> : null}
    </div>
    {captureError ? <p role="alert" className="workbench-browser__capture-error">{captureError}</p> : null}
    {downloads.length ? <details className="workbench-browser__downloads">
      <summary>{downloads.filter(row => ["in_progress", "finalizing"].includes(row.status)).length || downloads.length} download{downloads.length === 1 ? "" : "s"}</summary>
      <ul>{downloads.slice(-8).reverse().map(row => <li key={row.download_id} data-download-id={row.download_id}>
        <div><strong>{row.suggested_filename}</strong><small>{row.error || ({in_progress:"Receiving download", finalizing:"Verifying download", completed:"Downloaded · awaiting artifact storage", stored:"Stored in browser artifacts", cancelled:"Cancelled", interrupted:"Interrupted"} as Record<string,string>)[row.status] || row.status}
          {row.bytes ? ` · ${Math.ceil(row.bytes / 1024)} KB` : ""}</small></div>
        {["in_progress", "finalizing", "interrupted"].includes(row.status) ? <button type="button" onClick={() => {
          void runBrowserDownloadCommand({action:"cancel_download",tab_id:tab.id,download_id:row.download_id})
            .catch(error => setCaptureError(error instanceof Error ? error.message : String(error)));
        }}>Cancel download</button> : null}
      </li>)}</ul>
    </details> : null}
    {findOpen ? <form className="workbench-browser__find" onSubmit={event => { event.preventDefault(); if (findQuery) webviewRef.current?.findInPage?.(findQuery, {findNext: true}); }}>
      <input autoFocus value={findQuery} placeholder="Find in page" onChange={event => { setFindQuery(event.target.value); if (event.target.value) webviewRef.current?.findInPage?.(event.target.value); else webviewRef.current?.stopFindInPage?.("clearSelection"); }}/>
      <button type="button" aria-label="Previous match" onClick={() => webviewRef.current?.findInPage?.(findQuery, {forward: false, findNext: true})}><Icon name="send"/></button>
      <button type="submit" aria-label="Next match"><Icon name="send" style={{transform: "rotate(180deg)"}}/></button>
      <button type="button" aria-label="Close find" onClick={() => { webviewRef.current?.stopFindInPage?.("clearSelection"); setFindOpen(false); }}><Icon name="close"/></button>
    </form> : null}
    <div className="workbench-browser__body">
      <div className="workbench-browser__host" ref={hostRef}/>
      {loading ? <div className="workbench-browser__loading" aria-label="Loading page"><span/></div> : null}
      {failure ? <div className="workbench-browser__failure"><strong>Page failed to load</strong><span>{failure}</span><button onClick={() => recover("reload")}>Retry</button></div> : null}
    </div>
    {consoleOpen ? <section className="workbench-browser__console">
      <header><strong>Console</strong><span>{consoleLines.length}</span><button disabled={!consoleLines.length} onClick={() => retainChatDraft(tab.ownerChatId || "",consoleLines.map(line => line.text).join("\n").slice(-12_000))}>Add to chat</button><button onClick={() => setConsoleLines([])}>Clear</button><button onClick={() => setConsoleOpen(false)}>×</button></header>
      <div>{consoleLines.map((line, index) => <pre data-level={line.level} key={`${index}:${line.text}`}>{line.text}</pre>)}</div>
    </section> : null}
    {guestMenu ? <div className="workbench-browser__menu" role="menu" style={{left: guestMenu.x, top: guestMenu.y}} onPointerDown={event => event.stopPropagation()}>
      <button role="menuitem" disabled={!page.canGoBack} onClick={() => { recover("back"); setGuestMenu(null); }}>Back</button>
      <button role="menuitem" disabled={!page.canGoForward} onClick={() => { recover("forward"); setGuestMenu(null); }}>Forward</button>
      <button role="menuitem" onClick={() => { recover("reload"); setGuestMenu(null); }}>Reload</button>
      <hr/>
      {guestMenu.link ? <button role="menuitem" onClick={() => { openBrowser(guestMenu.link, {newTab: true,ownerChatId:tab.ownerChatId}); setGuestMenu(null); }}>Open link in new tab</button> : null}
      {guestMenu.link ? <button role="menuitem" onClick={() => { void ownerWindow.navigator.clipboard.writeText(guestMenu.link); setGuestMenu(null); }}>Copy link</button> : null}
      {guestMenu.selection ? <button role="menuitem" onClick={() => { void ownerWindow.navigator.clipboard.writeText(guestMenu.selection); setGuestMenu(null); }}>Copy selection</button> : null}
      <button role="menuitem" onClick={() => { webviewRef.current?.inspectElement?.(guestMenu.guestX, guestMenu.guestY); setGuestMenu(null); }}>Inspect element</button>
    </div> : null}
  </section>;
}

function mediaKind(tab: PreviewTab): "text" | "image" | "pdf" | "html" {
  const value = `${tab.target.mediaType || ""} ${tab.target.path || tab.target.url}`.toLowerCase();
  if (/image\//.test(value) || /\.(png|jpe?g|gif|webp|bmp|svg)(?:$|\?)/.test(value)) return "image";
  if (/pdf/.test(value) || /\.pdf(?:$|\?)/.test(value)) return "pdf";
  if (/html/.test(value) || /\.html?(?:$|\?)/.test(value)) return "html";
  return "text";
}

function renderInline(tokens: InlineToken[], key: string): ReactNode[] {
  return tokens.map((token, index) => {
    const childKey = `${key}-${index}`;
    if (token.type === "text") return token.text;
    if (token.type === "code") return <code key={childKey}>{token.text}</code>;
    if (token.type === "strong" || token.type === "em") {
      return createElement(token.type, {key: childKey}, renderInline(token.children, childKey));
    }
    if (!("href" in token)) return renderInline(token.children, childKey);
    return <a key={childKey} href={token.href} onClick={event => {
      event.preventDefault();
      void window.variant1Deck?.openExternal?.(token.href);
    }}>{renderInline(token.children, childKey)}</a>;
  });
}

function renderMarkdownBlocks(blocks: MdBlock[], key = "md"): ReactNode[] {
  return blocks.map((block, index) => {
    const blockKey = `${key}-${index}`;
    if (block.type === "heading") {
      const tag = `h${Math.max(1, Math.min(6, block.level))}`;
      return createElement(tag, {key: blockKey}, renderInline(block.inline, blockKey));
    }
    if (block.type === "paragraph") return <p key={blockKey}>{block.lines.map((line, lineIndex) => <span key={`${blockKey}-${lineIndex}`}>{renderInline(line, `${blockKey}-${lineIndex}`)}{lineIndex < block.lines.length - 1 ? <br/> : null}</span>)}</p>;
    if (block.type === "blockquote") return <blockquote key={blockKey}>{renderMarkdownBlocks(block.blocks, blockKey)}</blockquote>;
    if (block.type === "list") {
      const Tag = block.ordered ? "ol" : "ul";
      return <Tag key={blockKey}>{block.items.map((item, itemIndex) => <li key={`${blockKey}-${itemIndex}`}>{renderInline(item.inline, `${blockKey}-${itemIndex}`)}{renderMarkdownBlocks(item.children, `${blockKey}-${itemIndex}-nested`)}</li>)}</Tag>;
    }
    if (block.type === "table") return <table key={blockKey}><thead><tr>{block.headers.map((cell, cellIndex) => <th key={cellIndex}>{renderInline(cell, `${blockKey}-h${cellIndex}`)}</th>)}</tr></thead><tbody>{block.rows.map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}>{renderInline(cell, `${blockKey}-${rowIndex}-${cellIndex}`)}</td>)}</tr>)}</tbody></table>;
    return <pre key={blockKey}><code>{runtimeLib.highlightCode(block.text, block.language).map((token, tokenIndex) => <span className={`token-${token.type}`} key={tokenIndex}>{token.text}</span>)}</code></pre>;
  });
}

function RichTextPreview({path, text}: {path: string; text: string}) {
  if (/\.(md|markdown)$/i.test(path)) {
    return <article className="workbench-markdown-preview">{renderMarkdownBlocks(runtimeLib.parseMarkdown(text))}</article>;
  }
  return <pre className="workbench-file-preview__source"><code>{runtimeLib.highlightCode(text, path.split(".").pop() || "").map((token, index) => <span className={`token-${token.type}`} key={index}>{token.text}</span>)}</code></pre>;
}

function FilePreview({tab, api}: {tab: PreviewTab; api: RuntimeApi | null}) {
  const path = tab.target.path || tab.target.source;
  const state = useFileDocument(tab.id);
  const {text, original, dataUrl, loaded, loading, editing, editable, saving, conflict, error} = state;
  const editorRef = useRef<HTMLTextAreaElement>(null);
  const wasEditing = useRef(editing);
  const kind = useMemo(() => mediaKind(tab), [tab.target]);
  const load = (discard = false) => loadFileDocument(tab.id, path, api?.readWorkbenchFile, discard);

  useEffect(() => { void load(); }, [tab.id, path, api]);
  useEffect(() => {
    if (editing && !wasEditing.current) editorRef.current?.focus();
    wasEditing.current = editing;
  }, [editing]);
  useEffect(() => { setPreviewDirty(tab.id, text !== original); }, [tab.id, text !== original]);
  useEffect(() => {
    const cut = Math.max(path.lastIndexOf("\\"), path.lastIndexOf("/"));
    const parent = cut > 1 ? path.slice(0, cut) : path;
    const name = path.slice(cut + 1).toLowerCase();
    return watchPath(api, parent, () => { void load(); }, {
      matches: event => !event.filename || String(event.filename).toLowerCase().endsWith(name),
    });
  }, [tab.id, path, api]);

  // Loading and save failures must leave a live editor and its buffer intact.
  if (!loaded && loading) return <div className="workbench-preview__state">Loading preview…</div>;
  if (!loaded && error) return <div className="workbench-preview__state"><strong>Preview unavailable</strong><span>{error}</span><button onClick={() => void load()}>Retry</button></div>;

  return <section className="workbench-file-preview" tabIndex={0} onKeyDown={event => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s" && editing) {
      event.preventDefault(); event.stopPropagation();
      void saveFileDocument(tab.id, path, api?.writeWorkbenchFile);
    } else if (event.key.toLowerCase() === "e" && !event.ctrlKey && !event.metaKey && kind === "text" && editable && !editing) {
      event.preventDefault(); beginFileEdit(tab.id);
    }
  }}>
    <header>
      <span title={path}>{path}</span>
      <div>
        {kind === "text" && editable && !editing ? <button onClick={() => beginFileEdit(tab.id)}>Edit</button> : null}
        {editing ? <><button disabled={saving} onClick={() => cancelFileEdit(tab.id)}>Cancel</button><button disabled={saving || !editable} onClick={() => void saveFileDocument(tab.id, path, api?.writeWorkbenchFile)}>{saving ? "Saving…" : "Save"}</button></> : null}
        <button onClick={() => void api?.revealWorkbenchPath?.(path)}>Reveal</button>
        <button disabled={saving} onClick={() => void load()}>Reload</button>
      </div>
    </header>
    {error ? <div className="workbench-tool-error" role="alert">{error}</div> : null}
    {conflict ? <div className="workbench-file-preview__conflict">
      <span>The file changed outside this editor.</span>
      <button disabled={saving} onClick={() => void load(true)}>Discard and reload</button>
      <button disabled={saving || !editable} onClick={() => void saveFileDocument(tab.id, path, api?.writeWorkbenchFile, true)}>Overwrite</button>
    </div> : null}
    {kind === "image" ? <div className="workbench-file-preview__media"><img src={dataUrl} alt={tab.target.label}/></div> : null}
    {kind === "pdf" ? <PdfPreview source={dataUrl} label={tab.target.label}/> : null}
    {kind === "html" && tab.target.renderMode === "preview" ? <iframe className="workbench-file-preview__media" title={tab.target.label} sandbox="allow-scripts" srcDoc={text}/> : null}
    {kind === "text" || (kind === "html" && tab.target.renderMode !== "preview") ? (
      editing
        ? <textarea ref={editorRef} aria-label={`Edit ${tab.target.label}`} disabled={saving} className="workbench-file-preview__editor" value={text} onChange={event => updateFileDraft(tab.id, event.target.value)} spellCheck={false}/>
        : state.kind === "binary" ? <div className="workbench-preview__state"><strong>Binary file</strong><span>A text preview is unavailable. Use Reveal to open it in another application.</span></div>
        : <RichTextPreview path={path} text={text}/>
    ) : null}
  </section>;
}

function OutputPreview({tab}: {tab: PreviewTab}) {
  const kind = mediaKind(tab);
  if (kind === "image") return <div className="workbench-file-preview__media"><img src={tab.target.url} alt={tab.target.label}/></div>;
  if (kind === "pdf") return <PdfPreview source={tab.target.url} label={tab.target.label}/>;
  if (kind === "html") return <iframe className="workbench-file-preview__media" sandbox="allow-scripts" src={tab.target.url}/>;
  return <pre className="workbench-file-preview__source"><code>{tab.target.content || tab.target.url}</code></pre>;
}

export function PreviewPane({tabId, api}: {tabId: string; api: RuntimeApi | null}) {
  const state = usePreviewState();
  const tab = state.tabs.find(item => item.id === tabId);
  if (!tab) return <div className="workbench-preview__state">Preview closed.</div>;
  if (tab.target.kind === "url") return <BrowserPreview tab={tab} api={api}/>;
  if (tab.target.kind === "directory") return <FilesPanel directory={tab.target.path} chatId={tab.ownerChatId}/>;
  if (tab.target.kind === "file") return <FilePreview tab={tab} api={api}/>;
  return <OutputPreview tab={tab}/>;
}

export function previewPaneTitle(tab: PreviewTab): string {
  if (tab.target.kind === "url") {
    const page = getPreviewState().pages[tab.id];
    if (page?.title && page.title !== page.url) return page.title;
    try { return new URL(page?.url || tab.target.url).hostname.replace(/^www\./, "") || "Browser"; }
    catch { return "Browser"; }
  }
  return tab.target.label || tab.target.path?.split(/[\\/]/).pop() || "Preview";
}
