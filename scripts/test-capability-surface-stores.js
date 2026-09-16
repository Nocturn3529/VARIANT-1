'use strict';

const assert = require('assert');
const fs = require('fs');
const Module = require('module');
const path = require('path');
const {buildSync} = require('esbuild');

const root = path.join(__dirname, '..');

function loadStore(file) {
  const entry = path.join(root, 'frontend', 'main-deck', 'src', file);
  const output = buildSync({
    entryPoints: [entry],
    bundle: true,
    platform: 'node',
    format: 'cjs',
    target: ['node20'],
    write: false,
    logLevel: 'silent',
  }).outputFiles[0].text;
  const compiled = new Module(entry, module);
  compiled.filename = entry;
  compiled.paths = Module._nodeModulePaths(path.dirname(entry));
  compiled._compile(output, entry);
  return compiled.exports;
}

const cases = [
  {
    file: 'pluginsStore.ts',
    setContext: 'setPluginsContext',
    refresh: 'refreshPlugins',
    setConnection: 'setPluginsConnection',
    getState: 'getPluginsState',
    pending: state => Object.keys(state.pending).length,
  },
];

for (const testCase of cases) {
  const store = loadStore(testCase.file);
  store[testCase.setContext]({send: () => true, notify: () => {}});

  for (const status of ['connecting', 'reconnecting', 'disconnected']) {
    store[testCase.refresh]();
    let state = store[testCase.getState]();
    assert.ok(testCase.pending(state) > 0,
      `${testCase.file} must track requests sent before ${status}`);

    store[testCase.setConnection](status);
    state = store[testCase.getState]();
    assert.strictEqual(state.connected, false,
      `${testCase.file} must mark ${status} as offline`);
    assert.strictEqual(testCase.pending(state), 0,
      `${testCase.file} must drop stale correlations on ${status}`);
    if ('loading' in state) {
      assert.strictEqual(state.loading, false,
        `${testCase.file} must stop loading on ${status}`);
    }
  }
}

const pluginStore = loadStore('pluginsStore.ts');
const sent = [];
pluginStore.setPluginsContext({
  send: payload => { sent.push(payload); return true; },
  notify: () => {},
});

assert.strictEqual(pluginStore.refreshPlugins(), true);
const listCommand = sent.at(-1);
pluginStore.ingestPlugins({
  type: 'extension-v2:accepted',
  request_id: listCommand.request_id,
  operation: 'list',
  result: [{
    package_id: 'demo.plugin',
    name: 'Demo',
    version: '1.0.0',
    active: true,
    description: 'One compact plugin.',
    contribution_count: 2,
    contribution_kinds: ['capabilities', 'skills'],
    status: 'ready',
  }],
});
let pluginState = pluginStore.getPluginsState();
assert.strictEqual(pluginState.plugins.length, 1);
assert.deepStrictEqual(pluginState.plugins[0].contribution_kinds, ['capabilities', 'skills']);

assert.strictEqual(pluginStore.setPluginEnabled('demo.plugin', false), true);
const toggleCommand = sent.at(-1);
assert.strictEqual(toggleCommand.type, 'extension-v2:set-enabled');
assert.strictEqual(toggleCommand.enabled, false);
pluginStore.ingestPlugins({
  type: 'extension-v2:accepted',
  request_id: toggleCommand.request_id,
  operation: 'set-enabled',
  result: {package_id: 'demo.plugin', active: false},
});
assert.strictEqual(sent.at(-1).type, 'extension-v2:list');

const pluginView = fs.readFileSync(path.join(
  root, 'frontend', 'main-deck', 'src', 'PluginsSettings.tsx',
), 'utf8');
assert.match(pluginView, /Open folder/);
assert.match(pluginView, /Rescan/);
assert.match(pluginView, /Search plugins/);
assert.doesNotMatch(pluginView, /Package digest|Catalog revision|Worker launch contract|Curator/,
  'the Settings plugin manager must not expose internal package machinery');

console.log(`capability surface stores: ${cases.length} disconnect lifecycle + lean plugin manager passed`);

const platform = loadStore('store.ts'), platformCommands = [];
platform.setContext({send: command => {platformCommands.push(command);return true;},notify(){}});
platform.ingest({type:'inference:platform',targets:[{id:'runtime-target'}],install_jobs:[],local_runtime:{installed:false}});
const install = {id:'install-1',runtime_id:'llamacpp',operation:'install',target_id:'runtime-target',status:'running',progress:25};
platform.ingest({type:'inference:install:job',...install});
assert.equal(platform.getPlatformState().config.inference_platform.install_jobs[0].progress,25);
platform.ingest({type:'inference:install:job',...install,progress:65});
assert.equal(platform.getPlatformState().config.inference_platform.install_jobs.length,1);
assert.equal(platform.getPlatformState().config.inference_platform.install_jobs[0].progress,65);
assert.equal(platformCommands.length,0,'progress events do not request expensive platform round trips');
assert.equal(platform.getPlatformState().config.inference_platform.targets[0].id,'runtime-target');
platform.ingest({type:'inference:install:job',...install,status:'done',progress:100});
platform.ingest({type:'inference:install:job',...install,status:'done',progress:100});
assert.equal(platformCommands.length,1,'one terminal transition refreshes runtime facts');
platform.ingest({type:'inference:install:jobs',items:[{...install,id:'install-2',progress:110}]});
assert.equal(platform.getPlatformState().config.inference_platform.install_jobs[0].id,'install-2');
assert.equal(platform.getPlatformState().config.inference_platform.install_jobs[0].progress,100);
assert.equal(platformCommands.length,1,'job list projects its payload directly');
console.log('Install jobs: direct progress/list projection and single terminal refresh passed');
