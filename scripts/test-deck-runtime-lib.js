'use strict';

const assert = require('assert');
const Module = require('module');
const path = require('path');
const {buildSync} = require('esbuild');

const root = path.join(__dirname, '..');
const entry = path.join(root, 'frontend', 'main-deck', 'src', 'chat', 'runtimeLib.ts');
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
const lib = compiled.exports.default;
const messageListSource = require('fs').readFileSync(path.join(
  root, 'frontend', 'main-deck', 'src', 'chat', 'ChatMessageList.tsx',
), 'utf8');
const activitySource = require('fs').readFileSync(path.join(
  root, 'frontend', 'main-deck', 'src', 'chat', 'TurnActivity.tsx',
), 'utf8');
const timelineSource = require('fs').readFileSync(path.join(
  root, 'frontend', 'main-deck', 'src', 'chat', 'ConversationTimeline.tsx',
), 'utf8');

assert.deepStrictEqual(Object.keys(lib).sort(), [
  'formatTime',
  'highlightCode',
  'parseMarkdown',
  'safeHttpUrl',
  'sumMessageHeights',
  'virtualWindow',
]);

assert.strictEqual(lib.safeHttpUrl('https://example.com/a').startsWith('https://example.com/a'), true);
assert.strictEqual(lib.safeHttpUrl('javascript:alert(1)'), '');

// Virtual list math (spacers + overscan).
assert.strictEqual(lib.sumMessageHeights([10, 20, 30], 0, 3, 96), 60);

const virt = lib.virtualWindow({
  total: 100,
  scrollTop: 0,
  viewportHeight: 400,
  heights: null,
  defaultHeight: 100,
  overscan: 2,
});
assert.strictEqual(virt.start, 0);
assert.ok(virt.end >= 4 && virt.end <= 10, `expected a short top window, got end=${virt.end}`);
assert.strictEqual(virt.topPad, 0);
assert.ok(virt.bottomPad > 0);
assert.strictEqual(virt.totalHeight, 10000);

const mid = lib.virtualWindow({
  total: 100,
  scrollTop: 5000,
  viewportHeight: 400,
  heights: null,
  defaultHeight: 100,
  overscan: 2,
});
assert.ok(mid.start > 0 && mid.end < 100);
assert.ok(mid.topPad > 0 && mid.bottomPad > 0);
assert.ok(mid.end - mid.start < 20, 'mid window should only mount a slice, not the full list');

const blocks = lib.parseMarkdown([
  '# Heading',
  '',
  '> quoted **text**',
  '',
  '| Name | Value |',
  '| --- | ---: |',
  '| A | 1 |',
  '',
  '1. Parent',
  '   - Child',
  '',
  '```js',
  'const answer = 42;',
  '```',
].join('\n'));

assert.deepStrictEqual(blocks.map(block => block.type), ['heading', 'blockquote', 'table', 'list', 'code']);
assert.strictEqual(blocks[2].rows[0][1][0].text, '1');
assert.strictEqual(blocks[3].items[0].children[0].type, 'list');
assert.strictEqual(blocks[4].language, 'js');
assert.strictEqual(lib.highlightCode('const answer = 42;', 'js').some(token => token.type === 'keyword'), true);

// Bare autolinks must not recurse forever in long reports.
const bare = lib.parseMarkdown('see https://example.com/foo for more')[0].lines[0];
assert.strictEqual(bare.some(t => t.type === 'link' && t.href.startsWith('https://example.com/foo')), true);
assert.strictEqual(
  bare.find(t => t.type === 'link').children[0].type,
  'text',
  'bare URL link children must be text (no re-parse)',
);

// URL used as markdown link label (same recursion trap if bare path is wrong).
const urlLabel = lib.parseMarkdown('[https://example.com/a](https://example.com/b)')[0].lines[0];
assert.strictEqual(urlLabel.some(t => t.type === 'link'), true);

// Source-heavy markdown with many bare URLs and tables must parse.
const reportLines = [];
reportLines.push('# Report');
for (let i = 1; i <= 40; i += 1) {
  reportLines.push(`## Section ${i}`);
  reportLines.push(`Inkling is **multimodal** from [Lab](https://example.com/x${i}) — see https://example.com/y${i}.`);
  reportLines.push(`- bullet with https://example.com/z${i}`);
  reportLines.push('| A | B |');
  reportLines.push('| --- | --- |');
  reportLines.push('| **a** | *b* |');
}
const reportBlocks = lib.parseMarkdown(reportLines.join('\n'));
assert.ok(reportBlocks.length > 10, `expected many blocks, got ${reportBlocks.length}`);
assert.ok(reportBlocks.some(b => b.type === 'heading'));
assert.ok(reportBlocks.some(b => b.type === 'table'));

assert.match(
  messageListSource,
  /turnWasActiveRef[\s\S]*if\s*\(active\s*&&\s*!turnWasActiveRef\.current\)/,
  'a newly active turn may opt into bottom anchoring',
);
assert.doesNotMatch(
  messageListSource,
  /\[streaming,\s*turnActive,\s*streamText/,
  'stream tokens must not forcibly restore bottom anchoring after user scroll',
);

// The transcript virtualizes coherent turns rather than scattering a prompt,
// activity, and streaming tail across independent rows.
assert.match(
  messageListSource,
  /function buildTranscriptTurns[\s\S]*if \(!current \|\| current\.assistants\.length\)[\s\S]*current\.users\.push/,
  'consecutive active inputs should remain in one visual conversation turn',
);
assert.match(
  messageListSource,
  /const total = turns\.length/,
  'virtualization should measure visual turns, not individual message bubbles',
);
assert.match(
  messageListSource,
  /chat-turn__prompts[\s\S]*<TurnActivity[\s\S]*chat-turn__responses/,
  'a turn should render prompt, activity, then response in reading order',
);
assert.match(
  activitySource,
  /function TraceEntry[\s\S]*aria-expanded=\{hasDetails \? open/,
  'each activity entry must provide its own accessible disclosure',
);
assert.match(activitySource, /rows\.map\(row =>[\s\S]*<TraceEntry row=\{row\}/,
  'the collapsible execution trace must retain recorded row order');
assert.match(
  activitySource,
  /className="trace-entry__related"[\s\S]*onClick=\{\(\) => revealPaneForStep\(step\)\}/,
  'tool activity should retain a semantic keyboard action to its related pane',
);
assert.match(activitySource, /step.kind === "thinking"[\s\S]*if \(!rows.length\) return live && !streamText/,
  'thought content and unnamed waits must remain distinct');
assert.match(timelineSource, /MIN_ENTRIES = 4[\s\S]*activeConversationIndex[\s\S]*170/,
  'long chats should get the bounded Hermes prompt navigator only after four prompts');
assert.doesNotMatch(messageListSource + activitySource, /TurnStepsList|WorkBeeper|chat-turn-activity/,
  'the old forced-open Activity card must stay deleted');
assert.doesNotMatch(
  messageListSource,
  /className="(?:chat-turn-live|virt-live-tail)"/,
  'live activity and streaming text should belong to their owning turn',
);

console.log('deck runtime lib: all tests passed');
