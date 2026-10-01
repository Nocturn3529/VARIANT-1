'use strict';

// Read-only Review observations. Sender/absolute-path admission remains in IPC.
const fs = require('node:fs/promises');
const path = require('node:path');
const {projectGitDiff: defaultProjection, COMMAND_BYTES} = require('./electron-workbench-diff');
const MAX_FILES = 2000;
const MAX_CONTEXT = 1000;
const oidPattern = /^(?:[a-f0-9]{40}|[a-f0-9]{64})$/;

function bound(value, fallback, maximum, minimum = 0) {
  if (value === undefined) return fallback;
  if (!Number.isSafeInteger(value) || value < minimum) throw new Error('invalid_review_limit');
  return Math.min(value, maximum);
}
function reference(value) {
  if (typeof value !== 'string' || !value || value.length > 1024 || value.startsWith('-') || /[\x00-\x20\x7f]/.test(value)) throw new Error('invalid_revision');
  return value;
}
function fields(text, size) {
  const values = String(text || '').split('\0');
  const rows = [];
  let cursor = 0;
  while (cursor < values.length && values[cursor].replace(/^[\r\n]+/, '')) {
    if (cursor + size >= values.length) throw new Error('invalid_git_records');
    const row = values.slice(cursor, cursor + size);
    row[0] = row[0].replace(/^[\r\n]+/, '');
    rows.push(row); cursor += size;
  }
  if (values.slice(cursor).some(value => value.replace(/[\r\n]/g, ''))) throw new Error('invalid_git_records');
  return rows;
}
function terminated(text) {
  const value = String(text || '');
  if (value && !value.endsWith('\0')) throw new Error('truncated_git_records');
  return value ? value.slice(0, -1).split('\0') : [];
}
function errorResult(error) {
  return {ok:false,error:String(error?.stderr || error?.message || error).slice(0, 2000)};
}

