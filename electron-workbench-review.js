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

function createGitReviewReader({readGit, gitRoot, repositoryFile, workbenchGitStatus, projectGitDiff = defaultProjection, now = Date.now}) {
  const observations = new Map();
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
  async function defaultBase(root) {
    const raw=(await run(root,['for-each-ref','--format=%(refname)%00%(symref)%00','refs/remotes'])).stdout;
    const aliases=fields(raw,2).filter(([ref,target])=>ref.endsWith('/HEAD') && target);
    const chosen=aliases.find(([ref])=>ref==='refs/remotes/origin/HEAD') || (aliases.length===1 ? aliases[0] : undefined);
    if(chosen){await resolve(root,chosen[1]);return {baseRef:chosen[1],baseSource:chosen[0]};}
    if(aliases.length>1)throw new Error('ambiguous_default_base_select_explicit_base');
    let configured='';
    try {configured=(await run(root,['config','--get','init.defaultBranch'])).stdout.trim();}
    catch(error){if(error.code!==1)throw error;}
    if(configured){const baseRef=`refs/heads/${reference(configured)}`;await resolve(root,baseRef);return {baseRef,baseSource:'init.defaultBranch'};}
    throw new Error('comparison_base_unavailable_select_explicit_base');
  }
  async function selectedPlans(root,supplied) {
    if(!Array.isArray(supplied) || !supplied.length || supplied.length>100)throw new Error('invalid_commit_selection');
    const selected=[];
    for(const item of supplied){reference(item);selected.push(oidPattern.test(item) ? item : await resolve(root,item));}
    if(new Set(selected).size!==selected.length)throw new Error('duplicate_commit_selection');
    const raw=(await run(root,['rev-list','--parents','--no-walk=sorted',...selected.sort(),'--'])).stdout.trim();
    const plans=[];
    for(const record of raw.split(/\r?\n/)) {
      const [oid,...parents]=record.split(' ');
      if(!selected.includes(oid) || parents.some(value=>!oidPattern.test(value)))throw new Error('invalid_commit_parents');
      const baseOid=parents[0] || '';
      plans.push({scope:'commit',baseOid,headOid:oid,commits:[oid],command:baseOid?'diff':'diff-tree',selector:baseOid?[baseOid,oid]:['--root','--no-commit-id','-r',oid]});
    }
    if(new Set(plans.map(value=>value.headOid)).size!==selected.length)throw new Error('invalid_commit_selection');
    return plans;
  }
  async function plan(root, options = {}) {
    const scope = options.scope || 'uncommitted';
    const current = ['staged','unstaged','uncommitted'].includes(scope) ? await head(root) : '';
    if (scope === 'staged') return {scope,baseOid:current,headOid:current,command:'diff',selector:['--cached'],commits:[]};
    if (scope === 'unstaged') return {scope,baseOid:current,headOid:current,command:'diff',selector:[],commits:[]};
    if (scope === 'uncommitted') return {scope,baseOid:current,headOid:current,command:'diff',selector:current ? [current] : [],commits:[],unborn:!current};
    if (scope === 'branch') {
      const ref=options.ref || 'HEAD';
      const baseline=options.baseRef ? {baseRef:reference(options.baseRef),baseSource:'explicit'} : await defaultBase(root);
      const tip = await resolve(root,ref), base = await resolve(root,baseline.baseRef);
      const common = (await run(root,['merge-base','--all',base,tip])).stdout.trim();
      if (!oidPattern.test(common)) throw new Error('no_unique_branch_merge_base');
      const count=(await run(root,['rev-list','--count',tip,`^${common}`,'--'])).stdout.trim();
      if(!/^\d+$/.test(count))throw new Error('invalid_branch_commit_count');
      return {scope,baseOid:common,headOid:tip,command:'diff',selector:[common,tip],commits:[],baseRefOid:base,commitCount:Number(count),comparison:{ref,...baseline,baseRefOid:base,baseOid:common,headOid:tip}};
    }
    if (scope !== 'commit') throw new Error('invalid_review_scope');
    const segments=await selectedPlans(root,options.commits || (options.commit ? [options.commit] : []));
    return {scope,segments,baseOid:segments.length===1?segments[0].baseOid:'',headOid:segments.length===1?segments[0].headOid:'',commits:segments.map(value=>value.headOid)};
  }
  const identity = value => ({scope:value.scope,baseOid:value.baseOid,headOid:value.headOid,commits:[...value.commits],countBasis:value.scope==='commit'?'selected-commits':value.scope==='branch'?'branch-delta':'live',...(value.comparison ? {comparison:{...value.comparison},commitCount:value.commitCount,baseRefOid:value.baseRefOid} : {})});
  const diffArgs = (value, flags, paths = []) => [value.command,'--no-ext-diff','--no-textconv','--no-color','--find-renames',...flags,...value.selector,'--',...paths];
  function aggregate(rows) {
    return {added:rows.reduce((sum,row)=>sum+(row.added || 0),0),removed:rows.reduce((sum,row)=>sum+(row.removed || 0),0),fileCount:rows.length,binaryFiles:rows.filter(row=>row.binary).length,complete:rows.every(row=>row.binary || row.added!==null && row.removed!==null)};
  }
  async function showRecords(root,oids,kind) {
    const tokens=String((await run(root,['show','--format=%x00%H%x00',kind,'-z','--no-ext-diff','--no-textconv','--no-color','--diff-merges=first-parent','--find-renames','--root','--no-show-signature',...oids,'--'])).stdout).split('\0');
    const output=new Map();let active;
    for(let cursor=0;cursor<tokens.length;) {
      if(tokens[cursor]==='' && oidPattern.test(tokens[cursor+1] || '')) {
        const oid=tokens[cursor+1];if(!oids.includes(oid) || output.has(oid))throw new Error('invalid_git_commit_records');
        active=new Map();output.set(oid,active);cursor+=2;continue;
      }
      const token=tokens[cursor++].replace(/^\r?\n/,'');if(!token)continue;
      if(!active)throw new Error('invalid_git_commit_records');
      if(kind==='--name-status') {
        if(!/^[ACDMRTUXB][0-9]*$/.test(token))throw new Error('invalid_git_file_status');
        const originalPath=/^[RC]/.test(token)?safePath(root,tokens[cursor++]):'';
        const name=safePath(root,tokens[cursor++]);active.set(name,{path:name,originalPath,status:token[0]});
      } else {
        const [added,removed,...rest]=token.split('\t');let name=rest.join('\t'),originalPath='';
        if(!name){originalPath=safePath(root,tokens[cursor++]);name=tokens[cursor++];}
        name=safePath(root,name);
        if(!/^(?:\d+|-)$/.test(added) || !/^(?:\d+|-)$/.test(removed))throw new Error('invalid_git_numstat');
        const counts={added:added==='-'?null:Number(added),removed:removed==='-'?null:Number(removed)};
        if(Object.values(counts).some(value=>value!==null && !Number.isSafeInteger(value)))throw new Error('invalid_git_numstat');
        active.set(name,{path:name,originalPath,...counts,binary:added==='-' || removed==='-'});
      }
    }
    if(output.size!==oids.length)throw new Error('invalid_git_commit_records');return output;
  }
  async function selectionList(root,value) {
    const names=await showRecords(root,value.commits,'--name-status'),stats=await showRecords(root,value.commits,'--numstat');
    const entries=new Map();
    for(const oid of value.commits)for(const [name,meta] of names.get(oid)) {
      const count=stats.get(oid).get(name);if(!count)throw new Error('inconsistent_git_commit_records');
      const prior=entries.get(name);
      if(prior){prior.added+=count.added || 0;prior.removed+=count.removed || 0;prior.binary ||= count.binary;prior.commitOids.push(oid);}
      else entries.set(name,{...meta,added:count.added || 0,removed:count.removed || 0,binary:count.binary,untracked:false,commitOids:[oid]});
    }
    const all=[...entries.values()];return {files:all.slice(0,MAX_FILES),truncated:all.length>MAX_FILES,aggregate:aggregate(all),commitFiles:names};
  }
  async function list(root, value) {
    const entries = new Map();let branch;
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
    if(['uncommitted','staged','unstaged'].includes(value.scope)) {
      let status;
      if(workbenchGitStatus)status=await workbenchGitStatus(root);
      else {
        const tokens=terminated((await run(root,['status','--porcelain=v1','--untracked-files=all','-z','-b'])).stdout),files=[];
        const header=tokens.shift() || '';
        if(!header.startsWith('## '))throw new Error('invalid_git_status');
        const branchName=/^## (?:No commits yet on |Initial commit on )?(.+?)(?:\.\.\.| \[|$)/.exec(header)?.[1];
        for(let cursor=0;cursor<tokens.length;) {
          const record=tokens[cursor++];
          if(record.length<4 || record[2]!==' ')throw new Error('invalid_git_status');
          const row={path:record.slice(3),status:record.slice(0,2)};
          if(/[RC]/.test(row.status))row.originalPath=tokens[cursor++];
          files.push(row);
        }
        status={ok:true,files,branch:branchName};
      }
      if(!status?.ok || !Array.isArray(status.files))throw new Error('live_git_status_unavailable');
      if(typeof status.branch==='string')branch=status.branch;
      const live=new Map();
      for(const row of status.files) {
        const name=safePath(root,row.path),xy=row.status;
        if(typeof xy!=='string' || !/^[ MARCUDT?!]{2}$/.test(xy))throw new Error('invalid_git_status');
        if(xy==='??' && /[\\/]$/.test(row.path)) {
          for(const [file,entry] of entries)if(entry.untracked && file.startsWith(name+'/'))live.set(file,{path:file,originalPath:'',status:'??',staged:false,unstaged:true,untracked:true});
          continue;
        }
        const staged=xy[0]!==' ' && xy[0]!=='?',unstaged=xy==='??' || xy[1]!==' ';
        const prior=live.get(name);
        live.set(name,{path:name,originalPath:row.originalPath ? safePath(root,row.originalPath) : prior?.originalPath || '',status:prior && xy==='??' ? prior.status : xy,
          staged:staged || !!prior?.staged,unstaged:unstaged || !!prior?.unstaged,untracked:xy==='??' || !!prior?.untracked});
      }
      const admitted=new Set();
      for(const [name,row] of live) {
        if(value.scope==='staged' && !row.staged || value.scope==='unstaged' && !row.unstaged)continue;
        admitted.add(name);
        const existing=entries.get(name);
        if(existing)Object.assign(existing,{status:row.status,staged:row.staged,unstaged:row.unstaged,originalPath:existing.originalPath || row.originalPath});
        else entries.set(name,{...row,added:null,removed:null,binary:false});
      }
      for(const name of entries.keys())if(!admitted.has(name))throw new Error('review_status_changed_retry');
    }
    const all=[...entries.values()];return {files:all.slice(0,MAX_FILES),truncated:entries.size>MAX_FILES,aggregate:aggregate(all),...(branch===undefined?{}:{branch})};
  }
  function observation(root,options={}) {
    const scope=options.scope || 'uncommitted';
    const commits=options.commits || (options.commit ? [options.commit] : []);
    if(!Array.isArray(commits) || commits.length>100)throw new Error('invalid_commit_selection');
    const snapshot={scope,commits:commits.map(reference),ref:options.ref,baseRef:options.baseRef};
    if(snapshot.ref!==undefined)reference(snapshot.ref);
    if(snapshot.baseRef!==undefined)reference(snapshot.baseRef);
    const key=JSON.stringify([root,scope,[...snapshot.commits].sort(),snapshot.ref,snapshot.baseRef]);
    const cached=observations.get(key);
    if(cached && cached.expires>now()) {observations.delete(key);observations.set(key,cached);return cached.promise;}
    observations.delete(key);
    while(observations.size>=8)observations.delete(observations.keys().next().value);
    const entry={expires:Infinity,promise:null};
    entry.promise=(async()=>{const value=await plan(root,snapshot),listed=value.scope==='commit'?await selectionList(root,value):await list(root,value);entry.expires=now()+1000;return {value,listed};})().catch(error=>{if(observations.get(key)===entry)observations.delete(key);throw error;});
    observations.set(key,entry);return entry.promise;
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
      let baseline;
      try {const value=await defaultBase(root);baseline={defaultBaseRef:value.baseRef,defaultBaseSource:value.baseSource};}
      catch(error){baseline={defaultBaseRef:null,defaultBaseSource:null,baseError:errorResult(error).error};}
      return {ok:true,root,headOid,branches:rows.slice(0,200),truncated:rows.length>200 || fields(raw,5).length>200,...baseline};
    } catch(error) {return errorResult(error);}
  }
  async function history(target,options={}) {
    try {
      const root=await gitRoot(target),limit=bound(options.limit,50,100,1),offset=bound(options.offset,0,10000);
      let historyOids,excludeOid='',comparison;
      if(options.historyOids!==undefined) {
        if(!Array.isArray(options.historyOids) || !options.historyOids.length || options.historyOids.length>200 || options.historyOids.some(oid=>typeof oid!=='string' || !oidPattern.test(oid)))throw new Error('invalid_history_snapshot');
        historyOids=[...new Set(options.historyOids)];
        if(options.excludeOid){if(!oidPattern.test(options.excludeOid))throw new Error('invalid_history_snapshot');excludeOid=options.excludeOid;}
        const verified=(await run(root,['rev-list','--no-walk=unsorted',...historyOids,'--'])).stdout.trim().split(/\r?\n/);
        if(verified.length!==historyOids.length || verified.some(oid=>!historyOids.includes(oid)))throw new Error('invalid_history_snapshot');
      } else if(options.allBranches) {
        const raw=(await run(root,['for-each-ref','--format=%(objectname)%00','refs/heads','refs/remotes'])).stdout;
        historyOids=[...new Set(fields(raw,1).map(row=>row[0]))];
        if(historyOids.length>200)throw new Error('too_many_branch_tips_for_history');
        if(historyOids.some(oid=>!oidPattern.test(oid)))throw new Error('invalid_history_snapshot');
      } else {
        const resolvedOid=options.ref && options.ref!=='HEAD' ? await resolve(root,options.ref) : await head(root);
        historyOids=resolvedOid?[resolvedOid]:[];
        if(resolvedOid && options.branchOnly!==false) {
          const value=await plan(root,{scope:'branch',ref:options.ref || 'HEAD',baseRef:options.baseRef});
          comparison=value.comparison;historyOids=[value.headOid];excludeOid=value.baseOid;
        }
      }
      if(!historyOids.length)return {ok:true,root,resolvedOid:'',historyOids:[],excludeOid,commits:[],limit,offset,nextOffset:null,truncated:false};
      const raw=(await run(root,['log','--topo-order','--no-show-signature','--no-decorate',`--max-count=${limit+1}`,`--skip=${offset}`,'--format=%H%x00%P%x00%an%x00%ct%x00%s%x00',...historyOids,...(excludeOid?[`^${excludeOid}`]:[]),'--'])).stdout;
      const rows=fields(raw,5).map(([oid,parentText,authorName,date,subject])=>{
        const parents=parentText ? parentText.split(' ') : [];
        if(!oidPattern.test(oid) || parents.some(value=>!oidPattern.test(value)) || !/^\d+$/.test(date))throw new Error('invalid_git_history');
        return {oid,parents,authorName:authorName.slice(0,512),subject:subject.slice(0,2048),committedAt:Number(date)};
      });
      const page=rows.slice(0,limit);let stats;
      try {stats=page.length ? await showRecords(root,page.map(row=>row.oid),'--numstat') : new Map();}
      catch(error) {if(error.code!=='ERR_CHILD_PROCESS_STDIO_MAXBUFFER')throw error;}
      const commits=page.map(row=>{
        const values=stats?.get(row.oid);
        return {...row,...(values?aggregate([...values.values()]):{added:null,removed:null,fileCount:null,binaryFiles:null}),statsComplete:!!values};
      });
      return {ok:true,root,resolvedOid:historyOids.length===1?historyOids[0]:'',historyOids,excludeOid,...(comparison?{comparison}:{}),allBranches:!!options.allBranches,commits,limit,offset,nextOffset:rows.length>limit?offset+limit:null,truncated:rows.length>limit};
    } catch(error) {return errorResult(error);}
  }
  async function files(target,options={}) {
    try {const root=await gitRoot(target),{value,listed}=await observation(root,options);const {commitFiles,...visible}=listed;return {ok:true,root,...identity(value),...visible,aggregate:{...listed.aggregate},files:listed.files.map(row=>({...row,...(row.commitOids?{commitOids:[...row.commitOids]}:{})}))};}
    catch(error) {return errorResult(error);}
  }
  async function diff(target,file,options={}) {
    try {
      const root=await gitRoot(target),selected=safePath(root,file),context=bound(options.context,3,MAX_CONTEXT),{value,listed}=await observation(root,options);
      const row=listed.files.find(row=>row.path===selected);
      if(!row)throw new Error(listed.truncated?'review_file_list_truncated':'file_not_in_review_scope');
      if(value.scope==='commit') {
        const candidates=value.segments.filter(segment=>listed.commitFiles.get(segment.headOid).has(selected));
        const sections=[];let characters=0,omittedSections=0;
        for(const segment of candidates) {
          if(characters && 256*1024-characters<512){omittedSections++;continue;}
          const meta=listed.commitFiles.get(segment.headOid).get(selected);
          let projection;
          try {projection=projectGitDiff((await run(root,diffArgs(segment,['--patch',`--unified=${context}`],[...(meta.originalPath?[meta.originalPath]:[]),selected]))).stdout);}
          catch(error){if(error.code!=='ERR_CHILD_PROCESS_STDIO_MAXBUFFER' || !error.stdout)throw error;projection=projectGitDiff(error.stdout,true);}
          const remaining=256*1024-characters;
          if(projection.diff.length>remaining)projection={...projection,diff:projection.diff.slice(0,remaining),truncated:true};
          characters+=projection.diff.length;
          sections.push({oid:segment.headOid,baseOid:segment.baseOid,fullContents:false,...projection});
        }
        return {ok:true,root,...identity(value),context,fullContents:false,sections,omittedSections,
          diff:sections.length===1?sections[0].diff:'',binary:row.binary,truncated:!!omittedSections || sections.some(row=>row.truncated),
          originalBytes:sections.reduce((sum,row)=>sum+row.originalBytes,0),originalBytesExact:!omittedSections && sections.every(row=>row.originalBytesExact)};
      }
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
  return {branches,history,files,diff,invalidate:()=>observations.clear()};
}
module.exports={createGitReviewReader,MAX_CONTEXT,MAX_FILES};
