'use strict';
// Exercise real registered IPC handlers with native side effects replaced.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const vm = require('node:vm');
const {execFile, execFileSync} = require('node:child_process');
const {createRequire} = require('node:module');
const root = path.resolve(__dirname, '..');
const file = path.join(root, 'electron-deck-ipc.js');
const localRequire = createRequire(file);
const {projectGitDiff,COMMAND_BYTES}=require('../electron-workbench-diff');
assert.equal(COMMAND_BYTES,2*1024*1024);
assert.equal(projectGitDiff('Binary files a/image and b/image differ\n').binary,true);
assert.equal(projectGitDiff('+Binary files a/image and b/image differ\n').binary,false);
const bounded=projectGitDiff('diff header\n'+'+sample\n'.repeat(50000));
assert.ok(bounded.truncated && bounded.diff.length<=256*1024);
assert.equal(bounded.originalBytesExact,true);
assert.equal(projectGitDiff('Partial output',true).originalBytesExact,false);
const handlers = new Map();
const opens = [], reveals = [];
const deck = {}, monitor = {}, overlay = {};
let picks = 0;
let saves=0,saveChoice={canceled:true},saveOptions;
let readInvocations = null;
function fixtureExecFile(executable, args, options, callback) {
  if (readInvocations) {
    assert.equal(executable, 'git');
    assert.ok(args.includes('core.fsmonitor=false'));
    assert.equal(options.env.GIT_PAGER, 'cat');
    assert.ok(args.includes('core.hooksPath=' + (process.platform === 'win32' ? 'NUL' : '/dev/null')));
    if (args.some(arg => ['show', 'diff-tree', 'diff'].includes(arg))) {
      assert.ok(args.includes('--no-ext-diff') && args.includes('--no-textconv'));
    }
    readInvocations.push(args);
  }
  return execFile(executable, args, options, callback);
}
const moduleResult = {exports: {}};
vm.runInNewContext(fs.readFileSync(file, 'utf8'), {
  module: moduleResult, exports: moduleResult.exports,
  require: id => id === 'electron' ? {
    ipcMain: {handle: (name, fn) => handlers.set(name, fn), on: () => {}},
    BrowserWindow: {fromWebContents: sender => sender},
    dialog: {showOpenDialog: async () => {picks++; return {canceled: true};},showSaveDialog:async(_owner,options)=>{saves++;saveOptions=options;return saveChoice;}},
    shell: {openPath: async value => {opens.push(value); return '';}, showItemInFolder: value => reveals.push(value)},
  } : id === 'child_process' ? {execFile: fixtureExecFile} : localRequire(id),
  __dirname: root, process, Buffer, console, setTimeout, clearTimeout,
}, {filename: file});
moduleResult.exports.registerDeckIpc({app: {}, appRoot: root,
  getDeckWindow: () => deck, getMonitorWindow: () => monitor,
  isTrustedIpcSender: (event, expected) => !!expected && event.sender === expected,
  readSettings: () => ({language: 'en'}),
});

