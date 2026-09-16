'use strict';
// Real handlers/factories with native I/O replaced. Never executes Git or launches a window.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {EventEmitter}=require('node:events'),{createRequire}=require('node:module');
const root=path.resolve(__dirname,'..');
function load(name,overrides){const file=path.join(root,name),local=createRequire(file),module={exports:{}};
  vm.runInNewContext(fs.readFileSync(file,'utf8'),{module,exports:module.exports,require:id=>id in overrides?overrides[id]:local(id),__dirname:root,process,Buffer,console,setTimeout,clearTimeout},{filename:file});return module.exports;}

(async()=>{
  const handlers=new Map(),deck={},avatar={},commands=[],trashed=[];
  const repo=path.join(root,'virtual-repository');let untracked='new.txt',escapedParent=false;
  const electron={ipcMain:{handle:(id,fn)=>handlers.set(id,fn),on(){}},BrowserWindow:{},
    shell:{trashItem:async target=>trashed.push(target)}};
  const fakeFs={...fs,existsSync:()=>true,statSync:()=>({isDirectory:()=>true}),promises:{...fs.promises,
    realpath:async target=>escapedParent && target===path.join(repo,'link')?path.dirname(repo):target}};
  const ipc=load('electron-deck-ipc.js',{'electron':electron,fs:fakeFs,child_process:{execFile:(file,args,options,callback)=>{
    commands.push({file,args,options});callback(null,args.includes('rev-parse')?repo:args.includes('status')?`## main\0?? ${untracked}\0`:'','');}}});
  ipc.registerDeckIpc({app:{},appRoot:root,getDeckWindow:()=>deck,getMonitorWindow:()=>null,
    isTrustedIpcSender:(event,expected)=>event.sender===expected,readSettings:()=>({avatar:{size:240},secret:'must-not-leak'})});
  const run=handlers.get('workbench:git:run');
  for(const selected of ['../outside','a/../outside',path.join(path.dirname(repo),'outside'),'C:relative','\\\\host\\share\\file','bad\0name',{}]) {
    const result=await run({sender:deck},'stage',repo,{file:selected});assert.equal(result.ok,false);assert.match(result.error,/invalid_repository_file/);
  }
  assert.equal(commands.filter(row=>row.args.includes('add')).length,0,'invalid selections never become a stage-all command');
  assert.equal((await run({sender:deck},'stage',repo,{file:'file with spaces.txt'})).ok,true);
  assert.equal(commands.at(-1).options.env.GIT_LITERAL_PATHSPECS,'1');
  assert.equal(commands.at(-1).args.at(-1),'file with spaces.txt');
  assert.equal((await run({sender:deck},'revert',repo,{file:'new.txt'})).ok,true);
  assert.equal(trashed[0],path.join(repo,'new.txt'));
  untracked='link/new.txt';escapedParent=true;
  assert.equal((await run({sender:deck},'revert',repo,{file:untracked})).ok,false);
  assert.equal(trashed.length,1,'junction parents cannot redirect repository trash outside the root');
  assert.equal((await handlers.get('workbench:fs:unwatch')({sender:avatar},'missing')).ok,false);


  class Window extends EventEmitter {
    constructor(){super();this.dead=false;this.sent=[];this.loading=true;this.webContents=new EventEmitter();this.webContents.isLoading=()=>this.loading;this.webContents.send=(...args)=>this.sent.push(args);}
    show(){}focus(){}setMenuBarVisibility(){}maximize(){}loadURL(){}isDestroyed(){return this.dead;}
  }
  const windows=load('electron-app-windows.js',{'electron':{BrowserWindow:Window}}).createAppWindows({appRoot:root,hardenAppWindow(){}});
  const first=windows.openDeckWindow('chat');
  for(let i=0;i<20;i++)windows.openDeckWindow(i%2?'settings':'chat');
  assert.equal(first.webContents.listenerCount('did-finish-load'),1);
  first.loading=false;first.webContents.emit('did-finish-load');
  assert.deepEqual(first.sent,[['deck:navigate','settings']],'only newest queued navigation is delivered');
  first.loading=true;windows.openDeckWindow('settings');first.dead=true;
  const second=windows.openDeckWindow('chat');first.emit('closed');first.webContents.emit('did-finish-load');
  assert.equal(windows.getDeckWindow(),second);assert.equal(first.webContents.listenerCount('did-finish-load'),0);assert.equal(second.sent.length,0);

  const made=[],logs=[];
  const boot=load('electron-app-boot.js',{'electron':{},fs:{...fs,
    mkdirSync:target=>{made.push(target);if(path.basename(target)==='config')throw new Error('permission denied');},
    existsSync:()=>false,writeFileSync:()=>{throw new Error('read only');},rmSync(){}}});
  boot.createInitDataDir({app:{isPackaged:false},appRoot:repo,setDataDir(){},setConfigPath(){},setLogDir(){},rebindSettingsStore(){},beginSessionLog(){},log:line=>logs.push(line)})();
  assert.ok(made.some(target=>target.endsWith(path.join('runtimes','playwright'))),'one mkdir failure does not skip remaining independent folders');
  assert.ok(logs.some(line=>line.includes('could not create data directory')&&line.includes('permission denied')));
  assert.ok(logs.some(line=>line.includes('could not seed speech instructions')));
  console.log('Bug hunt Electron: concrete Git paths, junction trash fence, unwatch trust, latest navigation and boot diagnostics passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
