'use strict';

/* VARIANT-1 — Live Logs pop-out.
 *
 * Flat chronological view of the concise operational records in main0.log.
 * Full structured trajectories stay in data/traces; main1 retains filtered
 * diagnostics. Clear resets only this session view and main0.
 */

(function () {
  const feed = document.getElementById('feed');
  const emptyEl = document.getElementById('empty');
  const lineCountEl = document.getElementById('line-count');
  const clockEl = document.getElementById('clock');
  const scrollStateEl = document.getElementById('scroll-state');
  const resumeBtn = document.getElementById('resume-btn');
  const newCountEl = document.getElementById('new-count');
  const copyStatusEl = document.getElementById('copy-status');
  const api = window.variant1Monitor || {};
  const pinButton = document.getElementById('mon-pin');
  for (const [id, action] of [['mon-pin', 'pin'], ['mon-minimize', 'minimize'], ['mon-maximize', 'maximize']]) {
    document.getElementById(id)?.addEventListener('click', async () => {
      try {
        const result = await api.controlWindow?.(action);
        if (result?.ok) pinButton?.setAttribute('aria-pressed', String(result.pinned));
      } catch { /* A closing window has no remaining controls to update. */ }
    });
  }

  /** Soft cap for DOM nodes (matches electron-logging ring size). */
  const MAX_LINES = 8000;
  /** @type {string[]} */
  const lines = [];
  let autoScroll = true;
  let pendingNew = 0;
  let scrollScheduled = false;
  let copyStatusTimer = null;

  function tickClock() {
    if (!clockEl) return;
    const d = new Date();
    clockEl.textContent = [d.getHours(), d.getMinutes(), d.getSeconds()]
      .map((n) => String(n).padStart(2, '0')).join(':');
  }
  setInterval(tickClock, 1000);
  tickClock();

  function setLineCount() {
    if (lineCountEl) lineCountEl.textContent = String(lines.length);
  }

  function showEmpty(show) {
    if (!emptyEl) return;
    emptyEl.hidden = !show;
  }

  function scrollFeed() {
    if (!autoScroll) return;
    feed.scrollTop = feed.scrollHeight;
  }

  function scheduleScroll() {
    if (!autoScroll || scrollScheduled) return;
    scrollScheduled = true;
    requestAnimationFrame(() => {
      scrollScheduled = false;
      scrollFeed();
    });
  }

  function trimDom() {
    while (lines.length > MAX_LINES) {
      lines.shift();
      const first = feed.querySelector('.log-line');
      if (first) first.remove();
    }
  }

  function lineClass(text) {
    const s = String(text || '');
    if (/\[ERROR\]|\berror\b| FAIL |Exception|Traceback/i.test(s)) return 'log-line log-line--error';
    if (/\[WARN\]|\bwarn(ing)?\b|DeprecationWarning/i.test(s)) return 'log-line log-line--warn';
    if (/\[(?:run|tool|capability|desktop|vision)\]/.test(s)) return 'log-line log-line--activity';
    if (/\[(?:mutation|kernel|work|model|connector|review)\]/.test(s)) return 'log-line log-line--audit';
    if (/\[INFO\].*(?:ready|started|succeeded|completed|activated)/i.test(s)) return 'log-line log-line--ok';
    return 'log-line';
  }

  function appendLine(text, opts) {
    const line = String(text == null ? '' : text);
    if (!line) return;
    showEmpty(false);
    lines.push(line);

    const el = document.createElement('div');
    el.className = lineClass(line);
    el.textContent = line;
    feed.appendChild(el);

    trimDom();
    setLineCount();

    if (!autoScroll) {
      pendingNew += 1;
      if (resumeBtn) {
        resumeBtn.hidden = false;
        if (newCountEl) newCountEl.textContent = String(pendingNew);
      }
    } else if (!opts || !opts.skipScroll) {
      scheduleScroll();
    }
  }

  function replaceAll(nextLines) {
    const list = Array.isArray(nextLines) ? nextLines : [];
    lines.length = 0;
    // Keep #empty; remove only log rows.
    feed.querySelectorAll('.log-line').forEach((n) => n.remove());
    if (!list.length) {
      showEmpty(true);
      setLineCount();
      pendingNew = 0;
      if (resumeBtn) resumeBtn.hidden = true;
      return;
    }
    showEmpty(false);
    const frag = document.createDocumentFragment();
    for (let i = 0; i < list.length; i++) {
      const line = String(list[i] == null ? '' : list[i]);
      if (!line) continue;
      lines.push(line);
      const el = document.createElement('div');
      el.className = lineClass(line);
      el.textContent = line;
      frag.appendChild(el);
    }
    feed.appendChild(frag);
    trimDom();
    setLineCount();
    pendingNew = 0;
    if (resumeBtn) resumeBtn.hidden = true;
    autoScroll = true;
    if (scrollStateEl) scrollStateEl.textContent = 'ON';
    scheduleScroll();
  }

  // ---- Scroll follow ------------------------------------------------------
  feed.addEventListener('scroll', () => {
    const nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 48;
    if (!nearBottom && autoScroll) {
      autoScroll = false;
      if (scrollStateEl) scrollStateEl.textContent = 'PAUSED';
    } else if (nearBottom && !autoScroll) {
      autoScroll = true;
      pendingNew = 0;
      if (scrollStateEl) scrollStateEl.textContent = 'ON';
      if (resumeBtn) resumeBtn.hidden = true;
    }
  });

  if (resumeBtn) {
    resumeBtn.addEventListener('click', () => {
      autoScroll = true;
      pendingNew = 0;
      resumeBtn.hidden = true;
      if (scrollStateEl) scrollStateEl.textContent = 'ON';
      feed.scrollTop = feed.scrollHeight;
    });
  }

  // ---- Toolbar ------------------------------------------------------------
  if (document.getElementById('mon-close')) {
    document.getElementById('mon-close').addEventListener('click', () => {
      if (api.close) api.close();
    });
  }

  const btnCopy = document.getElementById('btn-copy');
  const btnClear = document.getElementById('btn-clear');
  const btnFolder = document.getElementById('btn-folder');

  function flashStatus(msg) {
    if (!copyStatusEl) return;
    copyStatusEl.hidden = false;
    copyStatusEl.textContent = msg;
    if (copyStatusTimer) clearTimeout(copyStatusTimer);
    copyStatusTimer = setTimeout(() => {
      copyStatusEl.hidden = true;
      copyStatusEl.textContent = '';
    }, 1800);
  }

  if (btnCopy) {
    btnCopy.addEventListener('click', async () => {
      try {
        let ok = false;
        if (api.copyLogs) {
          ok = !!(await api.copyLogs());
        } else if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(lines.join('\n'));
          ok = true;
        }
        flashStatus(ok ? ('COPIED ' + lines.length + ' LINES') : 'COPY FAILED');
      } catch (_) {
        flashStatus('COPY FAILED');
      }
    });
  }

  if (btnClear) {
    btnClear.addEventListener('click', async () => {
      try {
        const next = api.clearLogs ? await api.clearLogs() : [];
        replaceAll(next);
        flashStatus('CLEARED');
      } catch (_) {
        flashStatus('CLEAR FAILED');
      }
    });
  }

  if (btnFolder) {
    btnFolder.addEventListener('click', async () => {
      try {
        if (api.openLogFolder) await api.openLogFolder();
      } catch (_) { /* ignore */ }
    });
  }

  // ---- Bootstrap ----------------------------------------------------------
  async function bootstrap() {
    try {
      const history = api.getLogHistory ? await api.getLogHistory() : [];
      replaceAll(history);
    } catch (_) {
      showEmpty(true);
    }
    if (api.onLogLine) {
      api.onLogLine((line) => appendLine(line));
    }
  }

  bootstrap();
})();
