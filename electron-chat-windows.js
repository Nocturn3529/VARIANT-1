'use strict';
const path = require('node:path');
const {BrowserWindow, ipcMain, shell} = require('electron');
const {isAllowedExternalUrl} = require('./electron-security');

function createChatWindows({appRoot,getDeckWindow,getBackendInfo,isTrustedIpcSender,hardenAppWindow,show=true}) {
  const windows = new Map();
  const titles = new Map();let revision=0;
  const snapshot=()=>({revision,windows:[...windows].filter(([,win])=>!win.isDestroyed()).map(([chat_id,win])=>({chat_id,title:titles.get(chat_id)||'Chat',focused:win.isFocused()}))});
  const publish=()=>{revision++;const deck=getDeckWindow();if(deck&&!deck.isDestroyed())deck.webContents.send('chat-window:changed',snapshot());};
  ipcMain.handle('chat-window:list',event=>isTrustedIpcSender(event,getDeckWindow()) ? snapshot() : null);
  ipcMain.handle('chat-window:manage',(event,id,action)=>{
    if(!isTrustedIpcSender(event,getDeckWindow()))return {ok:false};const win=windows.get(id);if(!win||win.isDestroyed())return {ok:false};
    if(action==='focus'){if(win.isMinimized())win.restore();win.show();win.focus();}
    else if(action==='close')win.close();else return {ok:false};return {ok:true};
  });
  const target = id => `variant1://app/frontend/main-deck/index.html?detached_chat=${encodeURIComponent(id)}`;
  function owned(event) {
    for(const [id,win] of windows)if(!win.isDestroyed() && event.sender === win.webContents
      && event.senderFrame === win.webContents.mainFrame && event.senderFrame.url === target(id))return win;
    return null;
  }
  ipcMain.handle('chat-window:open',async(event,id,title)=>{
    if(!isTrustedIpcSender(event,getDeckWindow()) || typeof id !== 'string' || !/^[\w.:-]{1,256}$/.test(id))return {ok:false,error:'Invalid chat window request'};
    let win=windows.get(id);
    if(win && !win.isDestroyed()){if(win.isMinimized())win.restore();win.show();win.focus();return {ok:true};}
    win=new BrowserWindow({width:680,height:760,minWidth:380,minHeight:400,frame:false,show:false,
      title:String(title || 'Chat').slice(0,160),backgroundColor:'#0e0e0e',autoHideMenuBar:true,
      webPreferences:{preload:path.join(appRoot,'chat-preload.js'),contextIsolation:true,nodeIntegration:false,sandbox:true,backgroundThrottling:false}});
    windows.set(id,win);titles.set(id,String(title || "Chat").slice(0,160));hardenAppWindow(win);
    win.webContents.on("page-title-updated",event=>event.preventDefault());
    win.on("focus",publish);win.on("blur",publish);publish();
    win.webContents.on('will-navigate',(e,url)=>{if(url!==target(id))e.preventDefault();});
    win.webContents.on('will-redirect',(e,url)=>{if(url!==target(id))e.preventDefault();});
    win.once('closed',()=>{windows.delete(id);titles.delete(id);publish();});
    win.once('ready-to-show',()=>{if(show && !win.isDestroyed())win.show();});
    try {await win.loadURL(target(id));return {ok:true};}
    catch(error){if(!win.isDestroyed())win.destroy();return {ok:false,error:String(error)};}
  });
  ipcMain.handle('chat-window:backendInfo',event=>owned(event) ? getBackendInfo() : null);
  ipcMain.handle('chat-window:control',(event,action)=>{
    const win=owned(event);if(!win)return {ok:false};
    if(action==='close')win.close();else if(action==='minimize')win.minimize();else if(action==='maximize')win.isMaximized()?win.unmaximize():win.maximize();else return {ok:false};
    return {ok:true};
  });
  ipcMain.handle('chat-window:external',async(event,url)=>{
    if(!owned(event)||!isAllowedExternalUrl(url))return {ok:false};
    await shell.openExternal(url);return {ok:true};
  });
  return {list:()=>[...windows.values()].filter(win=>!win.isDestroyed()),closeAll:()=>{for(const win of windows.values())if(!win.isDestroyed())win.close();}};
}
module.exports={createChatWindows};
