'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const {createWriteStream} = require('node:fs');
const https = require('node:https');
const {pipeline} = require('node:stream/promises');

const TRANSIENT_CODES = new Set([
  'ETIMEDOUT', 'ECONNRESET', 'ECONNABORTED', 'EPIPE', 'EAI_AGAIN',
  'ENOTFOUND', 'ERR_STREAM_PREMATURE_CLOSE',
]);

function retryable(error) {
  return TRANSIENT_CODES.has(error.code) || error.statusCode === 408 ||
    error.statusCode === 429 || (error.statusCode >= 500 && error.statusCode <= 599);
}

async function receive(url, signal, get, redirects) {
  const response = await new Promise((resolve, reject) => {
    const request = get(url, {headers: {'user-agent': 'variant1-setup'}, signal}, resolve);
    request.on('error', reject);
  });
  if (response.statusCode >= 300 && response.statusCode < 400 && response.headers.location) {
    response.destroy();
    if (redirects <= 0) throw new Error('download redirect limit exceeded');
    return receive(new URL(response.headers.location, url).href, signal, get, redirects - 1);
  }
  if (response.statusCode !== 200) {
    response.destroy();
    const error = new Error('download failed: HTTP ' + response.statusCode);
    error.statusCode = response.statusCode;
    error.retryAfter = response.headers['retry-after'];
    throw error;
  }
  return response;
}

function retryDelay(error, attempt, baseDelayMs, maxDelayMs) {
  const text = String(error.retryAfter || '').trim();
  if (text) {
    const seconds = Number(text);
    const requested = Number.isFinite(seconds) ? seconds * 1000 : Date.parse(text) - Date.now();
    if (Number.isFinite(requested)) {
      if (requested > maxDelayMs) return null;
      return Math.max(0, requested);
    }
  }
  return Math.min(maxDelayMs, baseDelayMs * 2 ** attempt);
}

/** Each attempt stages its own complete file; a failed stream is never published. */
async function downloadPinnedAsset(url, destination, options = {}) {
  const get = options.get || https.get;
  const sleep = options.sleep || (ms => new Promise(resolve => setTimeout(resolve, ms)));
  const attempts = options.attempts ?? 4;
  const timeoutMs = options.timeoutMs ?? 45_000;
  const baseDelayMs = options.baseDelayMs ?? 1_000;
  const maxDelayMs = options.maxDelayMs ?? 10_000;
  const onRetry = options.onRetry || (() => {});
  if (!Number.isInteger(attempts) || attempts < 1 || attempts > 10 || timeoutMs <= 0) {
    throw new Error('invalid bounded download policy');
  }
  for (let attempt = 0; attempt < attempts; attempt++) {
    const partial = destination + '.part-' + process.pid + '-' + crypto.randomBytes(6).toString('hex');
    const controller = new AbortController();
    const timeout = Object.assign(new Error('download timed out after ' + timeoutMs + 'ms'), {code: 'ETIMEDOUT'});
    const timer = setTimeout(() => controller.abort(timeout), timeoutMs);
    let failure;
    try {
      const response = await receive(url, controller.signal, get, options.maxRedirects ?? 5);
      await pipeline(response, createWriteStream(partial, {flags: 'wx'}), {signal: controller.signal});
      await fs.rename(partial, destination);
      return;
    } catch (error) {
      failure = controller.signal.aborted ? controller.signal.reason : error;
    } finally {
      clearTimeout(timer);
      controller.abort();
      await fs.rm(partial, {force: true});
    }
    const delay = retryDelay(failure, attempt, baseDelayMs, maxDelayMs);
    if (attempt + 1 === attempts || !retryable(failure) || delay === null) throw failure;
    onRetry({attempt: attempt + 1, attempts, delayMs: delay, error: failure});
    await sleep(delay);
  }
}

module.exports = {downloadPinnedAsset};
