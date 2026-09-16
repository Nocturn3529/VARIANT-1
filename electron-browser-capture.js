'use strict';
const {ipcMain, webContents} = require('electron');

/** Same browser-host RPC; encode the native image in the main process. */
function registerBrowserCapture({getDeckWindow, isTrustedIpcSender, isNativeHost, getRetainedGuest, log = () => {}}) {
  ipcMain.handle('workbench:browser:capture', async (event, id) => {
    const deck = getDeckWindow();
    if (!isTrustedIpcSender(event, deck) || !Number.isSafeInteger(id)) return {ok:false, error:'untrusted_capture'};
    const guest = webContents.fromId(id);
    const retained = getRetainedGuest?.(id);
    const owner = retained?.owner || guest?.hostWebContents;
    if (!guest || guest.isDestroyed() || !owner || (retained
      ? retained.contents !== guest || retained.owner !== event.sender || !retained.visible
      : guest.getType() !== 'webview' || (owner !== deck?.webContents && !isNativeHost(owner)))) return {ok:false, error:'browser_guest_not_owned'};
    const sameAttachment = () => {
      if (!retained) return guest.hostWebContents === owner;
      const current = getRetainedGuest?.(id);
      return current?.owner === owner && current.attachmentId === retained.attachmentId
        && current.host === retained.host && current.visible;
    };
    let changed = false;
    let abandoned = false;
    let capturedViewport;
    const navigation = (_event, _url, inPlace, mainFrame) => { if (mainFrame && !inPlace) changed = true; };
    guest.on('did-start-navigation', navigation);
    let timer;
    try {
      const image = await Promise.race([
        (async () => {
          // dom-ready does not promise that a newly attached/resized guest has
          // submitted a compositor frame. Wait in that guest before capture.
          const viewport = await guest.executeJavaScript('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve({width: innerWidth, height: innerHeight, scale: devicePixelRatio}))))');
          if (abandoned) throw new Error('browser_capture_cancelled');
          if (changed || guest.isDestroyed() || !sameAttachment()) throw new Error('browser_document_changed_during_capture');
          if (!viewport || !Number.isInteger(viewport.width) || !Number.isInteger(viewport.height)
            || viewport.width < 1 || viewport.width > 3840 || viewport.height < 1 || viewport.height > 2160) throw new Error('browser_capture_invalid_viewport');
          // The default rectangle is the visible portion and can be cropped
          // at a detached window's screen edge. Capture the bounded guest view.
          capturedViewport = viewport;
          return guest.capturePage({x:0,y:0,width:viewport.width,height:viewport.height});
        })(),
        new Promise((_, reject) => { timer = setTimeout(() => { abandoned = true; reject(new Error('browser_capture_timeout')); }, 6000); }),
      ]);
      if (changed || guest.isDestroyed() || !sameAttachment()) return {ok:false, error:'browser_document_changed_during_capture'};
      if (image.isEmpty()) return {ok:false, error:'browser_capture_empty'};
      const png = image.toPNG();
      const width = png.readUInt32BE(16), height = png.readUInt32BE(20), scale = Number(capturedViewport.scale) || 1;
      if (!((width === capturedViewport.width && height === capturedViewport.height)
        || (width === Math.round(capturedViewport.width * scale) && height === Math.round(capturedViewport.height * scale)))) {
        return {ok:false,error:'browser_capture_clipped: move or enlarge the browser panel, or request a smaller viewport'};
      }
      return {ok:true, image:png.toString('base64'), image_width:width, image_height:height};
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      log(`[browser-capture] guest=${id} error=${message}`);
      return {ok:false, error:message};
    } finally { abandoned = true; clearTimeout(timer); if (!guest.isDestroyed()) guest.removeListener('did-start-navigation', navigation); }
  });
}
module.exports = {registerBrowserCapture};
