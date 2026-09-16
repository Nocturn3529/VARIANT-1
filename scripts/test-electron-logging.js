'use strict';

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const {
  createLogger,
  classifyLogMessage,
  includeInHistoryLog,
  includeInSessionLog,
  LOG_HISTORY,
  LOG_SESSION,
} = require('../electron-logging');

function testClassificationAndPolicy() {
  let record = classifyLogMessage('[backend] [cloud] request complete');
  assert.strictEqual(record.source, 'backend.cloud');
  assert.strictEqual(record.level, 'info');

  record = classifyLogMessage(
    '[backend] [op] level=warn source=mutation event=probation_transition status=failed',
  );
  assert.strictEqual(record.source, 'mutation');
  assert.strictEqual(record.level, 'warn');
  assert.strictEqual(record.event, 'probation_transition');
  assert.match(record.message, /^probation_transition\b/);

  record = classifyLogMessage('[backend:err] Traceback: boom');
  assert.strictEqual(record.source, 'backend.stderr');
  assert.strictEqual(record.level, 'error');

  record = classifyLogMessage('[backend] failed token=private-value Bearer another-secret');
  assert.doesNotMatch(record.raw, /private-value|another-secret/);
  assert.match(record.raw, /token=<redacted>/);

  assert.strictEqual(
    classifyLogMessage('[renderer.deck] process gone reason=crashed exit_code=1').level,
    'error',
  );
  assert.strictEqual(
    classifyLogMessage('[backend] exited code=1 signal=null').level,
    'error',
  );

  assert.strictEqual(includeInHistoryLog('[backend] [ws] in type=ping'), false);
  assert.strictEqual(
    includeInHistoryLog('[backend] [activity] event=perception:quality_metrics score=1'),
    false,
  );
  assert.strictEqual(includeInHistoryLog('[renderer] animations.json loaded'), false);
  assert.strictEqual(
    includeInSessionLog('[backend] [op] level=info source=model event=request_start model=m'),
    true,
  );
  assert.strictEqual(includeInSessionLog('[backend:err] RuntimeError: failed'), true);
  assert.strictEqual(includeInSessionLog('[renderer:deck] ws connected'), false);
}

function testFilesAndStreamAssembly() {
  const logDir = fs.mkdtempSync(path.join(os.tmpdir(), 'variant1-logging-test-'));
  try {
    const logger = createLogger(logDir);
    logger.beginSessionLog();
    logger.logToFile('[backend] [ws] out type=activity');
    logger.logToFile('[renderer] Black Cat loaded');
    logger.logToFile('[backend] [op] level=info source=model event=request_start model=unit');
    logger.logToFile('[backend:err] RuntimeError: unit failure');
    logger.logBackendStream('[backend] ', '[op] level=info source=tool event=result tool=read');
    logger.logBackendStream('[backend] ', '_file status=ok\n');
    logger.logBackendStream('[backend] ', '[op] level=warn source=kernel event=retry');
    logger.flushLogStreams();

    const session = fs.readFileSync(path.join(logDir, LOG_SESSION), 'utf8');
    const history = fs.readFileSync(path.join(logDir, LOG_HISTORY), 'utf8');
    for (const text of [session, history]) {
      assert.doesNotMatch(text, /out type=activity/);
      assert.doesNotMatch(text, /Black Cat loaded/);
      assert.match(text, /\[INFO\] \[model\] request_start model=unit/);
      assert.match(text, /\[ERROR\] \[backend\.stderr\] RuntimeError: unit failure/);
      assert.match(text, /\[INFO\] \[tool\] result tool=read_file status=ok/);
      assert.match(text, /\[WARN\] \[kernel\] retry/);
    }
    assert.strictEqual((session.match(/result tool=read_file status=ok/g) || []).length, 1);
  } finally {
    fs.rmSync(logDir, {recursive: true, force: true});
  }
}

testClassificationAndPolicy();
testFilesAndStreamAssembly();
console.log('electron logging tests passed');
