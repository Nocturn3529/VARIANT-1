'use strict';

/* VARIANT-1 — Log monitor pop-out.
 *
 * Flat chronological view of the concise operational records in main0.log,
 * shown as terminal columns (time, level, source, message) with a text filter
 * and level chips. Full structured trajectories stay in data/traces; main1
 * retains filtered diagnostics. Clear resets only this session view and main0.
 */

(function () {
  const feed = document.getElementById('feed');
  const emptyEl = document.getElementById('empty');
  const noMatchEl = document.getElementById('no-match');
  const lineCountEl = document.getElementById('line-count');
  const clockEl = document.getElementById('clock');
  const scrollStateEl = document.getElementById('scroll-state');
  const resumeBtn = document.getElementById('resume-btn');
  const newCountEl = document.getElementById('new-count');
  const copyStatusEl = document.getElementById('copy-status');
  const liveDot = document.getElementById('live-dot');
  const filterInput = document.getElementById('filter');
  const wrapBtn = document.getElementById('btn-wrap');
  const levelButtons = Array.from(document.querySelectorAll('.mon-levels button'));
  const counts = {error: 0, warn: 0, activity: 0};
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
  let liveTimer = null;
  let level = 'all';
  let query = '';

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
    for (const key of Object.keys(counts)) {
      const el = document.getElementById('count-' + key);
      if (!el) continue;
      el.textContent = String(counts[key]);
      el.classList.toggle('has-count', counts[key] > 0);
    }
  }

  function showEmpty(show) {
    if (emptyEl) emptyEl.hidden = !show;
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

  function pulseLive() {
    if (!liveDot) return;
    liveDot.classList.remove('is-live');
    void liveDot.offsetWidth;
    liveDot.classList.add('is-live');
    if (liveTimer) clearTimeout(liveTimer);
    liveTimer = setTimeout(() => liveDot.classList.remove('is-live'), 4000);
  }

  /** Error, warning, or the colored category a line belongs to. */
  function classify(text) {
    const s = String(text || '');
    const levelMatch = /^\[[^\]]+\]\s+\[(\w+)\]/.exec(s);
    const tag = levelMatch ? levelMatch[1].toUpperCase() : '';
    let lvl = '';
    if (tag === 'ERROR' || /\berror\b| FAIL |Exception|Traceback/i.test(s)) lvl = 'error';
    else if (tag === 'WARN' || tag === 'WARNING' || /\bwarn(ing)?\b|DeprecationWarning/i.test(s)) lvl = 'warn';
    let kind = '';
    if (/\[(?:run|tool|capability|desktop|vision)\]/.test(s)) kind = 'activity';
    else if (/\[(?:mutation|kernel|work|model|connector|review)\]/.test(s)) kind = 'audit';
    else if (/\[INFO\].*(?:ready|started|succeeded|completed|activated)/i.test(s)) kind = 'ok';
    return {lvl, kind};
  }

  /** [ISO time] [LEVEL] [source] message → columns; anything else stays plain. */
  function render(text) {
    const el = document.createElement('div');
    const {lvl, kind} = classify(text);
    el.className = 'log-line';
    if (lvl) el.dataset.level = lvl;
    if (kind) el.dataset.kind = kind;
    const match = /^\[([^\]]+)\]\s+\[(\w+)\]\s+(?:\[([^\]]+)\]\s+)?([\s\S]*)$/.exec(text);
    if (!match) {
      el.classList.add('is-plain');
      el.textContent = text;
      return el;
    }
    const stamp = new Date(match[1]);
    const time = Number.isNaN(stamp.getTime()) ? match[1]
      : [stamp.getHours(), stamp.getMinutes(), stamp.getSeconds()].map((n) => String(n).padStart(2, '0')).join(':')
        + '.' + String(stamp.getMilliseconds()).padStart(3, '0');
    for (const [cls, value] of [['time', time], ['level', match[2]], ['source', match[3] || ''], ['message', match[4]]]) {
      const cell = document.createElement('span');
      cell.className = 'log-line__' + cls;
      cell.textContent = value;
      if (cls === 'time') cell.title = match[1];
      if (cls === 'source' && value) cell.title = value;
      el.appendChild(cell);
    }
    return el;
  }

  function matches(el, text) {
    if (level === 'error' && el.dataset.level !== 'error') return false;
    if (level === 'warn' && el.dataset.level !== 'warn') return false;
    if (level === 'activity' && el.dataset.kind !== 'activity' && el.dataset.kind !== 'audit') return false;
    return !query || text.toLowerCase().includes(query);
  }

  function count(el, delta) {
    if (el.dataset.level === 'error') counts.error += delta;
    if (el.dataset.level === 'warn') counts.warn += delta;
    if (el.dataset.kind === 'activity' || el.dataset.kind === 'audit') counts.activity += delta;
  }

  function updateNoMatch() {
    if (!noMatchEl) return;
    const filtering = level !== 'all' || !!query;
    noMatchEl.hidden = !filtering || !lines.length || !!feed.querySelector('.log-line:not([hidden])');
  }

  function applyFilter() {
    const rows = feed.querySelectorAll('.log-line');
    rows.forEach((el, index) => { el.hidden = !matches(el, lines[index] || ''); });
    updateNoMatch();
    autoScroll = true;
    if (scrollStateEl) scrollStateEl.textContent = 'on';
    scheduleScroll();
  }

  function trimDom() {
    while (lines.length > MAX_LINES) {
      lines.shift();
      const first = feed.querySelector('.log-line');
      if (first) { count(first, -1); first.remove(); }
    }
  }

  function appendLine(text, opts) {
    const line = String(text == null ? '' : text);
    if (!line) return;
    showEmpty(false);
    lines.push(line);
    const el = render(line);
    el.hidden = !matches(el, line);
    el.classList.add('is-fresh');
    count(el, 1);
    feed.appendChild(el);
    trimDom();
    setLineCount();
    updateNoMatch();
    pulseLive();

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
    for (const key of Object.keys(counts)) counts[key] = 0;
    // Keep the empty states; remove only log rows.
    feed.querySelectorAll('.log-line').forEach((n) => n.remove());
    pendingNew = 0;
    if (resumeBtn) resumeBtn.hidden = true;
    if (!list.length) {
      showEmpty(true);
      setLineCount();
      updateNoMatch();
      return;
    }
    showEmpty(false);
    const frag = document.createDocumentFragment();
    for (let i = 0; i < list.length; i++) {
      const line = String(list[i] == null ? '' : list[i]);
      if (!line) continue;
      lines.push(line);
      const el = render(line);
      el.hidden = !matches(el, line);
      count(el, 1);
      frag.appendChild(el);
    }
    feed.appendChild(frag);
    trimDom();
    setLineCount();
    updateNoMatch();
    autoScroll = true;
    if (scrollStateEl) scrollStateEl.textContent = 'on';
    scheduleScroll();
  }

  // ---- Scroll follow ------------------------------------------------------
  feed.addEventListener('scroll', () => {
    const nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 48;
    if (!nearBottom && autoScroll) {
      autoScroll = false;
      if (scrollStateEl) scrollStateEl.textContent = 'paused';
    } else if (nearBottom && !autoScroll) {
      autoScroll = true;
      pendingNew = 0;
      if (scrollStateEl) scrollStateEl.textContent = 'on';
      if (resumeBtn) resumeBtn.hidden = true;
    }
  });

  if (resumeBtn) {
    resumeBtn.addEventListener('click', () => {
      autoScroll = true;
      pendingNew = 0;
      resumeBtn.hidden = true;
      if (scrollStateEl) scrollStateEl.textContent = 'on';
      feed.scrollTop = feed.scrollHeight;
    });
  }

  // ---- Filters ------------------------------------------------------------
  let filterTimer = null;
  filterInput?.addEventListener('input', () => {
    if (filterTimer) clearTimeout(filterTimer);
    filterTimer = setTimeout(() => {
      query = filterInput.value.trim().toLowerCase();
      applyFilter();
    }, 120);
  });
  for (const button of levelButtons) {
    button.addEventListener('click', () => {
      level = button.dataset.level || 'all';
      for (const other of levelButtons) other.setAttribute('aria-pressed', String(other === button));
      applyFilter();
    });
  }
  wrapBtn?.addEventListener('click', () => {
    const wrapped = !feed.classList.contains('is-wrapped');
    feed.classList.toggle('is-wrapped', wrapped);
    wrapBtn.setAttribute('aria-pressed', String(wrapped));
  });

  // ---- Toolbar ------------------------------------------------------------
  document.getElementById('mon-close')?.addEventListener('click', () => {
    if (api.close) api.close();
  });

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

  btnCopy?.addEventListener('click', async () => {
    try {
      let ok = false;
      if (api.copyLogs) {
        ok = !!(await api.copyLogs());
      } else if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(lines.join('\n'));
        ok = true;
      }
      flashStatus(ok ? ('Copied ' + lines.length + ' lines') : 'Copy failed');
    } catch (_) {
      flashStatus('Copy failed');
    }
  });

  btnClear?.addEventListener('click', async () => {
    try {
      const next = api.clearLogs ? await api.clearLogs() : [];
      replaceAll(next);
      flashStatus('Cleared');
    } catch (_) {
      flashStatus('Clear failed');
    }
  });

  btnFolder?.addEventListener('click', async () => {
    try {
      if (api.openLogFolder) await api.openLogFolder();
    } catch (_) { /* ignore */ }
  });

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
