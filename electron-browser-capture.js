'use strict';
const {ipcMain, webContents} = require('electron');

/** Same browser-host RPC; encode the native image in the main process. */
function registerBrowserCapture({getDeckWindow, isTrustedIpcSender, isNativeHost, getRetainedGuest, lendCaptureSurface, log = () => {}}) {
  ipcMain.handle('workbench:browser:capture', async (event, id) => {
    const deck = getDeckWindow();
    if (!isTrustedIpcSender(event, deck) || !Number.isSafeInteger(id)) return {ok:false, error:'untrusted_capture'};
    const guest = webContents.fromId(id);
    const retained = getRetainedGuest?.(id);
    const owner = retained?.owner || guest?.hostWebContents;
    const deny = (reason, guidance = 'Refresh this chat\'s browser state before capturing again') => {
      log(`[browser-capture] guest=${id} denied=${reason}`);
      return {ok:false, error:`browser_guest_not_owned: ${reason}. ${guidance}`};
    };
    if (!guest) return deny('guest_missing');
    if (guest.isDestroyed()) return deny('guest_destroyed');
    if (!owner) return deny('owner_missing');
    // A hidden retained tab is captured in the background: it borrows a tiny
    // visible paint surface (a never-shown or hidden view otherwise captures
    // empty) and is unthrottled for this read; the user's view is left as it is.
    let release = () => {}, lent = false;
    if (retained) {
      if (retained.contents !== guest) return deny('retained_guest_mismatch');
      if (retained.owner !== event.sender) return deny('owner_mismatch');
      if (!retained.visible && lendCaptureSurface) {
        const surface = lendCaptureSurface(id);
        if (!surface) return deny('attachment_hidden', 'Open this chat\'s browser panel before capturing again');
        release = surface; lent = true;
      }
    } else {
      if (guest.getType() !== 'webview') return deny('unsupported_guest_type');
      if (owner !== deck?.webContents && !isNativeHost(owner)) return deny('foreign_owner');
    }
    const sameAttachment = () => {
      if (!retained) return guest.hostWebContents === owner;
      const current = getRetainedGuest?.(id);
      return current?.contents === guest && current.owner === owner && current.attachmentId === retained.attachmentId
        && current.host === retained.host && current.document === retained.document;
    };
    let changed = false;
    let abandoned = false;
    let capturedViewport;
    const throttled = guest.getBackgroundThrottling();
    // Hidden retained guests normally idle. Enable their compositor only for
    // this bounded read so a screenshot observes current DOM, not old pixels.
    if (retained && !retained.visible) guest.setBackgroundThrottling(false);
    const navigation = (_event, _url, inPlace, mainFrame) => { if (mainFrame && !inPlace) changed = true; };
    guest.on('did-start-navigation', navigation);
    let timer;
    try {
      const image = await Promise.race([
        (async () => {
          // Prefer a painted frame after attachment/resizing. Occluded windows
          // can suspend animation frames despite backgroundThrottling:false;
          // bound that wait and let Chromium's capture request drive rendering.
          const measure = () => guest.executeJavaScript(`new Promise(resolve => {
            let frame, settled = false;
            const finish = () => {
              if (settled) return;
              settled = true; clearTimeout(timer); cancelAnimationFrame(frame);
              resolve({width: innerWidth, height: innerHeight, scale: devicePixelRatio});
            };
            const timer = setTimeout(finish, 200);
            frame = requestAnimationFrame(() => {frame = requestAnimationFrame(finish);});
          })`);
          let viewport = await measure();
          // A tab that has never been shown gets its size with its first
          // painted frame; give a borrowed surface a few frames to report it.
          for (let attempt = 0; lent && attempt < 10 && !(viewport?.width >= 1 && viewport?.height >= 1); attempt++) {
            if (abandoned) break;
            await new Promise(resolve => setTimeout(resolve, 50));
            viewport = await measure();
          }
          if (abandoned) throw new Error('browser_capture_cancelled');
          if (changed || guest.isDestroyed() || !sameAttachment()) throw new Error('browser_document_changed_during_capture');
          if (!viewport || !Number.isInteger(viewport.width) || !Number.isInteger(viewport.height)
            || viewport.width < 1 || viewport.width > 3840 || viewport.height < 1 || viewport.height > 2160) throw new Error('browser_capture_invalid_viewport');
          // The default rectangle is the visible portion and can be cropped
          // at a detached window's screen edge. Capture the bounded guest view.
          capturedViewport = viewport;
          while (!abandoned) {
            if (changed || guest.isDestroyed() || !sameAttachment()) throw new Error('browser_document_changed_during_capture');
            try {
              return await guest.capturePage({x:0,y:0,width:viewport.width,height:viewport.height}, {stayHidden:true});
            } catch (error) {
              // A newly embedded surface can lag its DOM/viewport. Capture is
              // a read; retry only this specific transient under the original
              // deadline. Never reload, resize, or replay page JavaScript.
              if (!/Current display surface not available for capture/.test(String(error?.message || error))) throw error;
              await new Promise(resolve => setTimeout(resolve, 50));
            }
          }
          throw new Error('browser_capture_cancelled');
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
      return {ok:true, image:png.toString('base64'), image_width:width, image_height:height,
        image_css_width:capturedViewport.width, image_css_height:capturedViewport.height};
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      log(`[browser-capture] guest=${id} error=${message}`);
      return {ok:false, error:message};
    } finally {
      abandoned = true; clearTimeout(timer); release();
      if (!guest.isDestroyed()) {
        guest.removeListener('did-start-navigation', navigation);
        // Another capture or input lease may still hold the borrowed surface.
        const current = getRetainedGuest?.(id);
        guest.setBackgroundThrottling(current ? !(current.visible || current.lent) : throttled);
      }
    }
  });
}
module.exports = {registerBrowserCapture};
