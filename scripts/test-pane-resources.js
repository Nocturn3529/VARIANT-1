'use strict';
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');
const vm=require('node:vm');
const {createRequire}=require('node:module');
const {EventEmitter}=require('node:events');
const {createWorkbenchWatchers,createReadCache,limitReadConcurrency,sharePendingRead}=require('../electron-workbench-watchers');
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const target=path.resolve(__dirname,'..');
class Sender extends EventEmitter { constructor(){super();this.sent=[]} isDestroyed(){return false} send(_channel,value){this.sent.push(value)} }
(async()=>{
  const native=[];
  const fileSystem={readFileSync:file=>{
    if(file===path.join(target,'.gitignore'))return 'logs/\n*.cache\n';
    if(file===path.join(target,'package','.gitignore'))return 'generated/\n';
    throw new Error('ENOENT');
  },watch:(file,options,callback)=>{const watcher=new EventEmitter();watcher.close=()=>{watcher.closed=true};native.push({file,options,callback,watcher});return watcher}};
  let invalidations=0;
  const pool=createWorkbenchWatchers({fileSystem,hiddenNames:new Set(['node_modules','__pycache__','.git']),delay:5,changed:()=>invalidations++});
  const sender=new Sender(),other=new Sender();
  const first=pool.start(sender,target,'workspace'),second=pool.start(sender,target,'workspace');
  assert.equal(native.length,1);assert.equal(sender.listenerCount('destroyed'),1);
  for(const file of ['node_modules/x.js','backend/__pycache__/x.pyc','logs/run.log','.git/index.lock','.git/objects/ab/c','package/generated/output.js'])native[0].callback('change',file);
  await pause(12);assert.equal(sender.sent.length,0,'ignored/cache/index-lock churn must not refresh panels');
  native[0].callback('change','src/a.ts');native[0].callback('change','src/b.ts');
  await pause(12);assert.equal(sender.sent.length,2);assert.equal(invalidations,1);assert.equal(sender.sent[0].filename,'');
  native[0].callback('change','.git/index');await pause(12);assert.equal(sender.sent.length,4,'external staging remains observable');
  pool.stop(other,first);assert.equal(native[0].watcher.closed,undefined,'another sender cannot dispose a watch');
  pool.stop(sender,first);assert.equal(native[0].watcher.closed,undefined);
  pool.stop(sender,second);assert.equal(native[0].watcher.closed,true);assert.equal(sender.listenerCount('destroyed'),0);
  for(let i=0;i<50;i++){const id=pool.start(sender,target,'workspace');pool.stop(sender,id)}
  assert.equal(sender.listenerCount('destroyed'),0,'panel toggling cannot accumulate owner listeners');
  const direct=pool.start(sender,target,'directory');const explicit=native.at(-1);
  assert.equal(explicit.options.recursive,false);explicit.callback('change','explicit.cache');await pause(12);
  assert.equal(sender.sent.at(-1).id,direct,'explicit file watches ignore workspace filtering');
  explicit.watcher.emit('error',new Error('watch stopped'));assert.equal(sender.sent.at(-1).event,'error');assert.equal(sender.listenerCount('destroyed'),0);
  pool.start(sender,target,'workspace');sender.emit('destroyed');assert.equal(native.at(-1).watcher.closed,true);
  const bounded=createWorkbenchWatchers({fileSystem,maxOwnerWatches:1,maxOwnerSubscriptions:2,maxWatches:2,maxSubscriptions:3});
  const boundA=bounded.start(sender,target),boundB=bounded.start(sender,target);
  assert.throws(()=>bounded.start(sender,target),/watch_limit_reached/,'duplicate subscriptions are bounded');
  assert.throws(()=>bounded.start(sender,path.join(target,'other')),/watch_limit_reached/,'unique native watchers are bounded per owner');
  const boundOther=bounded.start(other,target);
  assert.throws(()=>bounded.start(new Sender(),target),/watch_limit_reached/,'global watcher/subscription budgets apply across windows');
  bounded.stop(sender,boundA);bounded.stop(sender,boundB);bounded.stop(other,boundOther);
  const reuse=bounded.start(sender,path.join(target,'other'));bounded.stop(sender,reuse);
  assert.equal(sender.listenerCount('destroyed'),0,'watcher quota returns after release');

  let calls=0,resolve;
  const cache=createReadCache(async()=>{calls++;return new Promise(yes=>{resolve=yes})});
  const reads=Array.from({length:30},()=>cache.get('root'));await pause(0);assert.equal(calls,1);
  resolve({files:[]});await Promise.all(reads);await cache.get('root');assert.equal(calls,1);
  cache.invalidate();const stale=cache.get('root');await pause(0);cache.invalidate();const fresh=cache.get('root');
  resolve({files:['before change']});await stale;await pause(0);assert.equal(calls,3,'a request after invalidation cannot reuse an older in-flight result');
  resolve({files:['after change']});assert.deepEqual(await fresh,{files:['after change']});

  const limited=limitReadConcurrency(2);let active=0,peak=0;
  await Promise.all(Array.from({length:30},()=>limited(async()=>{active++;peak=Math.max(peak,active);await pause(1);active--})));
  assert.equal(peak,2,'rapid selections cannot create more than two Git read processes');
  let sharedCalls=0;
  const shared=sharePendingRead(async()=>{sharedCalls++;await pause(5);return 'diff'});
  await Promise.all(Array.from({length:20},()=>shared('same-file')));assert.equal(sharedCalls,1);

  const filename=path.join(target,'electron-deck-ipc.js'),loaded={exports:{}},localRequire=createRequire(filename),handlers=new Map(),commands=[];
  vm.runInNewContext(fs.readFileSync(filename,'utf8'),{module:loaded,exports:loaded.exports,require:name=>name==='electron'?{ipcMain:{handle:(name,fn)=>handlers.set(name,fn),on(){}}}:name==='child_process'?{execFile:(file,args,options,callback)=>{
    commands.push({file,args,options});setTimeout(()=>callback(null,args[0]==='rev-parse'?target:args[0]==='status'?'## main\0 M sample.ts\0':'1\t2\tsample.ts\n',''),5);
  }}:localRequire(name),__dirname:target,process,Buffer,console,setTimeout,clearTimeout},{filename});
  loaded.exports.registerDeckIpc({app:{},appRoot:target,getDeckWindow:()=>({}),getMonitorWindow:()=>null,isTrustedIpcSender:()=>true});
  const results=await Promise.all(Array.from({length:20},()=>handlers.get('workbench:git:status')({},target)));
  assert.ok(results.every(value=>value.ok));assert.equal(commands.length,4,'Files and Review share one complete status operation');
  assert.ok(commands.every(value=>value.options.env.GIT_OPTIONAL_LOCKS==='0'&&value.options.windowsHide===true),'background Git reads must not update the index or open consoles');
  console.log('Pane resources: watcher sharing/filtering/disposal, cache invalidation, concurrent Git coalescing, and read-only process options passed');
})().catch(error=>{console.error(error);process.exitCode=1});
