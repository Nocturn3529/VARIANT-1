'use strict';

/**
 * Source-aware local logging for the Electron host and backend streams.
 *
 * main0.log: concise operational session view.
 * main1.log: filtered diagnostic history.
 * data/traces/events.jsonl: full structured model/tool/runtime trajectory.
 */

const path = require('path');
const fs = require('fs');

const LOG_SESSION = 'main0.log';
const LOG_HISTORY = 'main1.log';
const LOG_BUFFER_MAX = 8000;
const HISTORY_MAX_BYTES = 8 * 1024 * 1024;
const HISTORY_KEEP_ROTATED = 3;
const MAX_LINE_CHARS = 12000;
const MAX_STREAM_REMAINDER = 64 * 1024;

const ERROR_WORDS = /\b(?:error|exception|failed|failure|fatal|traceback|crash(?:ed)?|oom|integrity-failure|launch-failed|unknown_effect|giving up)\b/i;
const WARN_WORDS = /\b(?:warn(?:ing)?|blocked|degraded|fallback|missed|retry|stale|unavailable)\b/i;

function oneLine(value, limit = MAX_LINE_CHARS) {
  const text = String(value == null ? '' : value)
    .replace(/\r?\n/g, ' ↵ ')
    .replace(/\s+/g, ' ')
    .replace(/\b(Bearer)\s+[^\s,;]+/gi, '$1 <redacted>')
    .replace(
      /\b((?:api[_-]?key|token|secret|password)\s*[=:]\s*)(?:"[^"]*"|'[^']*'|[^\s,;&]+)/gi,
      '$1<redacted>',
    )
    .replace(/\b(?:sk|xai|ghp|github_pat|AIza)[-_A-Za-z0-9]{12,}\b/g, '<redacted>')
    .trim();
  return text.length > limit ? text.slice(0, limit) + '…' : text;
}

function decodeField(value) {
  const raw = String(value || '');
  if (raw.startsWith('"')) {
    try { return String(JSON.parse(raw)); } catch (_) { return raw.slice(1, -1); }
  }
  return raw;
}

function opField(body, name) {
  const pattern = new RegExp(`(?:^|\\s)${name}=("(?:[^"\\\\]|\\\\.)*"|[^\\s]+)`);
  const match = pattern.exec(body);
  return match ? decodeField(match[1]) : '';
}

function sourceAlias(value) {
  const source = String(value || 'main').toLowerCase();
  const aliases = {
    'backend.variant1-backend': 'backend.lifecycle',
    'backend.ws': 'transport.ws',
    'backend.ws:activity': 'transport.presence',
    'backend.activity': 'activity',
    'backend.snapshot': 'snapshot',
    'renderer.deck': 'renderer.connection',
  };
  return aliases[source] || source.replace(/[^a-z0-9_.:-]+/g, '_').slice(0, 100);
}

/** Convert legacy prefixes and new [op] records into one source/severity row. */
function classifyLogMessage(message) {
  const raw = oneLine(message);
  let body = raw;
  const tags = [];
  for (let index = 0; index < 3; index += 1) {
    const match = /^\[([^\]]+)\]\s*/.exec(body);
    if (!match) break;
    tags.push(match[1].toLowerCase());
    body = body.slice(match[0].length);
  }

  const opIndex = tags.indexOf('op');
  const opBody = opIndex >= 0 ? body : '';
  let level = opBody ? opField(opBody, 'level').toLowerCase() : '';
  let source = opBody ? opField(opBody, 'source').toLowerCase() : '';
  const event = opBody ? opField(opBody, 'event') : '';

  if (!source) {
    if (tags[0] === 'backend' && tags[1]) source = `backend.${tags[1]}`;
    else if (tags[0] === 'backend') source = 'backend.manager';
    else if (tags[0] === 'backend:err') source = 'backend.stderr';
    else if (tags[0] === 'renderer' && tags[1]) source = `renderer.${tags[1]}`;
    else if (tags[0] === 'renderer') source = 'renderer';
    else if (tags[0]) source = tags[0];
    else source = 'main';
  }

  if (opBody) {
    body = opBody
      .replace(/(?:^|\s)level=(?:"(?:[^"\\]|\\.)*"|[^\s]+)/, '')
      .replace(/(?:^|\s)source=(?:"(?:[^"\\]|\\.)*"|[^\s]+)/, '')
      .replace(/(?:^|\s)event=(?:"(?:[^"\\]|\\.)*"|[^\s]+)/, '')
      .trim();
    body = `${event || 'event'}${body ? ' ' + body : ''}`;
  }

  if (!['debug', 'info', 'warn', 'error'].includes(level)) {
    if (
      source === 'backend.stderr'
      || ERROR_WORDS.test(body)
      || /\bexited code=(?:-[0-9]+|[1-9][0-9]*)\b/i.test(body)
    ) level = 'error';
    else if (WARN_WORDS.test(body)) level = 'warn';
    else level = 'info';
  }
  return {raw, source: sourceAlias(source), level, event, message: oneLine(body)};
}