(async () => {
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'variant1-ipc-review-'));
  try {
    const chooseExport=handlers.get('context-export:choose-path');
    assert.ok(chooseExport);
    assert.equal((await chooseExport({sender:overlay},'jsonl')).error,'untrusted_sender');
    assert.equal((await chooseExport({sender:deck},'yaml')).error,'invalid_export_format');assert.equal(saves,0);
    assert.equal((await chooseExport({sender:deck},'jsonl')).cancelled,true);assert.equal(saveOptions.defaultPath,'session-context.jsonl');
    const exportPath=path.join(scratch,'context.jsonl');fs.writeFileSync(exportPath,'Fixture');saveChoice={canceled:false,filePath:exportPath};
    const selectedExport=await chooseExport({sender:deck},'jsonl');assert.equal(selectedExport.path,exportPath);assert.equal(selectedExport.overwrite,true);
    let resolveSave;saveChoice=new Promise(resolve=>{resolveSave=resolve;});const choosing=chooseExport({sender:deck},'markdown');
    assert.equal((await chooseExport({sender:deck},'markdown')).error,'save_dialog_busy');resolveSave({canceled:true});await choosing;
    for (const channel of ['branches','history','review-files','review-diff']) {
      const handler=handlers.get(`workbench:git:${channel}`);
      assert.ok(handler,`${channel} read is registered`);
      assert.equal((await handler({sender:overlay},scratch,{})).error,'untrusted_sender');
      assert.equal((await handler({sender:deck},'relative/path',{})).error,'invalid_path');
    }
    const repo = fs.mkdtempSync(path.join(scratch, 'git-'));
    const git = (...args) => execFileSync('git', ['-c', 'core.hooksPath=' + (process.platform === 'win32' ? 'NUL' : '/dev/null'), ...args], {
      cwd: repo, windowsHide: true, encoding: 'utf8', env: {...process.env, GIT_CONFIG_NOSYSTEM:'1', GIT_CONFIG_GLOBAL:process.platform === 'win32' ? 'NUL' : '/dev/null'},
    });
    const mutate = async (action, file) => {
      const result = await handlers.get('workbench:git:run')({sender:deck}, action, repo, {file});
      assert.equal(result.ok, true, result.error);
    };
    git('init', '-q');
    git('config', 'core.autocrlf', 'false');
    const oldPath = 'First name.txt', newPath = 'Renamed file.txt';
    fs.writeFileSync(path.join(repo, oldPath), 'first\n');
    git('add', '--', oldPath);
    await mutate('unstage', oldPath);
    assert.equal(git('ls-files'), '', 'an unborn branch can unstage an initial file');
    assert.equal(fs.readFileSync(path.join(repo, oldPath), 'utf8'), 'first\n');
    git('add', '--', oldPath);
    git('-c','user.name=Deck fixture','-c','user.email=fixture@example.test','-c','commit.gpgSign=false','commit','-qm','Initial');
    fs.writeFileSync(path.join(repo, oldPath), 'working copy\n');
    git('config', 'core.fsmonitor', 'true');
    readInvocations = [];
    try {
      const status = await handlers.get('workbench:git:status')({sender:deck}, repo);
      assert.equal(status.ok, true, status.error);
      const file = status.files.find(row => row.path === oldPath);
      assert.deepEqual([file.status, file.added, file.removed], [' M', 1, 1]);
      const diff = await handlers.get('workbench:git:review-diff')({sender:deck}, repo, oldPath, {scope:'unstaged'});
      assert.equal(diff.ok, true, diff.error);
      assert.match(diff.diff, /\+working copy/);
      const history = await handlers.get('workbench:git:history')({sender:deck}, repo, {allBranches:true, limit:5});
      assert.equal(history.ok, true, history.error);
      assert.equal(history.commits.length, 1);
      const command = readInvocations.find(args => args.includes('status'));
      assert.equal(git(...command.slice(0, command.indexOf('status')), 'config', '--bool', '--get', 'core.fsmonitor').trim(), 'false');
      assert.equal(git('config', '--local', '--bool', '--get', 'core.fsmonitor').trim(), 'true');
    } finally {
      readInvocations = null;
      git('config', '--unset', 'core.fsmonitor');
    }
    fs.writeFileSync(path.join(repo, oldPath), 'staged version\n');
    git('add', '--', oldPath);
    fs.unlinkSync(path.join(repo, oldPath));
    await mutate('revert', oldPath);
    assert.equal(fs.readFileSync(path.join(repo, oldPath), 'utf8'), 'staged version\n', 'discard restores a tracked deletion from the index');
    assert.equal(git('show', ':' + oldPath), 'staged version\n');
    assert.equal(git('diff','--cached','--name-only').trim(), oldPath);
    git('restore', '--staged', '--worktree', '--', oldPath);
    git('mv', '--', oldPath, newPath);
    await mutate('unstage', newPath);
    assert.equal(git('diff', '--cached', '--name-only'), '', 'unstaging a rename resets both sides');
    assert.equal(fs.existsSync(path.join(repo, oldPath)), false);
    assert.equal(fs.readFileSync(path.join(repo, newPath), 'utf8'), 'first\n');
    const executable = path.join(scratch, 'sample.cmd');
    fs.writeFileSync(executable, 'This fixture must never be executed.');
    for (const sender of [overlay, monitor]) {
      assert.equal(handlers.get('settings:get')({sender}), null);
      assert.equal(await handlers.get('dialog:pickFolder')({sender}), null);
      assert.equal((await handlers.get('localPath:open')({sender}, executable)).ok, false);
    }
    assert.equal(picks, 0);
    assert.equal(handlers.get('settings:get')({sender: deck}).language, 'en');
    await handlers.get('dialog:pickFolder')({sender: deck});
    assert.equal(picks, 1);
    assert.equal((await handlers.get('localPath:open')({sender: deck}, executable)).ok, true);
    assert.deepEqual(reveals, [executable]);
    assert.equal(opens.length, 0, 'file reveal must never launch its file association');
    assert.equal((await handlers.get('localPath:open')({sender: deck}, scratch)).ok, true);
    assert.deepEqual(opens, [scratch]);
    assert.equal((await handlers.get('localPath:open')({sender: deck}, path.join(scratch, 'missing'))).ok, false);
    console.log('Grok review IPC: file reveal, directory opening, missing path, and Deck-only settings/folder access passed');
  } finally {
    // This directory is created and owned by this fixture only.
    assert.equal(path.dirname(path.resolve(scratch)), path.resolve(os.tmpdir()));
    assert.ok(path.basename(scratch).startsWith('variant1-ipc-review-'));
    fs.rmSync(scratch, {recursive: true, force: true});
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
