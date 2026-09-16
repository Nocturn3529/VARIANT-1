'use strict';
const fs = require('node:fs');
const path = require('node:path');
const ignore = require('ignore');

/** Share native watchers and release the owner's listener with its last watch. */
function createWorkbenchWatchers({hiddenNames = new Set(), changed = () => {}, fileSystem = fs, delay = 150,
  maxOwnerWatches = 128, maxOwnerSubscriptions = 512, maxWatches = 512, maxSubscriptions = 2048} = {}) {
  const owners = new Map();
  const subscriptions = new Map();
  let sequence = 0;
  let nativeCount = 0;
  function stop(sender, id) {
    const subscription = subscriptions.get(id);
    if (!subscription || subscription.sender !== sender) return;
    subscriptions.delete(id);
    const {owner, entry} = subscription;
    owner.subscriptionCount--;
    entry.ids.delete(id);
    if (!entry.ids.size) {
      clearTimeout(entry.timer); entry.watcher.close(); owner.entries.delete(entry.key);
      nativeCount--;
    }
    if (!owner.entries.size) { sender.removeListener('destroyed', owner.dispose); owners.delete(sender); }
  }
  function start(sender, target, scope = 'directory') {
    let owner = owners.get(sender);
    const key = `${scope}:${process.platform === 'win32' ? target.toLowerCase() : target}`;
    if (subscriptions.size >= maxSubscriptions || (owner?.subscriptionCount || 0) >= maxOwnerSubscriptions
      || (!owner?.entries.has(key) && ((owner?.entries.size || 0) >= maxOwnerWatches || nativeCount >= maxWatches))) {
      throw new Error('watch_limit_reached');
    }
    if (!owner) {
      owner = {entries:new Map(), subscriptionCount:0, dispose:()=>{ for (const [id, value] of subscriptions) if (value.sender === sender) stop(sender,id); }};
      owners.set(sender,owner); sender.once('destroyed',owner.dispose);
    }
    let entry = owner.entries.get(key);
    if (!entry) {
      entry = {key,target,ids:new Set(),watcher:null,timer:null,filename:undefined,rules:new Map()};
      const ignored = filename => {
        if (scope !== 'workspace' || !filename) return false;
        const relative = String(filename).replace(/\\/g,'/');
        const parts = relative.split('/');
        if (parts[0] === '.git') return !/^(?:\.git\/(?:HEAD|index|packed-refs)|\.git\/refs\/.+)$/.test(relative);
        if (parts.some(part => hiddenNames.has(part) || part === '.venv' || part === 'venv')) return true;
        if (parts.at(-1) === '.gitignore') { entry.rules.clear(); return false; }
        // Respect nested ignore files just as the Files pane does. Directory
        // watches used by an explicitly opened file remain unfiltered.
        for (let depth = 0; depth < parts.length; depth++) {
          const directory = path.join(target,...parts.slice(0,depth));
          if (!entry.rules.has(directory)) {
            const matcher = ignore();
            try { matcher.add(fileSystem.readFileSync(path.join(directory,'.gitignore'),'utf8')); } catch { /* no local rules */ }
            entry.rules.set(directory,matcher);
            if (entry.rules.size > 256) entry.rules.delete(entry.rules.keys().next().value);
          }
          try { if (entry.rules.get(directory)?.ignores(parts.slice(depth).join('/'))) return true; } catch { /* invalid path */ }
        }
        return false;
      };
      try {
        entry.watcher = fileSystem.watch(target,{recursive:scope === 'workspace' && process.platform === 'win32'},(_kind,filename)=>{
          if (ignored(filename)) return;
          const next = filename ? String(filename) : '';
          entry.filename = entry.filename === undefined ? next : entry.filename === next ? next : '';
          // A bounded notification rate also handles sustained build output.
          if (entry.timer) return;
          entry.timer = setTimeout(()=>{
            entry.timer=null;
            const name=entry.filename || ''; entry.filename=undefined;
            changed(target);
            if (!sender.isDestroyed()) for (const id of entry.ids) sender.send('workbench:fs:changed',{id,path:target,filename:name,event:'change'});
          },delay);
        });
        entry.watcher.on('error',error=>{
          if (!sender.isDestroyed()) for (const id of entry.ids) sender.send('workbench:fs:changed',{id,path:target,event:'error',error:String(error?.message || error)});
          for (const id of [...entry.ids]) stop(sender,id);
        });
        owner.entries.set(key,entry);
        nativeCount++;
      } catch (error) {
        if (!owner.entries.size) {sender.removeListener('destroyed',owner.dispose);owners.delete(sender)}
        throw error;
      }
    }
    const id=`watch-${++sequence}`;
    entry.ids.add(id); subscriptions.set(id,{sender,owner,entry});
    owner.subscriptionCount++;
    return id;
  }
  return {start,stop};
}

/** At most one status read per root; brief reuse across Files and Review. */
function createReadCache(read, {ttl = 2000, limit = 32} = {}) {
  const entries = new Map();
  let version = 0;
  function get(key) {
    let entry = entries.get(key);
    if (entry?.pending) return entry.version === version ? entry.pending : entry.pending.then(()=>get(key));
    if (entry && entry.until > Date.now()) return Promise.resolve(entry.value);
    entry = {pending:null,until:0,value:undefined,version}; entries.delete(key); entries.set(key,entry);
    // Pending entries are retained until they settle, preserving single flight.
    for (const [old, value] of entries) { if (entries.size <= limit) break; if (!value.pending && old !== key) entries.delete(old); }
    const current = entry;
    const pending = Promise.resolve().then(()=>read(key)).then(value=>{current.value=value;current.until=current.version === version ? Date.now()+ttl : 0;return value}).finally(()=>{current.pending=null});
    entry.pending=pending;
    return pending;
  }
  function invalidate() { version++; for (const entry of entries.values()) entry.until=0; }
  return {get,invalidate};
}
function limitReadConcurrency(limit = 2) {
  let active = 0;
  const queue = [];
  const drain = () => {
    while (active < limit && queue.length) {
      const next = queue.shift(); active++;
      Promise.resolve().then(next.read).then(next.resolve,next.reject).finally(()=>{active--;drain()});
    }
  };
  return read => new Promise((resolve,reject)=>{queue.push({read,resolve,reject});drain()});
}
function sharePendingRead(read) {
  const pending = new Map();
  return key => {
    if (!pending.has(key)) pending.set(key,Promise.resolve().then(()=>read(key)).finally(()=>pending.delete(key)));
    return pending.get(key);
  };
}
module.exports={createWorkbenchWatchers,createReadCache,limitReadConcurrency,sharePendingRead};
