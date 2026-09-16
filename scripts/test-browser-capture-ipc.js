'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {EventEmitter} = require('node:events');
const owner = {}, native = {}, outsider = {};
const guest = new EventEmitter();
Object.assign(guest, {hostWebContents:owner, isDestroyed:()=>false, getType:()=> 'webview', executeJavaScript:async()=>({width:1280,height:720})});
let handler, captures = 0, deadline;
const exported = {exports:{}};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../electron-browser-capture.js'),'utf8'), {
  module:exported, require:()=>({ipcMain:{handle:(_name,fn)=>{handler=fn}}, webContents:{fromId:id=>id===7?guest:undefined}}),
  setTimeout:fn=>{deadline=fn;return 1}, clearTimeout:()=>{},
});
exported.exports.registerBrowserCapture({getDeckWindow:()=>({webContents:owner}),isTrustedIpcSender:event=>event.trusted===true,isNativeHost:host=>host===native});
const png = Buffer.alloc(24);
png.writeUInt32BE(1280, 16); png.writeUInt32BE(720, 20);
const image = {isEmpty:()=>false,toPNG:()=>png};
guest.capturePage=async rect=>{assert.deepEqual({...rect},{x:0,y:0,width:1280,height:720});captures++;return image};
(async()=>{
  assert.equal((await handler({trusted:false},7)).ok,false);
  assert.equal((await handler({trusted:true},'7')).ok,false);
  assert.equal((await handler({trusted:true},99)).ok,false);
  guest.hostWebContents=outsider; assert.equal((await handler({trusted:true},7)).ok,false);
  assert.equal(captures,0,'untrusted or unrelated captures cannot reach Chromium');
  guest.hostWebContents=owner;
  const captured = await handler({trusted:true},7);
  assert.equal(captured.image,png.toString('base64'));
  assert.equal(captured.image_width,1280); assert.equal(captured.image_height,720);
  guest.hostWebContents=native; assert.equal((await handler({trusted:true},7)).ok,true);
  const clippedPng=Buffer.from(png);clippedPng.writeUInt32BE(444,16);
  guest.capturePage=async()=>({isEmpty:()=>false,toPNG:()=>clippedPng});
  assert.match((await handler({trusted:true},7)).error,/browser_capture_clipped/,'a cropped frame cannot be reported as a complete viewport');
  let finish;
  guest.capturePage=()=>new Promise(resolve=>{finish=resolve});
  const navigation=handler({trusted:true},7);
  await new Promise(setImmediate);
  guest.emit('did-start-navigation',{},'https://local.test/',false,true);
  finish(image); assert.equal((await navigation).error,'browser_document_changed_during_capture');
  const timed=handler({trusted:true},7); await new Promise(setImmediate); deadline();
  assert.match((await timed).error,/browser_capture_timeout/);
  finish(image); assert.equal(guest.listenerCount('did-start-navigation'),0);
  let painted, lateCaptures=0;
  guest.executeJavaScript=()=>new Promise(resolve=>{painted=resolve});
  guest.capturePage=async()=>{lateCaptures++;return image};
  const unpainted=handler({trusted:true},7); deadline();
  assert.equal((await unpainted).error,'browser_capture_timeout');
  painted(true);await new Promise(setImmediate);
  assert.equal(lateCaptures,0,'a frame arriving after timeout cannot start another native capture');
  let attachment='retained-a', visible=true;
  guest.hostWebContents=null;guest.getType=()=> 'window';
  guest.executeJavaScript=async()=>({width:1280,height:720});
  guest.capturePage=async()=>image;
  exported.exports.registerBrowserCapture({getDeckWindow:()=>({webContents:owner}),
    isTrustedIpcSender:event=>event.trusted===true,isNativeHost:()=>false,
    getRetainedGuest:id=>id===7?{contents:guest,owner,attachmentId:attachment,visible}:null});
  assert.equal((await handler({trusted:true,sender:outsider},7)).ok,false);
  assert.equal((await handler({trusted:true,sender:owner},7)).ok,true,'retained WebContentsView uses canonical ownership');
  visible=false;assert.equal((await handler({trusted:true,sender:owner},7)).ok,false);visible=true;
  guest.capturePage=()=>new Promise(resolve=>{finish=resolve});
  const moved=handler({trusted:true,sender:owner},7);await new Promise(setImmediate);
  attachment='retained-b';finish(image);
  assert.equal((await moved).error,'browser_document_changed_during_capture','capture rejects a mid-frame attachment transfer');
  console.log('E01 capture IPC: trusted guest ownership, native image encoding, navigation fence, deadline, and listener cleanup passed');
})().catch(error=>{console.error(error);process.exitCode=1});
