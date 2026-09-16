'use strict';

/**
 * VARIANT-1's read-only Live2D desktop-presence renderer.
 *
 * The overlay owns avatar rendering, drag/click-through behavior, and a small
 * projection of global backend activity into avatar moods. Main Deck owns all
 * user intent, chat/session state, onboarding, cancellation, and configuration.
 */

(function () {
  const ASSET_BASE = 'variant1://app/';
  const MODEL_URL = ASSET_BASE + 'assets/avatar/blackcat/model.json';
  const ANIMATIONS_URL = ASSET_BASE + 'config/animations.json';

  const DEV = (() => {
    try { return /[?&]dev=1\b/.test(location.search || ''); }
    catch (_) { return false; }
  })();

  const $ = (id) => document.getElementById(id);
  const avatarBoxEl = $('avatar-box');
  const canvasEl = $('avatar-canvas');
  const placeholderEl = $('placeholder');

  let app = null;
  let model = null;
  let animations = null;
  let avatarSize = 160;
  let currentMood = 'idle';

  let backendWS = null;
  let backendEndpoint = '';
  let lastActivityPort = 0;
  let lastActivityToken = '';
  let activityReconnectTimer = null;
  let activityReconnectAttempt = 0;
  const activeRuns = new Set();

  function log(msg) {
    if (window.variant1) window.variant1.log(msg);
    console.log('[variant1]', msg);
  }

  function setMood(moodName) {
    currentMood = moodName || 'neutral';
    if (!model || !animations) return;
    const map = animations.animations || {};
    const entry = map[currentMood] || map.neutral || map.idle;
    const group = entry && entry.group ? entry.group : 'idle';
    try {
      if (group === 'idle') model.motion('idle');
      else {
        const priority = (PIXI.live2d && PIXI.live2d.MotionPriority) || {FORCE: 3};
        model.motion(group, 0, priority.FORCE);
      }
    } catch (err) {
      log('setMood failed: ' + err.message);
    }
  }
  window.variant1SetMood = setMood;

  function scheduleActivityReconnect() {
    if (!lastActivityPort || !lastActivityToken || activityReconnectTimer) return;
    const delay = Math.min(10000, 250 * Math.pow(2, activityReconnectAttempt++));
    activityReconnectTimer = setTimeout(() => {
      activityReconnectTimer = null;
      connectActivity(lastActivityPort, lastActivityToken);
    }, delay);
  }

  function connectActivity(port, activityToken) {
    const endpoint = `${Number(port)}:${String(activityToken || '')}`;
    if (!Number.isFinite(Number(port)) || !activityToken) return;
    lastActivityPort = Number(port);
    lastActivityToken = String(activityToken);
    if (activityReconnectTimer) clearTimeout(activityReconnectTimer);
    activityReconnectTimer = null;
    if (backendEndpoint === endpoint && backendWS && backendWS.readyState <= 1) return;

    const previous = backendWS;
    backendWS = null;
    backendEndpoint = '';
    if (previous) {
      try { previous.close(); } catch (_) {}
    }

    let ws;
    try {
      ws = new WebSocket(
        `ws://127.0.0.1:${port}/ws/activity?token=${encodeURIComponent(activityToken)}`,
      );
    } catch (err) {
      log('WS construct failed: ' + err.message);
      scheduleActivityReconnect();
      return;
    }

    backendWS = ws;
    backendEndpoint = endpoint;
    ws.addEventListener('open', () => {
      if (backendWS !== ws) return;
      activityReconnectAttempt = 0;
      log('Backend activity stream connected.');
    });
    ws.addEventListener('message', (event) => {
      if (backendWS === ws) handleBackendMessage(event.data);
    });
    ws.addEventListener('close', () => {
      if (backendWS !== ws) return;
      backendWS = null;
      backendEndpoint = '';
      activeRuns.clear();
      setMood('neutral');
      scheduleActivityReconnect();
    });
  }

  function handleBackendMessage(raw) {
    let message;
    try { message = JSON.parse(raw); }
    catch (_) { return; }

    switch (message.type) {
      case 'hello':
      case 'engine':
        break;
      case 'activity':
        handleActivity(message);
        break;
      case 'proactive':
        if (message.mood) setMood(message.mood);
        break;
      default:
        break;
    }
  }

  function handleActivity(message) {
    if (!message || message.source === 'passive') return;
    const runId = message.run_id;
    if (!runId) return;

    if (message.event === 'task:start') {
      activeRuns.add(runId);
      setMood(message.mood || 'focused');
      return;
    }
    if (message.event === 'task:done') {
      activeRuns.delete(runId);
      if (!activeRuns.size) setMood(message.mood || 'neutral');
    }
  }

  async function setupBackend() {
    if (!window.variant1) return;
    window.variant1.onActivityStatus((payload) => {
      if (payload.status === 'ready' && payload.info) {
        connectActivity(payload.info.port, payload.info.activityToken);
      }
    });
  }

  function applyAvatarMetrics(size) {
    const normalized = Number(size) || 160;
    const root = document.documentElement.style;
    root.setProperty('--avatar-w', Math.round(normalized * 1.625) + 'px');
    root.setProperty('--avatar-h', Math.round(normalized * 2.125) + 'px');
  }

  function layoutModel() {
    if (!model || !app) return;
    const baseHeight = model.internalModel
      ? model.internalModel.height
      : (model.height / (model.scale.y || 1));
    const target = Math.min(avatarSize * 1.4, 320);
    const scale = baseHeight > 0 ? target / baseHeight : 0.2;
    model.scale.set(scale);
    model.anchor.set(0.5, 1.0);
    model.x = app.renderer.width / 2;
    model.y = app.renderer.height - 4;
  }

  async function loadAnimations() {
    try {
      animations = await (await fetch(ANIMATIONS_URL)).json();
      log('animations.json loaded (' + Object.keys(animations.animations || {}).length + ' moods).');
    } catch (err) {
      log('animations.json failed: ' + err.message);
      animations = {animations: {}};
    }
  }

  let dragging = false;
  function attachDrag(targetEl) {
    if (!window.variant1) return;
    targetEl.addEventListener('mousedown', (event) => {
      if (event.button !== 0) return;
      dragging = true;
      canvasEl.classList.add('dragging');
      window.variant1.dragStart(event.screenX, event.screenY);
      event.preventDefault();
    });
    window.addEventListener('mousemove', (event) => {
      if (dragging) window.variant1.dragMove(event.screenX, event.screenY);
    });
    window.addEventListener('mouseup', () => {
      if (!dragging) return;
      dragging = false;
      canvasEl.classList.remove('dragging');
      window.variant1.dragEnd();
    });
  }

  let pointer = null;
  let passThroughScheduled = false;
  let lastIgnore = null;
  function evaluatePassThrough() {
    passThroughScheduled = false;
    if (!pointer) return;
    const element = document.elementFromPoint(pointer.x, pointer.y);
    const interactive = dragging || !!(element && element.closest && element.closest('.io'));
    const ignore = !interactive;
    if (ignore === lastIgnore) return;
    lastIgnore = ignore;
    window.variant1.setMouseIgnore(ignore);
  }

  function attachPassThrough() {
    if (!window.variant1) return;
    window.addEventListener('mousemove', (event) => {
      pointer = {x: event.clientX, y: event.clientY};
      if (passThroughScheduled) return;
      passThroughScheduled = true;
      requestAnimationFrame(evaluatePassThrough);
    });
  }

  function attachDevHotkeys() {
    if (!DEV) return;
    const moods = [
      'idle', 'excited', 'thinking', 'curious', 'helpful',
      'focused', 'surprised', 'concerned', 'sad',
    ];
    window.addEventListener('keydown', (event) => {
      const index = parseInt(event.key, 10);
      if (!Number.isNaN(index) && index >= 0 && index < moods.length) {
        setMood(moods[index]);
      }
    });
  }

  function showPlaceholder() {
    canvasEl.hidden = true;
    placeholderEl.hidden = false;
    attachDrag(placeholderEl);
  }

  async function boot() {
    try {
      const settings = await window.variant1.getSettings();
      if (settings && settings.avatar && settings.avatar.size) {
        avatarSize = settings.avatar.size;
      }
    } catch (_) {}
    applyAvatarMetrics(avatarSize);
    await loadAnimations();
    setupBackend();
    attachPassThrough();
    attachDevHotkeys();
    window.addEventListener('resize', layoutModel);

    if (typeof PIXI === 'undefined' || !PIXI.live2d || !PIXI.live2d.Live2DModel) {
      log('Live2D unavailable — placeholder.');
      showPlaceholder();
      return;
    }
    try { PIXI.live2d.Live2DModel.registerTicker(PIXI.Ticker); } catch (_) {}
    app = new PIXI.Application({
      view: canvasEl,
      backgroundAlpha: 0,
      resizeTo: avatarBoxEl,
      antialias: true,
      autoStart: true,
    });
    try {
      model = await PIXI.live2d.Live2DModel.from(MODEL_URL, {autoInteract: false});
      app.stage.addChild(model);
      layoutModel();
      setMood('idle');
      attachDrag(canvasEl);
      log('Black Cat loaded. Idle looping.');
    } catch (err) {
      log('Model load failed: ' + (err && err.message ? err.message : err));
      showPlaceholder();
    }
  }

  if (!window.variant1) log('Preload bridge missing — outside Electron?');
  let booted = false;
  function bootOnce() {
    if (booted) return;
    booted = true;
    boot();
  }
  window.addEventListener('DOMContentLoaded', bootOnce);
  if (document.readyState !== 'loading') bootOnce();
})();
