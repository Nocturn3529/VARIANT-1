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
  setTimeout:(fn,ms)=>{if(ms===6000){deadline=fn;return 1}return setTimeout(fn,ms)}, clearTimeout:id=>{if(id!==1)clearTimeout(id)},
});
exported.exports.registerBrowserCapture({getDeckWindow:()=>({webContents:owner}),isTrustedIpcSender:event=>event.trusted===true,isNativeHost:host=>host===native});
const png = Buffer.alloc(24);
png.writeUInt32BE(1280, 16); png.writeUInt32BE(720, 20);
const image = {isEmpty:()=>false,toPNG:()=>png};
guest.capturePage=async (rect,options)=>{assert.deepEqual({...rect},{x:0,y:0,width:1280,height:720});assert.equal(options.stayHidden,true);captures++;return image};
(async()=>{
  assert.equal((await handler({trusted:false},7)).ok,false);
  assert.equal((await handler({trusted:true},'7')).ok,false);
  assert.match((await handler({trusted:true},99)).error,/guest_missing/);
  guest.isDestroyed=()=>true;assert.match((await handler({trusted:true},7)).error,/guest_destroyed/);guest.isDestroyed=()=>false;
  guest.hostWebContents=outsider; assert.match((await handler({trusted:true},7)).error,/foreign_owner/);
  assert.equal(captures,0,'untrusted or unrelated captures cannot reach Chromium');
  guest.hostWebContents=owner;
  const captured = await handler({trusted:true},7);
  assert.equal(captured.image,png.toString('base64'));
  assert.equal(captured.image_width,1280); assert.equal(captured.image_height,720);
  const originalViewport=guest.executeJavaScript;
  guest.executeJavaScript=async expression=>vm.runInNewContext(expression,{
    innerWidth:1280,innerHeight:720,devicePixelRatio:1,
    requestAnimationFrame:()=>1,cancelAnimationFrame:()=>{},clearTimeout:()=>{},
    setTimeout:(callback,ms)=>{assert.equal(ms,200);queueMicrotask(callback);return 1},
  });
  assert.equal((await handler({trusted:true},7)).ok,true,'an occluded guest whose animation frames never fire is still capturable');
  guest.executeJavaScript=originalViewport;
  const originalCapture=guest.capturePage;let surfaceAttempts=0;
  guest.capturePage=async (...args)=>{if(++surfaceAttempts<3)throw new Error('Current display surface not available for capture');return originalCapture(...args)};
  assert.equal((await handler({trusted:true},7)).ok,true,'fresh compositor surfaces may settle under the existing deadline');
  assert.equal(surfaceAttempts,3);
  guest.capturePage=originalCapture;
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
  assert.match((await handler({trusted:true,sender:outsider},7)).error,/owner_mismatch/);
  assert.equal((await handler({trusted:true,sender:owner},7)).ok,true,'retained WebContentsView uses canonical ownership');
  visible=false;assert.match((await handler({trusted:true,sender:owner},7)).error,/attachment_hidden.*Reveal/);visible=true;
  guest.capturePage=()=>new Promise(resolve=>{finish=resolve});
  const moved=handler({trusted:true,sender:owner},7);await new Promise(setImmediate);
  attachment='retained-b';finish(image);
  assert.equal((await moved).error,'browser_document_changed_during_capture','capture rejects a mid-frame attachment transfer');
  console.log('E01 capture IPC: trusted guest ownership, native image encoding, navigation fence, deadline, and listener cleanup passed');
})().catch(error=>{console.error(error);process.exitCode=1});
