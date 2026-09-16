'use strict';
const {BrowserWindow, WebContentsView, View, ipcMain} = require('electron');
const {isAllowedGuestUrl} = require('./electron-security');

/** Retain each tab's WebContents while only its native presentation moves. */
function createBrowserViewManager({getDeckWindow, getNativeWindow, isTrustedIpcSender, hardenGuestContents, log = () => {}}) {
  const tabs = new Map(), owners = new Set(), hosts = new Set();
  let generation = 0;
  const key = (owner, tabId) => `${owner.id}:${tabId}`;
  const alive = value => value && !value.isDestroyed();
  function state(row) {
    const wc = row.view.webContents;
    return {tabId:row.tabId, guestId:wc.id, generation:row.generation, document:row.document,
      attachmentId:row.attachmentId, url:wc.getURL(), title:wc.getTitle(), loading:wc.isLoading(), ready:row.ready,
      canGoBack:wc.navigationHistory.canGoBack(), canGoForward:wc.navigationHistory.canGoForward(),
      zoomFactor:wc.getZoomFactor(), devToolsOpened:wc.isDevToolsOpened(), visible:row.visible,
      viewport:row.viewport, bounds:row.bounds};
  }
  function publish(row, event, details = {}) {
    if (!alive(row.owner) || !alive(row.view.webContents)) return;
    try { row.owner.send('workbench:browser:event', {tabId:row.tabId, guestId:row.view.webContents.id, event, details, state:state(row)}); }
    catch (error) { log('[browser-view] event: ' + error.message); }
  }
  function park(row) {
    row.clip.setVisible(false); row.visible = false;
    if (alive(row.host)) row.host.contentView.removeChildView(row.clip);
    row.host = null;
  }
  function destroy(row) {
    tabs.delete(key(row.owner, row.tabId));
    park(row);
    if (alive(row.view.webContents)) row.view.webContents.close({waitForBeforeUnload:false});
  }
  function watchOwner(owner) {
    if (owners.has(owner)) return;
    owners.add(owner);
    const clear = () => { for (const row of [...tabs.values()]) if (row.owner === owner) destroy(row); };
    owner.once('destroyed', () => { clear(); owners.delete(owner); });
    owner.on('render-process-gone', clear);
    owner.on('did-start-navigation', (_event, _url, inPlace, mainFrame) => { if (mainFrame && !inPlace) clear(); });
  }
  function watchHost(host) {
    if (hosts.has(host)) return;
    hosts.add(host);
    // A closed popout does not own the guest lifetime. Remove native children
    // before destruction; the renderer decides whether to dock or close tabs.
    host.on('close', () => { for (const row of tabs.values()) if (row.host === host) park(row); });
    host.once('closed', () => { hosts.delete(host); for (const row of tabs.values()) if (row.host === host) park(row); });
  }
  function create(owner, tabId) {
    const view = new WebContentsView({webPreferences:{partition:'persist:variant1-preview',
      contextIsolation:true, nodeIntegration:false, sandbox:true, webSecurity:true,
      allowRunningInsecureContent:false, backgroundThrottling:false}});
    const clip = new View();
    clip.addChildView(view); clip.setVisible(false);
    const row = {owner, tabId, view, clip, host:null, attachmentId:'', generation:++generation,
      document:0, ready:false, visible:false, viewport:{width:800,height:480}, bounds:{x:0,y:0,width:0,height:0}};
    tabs.set(key(owner, tabId), row); watchOwner(owner);
    const wc = view.webContents;
    hardenGuestContents(wc);
    wc.setWindowOpenHandler(({url}) => { if (isAllowedGuestUrl(url)) publish(row, 'new-window', {url}); return {action:'deny'}; });
    wc.on('did-start-navigation', (_event, url, isInPlace, isMainFrame) => {
      if (isMainFrame && !isInPlace) { row.document++; row.ready = false; }
      publish(row, 'did-start-navigation', {url,isInPlace,isMainFrame});
    });
    wc.on('dom-ready', () => { row.ready = true; publish(row, 'dom-ready'); });
    for (const name of ['did-start-loading','did-stop-loading','did-navigate','did-navigate-in-page','page-title-updated','devtools-opened','devtools-closed']) {
      wc.on(name, () => publish(row, name));
    }
    wc.on('did-fail-load', (_event,errorCode,errorDescription,validatedURL,isMainFrame) => publish(row,'did-fail-load',{errorCode,errorDescription,validatedURL,isMainFrame}));
    wc.on('render-process-gone', (_event, details) => { row.ready = false; publish(row,'render-process-gone',details); });
    wc.on('context-menu', (_event, params) => publish(row,'context-menu',{params:{x:params.x,y:params.y,linkURL:params.linkURL,selectionText:params.selectionText}}));
    wc.on('console-message', event => {
      publish(row,'console-message',{level:event.level,message:event.message,line:event.lineNumber,sourceId:event.sourceId});
    });
    wc.once('destroyed', () => { if (tabs.get(key(owner, tabId)) === row) { tabs.delete(key(owner,tabId)); park(row); } });
    return row;
  }
  function geometry(command) {
    const rect = command.bounds, viewport = command.viewport;
    if (!rect || !['x','y','width','height'].every(k => Number.isFinite(rect[k]))
      || rect.width < 0 || rect.height < 0 || Object.values(rect).some(n => Math.abs(n) > 16384)) throw new Error('invalid_browser_bounds');
    if (!viewport || !Number.isFinite(viewport.width) || !Number.isFinite(viewport.height)
      || viewport.width < 1 || viewport.width > 3840 || viewport.height < 1 || viewport.height > 2160) throw new Error('invalid_browser_viewport');
    const scroll = command.scroll || {x:0,y:0};
    if (![scroll.x,scroll.y].every(n => Number.isFinite(n) && n >= 0 && n <= 16384)) throw new Error('invalid_browser_scroll');
    return {bounds:Object.fromEntries(['x','y','width','height'].map(k=>[k,Math.round(rect[k])])),
      viewport:{width:Math.round(viewport.width),height:Math.round(viewport.height)}, scroll};
  }
  function layout(row, dimensions, visible) {
    const {bounds,viewport,scroll} = dimensions;
    const size = row.host.getContentBounds();
    const x = Math.max(0,bounds.x), y = Math.max(0,bounds.y);
    const clipped = {x,y,width:Math.max(0,Math.min(size.width,bounds.x+bounds.width)-x),
      height:Math.max(0,Math.min(size.height,bounds.y+bounds.height)-y)};
    const guest = {x:-Math.round(scroll.x)+Math.min(0,bounds.x),y:-Math.round(scroll.y)+Math.min(0,bounds.y),...viewport};
    if (JSON.stringify(row.clip.getBounds()) !== JSON.stringify(clipped)) row.clip.setBounds(clipped);
    if (JSON.stringify(row.view.getBounds()) !== JSON.stringify(guest)) row.view.setBounds(guest);
    row.bounds = bounds; row.viewport = viewport;
    row.visible = visible !== false && clipped.width > 0 && clipped.height > 0;
    row.clip.setVisible(row.visible);
  }
  async function call(row, method, args) {
    const wc = row.view.webContents;
    if (!Array.isArray(args)) throw new Error('invalid_browser_arguments');
    if (method === 'loadURL') {
      if (!isAllowedGuestUrl(args[0])) throw new Error('invalid_browser_url');
      return wc.loadURL(args[0]);
    }
    if (method === 'executeJavaScript') {
      if (typeof args[0] !== 'string' || args[0].length > 4*1024*1024) throw new Error('invalid_browser_script');
      return wc.executeJavaScript(args[0], args[1] === true);
    }
    if (method === 'goBack' || method === 'goForward') return wc.navigationHistory[method]();
    const methods = new Set(['reload','reloadIgnoringCache','stop','sendInputEvent','focus','openDevTools','closeDevTools',
      'getZoomFactor','setZoomFactor','inspectElement','findInPage','stopFindInPage','insertText','copy','cut','paste','selectAll']);
    if (!methods.has(method)) throw new Error('unsupported_browser_method');
    const value = await wc[method](...args);
    if (method === 'setZoomFactor') publish(row,'zoom-changed');
    return value;
  }
  async function command(event, input) {
    try {
      if (!isTrustedIpcSender(event,getDeckWindow()) || typeof input?.tabId !== 'string' || !input.tabId || input.tabId.length > 512) throw new Error('untrusted_browser_request');
      const owner = event.sender;
      let row = tabs.get(key(owner,input.tabId));
      if (input.action === 'attach') {
        if (typeof input.attachmentId !== 'string' || !input.attachmentId || input.attachmentId.length > 256) throw new Error('invalid_browser_attachment');
        const host = input.windowId ? getNativeWindow(input.windowId,owner) : BrowserWindow.fromWebContents(owner);
        if (!alive(host)) throw new Error('browser_host_unavailable');
        const dimensions = geometry(input);
        if (!row && !isAllowedGuestUrl(input.url)) throw new Error('invalid_browser_url');
        const created = !row;
        if (!row) row = create(owner,input.tabId);
        if (row.host !== host) { park(row); host.contentView.addChildView(row.clip); row.host = host; watchHost(host); }
        row.attachmentId = input.attachmentId;
        layout(row,dimensions,input.visible);
        if (created) void row.view.webContents.loadURL(input.url).catch(error=>log('[browser-view] initial navigation: '+error.message));
        return {ok:true,created,state:state(row)};
      }
      if (!row || !alive(row.view.webContents)) throw new Error('browser_tab_not_found');
      if (input.action === 'layout' || input.action === 'detach') {
        if (input.attachmentId !== row.attachmentId) return {ok:false,error:'stale_browser_attachment'};
        if (input.action === 'detach') { park(row); row.attachmentId = ''; }
        else { if (!alive(row.host)) throw new Error('browser_host_unavailable'); layout(row,geometry(input),input.visible); }
        return {ok:true,state:state(row)};
      }
      if (input.action === 'destroy') { destroy(row); return {ok:true}; }
      if (input.action === 'state') return {ok:true,state:state(row)};
      if (input.action === 'call') {
        const value = await call(row,input.method,input.args || []);
        return {ok:true,value:value === undefined ? null : value,state:alive(row.view.webContents) ? state(row) : null};
      }
      throw new Error('unknown_browser_action');
    } catch (error) { return {ok:false,error:String(error.message || error)}; }
  }
  ipcMain.handle('workbench:browser:view',command);
  return {command, getGuest(id) {
    const row = [...tabs.values()].find(row=>row.view.webContents.id === id && alive(row.view.webContents));
    return row ? {contents:row.view.webContents, owner:row.owner, host:row.host, tabId:row.tabId,
      attachmentId:row.attachmentId, visible:row.visible} : null;
  }, dispose() { for (const row of [...tabs.values()]) destroy(row); ipcMain.removeHandler('workbench:browser:view'); }};
}
module.exports = {createBrowserViewManager};
