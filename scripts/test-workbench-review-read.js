'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const os=require('node:os');
const {execFile}=require('node:child_process');
const {promisify}=require('node:util');
const execute=promisify(execFile);
const {createGitReviewReader,MAX_CONTEXT}=require('../electron-workbench-review');
const {projectGitDiff,COMMAND_BYTES}=require('../electron-workbench-diff');

function repositoryFile(root,raw) {
  if(typeof raw!=='string' || !raw || raw.includes('\0') || path.isAbsolute(raw) || path.win32.isAbsolute(raw) || /^[a-z]:/i.test(raw) || raw.split(/[\\/]/).includes('..'))throw new Error('invalid_repository_file');
  const absolute=path.resolve(root,raw),relative=path.relative(root,absolute);
  if(!relative || relative==='..' || relative.startsWith('..'+path.sep) || path.isAbsolute(relative))throw new Error('invalid_repository_file');
  return {absolute,relative:relative.split(path.sep).join('/')};
}

(async()=>{
  const temporary=await fs.mkdtemp(path.join(os.tmpdir(),'variant1-review-read-'));
  const root=path.join(temporary,'repo');await fs.mkdir(root);
  const calls=[];
  const command=async(args,options={})=>execute('git',args,{cwd:root,windowsHide:true,encoding:'utf8',env:{...process.env,GIT_CONFIG_NOSYSTEM:'1',GIT_CONFIG_GLOBAL:process.platform==='win32'?'NUL':'/dev/null',GIT_OPTIONAL_LOCKS:'0',GIT_TERMINAL_PROMPT:'0',GIT_LITERAL_PATHSPECS:'1'},...options});
  const readGit=async(args,options)=>{calls.push(args);return command(args,options);};
  const reader=createGitReviewReader({readGit,gitRoot:async()=>root,repositoryFile,projectGitDiff});
  const git=async(...args)=>(await command(args)).stdout.trim();
  const put=(file,text)=>fs.writeFile(path.join(root,file),text);
  try {
    await git('init','-b','main');await git('config','user.name','Review fixture');await git('config','user.email','fixture@example.test');await git('config','commit.gpgSign','false');await git('config','core.autocrlf','false');
    assert.deepEqual((await reader.history(root)).commits,[],'unborn history is explicit');
    await put('new file.txt','Initial\n');await git('add','--','new file.txt');
    assert.equal((await reader.files(root,{scope:'staged'})).files[0].status,'A');
    assert.equal((await reader.diff(root,'new file.txt',{scope:'uncommitted'})).fullContents,true,'unborn worktree includes staged initial content');
    const baseLines=Array.from({length:300},(_,i)=>`line ${i}`).join('\n')+'\n';
    await put('new file.txt',baseLines);await put('binary.bin',Buffer.from([1,0,2,3]));
    await git('add','--all');await git('commit','-m','Root commit');const first=await git('rev-parse','HEAD');
    const rootFiles=await reader.files(root,{scope:'commit',commits:[first]});assert.equal(rootFiles.ok,true);assert.equal(rootFiles.baseOid,'');assert.equal(rootFiles.files.find(row=>row.path==='binary.bin').binary,true);
    const rootPatch=await reader.diff(root,'new file.txt',{scope:'commit',commits:[first]});assert.equal(rootPatch.ok,true);assert.match(rootPatch.diff,/new file mode/);
    await git('mv','new file.txt','renamed file.txt');await put('renamed file.txt',baseLines.replace('line 150\n','changed 150\n'));await git('add','--all');await git('commit','-m','Rename and edit');const second=await git('rev-parse','HEAD');
    await put('other.txt','third\n');await git('add','--all');await git('commit','-m','Third commit');const third=await git('rev-parse','HEAD');
    await git('branch','side',first);await git('checkout','side');await put('side.txt','side\n');await git('add','--all');await git('commit','-m','Side commit');const side=await git('rev-parse','HEAD');await git('checkout','main');await git('merge','--no-ff','side','-m','Merge side');const merge=await git('rev-parse','HEAD');
    const headBefore=await git('rev-parse','HEAD');
    const branches=await reader.branches(root);assert.equal(branches.ok,true);assert.ok(branches.branches.find(row=>row.name==='main' && row.current));
    const history=await reader.history(root,{ref:'refs/heads/main',limit:2});assert.equal(history.ok,true);assert.equal(history.truncated,true);assert.equal(history.nextOffset,2);assert.equal(history.commits[0].parents.length,2);
    const more=await reader.history(root,{ref:history.resolvedOid,limit:2,offset:history.nextOffset});assert.deepEqual(more.commits.map(row=>row.oid),[second,first],'first-parent pagination pins one lineage');
    const alternative=await reader.history(root,{ref:'refs/heads/side'});assert.equal(alternative.commits[0].oid,side);
    assert.equal((await reader.history(root,{ref:'--all'})).ok,false);assert.equal((await reader.history(root,{offset:-1})).ok,false);
    const continuous=await reader.files(root,{scope:'commit',commits:[second,third]});assert.equal(continuous.ok,true);assert.equal(continuous.baseOid,first);assert.equal(continuous.headOid,third);assert.deepEqual(continuous.commits,[third,second]);
    assert.match((await reader.files(root,{scope:'commit',commits:[third,first]})).error,/noncontiguous/);
    assert.match((await reader.files(root,{scope:'commit',commits:[first,first]})).error,/duplicate/);
    assert.match((await reader.files(root,{scope:'commit',commits:[merge,side]})).error,/noncontiguous/);
    assert.equal((await reader.files(root,{scope:'commit',commits:[merge,third]})).baseOid,second);
    const range=await reader.files(root,{scope:'branch',ref:'main',baseRef:'side'});assert.equal(range.ok,true);assert.equal(range.baseOid,side);assert.equal(range.headOid,merge);
    assert.match((await reader.files(root,{scope:'branch',ref:'main'})).error,/explicit_base/);
    const renamed=continuous.files.find(row=>row.path==='renamed file.txt');assert.equal(renamed.originalPath,'new file.txt');assert.equal(renamed.added,1);assert.equal(renamed.removed,1);
    const small=await reader.diff(root,'renamed file.txt',{scope:'commit',commits:[second],context:3});const expanded=await reader.diff(root,'renamed file.txt',{scope:'commit',commits:[second],context:1000});assert.match(small.diff,/rename from new file.txt/);assert.ok(expanded.diff.length>small.diff.length);assert.equal((await reader.diff(root,'renamed file.txt',{scope:'commit',commits:[second],context:99999})).context,MAX_CONTEXT);
    await put('renamed file.txt',baseLines.replace('line 150\n','staged 150\n'));await git('add','--','renamed file.txt');await put('renamed file.txt',baseLines.replace('line 150\n','working 150\n'));await put('fresh.txt','Untracked\n');await put('new binary.bin',Buffer.from([2,0,4]));
    const indexBefore=await fs.readFile(path.join(root,'.git','index'));
    const staged=await reader.diff(root,'renamed file.txt',{scope:'staged'});const unstaged=await reader.diff(root,'renamed file.txt',{scope:'unstaged'});const total=await reader.diff(root,'renamed file.txt',{scope:'uncommitted'});
    assert.match(staged.diff,/\+staged 150/);assert.match(unstaged.diff,/-staged 150/);assert.match(total.diff,/-changed 150/);assert.match(total.diff,/\+working 150/);
    assert.ok(!(await reader.files(root,{scope:'staged'})).files.some(row=>row.untracked));assert.ok((await reader.files(root,{scope:'unstaged'})).files.find(row=>row.path==='fresh.txt').untracked);
    assert.equal((await reader.diff(root,'fresh.txt',{scope:'uncommitted'})).fullContents,true);const binary=await reader.diff(root,'new binary.bin',{scope:'unstaged'});assert.equal(binary.binary,true);assert.equal(binary.originalBytes,3);
    assert.equal((await reader.diff(root,'../outside',{scope:'uncommitted'})).ok,false);assert.equal((await reader.diff(root,'C:\\outside',{scope:'uncommitted'})).ok,false);assert.equal((await reader.diff(root,'other.txt',{scope:'uncommitted'})).ok,false);
    assert.deepEqual(await fs.readFile(path.join(root,'.git','index')),indexBefore,'scope reads leave the staged index untouched');
    await put('large.txt','large line\n'.repeat(300000));const large=await reader.diff(root,'large.txt',{scope:'uncommitted'});assert.equal(large.ok,true);assert.equal(large.truncated,true);assert.equal(large.originalBytesExact,false);assert.ok(large.diff.length<=256*1024);
    await put('other.txt','patch line\n'.repeat(300000));const largeTracked=await reader.diff(root,'other.txt',{scope:'unstaged'});assert.equal(largeTracked.ok,true);assert.equal(largeTracked.truncated,true);assert.equal(largeTracked.originalBytesExact,false);
    // An inherited/custom diff program must never execute for Review reads.
    await git('config','diff.external','variant1-review-must-not-run');await git('config','diff.fixture.textconv','variant1-review-must-not-run');await put('.gitattributes','*.txt diff=fixture\n');assert.equal((await reader.diff(root,'renamed file.txt',{scope:'staged'})).ok,true);
    assert.equal(await git('rev-parse','HEAD'),headBefore,'read selection never checks out a branch');
    assert.ok(calls.every(args=>!args.includes('checkout') && !args.includes('switch') && !args.includes('fetch')),'helper has no repository mutation commands');
    assert.ok(calls.every(args=>args[0]==='--no-pager' && args[1]==='--literal-pathspecs'));
    console.log('Review reads: bounded branch/first-parent history, truthful commit ranges, root/merge commits, scope semantics, rename/context, untracked/binary/large output and inert Git programs passed');
  } finally {
    assert.equal(path.dirname(path.resolve(temporary)),path.resolve(os.tmpdir()));assert.ok(path.basename(temporary).startsWith('variant1-review-read-'));
    await fs.rm(temporary,{recursive:true,force:true});
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
