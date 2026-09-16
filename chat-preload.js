'use strict';
const {contextBridge,ipcRenderer,webUtils}=require('electron');
contextBridge.exposeInMainWorld('variant1Deck',{
  getBackendInfo:()=>ipcRenderer.invoke('chat-window:backendInfo'),
  onBackendStatus:callback=>{const listener=(_event,value)=>callback(value);ipcRenderer.on('backend:status',listener);return()=>ipcRenderer.removeListener('backend:status',listener);},
  getPathForFile:file=>{try{return webUtils.getPathForFile(file);}catch{return ''; }},
  openExternal:url=>ipcRenderer.invoke('chat-window:external',url),
  minimize:()=>ipcRenderer.invoke('chat-window:control','minimize'),
  toggleMaximize:()=>ipcRenderer.invoke('chat-window:control','maximize'),
  close:()=>ipcRenderer.invoke('chat-window:control','close'),
});