function createGitReviewReader({readGit, gitRoot, repositoryFile, projectGitDiff = defaultProjection}) {
  const run = (root, args) => readGit(['--no-pager', '--literal-pathspecs', ...args], {cwd:root,timeout:30000,maxBuffer:COMMAND_BYTES});
  const safePath = (root, value) => repositoryFile(root, value).relative;
  async function resolve(root, ref) {
    const {stdout} = await run(root, ['rev-parse','--verify','--end-of-options',`${reference(ref)}^{commit}`]);
    const oid = stdout.trim(); if (!oidPattern.test(oid)) throw new Error('invalid_commit_oid'); return oid;
  }
  async function head(root) {
    try { return await resolve(root, 'HEAD'); }
    catch (error) {
      // Only an actual unborn symbolic branch may omit HEAD; other errors remain errors.
      const symbolic = (await run(root,['symbolic-ref','-q','HEAD'])).stdout.trim();
      const refs = (await run(root,['for-each-ref','--format=%(refname)',symbolic])).stdout.trim();
      if (!symbolic.startsWith('refs/heads/') || refs) throw error;
      return '';
    }
  }
  async function parents(root, oid) {
    const raw = (await run(root,['rev-list','--parents','--max-count=1',oid])).stdout.trim().split(/\s+/);
    if (raw[0] !== oid || raw.some(value=>!oidPattern.test(value))) throw new Error('invalid_commit_parents');
    return raw.slice(1);
  }
  async function plan(root, options = {}) {
    const scope = options.scope || 'uncommitted';
    const current = await head(root);
    if (scope === 'staged') return {scope,baseOid:current,headOid:current,command:'diff',selector:['--cached'],commits:[]};
    if (scope === 'unstaged') return {scope,baseOid:current,headOid:current,command:'diff',selector:[],commits:[]};
    if (scope === 'uncommitted') return {scope,baseOid:current,headOid:current,command:'diff',selector:current ? [current] : [],commits:[],unborn:!current};
    if (scope === 'branch') {
      if (!options.ref || !options.baseRef) throw new Error('branch_comparison_requires_explicit_base');
      const tip = await resolve(root,options.ref), base = await resolve(root,options.baseRef);
      const common = (await run(root,['merge-base',base,tip])).stdout.trim();
      if (!oidPattern.test(common)) throw new Error('no_unique_branch_merge_base');
      return {scope,baseOid:common,headOid:tip,command:'diff',selector:[common,tip],commits:[],baseRefOid:base};
    }
    if (scope !== 'commit') throw new Error('invalid_review_scope');
    const supplied = options.commits || (options.commit ? [options.commit] : []);
    if (!Array.isArray(supplied) || !supplied.length || supplied.length > 100) throw new Error('invalid_commit_selection');
    const selected = [];
    for (const value of supplied) selected.push(await resolve(root,value));
    if (new Set(selected).size !== selected.length) throw new Error('duplicate_commit_selection');
    const ancestry = new Map();
    for (const oid of selected) ancestry.set(oid,(await parents(root,oid))[0] || '');
    const older = new Set([...ancestry.values()]);
    const tips = selected.filter(oid=>!older.has(oid));
    if (tips.length !== 1) throw new Error('noncontiguous_commit_selection');
    const ordered = []; let cursor = tips[0];
    while (ancestry.has(cursor)) {ordered.push(cursor);cursor=ancestry.get(cursor);}
    if (ordered.length !== selected.length) throw new Error('noncontiguous_commit_selection');
    return {scope,baseOid:cursor,headOid:ordered[0],commits:ordered,
      command:cursor ? 'diff' : 'diff-tree',selector:cursor ? [cursor,ordered[0]] : ['--root','--no-commit-id','-r',ordered[0]]};
  }
  const identity = value => ({scope:value.scope,baseOid:value.baseOid,headOid:value.headOid,commits:value.commits,...(value.baseRefOid ? {baseRefOid:value.baseRefOid} : {})});
  const diffArgs = (value, flags, paths = []) => [value.command,'--no-ext-diff','--no-textconv','--no-color','--find-renames',...flags,...value.selector,'--',...paths];
  async function list(root, value) {
    const entries = new Map();
    if (!value.unborn) {
      const tokens = terminated((await run(root,diffArgs(value,['--name-status','-z']))).stdout);
      for (let cursor=0;cursor<tokens.length;) {
        const rawStatus=tokens[cursor++];
        if (!/^[ACDMRTUXB][0-9]*$/.test(rawStatus)) throw new Error('invalid_git_file_status');
        let originalPath='';
        if (/^[RC]/.test(rawStatus)) originalPath=safePath(root,tokens[cursor++]);
        const name=safePath(root,tokens[cursor++]);
        entries.set(name,{path:name,originalPath,status:rawStatus[0],added:0,removed:0,binary:false,untracked:false});
      }
      const stats = terminated((await run(root,diffArgs(value,['--numstat','-z']))).stdout);
      for (let cursor=0;cursor<stats.length;) {
        const record=stats[cursor++].split('\t');
        if (record.length<3) throw new Error('invalid_git_numstat');
        const [added,removed,...rest]=record;
        let name=rest.join('\t');
        if (!name) {safePath(root,stats[cursor++]);name=stats[cursor++];}
        name=safePath(root,name);
        if (!/^(?:\d+|-)$/.test(added) || !/^(?:\d+|-)$/.test(removed)) throw new Error('invalid_git_numstat');
        const row=entries.get(name);
        if (row) Object.assign(row,{added:added==='-'?null:Number(added),removed:removed==='-'?null:Number(removed),binary:added==='-' || removed==='-'});
      }
    }
    if (value.scope === 'uncommitted' || value.scope === 'unstaged') {
      const args=['ls-files','--others','--exclude-standard','-z'];
      if (value.unborn) args.push('--cached');
      for (const raw of terminated((await run(root,args)).stdout)) {
        const name=safePath(root,raw);
        if (value.unborn) {
          try {await fs.lstat(repositoryFile(root,name).absolute);} catch(error) {if(error.code==='ENOENT')continue;throw error;}
        }
        if (!entries.has(name)) entries.set(name,{path:name,originalPath:'',status:value.unborn?'A':'??',added:null,removed:0,binary:false,untracked:true});
      }
    }
    return {files:[...entries.values()].slice(0,MAX_FILES),truncated:entries.size>MAX_FILES};
  }
  async function untracked(root, file) {
    const target=repositoryFile(root,file).absolute;
    const realRoot=await fs.realpath(root);
    const parent=await fs.realpath(path.dirname(target));
    const relative=path.relative(realRoot,parent);
    if (relative==='..' || relative.startsWith('..'+path.sep) || path.isAbsolute(relative)) throw new Error('repository_file_escapes_root');
    const info=await fs.lstat(target);
    if (info.isSymbolicLink()) {const text=await fs.readlink(target);return {text,bytes:Buffer.byteLength(text),overflow:false,binary:false};}
    if (!info.isFile()) throw new Error('unsupported_repository_file');
    const handle=await fs.open(target,'r');
    try {
      const opened=await handle.stat();
      if (opened.dev!==info.dev || opened.ino!==info.ino) throw new Error('repository_file_changed');
      const bytes=Buffer.alloc(COMMAND_BYTES+1);
      let count=0;
      while(count<bytes.length) {const read=await handle.read(bytes,count,bytes.length-count,count);if(!read.bytesRead)break;count+=read.bytesRead;}
      return {text:bytes.subarray(0,Math.min(count,COMMAND_BYTES)).toString('utf8'),bytes:Math.min(count,COMMAND_BYTES),overflow:count>COMMAND_BYTES,binary:bytes.subarray(0,Math.min(count,8192)).includes(0)};
    } finally {await handle.close();}
  }
  async function branches(target) {
    try {
      const root=await gitRoot(target),headOid=await head(root);
      const raw=(await run(root,['for-each-ref','--count=201','--sort=refname','--format=%(refname)%00%(refname:short)%00%(objectname)%00%(HEAD)%00%(symref)%00','refs/heads','refs/remotes'])).stdout;
      const rows=fields(raw,5).filter(row=>!row[4]).map(([ref,name,oid,current])=>{
        if(!oidPattern.test(oid))throw new Error('invalid_branch_oid');
        return {ref,name,oid,current:current==='*',remote:ref.startsWith('refs/remotes/')};
      });
      return {ok:true,root,headOid,branches:rows.slice(0,200),truncated:rows.length>200 || fields(raw,5).length>200};
    } catch(error) {return errorResult(error);}
  }
  async function history(target,options={}) {
    try {
      const root=await gitRoot(target),limit=bound(options.limit,50,100,1),offset=bound(options.offset,0,10000);
      const resolvedOid=options.ref ? await resolve(root,options.ref) : await head(root);
      if(!resolvedOid)return {ok:true,root,resolvedOid:'',commits:[],limit,offset,nextOffset:null,truncated:false};
      const raw=(await run(root,['log','--first-parent','--no-show-signature','--no-decorate',`--max-count=${limit+1}`,`--skip=${offset}`,'--format=%H%x00%P%x00%an%x00%ct%x00%s%x00',resolvedOid])).stdout;
      const rows=fields(raw,5).map(([oid,parentText,authorName,date,subject])=>{
        const parents=parentText ? parentText.split(' ') : [];
        if(!oidPattern.test(oid) || parents.some(value=>!oidPattern.test(value)) || !/^\d+$/.test(date))throw new Error('invalid_git_history');
        return {oid,parents,authorName:authorName.slice(0,512),subject:subject.slice(0,2048),committedAt:Number(date)};
      });
      return {ok:true,root,resolvedOid,commits:rows.slice(0,limit),limit,offset,nextOffset:rows.length>limit?offset+limit:null,truncated:rows.length>limit};
    } catch(error) {return errorResult(error);}
  }
  async function files(target,options={}) {
    try {const root=await gitRoot(target),value=await plan(root,options);return {ok:true,root,...identity(value),...await list(root,value)};}
    catch(error) {return errorResult(error);}
  }
  async function diff(target,file,options={}) {
    try {
      const root=await gitRoot(target),selected=safePath(root,file),value=await plan(root,options),context=bound(options.context,3,MAX_CONTEXT);
      const listed=await list(root,value),row=listed.files.find(row=>row.path===selected);
      if(!row)throw new Error(listed.truncated?'review_file_list_truncated':'file_not_in_review_scope');
      if(row.untracked) {
        const data=await untracked(root,selected);
        return {ok:true,root,...identity(value),context,fullContents:!data.binary,...projectGitDiff(data.binary?'':data.text,data.overflow),binary:data.binary,originalBytes:data.bytes,originalBytesExact:!data.overflow};
      }
      let projection;
      try {projection=projectGitDiff((await run(root,diffArgs(value,['--patch',`--unified=${context}`],[...(row.originalPath?[row.originalPath]:[]),selected]))).stdout);}
      catch(error) {if(error.code!=='ERR_CHILD_PROCESS_STDIO_MAXBUFFER' || !error.stdout)throw error;projection=projectGitDiff(error.stdout,true);}
      return {ok:true,root,...identity(value),context,fullContents:false,...projection,binary:row.binary || projection.binary};
    } catch(error) {return errorResult(error);}
  }
  return {branches,history,files,diff};
}
module.exports={createGitReviewReader,MAX_CONTEXT,MAX_FILES};
