'use strict';

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const walk = directory => fs.readdirSync(path.join(root, directory), {withFileTypes: true})
  .flatMap(entry => {
    const relative = path.join(directory, entry.name);
    return entry.isDirectory() ? walk(relative) : [relative];
  });
const html = read('frontend/main-deck/index.html');
const loader = read('frontend/main-deck/dev/fixture-loader.js');
const fixture = read('frontend/main-deck/src/fixture.ts');
const main = read('frontend/main-deck/src/main.tsx');
const pkg = JSON.parse(read('package.json'));

assert.doesNotMatch(html, /fixtures-(?:seed|handlers)\.js/);
assert.doesNotMatch(html, /Welcome to VARIANT-1|Trip planning/,
  'production HTML contains no fixture payload');
assert.doesNotMatch(html, /dev\/fixture-loader\.js/,
  'the production shell must not load development files');
assert.match(main, /get\("fixture"\) === "1"/,
  'the development fixture is explicitly enabled by ?fixture=1');
assert.match(main, /fixtureLoader\.src = "\.\/dev\/fixture-loader\.js"/,
  'the platform requests the development loader only in fixture mode');
assert.match(loader, /get\("fixture"\) !== "1"/,
  'the development loader independently enforces ?fixture=1');
assert.match(loader, /dist\/fixture\.js/,
  'the development loader requests the typed fixture adapter');
assert.doesNotMatch(loader, /document\.write|fixtures-seed|fixtures-handlers/);
assert.match(fixture, /variant1:fixture-message/);
assert.match(fixture, /chat:sessions/);
assert.match(fixture, /chat:session/);
assert.match(main, /ingestDevelopmentMessage/);
assert.match(main, /setDevelopmentConnectionState/);
for (const source of [loader, fixture, main]) {
  assert.doesNotMatch(source, /prototype/i,
    'the development fixture mechanism must not retain prototype terminology');
}
for (const file of [
  'frontend/main-deck/index.html',
  ...walk('frontend/main-deck/dev'),
  ...walk('frontend/main-deck/src'),
].filter(file => /\.(?:css|html|js|ts|tsx)$/.test(file))) {
  assert.doesNotMatch(read(file), /prototype/i,
    `${file} must use product or fixture terminology`);
}

for (const excluded of [
  '!frontend/main-deck/dev/**/*',
  '!frontend/main-deck/dist/fixture.js',
  '!frontend/main-deck/dist/fixture.js.map',
]) {
  assert.ok(pkg.build.files.includes(excluded),
    `${excluded} must be excluded from packaged builds`);
}

assert.doesNotMatch(html, /variant1-runtime-[a-z-]+-root/);
assert.match(html, /id="variant1-react-root"/);

console.log('deck fixture hygiene: all tests passed');