function noiseReason(record) {
  const source = record.source;
  const body = record.message;
  if (source === 'transport.ws' && /\b(?:in|out) type=/.test(body)) return 'wire_success';
  if (source === 'transport.presence' && /^(?:open|close)\b/.test(body)) return 'presence_wire';
  if (source === 'activity' && /event=(?:perception:quality_metrics|perception:capture_scope|agent_(?:graph|runtime):)/.test(body)) return 'duplicated_trace';
  if (source === 'snapshot' && /\b(?:result=(?:none|no_snapshot)|reason="?no snapshot|event=(?:save|restore_messages))/.test(body)) return 'normal_snapshot_miss';
  if (source === 'renderer' && /^(?:animations\.json loaded|Black Cat loaded|Engine (?:ready|unavailable)|Config:|Dim (?:on|off)|Mic recording)/i.test(body)) return 'renderer_state_chatter';
  if (source === 'renderer.connection' && /ws (?:connecting|connected)|ws offline no-backend-info attempt=[01]\b/.test(body)) return 'normal_connection';
  if (source === 'backend.event_sources' && /sense loop started/.test(body)) return 'worker_started';
  if (source === 'backend.automation' && /scheduler started/.test(body)) return 'worker_started';
  if (source === 'backend.llama' && /runtime flag probe: \d+ options/.test(body)) return 'capability_probe';
  if (source === 'backend.vision' && /^Vision capture: cropped/.test(body)) return 'successful_capture';
  return '';
}

function includeInHistoryLog(messageOrRecord) {
  const record = typeof messageOrRecord === 'string'
    ? classifyLogMessage(messageOrRecord)
    : messageOrRecord;
  return !noiseReason(record);
}

function includeInSessionLog(messageOrRecord) {
  const record = typeof messageOrRecord === 'string'
    ? classifyLogMessage(messageOrRecord)
    : messageOrRecord;
  if (noiseReason(record)) return false;
  if (record.level === 'warn' || record.level === 'error') return true;
  if ([
    'model', 'tool', 'capability', 'mutation', 'kernel', 'work', 'connector',
    'run', 'snapshot', 'runtime', 'vision', 'review', 'desktop',
  ].includes(record.source)) return true;
  if (['main', 'backend.manager', 'backend.lifecycle', 'backend.session'].includes(record.source)) return true;
  if (record.source === 'backend.automation') {
    return /\b(?:firing|delivered|completed|skipped)\b/.test(record.message);
  }
  if (record.source === 'backend.llama') {
    return /\b(?:starting|ready|stop|exited)\b/.test(record.message);
  }
  return false;
}

function formatRecord(record, timestamp = new Date().toISOString()) {
  return `[${timestamp}] [${record.level.toUpperCase()}] [${record.source}] ${record.message}`;
}

function rotateHistoryIfNeeded(historyPath) {
  try {
    if (!fs.existsSync(historyPath) || fs.statSync(historyPath).size < HISTORY_MAX_BYTES) return;
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    let rotated = `${historyPath}.${stamp}`;
    let n = 1;
    while (fs.existsSync(rotated)) rotated = `${historyPath}.${stamp}.${n++}`;
    fs.renameSync(historyPath, rotated);
    const dir = path.dirname(historyPath);
    const base = path.basename(historyPath) + '.';
    const extras = fs.readdirSync(dir)
      .filter((name) => name.startsWith(base))
      .map((name) => {
        const full = path.join(dir, name);
        try { return {full, mtime: fs.statSync(full).mtimeMs}; } catch (_) { return null; }
      })
      .filter(Boolean)
      .sort((a, b) => b.mtime - a.mtime);
    for (const row of extras.slice(HISTORY_KEEP_ROTATED)) {
      try { fs.unlinkSync(row.full); } catch (_) { /* ignore */ }
    }
  } catch (_) { /* logging must never crash the app */ }
}

function createLogger(logDirOrGetter) {
  const dir = () => (typeof logDirOrGetter === 'function' ? logDirOrGetter() : logDirOrGetter);
  let buffer = [];
  const listeners = new Set();
  const streamRemainders = new Map();
  const recentInfo = new Map();

  function notify(line) {
    for (const fn of listeners) {
      try { fn(line); } catch (_) { /* never break logging */ }
    }
  }

  function pushBuffer(line) {
    buffer.push(line);
    if (buffer.length > LOG_BUFFER_MAX) buffer.splice(0, buffer.length - LOG_BUFFER_MAX);
    notify(line);
  }

  function duplicateInfo(record, now) {
    if (record.level !== 'info') return false;
    const key = `${record.source}\0${record.message}`;
    const prior = Number(recentInfo.get(key) || 0);
    recentInfo.set(key, now);
    if (recentInfo.size > 512) {
      for (const [candidate, stamp] of recentInfo) {
        if (now - stamp > 10000) recentInfo.delete(candidate);
      }
    }
    return prior > 0 && now - prior < 1000;
  }

  function logToFile(message) {
    try {
      const record = classifyLogMessage(message);
      if (!record.message || !includeInHistoryLog(record)) return;
      const now = Date.now();
      if (duplicateInfo(record, now)) return;
      const logDir = dir();
      fs.mkdirSync(logDir, {recursive: true});
      const historyPath = path.join(logDir, LOG_HISTORY);
      rotateHistoryIfNeeded(historyPath);
      const line = formatRecord(record, new Date(now).toISOString());
      fs.appendFileSync(historyPath, line + '\n', 'utf-8');
      if (includeInSessionLog(record)) {
        fs.appendFileSync(path.join(logDir, LOG_SESSION), line + '\n', 'utf-8');
        pushBuffer(line);
      }
    } catch (_) { /* logging must never crash the app */ }
  }

  function beginSessionLog() {
    try {
      const logDir = dir();
      fs.mkdirSync(logDir, {recursive: true});
      const banner = [
        `=== VARIANT-1 session started ${new Date().toISOString()} ===`,
        '(main0.log = concise operations; main1.log = filtered diagnostics; data/traces = full structured trajectory)',
      ];
      fs.writeFileSync(path.join(logDir, LOG_SESSION), banner.join('\n') + '\n', 'utf-8');
      fs.appendFileSync(path.join(logDir, LOG_HISTORY), banner.join('\n') + '\n', 'utf-8');
      buffer = banner.slice();
      streamRemainders.clear();
      recentInfo.clear();
    } catch (_) { /* logging must never crash the app */ }
  }

  function logBackendStream(prefix, chunk) {
    const key = String(prefix || '[backend] ');
    let text = String(streamRemainders.get(key) || '') + String(chunk == null ? '' : chunk);
    const lines = text.split(/\r?\n/);
    text = lines.pop() || '';
    for (const line of lines) {
      if (line.length) logToFile(key + line);
    }
    if (text.length > MAX_STREAM_REMAINDER) {
      logToFile(key + text.slice(0, MAX_STREAM_REMAINDER) + '… [unterminated line clipped]');
      text = '';
    }
    streamRemainders.set(key, text);
  }

  function flushLogStreams() {
    for (const [prefix, remainder] of streamRemainders) {
      if (remainder) logToFile(prefix + remainder);
    }
    streamRemainders.clear();
  }

  function getLogBuffer() { return buffer.slice(); }

  function clearLogBuffer() {
    const stamp = new Date().toISOString();
    const bannerLines = [
      `=== operational log view cleared ${stamp} ===`,
      '(main0.log reset; main1 filtered history and structured traces retained)',
    ];
    try {
      const logDir = dir();
      fs.mkdirSync(logDir, {recursive: true});
      fs.writeFileSync(path.join(logDir, LOG_SESSION), bannerLines.join('\n') + '\n', 'utf-8');
      fs.appendFileSync(path.join(logDir, LOG_HISTORY), bannerLines.join('\n') + '\n', 'utf-8');
    } catch (_) { /* ignore */ }
    buffer = bannerLines.slice();
    return buffer.slice();
  }

  function subscribeLog(fn) {
    if (typeof fn !== 'function') return () => {};
    listeners.add(fn);
    return () => { listeners.delete(fn); };
  }

  return {
    logToFile,
    beginSessionLog,
    logBackendStream,
    flushLogStreams,
    getLogBuffer,
    clearLogBuffer,
    subscribeLog,
    getLogDir: dir,
    LOG_BUFFER_MAX,
  };
}

module.exports = {
  createLogger,
  classifyLogMessage,
  formatRecord,
  includeInHistoryLog,
  includeInSessionLog,
  LOG_SESSION,
  LOG_HISTORY,
  LOG_BUFFER_MAX,
  HISTORY_MAX_BYTES,
  rotateHistoryIfNeeded,
};
