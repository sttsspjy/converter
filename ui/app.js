'use strict';

const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const TASKS = ['video', 'gif', 'audio', 'image'];
const TASK_TAB = { video: 'Video', gif: 'GIF', audio: 'Audio', image: 'Image' };
const ACTIVE = new Set(['queued', 'running']);

const S = {
  settings: null, caps: {}, qnames: [], rev: 0,
  jobs: new Map(), order: [], previews: {},
  tab: null, more: {}, expanded: new Set(), rows: new Map(), dirty: new Set(),
  missing: [], sources: {}, fetch: { active: null, queue: [] }, toolsRev: 0, version: '', repo: '',
};
let api;

// ── formatting ─────────────────────────────────────────────
const pad = (n) => String(n).padStart(2, '0');
function fmtBytes(b) {
  if (b == null) return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  const v = i === 0 ? b : b < 10 ? b.toFixed(2) : b < 100 ? b.toFixed(1) : Math.round(b);
  return `${v} ${u[i]}`;
}
function fmtDur(s) {
  if (s == null || isNaN(s)) return '';
  s = Math.max(0, s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = Math.floor(s % 60);
  if (!h && !m && s < 10) return `${s.toFixed(1)} s`;
  return h ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}
function fmtClock(s) {
  s = Math.max(0, s || 0);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const ss = sec.toFixed(1).padStart(4, '0');
  return h ? `${h}:${pad(m)}:${ss}` : `${m}:${ss}`;
}
function parseClock(str) {
  const parts = String(str).trim().split(':').map(Number);
  if (!parts.length || parts.some(isNaN)) return null;
  return parts.reduce((acc, p) => acc * 60 + p, 0);
}
function fmtEta(s) {
  if (s == null || !isFinite(s)) return '';
  s = Math.ceil(s);
  if (s < 60) return `${s}s left`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${pad(s % 60)}s left`;
  return `${Math.floor(s / 3600)}h ${pad(Math.floor((s % 3600) / 60))}m left`;
}
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;

// ── boot ───────────────────────────────────────────────────
window.addEventListener('pywebviewready', boot);

async function boot() {
  api = window.pywebview.api;
  const init = await api.init();
  S.settings = init.settings;
  S.qnames = init.quality_names;
  S.version = init.version;
  S.repo = init.repo;
  applyTools(init);
  bindStatic();
  renderDest();
  renderChrome();
  renderSetup();
  pollLoop();
  $('#about').textContent = `Converter ${S.version}  ·  github.com/${S.repo}`;
  setTimeout(async () => {
    const up = await api.check_update();
    if (!up) return;
    const pill = $('#update-pill');
    pill.textContent = `Version ${up.version} is out`;
    pill.title = 'Open the download page';
    pill.hidden = false;
    pill.onclick = () => api.open_url(up.url);
  }, 2500);
}

// ── external tools ─────────────────────────────────────────
function applyTools(info) {
  S.caps = info.caps;
  S.missing = info.missing;
  S.sources = info.sources;
  S.toolsRev = info.tools_rev;
}

function renderSetup() {
  const f = S.fetch;
  const needsFfmpeg = S.missing.includes('ffmpeg');
  const fetchingFfmpeg = f.active && f.queue.includes('ffmpeg');
  const show = needsFfmpeg || fetchingFfmpeg;
  $('#setup').hidden = !show;
  for (const id of ['#add-files', '#add-folder']) $(id).disabled = show;
  if (!show) { $('#empty').hidden = S.order.length > 0; return; }
  $('#empty').hidden = true;
  const want = ['ffmpeg', 'opusenc'].filter((n) => S.missing.includes(n));
  const total = want.reduce((a, n) => a + (S.sources[n] ? S.sources[n].size : 0), 0);
  $('#setup-size').textContent = `about ${fmtBytes(total)}`;
  $('#setup-progress').hidden = !f.active;
  $('#setup-actions').hidden = !!f.active;
  $('#setup-cancel').hidden = !f.active;
  $('#setup-go').textContent = f.error ? 'Try again' : 'Download';
  $('#setup-error').hidden = !(f.error && (f.failed === 'ffmpeg' || f.failed === 'opusenc'));
  $('#setup-error').textContent = f.error || '';
  if (f.active) {
    const pct = f.total ? Math.min(100, (f.done / f.total) * 100) : 0;
    $('#setup-bar').style.width = `${pct}%`;
    $('#setup-status').textContent = `Downloading ${S.sources[f.active].label}: ${fmtBytes(f.done)} of ${fmtBytes(f.total)}`;
  }
}

function fetchLine(name) {
  const f = S.fetch;
  if (f.active === name) {
    const pct = f.total ? Math.floor((f.done / f.total) * 100) : 0;
    return `<div class="hint">Downloading… ${pct}%</div>`;
  }
  const size = S.sources[name] ? fmtBytes(S.sources[name].size) : '';
  return `<button class="btn small" data-fetch="${name}" ${f.active ? 'disabled' : ''}>Download ${name} (${size})</button>` +
    (f.error && !f.active && f.failed === name ? `<div class="hint warn">${esc(f.error)}</div>` : '');
}

async function startFetch(names) {
  const ok = await api.fetch_tools(names);
  if (ok) { S.fetch = { active: names[0], queue: names, done: 0, total: 0 }; renderSetup(); }
}

async function pollLoop() {
  try {
    applySnapshot(await api.poll(S.rev));
  } catch (e) {
    console.error(e);
  }
  const busy = S.fetch.active || [...S.jobs.values()].some((j) => ACTIVE.has(j.status) || j.status === 'probing');
  setTimeout(pollLoop, busy ? 250 : 800);
}

function applySnapshot(snap) {
  let needPreview = false;
  for (const j of snap.jobs) {
    const prev = S.jobs.get(j.id);
    if (!prev || prev.status !== j.status || prev.task !== j.task || JSON.stringify(prev.trim) !== JSON.stringify(j.trim)) needPreview = true;
    if (prev && prev.status !== 'done' && j.status === 'done') S.dirty.add('__totals');
    S.jobs.set(j.id, j);
    S.dirty.add(j.id);
  }
  const orderChanged = snap.ids.join() !== S.order.join();
  if (orderChanged) {
    const keep = new Set(snap.ids);
    for (const id of [...S.jobs.keys()]) if (!keep.has(id)) S.jobs.delete(id);
    S.order = snap.ids;
    needPreview = true;
  }
  S.rev = snap.rev;
  const prevFetch = S.fetch;
  S.fetch = snap.fetch;
  if (snap.fetch.active || prevFetch.active || snap.fetch.error !== prevFetch.error) {
    renderSetup();
    const inline = $('#panel-body [data-fetch-slot]');
    if (inline) inline.innerHTML = fetchLine(inline.dataset.fetchSlot);
  }
  if (snap.tools_rev !== S.toolsRev) {
    api.tools_info().then((info) => {
      applyTools(info);
      renderSetup();
      renderPanel();
      schedulePreview();
      if (!info.missing.includes('ffmpeg') && prevFetch.queue && prevFetch.queue.includes('ffmpeg')) toast('All set. Drop some files in');
    });
  }
  if (snap.jobs.length || orderChanged) {
    renderRows(orderChanged);
    renderChrome();
  }
  if (needPreview) schedulePreview();
}

let previewTimer;
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(async () => {
    S.previews = await api.preview();
    S.order.forEach((id) => S.dirty.add(id));
    renderRows(false);
    renderEstimate();
  }, 120);
}

// ── rows ───────────────────────────────────────────────────
const GLYPH = {
  video: '<svg class="glyph" viewBox="0 0 16 16"><rect x="2" y="3.5" width="12" height="9" rx="1.5"/><path d="M7 6.2v3.6L10 8z"/></svg>',
  gif: '<svg class="glyph" viewBox="0 0 16 16"><rect x="2" y="3.5" width="12" height="9" rx="1.5"/><path d="M5 8h2M9 6.5v3M11 6.5h1.5M11 8h1"/></svg>',
  audio: '<svg class="glyph" viewBox="0 0 16 16"><path d="M6 11.5V4l7-1.5V10"/><circle cx="4.5" cy="11.5" r="1.5"/><circle cx="11.5" cy="10" r="1.5"/></svg>',
  image: '<svg class="glyph" viewBox="0 0 16 16"><rect x="2" y="3" width="12" height="10" rx="1.5"/><circle cx="6" cy="6.5" r="1"/><path d="M3 12l3.5-3.5 2.5 2.5 2-2 2.5 2.5"/></svg>',
};
const ICON = {
  x: '<svg viewBox="0 0 16 16"><path d="M4 4l8 8M12 4l-8 8"/></svg>',
  trim: '<svg viewBox="0 0 16 16"><circle cx="4.5" cy="4.5" r="2"/><circle cx="4.5" cy="11.5" r="2"/><path d="M6.2 5.6L13 11M6.2 10.4L13 5"/></svg>',
  open: '<svg viewBox="0 0 16 16"><path d="M5.5 4.5L11 8l-5.5 3.5z"/></svg>',
  folder: '<svg viewBox="0 0 16 16"><path d="M2 4.5h4l1.5 1.5H14v6.5H2z"/></svg>',
  retry: '<svg viewBox="0 0 16 16"><path d="M3 8a5 5 0 1 0 1.5-3.5M3 2.5V5h2.5"/></svg>',
  stop: '<svg viewBox="0 0 16 16"><rect x="4.5" y="4.5" width="7" height="7" rx="1"/></svg>',
};

function taskLabel(kind, task) {
  if (task === 'video') return 'Compress video';
  if (task === 'gif') return kind === 'gif' ? 'Compress GIF' : 'Make a GIF';
  if (task === 'audio') return kind === 'audio' ? 'Convert audio' : 'Extract audio';
  return 'Convert image';
}

function metaParts(j) {
  const i = j.info;
  if (!i) return [];
  const out = [];
  const codec = (c) => ({ h264: 'H.264', hevc: 'HEVC', av1: 'AV1', vp9: 'VP9', mpeg4: 'MPEG-4', prores: 'ProRes' }[c] || (c || '').toUpperCase());
  if (j.kind === 'video') {
    out.push(codec(i.vcodec) + (i.bit_depth >= 10 ? ` ${i.bit_depth}-bit` : '') + (i.hdr ? ' HDR' : ''));
    out.push(`${i.width}×${i.height}`);
    if (i.fps) out.push(`${Math.round(i.fps * 100) / 100} fps`);
    if (i.audio_tracks > 1) out.push(`${i.audio_tracks} audio tracks`);
  } else if (j.kind === 'gif') {
    out.push(`${i.width}×${i.height}`);
    if (i.fps) out.push(`${Math.round(i.fps)} fps`);
    if (i.frames) out.push(`${i.frames} frames`);
  } else if (j.kind === 'audio') {
    out.push(codec(i.acodec));
    const bits = i.audio_bits && i.audio_bits > 16 ? `${i.audio_bits}-bit ` : '';
    if (i.sample_rate) out.push(`${bits}${(i.sample_rate / 1000).toFixed(i.sample_rate % 1000 ? 1 : 0)} kHz`);
    else if (i.abitrate) out.push(`${Math.round(i.abitrate / 1000)} kbps`);
    const t = i.tags || {};
    if (t.artist || t.title) out.push([t.artist, t.title].filter(Boolean).join(' – '));
  } else if (j.kind === 'image') {
    out.push((i.format || '').replace('_pipe', '').toUpperCase());
    out.push(`${i.width}×${i.height}`);
  }
  return out;
}

function createRow(id) {
  const li = document.createElement('li');
  li.className = 'row';
  li.dataset.id = id;
  li.innerHTML = `
    <div class="thumb"></div>
    <div class="head"><span class="taskslot"></span><span class="name"></span></div>
    <div class="meta"></div>
    <div class="sizeline"><div class="sizebar"><i></i></div><div class="sizes"></div></div>
    <div class="status"></div>
    <div class="actions"></div>`;
  li.querySelector('.thumb').style.gridRow = '1 / span 4';
  li.querySelector('.actions').style.gridRow = '1 / span 4';
  li._ = {};
  return li;
}

function setOnce(li, key, value, fn) {
  if (li._[key] === value) return;
  li._[key] = value;
  fn();
}

function patchRow(li, j) {
  const task = j.task || j.kind || 'video';
  li.dataset.task = task;
  const larger = j.status === 'done' && j.out_size && j.info && j.out_size > j.info.size;
  li.className = `row is-${j.status}` + (j.status === 'cancelled' || j.status === 'unsupported' ? ' is-dim' : '') +
    (j.status === 'error' || j.status === 'unsupported' ? ' is-error' : '') + (larger ? ' is-larger' : '');

  // thumbnail
  setOnce(li, 'thumb', `${j.thumb ? 1 : 0}|${j.kind}`, () => {
    const dur = j.info && j.info.duration && j.kind !== 'image' ? `<span class="dur">${fmtDur(j.info.duration)}</span>` : '';
    li.querySelector('.thumb').innerHTML = (j.thumb ? `<img src="${j.thumb}" alt="">` : GLYPH[j.kind || 'video'] || GLYPH.video) + dur;
  });

  // task + name
  const editable = !ACTIVE.has(j.status) && j.status !== 'probing';
  setOnce(li, 'task', `${j.task}|${(j.tasks || []).join()}|${editable}|${j.kind}`, () => {
    const slot = li.querySelector('.taskslot');
    if (!j.task) { slot.innerHTML = ''; return; }
    if (j.tasks.length > 1 && editable) {
      slot.innerHTML = `<select class="task-pick" data-act="task" aria-label="What to do with this file">${j.tasks
        .map((t) => `<option value="${t}" ${t === j.task ? 'selected' : ''}>${taskLabel(j.kind, t)}</option>`).join('')}</select>`;
    } else {
      slot.innerHTML = `<span class="task-tag">${taskLabel(j.kind, j.task)}</span>`;
    }
  });
  setOnce(li, 'name', j.name + j.rel_dir, () => {
    const n = li.querySelector('.name');
    n.innerHTML = (j.rel_dir ? `<span class="dir">${esc(j.rel_dir.replace(/\\/g, '/'))}/</span>` : '') + esc(j.name);
    n.title = j.path;
  });
  setOnce(li, 'meta', j.info ? 'i' : '-', () => {
    li.querySelector('.meta').innerHTML = metaParts(j).map((p) => `<span>${esc(p)}</span>`).join('');
  });

  // size bar + sizes
  const bar = li.querySelector('.sizebar i');
  const insize = j.info ? j.info.size : null;
  let width = 0;
  if (j.status === 'running') width = j.progress * 100;
  else if (j.status === 'done' && j.out_size && insize) width = Math.min(100, (j.out_size / insize) * 100);
  else if (j.status === 'done') width = 100;
  bar.style.width = `${width}%`;

  const pv = S.previews[j.id] || {};
  let sizes = '';
  if (j.status === 'running') sizes = `<b>${Math.floor(j.progress * 100)}%</b>`;
  else if (j.status === 'done' && j.out_size != null && insize) {
    const delta = Math.round((1 - j.out_size / insize) * 100);
    const tag = delta === 0 ? 'same size' : delta > 0 ? `−${delta}%` : `+${-delta}%`;
    sizes = `${fmtBytes(insize)} → <b>${fmtBytes(j.out_size)}</b><span class="saved">${tag}</span>`;
  } else if (insize != null) {
    sizes = `<b>${fmtBytes(insize)}</b>` + (pv.estimate && j.status === 'ready' ? ` → about ${fmtBytes(pv.estimate)}` : '');
  }
  li.querySelector('.sizes').innerHTML = sizes;

  // status line
  let st = '';
  const trimTxt = j.trim ? `<span class="trimmed">Trimmed ${fmtDur(j.trim[0])}–${fmtDur(j.trim[1] ?? j.info.duration)}</span>` : '';
  switch (j.status) {
    case 'probing': st = 'Reading file…'; break;
    case 'unsupported': st = `<span class="err">${esc(j.error)}</span>`; break;
    case 'ready':
      st = pv.problem ? `<span class="problem">${esc(pv.problem)}</span>` : `<span class="plan">${esc(pv.summary || '')}</span>`;
      st += trimTxt;
      break;
    case 'queued': st = 'Waiting for the encoder' + (trimTxt ? ' ' + trimTxt : ''); break;
    case 'running': {
      const bits = [esc(j.stage)];
      if (j.speed && task === 'video') bits.push(`${j.speed.toFixed(j.speed < 10 ? 2 : 0)}× speed`);
      if (j.eta) bits.push(fmtEta(j.eta));
      st = bits.join('<span style="color:var(--faint)">  ·  </span>');
      if (j.summary) st += `<span class="plan" style="color:var(--faint)">${esc(j.summary)}</span>`;
      break;
    }
    case 'done':
      if (j.out) {
        const outName = j.out.split(/[\\/]/).pop();
        st = `<a data-act="open" title="${esc(j.out)}">${esc(outName)}</a>`;
        if (j.summary) st += `<span class="plan" style="color:var(--faint)">${esc(j.summary)}</span>`;
      } else st = esc(j.stage || 'Done');
      if (larger) st += '<span class="warn">Bigger than the original. The source was already efficient</span>';
      if (j.warning) st += `<span class="warn">${esc(j.warning)}</span>`;
      break;
    case 'error':
      st = `<span class="err">${esc(j.error || 'Failed')}</span>` + (j.log ? `<a data-act="log">${S.expanded.has(j.id) ? 'Hide details' : 'Details'}</a>` : '');
      break;
    case 'cancelled': st = 'Cancelled'; break;
  }
  li.querySelector('.status').innerHTML = st;

  // actions
  const canTrim = j.kind === 'video' && (j.task === 'video' || j.task === 'gif' || j.task === 'audio');
  setOnce(li, 'actions', `${j.status}|${canTrim}|${!!j.out}`, () => {
    const b = (act, icon, title, cls = '') => `<button class="icon-btn ${cls}" data-act="${act}" title="${title}">${ICON[icon]}</button>`;
    let h = '';
    if (j.status === 'ready') h = (canTrim ? b('trim', 'trim', 'Trim') : '') + b('remove', 'x', 'Remove from list', 'danger');
    else if (ACTIVE.has(j.status)) h = b('cancel', 'stop', j.status === 'queued' ? 'Don’t convert' : 'Cancel', 'danger');
    else if (j.status === 'done') h = (j.out ? b('open', 'open', 'Open') + b('reveal', 'folder', 'Show in folder') : '') + b('remove', 'x', 'Remove from list');
    else if (j.status === 'error' || j.status === 'cancelled') h = (canTrim ? b('trim', 'trim', 'Trim') : '') + b('retry', 'retry', 'Try again') + b('remove', 'x', 'Remove from list', 'danger');
    else h = b('remove', 'x', 'Remove from list', 'danger');
    li.querySelector('.actions').innerHTML = h;
  });

  // error log
  let log = li.querySelector('.log');
  if (S.expanded.has(j.id) && j.log) {
    if (!log) { log = document.createElement('pre'); log.className = 'log'; li.appendChild(log); }
    log.textContent = j.log;
  } else if (log) log.remove();
}

function renderRows(orderChanged) {
  const list = $('#rows');
  for (const id of S.order) {
    let li = S.rows.get(id);
    if (!li) { li = createRow(id); S.rows.set(id, li); S.dirty.add(id); }
    if (S.dirty.has(id)) patchRow(li, S.jobs.get(id));
  }
  for (const [id, li] of S.rows) if (!S.jobs.has(id)) { li.remove(); S.rows.delete(id); }
  if (orderChanged) S.order.forEach((id) => list.appendChild(S.rows.get(id)));
  S.dirty.clear();
  $('#empty').hidden = S.order.length > 0 || !$('#setup').hidden;
}

// ── chrome: toolbar, tabs, footer ──────────────────────────
function renderChrome() {
  const jobs = [...S.jobs.values()];
  const live = jobs.filter((j) => j.status !== 'unsupported');
  const total = live.reduce((a, j) => a + (j.info ? j.info.size : 0), 0);
  $('#queue-count').textContent = jobs.length ? `${plural(jobs.length, 'file')}${total ? ', ' + fmtBytes(total) : ''}` : '';
  const done = jobs.filter((j) => j.status === 'done');
  const active = jobs.filter((j) => ACTIVE.has(j.status));
  const ready = jobs.filter((j) => j.status === 'ready' && !(S.previews[j.id] || {}).problem);
  $('#clear-done').hidden = !done.length;
  $('#clear-all').hidden = !jobs.length || jobs.length === active.length;

  const start = $('#start');
  start.disabled = !ready.length;
  start.textContent = ready.length ? `Convert ${plural(ready.length, 'file')}` : active.length ? 'Converting…' : 'Convert';
  $('#cancel-all').hidden = !active.length;

  // totals
  let t = '';
  if (active.length) {
    const etas = active.filter((j) => j.status === 'running' && j.eta).map((j) => j.eta);
    t = `${plural(active.length, 'file')} to go` + (etas.length && active.length === etas.length ? `, ${fmtEta(Math.max(...etas))}` : '');
  } else if (done.length) {
    const withOut = done.filter((j) => j.out_size != null && j.info);
    const a = withOut.reduce((s, j) => s + j.info.size, 0), b = withOut.reduce((s, j) => s + j.out_size, 0);
    if (a > b) t = `Saved <span class="saved">${fmtBytes(a - b)}</span> across ${plural(withOut.length, 'file')}`;
    else if (withOut.length) t = `${plural(withOut.length, 'file')} converted`;
  }
  $('#totals').innerHTML = t;

  renderTabs();
}

function taskCounts() {
  const m = new Map();
  for (const j of S.jobs.values()) if (j.task && j.status !== 'unsupported' && j.status !== 'done') m.set(j.task, (m.get(j.task) || 0) + 1);
  return m;
}

let lastTabsKey = '';
function renderTabs() {
  const counts = taskCounts();
  const shown = counts.size ? TASKS.filter((t) => counts.has(t)) : TASKS;
  if (!shown.includes(S.tab)) S.tab = shown[0];
  const key = shown.map((t) => `${t}:${counts.get(t) || 0}`).join() + '|' + S.tab;
  if (key === lastTabsKey) return;
  const tabChanged = !lastTabsKey.endsWith('|' + S.tab);
  lastTabsKey = key;
  $('#tabs').innerHTML = shown.map((t) => `<button class="tab" role="tab" data-tab="${t}" style="--c: var(--${t})" aria-selected="${t === S.tab}">
      ${TASK_TAB[t]}${counts.get(t) ? `<span class="n">${counts.get(t)}</span>` : ''}</button>`).join('');
  if (tabChanged) renderPanel();
}

function renderDest() {
  const o = S.settings.output;
  document.querySelectorAll('#dest-mode button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.v === o.mode)));
  const pb = $('#dest-folder');
  pb.hidden = o.mode !== 'folder';
  pb.textContent = o.folder || 'Choose a folder…';
  pb.classList.toggle('unset', !o.folder);
  pb.title = o.folder ? `${o.folder}\nClick to change` : 'Choose output folder';
  $('#conflict').value = o.conflict || 'rename';
}

// ── settings panel ─────────────────────────────────────────
const val = (v) => esc(JSON.stringify(v));

function seg(task, key, options, opts = {}) {
  const cur = opts.current !== undefined ? opts.current : S.settings[task][key];
  return `<div class="seg ${opts.stack ? 'stack' : ''}" role="group">${options.map(([v, label, sub, disabled]) =>
    `<button data-task="${task}" data-k="${key}" data-v="${val(v)}" aria-pressed="${v === cur}" ${disabled ? 'disabled' : ''}>${esc(label)}${sub ? `<small>${esc(sub)}</small>` : ''}</button>`).join('')}</div>`;
}
function slider(task, key, min, max, step) {
  return `<input type="range" class="slider" data-task="${task}" data-k="${key}" min="${min}" max="${max}" step="${step}" value="${S.settings[task][key]}">`;
}
function num(task, key, unit, min, max, step = 1) {
  return `<div class="num"><input type="number" data-task="${task}" data-k="${key}" min="${min}" max="${max}" step="${step}" value="${S.settings[task][key]}"><span>${unit}</span></div>`;
}
function check(task, key, label, sub) {
  return `<label class="check"><input type="checkbox" data-task="${task}" data-k="${key}" ${S.settings[task][key] ? 'checked' : ''}>
    <span>${esc(label)}${sub ? `<small>${esc(sub)}</small>` : ''}</span></label>`;
}
function field(label, control, hint, value) {
  return `<div class="field"><div class="label">${esc(label)}${value ? `<span class="value" data-live>${esc(value)}</span>` : ''}</div>${control}${hint ? `<div class="hint">${hint}</div>` : ''}</div>`;
}
function more(task, inner) {
  return `<details class="more" data-more="${task}" ${S.more[task] ? 'open' : ''}><summary><svg viewBox="0 0 16 16"><path d="M6 4l4 4-4 4"/></svg>More options</summary>${inner}</details>`;
}

const QUALITY_HINT = [
  'Indistinguishable from the source. Big files.',
  'Hard to tell apart from the source.',
  'Looks the same at normal viewing. The sweet spot.',
  'Some softening in fine detail and dark scenes.',
  'Visible artifacts. Only when size matters most.',
];
const CODEC_HINT = {
  hevc: 'About half the size of H.264. Plays on modern phones, TVs and Windows with the HEVC extension.',
  h264: 'Plays absolutely everywhere, including old devices and every browser.',
  av1: 'Smallest files. Plays in browsers, Discord and recent devices; older hardware may stutter.',
};
const OPUS_HINT = { 64: 'Speech and podcasts.', 96: 'Fine for casual listening.', 128: 'Transparent for most music. Recommended.', 160: 'Transparent for nearly everyone.', 192: 'Headroom for critical listening.', 256: 'Archival headroom.' };

const PANELS = {
  video(s) {
    const gpuOk = !!(S.caps.nvenc || {})[s.codec];
    const enc = gpuOk ? s.encoder : 'cpu';
    let h = field('Goal', seg('video', 'mode', [['quality', 'Keep the look', 'smallest file at a chosen quality'], ['size', 'Fit a file size', 'for upload limits']], { stack: true }));
    if (s.mode === 'quality') {
      h += field('Quality', slider('video', 'quality', 0, 4, 1) + `<div class="ticks"><span>Best</span><span>Smallest</span></div>`, `<span data-live-hint>${QUALITY_HINT[s.quality]}</span>`, S.qnames[s.quality]);
    } else {
      const presets = [[10, '10 MB'], [25, '25 MB'], [50, '50 MB'], [100, '100 MB'], [500, '500 MB']];
      h += field('Target size', seg('video', 'target_mb', presets) + `<div style="margin-top:8px">${num('video', 'target_mb', 'MB, or type any size', 1, 100000, 0.5)}</div>`,
        'Discord allows 10 MB, or 500 MB with Nitro. The result lands just under the target.');
      h += check('video', 'auto_scale', 'Lower the resolution when needed', 'Keeps it sharp instead of blocky when the size budget is tight');
    }
    h += field('Codec', seg('video', 'codec', [['hevc', 'HEVC'], ['h264', 'H.264'], ['av1', 'AV1']]), CODEC_HINT[s.codec]);
    h += field('Max resolution', seg('video', 'resolution', [[0, 'Original'], [2160, '4K'], [1440, '1440p'], [1080, '1080p'], [720, '720p'], [480, '480p']]),
      s.resolution ? 'Never upscales. Portrait videos are measured by their short side too.' : '');
    h += field('Encoder', seg('video', 'encoder', [['gpu', 'GPU', '', !gpuOk], ['cpu', 'CPU']], { current: enc }),
      !gpuOk ? 'No NVIDIA encoder for this codec here, so the CPU does it.'
        : enc === 'gpu' ? 'NVENC: many times faster. Files run a little larger at the same quality.'
          : 'Much slower, but around 10–20% smaller at the same quality.');
    let m = field('Encoder speed', seg('video', 'speed', [['fast', 'Faster'], ['balanced', 'Balanced'], ['small', 'Smaller file']]));
    m += field('Frame rate', seg('video', 'fps', [[0, 'Original'], [60, '60'], [30, '30'], [24, '24']]), s.fps ? 'Only lowers it, never raises.' : '');
    m += field('Audio', seg('video', 'audio', [['auto', 'Auto'], ['aac', 'AAC'], ['opus', 'Opus'], ['none', 'Remove']]),
      s.audio === 'auto' ? 'Copies the original audio untouched when it fits, otherwise AAC.' : s.audio === 'none' ? 'The output will be silent.' : '');
    if (s.audio === 'aac' || s.audio === 'opus' || (s.audio === 'auto' && s.mode === 'size')) {
      m += field('Audio bitrate', seg('video', 'audio_kbps', [[64, '64'], [96, '96'], [128, '128'], [160, '160'], [192, '192']]), s.mode === 'size' ? 'Lowered automatically when the size budget is tight.' : 'kbps');
    }
    m += field('Container', seg('video', 'container', [['mp4', 'MP4'], ['mkv', 'MKV']]), s.container === 'mkv' ? 'Keeps subtitles and every audio track.' : 'Plays anywhere. Subtitles are dropped.');
    return h + more('video', m);
  },

  gif(s) {
    const isGif = s.format === 'gif';
    let h = field('Output', seg('gif', 'format', [['gif', 'GIF'], ['webp', 'WebP'], ['mp4', 'MP4']]), {
      gif: 'Stays a GIF, so it works anywhere a GIF does.',
      webp: 'Animated WebP: usually 3–5× smaller. Works in browsers, Discord and Telegram.',
      mp4: 'A looping silent video, often 10× smaller. For places that don’t need a real GIF.',
    }[s.format]);
    h += field('Longest side', seg('gif', 'max_side', [[0, 'Original'], [800, '800'], [640, '640'], [480, '480'], [320, '320']]), 'Pixels. Halving the size cuts the file to roughly a quarter.');
    h += field('Frame rate', seg('gif', 'fps', [[0, 'Original'], [30, '30'], [20, '20'], [15, '15'], [10, '10']]));
    if (isGif) {
      h += field('Colors', seg('gif', 'colors', [[256, '256'], [128, '128'], [64, '64'], [32, '32']]));
      h += field('Dithering', seg('gif', 'dither', [['sierra2_4a', 'Smooth'], ['bayer', 'Pattern'], ['none', 'None']]), 'Smooth looks best. Pattern and None compress better on flat art.');
      if (S.caps.gifsicle) {
        h += field('Lossy compression', slider('gif', 'lossy', 0, 200, 10), 'Higher is smaller and grainier. Up to about 80 is usually invisible.', s.lossy ? String(s.lossy) : 'Off');
      } else {
        h += `<div class="callout" style="--c: var(--gif)"><b>Lossy GIF compression is off.</b> It needs gifsicle, a small tool
          that typically cuts another 30–60%.<div data-fetch-slot="gifsicle">${fetchLine('gifsicle')}</div></div>`;
      }
    } else {
      h += field('Quality', slider('gif', 'quality', 10, 100, 5), '', String(s.quality));
    }
    h += check('gif', 'limit_on', 'Keep it under a size', 'Shrinks the dimensions step by step until it fits');
    if (s.limit_on) h += `<div class="field" style="padding-top:8px">${num('gif', 'limit_mb', 'MB', 0.1, 1000, 0.1)}</div>`;
    return h;
  },

  audio(s) {
    let h = field('Format', seg('audio', 'format', [['opus', 'Opus', 'smallest, transparent at 128'], ['mp3', 'MP3', 'plays on anything'], ['aac', 'AAC (.m4a)', 'Apple devices'], ['flac', 'FLAC', 'lossless']], { stack: true }));
    if (s.format === 'opus') {
      if (!S.caps.opusenc) h += `<div class="callout" style="--c: var(--audio)"><b>Opus needs opusenc,</b> the official Opus encoder.
        It carries tags and cover art over intact.<div data-fetch-slot="opusenc">${fetchLine('opusenc')}</div></div>`;
      h += field('Bitrate', seg('audio', 'opus_kbps', [[64, '64'], [96, '96'], [128, '128'], [160, '160'], [192, '192'], [256, '256']]), `kbps. ${OPUS_HINT[s.opus_kbps] || ''}`);
    } else if (s.format === 'mp3') {
      h += field('Bitrate', seg('audio', 'mp3_kbps', [[128, '128'], [192, '192'], [256, '256'], [320, '320']]), 'kbps');
    } else if (s.format === 'aac') {
      h += field('Bitrate', seg('audio', 'aac_kbps', [[128, '128'], [192, '192'], [256, '256'], [320, '320']]), 'kbps');
    }
    h += field('Cover art', seg('audio', 'cover', [['keep', 'Keep'], ['shrink', 'Shrink'], ['remove', 'Remove']]), {
      keep: 'Embedded exactly as it is. Tags always carry over.',
      shrink: 'Re-saved as an 800 px JPEG. Full-size scans can add several MB per track.',
      remove: 'No embedded art. Tags still carry over.',
    }[s.cover]);
    if (S.settings.output.mode === 'folder') h += check('output', 'copy_art', 'Copy folder art', 'cover.jpg, folder.jpg and similar go along when you convert a folder');
    return h + '<div class="estimate" id="estimate"></div>';
  },

  image(s) {
    let h = field('Format', seg('image', 'format', [['webp', 'WebP'], ['avif', 'AVIF'], ['jpg', 'JPG'], ['png', 'PNG']]), {
      webp: 'Small and supported everywhere modern.', avif: 'Smallest, a little slower to encode.', jpg: 'Maximum compatibility. Transparency becomes white.', png: 'Lossless.',
    }[s.format]);
    if (s.format !== 'png') h += field('Quality', slider('image', 'quality', 30, 100, 1), '', String(s.quality));
    h += field('Longest side', seg('image', 'max_side', [[0, 'Original'], [3840, '3840'], [2560, '2560'], [1920, '1920'], [1280, '1280']]), 'Pixels. Never upscales.');
    return h;
  },
};

function renderPanel() {
  const body = $('#panel-body');
  body.style.setProperty('--c', `var(--${S.tab})`);
  const top = body.scrollTop;
  body.innerHTML = PANELS[S.tab](S.settings[S.tab]);
  body.scrollTop = top;
  renderEstimate();
}

function renderEstimate() {
  const el = $('#estimate');
  if (!el) return;
  const jobs = [...S.jobs.values()].filter((j) => j.task === 'audio' && j.status === 'ready');
  const ests = jobs.map((j) => (S.previews[j.id] || {}).estimate).filter(Boolean);
  if (!jobs.length || ests.length !== jobs.length) { el.innerHTML = ''; return; }
  const before = jobs.reduce((a, j) => a + j.info.size, 0);
  const after = ests.reduce((a, b) => a + b, 0);
  el.innerHTML = `About <b>${fmtBytes(after)}</b> for ${plural(jobs.length, 'track')}, down from ${fmtBytes(before)}.`;
}

async function setSetting(task, key, value) {
  S.settings[task][key] = value;
  if (task === 'output') renderDest();
  renderPanel();
  S.settings = await api.save_settings({ [task]: { [key]: value } });
  schedulePreview();
}

// ── trim dialog ────────────────────────────────────────────
const T = { id: null, dur: 0, a: 0, b: 0, frames: false, frameTimer: null };

async function openTrim(id) {
  const j = S.jobs.get(id);
  if (!j || !j.info) return;
  T.id = id;
  T.dur = j.info.duration;
  T.a = j.trim ? j.trim[0] : 0;
  T.b = j.trim && j.trim[1] != null ? j.trim[1] : T.dur;
  T.frames = false;
  $('#trim-title').textContent = `Trim ${j.name}`;
  const video = $('#trim-video'), img = $('#trim-frame');
  video.hidden = false; img.hidden = true;
  video.onerror = () => { T.frames = true; video.hidden = true; img.hidden = false; showFrame(T.a); };
  video.src = await api.media_url(id);
  video.currentTime = T.a;
  updateTrim();
  $('#trim').showModal();
}

function showFrame(t) {
  clearTimeout(T.frameTimer);
  T.frameTimer = setTimeout(async () => {
    const uri = await api.frame(T.id, t);
    if (uri) $('#trim-frame').src = uri;
  }, 90);
  T.cursor = t;
}
function seekTo(t) {
  if (T.frames) showFrame(t);
  else $('#trim-video').currentTime = t;
}
function currentTime() {
  return T.frames ? (T.cursor ?? T.a) : $('#trim-video').currentTime;
}
function updateTrim() {
  const pct = (t) => (T.dur ? (t / T.dur) * 1000 : 0);
  $('#trim-a').value = pct(T.a);
  $('#trim-b').value = pct(T.b);
  $('#trim-fill').style.left = `${pct(T.a) / 10}%`;
  $('#trim-fill').style.width = `${(pct(T.b) - pct(T.a)) / 10}%`;
  if (document.activeElement !== $('#trim-start')) $('#trim-start').value = fmtClock(T.a);
  if (document.activeElement !== $('#trim-end')) $('#trim-end').value = fmtClock(T.b);
  $('#trim-length').innerHTML = `<b>${fmtClock(T.b - T.a)}</b> of ${fmtClock(T.dur)}`;
}
function closeTrimMedia() {
  const v = $('#trim-video');
  v.pause();
  v.onerror = null;
  v.removeAttribute('src');
  v.load();
}

// ── events ─────────────────────────────────────────────────
let toastTimer;
function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 2600);
}

window.onFilesAdded = (r) => {
  if (!r) return;
  if (r.needs_ffmpeg) { toast('Download ffmpeg first, using the button in the middle'); return; }
  if (r.added) toast(`Added ${plural(r.added, 'file')}` + (r.skipped ? ` (skipped ${r.skipped} that aren’t media or are already listed)` : ''));
  else if (r.skipped) toast('Nothing added. Those aren’t media files, or they’re already in the list');
};

function bindStatic() {
  const addFiles = async () => window.onFilesAdded(await api.pick_files());
  const addFolder = async () => window.onFilesAdded(await api.pick_folder());
  $('#add-files').onclick = addFiles;
  $('#add-folder').onclick = addFolder;
  document.querySelectorAll('[data-action="add-files"]').forEach((b) => (b.onclick = addFiles));
  document.querySelectorAll('[data-action="add-folder"]').forEach((b) => (b.onclick = addFolder));
  $('#clear-done').onclick = () => api.clear('finished');
  $('#clear-all').onclick = () => api.clear('all');
  $('#start').onclick = async () => {
    const n = await api.start(null);
    if (n) toast(`Converting ${plural(n, 'file')}`);
  };
  $('#cancel-all').onclick = () => api.cancel(null);

  // destination
  $('#dest-mode').onclick = async (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    if (b.dataset.v === 'folder' && !S.settings.output.folder) {
      const s = await api.choose_output_folder();
      if (s) S.settings = s;
    } else {
      S.settings = await api.save_settings({ output: { mode: b.dataset.v } });
    }
    renderDest();
    if (S.tab === 'audio') renderPanel();
  };
  $('#dest-folder').onclick = async () => {
    const s = await api.choose_output_folder();
    if (s) { S.settings = s; renderDest(); }
  };
  $('#conflict').onchange = async (e) => {
    S.settings = await api.save_settings({ output: { conflict: e.target.value } });
  };

  // tabs
  $('#tabs').onclick = (e) => {
    const b = e.target.closest('[data-tab]');
    if (!b || b.dataset.tab === S.tab) return;
    S.tab = b.dataset.tab;
    lastTabsKey = '';
    renderTabs();
    renderPanel();
  };

  // panel controls
  $('#setup-go').onclick = () => startFetch(['ffmpeg', 'opusenc'].filter((n) => S.missing.includes(n)));
  $('#setup-cancel').onclick = () => api.cancel_fetch();
  $('#about').onclick = () => api.open_url(`https://github.com/${S.repo}`);

  const body = $('#panel-body');
  body.addEventListener('click', (e) => {
    const fb = e.target.closest('button[data-fetch]');
    if (fb) { startFetch([fb.dataset.fetch]); return; }
    const b = e.target.closest('button[data-k]');
    if (!b) return;
    setSetting(b.dataset.task, b.dataset.k, JSON.parse(b.dataset.v));
  });
  body.addEventListener('input', (e) => {
    const t = e.target;
    if (t.type !== 'range' || !t.dataset.k) return;
    const v = Number(t.value);
    const live = t.closest('.field').querySelector('[data-live]');
    if (t.dataset.task === 'video' && t.dataset.k === 'quality') {
      if (live) live.textContent = S.qnames[v];
      const hint = t.closest('.field').querySelector('[data-live-hint]');
      if (hint) hint.textContent = QUALITY_HINT[v];
    } else if (live) live.textContent = t.dataset.k === 'lossy' && !v ? 'Off' : String(v);
  });
  body.addEventListener('change', (e) => {
    const t = e.target;
    if (!t.dataset.k) return;
    let v;
    if (t.type === 'checkbox') v = t.checked;
    else if (t.type === 'range') v = Number(t.value);
    else if (t.type === 'number') {
      v = Number(t.value);
      if (!isFinite(v) || v <= 0) { t.value = S.settings[t.dataset.task][t.dataset.k]; return; }
    } else return;
    setSetting(t.dataset.task, t.dataset.k, v);
  });
  body.addEventListener('toggle', (e) => {
    if (e.target.dataset && e.target.dataset.more) S.more[e.target.dataset.more] = e.target.open;
  }, true);

  // row actions
  const rows = $('#rows');
  rows.addEventListener('click', (e) => {
    const a = e.target.closest('[data-act]');
    if (!a || a.tagName === 'SELECT') return;
    const id = a.closest('.row').dataset.id;
    const j = S.jobs.get(id);
    switch (a.dataset.act) {
      case 'remove': api.remove([id]); break;
      case 'cancel': api.cancel([id]); break;
      case 'retry': api.start([id]); break;
      case 'trim': openTrim(id); break;
      case 'open': api.open_path(j.out); break;
      case 'reveal': api.reveal(j.out); break;
      case 'log':
        S.expanded.has(id) ? S.expanded.delete(id) : S.expanded.add(id);
        S.dirty.add(id); renderRows(false);
        break;
    }
  });
  rows.addEventListener('change', (e) => {
    if (e.target.dataset.act === 'task') api.set_task(e.target.closest('.row').dataset.id, e.target.value);
  });
  rows.addEventListener('dblclick', (e) => {
    const row = e.target.closest('.row');
    if (!row || e.target.closest('button, select, a')) return;
    const j = S.jobs.get(row.dataset.id);
    if (j && j.out) api.reveal(j.out);
  });

  // drag & drop (paths are collected by the Python side)
  let depth = 0;
  document.addEventListener('dragenter', (e) => { e.preventDefault(); depth++; document.body.classList.add('dragging'); });
  document.addEventListener('dragleave', () => { if (--depth <= 0) { depth = 0; document.body.classList.remove('dragging'); } });
  document.addEventListener('dragover', (e) => e.preventDefault());
  document.addEventListener('drop', () => { depth = 0; document.body.classList.remove('dragging'); });

  // keyboard
  document.addEventListener('keydown', (e) => {
    if ($('#trim').open) return;
    if (e.ctrlKey && e.key.toLowerCase() === 'o') { e.preventDefault(); e.shiftKey ? addFolder() : addFiles(); }
    if (e.ctrlKey && e.key === 'Enter' && !$('#start').disabled) { e.preventDefault(); $('#start').click(); }
  });

  // trim dialog
  const dlg = $('#trim');
  const onRange = (which) => (e) => {
    const t = (Number(e.target.value) / 1000) * T.dur;
    if (which === 'a') T.a = Math.min(t, T.b - 0.1);
    else T.b = Math.max(t, T.a + 0.1);
    updateTrim();
    seekTo(which === 'a' ? T.a : T.b);
  };
  $('#trim-a').addEventListener('input', onRange('a'));
  $('#trim-b').addEventListener('input', onRange('b'));
  $('#trim-start').addEventListener('change', (e) => {
    const t = parseClock(e.target.value);
    if (t != null) { T.a = Math.max(0, Math.min(t, T.b - 0.1)); seekTo(T.a); }
    e.target.blur(); updateTrim();
  });
  $('#trim-end').addEventListener('change', (e) => {
    const t = parseClock(e.target.value);
    if (t != null) { T.b = Math.min(T.dur, Math.max(t, T.a + 0.1)); seekTo(T.b); }
    e.target.blur(); updateTrim();
  });
  for (const id of ['#trim-start', '#trim-end']) {
    $(id).addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); e.target.blur(); } });
  }
  $('#trim-set-start').onclick = () => { T.a = Math.min(currentTime(), T.b - 0.1); updateTrim(); };
  $('#trim-set-end').onclick = () => { T.b = Math.max(currentTime(), T.a + 0.1); updateTrim(); };
  $('#trim-reset').onclick = () => { T.a = 0; T.b = T.dur; updateTrim(); seekTo(0); };
  dlg.addEventListener('close', () => {
    if (dlg.returnValue === 'ok') api.set_trim(T.id, T.a, T.b);
    closeTrimMedia();
    dlg.returnValue = '';
  });
}
