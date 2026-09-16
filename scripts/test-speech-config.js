'use strict';
const assert = require('node:assert/strict'), path = require('node:path'), Module = require('node:module');
const {buildSync} = require('esbuild');
const root = path.resolve(__dirname, '..');
const output = buildSync({stdin: {contents: 'export * from "./generalStore";',
  resolveDir: path.join(root, 'frontend/main-deck/src'), loader: 'ts'}, bundle: true,
  platform: 'node', format: 'cjs', external: ['react', 'react-dom'], write: false, logLevel: 'silent'}).outputFiles[0].text;
const compiled = new Module(__filename + '.bundle', module);
compiled.filename = __filename; compiled.paths = module.paths; compiled._compile(output, __filename);
const {ingestGeneral: ingest, getGeneralState: state} = compiled.exports;
const engine = key => ({type: 'engine', voice: {stt: {}, tts: {provider: 'kokoro', config_key: key, available: true}}});
ingest(engine('server-a'));
ingest({type: 'tts:voices', provider: 'kokoro', config_key: 'server-a', items: ['voice-a'], error: 'old error'});
assert.equal(state().voices[0].id, 'voice-a');
ingest(engine('server-b'));
assert.deepEqual(state().voices, []);
assert.equal(state().voicesError, '');
assert.equal(state().voicesLoaded, false);
ingest({type: 'tts:voices', provider: 'kokoro', config_key: 'server-a', items: ['stale']});
assert.deepEqual(state().voices, []);
ingest({type: 'tts:voices', provider: 'kokoro', config_key: 'server-b', items: ['voice-b']});
assert.equal(state().voices[0].id, 'voice-b');
ingest({type: 'hello'});
assert.equal(state().voices[0].id, 'voice-b', 'unrelated hello must preserve complete voice state');
console.log('Speech configuration: same-provider endpoint changes clear stale lists/errors and reject delayed replies');
