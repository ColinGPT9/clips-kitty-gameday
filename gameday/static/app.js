/* Clips Kitty Game Day: the dashboard.
 *
 * Plain JavaScript, no build step. It talks only to the app's own routes
 * (docs/API.md): /api/* and /remote-approval/*. The browser handles Basic
 * auth; every request that changes something sends X-Gameday-Action: 1.
 * Nothing is built from HTML strings: text goes in with textContent.
 */
'use strict';

(function () {
  const BUSY_MS = 2000;
  const IDLE_MS = 10000;
  const VIEWS = ['gameday', 'clips', 'offers', 'setup', 'usage'];
  const STAGES = ['voice', 'spark', 'sam', 'render', 'captions'];
  const STAGE_LABELS = { voice: 'Muse Voice', spark: 'Muse Spark', sam: 'SAM 3.1', render: '9:16', captions: 'Captions' };
  const STAGE_WORDS = { waiting: 'Waiting', running: 'Running…', done: 'Done', skipped: 'Skipped', failed: 'Failed' };
  const MODEL_LABELS = { 'muse-voice-transcribe-1.0': 'Muse Voice', 'muse-spark-1.3': 'Muse Spark', 'sam-3.1': 'SAM 3.1' };
  const MODEL_ORDER = ['muse-voice-transcribe-1.0', 'muse-spark-1.3', 'sam-3.1'];
  const CATEGORY_LABELS = {
    clutch: 'Clutch', dunks: 'Dunks', threes: 'Threes', blocks: 'Blocks', steals: 'Steals', assists: 'Assists',
    fan_reactions: 'Fan reactions', big_moment: 'Big moments', other: 'Other', highlight: 'Highlights',
  };
  const JOB_WORDS = { queued: 'Waiting', running: 'Running', done: 'Done', failed: 'Failed', cancelled: 'Cancelled' };
  const JOB_GLYPHS = { queued: '○', running: '▶', done: '✓', failed: '⚠', cancelled: '⊘' };
  const PLATFORM_LABELS = { instagram: 'Instagram Reels', facebook: 'Facebook Page Reels' };
  const OFFER_WORDS = {
    READY_FOR_REVIEW: ['Getting ready', ''],
    NOTIFICATION_PENDING: ['Ready for Muse', 'sky'],
    NOTIFICATION_SENT: ['Sent to your phone', 'sky'],
    AWAITING_APPROVAL: ['Waiting for approval', 'caution'],
    DEFERRED: ['Remind me later', 'caution'],
    APPROVED: ['Approved, posting…', 'sky'],
    PUBLISH_QUEUED: ['Approved, posting…', 'sky'],
    PUBLISHING: ['Approved, posting…', 'sky'],
    PUBLISHED: ['Published', 'good'],
    PARTIAL_FAILURE: ['Partly published', 'caution'],
    FAILED: ['Failed', 'bad'],
    CANCELLED: ['Cancelled', ''],
    EXPIRED: ['Link expired', ''],
  };
  const OPEN_OFFERS = ['NOTIFICATION_SENT', 'AWAITING_APPROVAL', 'DEFERRED'];
  const TEMPLATE_WORDS = { APPROVED: 'approved', PENDING: 'in review', REJECTED: 'rejected' };
  const STORE_KEY = 'ck-gameday-setup-open';

  const S = {
    view: 'gameday',
    status: null,
    ra: null,
    raError: '',
    jobs: null,
    jobSig: '',
    settings: null,
    watches: null,
    clips: null,
    clipsStale: true,
    clipCat: '',
    reviews: null,
    usage: null,
    events: [],
    lastEvent: 0,
    eventLevel: '',
    upload: null,
    timer: 0,
    ticking: false,
    again: false,
    lastError: '',
    connectPending: false,
    stepOpen: {},
    showSteps: readStore(),
  };

  // ---- small helpers -----------------------------------------------------------------

  const $ = (id) => document.getElementById(id);

  /** An element. `props`: class, text, hidden/disabled/checked/value/open as
   *  properties, `on<event>` handlers, everything else as attributes. */
  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') el.className = value;
      else if (key === 'text') el.textContent = value;
      else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
      else if (['hidden', 'disabled', 'checked', 'value', 'open', 'preload'].includes(key)) el[key] = value;
      else el.setAttribute(key, value === true ? '' : String(value));
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid === null || kid === undefined || kid === false || kid === '') continue;
      el.append(kid instanceof Node ? kid : String(kid));
    }
    return el;
  }

  function setText(target, text) {
    const el = typeof target === 'string' ? $(target) : target;
    if (el && el.textContent !== text) el.textContent = text;
  }

  function show(target, visible) {
    const el = typeof target === 'string' ? $(target) : target;
    if (el) el.hidden = !visible;
  }

  /** A short line beside the control that caused it: ok, bad, warn or info. */
  function note(id, text, tone) {
    const el = $(id);
    if (!el) return;
    el.textContent = text || '';
    el.className = 'note' + (tone ? ' ' + tone : '');
  }

  function readStore() {
    try { return window.localStorage.getItem(STORE_KEY) === '1'; } catch (e) { return false; }
  }

  function writeStore(open) {
    try { window.localStorage.setItem(STORE_KEY, open ? '1' : '0'); } catch (e) { /* storage off: stays per page */ }
  }

  /** "$0.42", "$0.0040" below a cent, "—" when unknown. `approx` adds "~". */
  function usd(n, approx) {
    if (n === null || n === undefined || Number.isNaN(Number(n))) return '—';
    const v = Number(n);
    const text = v === 0 ? '$0.00' : '$' + v.toFixed(Math.abs(v) < 0.01 ? 4 : 2);
    return approx && v ? '~' + text : text;
  }

  /** "12s", "1m 43s", "2h 5m". */
  function dur(s) {
    const v = Math.max(0, Math.round(Number(s) || 0));
    if (v < 60) return v + 's';
    if (v < 3600) return `${Math.floor(v / 60)}m ${v % 60}s`;
    return `${Math.floor(v / 3600)}h ${Math.round((v % 3600) / 60)}m`;
  }

  function clock(s) {
    const v = Math.max(0, Math.round(Number(s) || 0));
    const hrs = Math.floor(v / 3600);
    const m = Math.floor((v % 3600) / 60);
    const r = String(v % 60).padStart(2, '0');
    return hrs ? `${hrs}:${String(m).padStart(2, '0')}:${r}` : `${m}:${r}`;
  }

  /** Milliseconds from Unix seconds (this app) or an ISO string (the relay's offers). */
  function toMs(value) {
    if (value === null || value === undefined || value === '') return NaN;
    if (typeof value === 'number') return value * 1000;
    const parsed = Date.parse(value);
    return Number.isNaN(parsed) ? NaN : parsed;
  }

  function at(value) {
    const ms = toMs(value);
    if (Number.isNaN(ms)) return '';
    const d = new Date(ms);
    return new Date().toDateString() === d.toDateString()
      ? d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
      : d.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  }

  function ago(value) {
    const ms = toMs(value);
    if (Number.isNaN(ms)) return '';
    const diff = (Date.now() - ms) / 1000;
    if (diff < 0) return at(value);
    if (diff < 45) return 'just now';
    if (diff < 3600) return Math.round(diff / 60) + ' min ago';
    if (diff < 86400) return Math.round(diff / 3600) + ' h ago';
    return at(value);
  }

  function timeOnly(value) {
    const ms = toMs(value);
    return Number.isNaN(ms) ? '' : new Date(ms).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' });
  }

  function size(bytes) {
    const mb = (Number(bytes) || 0) / (1024 * 1024);
    if (mb >= 1024) return (mb / 1024).toFixed(2) + ' GB';
    return (mb >= 100 ? Math.round(mb) : mb.toFixed(1)) + ' MB';
  }

  function plural(n, one, many) {
    return `${n} ${n === 1 ? one : (many || one + 's')}`;
  }

  /** Only https links (or, where allowed, a relay on this machine) are followed. */
  function safeUrl(value, allowLocal) {
    try {
      const url = new URL(String(value || ''), window.location.href);
      if (url.protocol === 'https:') return url.href;
      if (allowLocal && url.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(url.hostname)) return url.href;
    } catch (e) { /* not a URL */ }
    return '';
  }

  /** A path on this app, for media: never another site. */
  function mediaUrl(value) {
    const text = String(value || '');
    return text.startsWith('/') && !text.startsWith('//') ? text : '';
  }

  /** Replaces `button` with "Question? [Yes] [Cancel]" until one is picked. Esc cancels. */
  function confirmInline(button, question, yes) {
    return new Promise((resolve) => {
      const yesBtn = h('button', { type: 'button', class: 'btn small', text: yes });
      const noBtn = h('button', { type: 'button', class: 'btn small', text: 'Cancel' });
      const box = h('span', { class: 'confirm' }, h('span', { text: question }), yesBtn, noBtn);
      const done = (answer) => {
        box.remove();
        button.hidden = false;
        if (!answer) button.focus();
        resolve(answer);
      };
      yesBtn.addEventListener('click', () => done(true));
      noBtn.addEventListener('click', () => done(false));
      box.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); done(false); } });
      button.hidden = true;
      button.after(box);
      noBtn.focus();
    });
  }

  // ---- talking to the app ------------------------------------------------------------

  class ApiError extends Error {
    constructor(message, status) {
      super(message);
      this.status = status;
    }
  }

  function errorText(data, status) {
    if (data && typeof data.error === 'string' && data.error) return data.error;
    if (data && typeof data.detail === 'string' && data.detail) return data.detail;
    if (data && Array.isArray(data.detail) && data.detail[0] && data.detail[0].msg) return 'That was refused: ' + data.detail[0].msg + '.';
    if (status === 401) return 'The app asked for the password again. Reload the page to sign in.';
    if (status === 403) return 'The app refused that.';
    if (status === 413) return 'That is bigger than the app allows.';
    if (status === 502 || status === 504) return "The relay didn't answer. Try again in a moment.";
    return `The app answered with an error (${status}).`;
  }

  async function api(method, path, body) {
    const options = { method, headers: { Accept: 'application/json' }, credentials: 'same-origin', cache: 'no-store' };
    if (method !== 'GET') options.headers['X-Gameday-Action'] = '1';
    if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(path, options);
    } catch (e) {
      throw new ApiError("Can't reach the app. Check the connection.", 0);
    }
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
    if (!response.ok) throw new ApiError(errorText(data, response.status), response.status);
    return data;
  }

  /** Runs `work` with `button` disabled, and shows what went wrong in `noteId`. */
  async function act(button, noteId, work) {
    if (button) button.disabled = true;
    try {
      return await work();
    } catch (e) {
      if (noteId) note(noteId, e.message || String(e), 'bad');
      return undefined;
    } finally {
      if (button) button.disabled = false;
      renderSetup();
    }
  }

  // ---- polling: every 2 s while a game runs, every 10 s otherwise, never while hidden ----

  function busy() {
    if (S.upload) return true;
    const worker = S.status && S.status.worker;
    if (worker && (worker.busy || worker.queued > 0)) return true;
    return (S.jobs || []).some((j) => j.state === 'running' || j.state === 'queued');
  }

  function schedule() {
    clearTimeout(S.timer);
    if (document.hidden) return;
    S.timer = setTimeout(tick, busy() ? BUSY_MS : IDLE_MS);
  }

  async function tick() {
    if (S.ticking) { S.again = true; return; }
    S.ticking = true;
    clearTimeout(S.timer);
    try {
      const [status, jobs, ra] = await Promise.allSettled([
        api('GET', '/api/status'),
        api('GET', '/api/jobs?limit=20'),
        api('GET', '/remote-approval/status'),
      ]);
      const problems = [];
      if (status.status === 'fulfilled') S.status = status.value; else problems.push(status.reason.message);
      if (jobs.status === 'fulfilled') takeJobs(jobs.value); else problems.push(jobs.reason.message);
      if (ra.status === 'fulfilled') { S.ra = ra.value; S.raError = ''; } else S.raError = ra.reason.message;
      S.lastError = problems[0] || '';
      if (S.settings === null) await loadSettings();
      renderAll();
      await loadForView(false);
    } finally {
      S.ticking = false;
      if (S.again) { S.again = false; tick(); } else schedule();
    }
  }

  function takeJobs(list) {
    const jobs = Array.isArray(list) ? list : [];
    const sig = jobs.map((j) => `${j.id}:${j.state}:${j.clips}`).join(',');
    if (sig !== S.jobSig) { S.clipsStale = true; S.jobSig = sig; }
    S.jobs = jobs;
  }

  async function loadForView(force) {
    if (S.view === 'gameday') await loadEvents();
    if (S.view === 'clips' && (force || S.clipsStale || S.clips === null)) await loadClips();
    if (S.view === 'offers') await loadReviews();
    if (S.view === 'setup') {
      if (force) await loadSettings();
      await loadWatches();
    }
    if (S.view === 'usage') await loadUsage();
  }

  async function loadSettings() {
    try {
      S.settings = await api('GET', '/api/settings');
      renderOptions();
    } catch (e) {
      note('options-note', e.message, 'bad');
    }
  }

  async function loadWatches() {
    try {
      S.watches = await api('GET', '/api/watches');
      renderWatches();
      renderSetup();
    } catch (e) {
      note('watches-note', e.message, 'bad');
    }
  }

  async function loadClips() {
    const job = $('clips-job').value;
    try {
      const list = await api('GET', '/api/clips?limit=50' + (job ? '&job_id=' + encodeURIComponent(job) : ''));
      S.clips = Array.isArray(list) ? list : [];
      S.clipsStale = false;
      note('clips-note', '');
      renderClips();
    } catch (e) {
      note('clips-note', e.message, 'bad');
    }
  }

  async function loadReviews() {
    try {
      const got = await api('GET', '/remote-approval/reviews?limit=20');
      S.reviews = got && Array.isArray(got.reviews) ? got.reviews : [];
      renderOffers();
    } catch (e) {
      note('offers-note', e.message, 'bad');
    }
  }

  async function loadUsage() {
    try {
      S.usage = await api('GET', '/api/usage');
      note('usage-note', '');
      renderUsage();
    } catch (e) {
      note('usage-note', e.message, 'bad');
    }
  }

  async function loadEvents() {
    try {
      for (let page = 0; page < 20; page++) {
        const got = await api('GET', `/api/events?after=${S.lastEvent}&limit=100`);
        if (!Array.isArray(got) || !got.length) break;
        S.events.push(...got);
        S.lastEvent = Math.max(S.lastEvent, ...got.map((e) => Number(e.id) || 0));
        if (got.length < 100) break;
      }
      if (S.events.length > 300) S.events = S.events.slice(-300);
      renderEvents();
    } catch (e) {
      // The banner already says when the app can't be reached.
    }
  }

  /** One element per item, in order; an item is rebuilt only when its data changed,
   *  and never while it shows an inline confirm. */
  function keyed(container, items, keyOf, sigOf, build) {
    const existing = new Map();
    for (const child of container.children) existing.set(child.dataset.key, child);
    let previous = null;
    for (const item of items) {
      const key = String(keyOf(item));
      const sig = sigOf(item);
      let el = existing.get(key);
      if (!el || (el.dataset.sig !== sig && !el.querySelector('.confirm'))) {
        const fresh = build(item, el);
        fresh.dataset.key = key;
        fresh.dataset.sig = sig;
        if (el && fresh !== el) el.replaceWith(fresh);
        el = fresh;
      }
      existing.delete(key);
      const want = previous ? previous.nextSibling : container.firstChild;
      if (el !== want) container.insertBefore(el, want);
      previous = el;
    }
    for (const stale of existing.values()) stale.remove();
  }

  // ---- views and the phone drawer -----------------------------------------------------

  function viewFromHash() {
    const name = (window.location.hash || '').replace(/^#/, '').split('/')[0];
    return VIEWS.includes(name) ? name : 'gameday';
  }

  function showView(name) {
    if (S.view !== name) window.scrollTo(0, 0);
    S.view = name;
    for (const view of VIEWS) show('view-' + view, view === name);
    for (const link of document.querySelectorAll('.nav-item')) {
      if (link.dataset.view === name) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    closeMenu(false);
    renderAll();
    loadForView(true);
  }

  function openMenu() {
    document.body.classList.add('menu-open');
    show('scrim', true);
    $('menu-open').setAttribute('aria-expanded', 'true');
    $('main').inert = true;
    $('topbar').inert = true;
    window.setTimeout(() => $('menu-close').focus(), 50);
  }

  function closeMenu(returnFocus) {
    if (!document.body.classList.contains('menu-open')) return;
    document.body.classList.remove('menu-open');
    show('scrim', false);
    $('menu-open').setAttribute('aria-expanded', 'false');
    $('main').inert = false;
    $('topbar').inert = false;
    if (returnFocus) $('menu-open').focus();
  }

  function wireMenu() {
    $('menu-open').addEventListener('click', openMenu);
    $('menu-close').addEventListener('click', () => closeMenu(true));
    $('scrim').addEventListener('click', () => closeMenu(true));
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && document.body.classList.contains('menu-open')) closeMenu(true); });
    $('side').addEventListener('click', (e) => { if (e.target.closest('.nav-item, .brand')) closeMenu(true); });
    window.matchMedia('(min-width: 768px)').addEventListener('change', (e) => { if (e.matches) closeMenu(false); });
  }

  function renderAll() {
    renderShell();
    renderGameDay();
    renderSetup();
    if (S.view === 'offers') renderOffersHead();
    if (S.view === 'usage') renderUsage();
  }

  // ---- sidebar, top bar, header pills -------------------------------------------------

  function settingsRA() {
    return (S.ra && S.ra.settings) || null;
  }

  function pill(id, state, title) {
    const el = $(id);
    if (el.dataset.state !== state) el.dataset.state = state;
    el.title = title;
  }

  function renderShell() {
    const st = S.status;
    const set = settingsRA();
    const meta = st && st.meta_key;
    pill('pill-meta', !st ? 'unknown' : meta.from_env || meta.saved ? 'ok' : 'off',
      !st ? 'Checking…' : meta.from_env ? 'Meta key: from the environment' : meta.saved ? 'Meta key: saved' : 'Meta key: not saved');
    const raPill = (id, ok, name) => pill(id, set ? (ok ? 'ok' : 'off') : 'unknown', `${name}: ${set ? (ok ? 'yes' : 'not yet') : 'checking…'}`);
    raPill('pill-relay', set && set.registered, 'Relay connected');
    raPill('pill-pin', set && set.pin_set, 'PIN set');
    raPill('pill-muse', set && set.muse && set.muse.enabled, 'Muse connected');
    for (const platform of ['instagram', 'facebook']) {
      const account = set && set.accounts && set.accounts[platform];
      const name = platform === 'instagram' ? 'Instagram' : 'Facebook';
      if (account && account.connected) pill('pill-' + platform, 'ok', `${name}: ${account.name || 'connected'}`);
      else if (account && account.expired) pill('pill-' + platform, 'warn', `${name}: sign-in expired`);
      else pill('pill-' + platform, set ? 'off' : 'unknown', `${name}: not connected`);
    }

    const budget = st && st.budget;
    const spent = budget ? usd(budget.spent_usd) : '—';
    const limit = budget ? usd(budget.limit_usd) : '—';
    setText('side-spend', spent);
    setText('side-budget', limit);
    setText('head-spend', budget ? `${spent} of ${limit}` : '—');
    const share = budget && budget.limit_usd ? Math.min(100, Math.round((budget.spent_usd / budget.limit_usd) * 100)) : 0;
    $('side-bar').value = share;
    $('side-bar').classList.toggle('over', share >= 100);

    const banner = S.lastError || (S.raError ? 'Phone approval: ' + S.raError : '');
    setText('banner', banner);
    show('banner', !!banner);

    const running = busy();
    show('nav-live', running);
    const open = ((S.ra && S.ra.pending) || []).filter((r) => OPEN_OFFERS.includes(r.state)).length;
    setText('nav-offers', String(open));
    show('nav-offers', open > 0);
    const steps = setupSteps();
    const left = steps ? steps.required.filter((s) => !s.done).length : 0;
    setText('nav-setup', String(left));
    show('nav-setup', left > 0);

    const dot = $('top-dot');
    const dotState = !steps ? 'unknown' : left ? 'todo' : 'ok';
    const words = !steps ? 'Checking setup' : left ? `Setup: ${plural(left, 'step')} left` : 'Setup complete';
    dot.dataset.state = dotState;
    dot.classList.toggle('busy', running);
    dot.title = words + (running ? ' · a game is being clipped' : '');
    dot.setAttribute('aria-label', dot.title);
  }

  // ---- game day ---------------------------------------------------------------------------

  function renderGameDay() {
    const steps = setupSteps();
    if (steps && !steps.allDone) {
      const left = steps.required.filter((s) => !s.done).map((s) => s.name);
      setText('setup-callout-text', `Setup isn't finished: ${left.join(', ')}.`);
      show('setup-callout', true);
    } else {
      show('setup-callout', false);
    }
    const limit = S.status && S.status.upload_limit_mb;
    if (limit) setText('upload-limit', `MP4, MOV or MKV, up to ${size(limit * 1024 * 1024)}.`);
    const s = S.settings;
    if (s) setText('options-line', `${s.mode === 'talk' ? 'Talk' : 'Basketball'} · ${plural(s.max_clips, 'clip')} per game · Title cards ${s.title_cards ? 'on' : 'off'}`);
    renderNow();
    renderJobs();
  }

  function runningJob() {
    const jobs = S.jobs || [];
    const id = S.status && S.status.worker && S.status.worker.job_id;
    return jobs.find((j) => j.id === id && j.state === 'running') || jobs.find((j) => j.state === 'running') || null;
  }

  function renderNow() {
    const job = runningJob();
    show('now', !!job);
    if (!job) return;
    const video = job.video || {};
    setText('now-name', video.name || `Game ${job.id}`);
    setText('now-src', '· ' + (video.source === 'watch' ? 'Watched folder' : 'Upload') + (video.duration_s ? ` · ${clock(video.duration_s)}` : ''));
    const index = Math.max(0, STAGES.indexOf(job.stage));
    const pct = Math.round(Math.max(0, Math.min(1, Number(job.stage_progress) || 0)) * 100);
    setText('now-step', job.stage ? `Stage ${index + 1} of 5 · ${STAGE_LABELS[job.stage]}` : 'Starting…');
    setText('now-pct', job.stage ? `${pct}%` : '');
    setText('now-msg', job.message || '');
    const stages = job.stages || {};
    for (const li of $('stepper').children) paintStage(li, stages[li.dataset.stage], job);
    for (const seg of $('rail').querySelectorAll('.seg')) {
      const stage = stages[seg.dataset.stage] || {};
      const state = stage.state || 'waiting';
      seg.value = state === 'done' || state === 'failed' ? 100 : state === 'running' ? (job.stage === seg.dataset.stage ? pct : 0) : 0;
      seg.className = 'seg ' + state;
    }
    setText('now-total', `Estimated cost so far: ${usd(job.cost_usd, true)}`);
    const sig = `${job.id}:${job.state}`;
    const box = $('now-actions');
    if (box.dataset.sig !== sig || !box.children.length) {
      box.dataset.sig = sig;
      box.replaceChildren(h('button', { type: 'button', class: 'btn small', 'data-act': 'job-cancel', 'data-id': job.id, text: 'Cancel' }));
    }
    renderWarnings($('now-warnings'), job.warnings || []);
  }

  function paintStage(li, stage, job) {
    const state = (stage && stage.state) || 'waiting';
    if (li.dataset.state !== state) li.dataset.state = state;
    let word = STAGE_WORDS[state] || state;
    if (state === 'running' && job.stage === li.dataset.stage) word = `Running… ${Math.round((Number(job.stage_progress) || 0) * 100)}%`;
    setText(li.querySelector('.stage-word'), word);
    let meta = '–';
    if (stage && state !== 'waiting' && state !== 'skipped') {
      const cost = li.dataset.stage === 'render' && !stage.cost_usd ? 'no API cost' : usd(stage.cost_usd, true);
      meta = `${dur(stage.seconds)} · ${cost}`;
    }
    setText(li.querySelector('.stage-meta'), stage && stage.note ? `${meta} · ${stage.note}` : meta);
  }

  function renderWarnings(box, warnings) {
    const sig = JSON.stringify(warnings);
    if (box.dataset.sig === sig) return;
    box.dataset.sig = sig;
    if (!warnings.length) { box.replaceChildren(); return; }
    if (warnings.length === 1) { box.replaceChildren(h('p', { class: 'warn-one', text: '⚠ ' + warnings[0] })); return; }
    box.replaceChildren(h('div', { class: 'warn-box' }, h('p', { text: `${warnings.length} things to know` }),
      h('ul', {}, warnings.map((w) => h('li', { text: w })))));
  }

  function renderJobs() {
    const jobs = S.jobs || [];
    show('jobs-empty', S.jobs !== null && !jobs.length);
    setText('jobs-meta', jobs.length ? `last ${jobs.length}` : '');
    const groups = {
      running: jobs.filter((j) => j.state === 'running'),
      queued: jobs.filter((j) => j.state === 'queued').reverse(),
      failed: jobs.filter((j) => j.state === 'failed'),
      done: jobs.filter((j) => j.state === 'done' || j.state === 'cancelled'),
    };
    for (const [name, list] of Object.entries(groups)) {
      const section = $('group-' + name);
      show(section, list.length > 0);
      setText(section.querySelector('.group-count'), `(${list.length})`);
      keyed(section.querySelector('.jobs'), list, (j) => j.id,
        (j) => JSON.stringify([j.state, j.stage, j.message, j.clips, j.cost_usd, j.warnings, j.error, j.finished_at, Math.floor(Date.now() / 60000)]), buildJob);
    }
    syncClipJobs();
  }

  function buildJob(job) {
    const video = job.video || {};
    let line = JOB_WORDS[job.state] || job.state;
    if (job.state === 'running') line = [job.stage ? STAGE_LABELS[job.stage] : 'Starting…', job.message].filter(Boolean).join(' · ');
    if (job.state === 'done') line = `Clips made · ${plural(job.clips, 'clip')}` + (job.started_at && job.finished_at ? ` · finished in ${dur(job.finished_at - job.started_at)}` : '');
    if (job.state === 'queued') line = 'Waiting';
    const actions = [];
    const button = (act, text, primary) => h('button', { type: 'button', class: 'btn small' + (primary ? ' primary' : ''), 'data-act': act, 'data-id': job.id, text });
    if (job.state === 'queued' || job.state === 'running') actions.push(button('job-cancel', 'Cancel'));
    if (job.state === 'failed' || job.state === 'cancelled') actions.push(button('job-retry', 'Retry'));
    if (job.clips > 0) actions.push(button('job-clips', 'Clips', false));
    return h('li', { class: 'job', 'data-state': job.state },
      h('span', { class: 'job-glyph', title: JOB_WORDS[job.state] || job.state, text: JOB_GLYPHS[job.state] || '•' }),
      h('div', { class: 'job-main' },
        h('p', { class: 'job-title' },
          h('span', { class: 'job-name', text: video.name || `Game ${job.id}` }),
          h('span', { class: 'job-src', text: video.source === 'watch' ? 'Watched folder' : 'Upload' })),
        h('p', { class: 'job-line' + (job.state === 'running' ? ' running' : ''), text: line }),
        job.error ? h('p', { class: 'job-error', text: job.error }) : null,
        (job.warnings || []).map((w) => h('p', { class: 'job-warn', text: '⚠ ' + w })),
        h('p', { class: 'chips' },
          h('span', { class: 'chip', text: `Game ${job.id}` }),
          video.duration_s ? h('span', { class: 'chip', text: clock(video.duration_s) }) : null,
          h('span', { class: 'chip', text: `added ${ago(job.created_at)}` }),
          h('span', { class: 'chip', text: `${usd(job.cost_usd, true)} est.` }))),
      h('div', { class: 'job-actions' }, actions));
  }

  async function jobAction(button) {
    const id = button.dataset.id;
    const what = button.dataset.act;
    if (what === 'job-clips') {
      $('clips-job').value = String(id);
      S.clipsStale = true;
      if (window.location.hash !== '#clips') window.location.hash = 'clips';
      else loadClips();
      return;
    }
    if (what === 'job-cancel' && !(await confirmInline(button, 'Stop this game?', 'Stop'))) return;
    await act(button, 'jobs-note', async () => {
      await api('POST', `/api/jobs/${encodeURIComponent(id)}/${what === 'job-cancel' ? 'cancel' : 'retry'}`);
      note('jobs-note', what === 'job-cancel' ? 'Stopping it. Clips already made are kept.' : 'It will run again.', 'ok');
      tick();
    });
  }

  function renderEvents() {
    const level = S.eventLevel;
    const events = S.events.filter((e) => !level || e.level === level).reverse();
    show('events-empty', !events.length);
    setText('events-empty', S.events.length ? 'Nothing to show for this filter.' : 'Waiting for events…');
    keyed($('events'), events, (e) => e.id, () => '1', (e) => h('li', { class: e.level || 'info' },
      h('time', { datetime: new Date(toMs(e.at)).toISOString(), text: timeOnly(e.at) }),
      h('span', { class: 'msg' }, e.message,
        e.job_id ? [' ', h('a', { href: '#clips', 'data-job': e.job_id, text: `Game ${e.job_id} ↗` })] : null)));
  }

  // ---- upload --------------------------------------------------------------------------------

  function startUpload(file) {
    if (!file) return;
    if (S.upload) { note('upload-note', 'A game is already uploading. Wait for it to finish.', 'warn'); return; }
    const limitMb = S.status && S.status.upload_limit_mb;
    if (limitMb && file.size > limitMb * 1024 * 1024) {
      note('upload-note', `${file.name} is ${size(file.size)}, more than the ${size(limitMb * 1024 * 1024)} the app takes.`, 'bad');
      return;
    }
    const xhr = new XMLHttpRequest();
    const started = Date.now();
    S.upload = xhr;
    note('upload-note', '');
    setText('upload-name', file.name);
    setText('upload-nums', `0 of ${size(file.size)}`);
    $('upload-bar').value = 0;
    show('upload', true);
    $('drop').classList.add('busy');
    $('pick').disabled = true;

    xhr.open('POST', '/api/upload?filename=' + encodeURIComponent(file.name));
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.setRequestHeader('X-Gameday-Action', '1');
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.upload.addEventListener('progress', (event) => {
      if (!event.lengthComputable) return;
      const pct = Math.floor((event.loaded / event.total) * 100);
      $('upload-bar').value = pct;
      const elapsed = (Date.now() - started) / 1000;
      const rate = elapsed > 1 ? event.loaded / elapsed : 0;
      const left = rate ? (event.total - event.loaded) / rate : 0;
      setText('upload-nums', `${pct}% · ${size(event.loaded)} of ${size(event.total)}` + (left > 1 ? ` · about ${dur(left)} left` : ''));
    });
    const finish = () => {
      S.upload = null;
      show('upload', false);
      $('drop').classList.remove('busy');
      $('pick').disabled = false;
      $('file').value = '';
    };
    xhr.addEventListener('load', () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText || 'null'); } catch (e) { data = null; }
      finish();
      if (xhr.status >= 200 && xhr.status < 300) {
        note('upload-note', `Uploaded ${file.name}.` + (data && data.job_id ? ` Game ${data.job_id} is in the queue.` : ''), 'ok');
        tick();
      } else {
        note('upload-note', xhr.status === 413 ? `${file.name} is bigger than the app allows.` : errorText(data, xhr.status), 'bad');
      }
    });
    xhr.addEventListener('error', () => { finish(); note('upload-note', 'The upload stopped: the connection dropped. Try again.', 'bad'); });
    xhr.addEventListener('abort', () => { finish(); note('upload-note', 'Upload stopped.', 'warn'); });
    xhr.send(file);
    renderShell();
    schedule();
  }

  function wireUpload() {
    const drop = $('drop');
    $('pick').addEventListener('click', () => $('file').click());
    $('file').addEventListener('change', () => startUpload($('file').files[0]));
    $('upload-cancel').addEventListener('click', () => { if (S.upload) S.upload.abort(); });
    // A file dropped anywhere else would make the browser open it instead.
    window.addEventListener('dragover', (e) => e.preventDefault());
    window.addEventListener('drop', (e) => e.preventDefault());
    drop.addEventListener('dragenter', (e) => { e.preventDefault(); drop.classList.add('over'); });
    drop.addEventListener('dragover', (e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; drop.classList.add('over'); });
    drop.addEventListener('dragleave', (e) => { if (!drop.contains(e.relatedTarget)) drop.classList.remove('over'); });
    drop.addEventListener('drop', (e) => {
      e.preventDefault();
      drop.classList.remove('over');
      const files = e.dataTransfer && e.dataTransfer.files;
      if (files && files.length) startUpload(files[0]);
    });
  }

  // ---- clips ---------------------------------------------------------------------------------

  function jobName(id) {
    const job = (S.jobs || []).find((j) => j.id === Number(id));
    return (job && job.video && job.video.name) || `Game ${id}`;
  }

  function syncClipJobs() {
    const select = $('clips-job');
    const jobs = (S.jobs || []).filter((j) => j.clips > 0 || String(j.id) === select.value);
    const sig = jobs.map((j) => `${j.id}:${j.clips}`).join(',');
    if (select.dataset.sig === sig) return;
    select.dataset.sig = sig;
    const chosen = select.value;
    select.replaceChildren(h('option', { value: '', text: 'All games' }),
      ...jobs.map((j) => h('option', { value: String(j.id), text: `${jobName(j.id)} (${plural(j.clips, 'clip')})` })));
    select.value = chosen;
    if (select.value !== chosen) select.value = '';
  }

  function followText(clip) {
    const follow = clip.follow;
    if (!follow) return ['No tracking for this clip', 'none'];
    const coverage = Number(follow.ball_coverage) || 0;
    const onBall = follow.source === 'ball' || follow.source === 'blend';
    const text = follow.note || (onBall ? `Followed the ball ${Math.round(coverage * 100)}% of the time` : 'Kept the frame centred');
    return [text, onBall && coverage >= 0.5 ? '' : 'weak'];
  }

  function renderClips() {
    const all = S.clips || [];
    const query = $('clips-search').value.trim().toLowerCase();
    const cats = [...new Set(all.map((c) => c.category).filter(Boolean))];
    if (S.clipCat && !cats.includes(S.clipCat)) S.clipCat = '';
    keyed($('clips-cats'), all.length ? ['', ...cats] : [], (c) => c || 'all', (c) => String(c === S.clipCat),
      (c) => h('button', { type: 'button', class: 'filter', 'data-cat': c, 'aria-pressed': String(c === S.clipCat), text: c ? (CATEGORY_LABELS[c] || c) : 'All' }));
    const shown = all.filter((c) => (!S.clipCat || c.category === S.clipCat)
      && (!query || [c.title, c.label, c.category, c.team, c.player].join(' ').toLowerCase().includes(query)));
    setText('clips-count', String(shown.length));
    show('clips-empty', S.clips !== null && !all.length);
    const groups = [];
    for (const clip of shown) {
      let group = groups.find((g) => g.job === clip.job_id);
      if (!group) { group = { job: clip.job_id, clips: [] }; groups.push(group); }
      group.clips.push(clip);
    }
    keyed($('clip-groups'), groups, (g) => g.job, () => 'group', (g) => h('section', { class: 'clip-group' },
      h('p', { class: 'game-box' }, h('b'), h('span')), h('div', { class: 'clips' })));
    for (const group of groups) {
      const el = $('clip-groups').querySelector(`[data-key="${group.job}"]`);
      const well = group.clips.filter((c) => followText(c)[1] === '').length;
      setText(el.querySelector('.game-box b'), jobName(group.job));
      setText(el.querySelector('.game-box span'), ` · ${plural(group.clips.length, 'clip')} · ${well} followed the ball well`);
      keyed(el.querySelector('.clips'), group.clips, (c) => c.id,
        (c) => JSON.stringify([c.title, c.label, c.score, c.category, c.team, c.player, c.follow, c.video_url, c.thumb_url, c.debug_url]), buildClip);
    }
  }

  function buildClip(clip, old) {
    // A clip whose words changed keeps its player, so playback isn't cut off.
    const src = mediaUrl(clip.video_url);
    const kept = old ? old.querySelector('video') : null;
    const player = kept && kept.dataset.src === src ? kept : h('video', {
      controls: true, preload: 'none', playsinline: true, poster: mediaUrl(clip.thumb_url) || null, src: src || null,
      'data-src': src, 'aria-label': clip.title || 'Clip',
    });
    const score = Math.round(Number(clip.score) || 0);
    const length = Number(clip.end_s) - Number(clip.start_s);
    const [follow, followClass] = followText(clip);
    const debug = mediaUrl(clip.debug_url);
    const how = debug ? h('details', {
      class: 'how',
      ontoggle: (e) => {
        const img = e.currentTarget.querySelector('img');
        if (e.currentTarget.open && !img.getAttribute('src')) img.setAttribute('src', debug);
      },
    },
    h('summary', { text: 'How it follows the ball' }),
    h('img', { alt: `A wide frame from "${clip.title || 'this clip'}" with SAM 3.1's box on the ball and the 9:16 window`, loading: 'lazy' }),
    h('p', { text: "SAM 3.1's box on the ball, and the 9:16 window the crop follows." })) : null;
    const meta = [CATEGORY_LABELS[clip.category] || clip.category, clip.team, clip.player].filter(Boolean).join(' · ');
    return h('article', { class: 'clip' },
      h('div', { class: 'clip-media' },
        player,
        h('span', { class: 'score' + (score >= 80 ? ' high' : score >= 65 ? ' mid' : ''), title: 'Score out of 100', text: String(score) }),
        length > 0 ? h('span', { class: 'dur', text: Math.round(length) + 's' }) : null),
      h('div', { class: 'clip-body' },
        h('h3', { class: 'clip-title', text: clip.title || clip.label || `Clip ${clip.index}` }),
        meta ? h('p', { class: 'clip-meta', text: meta }) : null,
        h('p', { class: 'follow ' + followClass, text: follow }),
        how,
        h('p', { class: 'clip-foot' },
          h('span', { text: `at ${clock(clip.start_s)}` }),
          src ? h('a', { href: src, download: '', text: 'Download' }) : null)));
  }

  // ---- phone offers -------------------------------------------------------------------------

  function renderOffersHead() {
    const set = settingsRA();
    show('offers-off', !!set && set.registered && !set.remote_approvals);
    let phone = '';
    if (!S.ra) phone = S.raError ? 'Phone approval: ' + S.raError : 'Checking…';
    else if (!set.registered) phone = 'Not connected to a relay yet. Connect one in Setup.';
    else {
      const ways = [];
      if (set.muse && set.muse.enabled) ways.push('Muse');
      if (set.whatsapp_enabled && set.pairing && set.pairing.state === 'confirmed') ways.push('WhatsApp');
      phone = ways.length ? `Offers reach you through ${ways.join(' and ')}.` : 'Connect Muse or WhatsApp in Setup so offers reach you.';
      const outbox = Number(S.ra.outbox_pending) || 0;
      if (outbox) phone += ` ${plural(outbox, 'message')} waiting to go to the relay.`;
    }
    setText('offers-phone', phone);
    $('offers-refresh').disabled = !(set && set.registered);
    const u = S.ra && S.ra.usage;
    if (u) {
      const wa = u.whatsapp || {};
      const messages = (Number(wa.templates) || 0) + (Number(wa.free_form) || 0);
      const graph = u.meta_graph || {};
      setText('offers-cost', `Estimated messaging cost so far: ${usd(wa.usd)} · WhatsApp ${plural(messages, 'message')} · ${plural(Number(graph.uploads) || 0, 'Reel')} uploaded`);
    } else {
      setText('offers-cost', '');
    }
  }

  function renderOffers() {
    renderOffersHead();
    const reviews = S.reviews || [];
    show('offers-empty', S.reviews !== null && !reviews.length);
    keyed($('offers'), reviews, (r) => r.id, (r) => JSON.stringify(r), buildOffer);
  }

  function buildOffer(review) {
    let [word, tone] = OFFER_WORDS[review.state] || [review.state, ''];
    if (review.state === 'NOTIFICATION_PENDING' && review.error) [word, tone] = ['Not delivered', 'bad'];
    const platforms = (review.decision && review.decision.platforms && review.decision.platforms.length)
      ? review.decision.platforms.map((p) => PLATFORM_LABELS[p] || p).join(', ') : '';
    const line2 = [plural((review.clips || []).length, 'clip'), platforms, `sent ${ago(review.created_at)}`];
    if (OPEN_OFFERS.includes(review.state) && review.expires_at) line2.push(`link works until ${at(review.expires_at)}`);
    if (review.state === 'DEFERRED' && review.remind_at) line2.push(`reminder at ${at(review.remind_at)}`);
    const buttons = [];
    const button = (what, text) => h('button', { type: 'button', class: 'btn small', 'data-act': 'offer', 'data-what': what, 'data-id': review.id, text });
    if (review.can_cancel) buttons.push(button('cancel', 'Cancel'));
    if (review.can_retry) buttons.push(button('retry', 'Retry'));
    if (review.can_ask_again) buttons.push(button('ask-again', 'Ask again'));
    if (review.can_resend) buttons.push(button('resend', 'Send again'));
    const posted = ((review.results || {}).clips || []).map((clip) => h('p', { class: 'offer-line' },
      (clip.title || `Clip ${clip.clip_id}`) + ': ',
      (clip.posts || []).map((post) => {
        const name = PLATFORM_LABELS[post.platform] || post.platform;
        const link = safeUrl(post.url);
        if (post.state === 'published' && link) return h('a', { href: link, target: '_blank', rel: 'noopener noreferrer', text: name + ' ↗' });
        if (post.state === 'published') return h('span', { class: 'ok-text', text: name + ' ' });
        const tone2 = post.state === 'failed' ? 'bad-text' : post.state === 'unconfirmed' ? 'warn-text' : 'dim';
        return h('span', { class: tone2, text: `${name}: ${post.state}${post.error ? ` (${post.error})` : ''} ` });
      })));
    return h('article', { class: 'offer' },
      h('div', { class: 'offer-top' },
        h('h3', { class: 'offer-title', text: review.title || `Game ${review.job_id}` }),
        h('span', { class: 'offer-state ' + tone, text: word }),
        buttons.length ? h('span', { class: 'btns' }, buttons) : null),
      h('p', { class: 'offer-line', text: line2.filter(Boolean).join(' · ') }),
      review.summary ? h('p', { class: 'offer-line', text: review.summary }) : null,
      review.result_text ? h('p', { class: 'offer-line', text: review.result_text }) : null,
      posted,
      review.error ? h('p', { class: 'note bad', text: review.error }) : null,
      h('p', { class: 'note', role: 'status' }));
  }

  async function offerAction(button) {
    const id = button.dataset.id;
    const what = button.dataset.what;
    const row = button.closest('.offer');
    const line = row && row.querySelector('.note[role="status"]');
    if (what === 'cancel' && !(await confirmInline(button, 'Cancel this offer?', 'Cancel offer'))) return;
    button.disabled = true;
    try {
      const got = await api('POST', `/remote-approval/reviews/${encodeURIComponent(id)}/${what}`);
      const done = { cancel: 'Cancelled.', retry: 'Queued again.', 'ask-again': `Offered again${got && got.review_id ? ` as offer ${got.review_id}` : ''}.`, resend: 'Sent again.' };
      note('offers-note', done[what] || 'Done.', 'ok');
      await loadReviews();
      tick();
    } catch (e) {
      button.disabled = false;
      if (line) { line.textContent = e.message; line.className = 'note bad'; } else note('offers-note', e.message, 'bad');
    }
  }

  // ---- setup ----------------------------------------------------------------------------------

  /** Which steps are done. Null until the app has answered once. */
  function setupSteps() {
    const st = S.status;
    if (!st) return null;
    const set = settingsRA() || {};
    const accounts = set.accounts || {};
    const meta = !!(st.meta_key && (st.meta_key.saved || st.meta_key.from_env));
    const relay = !!set.registered;
    const pin = !!set.pin_set;
    const muse = !!(set.muse && set.muse.enabled);
    const whatsapp = !!(set.whatsapp_enabled && set.pairing && set.pairing.state === 'confirmed');
    const facebook = !!(accounts.facebook && accounts.facebook.connected);
    const instagram = !!(accounts.instagram && accounts.instagram.connected);
    const approvals = !!set.remote_approvals;
    const required = [
      { name: 'Meta key', done: meta },
      { name: 'Relay', done: relay },
      { name: 'PIN', done: pin },
      { name: 'Muse or WhatsApp', done: muse || whatsapp },
      { name: 'Facebook', done: facebook },
      { name: 'Phone approval', done: approvals },
    ];
    return { meta, relay, pin, muse, whatsapp, facebook, instagram, approvals, required, allDone: required.every((s) => s.done) };
  }

  /** A step's badge, status line, and whether it is open: a done step folds to its status
   *  line unless it was opened by hand. */
  function paintStep(id, done, stateText) {
    const step = $(id);
    step.dataset.done = done ? 'true' : 'false';
    setText(step.querySelector('.step-state'), stateText);
    const open = id in S.stepOpen ? S.stepOpen[id] : !done;
    step.querySelector('.step-head').setAttribute('aria-expanded', String(open));
    show(step.querySelector('.step-body'), open);
  }

  function renderSetup() {
    const steps = setupSteps();
    if (!steps) return;
    const st = S.status;
    const set = settingsRA() || {};
    const relayKnown = !!S.ra;
    const noRelay = !steps.relay;
    const locked = relayKnown ? 'Connect your relay first.' : S.raError ? "Can't read the relay settings." : 'Checking…';

    // The checklist card, which folds to one line once every required step is done.
    const doneCount = steps.required.filter((s) => s.done).length;
    keyed($('setup-rail'), steps.required, (s) => s.name, (s) => String(s.done), (s) => h('li', { class: s.done ? 'done' : '' }));
    $('checklist').classList.toggle('all-done', steps.allDone);
    setText('setup-title', steps.allDone ? '✓ Setup complete' : 'Get ready for game day');
    setText('setup-count', `${doneCount} of ${steps.required.length} done`);
    const stepsOpen = !steps.allDone || S.showSteps;
    show('steps', stepsOpen);
    show('steps-toggle', steps.allDone);
    setText('steps-toggle', stepsOpen ? 'Hide steps ▾' : 'Show steps ▸');
    $('steps-toggle').setAttribute('aria-expanded', String(stepsOpen));

    // 1. Meta key
    const fromEnv = !!(st.meta_key && st.meta_key.from_env);
    const saved = !!(st.meta_key && st.meta_key.saved);
    paintStep('step-meta', steps.meta, fromEnv ? 'Key from the environment.' : saved ? 'Key saved.' : 'Not set.');
    show('meta-saved', steps.meta);
    setText('meta-saved-text', fromEnv ? 'Key from the environment' : 'Key saved');
    show('meta-env-tag', fromEnv);
    show('meta-env-note', fromEnv);
    show('meta-remove', !fromEnv && saved);
    show('meta-form', !steps.meta);
    $('meta-save').disabled = !$('meta-key').value.trim();

    // 2. Relay
    paintStep('step-relay', steps.relay, steps.relay ? 'Connected.' : relayKnown ? 'Not connected.' : locked);
    show('relay-connected', steps.relay);
    setText('relay-url-shown', set.relay_url || '');
    show('relay-form', relayKnown && !steps.relay);
    const urlInput = $('relay-url');
    const env = (S.ra && S.ra.env) || {};
    const knownUrl = set.relay_url || env.relay_url || '';
    if (!urlInput.value && document.activeElement !== urlInput && knownUrl) urlInput.value = knownUrl;
    $('relay-connect').disabled = !urlInput.value.trim();
    $('relay-key').placeholder = env.enrolment_key ? 'Set where the app runs: leave it empty' : '';

    // 3. PIN
    paintStep('step-pin', steps.pin, noRelay ? locked : steps.pin ? 'PIN set.' : 'No PIN yet.');
    for (const id of ['pin', 'pin-save']) $(id).disabled = noRelay;
    setText('pin-save', steps.pin ? 'Change PIN' : 'Set PIN');

    // 4. Muse
    const muse = set.muse || {};
    let museText = noRelay ? locked : 'No key yet.';
    if (!noRelay && muse.enabled) museText = 'Connected · ' + (muse.last_used ? `last used ${ago(muse.last_used)}` : 'not used yet');
    else if (!noRelay && steps.whatsapp) museText = 'Not connected. Optional, since WhatsApp is paired.';
    paintStep('step-muse', steps.muse, museText);
    const relayUrl = safeUrl(set.relay_url, true);
    setText('muse-address', relayUrl ? relayUrl.replace(/\/$/, '') + '/mcp' : '');
    show('muse-address-box', !!relayUrl && !noRelay);
    $('muse-make').disabled = noRelay;
    setText('muse-make', muse.enabled ? 'Make a new key' : 'Make a Muse key');
    $('muse-make').classList.toggle('primary', !muse.enabled);
    show('muse-revoke', !!muse.enabled);

    // 5. Facebook + Instagram
    const accounts = set.accounts || {};
    const fb = accounts.facebook || {};
    const ig = accounts.instagram || {};
    paintStep('step-accounts', steps.facebook, noRelay ? locked
      : steps.facebook ? `Facebook${steps.instagram ? ' and Instagram' : ''} connected.` : 'Not connected.');
    const waiting = S.connectPending && !fb.connected;
    setText('fb-state', fb.connected ? fb.name || 'Connected' : fb.expired ? 'Sign-in expired. Connect again.' : waiting ? 'Waiting for sign-in…' : 'Not connected');
    $('fb-state').className = 'kv-value' + (fb.connected ? ' ok-text' : fb.expired || waiting ? ' warn-text' : '');
    setText('ig-state', ig.connected ? (ig.name || 'Connected') + ' · through your Facebook Page' : ig.expired ? 'Sign-in expired. Connect Facebook again.'
      : fb.connected ? 'No Instagram account is linked to that Page.' : 'Comes with your Facebook Page');
    $('ig-state').className = 'kv-value' + (ig.connected ? ' ok-text' : ig.expired ? ' warn-text' : '');
    show('fb-connect', !fb.connected);
    show('fb-disconnect', !!fb.connected);
    show('ig-disconnect', !!ig.connected);
    show('accounts-wait', waiting);
    for (const id of ['fb-connect', 'fb-disconnect', 'ig-disconnect', 'accounts-check']) $(id).disabled = noRelay;

    // 6. WhatsApp (optional)
    const pairing = set.pairing || {};
    let waText = noRelay ? locked : 'Off.';
    if (!noRelay && set.whatsapp_enabled) {
      waText = pairing.state === 'confirmed' ? `Paired with the number ending in ${pairing.tail || '····'}.`
        : pairing.state === 'awaiting_confirm' ? 'A code arrived. Confirm the number.'
          : pairing.state === 'pending' ? 'Waiting for the code from your phone.' : 'On, but no number is paired yet.';
    }
    paintStep('step-whatsapp', steps.whatsapp, waText);
    const waBox = $('wa-enabled');
    if (document.activeElement !== waBox) waBox.checked = !!set.whatsapp_enabled;
    waBox.disabled = noRelay;
    show('wa-more', !noRelay && !!set.whatsapp_enabled);
    show('wa-paired', pairing.state === 'confirmed');
    setText('wa-tail-ok', pairing.tail || '····');
    show('wa-confirm-box', pairing.state === 'awaiting_confirm');
    setText('wa-tail', pairing.tail || '····');
    if (pairing.state === 'confirmed' || pairing.state === 'awaiting_confirm') show('wa-pair-box', false);
    setText('wa-pair', pairing.state === 'confirmed' ? 'Pair a different number' : 'Pair my WhatsApp number');
    $('wa-test').disabled = pairing.state !== 'confirmed';
    for (const [id, key] of [['wa-finish', 'notify_on_finish'], ['wa-fail', 'notify_on_fail'], ['wa-publish', 'notify_on_publish']]) {
      if (document.activeElement !== $(id)) $(id).checked = !!set[key];
    }
    const templates = Object.entries(set.templates || {});
    keyed($('wa-templates'), templates.length ? templates : [['', '']], (t) => t[0] || 'none', (t) => t[1],
      (t) => (t[0]
        ? h('li', {}, h('span', { class: 'mono', text: t[0] }),
          h('span', { class: t[1] === 'APPROVED' ? 'ok-text' : t[1] === 'REJECTED' ? 'bad-text' : 'warn-text', text: TEMPLATE_WORDS[t[1]] || String(t[1]).toLowerCase() }))
        : h('li', { text: 'Not checked yet. Meta must approve them before WhatsApp can message you first.' })));

    // Publishing
    const why = (S.ra && S.ra.can_enable_remote_approval) || '';
    const box = $('approvals');
    if (document.activeElement !== box) box.checked = !!set.remote_approvals;
    box.disabled = noRelay || (!set.remote_approvals && !!why);
    setText('approvals-why', noRelay ? locked : !set.remote_approvals && why ? why
      : 'Finished games are offered on your phone. Nothing is posted until you approve it there with your PIN.');
    show('approvals-more', !!set.remote_approvals);
    if (document.activeElement !== $('offer-other')) $('offer-other').checked = set.basketball_only === false;
    const preferred = set.preferred_destinations || [];
    for (const platform of ['instagram', 'facebook']) {
      const input = $('pref-' + platform);
      if (document.activeElement !== input) input.checked = preferred.includes(platform);
      input.disabled = !(accounts[platform] && accounts[platform].connected);
    }
    for (const [id, key, fallback] of [['approval-hours', 'approval_hours', 24], ['remind-minutes', 'remind_minutes', 120]]) {
      const select = $(id);
      if (document.activeElement === select) continue;
      const value = String(set[key] || fallback);
      if (![...select.options].some((o) => o.value === value)) select.append(h('option', { value, text: value }));
      select.value = value;
    }
  }

  function renderOptions() {
    const s = S.settings;
    if (!s) return;
    const put = (id, apply) => { const el = $(id); if (document.activeElement !== el) apply(el); };
    put('opt-mode', (el) => { el.value = s.mode; });
    put('opt-clips', (el) => { el.value = String(s.max_clips); });
    put('opt-hint', (el) => { el.value = s.hint || ''; });
    put('opt-cards', (el) => { el.checked = !!s.title_cards; });
    renderGameDay();
  }

  function renderWatches() {
    const watches = S.watches || [];
    show('watches-empty', !watches.length);
    keyed($('watches'), watches, (w) => w.id, (w) => JSON.stringify([w, Math.floor(Date.now() / 60000)]), (w) => h('li', { class: 'kv' },
      h('span', { class: 'badge-tag', text: 'FOLDER' }),
      h('span', { class: 'watch-path', text: w.path }),
      h('span', { class: 'live-pill' + (w.enabled ? '' : ' off'), text: w.enabled ? 'Watching' : 'Paused' }),
      h('span', { class: 'btns push' },
        h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: !!w.enabled, 'data-act': 'watch-toggle', 'data-id': w.id }), 'On'),
        h('button', { type: 'button', class: 'btn small', 'data-act': 'watch-remove', 'data-id': w.id, text: 'Remove' })),
      h('span', { class: 'watch-facts' + (w.error ? ' bad-text' : ''), text: w.error
        || (w.last_checked_at ? `Checked ${ago(w.last_checked_at)}` : 'Not checked yet') + ` · ${plural(Number(w.videos) || 0, 'game')} found` })));
  }

  function takeRA(got) {
    if (got && got.settings) S.ra = { ...(S.ra || {}), ...got };
    renderAll();
  }

  async function patchRA(patch, noteId, okText) {
    const got = await api('PATCH', '/remote-approval/settings', patch);
    takeRA(got);
    if (got && got.relay_error) note(noteId, `Saved here, but the relay didn't get it: ${got.relay_error}`, 'warn');
    else if (okText) note(noteId, okText, 'ok');
    return got;
  }

  let savedTimer = 0;
  async function saveOptions(patch) {
    await act(null, 'options-note', async () => {
      S.settings = await api('PATCH', '/api/settings', patch);
      note('options-note', '');
      renderOptions();
      show('options-saved', true);
      clearTimeout(savedTimer);
      savedTimer = window.setTimeout(() => show('options-saved', false), 3000);
    });
  }

  async function copyText(text, noteId) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (e) {
      note(noteId, 'Select it and copy it yourself: this page may not use the clipboard.', 'warn');
      return false;
    }
  }

  function wireSetup() {
    $('steps-toggle').addEventListener('click', () => { S.showSteps = !S.showSteps; writeStore(S.showSteps); renderSetup(); });
    $('steps').addEventListener('click', (e) => {
      const head = e.target.closest('.step-head');
      if (!head) return;
      const step = head.closest('.step');
      S.stepOpen[step.id] = head.getAttribute('aria-expanded') !== 'true';
      renderSetup();
    });

    // 1. Meta key
    $('meta-key').addEventListener('input', () => { $('meta-save').disabled = !$('meta-key').value.trim(); });
    $('meta-show').addEventListener('click', () => {
      const input = $('meta-key');
      input.type = input.type === 'password' ? 'text' : 'password';
      setText('meta-show', input.type === 'password' ? 'Show' : 'Hide');
    });
    $('meta-form').addEventListener('submit', (e) => {
      e.preventDefault();
      const input = $('meta-key');
      const key = input.value.trim();
      if (!key) return;
      act($('meta-save'), 'meta-note', async () => {
        await api('PUT', '/api/meta/key', { key });
        input.value = '';
        input.type = 'password';
        setText('meta-show', 'Show');
        note('meta-note', 'Saved. Press Test to check it works.', 'ok');
        await tick();
      });
    });
    $('meta-test').addEventListener('click', (e) => act(e.currentTarget, 'meta-note', async () => {
      note('meta-note', 'Testing…', 'info');
      const started = Date.now();
      const got = await api('POST', '/api/meta/test');
      if (got && got.ok) note('meta-note', `Key works. Meta answered in ${((Date.now() - started) / 1000).toFixed(1)}s.`, 'ok');
      else note('meta-note', 'Meta turned the key down: ' + ((got && got.error) || 'no reason given.'), 'bad');
    }));
    $('meta-remove').addEventListener('click', async (e) => {
      const button = e.currentTarget;
      if (!(await confirmInline(button, 'Remove the key?', 'Remove'))) return;
      act(button, 'meta-note', async () => {
        await api('DELETE', '/api/meta/key');
        note('meta-note', 'Removed.', 'ok');
        await tick();
      });
    });

    // 2. Relay
    $('relay-url').addEventListener('input', () => { $('relay-connect').disabled = !$('relay-url').value.trim(); });
    $('relay-form').addEventListener('submit', (e) => {
      e.preventDefault();
      const url = $('relay-url').value.trim().replace(/\/+$/, '');
      if (!safeUrl(url, true)) { note('relay-note', 'The relay address must start with https://', 'bad'); return; }
      act($('relay-connect'), 'relay-note', async () => {
        note('relay-note', 'Connecting…', 'info');
        const got = await api('POST', '/remote-approval/register', { relay_url: url, enroll_key: $('relay-key').value.trim() });
        $('relay-key').value = '';
        takeRA(got);
        note('relay-note', 'This dashboard is connected to your relay.', 'ok');
      });
    });
    $('relay-disconnect').addEventListener('click', async (e) => {
      const button = e.currentTarget;
      if (!(await confirmInline(button, 'Disconnect? Open offers are cancelled and Muse stops working.', 'Disconnect'))) return;
      act(button, 'relay-note', async () => {
        takeRA(await api('POST', '/remote-approval/disconnect'));
        note('relay-note', 'Disconnected.', 'ok');
      });
    });

    // 3. PIN
    $('pin-form').addEventListener('submit', (e) => {
      e.preventDefault();
      const input = $('pin');
      const pin = input.value.trim();
      if (!/^\d{4,8}$/.test(pin)) { note('pin-note', 'The PIN must be 4 to 8 digits.', 'bad'); return; }
      if (/^(\d)\1+$/.test(pin) || '0123456789'.includes(pin) || '9876543210'.includes(pin)) {
        note('pin-note', 'Pick a PIN that is harder to guess than a repeat or a run.', 'bad');
        return;
      }
      act($('pin-save'), 'pin-note', async () => {
        const got = await api('POST', '/remote-approval/pin', { pin });
        input.value = '';
        takeRA(got);
        note('pin-note', 'PIN saved.', 'ok');
      });
    });

    // 4. Muse
    $('muse-make').addEventListener('click', async (e) => {
      const button = e.currentTarget;
      const had = !!((settingsRA() || {}).muse || {}).enabled;
      if (had && !(await confirmInline(button, 'The old key stops working.', 'Make a new key'))) return;
      act(button, 'muse-note', async () => {
        const got = await api('POST', '/remote-approval/muse/token');
        S.stepOpen['step-muse'] = true; // the key is inside the step: keep it open while it shows
        const key = (got && got.key) || '';
        takeRA(got);
        if (!key) { note('muse-note', "The relay didn't send a key. Try again.", 'bad'); return; }
        setText('muse-key', key);
        show('muse-secret', true);
        note('muse-note', '');
      });
    });
    $('muse-copy').addEventListener('click', async (e) => {
      const button = e.currentTarget;
      if (await copyText($('muse-key').textContent, 'muse-note')) {
        setText(button, 'Copied');
        button.classList.add('sky-text');
        window.setTimeout(() => { setText(button, 'Copy'); button.classList.remove('sky-text'); }, 2000);
      }
    });
    $('muse-address-copy').addEventListener('click', async () => {
      if (await copyText($('muse-address').textContent, 'muse-note')) note('muse-note', 'Address copied.', 'ok');
    });
    $('muse-done').addEventListener('click', () => {
      setText('muse-key', '');
      show('muse-secret', false);
      note('muse-note', 'The key is no longer shown here.', 'ok');
    });
    $('muse-revoke').addEventListener('click', async (e) => {
      const button = e.currentTarget;
      if (!(await confirmInline(button, 'Revoke this key?', 'Revoke'))) return;
      act(button, 'muse-note', async () => {
        takeRA(await api('POST', '/remote-approval/muse/revoke'));
        setText('muse-key', '');
        show('muse-secret', false);
        note('muse-note', 'Revoked. Muse can no longer reach your offers.', 'ok');
      });
    });

    // 5. Facebook + Instagram
    $('fb-connect').addEventListener('click', (e) => {
      // Opened during the click, so the browser doesn't block it; pointed at
      // Meta's sign-in once the relay has made the link.
      const tab = window.open('', '_blank');
      act(e.currentTarget, 'accounts-note', async () => {
        try {
          const got = await api('POST', '/remote-approval/accounts/connect', { platform: 'facebook' });
          const url = safeUrl(got && got.url, true);
          if (!url) throw new ApiError("The relay didn't send a sign-in link.", 0);
          $('accounts-link').href = url;
          if (tab) { tab.opener = null; tab.location.replace(url); }
          S.connectPending = true;
          note('accounts-note', '');
        } catch (err) {
          if (tab) tab.close();
          throw err;
        }
      });
    });
    for (const [id, platform, question] of [['fb-disconnect', 'facebook', 'Disconnect Facebook and its Instagram?'], ['ig-disconnect', 'instagram', 'Disconnect Instagram?']]) {
      $(id).addEventListener('click', async (e) => {
        const button = e.currentTarget;
        if (!(await confirmInline(button, question, 'Disconnect'))) return;
        act(button, 'accounts-note', async () => {
          takeRA(await api('POST', '/remote-approval/accounts/disconnect', { platform }));
          note('accounts-note', 'Disconnected.', 'ok');
        });
      });
    }
    $('accounts-check').addEventListener('click', (e) => act(e.currentTarget, 'accounts-note', async () => {
      takeRA(await api('POST', '/remote-approval/refresh'));
      if (((settingsRA() || {}).accounts || {}).facebook && settingsRA().accounts.facebook.connected) S.connectPending = false;
      note('accounts-note', 'Checked with the relay.', 'ok');
    }));

    // 6. WhatsApp
    $('wa-enabled').addEventListener('change', (e) => act(e.currentTarget, 'wa-note', async () => {
      await patchRA({ whatsapp_enabled: e.target.checked }, 'wa-note', e.target.checked ? 'WhatsApp messages are on.' : 'WhatsApp messages are off.');
    }));
    $('wa-pair').addEventListener('click', (e) => act(e.currentTarget, 'wa-note', async () => {
      const got = await api('POST', '/remote-approval/pair/start');
      setText('wa-code', (got && got.code) || '');
      const link = safeUrl(got && got.link);
      $('wa-link').href = link || '#';
      show('wa-link', !!link);
      setText('wa-expires', got && got.expires_at ? `The code works until ${at(got.expires_at)}.` : '');
      show('wa-pair-box', true);
      note('wa-note', '');
    }));
    $('wa-sent').addEventListener('click', (e) => act(e.currentTarget, 'wa-note', async () => {
      const got = await api('POST', '/remote-approval/refresh');
      takeRA(got);
      const state = ((got && got.settings && got.settings.pairing) || {}).state;
      if (state !== 'awaiting_confirm' && state !== 'confirmed') note('wa-note', "The code hasn't arrived yet. Wait a few seconds and try again.", 'warn');
    }));
    for (const [id, yes] of [['wa-yes', true], ['wa-no', false]]) {
      $(id).addEventListener('click', (e) => act(e.currentTarget, 'wa-note', async () => {
        takeRA(await api('POST', '/remote-approval/pair/confirm', { confirm: yes }));
        note('wa-note', yes ? 'Paired.' : "Not paired. That number won't get messages.", yes ? 'ok' : 'warn');
      }));
    }
    $('wa-test').addEventListener('click', (e) => act(e.currentTarget, 'wa-note', async () => {
      await api('POST', '/remote-approval/test');
      note('wa-note', 'A test message is on its way.', 'ok');
    }));
    for (const [id, key] of [['wa-finish', 'notify_on_finish'], ['wa-fail', 'notify_on_fail'], ['wa-publish', 'notify_on_publish']]) {
      $(id).addEventListener('change', (e) => act(e.currentTarget, 'wa-note', () => patchRA({ [key]: e.target.checked }, 'wa-note', 'Saved.')));
    }
    for (const [id, create] of [['wa-tpl-check', false], ['wa-tpl-submit', true]]) {
      $(id).addEventListener('click', (e) => act(e.currentTarget, 'wa-note', async () => {
        await api('POST', '/remote-approval/templates', { create });
        takeRA(await api('GET', '/remote-approval/status'));
        note('wa-note', create ? 'Sent to Meta for review. It can take a while.' : 'Checked.', 'ok');
      }));
    }

    // Publishing
    $('approvals').addEventListener('change', (e) => act(e.currentTarget, 'approvals-note', async () => {
      const on = e.target.checked;
      try {
        await patchRA({ remote_approvals: on }, 'approvals-note', on ? 'On.' : 'Off.');
      } catch (err) {
        e.target.checked = !on;
        throw err;
      }
    }));
    for (const platform of ['instagram', 'facebook']) {
      $('pref-' + platform).addEventListener('change', (e) => act(e.currentTarget, 'approvals-note', () =>
        patchRA({ preferred_destinations: ['instagram', 'facebook'].filter((p) => $('pref-' + p).checked) }, 'approvals-note', 'Saved.')));
    }
    $('approval-hours').addEventListener('change', (e) => act(e.currentTarget, 'approvals-note', () =>
      patchRA({ approval_hours: Number(e.target.value) }, 'approvals-note', 'Saved.')));
    $('remind-minutes').addEventListener('change', (e) => act(e.currentTarget, 'approvals-note', () =>
      patchRA({ remind_minutes: Number(e.target.value) }, 'approvals-note', 'Saved.')));
    $('offer-other').addEventListener('change', (e) => act(e.currentTarget, 'approvals-note', () =>
      patchRA({ basketball_only: !e.target.checked }, 'approvals-note', 'Saved.')));

    // Watched folders
    $('watch-path').addEventListener('input', () => { $('watch-add').disabled = !$('watch-path').value.trim(); });
    $('watch-form').addEventListener('submit', (e) => {
      e.preventDefault();
      const path = $('watch-path').value.trim();
      if (!path) return;
      act($('watch-add'), 'watches-note', async () => {
        await api('POST', '/api/watches', { path });
        $('watch-path').value = '';
        $('watch-add').disabled = true;
        note('watches-note', 'Watching it.', 'ok');
        await loadWatches();
      });
    });
    $('watches').addEventListener('change', (e) => {
      const input = e.target.closest('[data-act="watch-toggle"]');
      if (!input) return;
      act(input, 'watches-note', async () => {
        await api('PATCH', `/api/watches/${encodeURIComponent(input.dataset.id)}`, { enabled: input.checked });
        await loadWatches();
      });
    });
    $('watches').addEventListener('click', async (e) => {
      const button = e.target.closest('[data-act="watch-remove"]');
      if (!button || !(await confirmInline(button, 'Stop watching?', 'Remove'))) return;
      act(button, 'watches-note', async () => {
        await api('DELETE', `/api/watches/${encodeURIComponent(button.dataset.id)}`);
        note('watches-note', 'Removed. Its files stay where they are.', 'ok');
        await loadWatches();
      });
    });

    // Options
    $('opt-mode').addEventListener('change', (e) => saveOptions({ mode: e.target.value }));
    $('opt-clips').addEventListener('change', (e) => {
      const n = Math.max(1, Math.min(12, Math.round(Number(e.target.value) || 8)));
      e.target.value = String(n);
      saveOptions({ max_clips: n });
    });
    $('opt-hint').addEventListener('change', (e) => saveOptions({ hint: e.target.value.trim().slice(0, 200) }));
    $('opt-cards').addEventListener('change', (e) => saveOptions({ title_cards: e.target.checked }));
  }

  // ---- usage ---------------------------------------------------------------------------------

  function cells(row, labels) {
    [...row.children].forEach((td, i) => td.setAttribute('data-label', labels[i] || ''));
    return row;
  }

  function renderUsage() {
    const u = S.usage;
    const st = S.status;
    const total = u ? u.total_usd : st && st.budget ? st.budget.spent_usd : null;
    const budget = u ? u.budget_usd : st && st.budget ? st.budget.limit_usd : null;
    const byModel = (u && u.by_model) || {};
    const calls = Object.values(byModel).reduce((n, m) => n + (Number(m.calls) || 0), 0);
    const recent = u ? (u.recent || []).slice().sort((a, b) => (b.at || 0) - (a.at || 0)) : [];
    const failed = recent.filter((c) => !c.ok).length;
    const stats = [['Spent', usd(total)], ['Budget', usd(budget)], ['Calls', u ? String(calls) : '—'], ['Failed (last 50)', u ? String(failed) : '—']];
    keyed($('usage-stats'), stats, (s) => s[0], (s) => s[1], (s) => h('span', {}, s[0] + ' ', h('b', { text: s[1] })));
    const bar = $('usage-bar');
    bar.value = budget ? Math.min(100, Math.round(((Number(total) || 0) / budget) * 100)) : 0;
    bar.classList.toggle('over', !!budget && total >= budget);
    if (!u) return;

    const labels = ['Model', 'Calls', 'Frames', 'Est. cost'];
    const models = [...MODEL_ORDER, ...Object.keys(byModel).filter((m) => !MODEL_ORDER.includes(m))];
    keyed($('usage-models').tBodies[0], models, (m) => m, (m) => JSON.stringify(byModel[m] || null), (m) => {
      const row = byModel[m] || { calls: 0, usd: 0 };
      return cells(h('tr', {},
        h('td', { class: 'model', title: MODEL_LABELS[m] || m, text: m }),
        h('td', { class: 'num', text: String(row.calls || 0) }),
        h('td', { class: 'num', text: row.frames !== undefined ? String(row.frames) : '—' }),
        h('td', { class: 'num', text: usd(row.usd) })), labels);
    });
    $('usage-models').tFoot.replaceChildren(cells(h('tr', {},
      h('td', { text: 'Total' }), h('td', { class: 'num', text: String(calls) }), h('td', { class: 'num', text: '' }),
      h('td', { class: 'num', text: usd(total) })), ['', 'Calls', '', 'Est. cost']));

    show('calls-empty', !recent.length);
    show('calls', recent.length > 0);
    const callLabels = ['Time', 'Model', 'What', 'Cost', 'Status'];
    keyed($('calls').tBodies[0], recent, (c) => `${c.at}:${c.model}:${c.purpose}:${c.ms}`, () => '1', (c) => cells(h('tr', {},
      h('td', { class: 'when', text: timeOnly(c.at) }),
      h('td', { class: 'model', text: c.model }),
      h('td', { text: c.purpose + (c.job_id ? ` · game ${c.job_id}` : '') + ` · ${dur((c.ms || 0) / 1000)}` }),
      h('td', { class: 'num', text: usd(c.cost_usd) }),
      h('td', { class: c.ok ? 'ok-text' : 'bad-text', title: c.error || '', text: c.ok ? 'OK' : `Failed (${c.status || 'no answer'})${c.error ? ': ' + c.error : ''}` })), callLabels));

    const ra = S.ra && S.ra.usage;
    const facts = [];
    if (ra) {
      const wa = ra.whatsapp || {};
      const graph = ra.meta_graph || {};
      facts.push(['WhatsApp template messages', String(wa.templates || 0)], ['WhatsApp replies (free)', String(wa.free_form || 0)],
        ['WhatsApp cost (est.)', usd(wa.usd)], ['Graph API steps', String(graph.steps || 0)],
        ['Reels uploaded', String(graph.uploads || 0)], ['Sent to Meta', size(graph.bytes || 0)]);
    } else {
      facts.push(['Phone approval', S.raError || 'Checking…']);
    }
    keyed($('usage-phone'), facts, (f) => f[0], (f) => f[1], (f) => h('li', {}, h('span', { text: f[0] }), h('span', { text: f[1] })));
  }

  // ---- start ---------------------------------------------------------------------------------

  function wireClicks() {
    for (const id of ['group-running', 'group-queued', 'group-failed', 'group-done', 'now-actions']) {
      $(id).addEventListener('click', (e) => {
        const button = e.target.closest('button[data-act^="job-"]');
        if (button) jobAction(button);
      });
    }
    $('events').addEventListener('click', (e) => {
      const link = e.target.closest('a[data-job]');
      if (!link) return;
      $('clips-job').value = link.dataset.job;
      if ($('clips-job').value !== link.dataset.job) $('clips-job').value = '';
      S.clipsStale = true;
    });
    $('events-filter').addEventListener('click', (e) => {
      const button = e.target.closest('.filter');
      if (!button) return;
      S.eventLevel = button.dataset.level;
      for (const b of $('events-filter').children) b.setAttribute('aria-pressed', String(b === button));
      $('events').replaceChildren();
      renderEvents();
    });
    $('offers').addEventListener('click', (e) => {
      const button = e.target.closest('button[data-act="offer"]');
      if (button) offerAction(button);
    });
    $('offers-refresh').addEventListener('click', (e) => act(e.currentTarget, 'offers-note', async () => {
      takeRA(await api('POST', '/remote-approval/refresh'));
      await loadReviews();
      note('offers-note', 'Checked with the relay.', 'ok');
    }));
    $('clips-job').addEventListener('change', () => { S.clipsStale = true; $('clip-groups').replaceChildren(); loadClips(); });
    $('clips-search').addEventListener('input', renderClips);
    $('clips-cats').addEventListener('click', (e) => {
      const button = e.target.closest('.filter');
      if (!button) return;
      S.clipCat = button.dataset.cat;
      renderClips();
    });
  }

  function start() {
    wireMenu();
    wireUpload();
    wireSetup();
    wireClicks();
    window.addEventListener('hashchange', () => showView(viewFromHash()));
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) { clearTimeout(S.timer); return; }
      if (S.connectPending) api('POST', '/remote-approval/refresh').then(takeRA).catch(() => {});
      tick();
    });
    showView(viewFromHash());
    tick();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
}());
